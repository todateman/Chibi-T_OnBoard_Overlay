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


def test_disabled_widget_not_drawn(tel):
    p = parse_preset({"widgets": [{"type": "mask", "enabled": False}]})
    img = OverlayRenderer(tel, p, 0, tel.t[-1]).render(1.0)
    assert img.getchannel("A").getbbox() is None


def test_empty_preset_renders_transparent(tel):
    img = OverlayRenderer(tel, Preset(), 0, tel.t[-1]).render(1.0)
    assert img.getchannel("A").getbbox() is None
