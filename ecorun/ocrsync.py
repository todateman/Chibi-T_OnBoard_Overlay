"""ecorun-sync: 車載動画の焼き込み日時を OCR して、動画とログを自動で同期する

VSYSTO F9 は GPS と同期した日時を右下に焼き込む(例 `2023/06/10 10:37:31  Vsysto F9  35 Kmh`)。
動画の各位置でこれを読み、時刻の飛び(編集のつなぎ目・録画の欠け)ごとに区間へ分けて OsdSegment の列にする。
OCR は Tesseract(外部コマンド)。ffmpeg と同じく subprocess で呼ぶ。

使い方:
  ecorun-sync F9/                              # 区間の表を出す
  ecorun-sync F9/ --csv LOG0650.CSV -o sync.json
  ecorun-overlay LOG0650.CSV --video F9/ --sync sync.json -o out.mp4   # 保存した結果で書き出す
  ecorun-overlay LOG0650.CSV --video F9/ --sync auto -o out.mp4        # その場で OCR して書き出す
"""
from __future__ import annotations

import argparse
import io
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from statistics import median

import numpy as np
from PIL import Image

from .timemap import OsdSegment
from .video import VideoError, VideoSource, probe_videos

# 日時部分の枠(canvas 1920×1080 基準。動画の解像度へスケールして切り出す)
OSD_TIME_BOX = (1145, 992, 280, 46)
CANVAS = (1920, 1080)
WHITE_THRESHOLD = 185       # 焼き込みの白文字とみなす輝度
JUMP_TOL = 1.5              # 同じ区間とみなす「焼き込み時刻 − 動画秒」の揺れ [秒]。表示が整数秒なので 1 秒は揺れる
REF = datetime(2000, 1, 1)  # 時刻を秒に直す基準
WHITELIST = "0123456789/: "
RE_OSD = re.compile(r"(20\d\d)/?(\d\d)/?(\d\d)(\d\d):?(\d\d):?(\d\d)")


class SyncError(Exception):
    """自動同期の失敗(メッセージはそのまま利用者に見せる)"""


def require_tesseract():
    if not shutil.which("tesseract"):
        raise SyncError("tesseract が見つかりません。自動同期には Tesseract が必要です(macOS: brew install tesseract)")


def parse_osd(text: str) -> datetime | None:
    """OCR 結果の文字列 → 日時。空白の混入や区切りの欠けは許す。値域外は None"""
    m = RE_OSD.search(re.sub(r"\s", "", text))
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def scaled_box(box: Sequence[int], width: int, height: int) -> tuple[int, int, int, int]:
    """canvas 基準の枠を動画の解像度へ。はみ出しは切り詰める"""
    sx, sy = width / CANVAS[0], height / CANVAS[1]
    x, y = max(0, round(box[0] * sx)), max(0, round(box[1] * sy))
    w, h = min(width - x, round(box[2] * sx)), min(height - y, round(box[3] * sy))
    return x, y, w, h


def grab(src: VideoSource, v: float, box: Sequence[int] = OSD_TIME_BOX) -> np.ndarray | None:
    """動画の通し秒 v のフレームから枠を切り出す(グレースケール)"""
    i, t = src.locate(v)
    t = min(t, src.durations[i] - 1.5 / src.fps)  # 末尾ちょうどはシークで取れないことがある
    x, y, w, h = scaled_box(box, src.width, src.height)
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", src.files[i], "-frames:v", "1",
         "-vf", f"crop={w}:{h}:{x}:{y},format=gray", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    if len(out) != w * h:
        return None
    return np.frombuffer(out, np.uint8).reshape(h, w)


def prepare(gray: np.ndarray) -> Image.Image:
    """白文字だけを残して黒文字・白背景にし、Tesseract が読みやすい大きさにする"""
    img = Image.fromarray(gray)
    scale = max(1.0, 64 / gray.shape[0])                  # 文字の高さを 30 px 前後に
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    a = np.asarray(img)
    out = Image.fromarray(np.where(a >= WHITE_THRESHOLD, 0, 255).astype(np.uint8))
    pad = Image.new("L", (out.width + 40, out.height + 40), 255)
    pad.paste(out, (20, 20))
    return pad


def ocr(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "7", "-c", f"tessedit_char_whitelist={WHITELIST}"],
        input=buf.getvalue(), capture_output=True).stdout.decode(errors="replace")


def read_osd(src: VideoSource, v: float, box: Sequence[int] = OSD_TIME_BOX) -> datetime | None:
    gray = grab(src, v, box)
    return None if gray is None else parse_osd(ocr(prepare(gray)))


def _sec(w: datetime) -> float:
    return (w - REF).total_seconds()


def detect(src: VideoSource, step: float = 10.0, box: Sequence[int] = OSD_TIME_BOX,
           progress: Callable[[str], None] | None = None) -> list[OsdSegment]:
    """動画全体の焼き込み時刻を読み、時刻の飛びごとの区間に分ける"""
    require_tesseract()
    say = progress or (lambda s: None)
    dur = src.duration
    read = lambda v: read_osd(src, v, box)  # noqa: E731

    # 1) 粗く読む: step 秒ごと + 各ファイルの先頭・末尾付近(境目の録画欠けを拾う)
    vs = set(np.arange(0.2, max(0.3, dur - 0.2), step).round(3))
    for s, d in zip(src.starts, src.durations):
        vs |= {round(s + 0.2, 3), round(s + d - 0.3, 3)}
    vs = sorted(v for v in vs if 0 <= v < dur)
    say(f"焼き込み時刻を読み取り中: {len(vs)} 点")
    ex = ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4))
    try:
        return _detect(src, read, vs, ex, say)
    finally:
        ex.shutdown()


def _detect(src: VideoSource, read, vs: list[float], ex: ThreadPoolExecutor, say) -> list[OsdSegment]:
    eps = 1.0 / src.fps
    dur = src.duration
    walls = list(ex.map(read, vs))
    samples = [(v, _sec(w) - v) for v, w in zip(vs, walls) if w is not None]
    if len(samples) < max(2, len(vs) * 0.2):
        raise SyncError(f"焼き込み時刻を読めません({len(samples)}/{len(vs)} 点)。"
                        "枠の位置(--osd-box)と、カメラの日時表示が有効かを確認してください")
    say(f"  読めた点: {len(samples)}/{len(vs)}")

    # 2) 差(焼き込み時刻 − 動画秒)が揃う連続を区間にまとめる。
    #    1 点だけ外れたものは前後 0.5 秒でも読み、両方が同じ差でなければ誤読として捨てる
    #    (路面の白い粒が数字に重なると誤読するが、路面は動くので続かない)
    def confirmed(v: float, d: float) -> bool:
        for u in (max(0.0, v - 0.5), min(dur - eps, v + 0.5)):
            w = read(u)
            if w is None or abs(_sec(w) - u - d) > JUMP_TOL:
                return False
        return True

    runs = _group(samples)
    if len(runs) > 1:
        runs = _drop_sandwiched(_group([p for r in runs if len(r) > 1 or confirmed(*r[0]) for p in r]))

    # 3) 区間の境目を二分探索で 1 フレームまで詰める。分割ファイルの境目の近くならそこに合わせる
    def cut(ab):
        a, b = ab
        c = _find_cut(read, a[-1][0], b[0][0], _mid(a), _mid(b), eps)
        near = [s for s in src.starts[1:] if abs(s - c) < 0.25]
        return near[0] if near else c

    cuts = list(ex.map(cut, zip(runs, runs[1:])))
    say(f"  区間: {len(runs)}")

    # 4) 秒未満の補正: 秒の表示が変わるフレームを探す
    bounds = [0.0, *cuts, dur]
    ftime = lambda v: frame_time(src, v)  # noqa: E731
    offs = ex.map(lambda k: _refine(read, ftime, runs[k], bounds[k], bounds[k + 1], eps), range(len(runs)))
    parts = [(bounds[k], bounds[k + 1], off) for k, off in enumerate(offs)]
    return [OsdSegment(v0, v1, REF + timedelta(seconds=off + v0)) for v0, v1, off in _merge(parts)]


def _merge(parts: list[tuple[float, float, float]], tol: float = 0.5, min_len: float = 1.0):
    """offset がほぼ同じ隣り合う区間と、min_len 秒未満の区間(誤読の名残)を前の区間に統合する"""
    out: list[tuple[float, float, float]] = []
    for v0, v1, off in parts:
        if out and (abs(off - out[-1][2]) < tol or v1 - v0 < min_len):
            out[-1] = (out[-1][0], v1, out[-1][2])
        elif out and out[-1][1] - out[-1][0] < min_len:      # 先頭が短すぎた
            out[-1] = (out[-1][0], v1, off)
        else:
            out.append((v0, v1, off))
    return out


def _spread(xs: list[float], n: int) -> list[float]:
    """xs から端を含めて均等に n 個(重複なし)"""
    if len(xs) <= n:
        return xs
    return list(dict.fromkeys(xs[round(i * (len(xs) - 1) / (n - 1))] for i in range(n)))


def _mid(run: list[tuple[float, float]]) -> float:
    return median(d for _, d in run)


def _group(samples: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    runs: list[list[tuple[float, float]]] = []
    for v, d in sorted(samples):
        if runs and abs(d - _mid(runs[-1])) <= JUMP_TOL:
            runs[-1].append((v, d))
        else:
            runs.append([(v, d)])
    return runs


def _drop_sandwiched(runs: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """前後の区間の差が揃っているのに、間だけ差が違う区間を誤読として捨てる。
    録画の欠けなら差は増える一方で元の差には戻らないので、間の区間は同じ桁を読み続けた誤読
    (2026/10/25 茂木の F9 で「12:55」を 25 秒間「12:53」と読んだ)"""
    while True:
        k = next((k for k in range(1, len(runs) - 1)
                  if abs(_mid(runs[k - 1]) - _mid(runs[k + 1])) <= JUMP_TOL), None)
        if k is None:
            return runs
        runs = _group([p for i, r in enumerate(runs) if i != k for p in r])


def _find_cut(read, lo: float, hi: float, d_lo: float, d_hi: float, eps: float) -> float:
    """lo は前の区間、hi は後ろの区間に属する。後ろの区間の最初のフレームの位置を返す"""
    # 飛びが大きければ、数字 1 つの誤読くらいでは近いほうの区間に振り分けられる
    tol = max(JUMP_TOL, abs(d_hi - d_lo) / 4)

    def side(v: float) -> int | None:
        """前の区間なら 0、後ろなら 1。読めない・どちらの差とも合わない(誤読)なら None"""
        w = read(v)
        if w is None:
            return None
        d = _sec(w) - v
        e0, e1 = abs(d - d_lo), abs(d - d_hi)
        if min(e0, e1) > tol:
            return None
        return 0 if e0 <= e1 else 1

    while hi - lo > eps:
        k = None
        for frac in (0.5, 0.4, 0.6, 0.3, 0.7):   # 読めない・誤読なら位置を変えて読み直す
            mid = lo + (hi - lo) * frac
            k = side(mid)
            if k is not None:
                break
        if k is None:
            break
        if k == 0:
            lo = mid
        else:
            hi = mid
    return hi


def frame_time(src: VideoSource, v: float) -> float:
    """grab(src, v) が返すフレームの時刻。ffmpeg の -ss は v 以降の最初のフレームを返す"""
    i, t = src.locate(v)
    return src.starts[i] + math.ceil(t * src.fps - 1e-6) / src.fps


def _refine(read, ftime, run: list[tuple[float, float]], v0: float, v1: float, eps: float, n: int = 3) -> float:
    """区間の offset(焼き込み秒 − 動画秒)を秒未満まで求める。
    秒の表示が S → S+1 に変わった最初のフレームの時刻 ft では、焼き込み時刻は [S+1, S+1+1 フレーム) にある"""
    cands = [v for v, _ in run if v0 <= v and v + 1.0 + eps < v1]
    offs = []
    for v in _spread(cands, n + 2):          # にじみで失敗する点があるので多めに試し、n 個取れたら終わる
        if len(offs) >= n:
            break
        w0, w1 = read(v), read(v + 1.0 + eps)
        if w0 is None or w1 is None or not 1 <= _sec(w1) - _sec(w0) <= 2:
            continue
        s, lo, hi = _sec(w0), v, v + 1.0 + eps
        ok = True
        while hi - lo > eps * 0.9:
            mid = (lo + hi) / 2
            w = read(mid)
            if w is None or _sec(w) not in (s, s + 1):   # 切り替わりのにじみで誤読したフレーム
                ok = False
                break
            if _sec(w) == s:
                lo = mid
            else:
                hi = mid
        if ok:
            offs.append(s + 1 + eps / 2 - ftime(hi))
    if offs:
        return median(offs)
    return _mid(run) + 0.5          # 補正できなければ整数秒の中央


# ---- ログとの突き合わせ ----
def check_against_log(segs: list[OsdSegment], t0: datetime, t_end: float) -> list[str]:
    """ログの範囲(t0 〜 t0 + t_end 秒)と重ならない区間を警告する。どの区間も重ならなければ SyncError"""
    log_end = t0 + timedelta(seconds=t_end)
    warns, hit = [], False
    for k, s in enumerate(segs, 1):
        a, b = s.wall0, s.wall_at(s.v1)
        if b < t0 or a > log_end:
            warns.append(f"区間 {k} の焼き込み時刻 {a:%Y-%m-%d %H:%M:%S}〜{b:%H:%M:%S} はログの範囲外")
        else:
            hit = True
    if not hit:
        diff = (segs[0].wall0 - t0).total_seconds() / 3600
        hint = (f"。差がほぼ {round(diff)} 時間なので、カメラの時計のタイムゾーン設定を確認してください"
                if abs(diff) >= 0.9 and abs(diff - round(diff)) < 0.05 else "")
        raise SyncError(f"焼き込み時刻がログ({t0:%Y-%m-%d %H:%M:%S}〜{log_end:%H:%M:%S})と重なりません{hint}")
    return warns


def describe(segs: list[OsdSegment], t0: datetime | None = None) -> str:
    """区間の表(人が読む用)"""
    rows = ["区間  動画位置 [s]            焼き込み時刻(区間の先頭)" + ("   ログ秒" if t0 else "")]
    for k, s in enumerate(segs, 1):
        log = f"   {(s.wall0 - t0).total_seconds():9.2f}" if t0 else ""
        rows.append(f"{k:4d}  {s.v0:9.2f} – {s.v1:9.2f}    {s.wall0:%Y-%m-%d %H:%M:%S.%f}"[:-4] + log)
    return "\n".join(rows)


def detect_cli(src: VideoSource, step: float, box: Sequence[int]) -> list[OsdSegment]:
    """CLI 用: 進捗を stderr に出して detect"""
    return detect(src, step, box, progress=lambda s: print(s, file=sys.stderr))


def parse_box(s: str) -> tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(v) for v in s.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"X,Y,W,H(canvas 1920×1080 基準の整数)で指定してください: {s!r}") from None
    return x, y, w, h


def add_ocr_args(ap: argparse.ArgumentParser, step_flag: str):
    ap.add_argument(step_flag, type=float, default=10.0, metavar="SEC", help="焼き込みを読む間隔 [秒](既定 10)")
    ap.add_argument("--osd-box", type=parse_box, default=OSD_TIME_BOX, metavar="X,Y,W,H",
                    help="焼き込み日時の枠(canvas 1920×1080 基準。既定 %s)" % ",".join(map(str, OSD_TIME_BOX)))


def main(argv=None):
    from .telemetry import LogFormatError, load_log
    from .timemap import osd_to_json

    ap = argparse.ArgumentParser(prog="ecorun-sync", description="車載動画の焼き込み日時を OCR してログと同期する")
    ap.add_argument("video", nargs="+", help="車載動画(複数ファイル・フォルダはファイル名順に連結)")
    ap.add_argument("--csv", help="ロガーの CSV。指定するとログ秒と範囲の検査を出す")
    ap.add_argument("-o", "--output", help="結果を JSON に保存(ecorun-overlay --sync FILE.json で使う)")
    add_ocr_args(ap, "--step")
    ap.add_argument("--save-crop", metavar="PNG", help="先頭付近の切り出し画像と 2 値化後の画像を保存(枠の確認用)")
    args = ap.parse_args(argv)
    try:
        src = probe_videos(args.video)
        if args.save_crop:
            gray = grab(src, min(1.0, src.duration / 2), args.osd_box)
            if gray is None:
                sys.exit("フレームを取り出せません")
            a, b = Image.fromarray(gray), prepare(gray)
            both = Image.new("L", (max(a.width, b.width), a.height + b.height + 8), 128)
            both.paste(a, (0, 0))
            both.paste(b, (0, a.height + 8))
            both.save(args.save_crop)
            print(f"切り出し → {args.save_crop}  OCR: {ocr(b).strip()!r}")
        segs = detect_cli(src, args.step, args.osd_box)
        tel = load_log(args.csv) if args.csv else None
        t0 = tel.t0.to_pydatetime() if tel else None
        print(describe(segs, t0))
        if tel:
            for w in check_against_log(segs, t0, float(tel.t[-1])):
                print("警告: " + w, file=sys.stderr)
        if len(segs) == 1:
            print(f"--sync {segs[0].wall_at(0):%H:%M:%S.%f}"[:-4] + " と同等")
        if args.output:
            osd_to_json(segs, args.output, src.files)
            print(f"→ {args.output}")
    except (VideoError, SyncError, LogFormatError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
