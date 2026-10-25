import shutil

import pytest

from ecorun.cli import main
from ecorun.config import load_preset
from ecorun.draw import Fonts
from ecorun.render import OverlayRenderer
from ecorun.video import (VideoError, VideoSource, encode, expand_videos, extract_frame, h264_args, probe,
                          probe_videos, resolve_encoder)

from conftest import make_video

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg が無い")


def test_expand_videos(seg_dir, tmp_path):
    files = expand_videos([str(seg_dir)])
    assert [f.rsplit("/", 1)[1] for f in files] == ["F9_0000.MP4", "F9_0001.MP4", "F9_0002.MP4"]
    # 同じフォルダに書き出した結果は連結しない(明示すれば読める)
    out = seg_dir / "F9_0000_overlay.mp4"
    shutil.copy(files[0], out)
    try:
        assert expand_videos([str(seg_dir)]) == files
        assert expand_videos([str(out)]) == [str(out)]
    finally:
        out.unlink()
    # ファイルは指定順のまま
    assert expand_videos([files[2], files[0]]) == [files[2], files[0]]
    (tmp_path / "x.CSV").write_text("a")
    with pytest.raises(VideoError, match="CSV は --video より前"):
        expand_videos([str(tmp_path / "x.CSV")])
    with pytest.raises(VideoError, match="見つかりません"):
        expand_videos([str(tmp_path / "nothing.mp4")])
    with pytest.raises(VideoError, match="フォルダに動画がありません"):
        expand_videos([str(tmp_path)])


def test_probe_and_locate(seg_dir):
    src = probe_videos([str(seg_dir)])
    assert len(src.files) == 3 and (src.width, src.height) == (320, 180)
    assert src.duration == pytest.approx(3.0, abs=0.1)
    d0 = src.durations[0]
    assert src.locate(0.5) == (0, pytest.approx(0.5))
    assert src.locate(d0) == (1, pytest.approx(0.0))       # 境界は次のファイル
    assert src.locate(-1)[0] == 0 and src.locate(99) == (2, src.durations[2])


def test_mismatched_size_is_rejected(seg_dir, tmp_path):
    other = make_video(tmp_path / "big.mp4", "red", size="640x360")
    with pytest.raises(VideoError, match="連結できません"):
        probe_videos([str(seg_dir / "F9_0000.MP4"), str(other)])


def test_extract_frame_picks_segment(seg_dir):
    src = probe_videos([str(seg_dir)])
    for t, rgb in [(0.5, (255, 0, 0)), (1.5, (0, 0, 255)), (2.5, (0, 255, 0)), (src.duration, (0, 255, 0))]:
        px = extract_frame(src, t).getpixel((160, 90))
        assert all(abs(a - b) < 40 for a, b in zip(px, rgb)), (t, px)


def test_concat_only_cli(seg_dir, tmp_path):
    out = tmp_path / "joined.mp4"
    main(["--concat-only", "--video", str(seg_dir), "-o", str(out)])
    assert probe(str(out))[3] == pytest.approx(3.0, abs=0.15)


def test_csv_required_without_concat_only(seg_dir, tmp_path):
    with pytest.raises(SystemExit):
        main(["--video", str(seg_dir), "-o", str(tmp_path / "x.mp4")])


def test_encode_concatenated(seg_dir, tel, tmp_path):
    src = probe_videos([str(seg_dir)])
    r = OverlayRenderer(tel, load_preset("minimal"), 30, 30 + src.duration, Fonts())
    out = tmp_path / "out.mp4"
    encode(r, str(out), 30, src.duration, src.fps, src)
    w, h, _, dur = probe(str(out))
    assert (w, h) == (320, 180) and dur == pytest.approx(3.0, abs=0.15)
    assert extract_frame(str(out), 2.5).getpixel((160, 90))[1] > 200     # 3 本目(緑)まで入っている


@pytest.mark.parametrize("encoder", ["x264", "auto"])
def test_encode_parallel(seg_dir, tel, tmp_path, encoder):
    """描画を 2 プロセスにしても枚数・内容が揃う(auto は macOS なら videotoolbox)"""
    src = probe_videos([str(seg_dir)])
    r = OverlayRenderer(tel, load_preset("minimal"), 30, 30 + src.duration, Fonts())
    out = tmp_path / f"par_{encoder}.mp4"
    encode(r, str(out), 30, src.duration, src.fps, src, encoder=encoder, jobs=2)
    assert probe(str(out))[3] == pytest.approx(3.0, abs=0.15)
    assert extract_frame(str(out), 2.5).getpixel((160, 90))[1] > 200


def test_resolve_encoder():
    assert resolve_encoder("x264") == "x264"
    assert resolve_encoder("nvenc") == "nvenc"
    assert resolve_encoder("auto") in ("x264", "videotoolbox", "nvenc")
    assert h264_args("x264", 20, "veryfast") == ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    assert h264_args("videotoolbox")[1] == "h264_videotoolbox"
    assert h264_args("nvenc", 20, "veryfast") == \
        ["-c:v", "h264_nvenc", "-preset", "p2", "-rc", "vbr", "-cq", "20", "-b:v", "0"]
    with pytest.raises(ValueError):
        resolve_encoder("vaapi")


def test_single_file_source(seg_dir):
    src = probe_videos([str(seg_dir / "F9_0001.MP4")])
    assert isinstance(src, VideoSource) and len(src.files) == 1


def test_encode_from_start(seg_dir, tel, tmp_path):
    """動画の途中(1.2 秒 = 2 本目の青)から 1.5 秒だけ書き出す"""
    src = probe_videos([str(seg_dir)])
    r = OverlayRenderer(tel, load_preset("minimal"), 31.2, 32.7, Fonts())
    out = tmp_path / "part.mp4"
    encode(r, str(out), 30, 1.5, src.fps, src, start=1.2)
    assert probe(str(out))[3] == pytest.approx(1.5, abs=0.15)
    assert extract_frame(str(out), 0.0).getpixel((160, 90))[2] > 200    # 先頭は青
    assert extract_frame(str(out), 1.3).getpixel((160, 90))[1] > 200    # 末尾は緑


def test_start_end_cli(seg_dir, synth_csv, tmp_path):
    out = tmp_path / "cut.mp4"
    main([str(synth_csv), "--preset", "minimal", "--video", str(seg_dir), "--sync", "30",
          "--start", "0:01", "--end", "2.5", "-o", str(out)])
    assert probe(str(out))[3] == pytest.approx(1.5, abs=0.15)
    with pytest.raises(SystemExit):     # --end と --duration は排他
        main([str(synth_csv), "--video", str(seg_dir), "--start", "1", "--end", "2", "--duration", "1",
              "-o", str(tmp_path / "x.mp4")])
