"""렌더러 공용 색상 · 폰트."""

from __future__ import annotations

import colorsys
from pathlib import Path

from PIL import ImageDraw, ImageFont

BG = (28, 32, 41)
PANEL = (36, 41, 52)
LINE = (58, 65, 80)
TEXT = (232, 236, 244)
DIM = (140, 150, 166)
GOLD = (232, 182, 74)
SILVER = (186, 194, 208)
BRONZE = (198, 134, 90)

FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/gulim.ttc",
]

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def font(size: int) -> ImageFont.FreeTypeFont:
    """한글 지원 폰트. 크기별로 캐시한다."""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    loaded = None
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                loaded = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    _FONT_CACHE[size] = loaded or ImageFont.load_default()
    return _FONT_CACHE[size]


def make_colors(n: int) -> list[tuple[int, int, int]]:
    """구분이 잘 되는 색 n개. 황금각으로 색상환을 건너뛰어 인접 색이 겹치지 않게 한다."""
    golden = 0.6180339887
    colors = []
    for i in range(n):
        hue = (0.02 + i * golden) % 1.0
        value = 0.88 if i % 2 == 0 else 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 0.62, value)
        colors.append((int(r * 255), int(g * 255), int(b * 255)))
    return colors


def truncate(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> str:
    """max_w 픽셀을 넘으면 말줄임표로 자른다."""
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text + "…"
