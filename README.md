# AI Reels Creator

ИИ-агент для автоматического монтажа вертикальных роликов Instagram Reels / TikTok из фото, видео,
примерного сценария и ролика-референса. Интерфейс — Telegram-бот.

Архитектура: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (раздел 13 — что реализовано в MVP).

## Как это работает

```
файлы из Telegram ─► анализ (сцены, качество, лица, речь, биты, описания кадров)
                  ─► план (LLM или эвристика) ─► EDL (монтажный лист, JSON)
                  ─► раскадровка на согласование ─► превью 540p ─► правки ─► финал 1080p
```

LLM не монтирует видео сама: она выдаёт компактный план (какие фрагменты, порядок, длительности,
тексты), а детерминированный builder и рендерер на FFmpeg превращают его в ролик. Без ключа
Anthropic всё работает на эвристиках: не будет описаний кадров, выбора стиля ИИ и правок текстом.

## Быстрый старт

Нужны Python 3.11+ и FFmpeg (с libass).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # + ".[speech]" для распознавания речи (faster-whisper)
cp .env.example .env              # REELS_TELEGRAM_TOKEN, ANTHROPIC_API_KEY
reels-bot
```

Docker (бот + локальный сервер Bot API для файлов до 2 ГБ):

```bash
cp .env.example .env   # заполнить, включая TELEGRAM_API_ID / TELEGRAM_API_HASH
docker compose up -d --build
```

Музыку по умолчанию бот берёт из `assets/music/` (положите туда лицензированные треки),
если пользователь не прислал свою.

## Бот

1. `/new` — новый проект.
2. Пришлите фото и видео (альбомами или файлами), сценарий текстом или голосом
   (первая строка станет хуком), при желании — музыку аудиофайлом.
3. Ролик-пример: видео с подписью «референс» или команда `/ref` перед ним.
   Бот повторит ритм монтажа, синхронизацию с битом и структуру на вашем материале.
4. Выберите длительность и стиль, нажмите «Собрать ролик». Бот пришлёт раскадровку.
5. «Смонтировать превью», затем «Финал 1080p». На любом шаге можно написать правку текстом:
   «хук короче», «убери третью сцену», «сделай 15 секунд». Есть кнопки «Другой вариант» и
   «Откат», а также команды `/versions` и `/undo`.

## CLI (без Telegram)

```bash
reels templates
reels make path/to/media --script "Выходные в Казани\nКремль\nРынок\nПодпишись" \
    --duration 20 --template auto --out reel.mp4
reels make path/to/media --reference example.mp4 --music track.mp3 --out reel.mp4
```

## Структура

```
reels/
  bot/            Telegram: хендлеры, клавиатуры, прогресс, сводка по альбомам
  orchestrator/   машина состояний проекта и сервис (анализ → план → рендер → правки → версии)
  analysis/       прокси, детекция сцен, качество, лица, биты, речь, описания кадров (LLM), референс
  planning/       шаблоны стиля, эвристический и LLM-планировщик, builder PlanDraft → EDL
  edl/            контракт EDL, пресеты стилей, валидатор (безопасные зоны, диапазоны, тайминги)
  render/         компилятор EDL → FFmpeg (кеш клипов, xfade, zoompan, ASS-титры, микс), QA
  llm/            обёртка Anthropic SDK (структурированные ответы, изображения)
  storage/        SQLAlchemy: пользователи, проекты, ассеты, версии EDL, рендеры
```

## Настройки

Переменные окружения с префиксом `REELS_` (см. `reels/config.py`, `.env.example`):
`TELEGRAM_TOKEN`, `TELEGRAM_API_BASE`, `DATA_DIR`, `DATABASE_URL`, `LLM_PLANNER_MODEL`,
`LLM_VISION_MODEL`, `DEFAULT_DURATION_S`, `STORYBOARD_APPROVAL`, `MUSIC_DIR`, `FONT_NAME`,
`WHISPER_MODEL`. Ключ Anthropic — стандартный `ANTHROPIC_API_KEY`.

## Тесты

```bash
pytest -q        # синтетические медиа генерируются ffmpeg; сеть и API не нужны
ruff check reels tests
```
