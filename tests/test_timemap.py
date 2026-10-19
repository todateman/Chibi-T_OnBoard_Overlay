from datetime import datetime

import pytest

from ecorun.timemap import OsdSegment, Segment, TimeMap, osd_from_json, osd_to_json


def test_constant():
    tm = TimeMap.constant(30.0)
    assert tm.is_constant and tm.to_log(0) == 30 and tm.to_log(100.5) == 130.5
    assert tm.log_range(10, 20) == (40, 50)
    assert tm.shifted(-0.5).to_log(0) == 29.5


def test_piecewise():
    tm = TimeMap([Segment(5, 9, 200), Segment(0, 5, 100)])      # 順不同でもよい
    assert tm.to_log(0) == 100 and tm.to_log(4.9) == pytest.approx(104.9)
    assert tm.to_log(5) == 205                                  # 境界は後ろの区間
    assert tm.to_log(-1) == 99 and tm.to_log(12) == 212         # 範囲外は端の区間で外挿
    assert tm.segment_index(6) == 1
    assert tm.log_range(2, 7) == (102, 207)                     # 飛びをまたぐ
    assert tm.log_range(6, 7) == (206, 207)


def test_osd_roundtrip(tmp_path):
    segs = [OsdSegment(0, 5.5, datetime(2026, 10, 10, 15, 0, 7, 300000)),
            OsdSegment(5.5, 9, datetime(2026, 10, 10, 15, 3, 0, 600000))]
    osd_to_json(segs, tmp_path / "s.json", ["a.mp4"])
    assert osd_from_json(tmp_path / "s.json") == segs
    assert segs[1].wall_at(6.5) == datetime(2026, 10, 10, 15, 3, 1, 600000)
    tm = TimeMap.from_osd(segs, datetime(2026, 10, 10, 15, 0, 0))
    assert tm.to_log(0) == pytest.approx(7.3) and tm.to_log(5.5) == pytest.approx(180.6)
