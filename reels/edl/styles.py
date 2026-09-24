"""Пресеты оформления, на которые ссылается EDL (текст, субтитры, цвет)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextStyle:
    font_size: int  # при высоте кадра 1920
    primary: str  # цвет текста, #RRGGBB
    outline: str
    outline_w: float
    bold: bool = True
    box: bool = False  # плашка под текстом
    box_color: str = "#000000"
    box_alpha: float = 0.0  # 0 — непрозрачная
    uppercase: bool = False
    highlight: str = "#FFE600"  # цвет активного слова (для субтитров)


TEXT_STYLES: dict[str, TextStyle] = {
    "bold_white": TextStyle(font_size=84, primary="#FFFFFF", outline="#000000", outline_w=5),
    "bold_white_caps": TextStyle(font_size=80, primary="#FFFFFF", outline="#000000", outline_w=5, uppercase=True),
    "yellow_punch": TextStyle(font_size=90, primary="#FFE600", outline="#000000", outline_w=6, uppercase=True),
    "black_on_white": TextStyle(
        font_size=64, primary="#111111", outline="#FFFFFF", outline_w=14, box=True, box_color="#FFFFFF"
    ),
    "white_on_dark": TextStyle(
        font_size=64, primary="#FFFFFF", outline="#000000", outline_w=14, box=True, box_color="#000000", box_alpha=0.35
    ),
    "minimal": TextStyle(font_size=60, primary="#FFFFFF", outline="#000000", outline_w=2, bold=False),
    # Стили субтитров
    "karaoke_yellow": TextStyle(font_size=76, primary="#FFFFFF", outline="#000000", outline_w=6, uppercase=True),
    "karaoke_green": TextStyle(
        font_size=76, primary="#FFFFFF", outline="#000000", outline_w=6, uppercase=True, highlight="#39FF6A"
    ),
}

# Цветокоррекция: пресет -> цепочка фильтров ffmpeg
COLOR_PRESETS: dict[str, str] = {
    "none": "",
    "punchy": "eq=contrast=1.08:saturation=1.25:gamma=0.98",
    "warm": "colorbalance=rs=0.06:gs=0.01:bs=-0.06:rm=0.04:bm=-0.04,eq=saturation=1.1",
    "cool": "colorbalance=rs=-0.05:bs=0.07:bm=0.04,eq=saturation=1.05",
    "film": "eq=contrast=0.95:saturation=0.85:gamma=1.04,colorbalance=rs=0.04:bs=-0.03",
    "bw": "hue=s=0,eq=contrast=1.1",
}

# Безопасные зоны интерфейса (доля высоты/ширины кадра, куда нельзя ставить текст)
SAFE_ZONES = {
    "top": 0.08,  # статус-бар, вкладки
    "bottom": 0.22,  # подпись, ник, музыка
    "right": 0.13,  # кнопки лайк/коммент/репост
}

# Вертикальная позиция центра текста (доля высоты)
TEXT_POS_Y = {"top": 0.20, "center": 0.47, "bottom": 0.68}
