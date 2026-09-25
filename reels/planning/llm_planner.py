"""Планирование и правки через LLM. LLM выдаёт PlanDraft (структурированный вывод),
builder детерминированно превращает его в EDL."""

from __future__ import annotations

import json
import logging

from ..analysis.models import AssetAnalysis, ReferenceAnalysis
from ..config import get_settings
from ..edl.styles import COLOR_PRESETS
from ..llm import client as llm
from . import library
from .models import PlanDraft, StyleTemplate

log = logging.getLogger(__name__)

SYSTEM = """Ты — опытный монтажёр коротких вертикальных видео для Instagram Reels и TikTok.
Твоя задача — спланировать ролик из материала пользователя: выбрать фрагменты, их порядок,
длительности и тексты на экране.

Правила хорошего короткого видео:
- Первые 1–2 секунды решают всё: первая сцена — самый цепляющий кадр, hook_text — короткая
  интригующая фраза (вопрос, обещание результата, неожиданный факт), до 60 символов.
- Держи ритм: планы короче 1 секунды — для динамики, 2–4 секунды — для спокойных роликов.
- Не повторяй один и тот же сегмент без необходимости; если материала мало, можно.
- Текст на экране — короткий (до 6–7 слов), не пересказывает то, что и так видно.
- Если в сегменте человек говорит в кадр (есть transcript) и это важно по смыслу,
  ставь keep_audio=true и длительность не меньше длины фразы.
- Если пользователь дал сценарий — следуй его смыслу и порядку, подбирая подходящие кадры.
- Сумма длительностей сцен должна быть близка к целевой длительности.
- В конце можно добавить призыв к действию (cta_text), если это уместно.
- Подпись к публикации и 3–6 хэштегов — на языке пользователя.
Используй только segment_id из списка материала."""


def _material(analyses: list[AssetAnalysis], hook: dict[str, float]) -> str:
    rows = []
    for a in analyses:
        for s in a.segments:
            row = {
                "segment_id": s.id,
                "kind": s.kind,
                "len_s": round(s.dur, 1) if s.kind == "video" else "фото",
                "asset_len_s": round(a.duration, 1) if a.kind == "video" else None,
                "quality": s.quality,
                "faces": s.faces,
                "shot": s.shot_type,
                "desc": s.description,
                "tags": s.tags[:8],
            }
            if s.id in hook:
                row["hook_score"] = round(hook[s.id], 2)
            if s.transcript:
                row["transcript"] = s.transcript[:300]
            rows.append({k: v for k, v in row.items() if v not in (None, "", [])})
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)


def _template_brief(t: StyleTemplate) -> str:
    brief = {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "shot_len_s": [t.shot_min, t.shot_max],
        "texts_per_scene": t.text.per_scene,
        "keeps_speech": t.prefer_speech,
    }
    if t.text.hook_mode == "sticker":
        brief["sticker_example"] = t.text.sticker_example
    if t.slots:
        brief["slots"] = [
            {"role": s.role, "dur": round(s.dur, 2), "hint": s.hint}
            | ({"effect": s.effect} if s.effect != "none" else {})
            for s in t.slots
        ]
    return json.dumps(brief, ensure_ascii=False)


def _catalog() -> str:
    return "\n".join(_template_brief(t) for t in library.templates().values())


async def plan(analyses: list[AssetAnalysis], duration: float, script: str = "", template: StyleTemplate | None = None,
               hook: dict[str, float] | None = None, reference: ReferenceAnalysis | None = None,
               extra: str = "") -> PlanDraft:
    parts = [f"Целевая длительность: {duration:.0f} с."]
    if template:
        parts.append(f"Шаблон стиля (использовать его, template_id={template.id}):\n{_template_brief(template)}")
        if template.slots:
            parts.append(
                "Это структура ролика-референса: повтори её ритм, порядок частей и приёмы, подбирая фрагменты, "
                "похожие по смыслу и крупности на подсказки слотов. Эффекты (effect) ставь там же, где они в "
                "слотах референса, особенно на хуке. Если материала меньше, чем слотов, не повторяй фрагменты "
                "с речью — сцен может быть меньше. Речь в кадре не обрезай на полуслове, сохраняй логику рассказа."
            )
        if template.text.hook_mode == "sticker":
            parts.append(
                "В референсе на всём ролике висит стикер-заголовок: "
                f"«{template.text.sticker_example}». Придумай такой же по форме для этого материала и положи в "
                "hook_text: первая строка — короткая разговорная фраза (2–4 слова), вторая — 1–2 слова, "
                "главная эмоция или суть; строки через \\n. Не копируй текст референса."
            )
        if not template.text.cta:
            parts.append("В референсе нет призыва к действию на экране — оставь cta_text пустым.")
        if template.text.body_style == "label_script":
            parts.append("Тексты сцен (text) — редкие подписи разделов, как в референсе (1–3 слова, например смена "
                         "темы); у большинства сцен text пустой.")
    else:
        parts.append(f"Выбери подходящий шаблон стиля (template_id) из каталога:\n{_catalog()}")
    if reference and reference.transcript:
        parts.append(f"Речь в референсе (для понимания структуры, не копировать): {reference.transcript[:600]}")
    parts.append(f"Сценарий/пожелания пользователя:\n{script.strip() or '(нет — придумай сам по материалу)'}")
    parts.append(f"Цвет: один из {', '.join(COLOR_PRESETS)}.")
    if extra:
        parts.append(extra)
    parts.append(f"Материал (по одному сегменту на строку):\n{_material(analyses, hook or {})}")

    draft = await llm.parse(
        model=get_settings().llm_planner_model,
        system=SYSTEM,
        content="\n\n".join(parts),
        output_format=PlanDraft,
        effort="medium",
    )
    return _sanitize(draft, analyses, template)


async def revise(current: PlanDraft, instruction: str, analyses: list[AssetAnalysis], duration: float,
                 template: StyleTemplate) -> tuple[PlanDraft, float]:
    """Правка по тексту пользователя. Возвращает новый план и (возможно изменённую) длительность."""
    content = "\n\n".join(
        [
            f"Текущая целевая длительность: {duration:.0f} с. Если пользователь просит короче/длиннее — "
            "измени длительности сцен соответственно.",
            f"Текущий шаблон:\n{_template_brief(template)}\nДругие шаблоны (можно сменить, если просят другой стиль):\n"
            f"{_catalog()}",
            f"Текущий план:\n{current.model_dump_json(indent=1)}",
            f"Материал:\n{_material(analyses, {})}",
            f"Правка от пользователя: «{instruction}»\n"
            "Верни полный обновлённый план. Меняй только то, о чём просят; остальное сохрани как есть.",
        ]
    )
    draft = await llm.parse(
        model=get_settings().llm_planner_model,
        system=SYSTEM,
        content=content,
        output_format=PlanDraft,
        effort="medium",
    )
    draft = _sanitize(draft, analyses, None)
    new_duration = sum(s.duration for s in draft.scenes) or duration
    return draft, new_duration


def _sanitize(draft: PlanDraft, analyses: list[AssetAnalysis], template: StyleTemplate | None) -> PlanDraft:
    known = {s.id for a in analyses for s in a.segments}
    scenes = [s for s in draft.scenes if s.segment_id in known and s.duration > 0]
    dropped = len(draft.scenes) - len(scenes)
    if dropped:
        log.warning("LLM plan referenced %d unknown segments — dropped", dropped)
    if not scenes:
        raise llm.LLMError("plan has no valid scenes")
    tid = template.id if template else draft.template_id
    if tid not in library.templates() and tid != "reference":
        tid = "dynamic_montage"
    return draft.model_copy(update={"scenes": scenes, "template_id": tid})


MAX_REF_FRAMES = 32


async def describe_reference(ref: ReferenceAnalysis) -> ReferenceAnalysis:
    """Кадры референса -> роли, эффекты и тексты поверх видео. Без LLM возвращает как есть."""
    from typing import Literal

    from pydantic import BaseModel

    from ..analysis.models import OverlayText

    class RefShot(BaseModel):
        index: int
        description: str
        shot_type: Literal["close_up", "medium", "wide", "detail", "unknown"]
        has_text: bool
        effect: Literal["none", "bw", "inset"]

    class RefDescription(BaseModel):
        shots: list[RefShot]
        overlay_texts: list[OverlayText]
        has_captions: bool
        summary: str

    if not llm.available() or not ref.shots:
        return ref
    # Равномерная выборка по всему ролику, если планов больше лимита
    n = len(ref.shots)
    idx = list(range(n)) if n <= MAX_REF_FRAMES else sorted({round(k * (n - 1) / (MAX_REF_FRAMES - 1))
                                                              for k in range(MAX_REF_FRAMES)})
    content: list[dict] = []
    for j, i in enumerate(idx):
        s = ref.shots[i]
        content.append({"type": "text", "text": f"Кадр {j} (план {i}, {s.start:.1f}–{s.start + s.dur:.1f} с):"})
        content.append(llm.image_block(s.keyframe, 640))
    if ref.transcript:
        content.append({"type": "text", "text": f"Речь в ролике: {ref.transcript[:1500]}"})
    content.append({
        "type": "text",
        "text": (
            "Это кадры ролика-референса по порядку (по одному на план). Разбери монтаж так, чтобы другой монтажёр "
            "мог повторить стиль на другом материале.\n"
            "shots — для каждого кадра (index = номер кадра): что в кадре и зачем он в структуре (до 15 слов), "
            "крупность, есть ли текст поверх видео, эффект: bw — чёрно-белый, inset — видео уменьшено и стоит "
            "на чёрном/цветном фоне, none — обычный полноэкранный кадр.\n"
            "overlay_texts — различные надписи поверх видео (без повторов): persistent_title — стикер/заголовок, "
            "который висит на многих кадрах подряд (если он из нескольких строк разными шрифтами — строки через "
            "\\n, как на экране); section_label — подпись раздела на части кадров; hook, cta, other.\n"
            "has_captions — есть ли субтитры речи (текст, меняющийся вместе со словами).\n"
            "summary — структура и стиль (хук, развитие, финал, темп, приёмы) в 2–4 предложениях."
        ),
    })
    try:
        res = await llm.parse(model=get_settings().llm_planner_model, system="Ты анализируешь монтаж коротких видео.",
                              content=content, output_format=RefDescription, max_tokens=8000, effort="low")
    except llm.LLMError as e:
        log.warning("reference description failed: %s", e)
        return ref
    new_shots = list(ref.shots)
    for item in res.shots:
        if 0 <= item.index < len(idx):
            i = idx[item.index]
            new_shots[i] = new_shots[i].model_copy(update={
                "description": item.description, "shot_type": item.shot_type, "has_text": item.has_text,
                "effect": item.effect})
    return ref.model_copy(update={"shots": new_shots, "summary": res.summary, "overlay_texts": res.overlay_texts,
                                  "has_captions": res.has_captions})
