"""Raw DSD-FME logs, for debugging only (``decode.keep_raw_logs``).

One file per carrier per decode slice, named like a manual capture
(``dsd_fme_<freqHz>_<YYYYmmdd_HHMMSS>.log``) so ``mapper ingest`` can replay it. A file is
gzipped when it is closed or reaches ``raw_log_max_mb``; the directory is then pruned, oldest
first, to ``raw_log_max_mb × raw_log_keep`` in total. Nothing else is ever written raw.
"""

from __future__ import annotations

import gzip
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class RawLog:
    def __init__(self, directory: Path, freq_hz: int, cfg: dict[str, Any]):
        dcfg = cfg.get("decode", {}) or {}
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.freq_hz = freq_hz
        self.max_bytes = int(float(dcfg.get("raw_log_max_mb", 50)) * 1_000_000)
        self.total_cap = self.max_bytes * max(1, int(dcfg.get("raw_log_keep", 5)))
        self.tz = ZoneInfo(str(cfg.get("timezone") or "UTC"))
        self._fh = None
        self._path: Path | None = None
        self._size = 0
        self._open()

    def _open(self) -> None:
        stamp = datetime.now(tz=self.tz).strftime("%Y%m%d_%H%M%S")
        path = self.dir / f"dsd_fme_{self.freq_hz}_{stamp}.log"
        n = 1
        while path.exists() or path.with_suffix(".log.gz").exists():
            path = self.dir / f"dsd_fme_{self.freq_hz}_{stamp}_{n}.log"
            n += 1
        self._path = path
        self._fh = path.open("w", encoding="utf-8", errors="replace")
        self._size = 0

    def write(self, line: str) -> None:
        if self._fh is None:
            return
        self._fh.write(line)
        self._size += len(line)
        if self._size >= self.max_bytes:
            self._rotate()

    def _rotate(self) -> None:
        self._close_file()
        self._open()

    def _close_file(self) -> None:
        if self._fh is None or self._path is None:
            return
        self._fh.close()
        self._fh = None
        if self._path.stat().st_size == 0:
            self._path.unlink()
        else:
            gz = self._path.with_suffix(".log.gz")
            with self._path.open("rb") as src, gzip.open(gz, "wb") as dst:
                shutil.copyfileobj(src, dst)
            self._path.unlink()
        prune(self.dir, self.total_cap)

    def close(self) -> None:
        self._close_file()


def prune(directory: Path, cap_bytes: int) -> list[Path]:
    """Delete the oldest compressed raw logs until the directory holds at most ``cap_bytes``."""
    files = sorted(directory.glob("dsd_fme_*.log.gz"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed = []
    while files and total > cap_bytes:
        p = files.pop(0)
        total -= p.stat().st_size
        p.unlink()
        removed.append(p)
    return removed


def raw_dir(cfg: dict[str, Any]) -> Path:
    from ..config import resolve_path
    return resolve_path(cfg, "raw_logs")


def _now() -> int:
    return int(time.time())
