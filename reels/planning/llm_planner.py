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
    if t.slots:
        brief["slots"] = [{"role": s.role, "dur": round(s.dur, 2), "hint": s.hint} for s in t.slots]
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
            parts.append("Это структура ролика-референса: сделай по одной сцене на каждый слот, "
                         "подбирая фрагменты, похожие по смыслу и крупности на подсказки слотов.")
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


async def describe_reference(ref: ReferenceAnalysis) -> ReferenceAnalysis:
    """Кадры референса -> роли и описания слотов (быстрая модель). Без LLM возвращает как есть."""
    from pydantic import BaseModel

    class RefShot(BaseModel):
        index: int
        description: str
        shot_type: str
        has_text: bool

    class RefDescription(BaseModel):
        shots: list[RefShot]
        summary: str

    if not llm.available() or not ref.shots:
        return ref
    shots = ref.shots[:24]
    content: list[dict] = []
    for i, s in enumerate(shots):
        content.append({"type": "text", "text": f"План {i} ({s.dur:.1f} с):"})
        content.append(llm.image_block(s.keyframe, 512))
    content.append({
        "type": "text",
        "text": "Это кадры ролика-референса по порядку. Для каждого плана: что в кадре и зачем он в структуре "
                "(до 15 слов), крупность (close_up/medium/wide/detail), есть ли текст поверх видео. "
                "В summary опиши структуру и стиль ролика (хук, развитие, финал, темп) в 2–3 предложениях — "
                "это будет инструкцией монтажёру, который сделает похожий ролик из другого материала.",
    })
    try:
        res = await llm.parse(model=get_settings().llm_vision_model, system="Ты анализируешь монтаж коротких видео.",
                              content=content, output_format=RefDescription, max_tokens=4000)
    except llm.LLMError as e:
        log.warning("reference description failed: %s", e)
        return ref
    new_shots = list(ref.shots)
    for item in res.shots:
        if 0 <= item.index < len(shots):
            st = item.shot_type if item.shot_type in {"close_up", "medium", "wide", "detail"} else "unknown"
            new_shots[item.index] = new_shots[item.index].model_copy(
                update={"description": item.description, "shot_type": st, "has_text": item.has_text})
    return ref.model_copy(update={"shots": new_shots, "summary": res.summary})
