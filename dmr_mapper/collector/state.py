"""Daemon state, sessions and the command queue in SQLite (migration 003).

The daemon owns ``daemon_state`` and ``sessions``; every client (TUI, MCP, CLI) talks to it only
by inserting a row into ``commands`` and reading the tables back. This keeps one control channel
over the tuner whatever the front end.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Store, canonical_json

COMMAND_KINDS = ("start_survey", "start_decode", "start_watch", "stop", "correlate", "export")
SESSION_KINDS = ("survey", "decode", "watch")


def _now() -> int:
    return int(time.time())


@dataclass
class DaemonStatus:
    alive: bool
    pid: int | None
    started: int | None
    heartbeat: int | None
    mode: str
    session_id: int | None
    sdr: dict[str, Any] = field(default_factory=dict)
    power: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# -- daemon side ------------------------------------------------------------------------------
def write_heartbeat(store: Store, pid: int, started: int, mode: str, session_id: int | None, sdr: dict[str, Any],
                    power: dict[str, Any], progress: dict[str, Any], message: str | None = None) -> None:
    store.conn.execute(
        "INSERT INTO daemon_state (id, pid, started, heartbeat, mode, session_id, sdr, power, progress, message)"
        " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET pid = excluded.pid, started = excluded.started, heartbeat = excluded.heartbeat,"
        " mode = excluded.mode, session_id = excluded.session_id, sdr = excluded.sdr, power = excluded.power,"
        " progress = excluded.progress, message = excluded.message",
        (pid, started, _now(), mode, session_id, canonical_json(sdr), canonical_json(power), canonical_json(progress), message))
    store.commit()


def mark_offline(store: Store, pid: int, message: str = "daemon stopped") -> None:
    store.conn.execute("UPDATE daemon_state SET mode = 'offline', session_id = NULL, progress = '{}', message = ? WHERE id = 1 AND pid = ?",
                       (message, pid))
    store.commit()


def take_command(store: Store) -> dict[str, Any] | None:
    """Atomically claim the oldest pending command."""
    row = store.conn.execute(
        "UPDATE commands SET status = 'taken', taken_at = ? WHERE id = (SELECT id FROM commands WHERE status = 'pending' ORDER BY id LIMIT 1)"
        " RETURNING *", (_now(),)).fetchone()
    store.commit()
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"] or "{}")
    return d


def finish_command(store: Store, command_id: int, ok: bool, result: str) -> None:
    store.conn.execute("UPDATE commands SET status = ?, done_at = ?, result = ? WHERE id = ?",
                       ("done" if ok else "failed", _now(), result[:500], command_id))
    store.commit()


def create_session(store: Store, kind: str, requested_by: str, params: dict[str, Any], budget_s: int) -> int:
    cur = store.conn.execute(
        "INSERT INTO sessions (kind, requested_by, params, budget_s, created, status) VALUES (?, ?, ?, ?, ?, 'pending')",
        (kind, requested_by, canonical_json(params), int(budget_s), _now()))
    store.commit()
    return int(cur.lastrowid)


def start_session(store: Store, session_id: int) -> None:
    store.conn.execute("UPDATE sessions SET started = ?, status = 'running' WHERE id = ?", (_now(), session_id))
    store.commit()


def finish_session(store: Store, session_id: int, status: str, summary: dict[str, Any]) -> None:
    store.conn.execute("UPDATE sessions SET finished = ?, status = ?, summary = ? WHERE id = ?",
                       (_now(), status, canonical_json(summary), session_id))
    store.commit()


def update_session_summary(store: Store, session_id: int, summary: dict[str, Any]) -> None:
    store.conn.execute("UPDATE sessions SET summary = ? WHERE id = ?", (canonical_json(summary), session_id))
    store.commit()


# -- client side ------------------------------------------------------------------------------
def post_command(store: Store, source: str, kind: str, params: dict[str, Any] | None = None) -> int:
    if kind not in COMMAND_KINDS:
        raise ValueError(f"unknown command {kind!r}; expected one of {COMMAND_KINDS}")
    cur = store.conn.execute("INSERT INTO commands (created, source, kind, params) VALUES (?, ?, ?, ?)",
                             (_now(), source, kind, canonical_json(params or {})))
    store.commit()
    return int(cur.lastrowid)


def read_status(store: Store, stale_s: int = 10) -> DaemonStatus:
    row = store.conn.execute("SELECT * FROM daemon_state WHERE id = 1").fetchone()
    if row is None:
        return DaemonStatus(False, None, None, None, "offline", None, message="daemon has never run on this database")
    alive = row["mode"] != "offline" and (_now() - int(row["heartbeat"])) <= stale_s and _pid_alive(row["pid"])
    mode = row["mode"] if alive else "offline"
    return DaemonStatus(alive, row["pid"], row["started"], row["heartbeat"], mode, row["session_id"] if alive else None,
                        json.loads(row["sdr"] or "{}"), json.loads(row["power"] or "{}"), json.loads(row["progress"] or "{}") if alive else {},
                        row["message"] if alive else (row["message"] or "daemon is not running"))


def command(store: Store, command_id: int) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM commands WHERE id = ?", (command_id,)).fetchone()
    return dict(row) if row else None


def recent_commands(store: Store, limit: int = 20) -> list[dict[str, Any]]:
    return [dict(r) for r in store.rows("SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,))]


def sessions(store: Store, limit: int = 20) -> list[dict[str, Any]]:
    out = []
    for r in store.rows("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["params"] = json.loads(d["params"] or "{}")
        d["summary"] = json.loads(d["summary"] or "{}")
        out.append(d)
    return out


def wait_for_command(store: Store, command_id: int, timeout_s: float = 30.0, poll_s: float = 0.5) -> dict[str, Any] | None:
    """Block until the daemon has taken and finished the command (CLI convenience)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        c = command(store, command_id)
        if c and c["status"] in ("done", "failed"):
            return c
        time.sleep(poll_s)
    return command(store, command_id)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
