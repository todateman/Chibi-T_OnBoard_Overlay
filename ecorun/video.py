"""ffmpeg / ffprobe まわり

ドラレコの動画は 5 分ごとに分割されるので、複数ファイルを VideoSource にまとめて
1 本の動画として扱う。連結は中間ファイルを作らず ffmpeg の concat demuxer で直接読む。
"""
from __future__ import annotations

import bisect
import functools
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .parallel import render_frames
from .render import OverlayRenderer
from .timemap import TimeMap

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".ts"}
ENCODERS = ("auto", "x264", "videotoolbox", "nvenc")
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
# h264_videotoolbox の固定品質(1〜100、大きいほど高画質)。65 で元動画(F9、約 4.4 Mbps)に対し約 8 Mbps
VT_QUALITY = 65
# --x264-preset(x264 の 9 段階)→ h264_nvenc の preset(p1 最速〜p7 最高画質)への対応
NVENC_PRESETS = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p2",
    "fast": "p3", "medium": "p4", "slow": "p5", "slower": "p6", "veryslow": "p7",
}

# 音声は AAC で再エンコードする。F9 の .TS は壊れた ADTS フレームが混ざっていて、
# -c:a copy だと mp4 への mux(aac_adtstoasc)で ffmpeg が途中で止まる。デコーダは壊れたフレームを読み飛ばせる
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k"]


class VideoError(Exception):
    """動画の指定・読込の誤り(メッセージはそのまま利用者に見せる)"""


def expand_videos(paths: Sequence[str]) -> list[str]:
    """ファイルはそのまま、フォルダは直下の動画をファイル名順に展開する。
    フォルダからは書き出した結果(GUI の既定の出力名「名前_overlay.mp4」)を除く"""
    files: list[str] = []
    for s in paths:
        p = Path(s).expanduser()
        if p.is_dir():
            found = sorted((c for c in p.iterdir() if c.is_file() and c.suffix.lower() in VIDEO_EXTS
                            and not c.name.startswith(".") and not c.stem.endswith("_overlay")),
                           key=lambda c: c.name)
            if not found:
                raise VideoError(f"フォルダに動画がありません: {s}")
            files += [str(c) for c in found]
        elif p.is_file():
            if p.suffix.lower() not in VIDEO_EXTS:
                hint = "(CSV は --video より前に書いてください)" if p.suffix.lower() == ".csv" else ""
                raise VideoError(f"動画ではありません: {s}{hint}")
            files.append(str(p))
        else:
            raise VideoError(f"動画が見つかりません: {s}")
    if not files:
        raise VideoError("動画が指定されていません")
    return files


@dataclass
class VideoSource:
    """連結して 1 本として扱う動画。時刻は連結後の通し秒"""
    files: list[str]
    durations: list[float]
    width: int
    height: int
    fps: float

    @property
    def duration(self) -> float:
        return sum(self.durations)

    @property
    def starts(self) -> list[float]:
        out, acc = [], 0.0
        for d in self.durations:
            out.append(acc)
            acc += d
        return out

    def locate(self, t: float) -> tuple[int, float]:
        """通し秒 → (ファイル番号, そのファイル内の秒)。境界ちょうどは次のファイル、範囲外は端にクランプ"""
        starts = self.starts
        i = max(0, min(bisect.bisect_right(starts, t) - 1, len(self.files) - 1))
        return i, min(max(0.0, t - starts[i]), self.durations[i])


def probe_videos(paths: Sequence[str]) -> VideoSource:
    """動画(複数可・フォルダ可)を調べて VideoSource にする。解像度・fps が揃っていないものは連結できない"""
    files = expand_videos(paths)
    durations, size, fps = [], None, None
    for f in files:
        try:
            w, h, r, d = probe(f)
        except (subprocess.CalledProcessError, KeyError, ValueError) as e:
            raise VideoError(f"動画を読めません: {f}: {e}") from None
        if size is None:
            size, fps = (w, h), r
        elif (w, h) != size or abs(r - fps) > 0.01:
            raise VideoError(f"解像度・フレームレートが先頭の動画と違うので連結できません: {f} "
                             f"({w}×{h} {r:.2f} fps / 先頭 {size[0]}×{size[1]} {fps:.2f} fps)")
        durations.append(d)
    return VideoSource(files, durations, size[0], size[1], fps)


def _concat_quote(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


@contextmanager
def input_args(src: VideoSource) -> Iterator[list[str]]:
    """ffmpeg の入力引数。1 本ならそのまま、複数なら concat demuxer の一覧ファイルを一時的に書く。
    一覧に各ファイルの duration を書いて、ffmpeg 側のタイムラインを locate() と一致させる"""
    if len(src.files) == 1:
        yield ["-i", src.files[0]]
        return
    fd, list_path = tempfile.mkstemp(prefix="ecorun_concat_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            for path, d in zip(src.files, src.durations):
                f.write(f"file {_concat_quote(os.path.abspath(path))}\nduration {d:.6f}\n")
        yield ["-f", "concat", "-safe", "0", "-i", list_path]
    finally:
        os.unlink(list_path)


def concat_only(src: VideoSource, out_path: str):
    """オーバーレイなしで連結だけする(再エンコードなし)"""
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg が見つかりません")
    with input_args(src) as inp:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inp, "-map", "0:v", "-map", "0:a?",
                        "-c:v", "copy", *AUDIO_ARGS, "-movflags", "+faststart", out_path], check=True)


def probe(video: str):
    """→ (width, height, fps, duration)"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "default=nw=1", video],
        capture_output=True, text=True, check=True).stdout
    kv = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    num, den = kv["r_frame_rate"].split("/")
    return int(kv["width"]), int(kv["height"]), float(num) / float(den), float(kv["duration"])


def extract_frame(video: str | VideoSource, t: float, size: tuple[int, int] | None = None) -> Image.Image:
    """動画の t 秒目の 1 フレームを取り出す。VideoSource なら通し秒から該当ファイルを選ぶ"""
    if isinstance(video, VideoSource):
        i, t = video.locate(t)
        video, t = video.files[i], min(t, video.durations[i] - 1.5 / video.fps)  # 末尾ちょうどはシークで取れないことがある
    vf = ["-vf", f"scale={size[0]}:{size[1]}"] if size else []
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", video, "-frames:v", "1", *vf,
         "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, check=True).stdout
    if not out:
        raise ValueError(f"{t:.2f} 秒のフレームを取り出せません(動画の長さを超えている可能性)")
    return Image.open(io.BytesIO(out)).convert("RGB")


@functools.cache
def _ffmpeg_encoders() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


def resolve_encoder(name: str = "auto") -> str:
    """--encoder → "x264" / "videotoolbox" / "nvenc"。auto は macOS で h264_videotoolbox が使えればそちら、
    それ以外で h264_nvenc(NVIDIA GPU)が使えればそちら、どちらも無ければ x264
    (libx264 medium は 1080p で約 70 fps が上限で、描画と CPU を取り合う。videotoolbox・nvenc は約 200〜240 fps)"""
    if name not in ENCODERS:
        raise ValueError(f"未知のエンコーダ: {name}")
    if name == "auto":
        encs = _ffmpeg_encoders()
        if sys.platform == "darwin" and "h264_videotoolbox" in encs:
            return "videotoolbox"
        if sys.platform != "darwin" and "h264_nvenc" in encs:
            return "nvenc"
        return "x264"
    return name


def h264_args(encoder: str, crf: int = 18, x264_preset: str = "medium") -> list[str]:
    if encoder == "videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-q:v", str(VT_QUALITY)]
    if encoder == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", NVENC_PRESETS.get(x264_preset, "p4"),
                "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", x264_preset, "-crf", str(crf)]


def encode(r: OverlayRenderer, out_path: str, t_sync: float | TimeMap, duration: float, fps: float,
           video: VideoSource | None = None, crf: int = 18, start: float = 0.0,
           encoder: str = "auto", x264_preset: str = "medium", jobs: int = 1):
    """RGBA の raw フレームを stdin パイプで ffmpeg に渡す。
    video あり → overlay フィルタで合成して H.264 / なし → ProRes 4444(アルファ付き .mov)。
    t_sync は動画 0 秒目のログ秒(従来の --sync)か、区間ごとの対応 TimeMap。
    start は書き出し開始位置(動画の通し秒)。出力 k 枚目のログ秒は tm.to_log(start + k / fps)。
    encoder は H.264 のエンコーダ(ENCODERS)、jobs は描画のプロセス数(1 なら並列にしない)"""
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg が見つかりません")
    if video is None and not out_path.lower().endswith(".mov"):
        sys.exit("--video なしの場合、出力は .mov(透過 ProRes 4444)か .png(--preview)にしてください")
    with input_args(video) if video else nullcontext([]) as video_in:
        tm = t_sync if isinstance(t_sync, TimeMap) else TimeMap.constant(t_sync)
        venc = h264_args(resolve_encoder(encoder), crf, x264_preset) if video else []
        _encode(r, out_path, tm, duration, fps, video, video_in, venc, start, jobs)


def _encode(r: OverlayRenderer, out_path: str, tm: TimeMap, duration: float, fps: float,
            video: VideoSource | None, video_in: list[str], venc: list[str], start: float, jobs: int):
    tel = r.tel
    n_out = n_frames = int(duration * fps)
    if video:
        # 音声があると AAC のプライミング分だけ映像の pts が後ろにずれ、shortest で最後の 1 枚が落ちる。
        # 1 枚余分に送っておき、出力の枚数は -frames:v で揃える
        n_frames += 1
    raw_in = ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{r.W}x{r.H}", "-r", f"{fps}", "-i", "-"]
    if video:
        vw, vh = video.width, video.height
        # 入力シーク(-ss を -i の前)。再エンコードするのでフレーム単位で正確。
        # 映像は -frames:v で枚数を揃え、音声は -t で区間に切る(映像を切らないよう半フレーム長め)
        seek = ["-ss", f"{start:.3f}"] if start > 0 else []
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *seek, *video_in, *raw_in,
               "-filter_complex", f"[1:v]scale={vw}:{vh}:flags=lanczos[ov];[0:v][ov]overlay=0:0:shortest=1[v]",
               "-map", "[v]", "-map", "0:a?", *venc, "-pix_fmt", "yuv420p", *AUDIO_ARGS,
               "-frames:v", str(n_out), "-t", f"{duration + 0.5 / fps:.3f}", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *raw_in,
               "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", out_path]

    t0, t1 = float(tel.t[0]), float(tel.t[-1])
    ts_list = [min(max(tm.to_log(start + k / fps), t0), t1) for k in range(n_frames)]
    enc = venc[1] if venc else "prores_ks"
    print(f"{enc} / 描画 {jobs} プロセス", flush=True)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t_wall = t_print = time.time()
    k = 0
    frames = render_frames(r, ts_list, jobs)
    try:
        for k, frame in enumerate(frames):
            proc.stdin.write(frame)
            if k == 0 or time.time() - t_print >= 1.0:      # 進捗は 1 秒ごと(GUI はこれを読んで進捗バーを出す)
                t_print = time.time()
                el = t_print - t_wall
                eta = el / max(1, k) * (n_frames - k)
                print(f"\r{k}/{n_frames} frames  ETA {eta:5.0f}s", end="", flush=True)
    except BrokenPipeError:
        pass  # 動画が先に終わった
    finally:
        frames.close()                                   # 描画プロセスを止める
        if proc.stdin:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        proc.wait()
    if proc.returncode != 0:
        sys.exit(f"\nffmpeg が失敗しました(終了コード {proc.returncode}、{k + 1}/{n_frames} 枚目まで)")
    el = time.time() - t_wall
    print(f"\n{k + 1} frames in {el:.1f}s ({(k + 1) / max(el, 1e-6):.1f} fps)")
