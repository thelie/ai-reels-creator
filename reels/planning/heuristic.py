"""Планировщик без LLM: отбор лучших сегментов, раскладка сценария по сценам."""

from __future__ import annotations

import math
import random
import re

from ..analysis.models import AssetAnalysis, Segment
from .models import PlanDraft, PlanScene, StyleTemplate

CTA_WORDS = ("подпис", "ссылк", "пиши", "переход", "заказ", "сохрани", "коммент", "жми", "лайк")


def split_script(script: str) -> list[str]:
    """Сценарий -> короткие фразы для экрана (по строкам, затем по предложениям)."""
    lines = [ln.strip(" -•\t") for ln in script.splitlines() if ln.strip(" -•\t")]
    if len(lines) <= 1:
        lines = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", script.strip()) if s.strip()]
    out = []
    for ln in lines:
        # Длинные фразы экрану не нужны — режем по запятым
        if len(ln) > 70:
            out += [p.strip() for p in ln.split(",") if p.strip()]
        else:
            out.append(ln)
    return [x[:90] for x in out]


def _spread_repeats(items: list[Segment]) -> list[Segment]:
    """Повторы одного сегмента не ставим подряд: первые вхождения по порядку, повторы — в конец."""
    seen: set[str] = set()
    first, again = [], []
    for s in items:
        (again if s.id in seen else first).append(s)
        seen.add(s.id)
    out = first
    for s in again:
        # Вставляем повтор туда, где соседи отличаются
        pos = next((i for i in range(len(out), 0, -1)
                    if out[i - 1].id != s.id and (i == len(out) or out[i].id != s.id)), len(out))
        out.insert(pos, s)
    return out


def segment_score(seg: Segment, hook: dict[str, float]) -> float:
    return seg.quality + 0.4 * hook.get(seg.id, 0.0) + (0.15 if seg.faces else 0.0) + min(seg.motion, 0.08)


def plan(analyses: list[AssetAnalysis], template: StyleTemplate, duration: float, script: str = "",
         hook: dict[str, float] | None = None, seed: int = 0) -> PlanDraft:
    hook = hook or {}
    rng = random.Random(seed)
    order = {a.asset_id: i for i, a in enumerate(analyses)}
    segs = [s for a in analyses for s in a.segments if s.kind == "photo" or s.dur >= 0.5]
    if not segs:
        raise ValueError("Нет пригодного материала")

    # Число сцен: по слотам шаблона или по среднему плану
    if template.slots:
        n = len(template.slots)
        slot_durs = [s.dur for s in template.slots]
        k = duration / sum(slot_durs) if sum(slot_durs) else 1.0
        target = [d * k for d in slot_durs]
    else:
        n = max(3, round(duration / template.avg_shot))
        # Мало материала — меньше сцен, но длиннее (а не повторы одного и того же кадра)
        n = min(n, max(len(segs), math.ceil(duration / (template.shot_max * 1.5)), 3))
        target = [template.hook_dur] + [(duration - template.hook_dur) / max(n - 1, 1)] * (n - 1)

    # Отбор: лучшие по качеству, с разнообразием источников (штраф за повторы ассета)
    scored = sorted(segs, key=lambda s: segment_score(s, hook) + rng.uniform(0, 0.15 if seed else 0), reverse=True)
    picked: list[Segment] = []
    use_count: dict[str, int] = {}
    pool = list(scored)
    while len(picked) < n and pool:
        best = max(pool, key=lambda s: segment_score(s, hook) - 0.25 * use_count.get(s.asset_id, 0)
                   + rng.uniform(0, 0.1 if seed else 0))
        pool.remove(best)
        picked.append(best)
        use_count[best.asset_id] = use_count.get(best.asset_id, 0) + 1
    # Материала меньше, чем сцен, — повторно используем лучшие сегменты (другие окна)
    while len(picked) < n:
        picked.append(scored[len(picked) % len(scored)])

    # Хук — самый «цепляющий», остальное — в порядке загрузки (сохраняет хронологию истории)
    ranked = sorted(picked, key=lambda s: segment_score(s, hook) + 0.5 * hook.get(s.id, 0), reverse=True)
    # Другой вариант: другой хук из тройки лучших и, через раз, другой порядок сцен
    hook_seg = rng.choice(ranked[:3]) if seed else ranked[0]
    rest = [s for s in picked if s is not hook_seg]
    rest.sort(key=lambda s: (order.get(s.asset_id, 0), s.start))
    if seed % 2:
        rng.shuffle(rest)
    ordered = _spread_repeats([hook_seg, *rest])

    lines = split_script(script) if script else []
    hook_text = lines.pop(0) if lines else ""
    cta_text = ""
    if lines and any(w in lines[-1].lower() for w in CTA_WORDS):
        cta_text = lines.pop()

    scenes: list[PlanScene] = []
    body_idx = list(range(1, n))
    text_for: dict[int, str] = {}
    if lines and body_idx and template.text.per_scene:
        step = len(body_idx) / len(lines)
        for j, line in enumerate(lines):
            text_for[body_idx[min(int(j * step), len(body_idx) - 1)]] = line
    for i, seg in enumerate(ordered):
        keep_audio = template.prefer_speech and seg.has_speech
        dur = target[i] if i < len(target) else template.avg_shot
        if keep_audio:
            dur = max(dur, min(seg.dur, template.shot_max))
        role = "hook" if i == 0 else ("cta" if i == n - 1 and cta_text else ("talking" if keep_audio else "body"))
        scenes.append(PlanScene(segment_id=seg.id, role=role, duration=round(dur, 2), text=text_for.get(i, ""),
                                keep_audio=keep_audio))

    title = hook_text or "Новый ролик"
    return PlanDraft(template_id=template.id, title=title[:60], hook_text=hook_text[:80], scenes=scenes,
                     cta_text=cta_text[:80], caption=hook_text, hashtags=[], color=template.color)
