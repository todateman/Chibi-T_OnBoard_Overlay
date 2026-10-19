"""動画秒 → ログ秒の対応

従来の --sync は「ログ秒 = 動画秒 + t_sync」の 1 本の直線だった。焼き込み時刻を OCR すると、
編集のつなぎ目や分割ファイル境目の録画欠けで時刻が飛ぶ区間ごとに別の offset になるので、区間の列で持つ。
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    """動画の [v0, v1) 秒では ログ秒 = 動画秒 + offset"""
    v0: float
    v1: float
    offset: float


@dataclass(frozen=True)
class OsdSegment:
    """OCR の結果。動画の [v0, v1) 秒は、動画 v0 秒目の焼き込み時刻が wall0 で、そこから等速で進む"""
    v0: float
    v1: float
    wall0: datetime

    def wall_at(self, v: float) -> datetime:
        return self.wall0 + timedelta(seconds=v - self.v0)


class TimeMap:
    """区間の列(v0 の昇順、隙間なし)。範囲外の動画秒は端の区間で外挿する"""

    def __init__(self, segments: list[Segment]):
        if not segments:
            raise ValueError("区間がありません")
        self.segments = sorted(segments, key=lambda s: s.v0)
        self._starts = [s.v0 for s in self.segments]

    @classmethod
    def constant(cls, t_sync: float) -> TimeMap:
        return cls([Segment(0.0, float("inf"), t_sync)])

    @classmethod
    def from_osd(cls, segs: list[OsdSegment], t0: datetime) -> TimeMap:
        """焼き込み時刻をログ開始時刻 t0 基準のログ秒に直す"""
        return cls([Segment(s.v0, s.v1, (s.wall0 - t0).total_seconds() - s.v0) for s in segs])

    def segment_index(self, v: float) -> int:
        return max(0, bisect.bisect_right(self._starts, v) - 1)

    def to_log(self, v: float) -> float:
        return v + self.segments[self.segment_index(v)].offset

    def shifted(self, dt: float) -> TimeMap:
        """全区間に dt 秒を足す(--offset)"""
        return TimeMap([Segment(s.v0, s.v1, s.offset + dt) for s in self.segments])

    def log_range(self, v0: float, v1: float) -> tuple[float, float]:
        """動画の [v0, v1] に対応するログ秒の最小・最大(区間の飛びをまたいでもよい)"""
        lo, hi = float("inf"), float("-inf")
        for i, s in enumerate(self.segments):
            a = v0 if i == 0 else max(v0, s.v0)                 # 端の区間は外挿するので範囲外も含める
            b = v1 if i == len(self.segments) - 1 else min(v1, s.v1)
            if a > b:
                continue
            lo, hi = min(lo, a + s.offset), max(hi, b + s.offset)
        return lo, hi

    @property
    def is_constant(self) -> bool:
        return len(self.segments) == 1


# ---- OCR 結果の保存(ログに依存しない形) ----
def osd_to_json(segs: list[OsdSegment], path: str | Path, videos: list[str] | None = None):
    data = {"videos": videos or [],
            "segments": [{"video_start": round(s.v0, 3), "video_end": round(s.v1, 3),
                          "wall_start": s.wall0.isoformat(timespec="milliseconds")} for s in segs]}
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def osd_from_json(path: str | Path) -> list[OsdSegment]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [OsdSegment(float(s["video_start"]), float(s["video_end"]), datetime.fromisoformat(s["wall_start"]))
            for s in data["segments"]]
