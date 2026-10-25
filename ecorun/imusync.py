"""DJI のアクションカメラの動画を、内蔵 IMU の加速度でログに自動同期する

DJI Osmo Action 4 などは日時を焼き込まず、ファイルの creation_time は 1 秒単位で、カメラの時計も数分ずれていることがある。
代わりに `DJI meta`(codec tag djmd)のストリームに、フレームごとの protobuf で姿勢(クォータニオン)と加速度が入っている:

  field 3(1 フレーム)= {1: {1: フレーム番号, 2: カメラ内の時刻 [µs]}, 2: {9: 姿勢 (w, x, y, z), 10: 加速度 {2: x, 3: y, 4: z} [g]}}

加速度を姿勢で世界座標へ回して重力を引き、カメラの前方軸に沿った成分(前後加速度)を求めて、
ログの速度の微分と相互相関を取る。パルス&グライドの加速・惰行が強い手がかりになり、0.1 秒程度で合う。
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .ocrsync import SyncError
from .telemetry import Telemetry
from .timemap import OsdSegment
from .video import VideoSource

DT = 0.1                 # 相関を取る格子 [秒]
SMOOTH_S = 1.0           # 加速度・速度の移動平均 [秒](エンジンの振動と速度の量子化を消す)
MIN_SPEED_KMH = 2.0      # これ未満のログは相関に使わない(停車中にカメラを触った揺れを拾わない)
MIN_OVERLAP_S = 300.0    # 走行中の重なりがこれ未満の offset は候補にしない(ログ・動画が短ければその半分)
MIN_R = 0.3              # 相関の山がこれ未満なら失敗
MIN_PEAK_RATIO = 1.5     # 次の山(±5 秒より外)との比がこれ未満なら失敗
WINDOW_S = 250.0         # ドリフト確認の窓 [秒]
# 前方の候補(カメラの取り付け向きで変わる)。Osmo Action 4 を普通に付けると +x
AXES = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1), "-z": (0, 0, -1)}


# ---- djmd の読み出し ----
def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _fields(b: bytes):
    """protobuf のフィールドを (番号, 値) で順に返す。varint / 64bit / 長さ付き / 32bit(float)だけ"""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = struct.unpack_from("<d", b, i)[0], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 5:
            v, i = struct.unpack_from("<f", b, i)[0], i + 4
        else:
            raise ValueError(f"未対応の wire type {wt}")
        yield f, v


def parse_djmd(raw: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """djmd のパケットを連結したバイト列 → (v 秒(先頭 = 0), 姿勢 (N, 4) w,x,y,z, 加速度 (N, 3) [g])。
    protobuf は連結しても 1 つのメッセージとして読めるので、パケットの境目は要らない"""
    ts, qs, accs = [], [], []
    try:
        for f, frame in _fields(raw):
            if f != 3 or not isinstance(frame, bytes):
                continue
            hdr = st = None
            for f2, v2 in _fields(frame):
                if f2 == 1:
                    hdr = dict(_fields(v2))
                elif f2 == 2:
                    st = dict(_fields(v2))
            if hdr is None or st is None or 2 not in hdr or 9 not in st or 10 not in st:
                continue
            q, a = dict(_fields(st[9])), dict(_fields(st[10]))   # proto3 なので 0 の成分は省かれる
            ts.append(hdr[2])
            qs.append([q.get(k, 0.0) for k in (1, 2, 3, 4)])
            accs.append([a.get(k, 0.0) for k in (2, 3, 4)])
    except (ValueError, IndexError, struct.error) as e:
        raise SyncError(f"DJI meta の形式が想定と違います: {e}") from None
    if len(ts) < 2:
        raise SyncError("DJI meta から姿勢・加速度を読めません(この機種の形式には未対応)")
    v = (np.asarray(ts, float) - ts[0]) / 1e6
    return v, np.asarray(qs, float), np.asarray(accs, float)


def _probe_streams(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True)
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def find_djmd_stream(path: str) -> int | None:
    """DJI meta(djmd)のストリーム番号。無ければ None"""
    for s in _probe_streams(path).get("streams", []):
        if s.get("codec_tag_string") == "djmd" or s.get("tags", {}).get("handler_name") == "DJI meta":
            return int(s["index"])
    return None


def has_imu(src: VideoSource) -> bool:
    """全ファイルに DJI meta があるか(--sync auto で OCR の代わりにこちらを使う)"""
    return all(find_djmd_stream(f) is not None for f in src.files)


def creation_time(path: str) -> datetime | None:
    """ファイルの creation_time(カメラの時計。UTC)→ この PC のローカル時刻(naive)"""
    s = _probe_streams(path).get("format", {}).get("tags", {}).get("creation_time")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None) if s else None
    except ValueError:
        return None


def read_imu(path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """動画ファイルの DJI meta → parse_djmd の結果。動画は読み飛ばすので 17 GB でも十数秒"""
    idx = find_djmd_stream(path)
    if idx is None:
        raise SyncError(f"DJI meta(IMU)のストリームがありません: {path}")
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{idx}", "-c", "copy", "-f", "data", "-"],
                         capture_output=True)
    if out.returncode != 0:
        raise SyncError(f"DJI meta を取り出せません: {path}: {out.stderr.decode(errors='replace').strip()}")
    return parse_djmd(out.stdout)


# ---- 信号 ----
def _box(x: np.ndarray, n: int) -> np.ndarray:
    n = max(1, int(n))
    return np.convolve(x, np.ones(n) / n, mode="same")


def _rotate(q: np.ndarray, vec: np.ndarray) -> np.ndarray:
    """クォータニオン q (N, 4) で機体座標のベクトル vec (N, 3) を世界座標へ"""
    w, u = q[:, :1], q[:, 1:]
    t = 2 * np.cross(u, vec)
    return vec + w * t + np.cross(u, t)


def forward_accel(v: np.ndarray, q: np.ndarray, acc: np.ndarray, axis=(1, 0, 0)) -> np.ndarray:
    """前後加速度 [g] を DT 秒の格子(0, DT, 2DT, …)で。axis はカメラ座標での前方"""
    aw = _rotate(q, acc)
    aw[:, 2] += 1.0                                       # 静止時に (0, 0, -1) g になる重力を引く
    fwd = _rotate(q, np.tile(np.asarray(axis, float), (len(q), 1)))
    fwd[:, 2] = 0.0
    fwd /= np.linalg.norm(fwd, axis=1, keepdims=True) + 1e-9
    lon = (aw * fwd).sum(1)
    hz = (len(v) - 1) / max(v[-1] - v[0], 1e-9)
    grid = np.arange(0.0, v[-1] + DT / 2, DT)
    return _box(np.interp(grid, v, _box(lon, hz * SMOOTH_S)), SMOOTH_S / DT)


def log_accel(tel: Telemetry) -> tuple[np.ndarray, np.ndarray]:
    """ログの速度の微分 [g] を DT 秒の格子(ログ秒 0, DT, …)で → (加速度, 使える点のマスク)"""
    t = tel.t
    grid = np.arange(0.0, t[-1] + DT / 2, DT)
    sp = np.interp(grid, t, tel.df["speed"].to_numpy(float))
    a = np.gradient(_box(sp / 3.6, SMOOTH_S / DT), DT) / 9.81
    return a, sp >= MIN_SPEED_KMH


def _xcorr(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """c[k] = Σ_j a[j + k] b[j] を全ての k(-(len(b)-1) 〜 len(a)-1)について FFT で → (k, c)"""
    n = len(a) + len(b) - 1
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(a, nfft) * np.conj(np.fft.rfft(b, nfft)), nfft)
    k = np.arange(-(len(b) - 1), len(a))
    return k, c[k % nfft]


def _ncc(x: np.ndarray, mx: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """マスク付きの正規化相互相関: ずらし k ごとに x[j + k](mx が真の点だけ)と y[j] の相関係数 → (k, r, 点数)"""
    m = mx.astype(float)
    xm = np.where(mx, x, 0.0)
    ones = np.ones_like(y)
    k, n = _xcorr(m, ones)
    _, sx = _xcorr(xm, ones)
    _, sxx = _xcorr(xm * xm, ones)
    _, sy = _xcorr(m, y)
    _, syy = _xcorr(m, y * y)
    _, sxy = _xcorr(xm, y)
    n = np.round(n)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = (n * sxy - sx * sy) / np.sqrt(np.clip((n * sxx - sx ** 2) * (n * syy - sy ** 2), 1e-30, None))
    return k, np.where(n > 2, r, np.nan), n


def _corr_at(off: float, sig: np.ndarray, x: np.ndarray, mx: np.ndarray, sel=None) -> tuple[float, int]:
    """ログ秒 = 動画秒 + off としたときの相関係数と点数(秒未満の off は動画側を補間)"""
    g = np.arange(len(x)) * DT
    y = np.interp(g - off, np.arange(len(sig)) * DT, sig, left=np.nan, right=np.nan)
    m = mx & ~np.isnan(y)
    if sel is not None:
        m &= sel
    if m.sum() < 10:
        return float("nan"), int(m.sum())
    return float(np.corrcoef(x[m], y[m])[0, 1]), int(m.sum())


def _refine(off: float, sig, x, mx, half: float = 0.3, step: float = 0.01, sel=None) -> tuple[float, float]:
    offs = np.arange(off - half, off + half + step / 2, step)
    rs = np.array([_corr_at(o, sig, x, mx, sel)[0] for o in offs])
    if np.all(np.isnan(rs)):
        return off, float("nan")
    i = int(np.nanargmax(rs))
    return float(offs[i]), float(rs[i])


@dataclass
class ImuSync:
    offset: float                  # ログ秒 = 動画秒(そのファイルの先頭 = 0)+ offset
    r: float                       # 相関係数
    r_next: float                  # 次の山(±5 秒より外)の相関係数
    axis: str                      # 前方とみなしたカメラの軸
    windows: list[tuple[float, float, float]] = field(default_factory=list)  # (ログ秒の窓の先頭, offset, r)

    @property
    def spread(self) -> float:
        """窓ごとの offset のばらつき(最大 − 最小)。窓が 2 個未満なら 0"""
        offs = [o for _, o, _ in self.windows]
        return max(offs) - min(offs) if len(offs) > 1 else 0.0


def estimate_offset(sig: np.ndarray, x: np.ndarray, mx: np.ndarray, axis: str = "+x") -> ImuSync:
    """動画の前後加速度 sig(動画秒 0 から DT 刻み)と、ログの加速度 x・マスク mx(ログ秒 0 から DT 刻み)の
    ずれを求める。相関が弱い・山が 1 つに決まらなければ SyncError"""
    k, r, n = _ncc(x, mx, sig)
    need = min(MIN_OVERLAP_S, 0.5 * min(mx.sum() * DT, len(sig) * DT)) / DT
    r = np.where(n >= need, r, np.nan)
    if np.all(np.isnan(r)):
        raise SyncError("動画とログで、走行中に重なる時間が短すぎます")
    i = int(np.nanargmax(r))
    away = np.where(np.abs(k - k[i]) * DT > 5.0, r, np.nan)
    r_next = float(np.nanmax(away)) if not np.all(np.isnan(away)) else 0.0
    off, r_best = _refine(float(k[i]) * DT, sig, x, mx)
    res = ImuSync(off, r_best, r_next, axis)
    g = np.arange(len(x)) * DT
    for w0 in np.arange(0.0, g[-1], WINDOW_S):
        sel = (g >= w0) & (g < w0 + WINDOW_S)
        if (sel & mx).sum() * DT < WINDOW_S * 0.4:
            continue
        o, rw = _refine(off, sig, x, mx, half=3.0, step=0.02, sel=sel)
        if rw >= MIN_R:
            res.windows.append((float(w0), o, rw))
    return res


def _check(res: ImuSync, name: str):
    if res.r < MIN_R:
        raise SyncError(f"{name}: 加速度とログの速度の相関が弱く、同期できません(r = {res.r:.2f})")
    if res.r_next > 0 and res.r / res.r_next < MIN_PEAK_RATIO:
        raise SyncError(f"{name}: 同期の候補が 1 つに決まりません(r = {res.r:.2f}、次点 {res.r_next:.2f})")


def sync_file(path: str, tel: Telemetry, imu=None) -> ImuSync:
    """1 ファイルのずれ。前方の軸は相関がいちばん強いものを選ぶ。imu を渡せば read_imu を省く"""
    v, q, acc = imu if imu is not None else read_imu(path)
    x, mx = log_accel(tel)
    qs = q[::max(1, len(q) // 1000)]
    best = None
    for name, ax in AXES.items():
        fwd = _rotate(qs, np.tile(np.asarray(ax, float), (len(qs), 1)))
        if np.median(np.hypot(fwd[:, 0], fwd[:, 1])) < 0.7:   # ほぼ鉛直な軸は前方になりえない
            continue
        try:
            res = estimate_offset(forward_accel(v, q, acc, ax), x, mx, name)
        except SyncError:
            continue
        if best is None or res.r > best.r:
            best = res
    if best is None:
        raise SyncError(f"{Path(path).name}: 動画とログで、走行中に重なる時間が短すぎます")
    _check(best, Path(path).name)
    return best


def detect(src: VideoSource, tel: Telemetry, progress: Callable[[str], None] | None = None) -> list[OsdSegment]:
    """全ファイルを IMU で同期して OsdSegment の列にする(ファイルごとに 1 区間。カメラの時計はファイルごとに別)。
    wall0 は区間の先頭に対応するログの時刻なので、ocrsync の結果と同じく TimeMap.from_osd や JSON 保存に使える"""
    say = progress or (lambda s: None)
    t0 = tel.t0.to_pydatetime()
    segs = []
    for f, start, dur in zip(src.files, src.starts, src.durations):
        say(f"IMU を読み取り中: {Path(f).name}")
        res = sync_file(f, tel)
        say(describe_file(f, res, t0))
        segs.append(OsdSegment(start, start + dur, t0 + timedelta(seconds=res.offset)))
    return segs


def describe_file(path: str, res: ImuSync, t0: datetime) -> str:
    """1 ファイルの結果(人が読む用)。カメラの時計のずれも添える"""
    wall = t0 + timedelta(seconds=res.offset)
    lines = [f"  {Path(path).name}: 動画 0 秒目 = {wall:%H:%M:%S.%f}"[:-4]
             + f"(ログ秒 {res.offset:+.2f})  相関 {res.r:.2f}(次点 {res.r_next:.2f})  前方 = カメラの {res.axis} 軸"]
    if res.windows:
        lines.append(f"    {WINDOW_S:.0f} 秒ごとの窓 {len(res.windows)} 個のばらつき {res.spread:.2f} 秒"
                     + ("(大きいので、途中で時計がずれている可能性)" if res.spread > 0.5 else ""))
    ct = creation_time(path)
    if ct is not None:
        d = (ct - wall).total_seconds()
        m, s = divmod(abs(d), 60)
        lines.append(f"    カメラの時計(creation_time {ct:%H:%M:%S})は {int(m)} 分 {s:.1f} 秒"
                     + ("進んでいた" if d >= 0 else "遅れていた"))
    return "\n".join(lines)


def detect_cli(src: VideoSource, tel: Telemetry) -> list[OsdSegment]:
    """CLI 用: 進捗を stderr に出して detect"""
    return detect(src, tel, progress=lambda s: print(s, file=sys.stderr))
