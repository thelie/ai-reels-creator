"""Хендлеры Telegram-бота: приём материала, диалог, согласование, выдача."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

from ..analysis import speech
from ..config import get_settings
from ..media.ffmpeg import probe
from ..orchestrator.service import ReelsService, UserError
from ..orchestrator.states import BUSY, State
from ..planning import library
from . import keyboards as kb

log = logging.getLogger(__name__)
router = Router(name="reels")

HELP = (
    "Я монтирую вертикальные ролики для Reels и TikTok.\n\n"
    "1. Пришлите фото и видео (можно альбомами; большие — файлом).\n"
    "2. По желанию — примерный сценарий текстом или голосом: первая строка станет хуком.\n"
    "3. По желанию — ролик-пример: видео с подписью «референс» (или команда /ref перед ним). "
    "Я повторю его ритм и структуру на вашем материале.\n"
    "4. По желанию — музыку аудиофайлом.\n"
    "5. Нажмите «Собрать ролик».\n\n"
    "После плана и превью можно просто написать, что поменять: «хук короче», "
    "«убери третью сцену», «сделай 15 секунд».\n\n"
    "Команды: /new — новый проект, /ref — следующее видео будет референсом, "
    "/status — состояние, /versions — история версий, /undo — откат."
)
REF_WORDS = re.compile(r"\b(реф|референс|пример|ref|reference)\w*", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")

# Состояние, которое не нужно хранить в БД
_summary_msg: dict[int, int] = {}
_summary_task: dict[int, asyncio.Task] = {}
_ref_next: set[int] = set()


class ProgressMessage:
    """Одно редактируемое сообщение с прогрессом (не чаще раза в секунду)."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id
        self.msg_id: int | None = None
        self.last = ""
        self.last_t = 0.0

    async def __call__(self, text: str) -> None:
        if text == self.last:
            return
        now = time.monotonic()
        if self.msg_id and now - self.last_t < 1.0 and "0/" not in text:
            return
        self.last, self.last_t = text, now
        try:
            if self.msg_id is None:
                self.msg_id = (await self.bot.send_message(self.chat_id, text)).message_id
            else:
                await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.msg_id)
        except TelegramAPIError:  # прогресс — не критичен, сетевые сбои не должны ронять задачу
            log.warning("progress update failed", exc_info=True)

    async def done(self) -> None:
        if self.msg_id:
            try:
                await self.bot.delete_message(self.chat_id, self.msg_id)
            except TelegramAPIError:
                pass


# ---------- сводка проекта ----------


async def send_summary(bot: Bot, svc: ReelsService, chat_id: int, user_id: int) -> None:
    p = await svc.current_project(user_id, chat_id)
    text = await svc.summary(p.id)
    old = _summary_msg.pop(chat_id, None)
    if old:
        try:
            await bot.delete_message(chat_id, old)
        except TelegramBadRequest:
            pass
    msg = await bot.send_message(chat_id, text, reply_markup=kb.collecting(p.duration, p.template_id))
    _summary_msg[chat_id] = msg.message_id


def schedule_summary(bot: Bot, svc: ReelsService, chat_id: int, user_id: int, delay: float = 1.5) -> None:
    """Альбом приходит пачкой сообщений — показываем сводку один раз после паузы."""
    task = _summary_task.pop(chat_id, None)
    if task:
        task.cancel()

    async def later() -> None:
        try:
            await asyncio.sleep(delay)
            await send_summary(bot, svc, chat_id, user_id)
        except asyncio.CancelledError:
            pass

    _summary_task[chat_id] = asyncio.create_task(later())


# ---------- команды ----------


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("new"))
async def cmd_new(message: Message, bot: Bot, svc: ReelsService) -> None:
    await svc.new_project(message.from_user.id, message.chat.id)
    _ref_next.discard(message.chat.id)
    await message.answer("Новый проект. Присылайте фото, видео, сценарий или пример ролика.")
    await send_summary(bot, svc, message.chat.id, message.from_user.id)


@router.message(Command("ref"))
async def cmd_ref(message: Message) -> None:
    _ref_next.add(message.chat.id)
    await message.answer("Ок, следующее видео будет референсом — ролик-пример, по которому я повторю ритм и структуру.")


@router.message(Command("status"))
async def cmd_status(message: Message, svc: ReelsService) -> None:
    p = await svc.current_project(message.from_user.id, message.chat.id)
    names = {
        State.COLLECTING: "сбор материала", State.ANALYZING: "анализ", State.PLANNING: "планирование",
        State.AWAITING_APPROVAL: "ждёт согласования плана", State.RENDERING: "рендер",
        State.PREVIEW_SENT: "превью отправлено", State.DELIVERED: "готово", State.FAILED: "ошибка",
    }
    text = await svc.summary(p.id) + f"\nСостояние: {names.get(State(p.state), p.state)}"
    if p.error and p.state == State.FAILED:
        text += f"\nПоследняя ошибка: {p.error[:200]}"
    await message.answer(text)


@router.message(Command("versions"))
async def cmd_versions(message: Message, svc: ReelsService) -> None:
    p = await svc.current_project(message.from_user.id, message.chat.id)
    vs = await svc.versions(p.id)
    if not vs:
        await message.answer("Версий пока нет.")
        return
    lines = [f"v{v.version}: {v.note} · {v.duration:.0f} с" for v in vs[-15:]]
    await message.answer("История версий:\n" + "\n".join(lines) + "\n\n/undo — вернуть предыдущую")


@router.message(Command("undo"))
async def cmd_undo(message: Message, bot: Bot, svc: ReelsService) -> None:
    p = await svc.current_project(message.from_user.id, message.chat.id)
    try:
        board = await svc.undo(p.id)
    except UserError as e:
        await message.answer(str(e))
        return
    await send_storyboard(bot, message.chat.id, board)


# ---------- приём материала ----------


async def _download(bot: Bot, file_id: str, suffix: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="tg_")) / f"file{suffix}"
    await bot.download(file_id, destination=tmp)
    return tmp


async def _ingest(message: Message, bot: Bot, svc: ReelsService, file_id: str, unique_id: str, kind: str,
                  suffix: str, name: str = "") -> None:
    p = await svc.current_project(message.from_user.id, message.chat.id)
    if p.state in BUSY:
        await message.answer("Сейчас идёт обработка — пришлите файл чуть позже.")
        return
    try:
        path = await _download(bot, file_id, suffix)
    except TelegramBadRequest as e:
        if "too big" in str(e).lower():
            await message.answer("Файл больше 20 МБ — облачный Bot API такие не отдаёт. "
                                 "Сожмите видео или подключите локальный сервер Bot API.")
            return
        raise
    if kind == "video" and (message.chat.id in _ref_next or REF_WORDS.search(message.caption or "")):
        kind = "reference"
        _ref_next.discard(message.chat.id)
    try:
        info = await asyncio.to_thread(probe, path)
        if kind in ("video", "reference") and info.kind != "video":
            kind = info.kind if info.kind in ("photo", "audio") else kind
        await svc.add_asset(p.id, kind, path, name=name, unique_id=unique_id)
    except UserError as e:
        await message.answer(str(e))
        return
    except Exception:  # noqa: BLE001
        log.exception("cannot ingest file")
        await message.answer("Не получилось прочитать этот файл 😕")
        return
    if kind == "reference":
        await message.answer("🎞 Принял референс — повторю его ритм и структуру.")
    if message.caption and kind != "reference" and not REF_WORDS.search(message.caption):
        await svc.add_script(p.id, message.caption)
    schedule_summary(bot, svc, message.chat.id, message.from_user.id)


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot, svc: ReelsService) -> None:
    ph = message.photo[-1]
    await _ingest(message, bot, svc, ph.file_id, ph.file_unique_id, "photo", ".jpg")


@router.message(F.video | F.video_note | F.animation)
async def on_video(message: Message, bot: Bot, svc: ReelsService) -> None:
    v = message.video or message.video_note or message.animation
    name = getattr(v, "file_name", None) or "video.mp4"
    await _ingest(message, bot, svc, v.file_id, v.file_unique_id, "video", Path(name).suffix or ".mp4", name)


@router.message(F.audio)
async def on_audio(message: Message, bot: Bot, svc: ReelsService) -> None:
    a = message.audio
    name = a.file_name or "music.mp3"
    await _ingest(message, bot, svc, a.file_id, a.file_unique_id, "audio", Path(name).suffix or ".mp3", name)


@router.message(F.document)
async def on_document(message: Message, bot: Bot, svc: ReelsService) -> None:
    d = message.document
    mime = d.mime_type or ""
    name = d.file_name or "file"
    kind = "photo" if mime.startswith("image/") else "video" if mime.startswith("video/") else \
        "audio" if mime.startswith("audio/") else None
    if kind is None:
        await message.answer("Этот тип файла не поддерживается. Пришлите фото, видео или аудио.")
        return
    await _ingest(message, bot, svc, d.file_id, d.file_unique_id, kind, Path(name).suffix or "", name)


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot, svc: ReelsService) -> None:
    if not speech.available():
        await message.answer("Голосовые пока не распознаю (не установлен faster-whisper). Напишите текстом.")
        return
    path = await _download(bot, message.voice.file_id, ".ogg")
    text, _ = await asyncio.to_thread(speech.transcribe, path)
    if not text:
        await message.answer("Не расслышал 🙉 Попробуйте ещё раз или напишите текстом.")
        return
    await message.answer(f"🗣 Распознал: «{text}»")
    await handle_text(message, bot, svc, text)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot, svc: ReelsService) -> None:
    await handle_text(message, bot, svc, message.text)


async def handle_text(message: Message, bot: Bot, svc: ReelsService, text: str) -> None:
    p = await svc.current_project(message.from_user.id, message.chat.id)
    if p.state in BUSY:
        await message.answer("Уже работаю над роликом, подождите немного ⏳")
        return
    if URL_RE.search(text) and len(text.split()) <= 3:
        await message.answer("Ссылки пока не скачиваю — перешлите сам ролик-пример файлом с подписью «референс».")
        return
    if p.state in (State.AWAITING_APPROVAL, State.PREVIEW_SENT, State.DELIVERED):
        await run_plan(bot, svc, message.chat.id, message.from_user.id, instruction=text)
        return
    await svc.add_script(p.id, text)
    await message.answer("📝 Добавил в сценарий.")
    schedule_summary(bot, svc, message.chat.id, message.from_user.id, delay=0.5)


# ---------- кнопки ----------


@router.callback_query(F.data.startswith("dur:"))
async def cb_duration(cb: CallbackQuery, svc: ReelsService) -> None:
    p = await svc.current_project(cb.from_user.id, cb.message.chat.id)
    d = float(cb.data.split(":")[1])
    p = await svc.update(p.id, duration=d, duration_set=True)
    await cb.answer(f"Длительность {d:.0f} с")
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.collecting(p.duration, p.template_id))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("tpl:"))
async def cb_template(cb: CallbackQuery, svc: ReelsService) -> None:
    p = await svc.current_project(cb.from_user.id, cb.message.chat.id)
    arg = cb.data.split(":", 1)[1]
    if arg == "menu":
        markup = kb.templates(p.template_id)
        await cb.answer()
    elif arg == "back":
        markup = kb.collecting(p.duration, p.template_id)
        await cb.answer()
    else:
        tid = arg if arg == "auto" or arg in library.templates() else "auto"
        p = await svc.update(p.id, template_id=tid)
        await cb.answer("Стиль выбран")
        markup = kb.collecting(p.duration, p.template_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "build")
async def cb_build(cb: CallbackQuery, bot: Bot, svc: ReelsService) -> None:
    await cb.answer("Начинаю!")
    await run_plan(bot, svc, cb.message.chat.id, cb.from_user.id)


@router.callback_query(F.data == "variant")
async def cb_variant(cb: CallbackQuery, bot: Bot, svc: ReelsService) -> None:
    await cb.answer("Делаю другой вариант")
    await run_plan(bot, svc, cb.message.chat.id, cb.from_user.id, variant=True)


@router.callback_query(F.data == "undo")
async def cb_undo(cb: CallbackQuery, bot: Bot, svc: ReelsService) -> None:
    p = await svc.current_project(cb.from_user.id, cb.message.chat.id)
    try:
        board = await svc.undo(p.id)
    except UserError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer("Вернул предыдущую версию")
    await send_storyboard(bot, cb.message.chat.id, board)


@router.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, bot: Bot, svc: ReelsService) -> None:
    await cb.answer()
    await svc.new_project(cb.from_user.id, cb.message.chat.id)
    await bot.send_message(cb.message.chat.id, "Новый проект. Присылайте материал.")


@router.callback_query(F.data.startswith("render:"))
async def cb_render(cb: CallbackQuery, bot: Bot, svc: ReelsService) -> None:
    quality = cb.data.split(":")[1]
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await run_render(bot, svc, cb.message.chat.id, cb.from_user.id, quality)


# ---------- сценарии ----------


async def send_storyboard(bot: Bot, chat_id: int, board) -> None:
    frames = [f for f in board.keyframes if Path(f).exists()]
    if len(frames) >= 2:
        await bot.send_media_group(chat_id, [InputMediaPhoto(media=FSInputFile(f)) for f in frames[:10]])
    elif frames:
        await bot.send_photo(chat_id, FSInputFile(frames[0]))
    await bot.send_message(chat_id, board.text + "\n\n✏️ Можно написать, что изменить.", reply_markup=kb.storyboard())


async def run_plan(bot: Bot, svc: ReelsService, chat_id: int, user_id: int, instruction: str | None = None,
                   variant: bool = False) -> None:
    p = await svc.current_project(user_id, chat_id)
    progress = ProgressMessage(bot, chat_id)
    if instruction:
        await progress("✏️ Вношу правки…")
    try:
        board = await svc.plan(p.id, progress, instruction=instruction, variant=variant)
    except UserError as e:
        await progress.done()
        await bot.send_message(chat_id, f"⚠️ {e}")
        return
    await progress.done()
    if get_settings().storyboard_approval:
        await send_storyboard(bot, chat_id, board)
    else:
        await run_render(bot, svc, chat_id, user_id, "preview")


async def run_render(bot: Bot, svc: ReelsService, chat_id: int, user_id: int, quality: str) -> None:
    p = await svc.current_project(user_id, chat_id)
    progress = ProgressMessage(bot, chat_id)
    try:
        path = await svc.render(p.id, quality, progress)  # type: ignore[arg-type]
    except UserError as e:
        await progress.done()
        await bot.send_message(chat_id, f"⚠️ {e}")
        return
    await progress("📤 Отправляю…")
    info = await asyncio.to_thread(probe, path)
    _, edl = await svc.latest_edl(p.id)
    try:
        if quality == "preview":
            await bot.send_video(chat_id, FSInputFile(path), width=info.width, height=info.height,
                                 duration=round(info.duration), supports_streaming=True,
                                 caption="Превью. Напишите, что поменять, или жмите «Финал».",
                                 reply_markup=kb.preview())
        else:
            meta = edl.meta
            caption = "\n".join(x for x in [meta.caption, " ".join(meta.hashtags)] if x)[:1000]
            await bot.send_video(chat_id, FSInputFile(path, filename="reel.mp4"), width=info.width,
                                 height=info.height, duration=round(info.duration), supports_streaming=True)
            await bot.send_document(chat_id, FSInputFile(path, filename="reel.mp4"),
                                    caption="Файл без пережатия — для публикации.")
            await bot.send_message(chat_id, f"✅ Готово!\n\n{caption}" if caption else "✅ Готово!",
                                   reply_markup=kb.delivered())
    except TelegramBadRequest as e:
        log.exception("send failed")
        await bot.send_message(chat_id, f"Не получилось отправить видео: {e.message}")
    finally:
        await progress.done()
