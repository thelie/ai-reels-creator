"""Технический контроль качества готового ролика."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..media.ffmpeg import probe


def check(path: Path, expected_duration: float, expected_size: tuple[int, int]) -> dict:
    """Длительность, размер, чёрные и «застывшие» участки, громкость."""
    info = probe(path)
    problems: list[str] = []
    if abs(info.duration - expected_duration) > 0.3:
        problems.append(f"длительность {info.duration:.2f}с вместо {expected_duration:.2f}с")
    if (info.width, info.height) != expected_size:
        problems.append(f"размер {info.width}x{info.height} вместо {expected_size[0]}x{expected_size[1]}")
    if not info.has_audio:
        problems.append("нет звуковой дорожки")

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-vf", "blackdetect=d=0.4:pix_th=0.08,freezedetect=n=0.001:d=1.5",
         "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    log = proc.stderr
    black = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log)]
    freezes = re.findall(r"freeze_start: ([\d.]+)", log)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    loudness = float(m[-1]) if m else None
    if black:
        problems.append(f"чёрные кадры: {', '.join(f'{a:.1f}–{b:.1f}с' for a, b in black)}")
    if freezes:
        problems.append(f"застывшее изображение с {', '.join(freezes)}с")
    return {
        "ok": not problems,
        "problems": problems,
        "duration": info.duration,
        "size": [info.width, info.height],
        "loudness_lufs": loudness,
    }
