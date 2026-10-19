"""ecorun-gui: レイアウトを編集するローカル Web GUI のサーバー

  uv run ecorun-gui            # http://127.0.0.1:8765 を開く
  uv run ecorun-gui --port 9000 --no-browser

ブラウザ側(static/)は編集中のプリセットを JSON で持ち、描画・検証・保存をこの API に頼む。
プレビューは本番(ecorun-overlay)と同じ OverlayRenderer で描くので見た目は完全に一致する。
API は /docs(OpenAPI)に一覧がある。AI エージェントは TOML を直接編集してもよく、
その変更は /api/events(SSE)で GUI に通知され、自動で再読込される。
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import hashlib
import io
import json
import tempfile
import threading
import webbrowser
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import (PRESET_DIR, PRESET_NAME_RE, ROOT_DIR, PresetError, dump_preset, list_presets,
                      parse_preset, preset_json_schema, preset_to_dict, read_preset_file, save_preset)
from ..draw import Fonts
from ..layout import lint_preset, widget_types
from ..render import OverlayRenderer
from ..telemetry import OPTIONAL_COLS, LogFormatError, Telemetry, load_log, parse_sync
from ..timemap import OsdSegment, Segment, TimeMap, osd_to_json
from .export import ExportJob

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="ecorun-overlay レイアウトエディタ API",
    description="レイアウトプリセット(presets/*.toml)の取得・保存・検証と、オーバーレイのプレビュー描画。"
                "プリセットの仕様は GET /api/schema と GET /api/widget-types を参照。",
    version="1",
)


# =====================================================================
#  キャッシュ
# =====================================================================
class LRU(OrderedDict):
    def __init__(self, size):
        super().__init__()
        self.size = size

    def get_or(self, key, make):
        if key in self:
            self.move_to_end(key)
            return self[key]
        v = self[key] = make()
        while len(self) > self.size:
            self.popitem(last=False)
        return v


_lock = threading.Lock()          # 描画は 1 本ずつ(Pillow のフォントキャッシュ等を共有するため)
_tel_cache = LRU(4)
_renderer_cache = LRU(8)
_frame_cache = LRU(48)
_video_cache = LRU(8)
_sync_cache = LRU(8)
_fonts: dict[str, Fonts] = {}


def _resolve_file(path: str, what: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = ROOT_DIR / p
    if not p.is_file():
        raise HTTPException(404, f"{what}が見つかりません: {path}")
    return p


def _video_source(videos: list[str]):
    """動画(複数・フォルダ可)→ (キャッシュキー, VideoSource)。各パスはリポジトリ基準の相対パスでもよい"""
    from ..video import VideoError, expand_videos, probe_videos
    paths = []
    for v in videos:
        p = Path(v).expanduser()
        if not p.is_absolute():
            p = ROOT_DIR / p
        if not p.exists():
            raise HTTPException(404, f"動画が見つかりません: {v}")
        paths.append(str(p))
    try:
        files = expand_videos(paths)
        key = tuple((f, Path(f).stat().st_mtime) for f in files)
        return key, _video_cache.get_or(key, lambda: probe_videos(files))
    except VideoError as e:
        raise HTTPException(422, str(e)) from None


def _telemetry(csv: str, rpm_on_threshold: float, heading_lookback_s: float) -> Telemetry:
    p = _resolve_file(csv, "CSV ")
    key = (str(p), p.stat().st_mtime, rpm_on_threshold, heading_lookback_s)
    try:
        return _tel_cache.get_or(key, lambda: load_log(str(p), rpm_on_threshold, heading_lookback_s))
    except LogFormatError as e:
        raise HTTPException(422, str(e)) from None


def _font(path: str) -> Fonts:
    if path not in _fonts:
        _fonts[path] = Fonts(path or None)
    return _fonts[path]


def _check_name(name: str):
    if not PRESET_NAME_RE.match(name):
        raise HTTPException(400, "プリセット名は英数字・_・- のみ使えます")


def _preset_path(name: str) -> Path:
    _check_name(name)
    return PRESET_DIR / f"{name}.toml"


def _parse(data: dict):
    try:
        return parse_preset(data)
    except PresetError as e:
        raise HTTPException(422, {"message": e.args[0], "errors": e.errors}) from None


# =====================================================================
#  プリセット
# =====================================================================
class PresetBody(BaseModel):
    preset: dict = Field(description="プリセットの内容(TOML と同じ構造の JSON)")
    base_mtime: float | None = Field(None, description="読み込んだ時点のファイル mtime。外部で変更されていたら 409 を返す")
    force: bool = Field(False, description="外部の変更を無視して上書きする")


class CopyBody(BaseModel):
    to: str = Field(description="コピー先のプリセット名")


@app.get("/api/presets", summary="プリセット一覧")
def get_presets():
    return list_presets()


@app.get("/api/presets/{name}", summary="プリセットを取得")
def get_preset(name: str):
    path = _preset_path(name)
    if not path.exists():
        raise HTTPException(404, f"プリセットが見つかりません: {name}")
    try:
        p = parse_preset(read_preset_file(path), str(path))
    except PresetError as e:
        raise HTTPException(422, {"message": e.args[0], "errors": e.errors}) from None
    return {"name": name, "path": str(path), "mtime": path.stat().st_mtime, "preset": preset_to_dict(p)}


@app.put("/api/presets/{name}", summary="プリセットを保存(コメントは保持)")
def put_preset(name: str, body: PresetBody):
    path = _preset_path(name)
    p = _parse(body.preset)
    if path.exists() and body.base_mtime is not None and not body.force \
            and abs(path.stat().st_mtime - body.base_mtime) > 1e-6:
        raise HTTPException(409, "ファイルが外部で変更されています")
    PRESET_DIR.mkdir(parents=True, exist_ok=True)
    save_preset(path, p)
    return {"name": name, "path": str(path), "mtime": path.stat().st_mtime}


@app.post("/api/presets/{name}/copy", summary="プリセットを複製")
def copy_preset(name: str, body: CopyBody):
    src, dst = _preset_path(name), _preset_path(body.to)
    if not src.exists():
        raise HTTPException(404, f"プリセットが見つかりません: {name}")
    if dst.exists():
        raise HTTPException(409, f"{body.to} は既にあります")
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return {"name": body.to, "path": str(dst), "mtime": dst.stat().st_mtime}


@app.delete("/api/presets/{name}", summary="プリセットを削除")
def delete_preset(name: str):
    path = _preset_path(name)
    if not path.exists():
        raise HTTPException(404, f"プリセットが見つかりません: {name}")
    path.unlink()
    return {"deleted": name}


@app.get("/api/schema", summary="プリセットの JSON Schema")
def get_schema():
    return preset_json_schema()


@app.get("/api/widget-types", summary="ウィジェット型とパラメータ(既定値・範囲・説明)")
def get_widget_types():
    return widget_types()


class ValidateBody(BaseModel):
    preset: dict


@app.post("/api/validate", summary="スキーマ検証と lint")
def validate(body: ValidateBody):
    try:
        p = parse_preset(body.preset)
    except PresetError as e:
        return {"valid": False, "errors": e.errors, "issues": []}
    return {"valid": True, "errors": [], "issues": lint_preset(p)}


# =====================================================================
#  ログ・動画・描画
# =====================================================================
@app.get("/api/logs", summary="CSV ログの候補(リポジトリ直下と sampledata/)")
def get_logs():
    files = []
    for d in (ROOT_DIR, ROOT_DIR / "sampledata"):
        if d.is_dir():
            files += [p for p in d.iterdir() if p.suffix.lower() == ".csv" and p.is_file()]
    return [str(p.relative_to(ROOT_DIR)) for p in sorted(set(files))]


@app.get("/api/log-info", summary="ログの長さ・開始時刻・列の有無")
def get_log_info(csv: str, rpm_on_threshold: float = 300, heading_lookback_s: float = 3.0):
    tel = _telemetry(csv, rpm_on_threshold, heading_lookback_s)
    return {
        "t_end": float(tel.t[-1]),
        "start": tel.t0.strftime("%Y-%m-%d %H:%M:%S.%f")[:-4],
        "has_fuel_p": tel.has("fuel_p"),
        "missing": [label for k, label in OPTIONAL_COLS.items() if not tel.has(k)],
        "laps": [{"lap": int(n), "t": float(t)} for n, t in tel.lap_starts],
        "on_spans": len(tel.on_spans),
    }


VIDEO_DESC = "動画のパス。複数指定(?video=a&video=b)やフォルダはファイル名順に連結して 1 本として扱う"


@app.get("/api/video-info", summary="動画の解像度・fps・長さ(連結後の合計)と各ファイルの開始秒")
def get_video_info(video: list[str] = Query(description=VIDEO_DESC)):
    _, src = _video_source(video)
    return {
        "width": src.width, "height": src.height, "fps": src.fps, "duration": src.duration,
        "files": [{"path": f, "start": t0, "duration": d}
                  for f, t0, d in zip(src.files, src.starts, src.durations)],
    }


@app.get("/api/frame", summary="動画の 1 フレーム(JPEG)。t は連結後の通し秒", response_class=Response)
def get_frame(video: list[str] = Query(description=VIDEO_DESC), t: float = Query(0.0, ge=0),
              width: int = 1920, height: int = 1080):
    from ..video import extract_frame
    vkey, src = _video_source(video)
    key = (vkey, round(t, 2), width, height)

    def make():
        img = extract_frame(src, t, (width, height))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    try:
        data = _frame_cache.get_or(key, make)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"フレームを取り出せません: {e}") from None
    return Response(data, media_type="image/jpeg")


@app.get("/api/video-file", summary="動画ファイルそのもの(Range 対応)。GUI の再生用。i は連結の何本目か",
         response_class=FileResponse)
def get_video_file(video: list[str] = Query(description=VIDEO_DESC), i: int = Query(0, ge=0)):
    _, src = _video_source(video)          # ffprobe で読めた動画ファイルだけを返す
    if i >= len(src.files):
        raise HTTPException(404, f"動画は {len(src.files)} 本です: i={i}")
    return FileResponse(src.files[i])


class AutoSyncBody(BaseModel):
    video: list[str] = Field(description=VIDEO_DESC)
    csv: str | None = Field(None, description="CSV のパス。指定するとログ秒(offset)と範囲の検査を返す")
    step: float = Field(10.0, gt=0, description="焼き込みを読む間隔 [秒](ecorun-sync --step と同じ)")


@app.post("/api/auto-sync", summary="焼き込み日時を OCR して区間ごとの同期を求める(長い動画では数十秒かかる)")
def auto_sync(body: AutoSyncBody):
    from ..ocrsync import SyncError, check_against_log, detect
    vkey, src = _video_source(body.video)
    try:
        segs = _sync_cache.get_or((vkey, body.step), lambda: detect(src, body.step))
    except SyncError as e:
        raise HTTPException(422, str(e)) from None
    out = {"segments": [{"v0": s.v0, "v1": s.v1, "wall": s.wall0.isoformat(timespec="milliseconds")} for s in segs],
           "warnings": []}
    if body.csv:
        tel = _telemetry(body.csv, 300, 3.0)          # t0 だけ使うので閾値は何でもよい
        t0 = tel.t0.to_pydatetime()
        try:
            out["warnings"] = check_against_log(segs, t0, float(tel.t[-1]))
        except SyncError as e:
            raise HTTPException(422, str(e)) from None
        for d, seg in zip(out["segments"], TimeMap.from_osd(segs, t0).segments):
            d["offset"] = seg.offset
    return out


class TimeMapSegment(BaseModel):
    v0: float = Field(description="区間の開始(動画の通し秒)")
    v1: float = Field(description="区間の終了")
    offset: float = Field(description="ログ秒 = 動画秒 + offset")


class RenderBody(BaseModel):
    preset: dict = Field(description="プリセット(未保存の編集中の内容でよい)")
    csv: str = Field(description="CSV のパス(リポジトリ基準の相対パスまたは絶対パス)")
    ts: float = Field(description="描画するログ秒(ログ開始からの秒)")
    sync: str | None = Field(None, description="出力区間の開始。HH:MM:SS[.f] またはログ秒(CLI の --sync と同じ)")
    offset: float = Field(0.0, description="CLI の --offset と同じ")
    start: float | None = Field(None, ge=0, description="書き出し開始位置(動画 0 秒目 = sync からの秒。CLI の --start と同じ)")
    end: float | None = Field(None, ge=0, description="書き出し終了位置(CLI の --end と同じ)。省略時は duration、それも無ければログ末尾まで")
    duration: float | None = Field(None, description="出力区間の長さ [秒](start からの長さ)。end があればそちらを優先")
    timemap: list[TimeMapSegment] | None = Field(None, description="区間ごとの同期(/api/auto-sync の結果)。あれば sync より優先")
    debug_boxes: bool = Field(False, description="ウィジェット枠と id を描く")
    font: str = Field("", description="フォントのパス(空ならプリセットの theme.font → 自動検出)")


@app.post("/api/render", summary="オーバーレイを 1 フレーム描画(透過 PNG)", response_class=Response)
def render(body: RenderBody):
    p = _parse(body.preset)
    tel = _telemetry(body.csv, p.data.rpm_on_threshold, p.data.heading_lookback_s)
    if body.timemap:
        tm = TimeMap([Segment(s.v0, s.v1, s.offset) for s in body.timemap]).shifted(body.offset)
    else:
        tm = TimeMap.constant(parse_sync(tel, body.sync) + body.offset)
    start = body.start or 0.0
    v_end = body.end if body.end is not None else (start + body.duration if body.duration is not None else None)
    if v_end is None and not tm.is_constant:
        v_end = tm.segments[-1].v1                    # 区間ごとの同期なら動画の末尾まで
    lo, hi = (tm.to_log(start), float(tel.t[-1])) if v_end is None else tm.log_range(start, v_end)
    t_start = max(0.0, lo)
    t_end = max(t_start, min(float(tel.t[-1]), hi))
    font = body.font or p.theme.font
    key = (id(tel), hashlib.sha1(json.dumps(preset_to_dict(p), sort_keys=True).encode()).hexdigest(),
           round(t_start, 3), round(t_end, 3), font, body.debug_boxes)
    with _lock:
        r = _renderer_cache.get_or(key, lambda: OverlayRenderer(tel, p, t_start, t_end, _font(font), body.debug_boxes))
        img = r.render(min(max(body.ts, float(tel.t[0])), float(tel.t[-1])))
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"X-Skipped-Widgets": ",".join(r.skipped)})


# =====================================================================
#  書き出し(ecorun-overlay を別プロセスで実行)
# =====================================================================
_job: ExportJob | None = None
atexit.register(lambda: _job and _job.cancel())       # GUI を閉じたら書き出しも止める


class OsdSegmentBody(BaseModel):
    v0: float = Field(description="区間の開始(動画の通し秒)")
    v1: float = Field(description="区間の終了")
    wall: str = Field(description="v0 での焼き込み日時(ISO 8601)")


class ExportBody(BaseModel):
    kind: Literal["overlay", "concat"] = Field(
        "overlay", description="overlay = オーバーレイを書き出す(動画があれば合成)/ concat = 動画を連結するだけ")
    preset: dict | None = Field(None, description="プリセット(未保存の編集中の内容でよい)。overlay で必須")
    csv: str | None = Field(None, description="CSV のパス。overlay で必須")
    video: list[str] = Field([], description=VIDEO_DESC)
    sync: str | None = Field(None, description="CLI の --sync と同じ。timemap があればそちらを使う")
    offset: float = Field(0.0, description="CLI の --offset と同じ")
    start: float | None = Field(None, ge=0, description="CLI の --start と同じ(秒)")
    end: float | None = Field(None, ge=0, description="CLI の --end と同じ(秒)")
    timemap: list[OsdSegmentBody] | None = Field(None, description="OCR の結果(/api/auto-sync の segments)。--sync FILE.json として渡す")
    output: str = Field(description="出力ファイルのパス(.mp4 / 動画なしなら .mov)")
    overwrite: bool = Field(False, description="出力が既にあれば上書きする")


@app.post("/api/export", summary="書き出しを始める(1 本ずつ)。進み具合は GET /api/export")
def start_export(body: ExportBody):
    global _job
    if _job and _job.state == "running":
        raise HTTPException(409, {"message": "書き出し中です。終わるか中止してから始めてください"})
    out = Path(body.output).expanduser()
    if not out.is_absolute():
        out = ROOT_DIR / out
    if not out.parent.is_dir():
        raise HTTPException(422, {"message": f"出力先のフォルダがありません: {out.parent}"})
    if out.suffix.lower() not in (".mp4", ".mov", ".mkv", ".m4v"):
        raise HTTPException(422, {"message": f"出力は .mp4 / .mov などの動画ファイルにしてください: {out.name}"})
    src = _video_source(body.video)[1] if body.video else None
    if src and any(Path(f).resolve() == out.resolve() for f in src.files):
        raise HTTPException(422, {"message": "入力の動画と同じファイルには書き出せません"})
    if src is None and (body.kind == "concat" or out.suffix.lower() != ".mov"):
        raise HTTPException(422, {"message": "動画なしで書き出せるのは透過オーバーレイ(.mov)だけです"
                                  if body.kind == "overlay" else "連結する動画を指定してください"})
    if out.exists() and not body.overwrite:
        raise HTTPException(409, {"message": f"{out} は既にあります", "exists": True})

    args, tmp = [], None
    if src:
        args += ["--video", *src.files]
    if body.kind == "concat":
        args += ["--concat-only"]
    else:
        if body.preset is None or not body.csv:
            raise HTTPException(422, {"message": "プリセットと CSV を指定してください"})
        p = _parse(body.preset)
        csv = _resolve_file(body.csv, "CSV ")
        tmp = Path(tempfile.mkdtemp(prefix="ecorun-export-"))
        preset_path = tmp / "preset.toml"
        preset_path.write_text(dump_preset(p), encoding="utf-8")
        args = [str(csv), "--preset", str(preset_path), *args]
        if body.timemap:
            sync_path = tmp / "sync.json"
            osd_to_json([OsdSegment(s.v0, s.v1, datetime.fromisoformat(s.wall)) for s in body.timemap],
                        sync_path, src.files if src else None)
            args += ["--sync", str(sync_path)]
        elif body.sync:
            args += ["--sync", body.sync]
        if body.offset:
            args += ["--offset", f"{body.offset:g}"]
        if body.start:
            args += ["--start", f"{body.start:g}"]
        if body.end is not None:
            args += ["--end", f"{body.end:g}"]
    args += ["-o", str(out)]
    _job = ExportJob(body.kind, args, out, tmp)
    return _job.status()


@app.get("/api/export", summary="書き出しの進み具合(state: idle / running / done / failed / cancelled)")
def get_export():
    return _job.status() if _job else {"state": "idle"}


@app.post("/api/export/cancel", summary="書き出しを中止する(途中までの出力は消す)")
def cancel_export():
    if _job:
        _job.cancel()
    return get_export()


# =====================================================================
#  外部変更の通知(SSE)
# =====================================================================
def _snapshot() -> dict[str, float]:
    return {p.stem: p.stat().st_mtime for p in PRESET_DIR.glob("*.toml")} if PRESET_DIR.is_dir() else {}


@app.get("/api/events", summary="presets/ の変更通知(Server-Sent Events)")
async def events():
    async def gen():
        last = _snapshot()
        yield f"event: hello\ndata: {json.dumps(last)}\n\n"
        while True:
            await asyncio.sleep(1.0)
            cur = _snapshot()
            if cur != last:
                changed = sorted({k for k in cur.keys() | last.keys() if cur.get(k) != last.get(k)})
                yield f"event: presets\ndata: {json.dumps({'changed': changed, 'mtimes': cur})}\n\n"
                last = cur
            else:
                yield ": ping\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# =====================================================================
#  静的ファイル
# =====================================================================
@app.exception_handler(HTTPException)
async def _http_error(_, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(detail, status_code=exc.status_code)


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """GUI の JS/CSS を更新したらすぐ反映されるよう、ブラウザにキャッシュさせない"""
    res = await call_next(request)
    if request.url.path.startswith("/static/"):
        res.headers["Cache-Control"] = "no-cache"
    return res


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main(argv=None):
    import uvicorn

    ap = argparse.ArgumentParser(prog="ecorun-gui", description="レイアウトを編集するローカル Web GUI")
    ap.add_argument("--host", default="127.0.0.1", help="既定 127.0.0.1(ローカルのみ)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    args = ap.parse_args(argv)
    url = f"http://{args.host}:{args.port}/"
    print(f"ecorun-gui: {url}  (API: {url}docs)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
