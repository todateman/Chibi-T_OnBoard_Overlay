"""ロガー CSV の読み込みと前処理"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

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


class LogFormatError(ValueError):
    pass


@dataclass
class Telemetry:
    t: np.ndarray            # ログ開始からの秒
    t0: pd.Timestamp         # ログ開始時刻
    df: pd.DataFrame
    lap_starts: list         # [(lap_no, t_start)]
    heading: np.ndarray      # 方位 deg(GPS から算出)
    has_fix: np.ndarray      # GPS 有効フラグ
    on_spans: list           # エンジンON区間 [(t_start, t_end)]
    rpm_on_threshold: float  # on_spans の算出に使った閾値

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


def load_log(path: str, rpm_on_threshold: float = 300,
             heading_lookback_s: float = 3.0) -> Telemetry:
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
        raise LogFormatError(f"CSV に必要な列がありません: {', '.join(missing)}")
    df = df[list(rename)].rename(columns=rename)

    ts = pd.to_datetime(df["time"], format="%Y/%m/%d %H:%M:%S.%f", errors="coerce")
    df = df[ts.notna()].copy()
    ts = ts[ts.notna()]
    if len(ts) < 2:
        raise LogFormatError("CSV に有効な行がありません")
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

    # 方位: heading_lookback_s 秒前の位置からのベクトル。低速時は直前の値を保持
    lat = np.radians(df["lat"].to_numpy())
    lon = np.radians(df["lon"].to_numpy())
    idx = np.arange(len(df))
    j = np.clip(np.searchsorted(t, t - heading_lookback_s, side="right") - 1, 0, None)
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
    on = np.r_[0, (df["rpm"].to_numpy() > rpm_on_threshold).astype(int), 0]
    edges = np.diff(on)
    on_spans = [(t[max(s - 1, 0)], t[e - 1])
                for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))]
    return Telemetry(t, t0, df, lap_starts, hd, has_fix, on_spans, rpm_on_threshold)


def interp_angle(t: np.ndarray, a: np.ndarray, ts: float) -> float:
    s = np.interp(ts, t, np.sin(np.radians(a)))
    c = np.interp(ts, t, np.cos(np.radians(a)))
    return (math.degrees(math.atan2(s, c)) + 360) % 360


def parse_sync(tel: Telemetry, s: str | None) -> float:
    """ログ時刻文字列 (HH:MM:SS[.f]) または秒数 → ログ開始からの秒"""
    if s is None or s == "":
        return 0.0
    try:
        return float(s)  # 秒数で直接指定も可
    except ValueError:
        pass
    hms = pd.to_datetime(f"{tel.t0.date()} {s}")
    return (hms - tel.t0).total_seconds()
