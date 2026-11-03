"""`mapper session`: a session's full summary from the database, after `--no-wait` or a closed terminal."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from dmr_mapper.cli import _follow, main
from dmr_mapper.collector import state as st
from dmr_mapper.db import Store

from conftest import CAPPLUS_LOG, REPO

EXAMPLE = str(REPO / "config.example.yaml")

SURVEY_SUMMARY = {
    "budget_s": 600, "elapsed_s": 284.2, "dwell_s": 4.0, "sweeps_planned": 3,
    "sweeps": [{"sweep": 1, "seconds": 140, "hits": 12}, {"sweep": 2, "seconds": 141, "hits": 9}],
    "sweeps_skipped": 1, "sweeps_note": "2 of 3 sweeps: another one (2.4 min) would leave the decode less than half the budget",
    "then_decode_s": 315, "distinct_hits": 14,
}
DECODE_SUMMARY = {
    "budget_s": 315, "elapsed_s": 12.0, "error": "RuntimeError: receiver gone",
    "traceback": "Traceback (most recent call last):\n  File \"daemon.py\"\nRuntimeError: receiver gone",
    "plan": {"position": [32.1, 34.8], "radius_km": 5, "far_sweeps_left_out": 2},
    "carriers": {"168863000": {"lines": 10, "identities": {"radios": 3, "talkgroups": 1}}},
}


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(["--config", EXAMPLE, *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "sessions.db"
    with Store(path) as s:
        a = st.create_session(s, "survey", "cli", {"bands": ["vhf"], "then_decode": True}, 600)
        st.start_session(s, a)
        st.finish_session(s, a, "done", SURVEY_SUMMARY)
        b = st.create_session(s, "decode", "daemon", {"budget_s": 315}, 315)
        st.start_session(s, b)
        st.finish_session(s, b, "failed", DECODE_SUMMARY)
        c = st.create_session(s, "watch", "mcp", {"freqs": [168863000]}, 900)
        st.start_session(s, c)
    return path


def test_no_session_yet(tmp_path, capsys):
    code, out, err = run(capsys, "--db", str(tmp_path / "empty.db"), "session")
    assert code == 1 and "no session has run yet" in err and out == ""


def test_latest_by_default_and_by_id(db, capsys):
    code, out, _ = run(capsys, "--db", str(db), "session")
    assert code == 0
    head = out.splitlines()[0]
    assert head.startswith("session 3  watch  running  requested_by mcp  budget 900 s  started 20")
    assert "so far" in head
    assert 'params: {"freqs": [168863000]}' in out

    code, out, _ = run(capsys, "--db", str(db), "session", "1")
    assert code == 0
    head, body = out.split("\n", 1)
    assert "session 1  survey  done  requested_by cli  budget 600 s" in head and "elapsed 284.2 s" in head
    # everything after the header lines is the summary JSON, as `_follow` prints it
    summary = json.loads(body[body.index("{\n"):])
    assert summary == SURVEY_SUMMARY
    assert summary["sweeps_note"].startswith("2 of 3 sweeps") and summary["then_decode_s"] == 315


def test_traceback_only_on_request(db, capsys):
    _, out, _ = run(capsys, "--db", str(db), "session", "2")
    assert "receiver gone" in out and "far_sweeps_left_out" in out and "Traceback" not in out
    _, out, _ = run(capsys, "--db", str(db), "session", "2", "--traceback")
    assert "Traceback (most recent call last)" in out

    _, out, _ = run(capsys, "--db", str(db), "session", "2", "--json")
    v = json.loads(out)
    assert v["id"] == 2 and v["status"] == "failed" and "traceback" not in v["summary"]
    assert v["summary"]["plan"]["far_sweeps_left_out"] == 2 and v["started"] and v["finished"]
    _, out, _ = run(capsys, "--db", str(db), "session", "2", "--json", "--traceback")
    assert "traceback" in json.loads(out)["summary"]


def test_last_n(db, capsys):
    code, out, _ = run(capsys, "--db", str(db), "session", "--last", "2", "--json")
    views = json.loads(out)
    assert code == 0 and [v["id"] for v in views] == [3, 2]
    assert views[0]["status"] == "running" and views[0]["elapsed_s"] >= 0 and "progress" not in views[0]

    _, out, _ = run(capsys, "--db", str(db), "session", "--last", "10")
    assert [ln.split()[1] for ln in out.splitlines() if ln.startswith("session ")] == ["3", "2", "1"]


def test_bad_arguments(db, capsys):
    assert run(capsys, "--db", str(db), "session", "99")[0:3:2] == (1, "no session #99\n")
    code, _, err = run(capsys, "--db", str(db), "session", "1", "--last", "2")
    assert code == 2 and "not both" in err
    assert run(capsys, "--db", str(db), "session", "--last", "0")[0] == 2


def test_running_session_shows_the_daemon_progress(db, capsys):
    with Store(db) as s:
        st.write_heartbeat(s, os.getpid(), int(time.time()), "watching", 3, {}, {},
                           {"phase": "watching", "elapsed_s": 42, "carriers": {"x": 1}})
    _, out, _ = run(capsys, "--db", str(db), "session")
    assert 'progress: {"elapsed_s": 42, "phase": "watching"}' in out
    _, out, _ = run(capsys, "--db", str(db), "session", "--json")
    assert json.loads(out)["progress"]["phase"] == "watching"
    # the progress belongs to session 3 only
    _, out, _ = run(capsys, "--db", str(db), "session", "1", "--json")
    assert "progress" not in json.loads(out)


def test_follow_prints_the_same_digest(db, capsys):
    from dmr_mapper.config import load_config

    with Store(db) as s:
        assert _follow(s, load_config(EXAMPLE), 2) == 1      # a failed session
    out = capsys.readouterr().out
    assert out.startswith("session 2 failed after 12.0 s\n")
    assert json.loads(out.split("\n", 1)[1]) == {k: v for k, v in DECODE_SUMMARY.items() if k != "traceback"}


def test_summary_holds_no_radio_ids(tmp_path, cfg, capsys):
    """A decode summary built from a real capture (the Cap+ sample: nine radios, LRRP, data) has
    counts only, so the MCP redactor finds nothing to pseudonymise and the CLI shows it as stored."""
    from dmr_mapper.collector.ingest import CarrierIngest
    from dmr_mapper.mcp.tools import MapperTools

    path = tmp_path / "real.db"
    with Store(path) as s:
        ing = CarrierIngest(s, cfg, 168863000)
        with CAPPLUS_LOG.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                ing.feed(line)
        ing.finish()
        r = ing.summary()
        assert r["identities"]["radios"] == 9 and s.scalar("SELECT COUNT(*) FROM identities WHERE kind = 'radio'") == 9
        summary = {"budget_s": 60, "elapsed_s": 60.0, "carriers": {"168863000": r},
                   "slices": [{"center_hz": 168863000, "reason": "control", "dwell_s": 60, "result": {"168863000": r}}]}
        sid = st.create_session(s, "decode", "cli", {}, 60)
        st.finish_session(s, sid, "done", summary)
    assert MapperTools(cfg, path).redact(summary) == summary
    _, out, _ = run(capsys, "--db", str(path), "session", "--json")
    assert json.loads(out)["summary"] == json.loads(json.dumps(summary))


def test_cli_subprocess(db):
    r = subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--config", EXAMPLE, "--db", str(db), "session", "1", "--json"],
                       capture_output=True, text=True, cwd=REPO, env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["summary"]["sweeps"][1]["hits"] == 9
