"""Компилятор EDL -> ffmpeg.

Рендер в два этапа:
1. Каждый видеоклип рендерится в промежуточный файл нужного размера и точной длины
   (кадрирование, зум, скорость). Ключ кеша — хэш параметров клипа, поэтому при правках
   перерендериваются только изменённые клипы.
2. Сборка: склейки/переходы (concat/xfade), цвет, текст и субтитры (ASS), звук
   (речь клипов + музыка с приглушением под речь), нормализация громкости.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..edl.models import EDL, Source, VideoClip
from ..edl.styles import COLOR_PRESETS
from ..media.ffmpeg import run
from .ass import build_ass

log = logging.getLogger(__name__)

Quality = Literal["preview", "final"]

XFADE = {
    "fade": "fade",
    "dissolve": "dissolve",
    "slide_left": "slideleft",
    "slide_up": "slideup",
    "zoom_in": "zoomin",
    "whip": "smoothleft",
    "flash": "fadewhite",
}


@dataclass
class RenderProfile:
    quality: Quality
    scale: float
    clip_args: list[str]
    final_args: list[str]
    oversample: int  # запас разрешения для плавного зума


def profile(quality: Quality, preview_scale: float = 0.5) -> RenderProfile:
    if quality == "preview":
        return RenderProfile(
            quality,
            preview_scale,
            ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20"],
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-c:a", "aac", "-b:a", "128k"],
            oversample=1,
        )
    return RenderProfile(
        quality,
        1.0,
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14"],
        [
            "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high",
            "-c:a", "aac", "-b:a", "192k",
        ],
        oversample=2,
    )


def _even(x: float) -> int:
    return max(2, int(math.ceil(x / 2.0)) * 2)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def output_size(edl: EDL, quality: Quality, preview_scale: float = 0.5) -> tuple[int, int]:
    scale = profile(quality, preview_scale).scale
    return _even(edl.canvas.w * scale), _even(edl.canvas.h * scale)


def _source_path(src: Source, prof: RenderProfile) -> str:
    if prof.quality == "preview" and src.proxy_path:
        return src.proxy_path
    return src.path


def clip_filter(clip: VideoClip, src: Source, w: int, h: int, fps: int, oversample: int) -> str:
    """Цепочка фильтров для одного клипа -> кадр w×h ровно clip.dur секунд."""
    rf = clip.reframe
    animated = abs(rf.zoom_to - rf.zoom_from) > 1e-3
    os_ = oversample if animated else 1
    zoom_static = 1.0 if animated else rf.zoom_from
    cw, ch = w * os_, h * os_  # область кадрирования (9:16) до зум-анимации

    factor = max(cw / src.width, ch / src.height) * zoom_static
    sw, sh = _even(src.width * factor), _even(src.height * factor)
    x = _clamp(rf.anchor_x * sw - cw / 2, 0, sw - cw)
    y = _clamp(rf.anchor_y * sh - ch / 2, 0, sh - ch)

    parts: list[str] = []
    if src.kind == "video":
        parts.append(f"setpts=(PTS-STARTPTS)/{clip.speed:.4f}")
    parts += [
        f"fps={fps}",
        f"scale={sw}:{sh}:flags=lanczos",
        f"crop={cw}:{ch}:{x:.0f}:{y:.0f}",
    ]
    if animated:
        frames = max(1, round(clip.dur * fps))
        # Точка интереса внутри уже вырезанной области
        ax = _clamp((rf.anchor_x * sw - x) / cw, 0, 1)
        ay = _clamp((rf.anchor_y * sh - y) / ch, 0, 1)
        z = f"({rf.zoom_from:.4f}+({rf.zoom_to - rf.zoom_from:.4f})*on/{max(frames - 1, 1)})"
        parts.append(
            f"zoompan=z='{z}':d=1:s={w}x{h}:fps={fps}"
            f":x='max(0,min(iw-iw/zoom,{ax:.4f}*iw-iw/zoom/2))'"
            f":y='max(0,min(ih-ih/zoom,{ay:.4f}*ih-ih/zoom/2))'"
        )
    parts += [
        "tpad=stop_mode=clone:stop_duration=2",
        f"trim=duration={clip.dur:.4f}",
        "setpts=PTS-STARTPTS",
        "setsar=1",
        "format=yuv420p",
    ]
    return ",".join(parts)


def _clip_key(clip: VideoClip, src: Source, path: str, w: int, h: int, fps: int, prof: RenderProfile) -> str:
    payload = {
        "clip": clip.model_dump(exclude={"id", "at", "transition_in", "keep_audio", "audio_gain_db", "role"}),
        "path": path,
        "src": [src.width, src.height, src.kind],
        "canvas": [w, h, fps],
        "profile": [prof.quality, prof.oversample, prof.clip_args],
        "v": 2,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def render_clip(clip: VideoClip, src: Source, w: int, h: int, fps: int, prof: RenderProfile, cache_dir: Path) -> Path:
    path = _source_path(src, prof)
    out = cache_dir / f"clip_{_clip_key(clip, src, path, w, h, fps, prof)}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    vf = clip_filter(clip, src, w, h, fps, prof.oversample)
    if src.kind == "photo":
        inp = ["-loop", "1", "-framerate", str(fps), "-t", f"{clip.dur + 0.5:.3f}", "-i", path]
    else:
        inp = ["-ss", f"{clip.src_in:.3f}", "-t", f"{clip.dur * clip.speed + 0.2:.3f}", "-i", path]
    tmp = out.with_suffix(".tmp.mp4")
    run([*inp, "-vf", vf, "-an", *prof.clip_args, "-t", f"{clip.dur:.4f}", str(tmp)])
    tmp.rename(out)
    return out


def _atempo_chain(speed: float) -> str:
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-3:
        parts.append(f"atempo={s:.4f}")
    return ",".join(parts)


def render(edl: EDL, out_path: Path, work_dir: Path, quality: Quality = "final", font: str = "DejaVu Sans",
           fonts_dir: Path | None = None, preview_scale: float = 0.5) -> Path:
    prof = profile(quality, preview_scale)
    w, h = output_size(edl, quality, preview_scale)
    fps = edl.canvas.fps
    cache_dir = work_dir / "clip_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    total = edl.duration

    # 1. Клипы
    clip_files = [render_clip(c, edl.sources[c.asset_id], w, h, fps, prof, cache_dir) for c in edl.clips]

    # 2. Сборка
    args: list[str] = []
    for f in clip_files:
        args += ["-i", str(f)]
    n_video = len(clip_files)

    fg: list[str] = [f"[{i}:v]settb=AVTB,setsar=1[v{i}]" for i in range(n_video)]
    cur = "v0"
    for i in range(1, n_video):
        clip = edl.clips[i]
        nxt = f"x{i}"
        if clip.transition_in:
            tr = XFADE[clip.transition_in.type]
            fg.append(
                f"[{cur}][v{i}]xfade=transition={tr}:duration={clip.transition_in.dur:.3f}:offset={clip.at:.3f}[{nxt}]"
            )
        else:
            fg.append(f"[{cur}][v{i}]concat=n=2:v=1:a=0[{nxt}]")
        cur = nxt

    post = []
    color = COLOR_PRESETS.get(edl.color, "")
    if color:
        post.append(color)
    ass_text = build_ass(edl, font)
    if edl.texts or (edl.captions and edl.captions.words):
        ass_path = work_dir / f"overlay_{quality}.ass"
        ass_path.write_text(ass_text, encoding="utf-8")
        opt = f"ass='{_ff_escape(ass_path)}'"
        if fonts_dir and fonts_dir.exists():
            opt += f":fontsdir='{_ff_escape(fonts_dir)}'"
        post.append(opt)
    post += [f"trim=duration={total:.3f}", "setpts=PTS-STARTPTS", "format=yuv420p"]
    fg.append(f"[{cur}]{','.join(post)}[vout]")

    # Звук
    audio_labels: list[str] = []
    speech_windows: list[tuple[float, float]] = []
    idx = n_video
    for c in edl.clips:
        src = edl.sources[c.asset_id]
        if not (c.keep_audio and src.kind == "video" and src.has_audio):
            continue
        args += ["-ss", f"{c.src_in:.3f}", "-t", f"{c.dur * c.speed:.3f}", "-i", _source_path(src, prof)]
        chain = [f"[{idx}:a]aresample=48000", "asetpts=PTS-STARTPTS"]
        tempo = _atempo_chain(c.speed)
        if tempo:
            chain.append(tempo)
        delay = int(round(c.at * 1000))
        chain += [
            f"volume={c.audio_gain_db:.1f}dB",
            "afade=t=in:d=0.04",
            f"afade=t=out:st={max(c.dur - 0.06, 0):.3f}:d=0.06",
            f"adelay={delay}|{delay}",
            "aformat=channel_layouts=stereo",
        ]
        lab = f"s{idx}"
        fg.append(",".join(chain) + f"[{lab}]")
        audio_labels.append(lab)
        speech_windows.append((c.at, c.end))
        idx += 1

    if edl.music:
        m = edl.music
        args += ["-ss", f"{m.src_in:.3f}", "-i", m.path]
        chain = [
            f"[{idx}:a]aresample=48000",
            "aformat=channel_layouts=stereo",
            f"atrim=duration={total:.3f}",
            "asetpts=PTS-STARTPTS",
            f"volume={m.gain_db:.1f}dB",
        ]
        if speech_windows:
            duck = 10 ** (m.duck_db / 20)
            cond = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in speech_windows)
            chain.append(f"volume='if(gt({cond},0),{duck:.4f},1)':eval=frame")
        if m.fade_in > 0:
            chain.append(f"afade=t=in:d={m.fade_in:.2f}")
        if m.fade_out > 0:
            chain.append(f"afade=t=out:st={max(total - m.fade_out, 0):.3f}:d={m.fade_out:.2f}")
        fg.append(",".join(chain) + "[mus]")
        audio_labels.append("mus")
        idx += 1

    if audio_labels:
        mix = "".join(f"[{a}]" for a in audio_labels)
        fg.append(
            f"{mix}amix=inputs={len(audio_labels)}:normalize=0:duration=longest,"
            f"apad,atrim=duration={total:.3f},loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]"
        )
    else:
        fg.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={total:.3f}[aout]")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    graph = ";".join(fg)
    (work_dir / f"graph_{quality}.txt").write_text(graph, encoding="utf-8")  # для отладки
    run(
        [
            *args,
            "-filter_complex", graph,
            "-map", "[vout]", "-map", "[aout]",
            *prof.final_args,
            "-r", str(fps), "-pix_fmt", "yuv420p", "-ar", "48000",
            "-movflags", "+faststart",
            "-t", f"{total:.3f}",
            str(out_path),
        ],
        timeout=1800,
    )
    return out_path


def _ff_escape(p: Path) -> str:
    return str(p.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
