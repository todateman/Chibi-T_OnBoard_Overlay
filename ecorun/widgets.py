"""ウィジェットの描画実装

各ウィジェットは config.py のモデル 1 つに対応し、自分の box(x, y, w, h)の内側だけに描く。
  static(img)      : 毎フレーム共通の部分(パネル、目盛り、区間全体のグラフ)。1 回だけ呼ばれる
  dynamic(img, ts) : ログ秒 ts の値に依存する部分。毎フレーム呼ばれる
文字サイズ・余白は基準サイズ(モデルの REF)での値を書き、self.S() / self.F() で拡縮する。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from .config import DataParams, Theme, WidgetBase
from .draw import Fonts, fmt_time, panel, parse_color, rgba, text
from .telemetry import Telemetry, interp_angle

SS = 2                          # ゲージ・コース図のスーパーサンプリング倍率(アンチエイリアス用)
GAUGE_BG = (10, 14, 20, 160)
CURSOR = (255, 255, 255, 140)


def resolve_theme(theme: Theme) -> SimpleNamespace:
    return SimpleNamespace(**{k: parse_color(v) for k, v in theme.model_dump().items() if k != "font"})


@dataclass
class Ctx:
    tel: Telemetry
    col: SimpleNamespace        # 配色(RGB / RGBA タプル)
    data: DataParams
    fonts: Fonts
    t_start: float              # 出力区間(区間全体を描くグラフの横軸)
    t_end: float

    def x_of(self, t, gx0, gx1):
        return gx0 + (gx1 - gx0) * (t - self.t_start) / max(1e-6, self.t_end - self.t_start)

    def on_bands(self, d, X, ta, tb, gy0, gy1):
        """グラフ領域 [ta, tb] にエンジンON帯を塗る"""
        for a, b in self.tel.on_spans:
            if b >= ta and a <= tb:
                d.rectangle((X(max(a, ta)), gy0, X(min(b, tb)), gy1), fill=rgba(self.col.accent, 70))


class WidgetImpl:
    def __init__(self, cfg: WidgetBase, ctx: Ctx):
        self.cfg, self.ctx, self.c = cfg, ctx, ctx.col
        self.x0, self.y0 = cfg.x, cfg.y
        self.x1, self.y1 = cfg.x + cfg.w, cfg.y + cfg.h
        ref = type(cfg).REF
        self.s = min(cfg.w / ref[0], cfg.h / ref[1]) if ref else 1.0

    @property
    def box(self):
        return (self.x0, self.y0, self.x1, self.y1)

    def S(self, v):
        """基準サイズでの長さ → 実際の長さ"""
        return v * self.s

    def F(self, size):
        return self.ctx.fonts(max(1, round(size * self.s)))

    def lw(self, v):
        """線幅"""
        return max(1, round(v * self.s))

    def T(self, d, xy, s, size, fill=None, anchor="la", stroke=2):
        text(d, xy, s, self.F(size), self.c.text if fill is None else fill, anchor, round(stroke * self.s))

    def panel(self, d, box=None, r=14, fill=None):
        panel(d, box or self.box, round(r * self.s), self.c.panel if fill is None else fill)

    def available(self) -> bool:
        """ログに必要な列が無いなどで描けないときは False"""
        return True

    def static(self, img: Image.Image):
        pass

    def dynamic(self, img: Image.Image, ts: float):
        pass


# ---------------------------------------------------------------------
class Timer(WidgetImpl):
    def static(self, img):
        self.panel(ImageDraw.Draw(img))

    def dynamic(self, img, ts):
        d, tel, c, S = ImageDraw.Draw(img), self.ctx.tel, self.c, self.S
        x0, y0 = self.x0, self.y0
        run_t = tel.interp("run_time", ts)
        lap = int(tel.step("lap", ts))
        self.T(d, (x0 + S(20), y0 + S(12)), "走行時間", 20, c.sub)
        self.T(d, (x0 + S(20), y0 + S(40)), fmt_time(run_t), 42)
        lap_lbl = f"LAP {lap}" + (f"/{self.cfg.lap_total}" if self.cfg.lap_total else "")
        self.T(d, (x0 + S(260), y0 + S(12)), lap_lbl, 20, c.sub)
        cur = [s for s in tel.lap_starts if s[1] <= ts]
        if cur and lap > 0:
            self.T(d, (x0 + S(260), y0 + S(40)), fmt_time(ts - cur[-1][1], ms=True), 42)
            if len(cur) >= 3:  # 前周タイム
                prev = cur[-1][1] - cur[-2][1]
                self.T(d, (self.x1 - S(15), y0 + S(12)), f"前周 {fmt_time(prev, ms=True)}", 18, c.sub, "ra")
        else:
            self.T(d, (x0 + S(260), y0 + S(40)), "--:--.---", 42, c.sub)


class SpeedGraph(WidgetImpl):
    def static(self, img):
        d, c, S, cfg = ImageDraw.Draw(img), self.c, self.S, self.cfg
        self.panel(d)
        self.T(d, (self.x0 + S(16), self.y0 + S(10)), cfg.title, 22, c.sub)
        gx0, gy0, gx1, gy1 = self.inner = (self.x0 + S(16), self.y0 + S(46), self.x1 - S(16), self.y1 - S(16))
        for v in range(0, cfg.max + 1, cfg.grid_step):
            y = gy1 - (gy1 - gy0) * v / cfg.max
            d.line((gx0, y, gx1, y), fill=c.grid, width=1)
            self.T(d, (gx1 - S(2), y - S(2)), str(v), 14, c.sub, "rb", 0)
        tel, ctx = self.ctx.tel, self.ctx
        m = (tel.t >= ctx.t_start) & (tel.t <= ctx.t_end)
        tt, sp = tel.t[m], tel.arr("speed")[m]
        if len(tt) < 2:
            return
        X = lambda t: ctx.x_of(t, gx0, gx1)
        Y = lambda v: gy1 - (gy1 - gy0) * min(v, cfg.max) / cfg.max
        if cfg.show_on_bands:
            ctx.on_bands(d, X, ctx.t_start, ctx.t_end, gy0, gy1)
        d.line([(X(a), Y(b)) for a, b in zip(tt, sp)], fill=rgba(c.speed, 255), width=self.lw(2))

    def dynamic(self, img, ts):
        ctx = self.ctx
        if ctx.t_end <= ctx.t_start:
            return
        d = ImageDraw.Draw(img)
        gx0, gy0, gx1, gy1 = self.inner
        spd = max(0.0, ctx.tel.interp("speed", ts))
        cx = gx0 + (gx1 - gx0) * (ts - ctx.t_start) / (ctx.t_end - ctx.t_start)
        cy = gy1 - (gy1 - gy0) * min(spd, self.cfg.max) / self.cfg.max
        d.line((cx, gy0, cx, gy1), fill=CURSOR, width=1)
        r = self.S(6)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=self.c.text)


ENV_ITEMS = {
    "eng_temp": ("エンジン", lambda v: f"{v:.1f}℃"),
    "air_temp": ("気温", lambda v: f"{v:.1f}℃"),
    "hum": ("湿度", lambda v: f"{v:.0f}%"),
    "press": ("気圧", lambda v: f"{v:.1f}kPa"),
}


class Env(WidgetImpl):
    def static(self, img):
        self.panel(ImageDraw.Draw(img), r=10)

    def dynamic(self, img, ts):
        items = self.cfg.items
        if not items:
            return
        d, tel, S = ImageDraw.Draw(img), self.ctx.tel, self.S
        cw = (self.x1 - self.x0) / len(items)
        ym = (self.y0 + self.y1) / 2
        for k, key in enumerate(items):
            label, fmt = ENV_ITEMS[key]
            x = self.x0 + S(14) + k * cw
            self.T(d, (x, ym), label, 17, self.c.sub, "lm", 1)
            self.T(d, (x + cw - S(22), ym), fmt(tel.interp(key, ts)), 21, None, "rm", 1)


class FuelEco(WidgetImpl):
    def static(self, img):
        self.panel(ImageDraw.Draw(img))

    def dynamic(self, img, ts):
        d, tel, c, S = ImageDraw.Draw(img), self.ctx.tel, self.c, self.S
        x0, y0, x1 = self.x0, self.y0, self.x1
        dist = tel.interp("dist", ts)
        eco = tel.step("eco", ts)
        self.T(d, (x0 + S(20), y0 + S(15)), "燃費", 22, c.sub)
        eco_s = f"{eco:,.1f}" if dist >= self.ctx.data.fuel_eco_min_dist_m and eco > 0 else "---"
        self.T(d, (x1 - S(20), y0 + S(25)), eco_s, 64, c.accent, "ra")
        self.T(d, (x1 - S(20), y0 + S(100)), "km/L", 20, c.sub, "ra")
        if self.cfg.show_odometer:
            self.T(d, (x0 + S(20), y0 + S(100)), f"距離 {dist / 1000:.2f} km", 22)
            self.T(d, (x0 + S(20), y0 + S(130)), f"燃料 {tel.interp('fuel', ts):.1f} ml", 22)


class Odometer(WidgetImpl):
    def static(self, img):
        if self.cfg.panel:
            self.panel(ImageDraw.Draw(img))

    def dynamic(self, img, ts):
        d, tel, S = ImageDraw.Draw(img), self.ctx.tel, self.S
        st = self.cfg.stroke
        x = self.x0 + (S(16) if self.cfg.panel else round(st * self.s))  # 縁取りが枠外に出ないように
        self.T(d, (x, self.y0 + S(6)), f"距離 {tel.interp('dist', ts) / 1000:.2f} km", 21, stroke=st)
        self.T(d, (x, self.y0 + S(34)), f"燃料 {tel.interp('fuel', ts):.1f} ml", 21, stroke=st)


class Mask(WidgetImpl):
    def static(self, img):
        panel(ImageDraw.Draw(img), self.box, self.cfg.radius, parse_color(self.cfg.color))


class Map(WidgetImpl):
    def static(self, img):
        d = ImageDraw.Draw(img)
        self.panel(d)
        img.alpha_composite(self._track_layer(), (self.x0, self.y0))

    def _proj(self):
        tel = self.ctx.tel
        m = tel.has_fix
        lat = tel.df["lat"].to_numpy()[m]
        lon = tel.df["lon"].to_numpy()[m]
        lat0 = lat.mean()
        x = (lon - lon.mean()) * math.cos(math.radians(lat0))
        y = lat - lat0
        w, h = self.x1 - self.x0, self.y1 - self.y0
        bw, bh = w - self.S(60), h - self.S(60)
        span = max(x.max() - x.min(), y.max() - y.min()) or 1e-9
        sc = min(bw, bh) / span
        cx, cy = (x.max() + x.min()) / 2, (y.max() + y.min()) / 2
        lon_m, coslat = lon.mean(), math.cos(math.radians(lat0))

        def f(la, lo):
            xx = (lo - lon_m) * coslat
            yy = la - lat0
            return ((xx - cx) * sc + w / 2, -(yy - cy) * sc + h / 2)
        return f

    def _track_layer(self) -> Image.Image:
        w, h = self.x1 - self.x0, self.y1 - self.y0
        tel, c, cfg = self.ctx.tel, self.c, self.cfg
        img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        self.proj = None
        self._lap_layers = {}
        if tel.has_fix.sum() >= 2:
            self.proj = self._proj()
            m = tel.has_fix
            px, py = self.proj(tel.df["lat"].to_numpy()[m], tel.df["lon"].to_numpy()[m])
            self.pts = list(zip((px * SS).tolist(), (py * SS).tolist()))
            self.on = (tel.df["rpm"].to_numpy()[m] > self.ctx.data.rpm_on_threshold).tolist()
            self.laps = tel.df["lap"].to_numpy()[m]
            d.line(self.pts, fill=(255, 255, 255, 220), width=round(5 * self.s) * SS, joint="curve")
        img = img.resize((w, h), Image.LANCZOS)
        if self.proj is not None and cfg.show_on_segments and cfg.on_segments_scope == "all":
            img.alpha_composite(self._on_layer(np.ones(len(self.pts), dtype=bool)))
        dd = ImageDraw.Draw(img)
        S = self.S
        self.T(dd, (S(16), S(10)), cfg.title, 20, c.sub)
        if self.proj is None:
            self.T(dd, (w / 2, h / 2), "GPS 未測位", 20, c.sub, "mm", 0)
        if cfg.show_legend and cfg.show_on_segments:
            dd.line((S(16), h - S(22), S(36), h - S(22)), fill=c.accent, width=self.lw(6))
            self.T(dd, (S(44), h - S(22)), "エンジンON", 18, c.sub, "lm")
        return img

    def _on_layer(self, sel: np.ndarray) -> Image.Image:
        """sel の点のうちエンジンON区間をオレンジで描いた透明レイヤ(どこで加速したかが一目で分かる)。
        10 Hz では 1 区間が短いので、連続した ON 区間をまとめて 1 本の線にする"""
        w, h = self.x1 - self.x0, self.y1 - self.y0
        img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        pts, on = self.pts, self.on
        on_sel = [o and bool(s) for o, s in zip(on, sel)]
        seg = []
        for i in range(1, len(pts)):
            if on_sel[i]:
                seg = seg or [pts[i - 1]]   # 周の境界でも直前の点からつなぐ
                seg.append(pts[i])
            if seg and (not on_sel[i] or i == len(pts) - 1):
                d.line(seg, fill=rgba(self.c.accent, 180), width=round(9 * self.s) * SS, joint="curve")
                seg = []
        return img.resize((w, h), Image.LANCZOS)

    def dynamic(self, img, ts):
        tel, cfg = self.ctx.tel, self.cfg
        if self.proj is None:
            return
        if cfg.show_on_segments and cfg.on_segments_scope == "lap":
            # 走行中の周の ON 区間だけを描く(周ごとに 1 回だけ生成してキャッシュ)
            lap = int(tel.step("lap", ts))
            if lap not in self._lap_layers:
                self._lap_layers[lap] = self._on_layer(self.laps == lap)
            img.alpha_composite(self._lap_layers[lap], (self.x0, self.y0))
        i = int(np.clip(np.searchsorted(tel.t, ts), 0, len(tel.t) - 1))
        if not tel.has_fix[i]:
            return
        px, py = self.proj(tel.interp("lat", ts), tel.interp("lon", ts))
        px += self.x0
        py += self.y0
        r = self.S(11)
        ImageDraw.Draw(img).ellipse((px - r, py - r, px + r, py + r), fill=self.c.marker,
                                    outline=self.c.text, width=self.lw(3))


class Gauge(WidgetImpl):
    """円形ゲージの共通部分。円は box に内接し、基準半径 180 px で拡縮する"""
    unit = ""
    color_key = "speed"

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.r = min(cfg.w, cfg.h) // 2
        self.cx, self.cy = cfg.x + cfg.w // 2, cfg.y + cfg.h // 2
        self.s = self.r / 180

    def u(self, v):
        """基準サイズでの長さ → スーパーサンプリング後のピクセル数"""
        return max(1, round(v * self.s)) * SS

    def static(self, img):
        r, cfg, u = self.r, self.cfg, self.u
        R = r * SS
        g = Image.new("RGBA", (2 * R, 2 * R), (0, 0, 0, 0))
        d = ImageDraw.Draw(g)
        d.ellipse((0, 0, 2 * R, 2 * R), fill=GAUGE_BG)
        d.arc((u(22), u(22), 2 * R - u(22), 2 * R - u(22)), 135, 405, fill=(255, 255, 255, 50), width=u(18))
        mx, step = cfg.max, cfg.step
        n_minor = max(1, int(mx / step * 5))
        font = self.ctx.fonts(u(22))
        for k in range(n_minor + 1):
            a = math.radians(135 + 270 * k / n_minor)
            major = k % 5 == 0
            r0, r1 = R - u(52 if major else 46), R - u(40)
            d.line((R + r0 * math.cos(a), R + r0 * math.sin(a), R + r1 * math.cos(a), R + r1 * math.sin(a)),
                   fill=(255, 255, 255, 230 if major else 120), width=u(4 if major else 2))
            if major:
                v = int(step * k / 5)
                lab = str(v // 1000) if mx >= 1000 else str(v)
                rr = R - u(78)
                text(d, (R + rr * math.cos(a), R + rr * math.sin(a)), lab, font, self.c.sub, "mm", 0)
        unit = f"x1000 {self.unit}" if mx >= 1000 else self.unit
        text(d, (R, R + u(95)), unit, font, self.c.sub, "mm", 0)
        img.alpha_composite(g.resize((2 * r, 2 * r), Image.LANCZOS), (self.cx - r, self.cy - r))

    def needle(self, img, val):
        r, u = self.r, self.u
        R = r * SS
        lay = Image.new("RGBA", (2 * R, 2 * R), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        frac = max(0.0, min(1.0, val / self.cfg.max))
        if frac > 0.002:
            d.arc((u(22), u(22), 2 * R - u(22), 2 * R - u(22)), 135, 135 + 270 * frac,
                  fill=rgba(getattr(self.c, self.color_key), 255), width=u(18))
        a = math.radians(135 + 270 * frac)
        # 目盛り数字と大きな数値を隠さない外周だけの短い針
        d.line((R + (R - u(64)) * math.cos(a), R + (R - u(64)) * math.sin(a),
                R + (R - u(14)) * math.cos(a), R + (R - u(14)) * math.sin(a)),
               fill=(255, 255, 255, 255), width=u(6))
        img.alpha_composite(lay.resize((2 * r, 2 * r), Image.LANCZOS), (self.cx - r, self.cy - r))


class SpeedGauge(Gauge):
    unit = "km/h"
    color_key = "speed"

    def dynamic(self, img, ts):
        spd = max(0.0, self.ctx.tel.interp("speed", ts))
        self.needle(img, spd)
        self.T(ImageDraw.Draw(img), (self.cx, self.cy + self.S(10)), f"{spd:.0f}", 96, None, "mm", 3)


class RpmGauge(Gauge):
    unit = "rpm"
    color_key = "accent"

    def dynamic(self, img, ts):
        tel, c, S = self.ctx.tel, self.c, self.S
        rpm_raw = tel.step("rpm", ts)          # ON/OFF は補間しない
        rpm = tel.interp("rpm", ts) if rpm_raw > 0 else 0.0
        eng_on = rpm_raw > self.ctx.data.rpm_on_threshold
        self.needle(img, rpm)
        d = ImageDraw.Draw(img)
        cx, cy = self.cx, self.cy
        self.T(d, (cx, cy + S(10)), f"{rpm:,.0f}" if eng_on else "0", 72, None, "mm", 3)
        if not self.cfg.show_badge:
            return
        badge = (cx - S(70), cy + S(120), cx + S(70), cy + S(160))
        if eng_on:
            d.rounded_rectangle(badge, round(S(10)), fill=rgba(c.accent, 235))
            self.T(d, (cx, cy + S(140)), "ENGINE ON", 22, (20, 20, 20), "mm", 0)
        else:
            d.rounded_rectangle(badge, round(S(10)), outline=(255, 255, 255, 150), width=self.lw(2))
            self.T(d, (cx, cy + S(140)), "COAST", 22, c.sub, "mm", 0)


class ChartBase(WidgetImpl):
    def frame(self, d):
        """小グラフのパネルとタイトルを描き、プロット領域を返す"""
        S = self.S
        self.panel(d)
        self.T(d, (self.x0 + S(16), self.y0 + S(10)), self.cfg.title, 22, self.c.sub)
        return (self.x0 + S(16), self.y0 + S(50), self.x1 - S(16), self.y1 - S(16))


class AltGraph(ChartBase):
    def static(self, img):
        """標高: 出力区間全体のプロファイル(GPS 測位中のみ)"""
        d, c, ctx, S = ImageDraw.Draw(img), self.c, self.ctx, self.S
        gx0, gy0, gx1, gy1 = self.inner = self.frame(d)
        tel = ctx.tel
        m = (tel.t >= ctx.t_start) & (tel.t <= ctx.t_end) & tel.has_fix
        self.alt_range = None
        if m.sum() < 2:
            self.T(d, ((gx0 + gx1) / 2, (gy0 + gy1) / 2), "GPS 未測位", 20, c.sub, "mm", 0)
            return
        alt = tel.arr("alt")
        lo, hi = alt[m].min(), alt[m].max()
        mid, half = (lo + hi) / 2, max(hi - lo, self.cfg.min_span_m) / 2 * 1.15
        lo, hi = self.alt_range = (mid - half, mid + half)
        # 目盛り: 1-2-5 系列で 2〜4 本
        step = next((s for s in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000) if (hi - lo) / s <= 3.5), 1000)
        Y = lambda v: gy1 - (gy1 - gy0) * (v - lo) / (hi - lo)
        X = lambda t: ctx.x_of(t, gx0, gx1)
        # 測位が途切れた区間で線を切る
        idx = np.flatnonzero(m)
        for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
            if len(run) < 2:
                continue
            pts = [(X(tel.t[i]), Y(alt[i])) for i in run]
            d.polygon(pts + [(pts[-1][0], gy1), (pts[0][0], gy1)], fill=rgba(c.alt, 60))
            d.line(pts, fill=rgba(c.alt, 255), width=self.lw(2))
        for v in np.arange(math.ceil(lo / step) * step, hi, step):
            d.line((gx0, Y(v), gx1, Y(v)), fill=c.grid, width=1)
            self.T(d, (gx0 + S(2), Y(v) - S(2)), f"{v:g}", 14, c.sub, "lb", 0)

    def dynamic(self, img, ts):
        d, ctx, S = ImageDraw.Draw(img), self.ctx, self.S
        tel = ctx.tel
        i = int(np.clip(np.searchsorted(tel.t, ts), 0, len(tel.t) - 1))
        if self.alt_range is None or not tel.has_fix[i]:
            self.T(d, (self.x1 - S(16), self.y0 + S(8)), "--- m", 24, self.c.sub, "ra")
            return
        gx0, gy0, gx1, gy1 = self.inner
        lo, hi = self.alt_range
        alt = tel.interp("alt", ts)
        self.T(d, (self.x1 - S(16), self.y0 + S(8)), f"{alt:.1f} m", 24, None, "ra")
        if ctx.t_end > ctx.t_start:
            cx = gx0 + (gx1 - gx0) * (ts - ctx.t_start) / (ctx.t_end - ctx.t_start)
            cy = gy1 - (gy1 - gy0) * min(max((alt - lo) / (hi - lo), 0), 1)
            d.line((cx, gy0, cx, gy1), fill=CURSOR, width=1)
            r = S(5)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=self.c.text)


class FuelPressureGraph(ChartBase):
    def available(self):
        return self.ctx.tel.has("fuel_p")

    def static(self, img):
        d, c, cfg, S = ImageDraw.Draw(img), self.c, self.cfg, self.S
        gx0, gy0, gx1, gy1 = self.inner = self.frame(d)
        sub_x = self.x0 + S(16) + self.F(22).getlength(cfg.title) + S(10)
        self.T(d, (sub_x, self.y0 + S(34)), f"直近{cfg.window_s:g}秒", 15, c.sub, "ls", 0)
        n = int(round(cfg.max / cfg.grid_step))
        for k in range(n + 1):
            v = cfg.grid_step * k
            y = gy1 - (gy1 - gy0) * v / cfg.max
            d.line((gx0, y, gx1, y), fill=c.grid, width=1)
            if k % 2 == 0:
                self.T(d, (gx0 + S(2), y - S(2)), f"{v:.1f}", 14, c.sub, "lb", 0)

    def dynamic(self, img, ts):
        """燃圧: 右端を現在として直近 window_s 秒をスクロール表示"""
        d, tel, cfg, S = ImageDraw.Draw(img), self.ctx.tel, self.cfg, self.S
        gx0, gy0, gx1, gy1 = self.inner
        win, mx = cfg.window_s, cfg.max
        ta = ts - win
        X = lambda t: gx0 + (gx1 - gx0) * (t - ta) / win
        Y = lambda v: gy1 - (gy1 - gy0) * min(max(v, 0.0), mx) / mx
        self.ctx.on_bands(d, X, ta, ts, gy0, gy1)
        cur = tel.interp("fuel_p", ts)
        i0, i1 = np.searchsorted(tel.t, [ta, ts])
        tt = np.r_[tel.t[i0:i1], ts]
        vv = np.r_[tel.arr("fuel_p")[i0:i1], cur]
        if len(tt) >= 2:
            d.line(list(zip(X(tt), map(Y, vv))), fill=rgba(self.c.fuel_p, 255), width=self.lw(2))
        r = S(5)
        d.ellipse((gx1 - r, Y(cur) - r, gx1 + r, Y(cur) + r), fill=self.c.text)
        self.T(d, (self.x1 - S(16), self.y0 + S(8)), f"{cur:.3f} MPa", 24, None, "ra")


HEADING_NAMES = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}


class Heading(WidgetImpl):
    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.tape = tuple(round(v) for v in (self.x0, self.y0, self.x1, self.y0 + self.S(70)))

    def static(self, img):
        self.panel(ImageDraw.Draw(img), self.tape, r=12)

    def dynamic(self, img, ts):
        tel, c, S = self.ctx.tel, self.c, self.S
        hd = interp_angle(tel.t, tel.heading, ts)
        # テープはパネルの切り抜きに描いて戻す(端の目盛り・文字をパネル内でクリップする)
        tx0, ty0, tx1, ty1 = self.tape
        sub = img.crop(self.tape)
        d = ImageDraw.Draw(sub)
        cx, half, span = (tx1 - tx0) / 2, (tx1 - tx0) / 2 - S(20), self.cfg.span_deg
        start = int(math.floor((hd - span) / 5) * 5)
        for a in range(start, int(hd + span) + 1, 5):
            x = cx + (a - hd) / span * half
            aa = a % 360
            if aa % 15 == 0:
                d.line((x, S(50), x, S(66)), fill=(255, 255, 255, 200), width=self.lw(2))
                lab = HEADING_NAMES.get(aa, str(aa))
                self.T(d, (x, S(30)), lab, 24 if aa in HEADING_NAMES else 18,
                       c.accent if aa in HEADING_NAMES else c.sub, "mm", 0)
            else:
                d.line((x, S(58), x, S(66)), fill=(255, 255, 255, 110), width=1)
        img.paste(sub, (tx0, ty0))
        d = ImageDraw.Draw(img)
        mx = (self.x0 + self.x1) / 2
        d.polygon([(mx - S(9), self.y0 + S(70)), (mx + S(9), self.y0 + S(70)), (mx, self.y0 + S(56))],
                  fill=c.marker)
        self.T(d, (mx, self.y0 + S(92)), f"{hd:03.0f}°", 24, None, "mm")


class Text(WidgetImpl):
    def static(self, img):
        d, cfg = ImageDraw.Draw(img), self.cfg
        if cfg.panel:
            self.panel(d)
        pad = (16 if cfg.panel else 0) + cfg.stroke
        ym = (self.y0 + self.y1) / 2
        xy, anchor = {"left": ((self.x0 + pad, ym), "lm"),
                      "center": (((self.x0 + self.x1) / 2, ym), "mm"),
                      "right": ((self.x1 - pad, ym), "rm")}[cfg.align]
        # box に収まらなければ文字を小さくする
        size = cfg.size
        while size > 6:
            l, t, r, b = d.textbbox(xy, cfg.text, font=self.ctx.fonts(size), anchor=anchor, stroke_width=cfg.stroke)
            if r - l <= self.x1 - self.x0 - 2 * pad and b - t <= self.y1 - self.y0:
                break
            size -= 1
        text(d, xy, cfg.text, self.ctx.fonts(size), parse_color(cfg.color), anchor, cfg.stroke)


IMPLS: dict[str, type[WidgetImpl]] = {
    "timer": Timer, "speed_graph": SpeedGraph, "env": Env, "fuel_eco": FuelEco, "odometer": Odometer,
    "mask": Mask, "map": Map, "speed_gauge": SpeedGauge, "rpm_gauge": RpmGauge, "alt_graph": AltGraph,
    "fuel_p_graph": FuelPressureGraph, "heading": Heading, "text": Text,
}
