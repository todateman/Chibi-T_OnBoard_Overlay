import numpy as np
import pytest
from PIL import Image

from ecorun.config import WIDGET_BY_TYPE, Preset, load_preset, parse_preset
from ecorun.render import OverlayRenderer
from ecorun.telemetry import load_log
from conftest import write_synthetic_log


def _alpha_bbox_outside(img: Image.Image, box):
    """box(x, y, w, h)の外に不透明画素があればその bbox を返す"""
    a = np.array(img.getchannel("A"))
    x, y, w, h = box
    a[y:y + h + 1, x:x + w + 1] = 0      # Pillow の矩形は右下端を含むので 1 px は許容
    ys, xs = np.nonzero(a)
    return None if len(xs) == 0 else (xs.min(), ys.min(), xs.max(), ys.max())


@pytest.mark.parametrize("wtype", sorted(WIDGET_BY_TYPE))
@pytest.mark.parametrize("scale", [1.0, 0.6, 1.4])
def test_widget_draws_inside_its_box(tel, wtype, scale):
    base = WIDGET_BY_TYPE[wtype]()
    w, h = max(8, round(base.w * scale)), max(8, round(base.h * scale))
    x, y = min(base.x, 1920 - w), min(base.y, 1080 - h)
    p = parse_preset({"widgets": [{"type": wtype, "x": x, "y": y, "w": w, "h": h}]})
    r = OverlayRenderer(tel, p, 0, tel.t[-1])
    for ts in (0.0, 3.0, 45.0, 100.0, tel.t[-1]):
        out = _alpha_bbox_outside(r.render(ts), (x, y, w, h))
        assert out is None, f"{wtype} x{scale} が ts={ts} で box の外 {out} に描いている"


@pytest.mark.parametrize("name", ["default", "f9_mask", "minimal"])
def test_presets_render(tel, name):
    r = OverlayRenderer(tel, load_preset(name), 0, tel.t[-1], debug_boxes=True)
    img = r.render(60.0)
    assert img.size == (1920, 1080) and img.mode == "RGBA"


def test_fuel_p_graph_skipped_without_column(tmp_path):
    tel = load_log(str(write_synthetic_log(tmp_path / "NOFP.CSV", seconds=30, fuel_p=False)))
    r = OverlayRenderer(tel, load_preset("default"), 0, tel.t[-1])
    assert r.skipped == ["fuel_p_graph"]
    r.render(10.0)


def test_env_and_alt_skipped_without_columns(tmp_path):
    """2025 年のログ(標高・気圧・気温・湿度・燃圧なし)でも描ける。env はエンジン温度だけ出す"""
    tel = load_log(str(write_synthetic_log(tmp_path / "NOENV.CSV", seconds=30, fuel_p=False, env=False)))
    r = OverlayRenderer(tel, load_preset("default"), 0, tel.t[-1])
    assert r.skipped == ["alt_graph", "fuel_p_graph"]
    r.render(10.0)
    env = next(w for w in r.widgets if w.cfg.type == "env")
    assert env.items == ["eng_temp"]
    p = parse_preset({"widgets": [{"type": "env"}]})
    w = p.widgets[0]
    out = _alpha_bbox_outside(OverlayRenderer(tel, p, 0, tel.t[-1]).render(10.0), (w.x, w.y, w.w, w.h))
    assert out is None


def test_env_skipped_when_no_items(tmp_path):
    tel = load_log(str(write_synthetic_log(tmp_path / "NOENV.CSV", seconds=30, env=False)))
    p = parse_preset({"widgets": [{"type": "env", "items": ["air_temp", "hum"]}]})
    assert OverlayRenderer(tel, p, 0, tel.t[-1]).skipped == ["env"]


def test_disabled_widget_not_drawn(tel):
    p = parse_preset({"widgets": [{"type": "mask", "enabled": False}]})
    img = OverlayRenderer(tel, p, 0, tel.t[-1]).render(1.0)
    assert img.getchannel("A").getbbox() is None


def test_empty_preset_renders_transparent(tel):
    img = OverlayRenderer(tel, Preset(), 0, tel.t[-1]).render(1.0)
    assert img.getchannel("A").getbbox() is None


def test_map_on_segments_per_lap(tel):
    """コース図のエンジンON区間は走行中の周の分だけ描き、周が変わると切り替わる"""
    starts = [(lap, t) for lap, t in tel.lap_starts if lap >= 1]
    assert len(starts) >= 2
    r = OverlayRenderer(tel, parse_preset({"widgets": [{"type": "map"}]}), 0, tel.t[-1])
    m = r.widgets[0]
    for _, t in starts[:2]:
        r.render(t + 1.0)
    a1, a2 = (np.array(m._lap_layers[lap].getchannel("A")) for lap, _ in starts[:2])
    assert a1.any() and a2.any() and not np.array_equal(a1, a2)


def test_map_on_segments_all_laps_is_static(tel):
    p = {"widgets": [{"type": "map", "on_segments_scope": "all"}]}
    r_all = OverlayRenderer(tel, parse_preset(p), 0, tel.t[-1])
    r_lap = OverlayRenderer(tel, parse_preset({"widgets": [{"type": "map"}]}), 0, tel.t[-1])
    r_all.render(60.0)
    assert r_all.widgets[0]._lap_layers == {}
    assert not np.array_equal(np.array(r_all.base), np.array(r_lap.base))   # 全周分は静的レイヤに入る
