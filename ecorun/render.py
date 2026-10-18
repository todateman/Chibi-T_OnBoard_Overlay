"""プリセットに従ってオーバーレイのフレームを描く"""
from __future__ import annotations

from PIL import Image, ImageDraw

from .config import Preset
from .draw import Fonts, parse_color, text
from .telemetry import Telemetry
from .widgets import IMPLS, Ctx, resolve_theme

DEBUG_BOX = (0, 255, 255, 255)
DEBUG_KEEPOUT = (255, 0, 0, 50)


class OverlayRenderer:
    """静的レイヤ(全ウィジェットの static)を 1 回だけ作り、毎フレームそのコピーに dynamic を描く"""

    def __init__(self, tel: Telemetry, preset: Preset, t_start: float, t_end: float,
                 fonts: Fonts | None = None, debug_boxes: bool = False):
        self.tel, self.preset = tel, preset
        self.W, self.H = preset.canvas.width, preset.canvas.height
        self.fonts = fonts or Fonts(preset.theme.font or None)
        self.t_start, self.t_end = t_start, t_end
        self.debug_boxes = debug_boxes
        self.ctx = Ctx(tel, resolve_theme(preset.theme), preset.data, self.fonts, t_start, t_end)
        impls = [IMPLS[w.type](w, self.ctx) for w in preset.widgets if w.enabled]
        self.widgets = [w for w in impls if w.available()]
        self.skipped = [w.cfg.id for w in impls if not w.available()]
        self.base = self._build_static()

    def _build_static(self) -> Image.Image:
        img = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        for w in self.widgets:
            w.static(img)
        return img

    def render(self, ts: float) -> Image.Image:
        img = self.base.copy()
        for w in self.widgets:
            w.dynamic(img, ts)
        if self.debug_boxes:
            self._draw_debug(img)
        return img

    def _draw_debug(self, img: Image.Image):
        """各ウィジェットの枠と id、配置禁止帯を描く(エージェントや人がプレビューで要素を特定するため)"""
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        for k in self.preset.keepout:
            d.rectangle((k.x, k.y, k.x + k.w, k.y + k.h), fill=DEBUG_KEEPOUT, outline=(255, 0, 0, 160))
            text(d, (k.x + 6, k.y + 4), f"keepout: {k.name}", self.fonts(16), (255, 120, 120), stroke=2)
        for w in self.preset.widgets:
            if not w.enabled:
                continue
            col = DEBUG_BOX if w.id not in self.skipped else (160, 160, 160, 255)
            d.rectangle((w.x, w.y, w.x + w.w - 1, w.y + w.h - 1), outline=col, width=2)
            label = f"{w.id}" + ("" if w.id == w.type else f" ({w.type})")
            f = self.fonts(16)
            tw = f.getlength(label)
            ly = w.y - 21 if w.y >= 21 else w.y   # ラベルは枠の上に出す(上端に余裕が無ければ枠内)
            d.rectangle((w.x, ly, w.x + tw + 8, ly + 20), fill=(0, 0, 0, 200))
            d.text((w.x + 4, ly + 2), label, font=f, fill=col)
        img.alpha_composite(lay)


def preview_image(renderer: OverlayRenderer, ts: float, background: Image.Image | None = None,
                  bg_color: str = "#46505A") -> Image.Image:
    """プレビュー用に背景(動画フレームまたは単色)へ合成した RGB 画像を返す"""
    if background is not None:
        bg = background.convert("RGBA").resize((renderer.W, renderer.H))
    else:
        bg = Image.new("RGBA", (renderer.W, renderer.H), parse_color(bg_color) + (255,))
    bg.alpha_composite(renderer.render(ts))
    return bg.convert("RGB")
