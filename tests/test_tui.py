"""The console is a client: it renders the database and posts commands."""

from __future__ import annotations

import os
import time

import pytest

from dmr_mapper.collector import state as st
from dmr_mapper.db import Store
from dmr_mapper.ingest import ingest_log_file
from dmr_mapper.tui.app import MapperApp, StatusBar

from conftest import CAPPLUS_LOG


@pytest.fixture
def populated(tmp_path, cfg):
    db = tmp_path / "tui.db"
    with Store(db) as s:
        ingest_log_file(s, CAPPLUS_LOG, cfg)
        from dmr_mapper.correlate import run
        run(s, cfg)
        st.write_heartbeat(s, os.getpid(), int(time.time()), "decode", 3, {"status": "present", "driver": "sdrplay"},
                           {"supply_full": False, "volts": 4.9}, {"phase": "decoding", "slice": 1, "slices": 2, "reason": "refresh",
                                                                   "dwell_s": 60, "slice_elapsed_s": 5, "elapsed_s": 5, "budget_s": 1200,
                                                                   "carriers": [{"freq_hz": 168_863_000, "syncs": 42, "lines": 100, "level_db": 30}]}, "msg")
        sid = st.create_session(s, "decode", "tui", {}, 1200)
        st.start_session(s, sid)
    return db


@pytest.mark.asyncio
async def test_console_renders_and_posts_commands(populated, cfg):
    app = MapperApp(populated, cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        status = app.query_one("#status", StatusBar)
        text = status.text
        assert "alive" in text and "battery" in text and "decode #3" in text and "168.8630" in text
        carriers = app.query_one("#carriers")
        assert carriers.row_count == 1
        await pilot.press("s")
        await pilot.press("x")
        app.query_one("#budget").value = "15"
        await pilot.press("d")
        await pilot.pause()
    with Store(populated) as s:
        cmds = st.recent_commands(s)
        kinds = [c["kind"] for c in reversed(cmds)]
        assert kinds == ["start_survey", "stop", "start_decode"]
        assert '"budget_s":900' in cmds[0]["params"]
        assert all(c["source"] == "tui" for c in cmds)


@pytest.mark.asyncio
async def test_console_without_daemon_says_so(tmp_path, cfg):
    app = MapperApp(tmp_path / "empty.db", cfg)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert "offline" in app.query_one("#status", StatusBar).text
        await pilot.press("c")
        await pilot.pause()
    with Store(tmp_path / "empty.db") as s:
        assert st.recent_commands(s)[0]["kind"] == "correlate"
