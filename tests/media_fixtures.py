"""Синтетические медиа для тестов (генерируются ffmpeg, без внешних файлов)."""

from __future__ import annotations

from pathlib import Path

from reels.media.ffmpeg import run


def make_video(path: Path, dur: float = 4.0, size: str = "1280x720", src: str = "testsrc2", tone: int | None = 440,
               scenes: int = 1) -> Path:
    """Видео с тестовой картинкой; scenes>1 — склейка разных источников (для детекции сцен)."""
    sources = [src, "smptebars", "rgbtestsrc", "testsrc", "smptehdbars"]
    if scenes == 1:
        inputs = ["-f", "lavfi", "-i", f"{src}=size={size}:rate=30:duration={dur}"]
        vmap = "0:v"
        fg = []
    else:
        inputs = []
        seg = dur / scenes
        for i in range(scenes):
            inputs += ["-f", "lavfi", "-i", f"{sources[i % len(sources)]}=size={size}:rate=30:duration={seg}"]
        fg = ["-filter_complex", "".join(f"[{i}:v]" for i in range(scenes)) + f"concat=n={scenes}:v=1:a=0[v]"]
        vmap = "[v]"
    audio = []
    amap = []
    if tone:
        audio = ["-f", "lavfi", "-i", f"sine=frequency={tone}:duration={dur}:sample_rate=48000"]
        amap = ["-map", f"{scenes if scenes > 1 else 1}:a"]
    run([*inputs, *audio, *fg, "-map", vmap, *amap, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt",
         "yuv420p", "-c:a", "aac", "-shortest", str(path)])
    return path


def make_photo(path: Path, size: str = "1600x1200", src: str = "testsrc2") -> Path:
    run(["-f", "lavfi", "-i", f"{src}=size={size}:rate=1", "-frames:v", "1", str(path)])
    return path


def make_click_track(path: Path, bpm: float = 120.0, dur: float = 12.0) -> Path:
    """Метроном: короткие щелчки с заданным темпом поверх тихого тона."""
    period = 60.0 / bpm
    expr = f"0.9*sin(2*PI*1000*t)*lt(mod(t\\,{period:.6f})\\,0.03)+0.05*sin(2*PI*220*t)"
    run(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d={dur}", "-c:a", "libmp3lame", "-q:a", "4", str(path)])
    return path
