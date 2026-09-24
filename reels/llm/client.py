"""Обёртка над Anthropic SDK: структурированные ответы (Pydantic) + картинки.

Если ключа нет или LLM выключена — `available()` возвращает False, и вызывающий код
использует эвристики. Любая ошибка API превращается в LLMError, чтобы пайплайн мог
откатиться на эвристику, а не падать.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from ..config import get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Модели, для которых включаем серверный фолбэк при отказе классификаторов безопасности
_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
# Модели с adaptive thinking / effort
_ADAPTIVE_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7")


class LLMError(RuntimeError):
    pass


_client: anthropic.AsyncAnthropic | None = None


def available() -> bool:
    s = get_settings()
    if not s.llm_enabled:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=3, timeout=300)
    return _client


def image_block(path: str | Path, max_side: int = 768) -> dict:
    """JPEG-блок изображения (уменьшенный, чтобы экономить токены)."""
    from io import BytesIO

    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=82)
    data = base64.standard_b64encode(buf.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


async def parse(
    *,
    model: str,
    system: str,
    content: list[dict] | str,
    output_format: type[T],
    max_tokens: int = 16000,
    effort: str | None = None,
) -> T:
    kwargs: dict = {}
    betas: list[str] = []
    if model.startswith(_ADAPTIVE_MODELS):
        kwargs["thinking"] = {"type": "adaptive"}
        if effort:
            kwargs["output_config"] = {"effort": effort}
    if model in _FALLBACK_MODELS:
        betas.append("server-side-fallback-2026-07-01")
        kwargs["fallbacks"] = "default"
    if betas:
        kwargs["betas"] = betas

    try:
        resp = await client().beta.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=output_format,
            **kwargs,
        )
    except anthropic.RateLimitError as e:
        raise LLMError(f"rate limited: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"connection error: {e}") from e

    if resp.stop_reason == "refusal":
        raise LLMError("model refused the request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("response truncated (max_tokens)")
    parsed = resp.parsed_output
    if parsed is None:
        raise LLMError("no structured output in response")
    log.info("LLM %s ok, usage in=%s out=%s", model, resp.usage.input_tokens, resp.usage.output_tokens)
    return parsed
