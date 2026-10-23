"""ecorun-overlay: CSV を読み、プリセットのレイアウトでオーバーレイを描いて出力する

使い方:
  # 1) 静止画プレビュー(ログ開始から 150 秒の時点)
  ecorun-overlay LOG0650.CSV --preview 150 -o preview.png

  # 2) 車載動画に直接合成(動画の先頭 = ログ時刻 15:10:30)
  ecorun-overlay LOG0650.CSV --preset f9_mask --video onboard.mp4 --sync 15:10:30 -o out.mp4

  # 3) 5 分ごとに分割された動画を連結して合成(複数ファイル・フォルダ可。ファイル名順に連結)
  ecorun-overlay LOG0650.CSV --preset f9_mask --video F9/ --sync 15:10:30 -o out.mp4

  # 4) 動画の 1:35〜21:40 だけを書き出す(位置は連結後の動画の再生位置)
  ecorun-overlay LOG0650.CSV --preset f9_mask --video F9/ --sync 15:10:30 --start 1:35 --end 21:40 -o out.mp4

  # 5) 透過オーバーレイだけを書き出し(DaVinci Resolve / Premiere で重ねる)
  ecorun-overlay LOG0650.CSV --sync 15:10:30 --duration 1200 -o overlay.mov

  # 6) オーバーレイなしで連結だけ(再エンコードなし)
  ecorun-overlay --concat-only --video F9/*.MP4 -o joined.mp4

  # 7) 焼き込み日時を OCR して自動で同期(時刻の飛びごとに区間を分ける。要 Tesseract)
  ecorun-overlay LOG0650.CSV --preset f9_mask --video F9/ --sync auto -o out.mp4
  ecorun-overlay LOG0650.CSV --preset f9_mask --video F9/ --sync sync.json -o out.mp4   # ecorun-sync -o の結果
"""
from __future__ import annotations

import argparse
import sys

from .config import DEFAULT_PRESET, PresetError, load_preset
from .draw import Fonts
from .ocrsync import SyncError, add_ocr_args, check_against_log, describe, detect_cli
from .parallel import default_jobs
from .render import OverlayRenderer, preview_image
from .telemetry import LogFormatError, Telemetry, load_log, parse_sync
from .timemap import TimeMap, osd_from_json
from .video import ENCODERS, X264_PRESETS, VideoError, VideoSource, probe_videos

FPS_DEFAULT = 30


def parse_position(s: str) -> float:
    """動画の再生位置: 秒(95.5)または [H:]MM:SS[.f](1:35、0:21:40.5) → 秒"""
    try:
        parts = [float(x) for x in s.strip().split(":")]
    except ValueError:
        parts = []
    if not 1 <= len(parts) <= 3 or any(x < 0 for x in parts) or any(x >= 60 for x in parts[1:]):
        raise argparse.ArgumentTypeError(f"再生位置は 秒 か [H:]MM:SS[.f] で指定してください: {s!r}")
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return sec


def resolve_range(start: float | None, end: float | None, duration: float | None,
                  total: float, clamp: bool) -> tuple[float, float]:
    """--start / --end / --duration → (開始位置, 長さ)。位置は動画 0 秒目(= --sync)からの秒。
    total は省略時の終端(動画長 or ログ末尾)。clamp なら total を超える終端を切り詰める。誤りは ValueError"""
    start = start or 0.0
    if end is not None and duration is not None:
        raise ValueError("--end と --duration は同時に指定できません")
    if end is None:
        end = start + duration if duration is not None else total
    if clamp and end > total:
        print(f"警告: 終了位置 {end:.1f} 秒が動画の長さ {total:.1f} 秒を超えるので切り詰めます", file=sys.stderr)
        end = total
    if start >= end:
        raise ValueError(f"開始位置 {start:.1f} 秒が終了位置 {end:.1f} 秒以降です")
    return start, end - start


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ecorun-overlay", description="エコラン用テレメトリ・オーバーレイ")
    ap.add_argument("csv", nargs="?", help="ロガーの CSV(--concat-only のときは不要)")
    ap.add_argument("-o", "--output", required=True, help=".png(プレビュー) / .mp4(合成) / .mov(透過)")
    ap.add_argument("-p", "--preset", help=f"レイアウトプリセット名(presets/NAME.toml)または .toml のパス(既定 {DEFAULT_PRESET})")
    ap.add_argument("--video", nargs="+", metavar="VIDEO",
                    help="合成する車載動画。複数ファイル・フォルダを指定するとファイル名順に連結して 1 本として扱う")
    ap.add_argument("--concat-only", action="store_true", help="--video を連結しただけの動画を書き出す(オーバーレイなし・再エンコードなし)")
    ap.add_argument("--sync", help="動画 0 秒目に対応するログ時刻 (例 11:16:50) またはログ開始からの秒。"
                                   "auto で焼き込み日時を OCR して自動同期、.json なら ecorun-sync の結果を使う")
    ap.add_argument("--offset", type=float, default=0.0, help="同期の微調整 [秒]")
    ap.add_argument("--start", type=parse_position, metavar="POS",
                    help="書き出し開始位置。動画(連結後)の再生位置を 秒 か [H:]MM:SS で(既定 0)")
    ap.add_argument("--end", type=parse_position, metavar="POS",
                    help="書き出し終了位置(--start と同じ書式。省略時は動画の末尾 or ログ末尾まで)")
    ap.add_argument("--duration", type=float, help="出力長 [秒]。--start からの長さ(--end と併用不可)")
    ap.add_argument("--preview", type=float, help="ログ開始からの秒を指定して静止画 1 枚を出力")
    ap.add_argument("--debug-boxes", action="store_true", help="各ウィジェットの枠と id、配置禁止帯を描く(--preview 向け)")
    ap.add_argument("--fps", type=float, default=FPS_DEFAULT)
    ap.add_argument("--font", help="日本語フォント (.ttf/.ttc) のパス(プリセットの theme.font より優先)")
    ap.add_argument("--crf", type=int, default=18,
                    help="H.264 の画質(x264・nvenc のとき。既定 18、小さいほど高画質)")
    ap.add_argument("--encoder", choices=ENCODERS, default="auto",
                    help="H.264 のエンコーダ。auto は macOS なら videotoolbox、"
                         "それ以外で NVIDIA GPU があれば nvenc(どちらも無ければ x264)")
    ap.add_argument("--x264-preset", choices=X264_PRESETS, default="medium",
                    help="エンコードの速さと圧縮率(既定 medium。veryfast なら約 2 倍速。"
                         "nvenc では対応する preset(p1〜p7)に変換される)")
    ap.add_argument("--jobs", type=int, default=default_jobs(),
                    help=f"描画のプロセス数(既定 {default_jobs()}。1 なら並列にしない)")
    add_ocr_args(ap, "--sync-step")
    ap.add_argument("--mask-osd", action="store_true", help="(非推奨)--preset f9_mask と同じ")
    return ap


def build_timemap(sync: str | None, offset: float, tel: Telemetry, src: VideoSource | None,
                  step: float, box) -> TimeMap:
    """--sync → 動画秒からログ秒への対応。auto / .json は焼き込み日時の区間ごと、それ以外は従来の 1 本の直線"""
    s = (sync or "").strip()
    if s.lower() == "auto" or s.lower().endswith(".json"):
        if s.lower() == "auto":
            if src is None:
                raise SyncError("--sync auto には --video が必要です")
            segs = detect_cli(src, step, box)
        else:
            try:
                segs = osd_from_json(s)
            except (OSError, ValueError, KeyError) as e:
                raise SyncError(f"同期ファイルを読めません: {s}: {e}") from None
        t0 = tel.t0.to_pydatetime()
        print(describe(segs, t0))
        for w in check_against_log(segs, t0, float(tel.t[-1])):
            print("警告: " + w, file=sys.stderr)
        tm = TimeMap.from_osd(segs, t0)
    else:
        tm = TimeMap.constant(parse_sync(tel, s or None))
    return tm.shifted(offset)


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    src = None
    if args.video and (args.preview is None or (args.sync or "").strip().lower() == "auto"):
        try:
            src = probe_videos(args.video)
        except VideoError as e:
            sys.exit(str(e))
        if len(src.files) > 1:
            print(f"{len(src.files)} 本を連結: 合計 {src.duration:.1f} 秒")
            for f, t0, d in zip(src.files, src.starts, src.durations):
                print(f"  {t0:8.1f} s  {f} ({d:.1f} s)")

    if args.concat_only:
        if not src:
            ap.error("--concat-only には --video が必要です")
        from .video import concat_only
        concat_only(src, args.output)
        print(f"done → {args.output}")
        return
    if not args.csv:
        ap.error("csv を指定してください")

    preset_name = args.preset
    if args.mask_osd:
        if preset_name:
            print("警告: --mask-osd は --preset と併用できないので無視します", file=sys.stderr)
        else:
            print("警告: --mask-osd は非推奨です。--preset f9_mask を使ってください", file=sys.stderr)
            preset_name = "f9_mask"
    try:
        preset = load_preset(preset_name or DEFAULT_PRESET)
        tel = load_log(args.csv, preset.data.rpm_on_threshold, preset.data.heading_lookback_s)
    except (PresetError, LogFormatError) as e:
        sys.exit(str(e))
    fonts = Fonts(args.font or preset.theme.font or None)
    try:
        tm = build_timemap(args.sync, args.offset, tel, src, args.sync_step, args.osd_box)
    except SyncError as e:
        sys.exit(str(e))
    if src:
        total = src.duration
    elif tm.is_constant:
        total = float(tel.t[-1]) - tm.to_log(0)
    else:
        total = tm.segments[-1].v1
    try:
        start, duration = resolve_range(args.start, args.end, args.duration, total, clamp=src is not None)
    except ValueError as e:
        ap.error(str(e))
    # 区間全体のグラフなどは書き出す区間に合わせる(プレビューも本番と同じ見た目にする)
    lo, hi = tm.log_range(start, start + duration)
    t_start = max(0.0, lo)
    t_end = min(float(tel.t[-1]), hi)

    # ---- プレビュー ----
    if args.preview is not None:
        r = OverlayRenderer(tel, preset, t_start, max(t_start, t_end), fonts, args.debug_boxes)
        preview_image(r, args.preview).save(args.output)
        print(f"preview → {args.output}")
        return

    from .video import encode

    fps = src.fps if src else args.fps
    r = OverlayRenderer(tel, preset, t_start, max(t_start, t_end), fonts, args.debug_boxes)
    encode(r, args.output, tm, duration, fps, src, args.crf, start=start,
           encoder=args.encoder, x264_preset=args.x264_preset, jobs=max(1, args.jobs))
    print(f"done → {args.output}")


if __name__ == "__main__":
    main()
