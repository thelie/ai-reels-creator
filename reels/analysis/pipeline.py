"""Конвейер анализа: ассет -> прокси -> сцены -> сегменты с метриками, лицами, речью, описаниями."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..media import ffmpeg
from . import captioner, speech, vision
from .beats import BeatInfo, analyze_music
from .models import AssetAnalysis, ReferenceAnalysis, ReferenceShot, Segment, Word

log = logging.getLogger(__name__)

MAX_SEGMENT_S = 5.0
MIN_SEGMENT_S = 0.8


def _split_shot(start: float, end: float) -> list[tuple[float, float]]:
    """Длинные дубли режем на куски ≤ MAX_SEGMENT_S — так их проще распределять по сценам."""
    dur = end - start
    if dur <= MAX_SEGMENT_S:
        return [(start, end)]
    n = int(dur // MAX_SEGMENT_S) + (1 if dur % MAX_SEGMENT_S > MIN_SEGMENT_S else 0)
    step = dur / n
    return [(start + i * step, start + (i + 1) * step) for i in range(n)]


PHRASE_GAP_S = 0.35
MAX_SPEECH_SEGMENT_S = 7.0


def _split_by_speech(start: float, end: float, words: list[Word]) -> list[tuple[float, float]] | None:
    """Режем дубль с речью по паузам между фразами, чтобы склейки не рвали слова.

    Фразы = слова, разделённые паузой ≥ PHRASE_GAP_S или концом предложения; соседние фразы
    склеиваются в куски до MAX_SPEECH_SEGMENT_S. None — если речи в дубле почти нет.
    """
    ws = [w for w in words if start <= w.t < end]
    if len(ws) < 3:
        return None
    phrases: list[list[Word]] = [[ws[0]]]
    for prev, w in zip(ws, ws[1:]):
        gap = w.t - (prev.t + prev.d)
        if gap >= PHRASE_GAP_S or prev.w.rstrip().endswith((".", "!", "?", "…")):
            phrases.append([w])
        else:
            phrases[-1].append(w)
    chunks: list[tuple[float, float]] = []
    cur_s = cur_e = None
    for ph in phrases:
        ps, pe = ph[0].t, ph[-1].t + ph[-1].d
        if cur_s is None:
            cur_s, cur_e = ps, pe
        elif pe - cur_s <= MAX_SPEECH_SEGMENT_S:
            cur_e = pe
        else:
            chunks.append((cur_s, cur_e))
            cur_s, cur_e = ps, pe
    chunks.append((cur_s, cur_e))
    # Небольшие поля вокруг речи, не выходя за соседние куски и границы дубля
    out = []
    for i, (s, e) in enumerate(chunks):
        lo = max(start, chunks[i - 1][1] if i else start, s - 0.12)
        hi = min(end, chunks[i + 1][0] if i + 1 < len(chunks) else end, e + 0.2)
        out.append((round(lo, 3), round(hi, 3)))
    return out


def prepare_asset(asset_id: str, path: Path, work_dir: Path) -> tuple[ffmpeg.ProbeInfo, Path]:
    """Нормализация + прокси. Возвращает (probe оригинала, путь к прокси)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    info = ffmpeg.probe(path)
    if info.kind == "photo":
        proxy = work_dir / f"{asset_id}_photo.jpg"
        if not proxy.exists():
            ffmpeg.normalize_photo(path, proxy)
        info = ffmpeg.probe(proxy)  # размеры после EXIF-поворота
        return info, proxy
    if info.kind == "video":
        proxy = work_dir / f"{asset_id}_proxy.mp4"
        if not proxy.exists():
            ffmpeg.make_proxy(path, proxy)
        return info, proxy
    return info, path


def analyze_video_sync(asset_id: str, path: Path, work_dir: Path, transcribe: bool = True,
                       name: str = "") -> AssetAnalysis:
    info, proxy = prepare_asset(asset_id, path, work_dir)
    w, h = info.display_size
    shots, duration = vision.scan_video(proxy)
    duration = min(duration, info.duration) if info.duration else duration

    words: list[Word] = []
    if transcribe and info.has_audio:
        _, words = speech.transcribe(proxy)

    segments: list[Segment] = []
    for shot in shots:
        shot_end = min(shot.end, duration)
        for s, e in _split_by_speech(shot.start, shot_end, words) or _split_shot(shot.start, shot_end):
            if e - s < 0.3:
                continue
            frames = [f for f in shot.frames if s <= f.t < e] or shot.frames
            key_t = max(frames, key=lambda f: f.sharpness).t if frames else (s + e) / 2
            seg_id = f"{asset_id}_s{len(segments):02d}"
            key_path = work_dir / f"{seg_id}.jpg"
            ffmpeg.extract_frame(proxy, min(key_t, max(duration - 0.05, 0)), key_path)
            ax, ay, faces = vision.detect_anchor(key_path)
            seg_words = [x for x in words if s <= x.t < e]
            segments.append(
                Segment(
                    id=seg_id,
                    asset_id=asset_id,
                    kind="video",
                    start=round(s, 3),
                    end=round(e, 3),
                    keyframe=str(key_path),
                    quality=vision.quality_score(frames),
                    motion=round(sorted(f.motion for f in frames)[len(frames) // 2], 4) if frames else 0.0,
                    anchor_x=round(ax, 3),
                    anchor_y=round(ay, 3),
                    faces=faces,
                    has_speech=len(seg_words) >= max(2, int((e - s) * 0.6)),
                    transcript=" ".join(x.w for x in seg_words),
                    words=seg_words,
                )
            )
    for seg in segments:
        captioner.heuristic_describe(seg, name or path.name)
    return AssetAnalysis(asset_id=asset_id, kind="video", duration=duration, width=w, height=h,
                         has_audio=info.has_audio, segments=segments)


def analyze_photo_sync(asset_id: str, path: Path, work_dir: Path, name: str = "") -> AssetAnalysis:
    info, norm = prepare_asset(asset_id, path, work_dir)
    key = work_dir / f"{asset_id}_key.jpg"
    if not key.exists():
        ffmpeg.run(["-i", str(norm), "-vf", "scale=-2:512", str(key)])
    ax, ay, faces = vision.detect_anchor(key)
    seg = Segment(id=f"{asset_id}_s00", asset_id=asset_id, kind="photo", keyframe=str(key),
                  quality=vision.image_quality(key), anchor_x=round(ax, 3), anchor_y=round(ay, 3), faces=faces)
    captioner.heuristic_describe(seg, name or path.name)
    return AssetAnalysis(asset_id=asset_id, kind="photo", width=info.width, height=info.height, segments=[seg])


async def analyze_assets(items: list[tuple[str, Path, str] | tuple[str, Path, str, str]], work_dir: Path,
                         concurrency: int = 2, progress=None) -> tuple[list[AssetAnalysis], dict[str, float]]:
    """items: (asset_id, path, kind[, исходное имя]). Возвращает анализы и hook-оценки сегментов."""
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(asset_id: str, path: Path, kind: str, name: str = "") -> AssetAnalysis | None:
        nonlocal done
        async with sem:
            try:
                if kind == "photo":
                    res = await asyncio.to_thread(analyze_photo_sync, asset_id, path, work_dir, name)
                else:
                    res = await asyncio.to_thread(analyze_video_sync, asset_id, path, work_dir, True, name)
            except Exception:  # noqa: BLE001 — битый файл не должен ронять проект
                log.exception("analysis failed for %s", path)
                res = None
            done += 1
            if progress:
                await progress(done, len(items))
            return res

    results = await asyncio.gather(*(one(*it) for it in items))
    analyses = [r for r in results if r is not None]
    segments = [s for a in analyses for s in a.segments]
    hook = await captioner.describe_segments(segments)
    return analyses, hook


def analyze_reference_sync(path: Path, work_dir: Path) -> ReferenceAnalysis:
    """MVP-разбор референса: ритм монтажа, темп музыки, попадание склеек в бит, речь."""
    info, proxy = prepare_asset("ref", path, work_dir)
    shots, duration = vision.scan_video(proxy, threshold=0.35, min_shot=0.25)
    beats = BeatInfo(bpm=0.0)
    if info.has_audio:
        try:
            beats = analyze_music(str(proxy))
        except ffmpeg.FFmpegError:
            log.warning("no audio decoded from reference")
    cuts = [s.start for s in shots[1:]]
    on_beat = 0
    for c in cuts:
        if beats.beats and min(abs(c - b) for b in beats.beats) <= 0.08:
            on_beat += 1
    transcript, words = speech.transcribe(proxy) if info.has_audio else ("", [])
    ref_shots = []
    for i, s in enumerate(shots):
        key = work_dir / f"ref_shot_{i:02d}.jpg"
        ffmpeg.extract_frame(proxy, min((s.start + s.end) / 2, max(duration - 0.05, 0)), key)
        ref_shots.append(ReferenceShot(start=round(s.start, 3), dur=round(s.dur, 3), keyframe=str(key)))
    return ReferenceAnalysis(
        duration=round(duration, 3),
        shots=ref_shots,
        bpm=beats.bpm,
        cut_on_beat_ratio=round(on_beat / len(cuts), 2) if cuts else 0.0,
        has_speech=len(words) > max(3, duration * 0.8),
        transcript=transcript,
    )
