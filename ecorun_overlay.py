#!/usr/bin/env python3
"""
ecorun_overlay.py — エコラン用テレメトリ・オーバーレイ生成スクリプト

対象: Chibi-T ロガーの CSV
  - 現行フォーマット(10 Hz、LOG0650 以降): 記録日時, 速度(km/h), ラップ数(周目), 走行時間(秒),
    回転数(rpm), 走行距離積算(m), 積算燃料消費量(ml), 燃費(km/l), lat(緯度), lng(経度), 標高(m),
    温度(C), 気圧(kPa), 気温(C), 湿度(%), 燃圧(MPa) ほか
  - 旧フォーマット(1 Hz、LOG0612 など): 記録日時, 速度(km/h), ラップ数, 走行時間, 回転数, 走行距離,
    積算燃料, 燃費, lat, lon, alt, loc, 温度, 気圧(kPa), 気温(C), 湿度(%)

使い方:
  # 1) 静止画プレビュー(ログ開始から 150 秒の時点)
  python ecorun_overlay.py LOG0650.CSV --preview 150 -o preview.png

  # 2) 車載動画に直接合成(動画の先頭 = ログ時刻 15:10:30)
  python ecorun_overlay.py LOG0650.CSV --video onboard.mp4 --sync 15:10:30 --mask-osd -o out.mp4

  # 3) 透過オーバーレイだけを書き出し(DaVinci Resolve / Premiere で重ねる)
  python ecorun_overlay.py LOG0650.CSV --sync 15:10:30 --duration 1200 -o overlay.mov

同期の合わせ方:
  --sync  : 動画の 0 秒目に対応するログ時刻 (HH:MM:SS[.f])
  --offset: 上記からの微調整(秒、正で表示が先に進む)
  発進の瞬間など「速度が 0 から立ち上がる点」を動画とログで見比べると合わせやすい。

必要なもの: Python 3.9+, pandas, numpy, Pillow, ffmpeg(PATH 上)
  pip install pandas numpy pillow
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

# =====================================================================
#  テンプレート設定(ここを書き換えて見た目を調整)
# =====================================================================
W, H = 1920, 1080              # レイアウト基準解像度(動画サイズへは自動スケール)
FPS_DEFAULT = 30

ACCENT = (255, 140, 0)          # エンジンON・強調色(オレンジ)
SPEED_COL = (0, 200, 255)       # 速度系の色(シアン)
TEXT = (255, 255, 255)
SUB = (190, 196, 205)
ALT_COL = (140, 220, 110)       # 標高グラフの色(グリーン)
FUEL_P_COL = (235, 110, 200)    # 燃圧グラフの色(マゼンタ)
PANEL = (10, 14, 20, 170)       # パネル背景(RGBA)
GRID = (255, 255, 255, 40)

SPEED_MAX = 60                  # 速度ゲージ上限 km/h
RPM_MAX = 6000                  # 回転ゲージ上限 rpm
RPM_ON_THRESHOLD = 300          # これを超えたら「エンジンON」とみなす
FUEL_ECO_MIN_DIST_M = 300       # これより短い距離では燃費を "---" 表示
LAP_TOTAL = None                # 例: 3 にすると "LAP 2/3" 表示。None なら "LAP 2"
HEADING_LOOKBACK_S = 3.0        # 方位はこの秒数だけ前の位置からのベクトルで求める
ALT_MIN_SPAN_M = 5.0            # 標高グラフの縦軸の最小幅(GPS 標高の揺れを山に見せない)
FUEL_P_MAX = 0.4                # 燃圧グラフ上限 MPa
FUEL_P_WINDOW_S = 60            # 燃圧グラフに表示する直近の秒数
# ドラレコの焼き込み表示(日時・速度)を隠す帯。--mask-osd で有効。距離・燃料をここに表示
# VSYSTO F9 の 1920x1080 基準。機種や解像度が違えば --preview で見ながら調整
OSD_MASK_BOX = (1128, 992, 1760, 1052)

# CSV 列名 → 内部キー(ロガーの仕様が変わったらここだけ直す)
# 候補を先頭から探し、最初に見つかった列を使う(現行フォーマット, 旧フォーマット の順)
# ここに無い列(燃料噴射時間・点火進角・噴射終了角度・場所・1次/2次空気圧など)は読み捨てる
COL = dict(
    time=("記録日時",),
    speed=("速度(km/h)",),
    lap=("ラップ数(周目)", "ラップ数"),
    run_time=("走行時間(秒)", "走行時間"),
    rpm=("回転数(rpm)", "回転数"),
    dist=("走行距離積算(m)", "走行距離"),
    fuel=("積算燃料消費量(ml)", "積算燃料"),
    eco=("燃費(km/l)", "燃費"),
    lat=("lat(緯度)", "lat"),
    lon=("lng(経度)", "lon"),
    alt=("標高(m)", "alt"),
    eng_temp=("温度(C)", "温度"),
    press=("気圧(kPa)",),
    air_temp=("気温(C)",),
    hum=("湿度(%)",),
    fuel_p=("燃圧(MPa)",),
)
OPTIONAL_COLS = {"fuel_p"}      # 無ければその表示を省く(旧フォーマットには燃圧が無い)
# 10 Hz ログでは各センサの更新が 2〜5 Hz で、行の間は同じ値が続く。
# これらの列は値が変わった行を節点に補間し直して、ゲージやグラフの階段状の動きを消す
# (回転数は ON/OFF 判定に生の値を使い、ラップ数・燃費は step 表示なので対象外)
SMOOTH_COLS = ("speed", "dist", "fuel", "lat", "lon", "alt", "eng_temp",
               "press", "air_temp", "hum", "fuel_p")

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


# =====================================================================
#  データ読み込みと前処理
# =====================================================================
@dataclass
class Telemetry:
    t: np.ndarray            # ログ開始からの秒
    t0: pd.Timestamp         # ログ開始時刻
    df: pd.DataFrame
    lap_starts: list         # [(lap_no, t_start)]
    heading: np.ndarray      # 方位 deg(GPS から算出)
    has_fix: np.ndarray      # GPS 有効フラグ
    on_spans: list           # エンジンON区間 [(t_start, t_end)]

    def __post_init__(self):
        self._arr = {}

    def has(self, key: str) -> bool:
        return key in self.df.columns

    def arr(self, key: str) -> np.ndarray:
        if key not in self._arr:
            self._arr[key] = self.df[key].to_numpy(dtype=float)
        return self._arr[key]

    def interp(self, key: str, ts: float) -> float:
        return float(np.interp(ts, self.t, self.arr(key)))

    def step(self, key: str, ts: float):
        i = int(np.clip(np.searchsorted(self.t, ts, side="right") - 1, 0, len(self.t) - 1))
        return self.df[key].iloc[i]


def smooth_held(t: np.ndarray, v: np.ndarray, max_hold: float = 1.0) -> np.ndarray:
    """同じ値が続く行を間引き、値が変わった行だけを節点にして補間し直す。
    max_hold 秒以上同じ値が続く区間(停車中など)は終端も節点にして平坦なまま残す。
    1 Hz ログではほぼ元の値のまま。"""
    if len(v) < 2:
        return v
    start = np.flatnonzero(np.r_[True, v[1:] != v[:-1]])
    end = np.r_[start[1:] - 1, len(v) - 1]
    knots = np.union1d(start, end[(t[end] - t[start]) >= max_hold])
    knots = np.union1d(knots, [len(v) - 1])
    return np.interp(t, t[knots], v[knots])


def load_log(path: str) -> Telemetry:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    rename, missing = {}, []
    for key, names in COL.items():
        name = next((n for n in names if n in df.columns), None)
        if name:
            rename[name] = key
        elif key not in OPTIONAL_COLS:
            missing.append(" / ".join(names))
    if missing:
        sys.exit(f"CSV に必要な列がありません: {', '.join(missing)}")
    df = df[list(rename)].rename(columns=rename)

    ts = pd.to_datetime(df["time"], format="%Y/%m/%d %H:%M:%S.%f", errors="coerce")
    df = df[ts.notna()].copy()
    ts = ts[ts.notna()]
    t0 = ts.iloc[0]
    t = (ts - t0).dt.total_seconds().to_numpy()
    # 重複時刻を除去(np.interp は単調増加が必要)
    keep = np.concatenate([[True], np.diff(t) > 0])
    df, t = df[keep].reset_index(drop=True), t[keep]

    has_fix = ((df["lat"].abs() > 1e-6) & (df["lon"].abs() > 1e-6)).to_numpy()
    # GPS 未測位(0,0)は前方の有効値で埋めておく(描画側で非表示判定)
    for c in ("lat", "lon"):
        df[c] = df[c].where(has_fix).bfill().ffill()

    for c in SMOOTH_COLS:
        if c in df.columns:
            df[c] = smooth_held(t, df[c].to_numpy(dtype=float))

    # 方位: HEADING_LOOKBACK_S 秒前の位置からのベクトル。低速時は直前の値を保持
    lat = np.radians(df["lat"].to_numpy())
    lon = np.radians(df["lon"].to_numpy())
    idx = np.arange(len(df))
    j = np.clip(np.searchsorted(t, t - HEADING_LOOKBACK_S, side="right") - 1, 0, None)
    ok = (j < idx) & has_fix & has_fix[j] & (df["speed"].to_numpy() >= 3)
    dlon = lon - lon[j]
    y = np.sin(dlon) * np.cos(lat)
    x = np.cos(lat[j]) * np.sin(lat) - np.sin(lat[j]) * np.cos(lat) * np.cos(dlon)
    hd = np.where(ok, (np.degrees(np.arctan2(y, x)) + 360) % 360, np.nan)
    hd = pd.Series(hd).ffill().bfill().fillna(0).to_numpy()

    laps = df["lap"].to_numpy()
    lap_starts = [(int(laps[0]), t[0])]
    for i in range(1, len(laps)):
        if laps[i] != laps[i - 1]:
            lap_starts.append((int(laps[i]), t[i]))

    # エンジンON区間(ON の行 i について t[i-1]〜t[i] を ON とみなす)
    on = np.r_[0, (df["rpm"].to_numpy() > RPM_ON_THRESHOLD).astype(int), 0]
    edges = np.diff(on)
    on_spans = [(t[max(s - 1, 0)], t[e - 1])
                for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))]
    return Telemetry(t, t0, df, lap_starts, hd, has_fix, on_spans)


def interp_angle(t: np.ndarray, a: np.ndarray, ts: float) -> float:
    s = np.interp(ts, t, np.sin(np.radians(a)))
    c = np.interp(ts, t, np.cos(np.radians(a)))
    return (math.degrees(math.atan2(s, c)) + 360) % 360


# =====================================================================
#  描画ユーティリティ
# =====================================================================
class Fonts:
    def __init__(self, path: str | None):
        self.path = path or next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
        if not self.path:
            print("警告: 日本語フォントが見つかりません。--font で指定してください。", file=sys.stderr)
        self._cache = {}

    def __call__(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self._cache:
            self._cache[size] = (ImageFont.truetype(self.path, size) if self.path
                                 else ImageFont.load_default())
        return self._cache[size]


def panel(d: ImageDraw.ImageDraw, box, r=14, fill=PANEL):
    d.rounded_rectangle(box, r, fill=fill)


def text(d, xy, s, font, fill=TEXT, anchor="la", stroke=2):
    d.text(xy, s, font=font, fill=fill, anchor=anchor,
           stroke_width=stroke, stroke_fill=(0, 0, 0, 160))


def fmt_time(sec: float, ms=False) -> str:
    sec = max(0.0, sec)
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    if ms:
        return f"{int(m) + h * 60}:{s:06.3f}" if h else f"{int(m)}:{s:06.3f}"
    return f"{h}:{m:02d}:{int(s):02d}"


# =====================================================================
#  オーバーレイ描画クラス
# =====================================================================
class OverlayRenderer:
    SS = 2  # ゲージのスーパーサンプリング倍率(アンチエイリアス用)

    def __init__(self, tel: Telemetry, t_start: float, t_end: float, fonts: Fonts,
                 mask_osd: bool = False):
        self.tel, self.f = tel, fonts
        self.mask_osd = mask_osd
        self.t_start, self.t_end = t_start, t_end
        # レイアウト
        # 地平線付近 (y≈380〜640) はコース前方が映るので空けておく
        self.map_box = (1540, 30, 1890, 370)
        self.graph_box = (30, 150, 560, 360)
        self.env_box = (W // 2 - 340, 146, W // 2 + 340, 190)
        self.fuel_box = (780, 900, 1120, 1060)
        self.g_speed = (560, 880, 180)   # cx, cy, r
        self.g_rpm = (1340, 880, 180)
        # 下段の左右の隅: 標高(コース全体)と燃圧(直近 FUEL_P_WINDOW_S 秒)
        # 右側はドラレコ焼き込み表示(y≥1000)にかからない高さにする
        self.alt_box = (30, 770, 360, 980)
        self.fuel_p_box = (1560, 770, 1890, 980)
        self.show_fuel_p = tel.has("fuel_p")
        self.base = self._build_static()

    # ---------- 静的レイヤ(毎フレーム使い回す) ----------
    def _build_static(self) -> Image.Image:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        f = self.f
        # 左上: 時間・ラップ
        panel(d, (30, 30, 560, 130))
        # 速度グラフ
        panel(d, self.graph_box)
        text(d, (self.graph_box[0] + 16, self.graph_box[1] + 10), "スピード / エンジンON", f(22), SUB)
        # 環境(方位テープの下に 1 行)
        panel(d, self.env_box, r=10)
        # 燃費パネル
        panel(d, self.fuel_box)
        # ドラレコ焼き込み表示のマスク(ゲージより下に敷く)
        if self.mask_osd:
            panel(d, OSD_MASK_BOX, r=12, fill=(10, 14, 20, 255))
        # マップ
        panel(d, self.map_box)
        img.alpha_composite(self._track_layer(), (self.map_box[0], self.map_box[1]))
        # ゲージ背景
        for (cx, cy, r), mx, step, lab in ((self.g_speed, SPEED_MAX, 10, "km/h"),
                                           (self.g_rpm, RPM_MAX, 1000, "rpm")):
            g = self._gauge_bg(r, mx, step, lab)
            img.alpha_composite(g, (cx - r, cy - r))
        self._graph_static(img)
        self._alt_static(img)
        if self.show_fuel_p:
            self._fuel_p_static(img)
        return img

    def _proj(self):
        tel = self.tel
        m = tel.has_fix
        lat = tel.df["lat"].to_numpy()[m]
        lon = tel.df["lon"].to_numpy()[m]
        lat0 = lat.mean()
        x = (lon - lon.mean()) * math.cos(math.radians(lat0))
        y = lat - lat0
        x0, y0, x1, y1 = self.map_box
        bw, bh = (x1 - x0) - 60, (y1 - y0) - 60
        span = max(x.max() - x.min(), y.max() - y.min()) or 1e-9
        sc = min(bw, bh) / span
        cx, cy = (x.max() + x.min()) / 2, (y.max() + y.min()) / 2
        lon_m, lat_m = lon.mean(), lat0

        def f(la, lo):
            xx = (lo - lon_m) * math.cos(math.radians(lat0))
            yy = la - lat_m
            return ((xx - cx) * sc + (x1 - x0) / 2, -(yy - cy) * sc + (y1 - y0) / 2)
        return f

    def _track_layer(self) -> Image.Image:
        x0, y0, x1, y1 = self.map_box
        s = self.SS
        img = Image.new("RGBA", ((x1 - x0) * s, (y1 - y0) * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        self.proj = self._proj()
        tel = self.tel
        df = tel.df
        pts, on = [], []
        for i in range(len(df)):
            if not tel.has_fix[i]:
                continue
            px, py = self.proj(df["lat"].iat[i], df["lon"].iat[i])
            pts.append((px * s, py * s))
            on.append(df["rpm"].iat[i] > RPM_ON_THRESHOLD)
        if len(pts) > 1:
            d.line(pts, fill=(255, 255, 255, 220), width=5 * s, joint="curve")
            # エンジンON区間をオレンジで上書き(どこで加速したかが一目で分かる)
            # 10 Hz では 1 区間が短いので、連続した ON 区間をまとめて 1 本の線にする
            seg = []
            for i in range(1, len(pts)):
                if on[i]:
                    seg = seg or [pts[i - 1]]
                    seg.append(pts[i])
                if seg and (not on[i] or i == len(pts) - 1):
                    d.line(seg, fill=ACCENT + (180,), width=9 * s, joint="curve")
                    seg = []
        img = img.resize((x1 - x0, y1 - y0), Image.LANCZOS)
        dd = ImageDraw.Draw(img)
        text(dd, (16, 10), "COURSE", self.f(20), SUB)
        dd.line((16, (y1 - y0) - 22, 36, (y1 - y0) - 22), fill=ACCENT, width=6)
        text(dd, (44, (y1 - y0) - 22), "エンジンON", self.f(18), SUB, anchor="lm")
        return img

    def _gauge_bg(self, r, mx, step, label) -> Image.Image:
        s = self.SS
        R = r * s
        img = Image.new("RGBA", (2 * R, 2 * R), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((0, 0, 2 * R, 2 * R), fill=(10, 14, 20, 160))
        d.arc((22 * s, 22 * s, 2 * R - 22 * s, 2 * R - 22 * s), 135, 405,
              fill=(255, 255, 255, 50), width=18 * s)
        n_minor = int(mx / step * 5)
        for k in range(n_minor + 1):
            a = math.radians(135 + 270 * k / n_minor)
            major = k % 5 == 0
            r0, r1 = R - (52 if major else 46) * s, R - 40 * s
            d.line((R + r0 * math.cos(a), R + r0 * math.sin(a), R + r1 * math.cos(a), R + r1 * math.sin(a)),
                   fill=(255, 255, 255, 230 if major else 120), width=(4 if major else 2) * s)
            if major:
                v = int(step * k / 5)
                lab = str(v // 1000) if mx >= 1000 else str(v)
                rr = R - 78 * s
                text(d, (R + rr * math.cos(a), R + rr * math.sin(a)), lab, self.f(22 * s), SUB, "mm", 0)
        unit = "x1000 rpm" if mx >= 1000 else label
        text(d, (R, R + 95 * s), unit, self.f(22 * s), SUB, "mm", 0)
        return img.resize((2 * r, 2 * r), Image.LANCZOS)

    def _graph_static(self, img):
        x0, y0, x1, y1 = self.graph_box
        gx0, gy0, gx1, gy1 = x0 + 16, y0 + 46, x1 - 16, y1 - 16
        self.g_inner = (gx0, gy0, gx1, gy1)
        d = ImageDraw.Draw(img)
        for v in range(0, SPEED_MAX + 1, 20):
            y = gy1 - (gy1 - gy0) * v / SPEED_MAX
            d.line((gx0, y, gx1, y), fill=GRID, width=1)
            text(d, (gx1 - 2, y - 2), str(v), self.f(14), SUB, "rb", 0)
        tel = self.tel
        m = (tel.t >= self.t_start) & (tel.t <= self.t_end)
        tt, sp = tel.t[m], tel.arr("speed")[m]
        if len(tt) < 2:
            return
        X = lambda t: gx0 + (gx1 - gx0) * (t - self.t_start) / max(1e-6, self.t_end - self.t_start)
        Y = lambda v: gy1 - (gy1 - gy0) * min(v, SPEED_MAX) / SPEED_MAX
        self._on_bands(d, X, self.t_start, self.t_end, gy0, gy1)
        d.line([(X(a), Y(b)) for a, b in zip(tt, sp)], fill=SPEED_COL + (255,), width=2)

    def _on_bands(self, d, X, ta, tb, gy0, gy1):
        """グラフ領域 [ta, tb] にエンジンON帯を塗る"""
        for a, b in self.tel.on_spans:
            if b >= ta and a <= tb:
                d.rectangle((X(max(a, ta)), gy0, X(min(b, tb)), gy1), fill=ACCENT + (70,))

    def _chart_frame(self, d, box, title):
        """小グラフのパネルとタイトルを描き、プロット領域を返す"""
        x0, y0, x1, y1 = box
        panel(d, box)
        text(d, (x0 + 16, y0 + 10), title, self.f(22), SUB)
        return (x0 + 16, y0 + 50, x1 - 16, y1 - 16)

    def _alt_static(self, img):
        """標高: 出力区間全体のプロファイル(GPS 測位中のみ)"""
        d = ImageDraw.Draw(img)
        gx0, gy0, gx1, gy1 = self.alt_inner = self._chart_frame(d, self.alt_box, "標高")
        tel = self.tel
        m = (tel.t >= self.t_start) & (tel.t <= self.t_end) & tel.has_fix
        self.alt_range = None
        if m.sum() < 2:
            text(d, ((gx0 + gx1) / 2, (gy0 + gy1) / 2), "GPS 未測位", self.f(20), SUB, "mm", 0)
            return
        alt = tel.arr("alt")
        lo, hi = alt[m].min(), alt[m].max()
        mid, half = (lo + hi) / 2, max(hi - lo, ALT_MIN_SPAN_M) / 2 * 1.15
        lo, hi = self.alt_range = (mid - half, mid + half)
        # 目盛り: 1-2-5 系列で 2〜4 本
        step = next(s for s in (1, 2, 5, 10, 20, 50, 100, 200, 500) if (hi - lo) / s <= 3.5)
        Y = lambda v: gy1 - (gy1 - gy0) * (v - lo) / (hi - lo)
        X = lambda t: gx0 + (gx1 - gx0) * (t - self.t_start) / max(1e-6, self.t_end - self.t_start)
        # 測位が途切れた区間で線を切る
        idx = np.flatnonzero(m)
        for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
            if len(run) < 2:
                continue
            pts = [(X(tel.t[i]), Y(alt[i])) for i in run]
            d.polygon(pts + [(pts[-1][0], gy1), (pts[0][0], gy1)], fill=ALT_COL + (60,))
            d.line(pts, fill=ALT_COL + (255,), width=2)
        for v in np.arange(math.ceil(lo / step) * step, hi, step):
            d.line((gx0, Y(v), gx1, Y(v)), fill=GRID, width=1)
            text(d, (gx0 + 2, Y(v) - 2), f"{v:g}", self.f(14), SUB, "lb", 0)

    def _fuel_p_static(self, img):
        d = ImageDraw.Draw(img)
        gx0, gy0, gx1, gy1 = self.fuel_p_inner = self._chart_frame(d, self.fuel_p_box, "燃圧")
        text(d, (self.fuel_p_box[0] + 70, self.fuel_p_box[1] + 34), f"直近{FUEL_P_WINDOW_S}秒",
             self.f(15), SUB, "ls", 0)
        for k in range(int(round(FUEL_P_MAX / 0.1)) + 1):
            v = 0.1 * k
            y = gy1 - (gy1 - gy0) * v / FUEL_P_MAX
            d.line((gx0, y, gx1, y), fill=GRID, width=1)
            if k % 2 == 0:
                text(d, (gx0 + 2, y - 2), f"{v:.1f}", self.f(14), SUB, "lb", 0)

    # ---------- 動的要素 ----------
    def _alt_dynamic(self, d, ts):
        x0, y0, x1, _ = self.alt_box
        i = int(np.clip(np.searchsorted(self.tel.t, ts), 0, len(self.tel.t) - 1))
        if self.alt_range is None or not self.tel.has_fix[i]:
            text(d, (x1 - 16, y0 + 8), "--- m", self.f(24), SUB, "ra")
            return
        gx0, gy0, gx1, gy1 = self.alt_inner
        lo, hi = self.alt_range
        alt = self.tel.interp("alt", ts)
        text(d, (x1 - 16, y0 + 8), f"{alt:.1f} m", self.f(24), TEXT, "ra")
        if self.t_end > self.t_start:
            cx = gx0 + (gx1 - gx0) * (ts - self.t_start) / (self.t_end - self.t_start)
            cy = gy1 - (gy1 - gy0) * min(max((alt - lo) / (hi - lo), 0), 1)
            d.line((cx, gy0, cx, gy1), fill=(255, 255, 255, 140), width=1)
            d.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=TEXT)

    def _fuel_p_dynamic(self, d, ts):
        """燃圧: 右端を現在として直近 FUEL_P_WINDOW_S 秒をスクロール表示"""
        tel = self.tel
        x0, y0, x1, _ = self.fuel_p_box
        gx0, gy0, gx1, gy1 = self.fuel_p_inner
        ta = ts - FUEL_P_WINDOW_S
        X = lambda t: gx0 + (gx1 - gx0) * (t - ta) / FUEL_P_WINDOW_S
        Y = lambda v: gy1 - (gy1 - gy0) * min(max(v, 0.0), FUEL_P_MAX) / FUEL_P_MAX
        self._on_bands(d, X, ta, ts, gy0, gy1)
        cur = tel.interp("fuel_p", ts)
        i0, i1 = np.searchsorted(tel.t, [ta, ts])
        tt = np.r_[tel.t[i0:i1], ts]
        vv = np.r_[tel.arr("fuel_p")[i0:i1], cur]
        if len(tt) >= 2:
            d.line(list(zip(X(tt), map(Y, vv))), fill=FUEL_P_COL + (255,), width=2)
        d.ellipse((gx1 - 5, Y(cur) - 5, gx1 + 5, Y(cur) + 5), fill=TEXT)
        text(d, (x1 - 16, y0 + 8), f"{cur:.3f} MPa", self.f(24), TEXT, "ra")

    # ---------- 動的要素 ----------
    def _needle(self, base: Image.Image, g, val, mx, color):
        cx, cy, r = g
        s = self.SS
        R = r * s
        lay = Image.new("RGBA", (2 * R, 2 * R), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        frac = max(0.0, min(1.0, val / mx))
        if frac > 0.002:
            d.arc((22 * s, 22 * s, 2 * R - 22 * s, 2 * R - 22 * s), 135, 135 + 270 * frac,
                  fill=color + (255,), width=18 * s)
        a = math.radians(135 + 270 * frac)
        # 目盛り数字と大きな数値を隠さない外周だけの短い針
        d.line((R + (R - 64 * s) * math.cos(a), R + (R - 64 * s) * math.sin(a),
                R + (R - 14 * s) * math.cos(a), R + (R - 14 * s) * math.sin(a)),
               fill=(255, 255, 255, 255), width=6 * s)
        lay = lay.resize((2 * r, 2 * r), Image.LANCZOS)
        base.alpha_composite(lay, (cx - r, cy - r))

    def render(self, ts: float) -> Image.Image:
        tel, f = self.tel, self.f
        img = self.base.copy()
        d = ImageDraw.Draw(img)

        spd = max(0.0, tel.interp("speed", ts))
        rpm_raw = tel.step("rpm", ts)          # ON/OFF は補間しない
        rpm = tel.interp("rpm", ts) if rpm_raw > 0 else 0.0
        eng_on = rpm_raw > RPM_ON_THRESHOLD
        dist = tel.interp("dist", ts)
        fuel = tel.interp("fuel", ts)
        eco = tel.step("eco", ts)
        run_t = tel.interp("run_time", ts)
        lap = int(tel.step("lap", ts))

        # --- 左上: 走行時間・ラップ ---
        text(d, (50, 42), "走行時間", f(20), SUB)
        text(d, (50, 70), fmt_time(run_t), f(42))
        lap_lbl = f"LAP {lap}" + (f"/{LAP_TOTAL}" if LAP_TOTAL else "")
        text(d, (290, 42), lap_lbl, f(20), SUB)
        cur = [s for s in tel.lap_starts if s[1] <= ts]
        if cur and lap > 0:
            lap_t = ts - cur[-1][1]
            text(d, (290, 70), fmt_time(lap_t, ms=True), f(42))
            if len(cur) >= 3:  # 前周タイム
                prev = cur[-1][1] - cur[-2][1]
                text(d, (545, 42), f"前周 {fmt_time(prev, ms=True)}", f(18), SUB, "ra")
        else:
            text(d, (290, 70), "--:--.---", f(42), SUB)

        # --- 速度グラフのカーソル ---
        gx0, gy0, gx1, gy1 = self.g_inner
        if self.t_end > self.t_start:
            cx = gx0 + (gx1 - gx0) * (ts - self.t_start) / (self.t_end - self.t_start)
            cy = gy1 - (gy1 - gy0) * min(spd, SPEED_MAX) / SPEED_MAX
            d.line((cx, gy0, cx, gy1), fill=(255, 255, 255, 140), width=1)
            d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=TEXT)

        # --- 標高・燃圧グラフ ---
        self._alt_dynamic(d, ts)
        if self.show_fuel_p:
            self._fuel_p_dynamic(d, ts)

        # --- 環境 ---
        rows = [("エンジン", f"{tel.interp('eng_temp', ts):.1f}℃"),
                ("気温", f"{tel.interp('air_temp', ts):.1f}℃"),
                ("湿度", f"{tel.interp('hum', ts):.0f}%"),
                ("気圧", f"{tel.interp('press', ts):.1f}kPa")]
        ex0, ey0, ex1, ey1 = self.env_box
        cw = (ex1 - ex0) / len(rows)
        for k, (a, b) in enumerate(rows):
            x = ex0 + 14 + k * cw
            text(d, (x, (ey0 + ey1) / 2), a, f(17), SUB, "lm", 1)
            text(d, (x + cw - 22, (ey0 + ey1) / 2), b, f(21), TEXT, "rm", 1)

        # --- 燃費パネル ---
        fx0, fy0, fx1, fy1 = self.fuel_box
        text(d, (fx0 + 20, fy0 + 15), "燃費", f(22), SUB)
        eco_s = f"{eco:,.1f}" if dist >= FUEL_ECO_MIN_DIST_M and eco > 0 else "---"
        text(d, (fx1 - 20, fy0 + 25), eco_s, f(64), ACCENT, "ra")
        text(d, (fx1 - 20, fy0 + 100), "km/L", f(20), SUB, "ra")
        if self.mask_osd:   # 距離・燃料はマスク帯(ゲージ右側)へ
            mx0, my0, mx1, my1 = OSD_MASK_BOX
            gx = self.g_rpm[0] + self.g_rpm[2] + 12
            text(d, (gx, my0 + 6), f"距離 {dist / 1000:.2f} km", f(21), TEXT, stroke=0)
            text(d, (gx, my0 + 34), f"燃料 {fuel:.1f} ml", f(21), TEXT, stroke=0)
        else:
            text(d, (fx0 + 20, fy0 + 100), f"距離 {dist / 1000:.2f} km", f(22))
            text(d, (fx0 + 20, fy0 + 130), f"燃料 {fuel:.1f} ml", f(22))

        # --- ゲージ ---
        self._needle(img, self.g_speed, spd, SPEED_MAX, SPEED_COL)
        self._needle(img, self.g_rpm, rpm, RPM_MAX, ACCENT)
        d = ImageDraw.Draw(img)
        cx, cy, _ = self.g_speed
        text(d, (cx, cy + 10), f"{spd:.0f}", f(96), TEXT, "mm", 3)
        cx, cy, _ = self.g_rpm
        text(d, (cx, cy + 10), f"{rpm:,.0f}" if eng_on else "0", f(72), TEXT, "mm", 3)
        badge = (cx - 70, cy + 120, cx + 70, cy + 160)
        if eng_on:
            d.rounded_rectangle(badge, 10, fill=ACCENT + (235,))
            text(d, (cx, cy + 140), "ENGINE ON", f(22), (20, 20, 20), "mm", 0)
        else:
            d.rounded_rectangle(badge, 10, outline=(255, 255, 255, 150), width=2)
            text(d, (cx, cy + 140), "COAST", f(22), SUB, "mm", 0)

        # --- 方位テープ(上中央) ---
        hd = interp_angle(tel.t, tel.heading, ts)
        self._heading_tape(d, hd)

        # --- マップ現在地 ---
        i = int(np.clip(np.searchsorted(tel.t, ts), 0, len(tel.t) - 1))
        if tel.has_fix[i]:
            la = tel.interp("lat", ts)
            lo = tel.interp("lon", ts)
            px, py = self.proj(la, lo)
            px += self.map_box[0]
            py += self.map_box[1]
            d.ellipse((px - 11, py - 11, px + 11, py + 11), fill=(255, 40, 40), outline=TEXT, width=3)
        return img

    def _heading_tape(self, d, hd):
        cx, y0, half, span = W // 2, 30, 320, 70.0
        d.rounded_rectangle((cx - half - 20, y0, cx + half + 20, y0 + 70), 12, fill=PANEL)
        names = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}
        start = int(math.floor((hd - span) / 5) * 5)
        for a in range(start, int(hd + span) + 1, 5):
            dx = (a - hd) / span * half
            x = cx + dx
            aa = a % 360
            if aa % 15 == 0:
                d.line((x, y0 + 50, x, y0 + 66), fill=(255, 255, 255, 200), width=2)
                lab = names.get(aa, str(aa))
                text(d, (x, y0 + 30), lab, self.f(24 if aa in names else 18),
                     ACCENT if aa in names else SUB, "mm", 0)
            else:
                d.line((x, y0 + 58, x, y0 + 66), fill=(255, 255, 255, 110), width=1)
        d.polygon([(cx - 9, y0 + 70), (cx + 9, y0 + 70), (cx, y0 + 56)], fill=(255, 40, 40))
        text(d, (cx, y0 + 92), f"{hd:03.0f}°", self.f(24), TEXT, "mm")


# =====================================================================
#  動画 I/O
# =====================================================================
def probe(video: str):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "default=nw=1", video],
        capture_output=True, text=True, check=True).stdout
    kv = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    num, den = kv["r_frame_rate"].split("/")
    return int(kv["width"]), int(kv["height"]), float(num) / float(den), float(kv["duration"])


def parse_sync(tel: Telemetry, s: str | None) -> float:
    """ログ時刻文字列 → ログ開始からの秒"""
    if s is None:
        return 0.0
    try:
        return float(s)  # 秒数で直接指定も可
    except ValueError:
        pass
    hms = pd.to_datetime(f"{tel.t0.date()} {s}")
    return (hms - tel.t0).total_seconds()


def main():
    ap = argparse.ArgumentParser(description="エコラン用テレメトリ・オーバーレイ")
    ap.add_argument("csv")
    ap.add_argument("-o", "--output", required=True, help=".png(プレビュー) / .mp4(合成) / .mov(透過)")
    ap.add_argument("--video", help="合成する車載動画")
    ap.add_argument("--sync", help="動画 0 秒目に対応するログ時刻 (例 11:16:50) またはログ開始からの秒")
    ap.add_argument("--offset", type=float, default=0.0, help="同期の微調整 [秒]")
    ap.add_argument("--duration", type=float, help="出力長 [秒](省略時は動画長 or ログ末尾まで)")
    ap.add_argument("--preview", type=float, help="ログ開始からの秒を指定して静止画 1 枚を出力")
    ap.add_argument("--fps", type=float, default=FPS_DEFAULT)
    ap.add_argument("--font", help="日本語フォント (.ttf/.ttc) のパス")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--mask-osd", action="store_true", help="ドラレコの焼き込み日時・速度を隠す")
    args = ap.parse_args()

    tel = load_log(args.csv)
    fonts = Fonts(args.font)
    t_sync = parse_sync(tel, args.sync) + args.offset

    # ---- プレビュー ----
    if args.preview is not None:
        r = OverlayRenderer(tel, max(0, t_sync), tel.t[-1], fonts, args.mask_osd)
        img = r.render(args.preview)
        bg = Image.new("RGBA", (W, H), (70, 80, 90, 255))
        bg.alpha_composite(img)
        bg.convert("RGB").save(args.output)
        print(f"preview → {args.output}")
        return

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg が見つかりません")

    vw, vh, fps = W, H, args.fps
    vdur = None
    if args.video:
        vw, vh, fps, vdur = probe(args.video)
    duration = args.duration or vdur or (tel.t[-1] - t_sync)
    n_frames = int(duration * fps)
    t_end = min(tel.t[-1], t_sync + duration)
    r = OverlayRenderer(tel, max(0.0, t_sync), t_end, fonts, args.mask_osd)

    raw_in = ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-r", f"{fps}", "-i", "-"]
    if args.video:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", args.video, *raw_in,
               "-filter_complex", f"[1:v]scale={vw}:{vh}:flags=lanczos[ov];[0:v][ov]overlay=0:0:shortest=1[v]",
               "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
               "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-c:a", "copy", args.output]
    elif args.output.lower().endswith(".mov"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *raw_in,
               "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", args.output]
    else:
        sys.exit("--video なしの場合、出力は .mov(透過 ProRes 4444)か .png(--preview)にしてください")

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t_wall = time.time()
    try:
        for k in range(n_frames):
            ts = t_sync + k / fps
            frame = r.render(min(max(ts, tel.t[0]), tel.t[-1]))
            proc.stdin.write(frame.tobytes())
            if k % int(fps * 10) == 0:
                el = time.time() - t_wall
                eta = el / max(1, k) * (n_frames - k)
                print(f"\r{k}/{n_frames} frames  ETA {eta:5.0f}s", end="", flush=True)
    except BrokenPipeError:
        pass  # 動画が先に終わった
    finally:
        if proc.stdin:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        proc.wait()
    print(f"\ndone → {args.output}")


if __name__ == "__main__":
    main()
