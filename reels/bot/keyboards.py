"""Inline-клавиатуры бота."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup

from ..planning import library


def collecting(duration: float, template_id: str) -> InlineKeyboardMarkup:
    durs = [15, 30, 60]
    row = [B(text=("● " if abs(duration - d) < 0.5 else "") + f"{d} с", callback_data=f"dur:{d}") for d in durs]
    tpl = "авто" if template_id == "auto" else library.get(template_id).name
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row,
            [B(text=f"🎨 Стиль: {tpl}", callback_data="tpl:menu")],
            [B(text="▶️ Собрать ролик", callback_data="build")],
        ]
    )


def templates(current: str) -> InlineKeyboardMarkup:
    rows = [[B(text=("● " if current == "auto" else "") + "Авто (выберет ИИ)", callback_data="tpl:auto")]]
    for t in library.templates().values():
        rows.append([B(text=("● " if current == t.id else "") + t.name, callback_data=f"tpl:{t.id}")])
    rows.append([B(text="← Назад", callback_data="tpl:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def storyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [B(text="✅ Смонтировать превью", callback_data="render:preview")],
            [B(text="🔄 Другой вариант", callback_data="variant"), B(text="↩️ Откат", callback_data="undo")],
        ]
    )


def preview() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [B(text="✅ Финал 1080p", callback_data="render:final")],
            [B(text="🔄 Другой вариант", callback_data="variant"), B(text="↩️ Откат", callback_data="undo")],
        ]
    )


def delivered() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [B(text="🔄 Другой вариант", callback_data="variant"), B(text="🆕 Новый ролик", callback_data="new")]
        ]
    )
