"""Fixes from the full review before the first long field use."""

from __future__ import annotations

import copy
import gc
import subprocess
import sys
import threading
import time
import types
import weakref

import numpy as np
import pytest

from dmr_mapper.collector import state as st
from dmr_mapper.collector.daemon import Daemon
from dmr_mapper.collector.scheduler import DC_GUARD_HZ, Target, off_dc, plan
from dmr_mapper.collector.sdr import SoapyBackend, SynthCarrier, SyntheticBackend, SyntheticSource
from dmr_mapper.collector.survey import analyse_block, step_centres
from dmr_mapper.correlate import run
from dmr_mapper.db import Store

from conftest import REPO

FAKE = REPO / "tests" / "fake_dsd_fme.py"


def _cfg(cfg, port):
    c = copy.deepcopy(cfg)
    c["survey"]["bands"] = [{"name": "t", "start_hz": 469_000_000, "stop_hz": 470_000_000, "raster_hz": 12_500}]
    c["survey"]["dwell_s"] = 0.3
    c["decode"].update({"dsd_fme_bin": str(FAKE), "seconds_per_carrier": 2, "max_instances": 2, "tcp_base_port": port})
    c["daemon"].update({"tick_s": 0.05, "sdr_probe_interval_s": 1})
    c["power"]["state_file"] = ""
    c["gps"] = {"source": "none"}
    return c


def _run(d):
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    time.sleep(0.4)
    return th


def _wait_sessions(client, n, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ss = st.sessions(client)
        if len(ss) >= n and all(x["status"] in ("done", "stopped", "failed") for x in ss[:n]):
            return ss
        time.sleep(0.2)
    raise AssertionError(st.sessions(client))


def test_chained_decode_is_not_lost_while_the_survey_is_closing(tmp_path, cfg, monkeypatch):
    """The survey thread queues the decode and then still writes its summary. A fast main loop
    must wait for it rather than refuse the decode as "already running"."""
    real_finish = st.finish_session

    def slow_finish(store, sid, status, summary):
        time.sleep(0.6)                      # widen the window the bug lived in
        real_finish(store, sid, status, summary)

    monkeypatch.setattr(st, "finish_session", slow_finish)
    src = SyntheticSource(2_000_000, 469_500_000, [SynthCarrier(469_350_000, 25, None)])
    db = tmp_path / "c.db"
    d = Daemon(db, _cfg(cfg, 7655), SyntheticBackend(src))
    th = _run(d)
    client = Store(db)
    try:
        st.post_command(client, "t", "start_survey", {"then_decode": True, "budget_s": 64, "total_budget_s": 64})
        deadline = time.time() + 30
        while time.time() < deadline and len(st.sessions(client)) < 2:
            time.sleep(0.1)
        cmds = st.recent_commands(client)
        assert not any(c["status"] == "failed" for c in cmds), cmds
        assert [x["kind"] for x in st.sessions(client)] == ["decode", "survey"]
        st.post_command(client, "t", "stop")
        _wait_sessions(client, 2)
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


def test_budget_end_is_done_and_operator_stop_is_stopped(tmp_path, cfg):
    src = SyntheticSource(2_000_000, 469_500_000, [SynthCarrier(469_350_000, 25, None)])
    db = tmp_path / "b.db"
    d = Daemon(db, _cfg(cfg, 7665), SyntheticBackend(src))
    th = _run(d)
    client = Store(db)
    try:
        st.post_command(client, "tui", "start_survey", {"budget_s": 30})
        assert _wait_sessions(client, 1)[0]["status"] == "done"
        st.post_command(client, "tui", "start_decode", {"budget_s": 5})       # no freqs: loops until the budget ends
        s1 = _wait_sessions(client, 2)[0]
        assert s1["status"] == "done" and s1["summary"]["ended"] == "time budget used", s1["summary"]
        st.post_command(client, "tui", "start_decode", {"budget_s": 60})
        time.sleep(2)
        st.post_command(client, "tui", "stop")
        s2 = _wait_sessions(client, 3)[0]
        assert s2["status"] == "stopped" and s2["summary"]["stopped"] == "stopped by tui", s2["summary"]
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


def test_correlate_runs_beside_the_main_loop(tmp_path, cfg, monkeypatch):
    import dmr_mapper.correlate as corr
    started = threading.Event()

    def slow_run(store, cfg, now=None, window_days=None):
        started.set()
        time.sleep(3)
        from dmr_mapper.correlate.model import CorrelateReport
        return CorrelateReport(now=0, window_days=30)

    monkeypatch.setattr(corr, "run", slow_run)
    db = tmp_path / "j.db"
    d = Daemon(db, _cfg(cfg, 7675), SyntheticBackend(SyntheticSource()))
    th = _run(d)
    client = Store(db)
    try:
        cid = st.post_command(client, "tui", "correlate")
        assert started.wait(5)
        beats = set()
        for _ in range(10):                    # the heartbeat keeps moving while correlate runs
            beats.add(client.scalar("SELECT heartbeat FROM daemon_state"))
            time.sleep(0.25)
        assert st.read_status(client).alive and len(beats) >= 2
        assert st.post_command(client, "tui", "correlate")              # a second one is refused, not queued behind
        c = st.wait_for_command(client, cid, 10)
        assert c["status"] == "done" and c["result"].startswith("links"), c
        second = st.recent_commands(client)[0]
        assert second["status"] in ("failed", "done")
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


def test_slices_keep_carriers_off_the_dc_point(cfg):
    one = [Target(469_350_000, score=5)]
    s = plan(one, cfg, 2, 2_000_000)[0]
    assert abs(s.center_hz - 469_350_000) >= DC_GUARD_HZ and abs(s.center_hz - 469_350_000) <= 800_000
    two = [Target(469_062_500, score=5), Target(469_650_000, score=5)]
    s = plan(two, cfg, 2, 2_000_000)[0]
    assert all(DC_GUARD_HZ <= abs(s.center_hz - t.freq_hz) <= 800_000 for t in two)
    # three carriers right around the centre: still moved off them
    c = off_dc(469_350_000, [469_337_500, 469_350_000, 469_362_500], 1_600_000)
    assert min(abs(c - f) for f in (469_337_500, 469_350_000, 469_362_500)) >= DC_GUARD_HZ


def test_survey_ignores_the_dc_spike_and_steps_off_raster():
    src = SyntheticSource(2_000_000, 469_500_000, [SynthCarrier(469_500_000, 40, None)], noise_db=-20)   # a "carrier" at DC = LO leakage
    hits = analyse_block(src.read(2_000_000), 2_000_000, 469_500_000, 12_500, 0.8, 0.5, 9.0)
    assert not any(abs(h.freq_hz - 469_500_000) <= 12_500 for h in hits), hits
    assert all((c % 12_500) == 6_250 for c in step_centres(469_000_000, 470_000_000, 2_000_000, 0.8))


def test_tier3_same_numbers_different_sys_code_are_not_one_site(store, cfg):
    now = 1_793_380_000
    for f, sys_code in ((469_350_000, "2101"), (453_350_000, "5101")):
        cid = store.upsert_carrier(f, now - 600)
        store.upsert_carrier_seen(cid, now - 600, now - 60)
        store.conn.execute("UPDATE carriers SET variant = 'tier3', dominant_cc = 1 WHERE id = ?", (cid,))
        store.add_csbk_event(cid, now - 120, 1, 1, "tier3_sys_parms",
                             {"model": "Large", "net_id": 2, "site_id": 1, "sub_site": 2, "sys_code": sys_code, "source": "slc_c_sys_parms"}, "x")
    store.commit()
    run(store, cfg, now=now)
    assert store.scalar("SELECT COUNT(*) FROM links WHERE kind = 'same_site'") == 0


def test_failed_receiver_setup_releases_the_device(monkeypatch, cfg):
    released = []

    class Device:
        @staticmethod
        def enumerate(args):
            return [{"driver": "sdrplay", "serial": "X"}]

        def __init__(self, args):
            pass

        def __del__(self):
            released.append(True)            # what SoapySDR's own __del__ does (unmake)

        def listSampleRates(self, *a):
            return [2e6]

        def setSampleRate(self, *a):
            pass

        def setGainMode(self, *a):
            pass

        def listGains(self, *a):
            return []

        def setupStream(self, *a):
            raise RuntimeError("setupStream failed")

    mod = types.ModuleType("SoapySDR")
    mod.Device = Device
    mod.SOAPY_SDR_RX, mod.SOAPY_SDR_CF32 = 0, "CF32"
    monkeypatch.setitem(sys.modules, "SoapySDR", mod)
    with pytest.raises(RuntimeError, match="setupStream failed"):
        SoapyBackend(cfg).open()
    gc.collect()
    assert released == [True]


def test_cli_then_decode_follows_the_decode_with_a_temporary_daemon(tmp_path):
    conf = tmp_path / "c.yaml"
    conf.write_text(f"""
sdr: {{source: synthetic}}
gps: {{source: none}}
power: {{state_file: ""}}
survey:
  bands: [{{name: t, start_hz: 469000000, stop_hz: 469500000, raster_hz: 12500}}]
  dwell_s: 0.2
decode: {{dsd_fme_bin: {FAKE}, tcp_base_port: 7685}}
daemon: {{tick_s: 0.1}}
""")
    r = subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--config", str(conf), "--db", str(tmp_path / "x.db"),
                        "survey", "--then-decode", "--minutes", "1.1"], capture_output=True, text=True, cwd=REPO, timeout=120,
                       env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "decode session 2 follows" in r.stdout and "session 2 done" in r.stdout, r.stdout
