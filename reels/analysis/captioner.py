"""Описание ключевых кадров мультимодальной моделью (быстрая модель, пачками)."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from ..config import get_settings
from ..llm import client as llm
from .models import Segment

log = logging.getLogger(__name__)

BATCH = 8

SYSTEM = (
    "Ты помогаешь видеомонтажёру коротких вертикальных роликов (Reels/TikTok). "
    "Тебе показывают ключевые кадры фрагментов исходного материала. Для каждого кадра дай "
    "краткое описание на русском (что/кто в кадре, действие, место, настроение; до 20 слов), "
    "5–8 тегов на русском, крупность плана и оценку, насколько кадр подходит как «хук» — "
    "первый цепляющий кадр ролика (0..1)."
)


class FrameDescription(BaseModel):
    index: int
    description: str
    tags: list[str]
    shot_type: Literal["close_up", "medium", "wide", "detail", "unknown"]
    hook_score: float


class FrameDescriptions(BaseModel):
    items: list[FrameDescription]


def heuristic_describe(seg: Segment, name: str) -> None:
    kind = "фото" if seg.kind == "photo" else "видео"
    span = "" if seg.kind == "photo" else f" {seg.start:.1f}–{seg.end:.1f}с"
    seg.description = seg.description or f"{kind} «{name}»{span}"
    if seg.faces:
        seg.shot_type = "close_up" if seg.faces == 1 else "medium"
        seg.tags = sorted(set(seg.tags) | {"люди"})
    if seg.motion > 0.05:
        seg.tags = sorted(set(seg.tags) | {"движение"})


async def describe_segments(segments: list[Segment]) -> dict[str, float]:
    """Заполняет description/tags/shot_type. Возвращает hook_score по id сегмента."""
    hook: dict[str, float] = {}
    if not llm.available():
        return hook
    model = get_settings().llm_vision_model
    for i in range(0, len(segments), BATCH):
        batch = [s for s in segments[i : i + BATCH] if s.keyframe]
        if not batch:
            continue
        content: list[dict] = []
        for j, seg in enumerate(batch):
            content.append({"type": "text", "text": f"Кадр {j}:"})
            content.append(llm.image_block(seg.keyframe, 640))
        content.append({"type": "text", "text": f"Опиши кадры 0..{len(batch) - 1}."})
        try:
            res = await llm.parse(model=model, system=SYSTEM, content=content, output_format=FrameDescriptions,
                                  max_tokens=4000)
        except llm.LLMError as e:
            log.warning("captioning failed, using heuristics: %s", e)
            continue
        for item in res.items:
            if 0 <= item.index < len(batch):
                seg = batch[item.index]
                seg.description = item.description
                seg.tags = item.tags
                seg.shot_type = item.shot_type
                hook[seg.id] = max(0.0, min(1.0, item.hook_score))
    return hook
