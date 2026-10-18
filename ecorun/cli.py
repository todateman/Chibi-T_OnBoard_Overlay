"""ecorun-overlay: CSV を読み、プリセットのレイアウトでオーバーレイを描いて出力する

使い方:
  # 1) 静止画プレビュー(ログ開始から 150 秒の時点)
  ecorun-overlay LOG0650.CSV --preview 150 -o preview.png

  # 2) 車載動画に直接合成(動画の先頭 = ログ時刻 15:10:30)
  ecorun-overlay LOG0650.CSV --preset f9_mask --video onboard.mp4 --sync 15:10:30 -o out.mp4

  # 3) 透過オーバーレイだけを書き出し(DaVinci Resolve / Premiere で重ねる)
  ecorun-overlay LOG0650.CSV --sync 15:10:30 --duration 1200 -o overlay.mov
"""
from __future__ import annotations

import argparse
import sys

from .config import DEFAULT_PRESET, PresetError, load_preset
from .draw import Fonts
from .render import OverlayRenderer, preview_image
from .telemetry import LogFormatError, load_log, parse_sync

FPS_DEFAULT = 30


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ecorun-overlay", description="エコラン用テレメトリ・オーバーレイ")
    ap.add_argument("csv")
    ap.add_argument("-o", "--output", required=True, help=".png(プレビュー) / .mp4(合成) / .mov(透過)")
    ap.add_argument("-p", "--preset", help=f"レイアウトプリセット名(presets/NAME.toml)または .toml のパス(既定 {DEFAULT_PRESET})")
    ap.add_argument("--video", help="合成する車載動画")
    ap.add_argument("--sync", help="動画 0 秒目に対応するログ時刻 (例 11:16:50) またはログ開始からの秒")
    ap.add_argument("--offset", type=float, default=0.0, help="同期の微調整 [秒]")
    ap.add_argument("--duration", type=float, help="出力長 [秒](省略時は動画長 or ログ末尾まで)")
    ap.add_argument("--preview", type=float, help="ログ開始からの秒を指定して静止画 1 枚を出力")
    ap.add_argument("--debug-boxes", action="store_true", help="各ウィジェットの枠と id、配置禁止帯を描く(--preview 向け)")
    ap.add_argument("--fps", type=float, default=FPS_DEFAULT)
    ap.add_argument("--font", help="日本語フォント (.ttf/.ttc) のパス(プリセットの theme.font より優先)")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--mask-osd", action="store_true", help="(非推奨)--preset f9_mask と同じ")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

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
    t_sync = parse_sync(tel, args.sync) + args.offset

    # ---- プレビュー ----
    if args.preview is not None:
        r = OverlayRenderer(tel, preset, max(0, t_sync), tel.t[-1], fonts, args.debug_boxes)
        preview_image(r, args.preview).save(args.output)
        print(f"preview → {args.output}")
        return

    from .video import encode, probe

    fps, vsize, vdur = args.fps, None, None
    if args.video:
        vw, vh, fps, vdur = probe(args.video)
        vsize = (vw, vh)
    duration = args.duration or vdur or (tel.t[-1] - t_sync)
    t_end = min(tel.t[-1], t_sync + duration)
    r = OverlayRenderer(tel, preset, max(0.0, t_sync), t_end, fonts, args.debug_boxes)
    encode(r, args.output, t_sync, duration, fps, args.video, vsize, args.crf)
    print(f"done → {args.output}")


if __name__ == "__main__":
    main()
