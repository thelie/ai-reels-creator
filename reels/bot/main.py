"""Точка входа Telegram-бота."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand

from ..analysis import speech
from ..config import get_settings
from ..llm import client as llm
from ..orchestrator.service import ReelsService
from ..storage.db import Database
from .handlers import router

log = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    if not settings.telegram_token:
        raise SystemExit("Укажите REELS_TELEGRAM_TOKEN")

    session = None
    if settings.telegram_api_base:
        # Локальный Bot API: файлы до 2 ГБ вместо 20 МБ на скачивание / 50 МБ на отправку
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_base, is_local=True))
    bot = Bot(settings.telegram_token, session=session)

    db = Database(settings.db_url)
    await db.init()
    dp = Dispatcher()
    dp["svc"] = ReelsService(db, settings)
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="new", description="Новый ролик"),
        BotCommand(command="ref", description="Следующее видео — референс"),
        BotCommand(command="status", description="Состояние проекта"),
        BotCommand(command="versions", description="История версий"),
        BotCommand(command="undo", description="Откатить последнюю правку"),
        BotCommand(command="help", description="Как пользоваться"),
    ])
    log.info("LLM: %s, распознавание речи: %s, локальный Bot API: %s",
             "вкл" if llm.available() else "выкл (эвристики)", "вкл" if speech.available() else "выкл",
             settings.telegram_api_base or "нет")
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
