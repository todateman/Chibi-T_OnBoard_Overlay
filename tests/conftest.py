import math
import subprocess

import numpy as np
import pandas as pd
import pytest

from ecorun.telemetry import load_log


def write_synthetic_log(path, seconds=180, hz=10, fuel_p=True):
    """現行フォーマット(10 Hz)の列名で、周回コースをパルス&グライドで走る合成ログを書く"""
    n = seconds * hz
    t = np.arange(n) / hz
    on = (t % 40) < 8                                   # 40 秒周期で 8 秒だけエンジンON
    speed = np.clip(np.where(on, (t % 40) * 4, 32 - ((t % 40) - 8) * 0.6), 0, 60)
    dist = np.cumsum(speed / 3.6 / hz)
    ang = dist / 600 * 2 * math.pi                      # 1 周 600 m の円
    lat = 34.84 + 0.0015 * np.sin(ang)
    lon = 136.54 + 0.0018 * np.cos(ang)
    lat[:50] = lon[:50] = 0.0                           # 先頭 5 秒は GPS 未測位
    ts = pd.Timestamp("2026-10-10 15:00:00") + pd.to_timedelta(t, unit="s")
    df = pd.DataFrame({
        "記録日時": ts.strftime("%Y/%m/%d %H:%M:%S.%f").str[:-4],
        "速度(km/h)": speed.round(1),
        "ラップ数(周目)": (dist // 600).astype(int) + 1,
        "走行時間(秒)": t,
        "回転数(rpm)": np.where(on, 2500, 0),
        "走行距離積算(m)": dist.round(0),
        "積算燃料消費量(ml)": (np.cumsum(on) * 0.002).round(2),
        "燃費(km/l)": np.where(dist > 100, 300.0, 0.0),
        "lat(緯度)": lat, "lng(経度)": lon,
        "標高(m)": 10 + 2 * np.sin(ang),
        "温度(C)": 80.0, "気圧(kPa)": 101.3, "気温(C)": 25.0, "湿度(%)": 50.0,
    })
    if fuel_p:
        df["燃圧(MPa)"] = np.where(on, 0.31, 0.26)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


@pytest.fixture(scope="session")
def synth_csv(tmp_path_factory):
    return write_synthetic_log(tmp_path_factory.mktemp("log") / "SYN.CSV")


@pytest.fixture(scope="session")
def tel(synth_csv):
    return load_log(str(synth_csv))


COLORS = ["red", "blue", "lime"]


def make_video(path, color, seconds=1.0, size="320x180", rate=10):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"color=c={color}:size={size}:rate={rate}", "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.fixture(scope="session")
def seg_dir(tmp_path_factory):
    """ドラレコの分割ファイル相当: 色違いの 1 秒動画 3 本(名前順 = 色順)"""
    d = tmp_path_factory.mktemp("seg")
    for i, c in enumerate(COLORS):
        make_video(d / f"F9_{i:04d}.MP4", c)
    (d / "._F9_0000.MP4").write_bytes(b"")   # macOS のリソースフォーク
    (d / "memo.txt").write_text("x")
    return d
