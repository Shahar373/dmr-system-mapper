"""Daemon state, sessions and the command queue in SQLite (migration 003).

The daemon owns ``daemon_state`` and ``sessions``; every client (TUI, MCP, CLI) talks to it only
by inserting a row into ``commands`` and reading the tables back. This keeps one control channel
over the tuner whatever the front end.

A session Claude asks for through MCP (Phase 5) can wait for the operator's approval: it is inserted
with status ``awaiting_approval``, which the daemon never takes, and becomes ``pending`` only when
the operator approves it in the console or with ``mapper approve``. A request nobody answers
expires after ``mcp.approval_timeout_min``: a scan asked for at one place must not start an hour
later somewhere else.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Store, canonical_json

COMMAND_KINDS = ("start_survey", "start_decode", "start_watch", "start_campaign", "stop", "correlate", "export")
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
def post_command(store: Store, source: str, kind: str, params: dict[str, Any] | None = None, awaiting_approval: bool = False) -> int:
    if kind not in COMMAND_KINDS:
        raise ValueError(f"unknown command {kind!r}; expected one of {COMMAND_KINDS}")
    cur = store.conn.execute("INSERT INTO commands (created, source, kind, params, status) VALUES (?, ?, ?, ?, ?)",
                             (_now(), source, kind, canonical_json(params or {}), "awaiting_approval" if awaiting_approval else "pending"))
    store.commit()
    return int(cur.lastrowid)


def expire_requests(store: Store, timeout_s: int) -> int:
    """Requests waiting for approval longer than ``timeout_s`` will never start."""
    cur = store.conn.execute("UPDATE commands SET status = 'expired', done_at = ?, result = 'nobody approved it in time'"
                             " WHERE status = 'awaiting_approval' AND created < ?", (_now(), _now() - int(timeout_s)))
    store.commit()
    return cur.rowcount


def awaiting_approval(store: Store, timeout_s: int) -> list[dict[str, Any]]:
    expire_requests(store, timeout_s)
    out = []
    for r in store.rows("SELECT * FROM commands WHERE status = 'awaiting_approval' ORDER BY id"):
        d = dict(r)
        d["params"] = json.loads(d["params"] or "{}")
        out.append(d)
    return out


def decide(store: Store, command_id: int, approve: bool, by: str, timeout_s: int) -> dict[str, Any]:
    """Approve (the daemon takes it at its next tick) or refuse a request that awaits approval."""
    expire_requests(store, timeout_s)
    c = command(store, command_id)
    if c is None:
        raise KeyError(f"no request #{command_id}")
    if c["status"] != "awaiting_approval":
        raise ValueError(f"request #{command_id} is {c['status']}, not waiting for approval")
    if approve:
        store.conn.execute("UPDATE commands SET status = 'pending', result = ? WHERE id = ? AND status = 'awaiting_approval'",
                           (f"approved by {by}", command_id))
    else:
        store.conn.execute("UPDATE commands SET status = 'refused', done_at = ?, result = ? WHERE id = ? AND status = 'awaiting_approval'",
                           (_now(), f"refused by {by}", command_id))
    store.commit()
    return command(store, command_id) or {}


def describe_request(kind: str, params: dict[str, Any]) -> str:
    """One line for the operator: what the session would do, where and for how long."""
    what = kind.removeprefix("start_")
    bits: list[str] = []
    if params.get("bands"):
        bits.append("bands " + ", ".join(map(str, params["bands"])))
    if params.get("range"):
        r = params["range"]
        bits.append(f"{r['start_hz'] / 1e6:.4f}-{r['stop_hz'] / 1e6:.4f} MHz")
    if params.get("freqs"):
        bits.append(", ".join(f"{int(f) / 1e6:.4f}" for f in params["freqs"]) + " MHz")
    if params.get("center_hz"):
        bits.append(f"centre {int(params['center_hz']) / 1e6:.4f} MHz")
    if params.get("sweeps") and int(params["sweeps"]) > 1:
        bits.append(f"{int(params['sweeps'])} sweeps")
    if params.get("dwell_s") is not None:
        bits.append(f"{float(params['dwell_s']):g} s per step")
    if params.get("then_decode"):
        what = "survey, then decode,"
    if params.get("budget_s"):
        bits.append(f"{int(params['budget_s']) // 60} min")
    return f"{what} " + (" ".join(bits) if bits else "(defaults)")


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


def sessions(store: Store, limit: int = 20, session_id: int | None = None) -> list[dict[str, Any]]:
    """The newest ``limit`` sessions, newest first; with ``session_id``, only that one (if it exists)."""
    out = []
    where, args = ("WHERE id = ? ", (int(session_id), limit)) if session_id is not None else ("", (limit,))
    for r in store.rows(f"SELECT * FROM sessions {where}ORDER BY id DESC LIMIT ?", args):
        d = dict(r)
        d["params"] = json.loads(d["params"] or "{}")
        d["summary"] = json.loads(d["summary"] or "{}")
        out.append(d)
    return out


def recover_commands(store: Store, pending_max_age_s: int) -> dict[str, int]:
    """Run by a daemon as it starts, holding the lock: no other daemon is alive on this database.

    * a ``taken`` command was claimed by an earlier daemon that died or hung before answering:
      nobody will ever finish it, so it is marked failed (its client gave up long ago);
    * a ``pending`` command older than ``pending_max_age_s`` was posted while no daemon could take
      it (e.g. by a client that then hit an error or a Ctrl-C): starting it now, perhaps hours later
      and somewhere else, would surprise the operator, so it expires. ``0`` keeps them all.
    """
    now = _now()
    taken = store.conn.execute(
        "UPDATE commands SET status = 'failed', done_at = ?, result = ? WHERE status = 'taken'",
        (now, "the daemon that took this command stopped or hung before answering it; ask again")).rowcount
    expired = 0
    if pending_max_age_s > 0:
        expired = store.conn.execute(
            "UPDATE commands SET status = 'expired', done_at = ?, result = ? WHERE status = 'pending' AND created < ?",
            (now, "posted while no daemon was taking commands; ask again", now - int(pending_max_age_s))).rowcount
    store.commit()
    return {"taken_failed": taken, "pending_expired": expired}


def explain_unanswered(kind: str, command_id: int, c: dict[str, Any] | None, waited_s: float) -> str:
    """What a client tells the operator when ``wait_for_command`` came back without an answer."""
    status = (c or {}).get("status")
    if status == "taken":
        return (f"{kind}: the daemon took command #{command_id} but has not answered in {waited_s:.0f} s."
                " It may be stuck in the receiver driver: see `mapper status` and `journalctl -u dmr-mapper`.")
    if status == "pending":
        return (f"{kind}: command #{command_id} is still waiting: the daemon has not taken it in {waited_s:.0f} s."
                " See `mapper status` and `journalctl -u dmr-mapper`.")
    return f"{kind}: command #{command_id} is {status or 'missing'}"


def lock_path(db_path: str | os.PathLike[str]) -> str:
    """One daemon per database: it holds an exclusive flock on this file while it runs."""
    return str(db_path) + ".daemon.lock"


def lock_holder(db_path: str | os.PathLike[str]) -> str | None:
    """The pid written in the lock file if a daemon holds the lock now, else None. The kernel drops
    a flock when its process dies, so a held lock means a live process, heartbeat or not.
    (Only asks: a free lock is released again at once.)"""
    import fcntl

    path = lock_path(db_path)
    if not os.path.exists(path):
        return None
    try:
        fh = open(path, "r")
    except OSError:
        return None
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            return fh.read().strip() or "?"
        fcntl.flock(fh, fcntl.LOCK_UN)
        return None


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
