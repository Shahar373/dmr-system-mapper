"""A daemon that cannot answer (2026-11-03 on the Pi): the receiver driver blocks in open(), the
database refuses the heartbeat, or a daemon holds the lock with a stale heartbeat.

Offline: a synthetic receiver whose ``open()`` blocks, a heartbeat write that fails, and a lock
file held by this test."""

from __future__ import annotations

import copy
import fcntl
import logging
import os
import re
import sqlite3
import threading
import time

import pytest

from dmr_mapper import cli
from dmr_mapper.cli import main
from dmr_mapper.collector import state as st
from dmr_mapper.collector.daemon import Daemon
from dmr_mapper.collector.sdr import SyntheticBackend, SyntheticSource
from dmr_mapper.db import Store

from conftest import REPO

EXAMPLE = str(REPO / "config.example.yaml")


@pytest.fixture
def dcfg(cfg):
    c = copy.deepcopy(cfg)
    c["survey"]["bands"] = [{"name": "test", "start_hz": 164_400_000, "stop_hz": 166_000_000, "raster_hz": 12_500}]
    c["survey"]["dwell_s"] = 1
    c["daemon"].update({"tick_s": 0.1, "sdr_probe_interval_s": 1, "default_budget_min": 1,
                        "sdr_open_timeout_s": 2, "sdr_probe_timeout_s": 2})
    c["power"]["state_file"] = ""
    return c


class BlockingBackend(SyntheticBackend):
    """``open()`` blocks until ``release`` is set, like the SDRplay API waiting for a receiver that
    another program holds."""

    def __init__(self) -> None:
        super().__init__(SyntheticSource())
        self.release = threading.Event()
        self.block = True
        self.returned = threading.Event()

    def open(self):
        if self.block:
            self.release.wait(60)
        try:
            return super().open()
        finally:
            self.returned.set()


def _start(d: Daemon) -> threading.Thread:
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    return th


def _wait_alive(client: Store, timeout: float = 10) -> st.DaemonStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = st.read_status(client, stale_s=2)
        if s.alive:
            return s
        time.sleep(0.05)
    raise AssertionError(f"daemon never came up: {st.read_status(client).to_dict()}")


def _wait_log(caplog, *parts: str, timeout: float = 5) -> None:
    """The daemon logs from its own threads, a moment after the state a test waits on."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(all(p in r.getMessage() for p in parts) for r in caplog.records):
            return
        time.sleep(0.05)
    raise AssertionError(f"no log line with {parts}: {[r.getMessage() for r in caplog.records]}")


def test_a_blocking_open_is_abandoned_and_the_heartbeat_keeps_going(tmp_path, dcfg, caplog):
    caplog.set_level(logging.INFO, logger="dmr_mapper.daemon")
    backend = BlockingBackend()
    db = tmp_path / "block.db"
    d = Daemon(db, dcfg, backend)
    th = _start(d)
    client = Store(db)
    try:
        _wait_alive(client)
        cid = st.post_command(client, "test", "start_survey", {"budget_s": 60})
        # while the driver call blocks, the heartbeat stays fresh (stale_s 2, open timeout 2 s)
        beats = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            beats.append(st.read_status(client, stale_s=2).alive)
            c = st.command(client, cid)
            if c["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        assert all(beats), beats
        assert c["status"] == "failed", c
        assert "receiver open timed out after 2 s" in c["result"], c
        assert "in use by" in c["result"] or "another program may be holding the receiver" in c["result"], c
        assert st.sessions(client) == []
        s = st.read_status(client, stale_s=2)
        assert s.alive and s.mode == "idle" and s.sdr["status"] == "busy", s.to_dict()

        # the call is still blocked: the next attempt is refused at once, it does not pile up
        t0 = time.monotonic()
        c2 = st.wait_for_command(client, st.post_command(client, "test", "start_survey", {"budget_s": 60}), 10, 0.05)
        assert c2["status"] == "failed" and "still blocked in the driver" in c2["result"], c2
        assert time.monotonic() - t0 < 2
        # other commands are served meanwhile
        assert st.wait_for_command(client, st.post_command(client, "test", "stop"), 5, 0.05)["status"] == "done"

        # the driver answers at last: the receiver it opened for nobody is released again
        backend.block = False
        backend.release.set()
        assert backend.returned.wait(5)
        deadline = time.monotonic() + 5
        while not backend.source.closed and time.monotonic() < deadline:
            time.sleep(0.05)
        assert backend.source.closed
        _wait_log(caplog, "returned after", "released")
        deadline = time.monotonic() + 5
        while d._stuck is not None and d._stuck[0].is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)

        # and the next session starts normally
        c3 = st.wait_for_command(client, st.post_command(client, "test", "start_survey", {"budget_s": 60}), 15, 0.05)
        assert c3["status"] == "done" and c3["result"].startswith("session "), c3
        assert st.wait_for_command(client, st.post_command(client, "test", "stop"), 15, 0.05)["status"] == "done"
    finally:
        backend.release.set()
        d.stop_event.set()
        th.join(timeout=30)
        client.close()
    assert any("did not return within 2 s" in r.getMessage() for r in caplog.records)


def test_a_failing_heartbeat_write_is_logged_once_and_answers_are_kept(tmp_path, dcfg, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="dmr_mapper.daemon")
    real_beat, real_finish = st.write_heartbeat, st.finish_command
    broken = threading.Event()
    broken.set()
    finish_fails = {"left": 1}

    def beat(*a, **kw):
        if broken.is_set():
            raise sqlite3.OperationalError("disk I/O error")
        return real_beat(*a, **kw)

    def finish(*a, **kw):
        if finish_fails["left"]:
            finish_fails["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_finish(*a, **kw)

    monkeypatch.setattr(st, "write_heartbeat", beat)
    db = tmp_path / "beat.db"
    d = Daemon(db, dcfg, SyntheticBackend())
    th = _start(d)
    client = Store(db)
    try:
        time.sleep(1.5)                     # ~15 ticks, every one failing at the heartbeat
        assert not st.read_status(client, stale_s=2).alive
        errors = [r for r in caplog.records if "database error in the main loop" in r.getMessage()]
        assert len(errors) == 1, [r.getMessage() for r in errors]       # rate-limited, not once per tick
        assert "disk I/O error" in errors[0].getMessage() and errors[0].levelno == logging.WARNING
        assert d._db_errors >= 5 and d.message == "database error: disk I/O error"

        # the database comes back; an answer whose write fails is written at the next tick
        monkeypatch.setattr(st, "finish_command", finish)
        broken.clear()
        _wait_alive(client)
        _wait_log(caplog, "writable again after")
        c = st.wait_for_command(client, st.post_command(client, "test", "stop"), 10, 0.05)
        assert c["status"] == "done" and c["result"] == "nothing running", c
        assert finish_fails["left"] == 0
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


def test_daemon_start_fails_orphaned_taken_and_expires_old_pending(tmp_path, dcfg):
    db = tmp_path / "recover.db"
    with Store(db) as s:
        taken = st.post_command(s, "cli", "start_campaign", {"budget_s": 1200})
        assert st.take_command(s)["id"] == taken           # an earlier daemon took it and hung
        old = st.post_command(s, "cli", "start_campaign", {"budget_s": 1200})
        stop = st.post_command(s, "cli", "stop")
        s.conn.execute("UPDATE commands SET created = created - 3600 WHERE id IN (?, ?)", (old, stop))
        s.commit()
        fresh = st.post_command(s, "cli", "stop")
    d = Daemon(db, dcfg, SyntheticBackend())
    th = _start(d)
    client = Store(db)
    try:
        assert st.wait_for_command(client, fresh, 10, 0.05)["status"] == "done"
        rows = {c["id"]: c for c in st.recent_commands(client)}
        assert rows[taken]["status"] == "failed" and "stopped or hung" in rows[taken]["result"]
        assert rows[old]["status"] == "expired" and rows[stop]["status"] == "expired"
        assert st.sessions(client) == []                    # the hour-old campaign did not start
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


# -- CLI ----------------------------------------------------------------------------------------------
def _stale_heartbeat(db, age_s: int = 120) -> None:
    with Store(db) as s:
        st.write_heartbeat(s, os.getpid(), int(time.time()) - 3600, "idle", None, {}, {}, {}, "started")
        s.conn.execute("UPDATE daemon_state SET heartbeat = heartbeat - ?", (age_s,))
        s.commit()


@pytest.fixture
def held_lock(tmp_path):
    """A daemon (pid 1473) holds the database's lock: its own open file, as the kernel sees it."""
    db = tmp_path / "locked.db"
    fh = open(st.lock_path(db), "a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fh.write("1473")
    fh.flush()
    yield db
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()


def test_lock_holder(tmp_path, held_lock):
    assert st.lock_holder(held_lock) == "1473"
    assert st.lock_holder(tmp_path / "other.db") is None
    free = tmp_path / "free.db"
    open(st.lock_path(free), "w").write("99")       # left behind by a daemon that has exited
    assert st.lock_holder(free) is None


def test_stale_heartbeat_with_the_lock_held_posts_nothing(held_lock, capsys):
    _stale_heartbeat(held_lock)
    code = main(["--config", EXAMPLE, "--db", str(held_lock), "campaign", "--minutes", "20"])
    out = capsys.readouterr()
    assert code == 1
    assert re.search(r"the daemon \(pid 1473\) is running but its heartbeat is stale \(last one 12\d s ago, it last said: started\)",
                     out.err), out.err
    assert "journalctl -u dmr-mapper" in out.err
    assert "Traceback" not in out.err + out.out and "already running" not in out.err
    with Store(held_lock) as s:
        assert s.scalar("SELECT COUNT(*) FROM commands") == 0

    assert main(["--config", EXAMPLE, "--db", str(held_lock), "status"]) == 0
    out = capsys.readouterr().out
    assert "daemon: STALE: the daemon (pid 1473) is running but its heartbeat is stale" in out, out

    assert main(["--config", EXAMPLE, "--db", str(held_lock), "stop"]) == 1
    assert "heartbeat is stale" in capsys.readouterr().err
    with Store(held_lock) as s:
        assert s.scalar("SELECT COUNT(*) FROM commands") == 0


def test_taken_but_unanswered_is_explained(tmp_path, capsys, monkeypatch):
    """A live daemon takes the command and never answers it (stuck in the driver)."""
    db = tmp_path / "taken.db"
    monkeypatch.setattr(cli, "COMMAND_WAIT_S", 1)
    stop = threading.Event()

    def stuck_daemon() -> None:
        with Store(db) as s:
            while not stop.is_set():
                st.write_heartbeat(s, os.getpid(), int(time.time()), "idle", None, {}, {}, {}, "started")
                st.take_command(s)
                time.sleep(0.1)

    _stale_heartbeat(db, 0)
    th = threading.Thread(target=stuck_daemon, daemon=True)
    th.start()
    try:
        code = main(["--config", EXAMPLE, "--db", str(db), "campaign", "--minutes", "20"])
    finally:
        stop.set()
        th.join(5)
    out = capsys.readouterr()
    assert code == 1
    assert "taken: None" not in out.out + out.err
    assert "the daemon took command #1 but has not answered in 1 s" in out.err, out.err
    assert "mapper status" in out.err and "journalctl -u dmr-mapper" in out.err
    assert main(["--config", EXAMPLE, "--db", str(db), "status"]) == 0
    assert "command #1 start_campaign from cli: taken since" in capsys.readouterr().out
