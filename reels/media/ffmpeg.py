"""Тонкие обёртки над ffmpeg/ffprobe."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"}


class FFmpegError(RuntimeError):
    pass


def run(args: list[str], timeout: float | None = 600) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    log.debug("ffmpeg %s", " ".join(args))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-2000:]}")
    return proc


async def arun(args: list[str], timeout: float | None = 600) -> None:
    """Асинхронный запуск — не блокирует event loop бота."""
    await asyncio.to_thread(run, args, timeout)


@dataclass
class ProbeInfo:
    kind: str  # video | photo | audio
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    has_video: bool
    rotation: int = 0

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation in (90, 270, -90, -270):
            return self.height, self.width
        return self.width, self.height


def probe(path: str | Path) -> ProbeInfo:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {proc.stderr[-500:]}")
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or (v or a or {}).get("duration") or 0.0)

    is_image = Path(path).suffix.lower() in IMAGE_EXT or (
        v is not None and (v.get("codec_name") in {"mjpeg", "png", "webp", "bmp"}) and duration < 0.1
    )
    width = int(v.get("width", 0)) if v else 0
    height = int(v.get("height", 0)) if v else 0
    fps = 0.0
    rotation = 0
    if v:
        num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
        try:
            fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        except ValueError:
            fps = 0.0
        rotation = int(v.get("tags", {}).get("rotate", 0) or 0)
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(sd["rotation"])

    if v is None:
        kind = "audio"
    elif is_image:
        kind = "photo"
        duration = 0.0
    else:
        kind = "video"
    return ProbeInfo(
        kind=kind,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_audio=a is not None,
        has_video=v is not None,
        rotation=rotation,
    )


def make_proxy(src: str | Path, dst: str | Path, height: int = 640) -> None:
    """Лёгкая копия для анализа и превью: ≤640px по высоте, 30 fps, быстрый пресет.

    Поворот из метаданных применяется автоматически (autorotate ffmpeg).
    """
    run(
        [
            "-i", str(src),
            "-vf", f"scale=-2:'min({height},ih)':flags=bicubic,fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-ar", "48000",
            "-movflags", "+faststart",
            str(dst),
        ]
    )


def normalize_photo(src: str | Path, dst: str | Path, max_side: int = 3000) -> None:
    """Фото -> JPEG с применённым EXIF-поворотом и ограниченным размером."""
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_side, max_side))
        im.save(dst, "JPEG", quality=92)


def extract_frame(src: str | Path, t: float, dst: str | Path, height: int = 512) -> None:
    run(["-ss", f"{max(t, 0):.3f}", "-i", str(src), "-frames:v", "1", "-vf", f"scale=-2:{height}", str(dst)])


def decode_audio_mono(src: str | Path, sr: int = 22050):
    """Аудио -> numpy float32 моно."""
    import numpy as np

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.decode(errors="ignore")[-500:])
    return np.frombuffer(proc.stdout, dtype=np.float32)
