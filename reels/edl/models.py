"""EDL (Edit Decision List) — центральный контракт между планировщиком и рендерером.

LLM никогда не пишет EDL напрямую: она выдаёт компактный план (planning.models.PlanDraft),
а детерминированный builder превращает его в EDL. Рендерер понимает только EDL.

Временная модель: клипы идут последовательно. Клип i занимает [at, at + dur] выходного
таймлайна. Если у клипа i+1 есть transition_in длительностью td, он начинается раньше
конца предыдущего: at[i+1] = at[i] + dur[i] - td (перекрытие для перехода).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TransitionType = Literal["fade", "dissolve", "slide_left", "slide_up", "zoom_in", "whip", "flash"]
TextPosition = Literal["top", "center", "bottom"]
TextAnim = Literal["none", "fade", "pop"]


class Canvas(BaseModel):
    w: int = 1080
    h: int = 1920
    fps: int = 30


class Source(BaseModel):
    """Исходный файл, на который ссылаются клипы."""

    asset_id: str
    kind: Literal["video", "photo"]
    path: str
    proxy_path: str | None = None
    duration: float = 0.0  # для фото 0
    width: int
    height: int
    has_audio: bool = False


class Reframe(BaseModel):
    """Кадрирование в 9:16: точка интереса (0..1) и зум (1.0 = «cover» без запаса)."""

    anchor_x: float = Field(0.5, ge=0, le=1)
    anchor_y: float = Field(0.5, ge=0, le=1)
    zoom_from: float = Field(1.0, ge=1.0, le=3.0)
    zoom_to: float = Field(1.0, ge=1.0, le=3.0)


class Transition(BaseModel):
    type: TransitionType
    dur: float = Field(0.3, gt=0, le=1.5)


class VideoClip(BaseModel):
    id: str
    asset_id: str
    src_in: float = Field(0.0, ge=0)
    at: float = Field(ge=0)
    dur: float = Field(gt=0)
    speed: float = Field(1.0, gt=0.1, le=4.0)
    reframe: Reframe = Field(default_factory=Reframe)
    keep_audio: bool = False
    audio_gain_db: float = 0.0
    transition_in: Transition | None = None
    role: str = ""

    @property
    def src_out(self) -> float:
        return self.src_in + self.dur * self.speed

    @property
    def end(self) -> float:
        return self.at + self.dur


class TextOverlay(BaseModel):
    at: float = Field(ge=0)
    dur: float = Field(gt=0)
    content: str
    style: str = "bold_white"
    pos: TextPosition = "center"
    anim: TextAnim = "pop"


class CaptionWord(BaseModel):
    t: float = Field(ge=0)  # время на выходном таймлайне
    d: float = Field(gt=0)
    w: str


class Captions(BaseModel):
    words: list[CaptionWord] = Field(default_factory=list)
    style: str = "karaoke_yellow"
    words_per_screen: int = Field(3, ge=1, le=8)


class Music(BaseModel):
    path: str
    src_in: float = Field(0.0, ge=0)
    gain_db: float = -4.0
    fade_in: float = 0.0
    fade_out: float = 1.0
    duck_db: float = -14.0  # приглушение музыки под речью клипов с keep_audio


class Meta(BaseModel):
    title: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)


class EDL(BaseModel):
    version: int = 1
    canvas: Canvas = Field(default_factory=Canvas)
    sources: dict[str, Source]
    clips: list[VideoClip]
    texts: list[TextOverlay] = Field(default_factory=list)
    captions: Captions | None = None
    music: Music | None = None
    color: str = "none"  # пресет цветокоррекции, см. render.color
    template_id: str = ""
    meta: Meta = Field(default_factory=Meta)

    @property
    def duration(self) -> float:
        return max((c.end for c in self.clips), default=0.0)
