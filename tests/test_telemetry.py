import pandas as pd
import pytest

from ecorun.telemetry import load_log
from conftest import write_synthetic_log


def test_lap_time_starts_at_run_start(tmp_path):
    """電源投入時から 1 周目でも、ラップタイムは走行時間が動き出した時刻から数える"""
    tel = load_log(str(write_synthetic_log(tmp_path / "IDLE.CSV", seconds=120, idle=30)))
    assert tel.run_start == pytest.approx(30.0, abs=0.11)
    assert tel.lap_starts[0] == (1, tel.run_start)
    assert all(t > tel.run_start for _, t in tel.lap_starts[1:])


def test_no_run_has_no_lap_starts(tmp_path):
    path = write_synthetic_log(tmp_path / "STOP.CSV", seconds=20)
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["走行時間(秒)"] = 0
    df.to_csv(path, index=False, encoding="utf-8-sig")
    tel = load_log(str(path))
    assert tel.run_start is None and tel.lap_starts == []
