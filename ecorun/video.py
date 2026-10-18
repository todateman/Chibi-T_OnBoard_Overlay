"""ffmpeg / ffprobe まわり"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import time

from PIL import Image

from .render import OverlayRenderer


def probe(video: str):
    """→ (width, height, fps, duration)"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "default=nw=1", video],
        capture_output=True, text=True, check=True).stdout
    kv = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    num, den = kv["r_frame_rate"].split("/")
    return int(kv["width"]), int(kv["height"]), float(num) / float(den), float(kv["duration"])


def extract_frame(video: str, t: float, size: tuple[int, int] | None = None) -> Image.Image:
    """動画の t 秒目の 1 フレームを取り出す"""
    vf = ["-vf", f"scale={size[0]}:{size[1]}"] if size else []
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", video, "-frames:v", "1", *vf,
         "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, check=True).stdout
    if not out:
        raise ValueError(f"{t:.2f} 秒のフレームを取り出せません(動画の長さを超えている可能性)")
    return Image.open(io.BytesIO(out)).convert("RGB")


def encode(r: OverlayRenderer, out_path: str, t_sync: float, duration: float, fps: float,
           video: str | None = None, vsize: tuple[int, int] | None = None, crf: int = 18):
    """RGBA の raw フレームを stdin パイプで ffmpeg に渡す。
    video あり → overlay フィルタで合成して H.264 / なし → ProRes 4444(アルファ付き .mov)"""
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg が見つかりません")
    tel = r.tel
    n_frames = int(duration * fps)
    raw_in = ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{r.W}x{r.H}", "-r", f"{fps}", "-i", "-"]
    if video:
        vw, vh = vsize
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video, *raw_in,
               "-filter_complex", f"[1:v]scale={vw}:{vh}:flags=lanczos[ov];[0:v][ov]overlay=0:0:shortest=1[v]",
               "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
               "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "copy", out_path]
    elif out_path.lower().endswith(".mov"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *raw_in,
               "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", out_path]
    else:
        sys.exit("--video なしの場合、出力は .mov(透過 ProRes 4444)か .png(--preview)にしてください")

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t_wall = time.time()
    k = 0
    try:
        for k in range(n_frames):
            ts = t_sync + k / fps
            frame = r.render(min(max(ts, tel.t[0]), tel.t[-1]))
            proc.stdin.write(frame.tobytes())
            if k % int(fps * 10) == 0:
                el = time.time() - t_wall
                eta = el / max(1, k) * (n_frames - k)
                print(f"\r{k}/{n_frames} frames  ETA {eta:5.0f}s", end="", flush=True)
    except BrokenPipeError:
        pass  # 動画が先に終わった
    finally:
        if proc.stdin:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        proc.wait()
    el = time.time() - t_wall
    print(f"\n{k + 1} frames in {el:.1f}s ({(k + 1) / max(el, 1e-6):.1f} fps)")
