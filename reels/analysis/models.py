"""Результаты анализа материала (сохраняются в БД как JSON)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ShotType = Literal["close_up", "medium", "wide", "detail", "unknown"]


class Word(BaseModel):
    t: float  # время в исходнике
    d: float
    w: str


class Segment(BaseModel):
    """Непрерывный кусок исходника, пригодный как один шот ролика."""

    id: str
    asset_id: str
    kind: Literal["video", "photo"]
    start: float = 0.0
    end: float = 0.0  # для фото 0
    keyframe: str = ""  # путь к jpeg
    quality: float = 0.5
    motion: float = 0.0
    anchor_x: float = 0.5
    anchor_y: float = 0.5
    faces: int = 0
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    shot_type: ShotType = "unknown"
    has_speech: bool = False
    transcript: str = ""
    words: list[Word] = Field(default_factory=list)

    @property
    def dur(self) -> float:
        return self.end - self.start


class AssetAnalysis(BaseModel):
    asset_id: str
    kind: Literal["video", "photo"]
    duration: float = 0.0
    width: int
    height: int
    has_audio: bool = False
    segments: list[Segment] = Field(default_factory=list)


class ReferenceShot(BaseModel):
    start: float
    dur: float
    keyframe: str = ""
    description: str = ""
    shot_type: ShotType = "unknown"
    has_text: bool = False
    effect: Literal["none", "bw", "inset"] = "none"


class OverlayText(BaseModel):
    """Текст поверх видео в референсе."""

    text: str
    role: Literal["persistent_title", "section_label", "hook", "cta", "other"]


class ReferenceAnalysis(BaseModel):
    """Упрощённый разбор ролика-референса (MVP): ритм, биты, структура."""

    duration: float
    shots: list[ReferenceShot]
    bpm: float = 0.0
    cut_on_beat_ratio: float = 0.0
    has_speech: bool = False
    transcript: str = ""
    summary: str = ""  # описание структуры/стиля от LLM (если доступна)
    overlay_texts: list[OverlayText] = Field(default_factory=list)
    has_captions: bool | None = None  # None — неизвестно (без LLM)
