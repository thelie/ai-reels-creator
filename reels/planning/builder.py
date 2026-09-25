"""PlanDraft -> EDL: тайминги (бит/речь), окна в исходниках, кадрирование, переходы, текст, звук.

Всё здесь детерминировано: одинаковый план и материал всегда дают одинаковый EDL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..analysis.beats import BeatInfo
from ..analysis.models import AssetAnalysis, Segment
from ..edl.models import (
    EDL,
    Canvas,
    Captions,
    CaptionWord,
    Meta,
    Music,
    Reframe,
    Source,
    TextOverlay,
    Transition,
    VideoClip,
)
from ..edl.styles import COLOR_PRESETS
from .models import PlanDraft, StyleTemplate

log = logging.getLogger(__name__)

MIN_SHOT = 0.4
MAX_TRANSITION = 0.6
MIN_SPEED = 0.5


@dataclass
class MediaIndex:
    """Всё, что известно о материале проекта."""

    analyses: dict[str, AssetAnalysis]
    paths: dict[str, tuple[str, str | None]]  # asset_id -> (путь для финала, путь к прокси)
    segments: dict[str, Segment] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.segments:
            self.segments = {s.id: s for a in self.analyses.values() for s in a.segments}


@dataclass
class MusicInput:
    path: str
    beats: BeatInfo


def _available(seg: Segment, a: AssetAnalysis, keep_audio: bool) -> float:
    """Максимальная длительность сцены из этого сегмента (с учётом замедления)."""
    if seg.kind == "photo":
        return 1e9
    return a.duration if keep_audio else a.duration / MIN_SPEED


def _snap(cuts: list[float], grid: list[float], locked: set[int], max_len: list[float]) -> list[float]:
    """Притягиваем склейки к битам. cuts[0]=0, cuts[-1]=конец; locked — индексы, которые не двигаем."""
    if not grid:
        return cuts
    out = list(cuts)
    for i in range(1, len(out)):
        if i in locked:
            continue
        lo = out[i - 1] + MIN_SHOT
        hi = out[i - 1] + max_len[i - 1]
        cands = [g for g in grid if lo <= g <= hi]
        if not cands:
            continue
        g = min(cands, key=lambda x: abs(x - out[i]))
        if abs(g - out[i]) <= 0.6 * max(out[i] - out[i - 1], MIN_SHOT):
            out[i] = g
    return out


def build_edl(draft: PlanDraft, media: MediaIndex, template: StyleTemplate, duration: float,
              music: MusicInput | None = None, canvas: Canvas | None = None) -> EDL:
    canvas = canvas or Canvas()
    scenes = [sc for sc in draft.scenes if sc.segment_id in media.segments]
    if not scenes:
        raise ValueError("В плане нет сцен с существующими сегментами")

    segs = [media.segments[sc.segment_id] for sc in scenes]
    assets = [media.analyses[s.asset_id] for s in segs]
    keep = [sc.keep_audio and s.kind == "video" and a.has_audio for sc, s, a in zip(scenes, segs, assets)]

    # 1. Длительности сцен: речь — по длине фраз, остальное масштабируем под цель
    d = []
    for sc, s, k in zip(scenes, segs, keep):
        if k:
            words = s.words
            speech_len = (words[-1].t + words[-1].d - words[0].t + 0.25) if words else s.dur
            d.append(max(MIN_SHOT, min(speech_len, s.dur + 0.5)))
        else:
            d.append(max(MIN_SHOT, sc.duration))
    fixed = sum(x for x, k in zip(d, keep) if k)
    flex = sum(x for x, k in zip(d, keep) if not k)
    if flex > 0 and duration > fixed:
        kf = max(0.5, min(2.0, (duration - fixed) / flex))
        d = [x if k else x * kf for x, k in zip(d, keep)]
    max_len = [min(_available(s, a, k), max(template.shot_max * 2, 6.0) if not k else 60.0) - MAX_TRANSITION
               for s, a, k in zip(segs, assets, keep)]
    d = [max(MIN_SHOT, min(x, m)) for x, m in zip(d, max_len)]

    # 2. Склейки на биты (кроме границ речевых сцен)
    cuts = [0.0]
    for x in d:
        cuts.append(cuts[-1] + x)
    music_src_in = 0.0
    if music and music.beats.beats:
        first = music.beats.beats[0]
        music_src_in = first if first < 3.0 else 0.0
    if template.cut_on_beat and music and music.beats.beats:
        grid = [round(b - music_src_in, 3) for b in music.beats.beats if b >= music_src_in]
        locked = {i for i in range(1, len(cuts)) if (i - 1 < len(keep) and keep[i - 1]) or (i < len(keep) and keep[i])}
        cuts = _snap(cuts, grid, locked, [m + MAX_TRANSITION for m in max_len])
    d = [cuts[i + 1] - cuts[i] for i in range(len(d))]
    total = cuts[-1]

    # 3. Переходы
    tds: list[Transition | None] = [None]
    for i in range(1, len(d)):
        tr = None
        if (template.transitions and template.transition_every > 0 and i % template.transition_every == 0
                and not keep[i] and not keep[i - 1]):
            td = min(template.transition_dur, 0.4 * d[i - 1], 0.4 * d[i], MAX_TRANSITION)
            if td >= 0.1:
                kind = template.transitions[(i // template.transition_every - 1) % len(template.transitions)]
                tr = Transition(type=kind, dur=round(td, 3))
        tds.append(tr)

    # 4. Клипы
    clips: list[VideoClip] = []
    used_windows: dict[str, list[float]] = {}
    for i, (sc, seg, a, k) in enumerate(zip(scenes, segs, assets, keep)):
        td = tds[i].dur if tds[i] else 0.0
        clip_dur = round(d[i] + td, 3)
        at = round(cuts[i] - td, 3)
        speed = 1.0
        src_in = 0.0
        if seg.kind == "video":
            need = clip_dur
            if k and seg.words:
                src_in = max(0.0, seg.words[0].t - 0.12)
            elif seg.dur >= need:
                # Повторное использование сегмента — берём другое окно
                prev = used_windows.get(seg.id, [])
                src_in = seg.start + (seg.dur - need) / 2
                if prev:
                    src_in = seg.start if prev[-1] > seg.start + 0.01 else seg.end - need
            else:
                src_in = max(0.0, min(seg.start - (need - seg.dur) / 2, a.duration - need))
            if src_in + need > a.duration:
                src_in = max(0.0, a.duration - need)
            if need > a.duration - 0.02:
                speed = max(MIN_SPEED, (a.duration - 0.05) / need)
                src_in = 0.0
            used_windows.setdefault(seg.id, []).append(src_in)

        # Кадрирование и зум
        if seg.kind == "photo":
            z = template.photo_zoom
            zf, zt = (1.0, z) if i % 2 == 0 else (z, 1.0)
        elif template.zoom == "punch":
            if k:
                zf = zt = 1.0 if i % 2 == 0 else 1.12  # «джамп-кат» с наездом на каждой второй фразе
            elif i == 0:
                zf, zt = 1.0, 1.12
            else:
                zf = zt = 1.0
        elif template.zoom == "subtle":
            zf, zt = (1.0, 1.05) if i % 2 == 0 else (1.05, 1.0)
        else:
            zf = zt = 1.0
        clips.append(
            VideoClip(
                id=f"c{i + 1}",
                asset_id=seg.asset_id,
                src_in=round(src_in, 3),
                at=at,
                dur=clip_dur,
                speed=round(speed, 4),
                reframe=Reframe(anchor_x=seg.anchor_x, anchor_y=seg.anchor_y, zoom_from=zf, zoom_to=zt),
                keep_audio=k,
                transition_in=tds[i],
                effect=sc.effect,
                role=sc.role,
            )
        )

    # 5. Субтитры из речи клипов
    cap_words: list[CaptionWord] = []
    if template.captions.enabled:
        for c, seg, k in zip(clips, segs, keep):
            if not k:
                continue
            for w in seg.words:
                if c.src_in <= w.t < c.src_out:
                    t_out = c.at + (w.t - c.src_in) / c.speed
                    dur_out = min(w.d / c.speed, c.end - t_out)
                    if dur_out > 0.02:
                        cap_words.append(CaptionWord(t=round(t_out, 3), d=round(dur_out, 3), w=w.w))
    captioned = {c.id for c in clips if c.keep_audio} if cap_words else set()

    # 6. Текст на экране
    texts: list[TextOverlay] = []
    tt = template.text

    def pos_for(clip: VideoClip):
        return "top" if clip.id in captioned else tt.pos

    text_end = 0.0
    hook = draft.hook_text.replace("\\n", "\n").strip()
    timed_hook = bool(hook) and tt.hook_mode == "timed"
    if hook and tt.hook_mode == "sticker":
        # Стикер на весь ролик: рукописная фраза сверху, крупное слово заглавными под ней
        lines = [ln.strip() for ln in hook.split("\n") if ln.strip()]
        script, caps = (lines[0], " ".join(lines[1:])) if len(lines) > 1 else ("", lines[0])
        dur = round(total - 0.02, 3)
        if script:
            texts.append(TextOverlay(at=0.0, dur=dur, content=script[:60], style="sticker_script",
                                     y=tt.sticker_y - 0.045, anim="fade"))
        texts.append(TextOverlay(at=0.0, dur=dur, content=caps[:40], style="sticker_caps",
                                 y=tt.sticker_y + 0.01, anim="pop"))
    elif timed_hook:
        hook_end = min(total, max(1.2, min(cuts[1] if len(cuts) > 2 else total, 3.5)))
        texts.append(TextOverlay(at=0.0, dur=round(hook_end - 0.02, 3), content=hook,
                                 style=tt.hook_style, pos=pos_for(clips[0]), anim=tt.anim))
        text_end = hook_end
    cta = draft.cta_text.strip() if tt.cta else ""
    for i, (sc, c) in enumerate(zip(scenes, clips)):
        is_last = i == len(clips) - 1
        content = cta if (is_last and cta) else sc.text.strip()
        if not content or (i == 0 and timed_hook):
            continue
        start = max(cuts[i], text_end)
        end = cuts[i + 1] - 0.05
        if end - start < 0.5:
            continue
        style = tt.cta_style if (is_last and cta) else tt.body_style
        texts.append(TextOverlay(at=round(start, 3), dur=round(end - start, 3), content=content[:100],
                                 style=style, pos=pos_for(c), anim=tt.anim))
        text_end = end
    if cta and not any(t.content == cta for t in texts) and total > 1.5:
        start = max(text_end, total - 2.0)
        if total - start >= 0.8:
            texts.append(TextOverlay(at=round(start, 3), dur=round(total - start - 0.02, 3), content=cta[:100],
                                     style=tt.cta_style, pos="center", anim=tt.anim))

    # 7. Источники, музыка, мета
    sources: dict[str, Source] = {}
    for a in {s.asset_id: media.analyses[s.asset_id] for s in segs}.values():
        path, proxy = media.paths[a.asset_id]
        sources[a.asset_id] = Source(asset_id=a.asset_id, kind=a.kind, path=path, proxy_path=proxy,
                                     duration=a.duration, width=a.width, height=a.height, has_audio=a.has_audio)
    music_track = None
    if music:
        music_track = Music(path=music.path, src_in=round(music_src_in, 3), gain_db=template.music_gain_db,
                            fade_out=min(1.0, total * 0.1))

    return EDL(
        canvas=canvas,
        sources=sources,
        clips=clips,
        texts=texts,
        captions=Captions(words=cap_words, style=template.captions.style,
                          words_per_screen=template.captions.words_per_screen) if cap_words else None,
        music=music_track,
        color=draft.color if draft.color in COLOR_PRESETS else template.color,
        template_id=template.id,
        meta=Meta(title=draft.title, caption=draft.caption,
                  hashtags=[h if h.startswith("#") else f"#{h}" for h in draft.hashtags if h.strip()]),
    )


def pick_library_music(music_dir: Path, bpm_hint: float | None = None) -> Path | None:
    """Первый подходящий трек из локальной лицензированной библиотеки (MVP — без тегов настроения)."""
    if not music_dir.exists():
        return None
    tracks = sorted(p for p in music_dir.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg", ".aac"})
    return tracks[0] if tracks else None
