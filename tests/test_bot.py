"""Разводка хендлеров бота на поддельной сессии Telegram (без сети)."""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, SendMessage
from aiogram.types import CallbackQuery, Chat, File, Message, PhotoSize, Update, User

from reels.bot import handlers
from reels.config import Settings
from reels.orchestrator.service import ReelsService
from reels.storage.db import Database

USER = User(id=100, is_bot=False, first_name="Тест")
CHAT = Chat(id=100, type="private")


class FakeSession(BaseSession):
    def __init__(self, files: dict[str, Path]):
        super().__init__()
        self.files = files
        self.calls: list = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id=method.file_id, file_path=method.file_id)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(), chat=CHAT, text=method.text)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield self.files[url.rsplit("/", 1)[-1]].read_bytes()

    async def close(self):
        pass

    def texts(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, SendMessage)]


@pytest.fixture
async def env(tmp_path, media, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", music_dir=tmp_path / "none")
    settings.data_dir.mkdir()
    monkeypatch.setattr(handlers, "get_settings", lambda: settings)
    db = Database(settings.db_url)
    await db.init()
    session = FakeSession({"photo1": media["photo"], "photo2": media["photo2"]})
    bot = Bot("42:TEST", session=session)
    dp = Dispatcher()
    dp["svc"] = ReelsService(db, settings)
    dp.include_router(handlers.router)
    yield bot, dp, session
    handlers.router._parent_router = None  # роутер модульный — отвязываем для следующего теста
    await db.close()


def _msg(i: int, **kw) -> Update:
    return Update(update_id=i, message=Message(message_id=i, date=datetime.now(), chat=CHAT, from_user=USER, **kw))


def _cb(i: int, data: str) -> Update:
    msg = Message(message_id=999, date=datetime.now(), chat=CHAT, from_user=USER, text="x")
    return Update(update_id=i, callback_query=CallbackQuery(id=str(i), from_user=USER, chat_instance="c",
                                                            message=msg, data=data))


async def test_dialog(env):
    bot, dp, session = env
    await dp.feed_update(bot, _msg(1, text="/start"))
    assert "Reels" in session.texts()[-1]

    await dp.feed_update(bot, _msg(2, text="/new"))
    await dp.feed_update(bot, _cb(3, "build"))  # без материала
    assert any("хотя бы одно" in t for t in session.texts())

    await dp.feed_update(bot, _msg(4, text="Мой хук\nВторая строка"))
    assert "сценарий" in session.texts()[-1]

    for i, fid in enumerate(("photo1", "photo2"), start=5):
        await dp.feed_update(bot, _msg(i, photo=[PhotoSize(file_id=fid, file_unique_id=fid, width=100, height=80)]))
    await asyncio.sleep(1.8)  # сводка после паузы (альбомы)
    summary = session.texts()[-1]
    assert "Фото: 2" in summary and "Мой хук" in summary

    await dp.feed_update(bot, _cb(10, "dur:15"))
    await dp.feed_update(bot, _cb(11, "tpl:photo_slideshow"))
    await dp.feed_update(bot, _cb(12, "build"))
    board = session.texts()[-1]
    assert board.startswith("📋 План · Слайд-шоу") and "Мой хук" in board

    # Текст после плана — это правка; без LLM бот объясняет, что нужна LLM
    await dp.feed_update(bot, _msg(13, text="сделай короче"))
    assert "LLM" in session.texts()[-1]
