"""Компьютерное зрение без LLM: сцены, качество, движение, лица."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

SAMPLE_FPS = 6.0
ANALYSIS_W = 256


@dataclass
class FrameStats:
    t: float
    sharpness: float
    brightness: float
    motion: float


@dataclass
class ShotInfo:
    start: float
    end: float
    frames: list[FrameStats] = field(default_factory=list)

    @property
    def dur(self) -> float:
        return self.end - self.start


def _hist(frame_hsv: np.ndarray) -> np.ndarray:
    h = cv2.calcHist([frame_hsv], [0, 1, 2], None, [16, 8, 8], [0, 180, 0, 256, 0, 256])
    return cv2.normalize(h, h).flatten()


def scan_video(path: str | Path, threshold: float = 0.45, min_shot: float = 0.6) -> tuple[list[ShotInfo], float]:
    """Один проход по видео: границы сцен (разница HSV-гистограмм) + покадровые метрики."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Не удалось открыть {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = n / fps if n else 0.0
    step = max(1, int(round(fps / SAMPLE_FPS)))

    shots: list[ShotInfo] = [ShotInfo(0.0, 0.0)]
    prev_hist = None
    prev_gray = None
    idx = 0
    last_t = 0.0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            t = idx / fps
            last_t = t
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (ANALYSIS_W, max(2, int(h * ANALYSIS_W / w))), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            hist = _hist(cv2.cvtColor(small, cv2.COLOR_BGR2HSV))
            motion = float(np.mean(cv2.absdiff(gray, prev_gray))) / 255.0 if prev_gray is not None else 0.0
            if prev_hist is not None:
                dist = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
                if dist > threshold and t - shots[-1].start >= min_shot:
                    shots[-1].end = t
                    shots.append(ShotInfo(t, t))
                    motion = 0.0  # скачок на склейке — не движение
            shots[-1].frames.append(
                FrameStats(
                    t=t,
                    sharpness=float(cv2.Laplacian(gray, cv2.CV_64F).var()),
                    brightness=float(np.mean(gray)) / 255.0,
                    motion=motion,
                )
            )
            prev_hist, prev_gray = hist, gray
        idx += 1
    cap.release()
    duration = max(duration, last_t)
    shots[-1].end = duration
    # Последний «хвостик» короче min_shot приклеиваем к предыдущему
    if len(shots) > 1 and shots[-1].dur < min_shot:
        tail = shots.pop()
        shots[-1].end = tail.end
        shots[-1].frames += tail.frames
    return shots, duration


def quality_score(frames: list[FrameStats]) -> float:
    """0..1: резкость, экспозиция, умеренное движение."""
    if not frames:
        return 0.0
    sharp = np.median([f.sharpness for f in frames])
    bright = np.median([f.brightness for f in frames])
    motion = np.median([f.motion for f in frames])
    s_sharp = float(np.clip(np.log10(sharp + 1) / 3.0, 0, 1))  # ~1000 дисперсия Лапласиана -> 1
    s_expo = float(1.0 - np.clip(abs(bright - 0.5) / 0.4, 0, 1))
    s_motion = float(1.0 - np.clip((motion - 0.12) / 0.2, 0, 1))  # сильная тряска штрафуется
    return round(0.5 * s_sharp + 0.3 * s_expo + 0.2 * s_motion, 3)


def best_window(frames: list[FrameStats], start: float, end: float, length: float) -> float:
    """Начало лучшего окна длиной length внутри [start, end] (резкость + немного движения)."""
    if end - start <= length + 1e-3 or not frames:
        return start
    best_t, best_v = start, -1e9
    t = start
    while t + length <= end + 1e-6:
        win = [f for f in frames if t <= f.t <= t + length]
        if win:
            v = np.mean([np.log10(f.sharpness + 1) + 2.0 * min(f.motion, 0.1) for f in win])
            if v > best_v:
                best_v, best_t = v, t
        t += 0.25
    return best_t


@lru_cache(maxsize=1)
def _face_detector() -> cv2.CascadeClassifier:
    return cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))


def detect_anchor(image_path: str | Path) -> tuple[float, float, int]:
    """Точка интереса для кадрирования в 9:16: центр крупнейшего лица, иначе центр «энергии» кадра.

    Возвращает (x, y, число_лиц) в долях кадра.
    """
    img = cv2.imread(str(image_path))
    if img is None:
        return 0.5, 0.5, 0
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = _face_detector().detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(max(24, w // 20),) * 2)
    if len(faces):
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        # Лицо чуть выше центра кадра выглядит естественнее
        return (x + fw / 2) / w, min(1.0, (y + fh / 2) / h + 0.08), len(faces)
    # Центр масс градиентов — грубая оценка значимой области
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    energy = cv2.GaussianBlur(np.abs(gx) + np.abs(gy), (0, 0), sigmaX=w / 20)
    total = float(energy.sum())
    if total <= 0:
        return 0.5, 0.5, 0
    cx = float((energy.sum(axis=0) * np.arange(w)).sum() / total) / w
    cy = float((energy.sum(axis=1) * np.arange(h)).sum() / total) / h
    # Смягчаем к центру, чтобы не уезжать в край из-за текстуры
    return 0.5 + (cx - 0.5) * 0.6, 0.5 + (cy - 0.5) * 0.6, 0


def image_quality(image_path: str | Path) -> float:
    img = cv2.imread(str(image_path))
    if img is None:
        return 0.0
    h, w = img.shape[:2]
    small = cv2.resize(img, (ANALYSIS_W, max(2, int(h * ANALYSIS_W / w))))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return quality_score(
        [FrameStats(0, float(cv2.Laplacian(gray, cv2.CV_64F).var()), float(np.mean(gray)) / 255.0, 0.0)]
    )
