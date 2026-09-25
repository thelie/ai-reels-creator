"""Модели планирования: шаблон стиля и черновик плана (то, что выдаёт LLM или эвристика)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..edl.models import ClipEffect, TextAnim, TextPosition, TransitionType


class Slot(BaseModel):
    """Место в структуре ролика (из референса): роль, длительность, подсказка о содержании."""

    role: str = "body"
    dur: float
    hint: str = ""
    effect: ClipEffect = "none"


class TemplateText(BaseModel):
    hook_style: str = "bold_white_caps"
    body_style: str = "white_on_dark"
    cta_style: str = "yellow_punch"
    pos: TextPosition = "center"
    anim: TextAnim = "pop"
    per_scene: bool = True  # показывать тексты сцен (а не только хук и CTA)
    # timed — хук на первые секунды; sticker — двухстрочный стикер-заголовок на весь ролик
    hook_mode: Literal["timed", "sticker"] = "timed"
    sticker_example: str = ""  # стикер из референса, образец для LLM
    sticker_y: float = 0.62


class TemplateCaptions(BaseModel):
    enabled: bool = True
    style: str = "karaoke_yellow"
    words_per_screen: int = 3


class StyleTemplate(BaseModel):
    id: str
    name: str
    description: str
    shot_min: float = 0.8
    shot_max: float = 3.0
    hook_dur: float = 1.5
    cut_on_beat: bool = True
    transitions: list[TransitionType] = Field(default_factory=list)  # пусто — только склейки
    transition_every: int = 0  # переход на каждой N-й склейке (0 — никогда)
    transition_dur: float = 0.25
    zoom: Literal["none", "subtle", "punch"] = "subtle"
    photo_zoom: float = 1.12
    text: TemplateText = Field(default_factory=TemplateText)
    captions: TemplateCaptions = Field(default_factory=TemplateCaptions)
    color: str = "none"
    prefer_speech: bool = False  # «говорящая голова»: сохранять звук клипов с речью
    music_gain_db: float = -4.0
    slots: list[Slot] = Field(default_factory=list)  # заполняется для шаблонов из референса

    @property
    def avg_shot(self) -> float:
        if self.slots:
            return sum(s.dur for s in self.slots) / len(self.slots)
        return (self.shot_min + self.shot_max) / 2


class PlanScene(BaseModel):
    segment_id: str = Field(description="id сегмента из списка материала")
    role: str = Field(description="hook | body | broll | cta | talking")
    duration: float = Field(description="желаемая длительность сцены, секунды")
    text: str = Field(description="текст на экране для этой сцены, пустая строка — без текста")
    keep_audio: bool = Field(description="оставить исходный звук (речь в кадре)")
    effect: ClipEffect = Field("none", description="эффект кадра: none | bw (чёрно-белый) | inset (уменьшенный кадр на "
                               "чёрном фоне)")


class PlanDraft(BaseModel):
    template_id: str
    title: str = Field(description="рабочее название ролика")
    hook_text: str = Field(description="цепляющий текст на первые 1–3 секунды, до 60 символов; для стикера — "
                           "две строки через \\n")
    scenes: list[PlanScene]
    cta_text: str = Field(description="призыв к действию в конце, пустая строка — без него")
    caption: str = Field(description="подпись к публикации")
    hashtags: list[str]
    color: str = Field(description="пресет цвета: none | punchy | warm | cool | film | bw")
