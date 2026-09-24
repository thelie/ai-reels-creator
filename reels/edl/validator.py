"""Детерминированная проверка EDL перед рендером."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import EDL
from .styles import COLOR_PRESETS, TEXT_STYLES

MIN_SHOT_S = 0.3
TIME_EPS = 0.02
MAX_UPSCALE = 1.6  # во сколько раз допустимо увеличивать исходник
MAX_TEXT_CHARS = 110


@dataclass
class Issue:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    clip_id: str | None = None

    def __str__(self) -> str:
        where = f" [{self.clip_id}]" if self.clip_id else ""
        return f"{self.severity}:{self.code}{where}: {self.message}"


def validate(edl: EDL, target_duration: float | None = None, duration_tolerance: float = 0.25) -> list[Issue]:
    issues: list[Issue] = []
    if not edl.clips:
        return [Issue("empty", "В таймлайне нет клипов")]

    prev_end = 0.0
    for i, clip in enumerate(edl.clips):
        src = edl.sources.get(clip.asset_id)
        if src is None:
            issues.append(Issue("unknown_asset", f"Нет источника {clip.asset_id}", clip_id=clip.id))
            continue

        if clip.dur < MIN_SHOT_S - TIME_EPS:
            issues.append(Issue("shot_too_short", f"Клип {clip.dur:.2f}с короче {MIN_SHOT_S}с", clip_id=clip.id))

        if src.kind == "video" and clip.src_out > src.duration + TIME_EPS:
            issues.append(
                Issue(
                    "src_out_of_range",
                    f"Нужен фрагмент до {clip.src_out:.2f}с, а исходник {src.duration:.2f}с",
                    clip_id=clip.id,
                )
            )

        # Ожидаемое начало клипа с учётом перехода
        overlap = clip.transition_in.dur if (clip.transition_in and i > 0) else 0.0
        expected_at = prev_end - overlap if i > 0 else 0.0
        if abs(clip.at - expected_at) > TIME_EPS:
            kind = "gap" if clip.at > expected_at else "overlap"
            issues.append(
                Issue(kind, f"Начало {clip.at:.2f}с, ожидалось {expected_at:.2f}с", clip_id=clip.id)
            )
        if clip.transition_in and i > 0:
            prev = edl.clips[i - 1]
            if clip.transition_in.dur >= min(prev.dur, clip.dur):
                issues.append(Issue("transition_too_long", "Переход длиннее соседнего клипа", clip_id=clip.id))
        prev_end = clip.end

        # Разрешение: хватает ли пикселей на кадрирование с зумом
        cover = max(edl.canvas.w / src.width, edl.canvas.h / src.height)
        upscale = cover * max(clip.reframe.zoom_from, clip.reframe.zoom_to)
        if upscale > MAX_UPSCALE:
            issues.append(
                Issue(
                    "upscale",
                    f"Исходник {src.width}x{src.height} придётся увеличить в {upscale:.1f} раза",
                    severity="warning",
                    clip_id=clip.id,
                )
            )

    total = edl.duration
    for t in edl.texts:
        if t.style not in TEXT_STYLES:
            issues.append(Issue("unknown_text_style", f"Неизвестный стиль текста {t.style!r}"))
        if t.at + t.dur > total + TIME_EPS:
            issues.append(Issue("text_out_of_range", f"Текст «{t.content[:20]}» выходит за конец ролика"))
        if len(t.content) > MAX_TEXT_CHARS:
            issues.append(Issue("text_too_long", f"Текст длиннее {MAX_TEXT_CHARS} символов: «{t.content[:30]}…»"))
        if not t.content.strip():
            issues.append(Issue("text_empty", "Пустой текст"))

    if edl.captions and edl.captions.style not in TEXT_STYLES:
        issues.append(Issue("unknown_caption_style", f"Неизвестный стиль субтитров {edl.captions.style!r}"))

    if edl.color not in COLOR_PRESETS:
        issues.append(Issue("unknown_color", f"Неизвестный пресет цвета {edl.color!r}"))

    if target_duration and abs(total - target_duration) > max(1.0, target_duration * duration_tolerance):
        issues.append(
            Issue(
                "duration_mismatch",
                f"Длительность {total:.1f}с, цель {target_duration:.1f}с",
                severity="warning",
            )
        )
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
