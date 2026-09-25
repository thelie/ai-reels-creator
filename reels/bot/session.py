"""HTTP-сессия aiogram, уважающая HTTPS_PROXY и SSL_CERT_FILE (корпоративные/облачные прокси).

Штатная AiohttpSession игнорирует переменные окружения прокси и доверяет только certifi.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl

from aiogram.__meta__ import __version__
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiohttp import ClientSession
from aiohttp.hdrs import USER_AGENT
from aiohttp.http import SERVER_SOFTWARE

log = logging.getLogger(__name__)

NETWORK_RETRIES = 3


def env_proxy() -> str | None:
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


class EnvProxySession(AiohttpSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        cafile = os.environ.get("SSL_CERT_FILE")
        if cafile and os.path.exists(cafile):
            self._connector_init["ssl"] = ssl.create_default_context(cafile=cafile)

    async def create_session(self) -> ClientSession:
        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                connector=self._connector_type(**self._connector_init),
                headers={USER_AGENT: f"{SERVER_SOFTWARE} aiogram/{__version__}"},
                trust_env=True,  # HTTPS_PROXY / NO_PROXY из окружения
            )
            self._should_reset_connector = False
        return self._session

    async def make_request(self, bot, method, timeout=None):
        """Повтор при обрыве соединения: прокси закрывает простаивающие keep-alive соединения."""
        for attempt in range(NETWORK_RETRIES):
            try:
                return await super().make_request(bot, method, timeout)
            except TelegramNetworkError:
                if attempt == NETWORK_RETRIES - 1:
                    raise
                log.warning("Telegram network error on %s, retry %d", type(method).__name__, attempt + 1)
                await asyncio.sleep(0.5 * (attempt + 1))
