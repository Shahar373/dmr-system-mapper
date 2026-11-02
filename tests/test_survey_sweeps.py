"""Per-session dwell, several sweeps in one survey session, and decode planning from the merged
hits of the recent sweeps."""

from __future__ import annotations

import copy
import os
import threading
import time

import pytest

from dmr_mapper.cli import main
from dmr_mapper.collector import state as st
from dmr_mapper.collector.daemon import Daemon
from dmr_mapper.collector.scheduler import targets_from_store
from dmr_mapper.collector.sdr import SyntheticBackend, SyntheticSource, SynthCarrier
from dmr_mapper.collector.survey import (check_survey_params, latest_hits, max_dwell_s, plan_window_s, recent_hits,
                                         sweep_seconds)
from dmr_mapper.db import Store
from dmr_mapper.mcp.tools import MapperTools


def test_longest_dwell_follows_the_hit_test(cfg):
    # 1 s segments and a 9 dB gate: one loud second (12 dB) still clears the p95 gate in 11 segments, not in 12
    assert max_dwell_s(cfg) == 11
    c = copy.deepcopy(cfg)
    c["survey"]["segment_s"] = 0.5
    assert max_dwell_s(c) == 5.5
    c["survey"].update(segment_s=1, snr_threshold_db=6)
    assert max_dwell_s(c) >= 11


def test_check_survey_params(cfg):
    assert check_survey_params({}, cfg) == (3.0, 1)
    assert check_survey_params({"dwell_s": 8, "sweeps": 3}, cfg) == (8.0, 3)
    for bad in ({"dwell_s": 12}, {"dwell_s": 0.5}, {"sweeps": 0}, {"sweeps": 21}):
        with pytest.raises(ValueError):
            check_survey_params(bad, cfg)
    # the configured dwell is not checked: tests and replays use short ones on purpose
    c = copy.deepcopy(cfg)
    c["survey"]["dwell_s"] = 0.2
    assert check_survey_params({}, c) == (0.2, 1)


def test_sweep_time_estimate(cfg):
    bands = cfg["survey"]["bands"]
    assert 2.5 * 60 < sweep_seconds(bands, 2_000_000, 0.8, 3) < 3 * 60      # 53 steps
    assert 7 * 60 < sweep_seconds(bands, 2_000_000, 0.8, 8) < 7.5 * 60


def _run(store: Store, band: str, started: int, hits: dict[int, float]) -> int:
    rid = store.add_survey_run(band, started, started + 60, "test", {})
    for f, snr in hits.items():
        store.add_survey_hit(rid, f, snr_db=snr, p95_snr_db=snr + 2, occupancy_pct=50, width_hz=8000, **{"class": "narrow"})
    store.commit()
    return rid


def test_recent_hits_merge_the_sweeps_of_the_window(store, cfg):
    now = int(time.time())
    A, B, C, D = 456_850_000, 457_600_000, 458_575_000, 165_625_000
    first = _run(store, "uhf", now - 7200, {A: 20, B: 10})
    _run(store, "uhf", now - 3600, {A: 15, C: 12})
    _run(store, "uhf", now - 60, {A: 18})
    _run(store, "vhf-high", now - 9 * 86400, {D: 25})                     # too old for planning
    _run(store, "uhf:469.0000-470.0000", now - 30, {469_350_000: 30})      # a range survey keeps its own name

    merged = {h["freq_hz"]: h for h in recent_hits(store, 3 * 3600, max_age_s=7 * 86400)}
    assert set(merged) == {A, B, C, 469_350_000}
    assert merged[A]["heard_in"] == 3 and merged[A]["sweeps"] == 3
    assert merged[A]["snr_db"] == 20 and merged[A]["run_id"] == first      # the values of the strongest sweep
    assert merged[A]["run_started"] == now - 60                             # but the last time it was heard
    assert (merged[B]["heard_in"], merged[B]["sweeps"]) == (1, 3)
    assert merged[469_350_000]["sweeps"] == 1 and merged[469_350_000]["band"] == "uhf:469.0000-470.0000"

    # a shorter window: the newest sweep and the one an hour before it (3540 s apart)
    assert {h["freq_hz"] for h in recent_hits(store, 3600, max_age_s=7 * 86400)} == {A, C, 469_350_000}
    # window 0 is the newest run per band, as before
    assert {h["freq_hz"] for h in latest_hits(store, max_age_s=7 * 86400)} == {A, 469_350_000}
    assert {h["freq_hz"] for h in latest_hits(store)} == {A, D, 469_350_000}

    # the decode planner sees a carrier that only an earlier sweep heard
    targets = {t.freq_hz: t for t in targets_from_store(store, cfg, recent_hits(store, plan_window_s(cfg), 7 * 86400))}
    assert B in targets and targets[B].snr_db == 10


def test_plan_window_from_config(cfg):
    assert plan_window_s(cfg) == 180 * 60
    assert plan_window_s({"survey": {"plan_window_minutes": 30}}) == 1800
    assert plan_window_s({}) == 180 * 60


def test_several_sweeps_with_a_session_dwell(tmp_path, cfg):
    c = copy.deepcopy(cfg)
    c["survey"]["bands"] = [{"name": "test", "start_hz": 164_400_000, "stop_hz": 166_000_000, "raster_hz": 12_500}]
    c["survey"].update(segment_s=0.5)
    c["daemon"].update({"tick_s": 0.2, "sdr_probe_interval_s": 1, "default_budget_min": 1})
    c["power"]["state_file"] = ""
    src = SyntheticSource(2_400_000, 165_000_000, [SynthCarrier(164_512_500, 25, None), SynthCarrier(165_300_000, 30, None)])
    db = tmp_path / "d.db"
    d = Daemon(db, c, SyntheticBackend(src))
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    client = Store(db)
    try:
        time.sleep(0.6)
        # a dwell the hit test cannot honour is refused before the receiver is opened
        cid = st.post_command(client, "test", "start_survey", {"budget_s": 60, "dwell_s": 30})
        c1 = st.wait_for_command(client, cid, 30)
        assert c1["status"] == "failed" and "dwell must be between" in c1["result"], c1

        cid = st.post_command(client, "test", "start_survey", {"budget_s": 60, "dwell_s": 0.5, "sweeps": 3})
        assert st.wait_for_command(client, cid, 30)["status"] == "done"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            sess = st.sessions(client)[0]
            if sess["status"] != "running" and sess["status"] != "starting":
                break
            time.sleep(0.2)
        assert sess["status"] == "done", sess
        sm = sess["summary"]
        assert sm["dwell_s"] == 0.5 and sm["sweeps_planned"] == 3 and len(sm["sweeps"]) == 3, sm
        assert sm["distinct_hits"] >= 2
        runs = client.rows("SELECT settings FROM survey_runs ORDER BY id")
        assert len(runs) == 3 and all('"dwell_s":0.5' in r["settings"] for r in runs), [r["settings"] for r in runs]
        assert '"sweep":"3/3"' in runs[-1]["settings"]
        merged = {h["freq_hz"]: h for h in recent_hits(client, plan_window_s(c))}
        assert merged[165_300_000]["heard_in"] == 3 and merged[165_300_000]["sweeps"] == 3
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


def _alive(path) -> None:
    with Store(path) as s:
        st.write_heartbeat(s, os.getpid(), int(time.time()), "idle", None, {"status": "present"}, {}, {})


def test_mcp_scan_with_dwell_and_sweeps(tmp_path, cfg):
    db = tmp_path / "m.db"
    Store(db).close()
    _alive(db)
    t = MapperTools(cfg, db)
    command, params, what = t.prepare_session("survey", minutes=20, dwell_s=8, sweeps=2)
    assert command == "start_survey" and params["dwell_s"] == 8 and params["sweeps"] == 2
    assert what == "survey 2 sweeps 8 s per step 20 min"
    with pytest.raises(ValueError, match="dwell must be between"):
        t.prepare_session("survey", dwell_s=15)


def test_cli_refuses_a_bad_dwell_without_a_daemon(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "c.db"), "survey", "--dwell", "30"]) == 2
    assert "dwell must be between" in capsys.readouterr().err
