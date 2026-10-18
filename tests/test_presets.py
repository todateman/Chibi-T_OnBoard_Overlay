import json
import tomllib

import pytest

from ecorun.config import (PRESET_DIR, SCHEMA_PATH, PresetError, dump_preset, load_preset, parse_preset,
                           preset_json_schema, preset_to_dict)
from ecorun.layout import lint_preset, widget_types

PRESETS = sorted(p.stem for p in PRESET_DIR.glob("*.toml"))


@pytest.mark.parametrize("name", PRESETS)
def test_preset_is_valid_and_lint_clean(name):
    p = load_preset(name)
    assert not [i for i in lint_preset(p) if i["level"] != "info"]


@pytest.mark.parametrize("name", PRESETS)
def test_dump_roundtrip_keeps_file_unchanged(name):
    src = (PRESET_DIR / f"{name}.toml").read_text(encoding="utf-8")
    assert dump_preset(load_preset(name), src) == src


def test_dump_keeps_comments_on_edit():
    src = (PRESET_DIR / "default.toml").read_text(encoding="utf-8")
    d = preset_to_dict(load_preset("default"))
    d["widgets"][0]["x"] = 44
    d["widgets"].reverse()
    d["widgets"].append({"type": "text"})
    out = dump_preset(parse_preset(d), src)
    assert "# 地平線付近はコース前方" in out
    assert "# エンジンON・強調色(オレンジ)" in out
    p2 = parse_preset(tomllib.loads(out))
    assert p2 == parse_preset(d)
    assert p2.widgets[-1].id == "text"


def test_schema_file_is_up_to_date():
    expected = json.dumps(preset_json_schema(), ensure_ascii=False, indent=2) + "\n"
    assert SCHEMA_PATH.read_text(encoding="utf-8") == expected, "ecorun-layout schema --write を実行する"


def test_validation_errors_point_to_location():
    with pytest.raises(PresetError) as e:
        parse_preset({"widgets": [{"type": "speed_gauge", "max": 0, "colour": "x"}]})
    locs = {err["loc"] for err in e.value.errors}
    assert {"widgets[0].max", "widgets[0].colour"} <= locs


def test_duplicate_ids_rejected():
    with pytest.raises(PresetError):
        parse_preset({"widgets": [{"type": "timer", "id": "a"}, {"type": "map", "id": "a"}]})


def test_ids_are_filled_from_type():
    p = parse_preset({"widgets": [{"type": "text"}, {"type": "text"}]})
    assert [w.id for w in p.widgets] == ["text", "text_2"]


def test_lint_detects_problems():
    p = parse_preset({
        "keepout": [{"name": "地平線", "x": 0, "y": 380, "w": 1920, "h": 260}],
        "widgets": [{"type": "timer", "x": 1800}, {"type": "map", "y": 400},
                    {"type": "speed_gauge", "x": 0, "y": 0}, {"type": "rpm_gauge", "x": 100, "y": 100}],
    })
    codes = {(i["code"], i["level"]) for i in lint_preset(p)}
    assert ("out-of-canvas", "error") in codes
    assert ("keepout", "warning") in codes
    assert ("overlap", "warning") in codes


def test_widget_types_cover_all_models():
    types = widget_types()
    assert {t["type"] for t in types} >= {"timer", "map", "speed_gauge", "rpm_gauge", "mask", "text"}
    for t in types:
        assert t["title"] and t["description"]
