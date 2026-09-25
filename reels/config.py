"""Настройки приложения (из переменных окружения / .env)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="REELS_", extra="ignore")

    # Хранилище
    data_dir: Path = Path("data")
    database_url: str = ""  # по умолчанию sqlite в data_dir

    # Telegram
    telegram_token: str = ""
    # Локальный сервер Bot API (например http://telegram-bot-api:8081); пусто — облачный API
    telegram_api_base: str = ""

    # LLM. Ключ берётся SDK из ANTHROPIC_API_KEY. Без ключа работают эвристики.
    llm_enabled: bool = True
    llm_planner_model: str = "claude-opus-5"
    llm_vision_model: str = "claude-haiku-4-5"

    # Монтаж
    canvas_w: int = 1080
    canvas_h: int = 1920
    fps: int = 30
    preview_scale: float = 0.5
    default_duration_s: float = 20.0
    storyboard_approval: bool = True

    # Библиотеки ассетов
    music_dir: Path = Path("assets/music")
    fonts_dir: Path = Path("assets/fonts")
    font_name: str = "DejaVu Sans"

    # Ограничения
    max_assets_per_project: int = 40
    max_concurrent_jobs: int = 2

    # Распознавание речи (faster-whisper, опционально)
    whisper_model: str = "small"

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'reels.db').resolve()}"


@lru_cache
def get_settings() -> Settings:
    # ANTHROPIC_API_KEY и прочие переменные без префикса SDK читает из окружения — подгружаем .env туда
    from dotenv import load_dotenv

    load_dotenv(".env", override=False)
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
