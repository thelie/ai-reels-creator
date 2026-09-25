"""Распознавание речи (faster-whisper, опционально: pip install '.[speech]')."""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path

from ..config import get_settings
from .models import Word

log = logging.getLogger(__name__)

# Анализ идёт в нескольких потоках: модель загружаем и вызываем по одной (иначе гонки в CTranslate2)
_lock = threading.Lock()


def available() -> bool:
    if not get_settings().speech_enabled:
        return False
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    return WhisperModel(get_settings().whisper_model, device="auto", compute_type="int8")


def transcribe(path: str | Path, language: str | None = None) -> tuple[str, list[Word]]:
    """Текст и слова с таймкодами. Без faster-whisper возвращает пустой результат."""
    if not available():
        return "", []
    try:
        words: list[Word] = []
        texts: list[str] = []
        with _lock:
            segments, _info = _model().transcribe(str(path), language=language, word_timestamps=True,
                                                  vad_filter=True)
            for seg in segments:  # генератор: распознавание идёт при итерации — тоже под блокировкой
                texts.append(seg.text.strip())
                for w in seg.words or []:
                    token = w.word.strip()
                    if token:
                        words.append(Word(t=round(w.start, 3), d=round(max(w.end - w.start, 0.05), 3), w=token))
        return " ".join(texts).strip(), words
    except Exception:  # noqa: BLE001 — распознавание не должно ронять пайплайн
        log.exception("transcription failed for %s", path)
        return "", []
