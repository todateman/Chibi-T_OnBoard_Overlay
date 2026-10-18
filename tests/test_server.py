import pytest
from fastapi.testclient import TestClient

import ecorun.config as config
import ecorun.gui.server as server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """presets/ を一時ディレクトリに差し替えたクライアント"""
    for p in config.PRESET_DIR.glob("*.toml"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config, "PRESET_DIR", tmp_path)
    monkeypatch.setattr(server, "PRESET_DIR", tmp_path)
    return TestClient(server.app)


def test_preset_crud(client, tmp_path):
    names = {p["name"] for p in client.get("/api/presets").json()}
    assert {"default", "f9_mask", "minimal"} <= names
    r = client.get("/api/presets/default").json()
    preset, mtime = r["preset"], r["mtime"]
    preset["widgets"][0]["x"] = 50
    r = client.put("/api/presets/default", json={"preset": preset, "base_mtime": mtime})
    assert r.status_code == 200, r.text
    text = (tmp_path / "default.toml").read_text(encoding="utf-8")
    assert "x = 50" in text and "# 地平線付近" in text            # コメントは残る
    # 古い mtime で保存しようとすると 409
    r = client.put("/api/presets/default", json={"preset": preset, "base_mtime": mtime - 10})
    assert r.status_code == 409
    assert client.post("/api/presets/default/copy", json={"to": "mine"}).status_code == 200
    assert client.post("/api/presets/default/copy", json={"to": "mine"}).status_code == 409
    assert client.delete("/api/presets/mine").status_code == 200
    assert client.get("/api/presets/../etc").status_code in (400, 404)


def test_put_rejects_invalid(client):
    r = client.put("/api/presets/x", json={"preset": {"widgets": [{"type": "timer", "w": 1}]}})
    assert r.status_code == 422
    assert r.json()["errors"][0]["loc"] == "widgets[0].w"


def test_validate_returns_lint(client):
    r = client.post("/api/validate", json={"preset": {"widgets": [{"type": "timer", "x": 1900}]}}).json()
    assert r["valid"] and r["issues"][0]["code"] == "out-of-canvas"


def test_render_png(client, synth_csv):
    preset = client.get("/api/presets/default").json()["preset"]
    r = client.post("/api/render", json={"preset": preset, "csv": str(synth_csv), "ts": 30, "debug_boxes": True})
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    info = client.get("/api/log-info", params={"csv": str(synth_csv)}).json()
    assert info["has_fuel_p"] and info["t_end"] > 100


def test_schema_and_types(client):
    assert "プリセット" in client.get("/api/schema").json()["title"]
    types = client.get("/api/widget-types").json()
    assert any(t["type"] == "map" for t in types)
    assert client.get("/").status_code == 200
