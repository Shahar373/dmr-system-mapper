"""End-to-end daemon test: synthetic receiver, fake dsd-fme, commands through the queue."""

from __future__ import annotations

import copy
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from scipy import signal as ss

from dmr_mapper.collector import state as st
from dmr_mapper.collector.daemon import Daemon
from dmr_mapper.collector.sdr import SyntheticBackend, SyntheticSource, SynthCarrier
from dmr_mapper.db import Store

from conftest import REPO

FAKE = REPO / "tests" / "fake_dsd_fme.py"
WAV = REPO / "samples/surveyor/decodes_20260713/C0013/20260713_150256/iq/discriminator.wav"


def _audio() -> np.ndarray:
    with wave.open(str(WAV)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    lp = ss.firwin(101, 5000, fs=48000)
    a = ss.lfilter(lp, 1.0, a).astype(np.float32)
    return a / np.percentile(np.abs(a), 99.5) * 0.9


@pytest.fixture
def daemon_cfg(cfg):
    c = copy.deepcopy(cfg)
    c["survey"]["bands"] = [{"name": "test", "start_hz": 164_400_000, "stop_hz": 166_000_000, "raster_hz": 12_500}]
    c["survey"]["dwell_s"] = 1
    c["decode"].update({"dsd_fme_bin": str(FAKE), "seconds_per_carrier": 4, "max_instances": 2, "tcp_base_port": 7455, "invert_retry_s": 2})
    c["daemon"].update({"tick_s": 0.2, "sdr_probe_interval_s": 1, "default_budget_min": 1})
    c["power"]["state_file"] = ""
    return c


def _wait(store, cmd_id, timeout=60):
    c = st.wait_for_command(store, cmd_id, timeout)
    assert c and c["status"] in ("done", "failed"), c
    return c


def _wait_idle(store, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = st.read_status(store, stale_s=5)
        if s.alive and s.mode == "idle":
            return s
        time.sleep(0.2)
    raise AssertionError(f"daemon did not return to idle: {st.read_status(store).to_dict()}")


def test_survey_then_decode_through_the_command_queue(tmp_path, daemon_cfg):
    audio = _audio()
    src = SyntheticSource(2_400_000, 165_000_000, [
        SynthCarrier(164_512_500, 25, None), SynthCarrier(165_300_000, 30, audio), SynthCarrier(165_600_000, 22, audio.copy())])
    backend = SyntheticBackend(src)
    db = tmp_path / "d.db"
    d = Daemon(db, daemon_cfg, backend)
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    client = Store(db)
    try:
        time.sleep(0.6)
        s = st.read_status(client, stale_s=5)
        assert s.alive and s.mode == "idle" and s.sdr.get("status") == "present"

        cmd = st.post_command(client, "test", "start_survey", {"budget_s": 60})
        c = _wait(client, cmd)
        assert c["status"] == "done", c
        s = _wait_idle(client)
        sess = st.sessions(client)[0]
        assert sess["kind"] == "survey" and sess["status"] == "done", sess
        hits = {int(r["freq_hz"]) for r in client.rows("SELECT freq_hz FROM survey_hits")}
        assert {164_512_500, 165_300_000, 165_600_000} <= hits, hits
        assert len(hits) <= 6

        cmd = st.post_command(client, "test", "start_decode", {"budget_s": 40})
        c = _wait(client, cmd)
        assert c["status"] == "done", c
        # the daemon reports progress while decoding
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            s = st.read_status(client, stale_s=5)
            if s.progress.get("phase") == "decoding" and s.progress.get("carriers"):
                break
            time.sleep(0.3)
        assert s.mode == "decode" and s.progress.get("phase") == "decoding", s.to_dict()
        assert s.progress["freqs"] and s.progress["dwell_s"] >= 4, s.progress
        s = _wait_idle(client, timeout=120)
        sess = st.sessions(client)[0]
        assert sess["kind"] == "decode" and sess["status"] in ("done", "stopped"), sess
        carriers = sess["summary"]["carriers"]
        assert {"164512500", "165300000", "165600000"} <= set(carriers), carriers
        assert all(v["syncs"] > 100 for v in carriers.values()), carriers
        assert any(sl["reason"] for sl in sess["summary"]["slices"])
        assert "correlate" in sess["summary"]
        assert client.scalar("SELECT COUNT(*) FROM carriers") == 3
        assert client.scalar("SELECT COUNT(*) FROM csbk_events") > 0
        assert client.scalar("SELECT COUNT(*) FROM ingest_runs WHERE source = 'collector' AND finished IS NOT NULL") >= 3
        assert client.scalar("SELECT MAX(snr_db_max) FROM observations") > 20

        # stop a running session early
        cmd = st.post_command(client, "test", "start_decode", {"budget_s": 60, "freqs": [165_300_000]})
        assert _wait(client, cmd)["status"] == "done"
        time.sleep(2)
        cmd = st.post_command(client, "test", "stop")
        assert _wait(client, cmd)["status"] == "done"
        s = _wait_idle(client)
        assert st.sessions(client)[0]["status"] == "stopped"
        assert src.closed
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()
    with Store(db) as s2:
        assert not st.read_status(s2).alive


def test_busy_receiver_and_double_start_are_refused(tmp_path, daemon_cfg):
    backend = SyntheticBackend(SyntheticSource(), status="busy", holder="trunk-recorder (pid 42)")
    db = tmp_path / "busy.db"
    d = Daemon(db, daemon_cfg, backend)
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    client = Store(db)
    try:
        time.sleep(0.5)
        c = _wait(client, st.post_command(client, "test", "start_survey"))
        assert c["status"] == "failed" and "busy" in c["result"] and "trunk-recorder" in c["result"], c
        c = _wait(client, st.post_command(client, "test", "stop"))
        assert c["status"] == "done" and c["result"] == "nothing running"
        with pytest.raises(RuntimeError):
            Daemon(db, daemon_cfg, backend).run()
    finally:
        d.stop_event.set()
        th.join(timeout=10)
        client.close()
