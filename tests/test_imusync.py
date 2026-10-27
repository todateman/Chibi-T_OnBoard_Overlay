import struct

import numpy as np
import pandas as pd
import pytest

from ecorun import imusync
from ecorun.imusync import DT, estimate_offset, log_accel, parse_djmd, sync_file
from ecorun.ocrsync import SyncError
from ecorun.telemetry import load_log
from ecorun.timemap import TimeMap
from ecorun.video import VideoSource

from conftest import write_synthetic_log

OFFSET = 87.35       # ログ秒 = 動画秒 + OFFSET
FPS = 60000 / 1001


# ---- djmd 風のバイト列を組む ----
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _len(f: int, body: bytes) -> bytes:
    return _varint(f << 3 | 2) + _varint(len(body)) + body


def _vint(f: int, n: int) -> bytes:
    return _varint(f << 3) + _varint(n) if n else b""          # proto3 は 0 を省く


def _f32(f: int, x: float) -> bytes:
    return _varint(f << 3 | 5) + struct.pack("<f", x) if x else b""


def encode_djmd(ts_us, q, acc) -> bytes:
    """parse_djmd が読む形。先頭パケットにはヘッダ(field 1)が付く"""
    out = [_len(1, _len(1, b"dvtm_ac203.proto"))]
    for k, (t, qq, a) in enumerate(zip(ts_us, q, acc)):
        hdr = _vint(1, k) + _vint(2, int(t))
        st = (_len(2, _f32(1, 100.0)) + _len(9, b"".join(_f32(i + 1, x) for i, x in enumerate(qq)))
              + _len(10, b"".join(_f32(i + 2, x) for i, x in enumerate(a))))
        out.append(_len(3, _len(1, hdr) + _len(2, st) + _len(4, b"\x08\x01")))
    return b"".join(out)


# ---- 不規則なパルス&グライドのログと、それを撮ったカメラの IMU ----
def irregular_log(path, seconds=600, seed=1):
    """40 秒周期の合成ログでは周期ごとに同じ高さの山が出るので、パルスの間隔と長さを乱数にした速度に差し替える"""
    write_synthetic_log(path, seconds=seconds)
    df = pd.read_csv(path, encoding="utf-8-sig")
    rng = np.random.default_rng(seed)
    t = df["走行時間(秒)"].to_numpy()
    on = np.zeros(len(t), bool)
    s = 10.0
    while s < seconds:
        d = rng.uniform(4, 12)
        on |= (t >= s) & (t < s + d)
        s += d + rng.uniform(15, 50)
    sp = np.zeros(len(t))
    for i in range(1, len(t)):
        sp[i] = np.clip(sp[i - 1] + (3.0 if on[i] else -0.4) * (t[i] - t[i - 1]), 0, 45)
    df["速度(km/h)"] = sp.round(1)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _quat_yaw(yaw):
    return np.stack([np.cos(yaw / 2), np.zeros_like(yaw), np.zeros_like(yaw), np.sin(yaw / 2)], 1)


def camera_imu(tel, offset=OFFSET, seconds=400.0, axis=(1.0, 0.0, 0.0), seed=2):
    """ログの速度の微分を、動画秒 v = ログ秒 − offset のカメラが測った加速度にする(振動のノイズ付き)"""
    rng = np.random.default_rng(seed)
    v = np.arange(0, seconds, 1 / FPS)
    x, _ = log_accel(tel)
    a_lon = np.interp(v + offset, np.arange(len(x)) * DT, x)
    q = _quat_yaw(np.cumsum(rng.normal(0, 0.002, len(v))))   # 向きがゆっくり変わる
    fwd = imusync._rotate(q, np.tile(axis, (len(v), 1)))
    world = a_lon[:, None] * fwd + np.array([0.0, 0.0, -1.0])
    conj = q * np.array([1, -1, -1, -1])
    body = imusync._rotate(conj, world) + rng.normal(0, 0.05, (len(v), 3))
    return v, q, body


@pytest.fixture(scope="module")
def irr_tel(tmp_path_factory):
    return load_log(str(irregular_log(tmp_path_factory.mktemp("imu") / "IRR.CSV")))


def test_parse_djmd_roundtrip():
    ts = 6829716 + np.arange(5) * 16683
    q = np.array([[0.98, 0.02, -0.18, 0.02]] * 5)
    q[2] = [1.0, 0.0, 0.0, 0.0]                         # 0 の成分が省かれても読める
    acc = np.array([[-0.27, 0.01, -0.84]] * 5)
    v, qq, aa = parse_djmd(encode_djmd(ts, q, acc))
    assert v == pytest.approx((ts - ts[0]) / 1e6)
    assert qq == pytest.approx(q, abs=1e-6)
    assert aa == pytest.approx(acc, abs=1e-6)


def test_parse_djmd_rejects_unknown():
    with pytest.raises(SyncError):
        parse_djmd(_len(1, b"header only"))


def test_estimate_offset_subsample():
    rng = np.random.default_rng(0)
    x = np.convolve(rng.normal(size=6000), np.ones(20) / 20, mode="same")   # 600 秒の不規則な信号
    off = 123.45
    g = np.arange(4000) * DT                                                 # 動画 400 秒
    sig = np.interp(g + off, np.arange(len(x)) * DT, x) + rng.normal(0, 0.02, len(g))
    res = estimate_offset(sig, x, np.ones(len(x), bool))
    assert res.offset == pytest.approx(off, abs=0.05)
    assert res.r > 0.8 and res.r / res.r_next > 1.5
    assert res.windows and res.spread < 0.2


def test_sync_file_from_djmd(irr_tel):
    v, q, acc = camera_imu(irr_tel)
    imu = parse_djmd(encode_djmd(6829716 + v * 1e6, q, acc))
    res = sync_file("DJI_TEST.MP4", irr_tel, imu)
    assert res.offset == pytest.approx(OFFSET, abs=0.1)
    assert res.axis == "+x"


def test_sync_file_mount_direction(irr_tel):
    """カメラを横向き(前方 = -y)に付けても、相関のいちばん強い軸を選ぶ"""
    res = sync_file("DJI_TEST.MP4", irr_tel, camera_imu(irr_tel, axis=(0.0, -1.0, 0.0)))
    assert res.axis == "-y"
    assert res.offset == pytest.approx(OFFSET, abs=0.1)


def test_sync_file_no_correlation(irr_tel):
    rng = np.random.default_rng(3)
    v = np.arange(0, 400, 1 / FPS)
    q = _quat_yaw(np.zeros(len(v)))
    acc = np.array([0.0, 0.0, -1.0]) + rng.normal(0, 0.05, (len(v), 3))
    with pytest.raises(SyncError):
        sync_file("DJI_TEST.MP4", irr_tel, (v, q, acc))


def test_detect_segments_per_file(irr_tel, monkeypatch):
    """ファイルごとに時計が別なので 1 ファイル = 1 区間。TimeMap にすると各ファイルの offset になる"""
    offs = {"a.MP4": OFFSET, "b.MP4": OFFSET + 250.0}
    imus = {f: camera_imu(irr_tel, offset=o, seconds=300.0) for f, o in offs.items()}
    monkeypatch.setattr(imusync, "read_imu", lambda f: imus[f])
    monkeypatch.setattr(imusync, "creation_time", lambda f: None)
    src = VideoSource(["a.MP4", "b.MP4"], [300.0, 300.0], 2688, 1512, FPS)
    segs = imusync.detect(src, irr_tel)
    assert [(s.v0, s.v1) for s in segs] == [(0.0, 300.0), (300.0, 600.0)]
    tm = TimeMap.from_osd(segs, irr_tel.t0.to_pydatetime())
    assert tm.to_log(10.0) == pytest.approx(10.0 + offs["a.MP4"], abs=0.1)
    assert tm.to_log(310.0) == pytest.approx(10.0 + offs["b.MP4"], abs=0.1)


def test_build_timemap_imu_needs_video(irr_tel):
    from ecorun.cli import build_timemap
    with pytest.raises(SyncError, match="--video"):
        build_timemap("imu", 0.0, irr_tel, None, 10.0, None)
