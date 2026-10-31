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


@pytest.mark.asyncio
async def test_console_watch_status_and_naming(populated, cfg):
    with Store(populated) as s:
        st.write_heartbeat(s, os.getpid(), int(time.time()), "watch", 3, {"status": "present", "driver": "sdrplay"},
                           {"supply_full": False}, {"phase": "watching", "center_hz": 469_400_000, "instances": "2/3", "keyups": 4, "missed": 1,
                                                    "elapsed_s": 30, "budget_s": 600,
                                                    "channels": [{"freq_hz": 469_350_000, "state": "pinned", "snr_db": 31.0},
                                                                 {"freq_hz": 469_062_500, "state": "decoding", "snr_db": 18.5}]}, "msg")
        sys_id = int(s.scalar("SELECT id FROM systems ORDER BY id LIMIT 1"))
    app = MapperApp(populated, cfg)
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        text = app.query_one("#status", StatusBar).text
        assert "watch #3 at 469.4000 MHz" in text and "469.0625" in text and "key-ups 4 (missed 1)" in text, text
        await pilot.press("w")
        await pilot.press("n")                      # nothing selected yet: only a hint in the log
        tree = app.query_one("#systems")
        node = next(n for n in tree.root.children if n.data[:2] == ("system", sys_id))
        tree.move_cursor(node)
        await pilot.pause()
        assert app._selected[:2] == ("system", sys_id) and app._selected[2] == 168_863_000
        await pilot.press("n")
        assert app.focused is app.query_one("#name")
        await pilot.press(*"Base [/x] [red]", "enter")     # brackets in a name are text, not console markup
        await pilot.pause()
        await pilot.pause()
        assert str(tree.root.children[0].label).startswith("Base [/x] [red] (")
    with Store(populated) as s:
        assert [c["kind"] for c in st.recent_commands(s)][:1] == ["start_watch"]
        assert s.scalar("SELECT name FROM systems WHERE id = ?", (sys_id,)) == "Base [/x] [red]"


@pytest.mark.asyncio
async def test_console_approves_and_refuses_claudes_requests(populated, cfg):
    with Store(populated) as s:
        a = st.post_command(s, "mcp", "start_watch", {"freqs": [469_350_000], "budget_s": 600}, awaiting_approval=True)
        b = st.post_command(s, "mcp", "start_survey", {"bands": ["uhf"], "budget_s": 300}, awaiting_approval=True)
    app = MapperApp(populated, cfg)
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        text = app.query_one("#status", StatusBar).text
        assert f"Claude asks #{b}" in text and "survey bands uhf 5 min" in text and "+1 more" in text, text
        await pilot.press("v")                      # the newest first
        await pilot.press("y")
        await pilot.pause()
        assert "Claude asks" not in app.query_one("#status", StatusBar).text
    with Store(populated) as s:
        assert st.command(s, b)["status"] == "refused" and st.command(s, b)["result"] == "refused by tui"
        assert st.command(s, a)["status"] == "pending" and st.command(s, a)["result"] == "approved by tui"
