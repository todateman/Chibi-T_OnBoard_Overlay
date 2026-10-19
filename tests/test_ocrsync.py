import json
import shutil
from datetime import datetime

import pytest

from ecorun.cli import main
from ecorun.ocrsync import SyncError, check_against_log, detect, main as sync_main, parse_osd
from ecorun.timemap import OsdSegment, TimeMap, osd_from_json
from ecorun.video import probe

from conftest import OSD_FPS, OSD_SEGMENTS

needs_tools = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("tesseract")),
                                 reason="ffmpeg か tesseract が無い")


@pytest.mark.parametrize("text, expected", [
    ("2023/06/10 10:37:31", datetime(2023, 6, 10, 10, 37, 31)),
    ("2023/06/1 0 10:37:31\n", datetime(2023, 6, 10, 10, 37, 31)),      # 空白の混入
    ("2023/06/10 1037:31", datetime(2023, 6, 10, 10, 37, 31)),          # 区切りの欠け
    ("2023/06/10 40:37:00", None),                                      # 値域外(1 → 4 の誤読)
    ("11:31:84", None),
    ("", None),
])
def test_parse_osd(text, expected):
    assert parse_osd(text) == expected


def test_check_against_log():
    t0 = datetime(2026, 10, 10, 15, 0)
    segs = [OsdSegment(0, 10, datetime(2026, 10, 10, 15, 1)), OsdSegment(10, 20, datetime(2026, 10, 10, 18, 0))]
    warns = check_against_log(segs, t0, 600)
    assert len(warns) == 1 and "区間 2" in warns[0]
    with pytest.raises(SyncError, match="タイムゾーン"):    # ちょうど 9 時間ずれ
        check_against_log([OsdSegment(0, 10, datetime(2026, 10, 10, 6, 1))], t0, 600)


@needs_tools
def test_detect_segments(osd_video):
    from ecorun.video import probe_videos
    segs = detect(probe_videos([str(osd_video)]), step=2)
    assert len(segs) == 2
    eps = 1 / OSD_FPS
    assert segs[0].v0 == 0 and segs[1].v0 == pytest.approx(5.0, abs=eps + 1e-6)
    for got, (v0, _, wall0) in zip(segs, OSD_SEGMENTS):
        assert abs((got.wall_at(v0) - wall0.to_pydatetime()).total_seconds()) <= 0.1 + eps


@needs_tools
def test_sync_auto_and_json(osd_video, osd_csv, tmp_path, capsys):
    js = tmp_path / "sync.json"
    sync_main([str(osd_video), "--csv", str(osd_csv), "--step", "2", "-o", str(js)])
    assert "ログ秒" in capsys.readouterr().out
    segs = osd_from_json(js)
    assert len(segs) == 2 and json.loads(js.read_text())["videos"] == [str(osd_video)]
    tm = TimeMap.from_osd(segs, datetime(2026, 10, 10, 15))
    assert tm.to_log(4.0) == pytest.approx(11.3, abs=0.2) and tm.to_log(6.0) == pytest.approx(181.6, abs=0.2)

    for sync in ("auto", str(js)):
        out = tmp_path / f"out_{sync[-4:]}.mp4"
        main([str(osd_csv), "--preset", "minimal", "--video", str(osd_video), "--sync", sync, "--sync-step", "2",
              "--start", "4", "--end", "6", "-o", str(out)])
        assert probe(str(out))[3] == pytest.approx(2.0, abs=0.15)


def test_sync_auto_needs_video(synth_csv, tmp_path):
    with pytest.raises(SystemExit, match="--video が必要"):
        main([str(synth_csv), "--sync", "auto", "-o", str(tmp_path / "x.mov")])
