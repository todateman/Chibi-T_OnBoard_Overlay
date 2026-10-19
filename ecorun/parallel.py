"""フレームの描画を複数プロセスで並列に行い、順番どおりに取り出す

各ワーカーが OverlayRenderer を作り直し、共有メモリのスロットに RGBA を書く。
フレーム(1920×1080 で 8 MB)を pickle で戻すとメインプロセスが詰まるので、キューで渡すのはスロット番号だけにする。
共有メモリは RawArray(名前付きの SharedMemory と違い、中止されても後始末が要らない)。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue
import signal
import traceback
from collections.abc import Iterator, Sequence

from .render import OverlayRenderer


def default_jobs() -> int:
    """描画プロセス数の既定。ffmpeg のデコード・合成にもコアを残す(Apple M5 10 コアで 4 が最速、6 では落ちた)"""
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def _worker(spec, buf, frame_bytes: int, jobs, done):
    signal.signal(signal.SIGINT, signal.SIG_IGN)     # Ctrl-C はメインが受けて後始末する
    ppid = os.getppid()
    try:
        r = OverlayRenderer.from_spec(spec)
        view = memoryview(buf).cast("B")
        while True:
            try:
                job = jobs.get(timeout=1.0)
            except queue.Empty:
                if os.getppid() != ppid:             # 親が落ちたら抜ける(孤児にならない)
                    return
                continue
            if job is None:
                return
            k, slot, ts = job
            view[slot * frame_bytes:(slot + 1) * frame_bytes] = r.render(ts).tobytes()
            done.put((k, slot, None))
    except BaseException:
        done.put((-1, -1, traceback.format_exc()))


def render_frames(r: OverlayRenderer, ts_list: Sequence[float], jobs: int) -> Iterator[bytes | memoryview]:
    """ts_list の各ログ秒のフレーム(RGBA の生バイト列)を順に返す。
    jobs > 1 のとき返す memoryview は共有メモリの中身なので、次を取り出す前に使い終えること"""
    if jobs <= 1 or len(ts_list) < 2:
        for ts in ts_list:
            yield r.render(ts).tobytes()
        return

    frame_bytes = r.W * r.H * 4
    n_slots = 2 * jobs
    buf = mp.RawArray("B", frame_bytes * n_slots)
    view = memoryview(buf).cast("B")
    jobs_q, done_q = mp.Queue(), mp.Queue()
    procs = [mp.Process(target=_worker, args=(r.spec(), buf, frame_bytes, jobs_q, done_q), daemon=True)
             for _ in range(jobs)]
    for p in procs:
        p.start()
    try:
        n = len(ts_list)
        nxt = 0
        for slot in range(min(n_slots, n)):
            jobs_q.put((nxt, slot, ts_list[nxt]))
            nxt += 1
        ready: dict[int, int] = {}
        for k in range(n):
            while k not in ready:
                try:
                    kk, slot, err = done_q.get(timeout=1.0)
                except queue.Empty:
                    if not all(p.is_alive() for p in procs):
                        raise RuntimeError("描画プロセスが異常終了しました") from None
                    continue
                if err:
                    raise RuntimeError(f"描画プロセスでエラーが出ました:\n{err}")
                ready[kk] = slot
            slot = ready.pop(k)
            yield view[slot * frame_bytes:(slot + 1) * frame_bytes]
            if nxt < n:                              # 書き終えたスロットに次のフレームを描かせる
                jobs_q.put((nxt, slot, ts_list[nxt]))
                nxt += 1
    finally:
        for _ in procs:
            jobs_q.put(None)
        for p in procs:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
                p.join()
        jobs_q.close()
        done_q.close()
