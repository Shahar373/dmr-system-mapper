"""Unit tests for the collector pieces: channelizer, survey analysis, scheduler, power, state."""

from __future__ import annotations

import json
import time
import wave

import numpy as np
import pytest
from scipy import signal as ss

from dmr_mapper.collector import state as st
from dmr_mapper.collector.channelizer import AUDIO_RATE, Channelizer
from dmr_mapper.collector.power import PowerState, budget_for, parse_state_file
from dmr_mapper.collector.scheduler import Target, plan, targets_from_store
from dmr_mapper.collector.sdr import FileBackend, SyntheticSource, SynthCarrier, capture_iq
from dmr_mapper.collector.survey import analyse_block, latest_hits, record, step_centres, sweep
from dmr_mapper.db import Store

from conftest import REPO

WAV = REPO / "samples/surveyor/decodes_20260713/C0013/20260713_150256/iq/discriminator.wav"


def _audio() -> np.ndarray:
    with wave.open(str(WAV)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    a = ss.lfilter(ss.firwin(101, 5000, fs=48000), 1.0, a).astype(np.float32)
    return a / np.percentile(np.abs(a), 99.5) * 0.9


def test_channelizer_recovers_the_modulating_audio():
    audio = _audio()
    src = SyntheticSource(2_400_000, 165_000_000, [SynthCarrier(165_300_000, 30, audio), SynthCarrier(164_512_500, 25, None)])
    ch = Channelizer(2_400_000, 165_000_000)
    ch.set_channels([165_300_000, 164_512_500])
    outs = []
    for _ in range(20):
        outs.append(ch.process(src.read(ch.hop))[165_300_000])
    y = np.concatenate(outs).astype(np.float32) / 32767
    assert abs(len(y) - 20 * ch.hop // 50) <= 2 and AUDIO_RATE == 48_000
    a = audio[:len(y)]
    corr, delay = max((float(np.corrcoef(a[:len(y) - d], y[d:])[0, 1]), d) for d in range(200, 300))
    assert corr > 0.9, (corr, delay)
    assert 0.5 < np.percentile(np.abs(y[4800:]), 99.5) < 1.0          # normalised to ~0.9 of full scale
    assert ch.channels[165_300_000].level_db > ch.channels[164_512_500].level_db
    # the unmodulated carrier demodulates to silence
    quiet = np.concatenate([ch.process(src.read(ch.hop))[164_512_500] for _ in range(3)]).astype(np.float32) / 32767
    assert np.abs(quiet).mean() < 0.2


def test_channelizer_rejects_wrong_block_size():
    ch = Channelizer(2_400_000, 165_000_000)
    ch.set_channels([165_000_000])
    with pytest.raises(ValueError):
        ch.process(np.zeros(10, dtype=np.complex64))
    with pytest.raises(ValueError):
        Channelizer(1_000_003, 165_000_000)


@pytest.mark.parametrize("fs", [2_000_000, 2_400_000, 3_000_000])
def test_channelizer_works_at_the_rates_the_sdrplay_offers(fs):
    from dmr_mapper.collector.channelizer import pick_decimation
    D, up, down = pick_decimation(fs)
    assert 64_000 <= fs / D <= 128_000 and (fs / D) * up / down == 48_000
    audio = _audio()
    src = SyntheticSource(fs, 165_000_000, [SynthCarrier(165_300_000, 30, audio)])
    ch = Channelizer(fs, 165_000_000)
    ch.set_channels([165_300_000])
    y = np.concatenate([ch.process(src.read(ch.hop))[165_300_000] for _ in range(10)]).astype(np.float32) / 32767
    assert abs(len(y) - 10 * ch.hop * 48_000 / fs) <= 10          # 48 kHz out, whatever the capture rate
    a = audio[:len(y)]
    corr = max(float(np.corrcoef(a[:len(y) - d], y[d:])[0, 1]) for d in range(0, 600))
    assert corr > 0.95, (fs, corr)


def test_survey_analysis_finds_carriers_on_the_raster():
    audio = _audio()
    src = SyntheticSource(2_400_000, 165_000_000, [
        SynthCarrier(164_512_500, 25, None), SynthCarrier(165_300_000, 30, audio), SynthCarrier(165_612_500, 12, audio.copy())], noise_db=-20)
    x = src.read(int(2.4e6 * 2))
    hits = analyse_block(x, 2.4e6, 165e6, 12_500, 0.8, 1.0, 9.0)
    freqs = {h.freq_hz: h for h in hits}
    assert {164_512_500, 165_300_000, 165_612_500} <= set(freqs), freqs
    assert len(hits) <= 5
    assert freqs[165_300_000].snr_db > freqs[165_612_500].snr_db
    assert all(h.occupancy_pct > 50 for h in hits)
    assert freqs[164_512_500].width_hz < 6000 and freqs[164_512_500].klass == "narrow"


def test_step_centres_cover_the_band():
    c = step_centres(160_000_000, 174_000_000, 2_400_000, 0.8)
    assert len(c) == 8 and c[0] == 160_960_000 and c[-1] + 960_000 >= 174_000_000


def test_sweep_and_record(store, cfg):
    src = SyntheticSource(2_400_000, 0, [SynthCarrier(160_512_500, 25, None), SynthCarrier(162_300_000, 20, None)])
    c = dict(cfg)
    c["survey"] = dict(cfg["survey"], dwell_s=0.5)
    band = {"name": "t", "start_hz": 160_000_000, "stop_hz": 163_000_000, "raster_hz": 12_500}
    seen = []
    hits = sweep(src, band, c, progress=seen.append)
    assert {h.freq_hz for h in hits} >= {160_512_500, 162_300_000} and seen[-1]["steps"] == 2
    run_id = record(store, "t", int(time.time()), hits, src.describe(), {"dwell_s": 0.5})
    latest = latest_hits(store)
    assert {h["freq_hz"] for h in latest} == {h.freq_hz for h in hits} and latest[0]["band"] == "t"
    assert store.scalar("SELECT COUNT(*) FROM survey_hits WHERE run_id = ?", (run_id,)) == len(hits)
    # a stop callback ends the sweep early
    assert sweep(src, band, c, stop=lambda: True) == []


def test_scheduler_groups_same_site_carriers_and_prefers_unknown(store, cfg):
    now = int(time.time())
    a = store.upsert_carrier(168_500_000, now)
    b = store.upsert_carrier(168_512_500, now)
    for cid in (a, b):
        store.conn.execute("UPDATE carriers SET variant = 'capplus', dominant_cc = 2 WHERE id = ?", (cid,))
        store.add_csbk_event(cid, now, 1, 2, "capplus_site", {"site": 2, "rest_lsn": 1, "rs": "00"}, "2")
    run = store.start_ingest_run("collector", None, a, now - 7200, None)
    store.finish_ingest_run(run, now - 7000, 10, 10, {})
    hits = [{"freq_hz": 168_500_000, "snr_db": 20}, {"freq_hz": 168_512_500, "snr_db": 18}, {"freq_hz": 171_000_000, "snr_db": 30},
            {"freq_hz": 453_000_000, "snr_db": 15}]
    targets = targets_from_store(store, cfg, hits, now)
    by = {t.freq_hz: t for t in targets}
    assert by[168_500_000].cap_site == 2 and by[168_500_000].last_decoded is not None and by[168_512_500].last_decoded is None
    assert "never decoded" in by[171_000_000].reasons
    slices = plan(targets, cfg, max_instances=2, sample_rate=2_400_000)
    first = slices[0]
    assert {t.freq_hz for t in first.targets} == {168_500_000, 168_512_500} and first.reason.startswith("same-site")
    assert first.dwell_s == 2 * cfg["decode"]["seconds_per_carrier"]
    assert all(max(t.freq_hz for t in s.targets) - min(t.freq_hz for t in s.targets) <= 2_400_000 * 0.8 for s in slices)
    assert sum(len(s.targets) for s in slices) == 4
    only = plan(targets_from_store(store, cfg, hits, now, only_freqs=[171_000_000, 171_012_500]), cfg, 2)
    assert {t.freq_hz for s in only for t in s.targets} == {171_000_000, 171_012_500}


def test_power_state_parsing_and_budget(cfg):
    ps = parse_state_file({"ext5v_v": 4.87, "throttled": "0x50005", "cap_mhz": 1800, "max_current_ma": 3000})
    assert ps.volts == 4.87 and ps.undervolt_now and ps.undervolt_ever and ps.supply_full is False and ps.capped
    assert budget_for(ps, cfg) == (1, cfg["power"]["battery_session_s"])
    ps2 = parse_state_file({"supply": {"full_supply": True}, "status": {"throttled": 0}, "volts": 5.1})
    assert ps2.supply_full is True and not ps2.on_battery and ps2.capped is False
    assert budget_for(ps2, cfg) == (cfg["decode"]["max_instances"], cfg["daemon"]["default_budget_min"] * 60)
    assert budget_for(PowerState(), cfg)[0] == cfg["power"]["battery_max_instances"]
    assert "UNDERVOLTAGE" in ps.short() and "mains" in ps2.short()


def test_state_roundtrip_and_stale_detection(store):
    assert not st.read_status(store).alive
    st.write_heartbeat(store, 999999, 1, "idle", None, {"status": "present"}, {}, {}, "hi")
    s = st.read_status(store)
    assert not s.alive and s.mode == "offline"           # pid 999999 does not exist
    import os
    st.write_heartbeat(store, os.getpid(), 1, "decode", 7, {"status": "present"}, {"volts": 5.0}, {"phase": "decoding"}, "x")
    s = st.read_status(store)
    assert s.alive and s.mode == "decode" and s.session_id == 7 and s.progress["phase"] == "decoding"
    cid = st.post_command(store, "tui", "stop")
    taken = st.take_command(store)
    assert taken and taken["id"] == cid and taken["kind"] == "stop" and st.take_command(store) is None
    st.finish_command(store, cid, True, "nothing running")
    assert st.command(store, cid)["status"] == "done"
    with pytest.raises(ValueError):
        st.post_command(store, "tui", "fly")
    sid = st.create_session(store, "survey", "tui", {"bands": ["x"]}, 60)
    st.start_session(store, sid)
    st.finish_session(store, sid, "done", {"hits": 3})
    assert st.sessions(store)[0]["summary"] == {"hits": 3}
    st.mark_offline(store, os.getpid())
    assert not st.read_status(store).alive


def test_file_backend_replay(tmp_path, cfg):
    src = SyntheticSource(2_400_000, 165_000_000, [SynthCarrier(165_300_000, 30, None)])
    out = tmp_path / "iq.cf32"
    n = capture_iq(src, 165_000_000, 0.3, out)
    assert n == 720_000 and out.stat().st_size == n * 8
    c = dict(cfg)
    c["sdr"] = dict(cfg["sdr"], sample_rate_hz=2_400_000, source="file", replay={"path": str(out), "format": "cf32", "center_hz": 165_000_000})
    b = FileBackend(c)
    assert b.probe().status == "present"
    fs = b.open()
    x = fs.read(1_000_000)          # longer than the file: wraps
    assert len(x) == 1_000_000 and x.dtype == np.complex64
    hits = analyse_block(x, 2.4e6, 165e6, 12_500, 0.8, 0.2, 9.0)
    assert any(h.freq_hz == 165_300_000 for h in hits)
    # a sweep over the replay labels hits with the recording's centre, whatever the step centre
    swept = sweep(fs, {"name": "r", "start_hz": 164_100_000, "stop_hz": 165_900_000, "raster_hz": 12_500},
                  {**c, "survey": dict(c["survey"], dwell_s=0.2, segment_s=0.1)})
    assert 165_300_000 in {h.freq_hz for h in swept}
    fs.close()
    c["sdr"]["replay"]["path"] = str(tmp_path / "missing.cf32")
    assert FileBackend(c).probe().status == "absent"


def test_scheduler_skips_carriers_with_no_dmr_and_stray_capplus_sites(store, cfg):
    now = int(time.time())
    silent = store.upsert_carrier(421_162_500, now)
    run = store.start_ingest_run("collector", None, silent, now - 400, None)
    store.finish_ingest_run(run, now - 300, 3, 3, {})            # 100 s of decoding, nothing heard
    t3 = store.upsert_carrier(469_062_500, now)
    store.conn.execute("UPDATE carriers SET variant = 'tier3', dominant_cc = 1 WHERE id = ?", (t3,))
    store.add_csbk_event(t3, now, 1, 1, "capplus_site", {"site": 0, "rest_lsn": 0, "rs": "00"}, "0")
    hits = [{"freq_hz": 421_162_500, "snr_db": 20}, {"freq_hz": 469_062_500, "snr_db": 20}]
    by = {t.freq_hz: t for t in targets_from_store(store, cfg, hits, now)}
    assert by[421_162_500].score <= 0 and any("no DMR" in r for r in by[421_162_500].reasons)
    assert by[469_062_500].cap_site is None                     # a Cap+ line on a Tier III carrier is not a Cap+ site
    planned = {t.freq_hz for s in plan(list(by.values()), cfg, 2) for t in s.targets}
    assert planned == {469_062_500}
    # asked for by frequency, it is decoded anyway
    only = targets_from_store(store, cfg, hits, now, only_freqs=[421_162_500])
    assert {t.freq_hz for s in plan(only, cfg, 2) for t in s.targets} == {421_162_500}
