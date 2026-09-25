"""Библиотека шаблонов стиля и построение шаблона из референса."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..analysis.models import AssetAnalysis, ReferenceAnalysis
from .models import Slot, StyleTemplate, TemplateCaptions, TemplateText

TEMPLATES_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=1)
def templates() -> dict[str, StyleTemplate]:
    out = {}
    for f in sorted(TEMPLATES_DIR.glob("*.json")):
        t = StyleTemplate.model_validate_json(f.read_text(encoding="utf-8"))
        out[t.id] = t
    return out


def get(template_id: str) -> StyleTemplate:
    return templates().get(template_id) or templates()["dynamic_montage"]


def auto_select(analyses: list[AssetAnalysis], script: str = "") -> StyleTemplate:
    """Эвристический выбор шаблона по составу материала (когда LLM недоступна)."""
    segs = [s for a in analyses for s in a.segments]
    if not segs:
        return get("dynamic_montage")
    photos = sum(1 for a in analyses if a.kind == "photo")
    speech = sum(1 for s in segs if s.has_speech)
    script_l = script.lower()
    if speech >= max(2, len(segs) * 0.3):
        return get("talking_head")
    if any(w in script_l for w in ("топ", "1.", "лайфхак", "совет", "причин")):
        return get("listicle")
    if any(w in script_l for w in ("купить", "товар", "заказ", "скидк", "цена", "распаков")):
        return get("product_showcase")
    if photos >= max(3, len(analyses) * 0.6):
        return get("photo_slideshow")
    return get("dynamic_montage")


def from_reference(ref: ReferenceAnalysis, base: StyleTemplate | None = None) -> StyleTemplate:
    """Шаблон по ролику-референсу: структура слотов = монтажный ритм референса."""
    base = base or get("dynamic_montage")
    slots: list[Slot] = []
    for i, shot in enumerate(ref.shots):
        role = "hook" if i == 0 else ("cta" if i == len(ref.shots) - 1 and len(ref.shots) > 2 else "body")
        slots.append(Slot(role=role, dur=max(0.4, shot.dur), hint=shot.description, effect=shot.effect))
    durs = [s.dur for s in slots] or [base.avg_shot]
    fast = sum(durs) / len(durs) < 1.3
    text = base.text if ref.shots and any(s.has_text for s in ref.shots) else TemplateText(per_scene=False)
    sticker = next((t.text for t in ref.overlay_texts if t.role == "persistent_title"), "")
    labels = [t.text for t in ref.overlay_texts if t.role == "section_label"]
    if sticker:
        text = text.model_copy(update={"hook_mode": "sticker", "sticker_example": sticker,
                                       "hook_style": "sticker_caps"})
    if labels:
        text = text.model_copy(update={"per_scene": True, "body_style": "label_script", "pos": "top",
                                       "anim": "fade"})
    captions = ref.has_captions if ref.has_captions is not None else ref.has_speech
    return base.model_copy(
        update={
            "id": "reference",
            "name": "По референсу",
            "description": ref.summary or (
                f"Ритм как в референсе: {len(slots)} планов, средний {sum(durs) / len(durs):.1f} с"
                + (f", {ref.bpm:.0f} BPM" if ref.bpm else "")
            ),
            "shot_min": max(0.3, min(durs)),
            "shot_max": max(durs),
            "hook_dur": durs[0],
            "cut_on_beat": ref.cut_on_beat_ratio >= 0.5,
            "transitions": [] if fast else base.transitions,
            "text": text,
            "captions": TemplateCaptions(enabled=captions),
            "slots": slots,
        }
    )
