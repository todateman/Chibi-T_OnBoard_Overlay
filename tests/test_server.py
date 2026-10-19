import shutil

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
    r = client.post("/api/render", json={"preset": preset, "csv": str(synth_csv), "ts": 70,
                                         "sync": "30", "start": 20, "end": 80})
    assert r.status_code == 200
    info = client.get("/api/log-info", params={"csv": str(synth_csv)}).json()
    assert info["has_fuel_p"] and info["missing"] == [] and info["t_end"] > 100


def test_schema_and_types(client):
    assert "プリセット" in client.get("/api/schema").json()["title"]
    types = client.get("/api/widget-types").json()
    assert any(t["type"] == "map" for t in types)
    assert client.get("/").status_code == 200


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg が無い")
def test_video_concat(client, seg_dir):
    files = sorted(str(p) for p in seg_dir.glob("F9_*.MP4"))
    by_dir = client.get("/api/video-info", params={"video": str(seg_dir)}).json()
    by_list = client.get("/api/video-info", params={"video": files}).json()
    assert by_dir == by_list
    assert [f["path"] for f in by_dir["files"]] == files
    assert by_dir["duration"] == pytest.approx(3.0, abs=0.1) and by_dir["files"][1]["start"] > 0.9
    r = client.get("/api/frame", params={"video": files, "t": 1.5, "width": 320, "height": 180})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get("/api/video-info", params={"video": "nothing.mp4"}).status_code == 404
    assert client.get("/api/video-info", params={"video": [files[0], "README.md"]}).status_code == 422
    # 再生用のファイル配信(<video> がシークできるよう Range に応える)
    r = client.get("/api/video-file", params={"video": str(seg_dir), "i": 1}, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert r.content == open(files[1], "rb").read(100)
    assert client.get("/api/video-file", params={"video": str(seg_dir), "i": 9}).status_code == 404


def test_render_with_timemap(client, synth_csv):
    preset = client.get("/api/presets/minimal").json()["preset"]
    tm = [{"v0": 0, "v1": 5, "offset": 10}, {"v0": 5, "v1": 9, "offset": 100}]
    r = client.post("/api/render", json={"preset": preset, "csv": str(synth_csv), "ts": 106, "timemap": tm})
    assert r.status_code == 200


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("tesseract")), reason="ffmpeg か tesseract が無い")
def test_auto_sync(client, osd_video, osd_csv):
    r = client.post("/api/auto-sync", json={"video": [str(osd_video)], "csv": str(osd_csv), "step": 2}).json()
    segs = r["segments"]
    assert len(segs) == 2 and r["warnings"] == []
    assert segs[0]["wall"].startswith("2026-10-10T15:00:07") and segs[1]["offset"] == pytest.approx(175.6, abs=0.2)
    # ログと重ならなければ 422
    r = client.post("/api/auto-sync", json={"video": [str(osd_video)], "csv": "sampledata/LOG0612.CSV", "step": 2})
    assert r.status_code in (404, 422)


def _wait_export(client, timeout=60):
    import time
    t_end = time.time() + timeout
    while time.time() < t_end:
        st = client.get("/api/export").json()
        if st["state"] != "running":
            return st
        time.sleep(0.2)
    raise AssertionError(f"書き出しが終わらない: {st}")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg が無い")
def test_export_overlay(client, synth_csv, tmp_path):
    """GUI の書き出し: 画面のプリセット(未保存でよい)で ecorun-overlay を別プロセスで実行する"""
    preset = client.get("/api/presets/minimal").json()["preset"]
    out = tmp_path / "ov.mov"
    body = {"preset": preset, "csv": str(synth_csv), "sync": "10", "start": 0, "end": 0.5, "output": str(out)}
    st = client.post("/api/export", json=body).json()
    assert st["state"] == "running" and "--start 0" not in st["command"] and "--end 0.5" in st["command"]
    st = _wait_export(client)
    assert st["state"] == "done", st["log"]
    assert out.stat().st_size > 0 and st["total"] == 15
    # 既にあれば 409(GUI は確認してから overwrite: true で送り直す)
    r = client.post("/api/export", json=body)
    assert r.status_code == 409 and r.json()["exists"]
    assert client.post("/api/export", json={**body, "overwrite": True}).status_code == 200
    assert _wait_export(client)["state"] == "done"


def test_export_validation(client, synth_csv, tmp_path):
    preset = client.get("/api/presets/minimal").json()["preset"]
    body = {"preset": preset, "csv": str(synth_csv)}
    # 動画なしで .mp4 は書けない / 出力先フォルダが無い / 動画以外の拡張子
    assert client.post("/api/export", json={**body, "output": str(tmp_path / "a.mp4")}).status_code == 422
    assert client.post("/api/export", json={**body, "output": str(tmp_path / "no/a.mov")}).status_code == 422
    assert client.post("/api/export", json={**body, "output": str(tmp_path / "a.png")}).status_code == 422
    assert client.post("/api/export", json={"kind": "concat", "output": str(tmp_path / "a.mp4")}).status_code == 422


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg が無い")
def test_export_cancel(client, synth_csv, tmp_path):
    preset = client.get("/api/presets/default").json()["preset"]
    out = tmp_path / "long.mov"
    client.post("/api/export", json={"preset": preset, "csv": str(synth_csv), "output": str(out)})
    assert client.post("/api/export", json={"preset": preset, "csv": str(synth_csv),
                                            "output": str(tmp_path / "b.mov")}).status_code == 409   # 1 本ずつ
    client.post("/api/export/cancel")
    st = _wait_export(client)
    assert st["state"] == "cancelled" and not out.exists()
