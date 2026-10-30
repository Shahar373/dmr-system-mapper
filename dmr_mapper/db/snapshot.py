"""Consistent copies of the live database for the archive.

``VACUUM INTO`` writes a compact, transaction-consistent copy while the daemon keeps writing (WAL).
The copy is checked with ``PRAGMA quick_check`` before it replaces the day's snapshot, so the
archive never pulls a half-written file. One file per local day: ``mapper-YYYYmmdd.db``.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def snapshot(db_path: Path, out_dir: Path, keep: int = 3, tz: str = "UTC") -> tuple[Path, list[Path]]:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(tz=ZoneInfo(tz)).strftime("%Y%m%d")
    final = out_dir / f"mapper-{day}.db"
    tmp = out_dir / f".mapper-{day}.db.tmp"
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()
    check = sqlite3.connect(str(tmp))
    try:
        result = check.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        tmp.unlink()
        raise RuntimeError(f"snapshot failed its integrity check: {result}")
    os.replace(tmp, final)
    removed = []
    if keep > 0:
        for old in sorted(out_dir.glob("mapper-*.db"))[:-keep]:
            old.unlink()
            removed.append(old)
    return final, removed
