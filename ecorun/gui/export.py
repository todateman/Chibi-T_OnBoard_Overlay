"""GUI からの書き出し: ecorun-overlay を別プロセスで動かし、進み具合とログを保持する

描画をサーバーのプロセス内で回すとプレビューの描画が止まるので、CLI をそのまま子プロセスで実行する。
同時に動かすのは 1 本だけ。"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from ..config import ROOT_DIR

PROGRESS_RE = re.compile(r"(\d+)/(\d+) frames\s+ETA\s+(\d+)s")


class ExportJob:
    def __init__(self, kind: str, args: list[str], output: Path, tmpdir: Path | None = None):
        self.kind, self.args, self.output, self.tmpdir = kind, args, output, tmpdir
        self.state = "running"            # running / done / failed / cancelled
        self.frame = self.total = self.eta = None
        self.log: deque[str] = deque(maxlen=200)
        self.returncode = None
        self.cancelling = False
        self.t0 = time.time()
        self.t1 = None
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "ecorun.cli", *args], cwd=ROOT_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)       # 中止のとき ffmpeg ごとプロセスグループで止める
        threading.Thread(target=self._read, daemon=True).start()

    @property
    def command(self) -> str:
        return shlex.join(["uv", "run", "ecorun-overlay", *self.args])

    def _read(self):
        buf = b""
        while chunk := self.proc.stdout.read1(4096):
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)     # 進捗は \r で上書きして出てくる
            for ln in lines:
                self._line(ln.decode("utf-8", "replace"))
        if buf:
            self._line(buf.decode("utf-8", "replace"))
        rc = self.proc.wait()
        with self._lock:
            if self.cancelling:
                self.output.unlink(missing_ok=True)       # 途中までの出力は残さない
            if self.tmpdir:
                shutil.rmtree(self.tmpdir, ignore_errors=True)
            # 後始末が済んでから終わったことにする(state を見て次の書き出しを始めてよい)
            self.returncode, self.t1 = rc, time.time()
            self.state = "cancelled" if self.cancelling else "done" if rc == 0 else "failed"

    def _line(self, s: str):
        s = s.strip()
        if not s:
            return
        m = PROGRESS_RE.search(s)
        with self._lock:
            if m:
                self.frame, self.total, self.eta = int(m[1]), int(m[2]), int(m[3])
            else:
                self.log.append(s)

    def cancel(self):
        with self._lock:
            if self.state != "running" or self.cancelling:
                return
            self.cancelling = True
        try:
            if hasattr(os, "killpg"):
                os.killpg(self.proc.pid, signal.SIGTERM)
            else:
                self.proc.terminate()
        except ProcessLookupError:
            pass

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state, "cancelling": self.cancelling, "kind": self.kind,
                "output": str(self.output), "command": self.command,
                "frame": self.frame, "total": self.total, "eta": self.eta,
                "elapsed": round((self.t1 or time.time()) - self.t0, 1),
                "returncode": self.returncode, "log": list(self.log)[-40:],
            }
