"""One DSD-FME process per carrier: command line per CAPTURE.md §2, stderr read by a thread into
a shared queue of ``(freq_hz, line)``; ``(freq_hz, None)`` marks the end of the stream."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any


def dsd_fme_command(cfg: dict[str, Any], port: int, inverted: bool = False) -> list[str]:
    dcfg = cfg.get("decode", {}) or {}
    cmd = [str(dcfg.get("dsd_fme_bin", "dsd-fme")), "-fs", "-i", f"tcp:127.0.0.1:{port}", "-o", "null", "-Z", "-D", "0"]
    if inverted:
        cmd.append("-xr")
    cmd.extend(str(a) for a in (dcfg.get("extra_args") or []))
    return cmd


def find_dsd_fme(cfg: dict[str, Any]) -> str | None:
    name = str((cfg.get("decode", {}) or {}).get("dsd_fme_bin", "dsd-fme"))
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) else None
    return shutil.which(name)


class DecoderProcess:
    def __init__(self, cfg: dict[str, Any], freq_hz: int, port: int, lines: "queue.Queue[tuple[int, str | None]]",
                 inverted: bool = False):
        self.cfg = cfg
        self.freq_hz = freq_hz
        self.port = port
        self.inverted = inverted
        self.lines = lines
        self.proc: subprocess.Popen[str] | None = None
        self.started_at = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        cmd = dsd_fme_command(self.cfg, self.port, self.inverted)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._reader, name=f"dsd-{self.freq_hz}", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                self.lines.put((self.freq_hz, line))
        except ValueError:
            pass
        finally:
            self.lines.put((self.freq_hz, None))

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop_async(self) -> threading.Thread:
        """Stop in the background: the session thread must keep reading the receiver."""
        th = threading.Thread(target=self.stop, name=f"dsd-stop-{self.freq_hz}", daemon=True)
        th.start()
        return th

    def stop(self, grace_s: float = 3.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=grace_s)
        if self._thread is not None:
            self._thread.join(timeout=2)
