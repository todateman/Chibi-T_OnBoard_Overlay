import argparse

import pytest

from ecorun.cli import parse_position, resolve_range


@pytest.mark.parametrize("s, sec", [("95", 95), ("95.5", 95.5), ("1:35", 95), ("0:01:35.5", 95.5),
                                    ("1:00:00", 3600), (" 2:00 ", 120)])
def test_parse_position(s, sec):
    assert parse_position(s) == pytest.approx(sec)


@pytest.mark.parametrize("s", ["", "abc", "1:60", "-5", "1:2:3:4", "1::2"])
def test_parse_position_rejects(s):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_position(s)


def test_resolve_range_defaults():
    assert resolve_range(None, None, None, 300, clamp=True) == (0, 300)
    assert resolve_range(60, None, None, 300, clamp=True) == (60, 240)
    assert resolve_range(60, 100, None, 300, clamp=True) == (60, 40)
    assert resolve_range(60, None, 10, 300, clamp=True) == (60, 10)     # --duration は --start からの長さ


def test_resolve_range_clamps_to_video(capsys):
    assert resolve_range(60, 400, None, 300, clamp=True) == (60, 240)
    assert "切り詰め" in capsys.readouterr().err
    assert resolve_range(60, 400, None, 300, clamp=False) == (60, 340)   # 透過出力はログ末尾を超えてよい


@pytest.mark.parametrize("start, end, duration", [(10, 20, 5), (100, 50, None), (300, None, None), (10, None, 0)])
def test_resolve_range_rejects(start, end, duration):
    with pytest.raises(ValueError):
        resolve_range(start, end, duration, 300, clamp=True)
