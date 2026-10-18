"""描画ユーティリティ"""
from __future__ import annotations

import os
import sys

from PIL import ImageDraw, ImageFont

FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\BIZ-UDGothicB.ttc", r"C:\Windows\Fonts\YuGothB.ttc",
    r"C:\Windows\Fonts\meiryob.ttc", r"C:\Windows\Fonts\meiryo.ttc",
    # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]

STROKE_FILL = (0, 0, 0, 160)


class Fonts:
    def __init__(self, path: str | None = None):
        self.path = path or next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
        if not self.path:
            print("警告: 日本語フォントが見つかりません。--font で指定してください。", file=sys.stderr)
        self._cache = {}

    def __call__(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self._cache:
            self._cache[size] = (ImageFont.truetype(self.path, size) if self.path
                                 else ImageFont.load_default())
        return self._cache[size]


def parse_color(s: str) -> tuple:
    """'#RRGGBB' → (r, g, b)、'#RRGGBBAA' → (r, g, b, a)"""
    h = s.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in range(0, len(h), 2))


def rgba(c: tuple, a: int) -> tuple:
    return tuple(c[:3]) + (a,)


def panel(d: ImageDraw.ImageDraw, box, r=14, fill=None):
    d.rounded_rectangle(box, r, fill=fill)


def text(d, xy, s, font, fill=(255, 255, 255), anchor="la", stroke=2):
    d.text(xy, s, font=font, fill=fill, anchor=anchor,
           stroke_width=stroke, stroke_fill=STROKE_FILL)


def fmt_time(sec: float, ms=False) -> str:
    sec = max(0.0, sec)
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    if ms:
        return f"{int(m) + h * 60}:{s:06.3f}" if h else f"{int(m)}:{s:06.3f}"
    return f"{h}:{m:02d}:{int(s):02d}"
