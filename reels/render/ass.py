"""Генерация субтитров ASS (libass) для текста на экране и «караоке»-субтитров."""

from __future__ import annotations

import re

from ..edl.models import EDL, CaptionWord, TextOverlay
from ..edl.styles import SAFE_ZONES, TEXT_POS_Y, TEXT_STYLES, TextStyle

CAPTION_Y = 0.62
CHAR_W = 0.56  # средняя ширина символа относительно кегля


def ass_color(hex_rgb: str, alpha: float = 0.0) -> str:
    """#RRGGBB + прозрачность (0 — непрозрачно) -> &HAABBGGRR."""
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    a = max(0, min(255, round(alpha * 255)))
    return f"&H{a:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# libass не рисует цветные эмодзи — убираем их из титров, чтобы не было «квадратиков»
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def escape(text: str) -> str:
    text = _EMOJI.sub("", text)
    return " ".join(text.replace("\\", "/").replace("{", "(").replace("}", ")").split())


def wrap(text: str, font_size: int, max_width: float) -> list[str]:
    """Перенос по словам по оценочной ширине символа (WrapStyle=2 — libass не переносит сам)."""
    max_chars = max(8, int(max_width / (font_size * CHAR_W)))
    lines: list[str] = []
    cur = ""
    for word in text.split():
        cand = f"{cur} {word}".strip()
        if len(cand) <= max_chars or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _style_line(name: str, st: TextStyle, font: str) -> str:
    if st.box:
        border_style, outline_col = 3, ass_color(st.box_color, st.box_alpha)
    else:
        border_style, outline_col = 1, ass_color(st.outline)
    return (
        f"Style: {name},{st.font or font},{st.font_size},{ass_color(st.primary)},{ass_color(st.highlight)},"
        f"{outline_col},&H80000000,{-1 if st.bold else 0},0,0,0,100,100,0,{st.angle:g},"
        f"{border_style},{st.outline_w},0,5,0,0,0,1"
    )


def _layout(w: int) -> tuple[float, float]:
    """Центр по X и доступная ширина с учётом правой колонки кнопок."""
    left = w * 0.06
    right = w * (1 - SAFE_ZONES["right"])
    return (left + right) / 2, right - left


def _text_event(t: TextOverlay, w: int, h: int) -> str:
    st = TEXT_STYLES[t.style]
    cx, avail = _layout(w)
    content = t.content.upper() if st.uppercase else t.content
    lines = wrap(escape(content), st.font_size, avail)
    y = (t.y if t.y is not None else TEXT_POS_Y[t.pos]) * h
    # Не заезжаем в нижнюю безопасную зону
    block_h = len(lines) * st.font_size * 1.15
    y = min(y, h * (1 - SAFE_ZONES["bottom"]) - block_h / 2)
    y = max(y, h * SAFE_ZONES["top"] + block_h / 2)
    tags = [rf"\an5\pos({cx:.0f},{y:.0f})"]
    if t.anim == "pop":
        tags.append(r"\fscx60\fscy60\t(0,110,\fscx108\fscy108)\t(110,190,\fscx100\fscy100)\fad(0,150)")
    elif t.anim == "fade":
        tags.append(r"\fad(200,200)")
    text = "{" + "".join(tags) + "}" + r"\N".join(lines)
    return f"Dialogue: 1,{ass_time(t.at)},{ass_time(t.at + t.dur)},{t.style},,0,0,0,,{text}"


def _caption_chunks(words: list[CaptionWord], per_screen: int) -> list[list[CaptionWord]]:
    chunks: list[list[CaptionWord]] = []
    cur: list[CaptionWord] = []
    for w in words:
        gap = w.t - (cur[-1].t + cur[-1].d) if cur else 0.0
        if cur and (len(cur) >= per_screen or gap > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _caption_events(edl: EDL, w: int, h: int) -> list[str]:
    cap = edl.captions
    if not cap or not cap.words:
        return []
    st = TEXT_STYLES[cap.style]
    cx, avail = _layout(w)
    hl = ass_color(st.highlight)
    events = []
    for chunk in _caption_chunks(cap.words, cap.words_per_screen):
        texts = [escape(x.w.upper() if st.uppercase else x.w) for x in chunk]
        chunk_end = chunk[-1].t + chunk[-1].d
        for i, word in enumerate(chunk):
            start = word.t
            end = chunk[i + 1].t if i + 1 < len(chunk) else chunk_end
            if end <= start:
                continue
            parts = [
                (rf"{{\c{hl}}}{txt}{{\r}}" if j == i else txt) for j, txt in enumerate(texts)
            ]
            line = " ".join(parts)
            pos = rf"{{\an5\pos({cx:.0f},{h * CAPTION_Y:.0f})}}"
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{cap.style},,0,0,0,,{pos}{line}")
    return events


def build_ass(edl: EDL, font: str) -> str:
    w, h = edl.canvas.w, edl.canvas.h
    used = {t.style for t in edl.texts}
    if edl.captions and edl.captions.words:
        used.add(edl.captions.style)
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *(_style_line(name, TEXT_STYLES[name], font) for name in sorted(used)),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = [_text_event(t, w, h) for t in edl.texts] + _caption_events(edl, w, h)
    return "\n".join(header + events) + "\n"
