from __future__ import annotations

import os
from pathlib import Path

import pytest

# Распознавание речи в тестах выключено: медленно и требует скачивания модели
os.environ["REELS_SPEECH_ENABLED"] = "false"

from tests.media_fixtures import make_click_track, make_photo, make_video


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """Тесты не ходят в API: LLM выключена, работают эвристики."""
    from reels.llm import client as llm

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm, "available", lambda: False)  # даже если в .env есть ключ


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    return {
        "multi": make_video(d / "multi.mp4", 9, scenes=3),
        "short": make_video(d / "short.mp4", 4, src="testsrc"),
        "vertical": make_video(d / "vertical.mp4", 3, size="720x1280", tone=None),
        "photo": make_photo(d / "photo.jpg"),
        "photo2": make_photo(d / "photo2.jpg", size="1200x1600", src="smptebars"),
        "music": make_click_track(d / "music.mp3", 120, 30),
    }
