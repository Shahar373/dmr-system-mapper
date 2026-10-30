"""Phase 4: watching a whole slice. Planning, key-up detection and decoder allocation as units,
then a watch session end to end: synthetic receiver with keyed transmitters, fake dsd-fme."""

from __future__ import annotations

import copy
import socket
import threading
import time

import numpy as np
import pytest

from dmr_mapper.collector import state as st
from dmr_mapper.collector.bridge import PcmServer
from dmr_mapper.collector.channelizer import Channelizer
from dmr_mapper.collector.daemon import Daemon
from dmr_mapper.collector.power import PowerState
from dmr_mapper.collector.sdr import SyntheticBackend, SyntheticSource, SynthCarrier
from dmr_mapper.collector.watch import InstancePlanner, KeyupDetector, plan_watch, watch_instances
from dmr_mapper.db import Store

from test_collector_daemon import FAKE, _audio, _wait, _wait_idle

NOW = int(time.time())


def _tier3(store: Store, freq: int, source: str, net: int = 2, sub: int = 2) -> int:
    cid = store.upsert_carrier(freq, NOW - 600)
    store.conn.execute("UPDATE carriers SET variant = 'tier3', variant_confidence = 0.9, dominant_cc = 1 WHERE id = ?", (cid,))
    payload = {"source": source, "model": "Large", "net_id": net, "site_id": 1, "sub_site": sub, "sys_code": f"21{sub - 1:02d}"}
    for _ in range(5):
        store.add_csbk_event(cid, NOW - 300, 1, 1, "tier3_sys_parms", payload, "k")
    return cid


def test_watch_instances_follow_the_power_supply(cfg):
    assert watch_instances(PowerState(supply_full=True), cfg) == 6
    assert watch_instances(PowerState(supply_full=False), cfg) == 3
    assert watch_instances(PowerState(supply_full=False, undervolt_now=True), cfg) == 1


def test_plan_picks_the_tier3_network_and_pins_control_channels(store, cfg):
    for f in (469_350_000, 469_650_000):
        _tier3(store, f, "slc_c_sys_parms", sub=2 if f == 469_350_000 else 3)
    for f in (469_062_500, 469_200_000):
        _tier3(store, f, "slc_p_sys_parms")
    _tier3(store, 465_000_000, "slc_c_sys_parms", net=5)        # another network, elsewhere
    store.commit()
    p = plan_watch(store, cfg, 2_000_000, limit=3)
    assert p.system == "tier3 Large net 1" and p.reason.startswith("Tier III")
    assert p.channels[0] <= 469_062_500 and p.channels[-1] >= 469_650_000
    assert all(abs(f - p.center_hz) >= 25_000 for f in p.channels)            # nothing on the receiver's DC point
    assert len(p.pinned) == 1 and p.pinned[0] in (469_350_000, 469_650_000)    # battery: one CC, two traffic
    assert len(plan_watch(store, cfg, 2_000_000, limit=6).pinned) == 2
    p = plan_watch(store, cfg, 2_000_000, limit=3, freqs=[469_350_000, 469_650_000])
    assert p.reason == "requested carriers" and len(p.pinned) == 1
    with pytest.raises(ValueError):
        plan_watch(store, cfg, 2_000_000, limit=3, freqs=[465_000_000, 469_350_000])


def test_plan_keeps_known_carriers_off_dc_and_off_the_raster(store, cfg):
    _tier3(store, 469_350_000, "slc_c_sys_parms")
    store.upsert_carrier(469_102_000, NOW - 600)               # 2 kHz off the 12.5 kHz raster (a kHz-named import)
    store.commit()
    p = plan_watch(store, cfg, 2_000_000, limit=3, center_hz=469_350_000)
    assert abs(p.center_hz - 469_350_000) >= 25_000 and p.pinned == [469_350_000] and not p.blind
    assert 469_102_000 in p.channels and 469_100_000 not in p.channels    # watched as itself, no raster twin
    assert p.priority[469_102_000] == 1 and p.priority[469_350_000] == 2


def test_requested_carriers_that_are_not_control_channels_are_not_pinned(store, cfg):
    p = plan_watch(store, cfg, 2_000_000, limit=2, freqs=[469_400_000, 469_500_000])
    assert p.pinned == [] and p.priority[469_400_000] == 0


def test_plan_without_anything_known_is_refused(store, cfg):
    with pytest.raises(ValueError, match="nothing known"):
        plan_watch(store, cfg, 2_000_000, limit=3)
    assert plan_watch(store, cfg, 2_000_000, limit=3, center_hz=165_000_000).center_hz != 0


def test_keyup_detector():
    freqs = [100_000 + 12_500 * i for i in range(20)]
    det = KeyupDetector(freqs, on_db=10, off_db=6, hang_s=1.0, min_blocks=2)
    noise = np.ones(20)

    def step(t, on=(), level=100.0):
        p = noise.copy()
        for i in on:
            p[i] = level
            p[i - 1] = p[i + 1] = max(p[i - 1], 3.0)  # its skirts, 5 dB over the floor: under the key-up threshold
        return det.update(t, p)

    assert step(0.0, (5,)) == ([], [])                # one block is not a key-up
    started, _ = step(0.1, (5,))
    assert started == [freqs[5]] and det.active() == {freqs[5]: 0.0}
    for k in range(2, 10):
        step(k / 10, (5,))
    step(1.0, (5, 12))
    assert step(1.1, (5, 12))[0] == [freqs[12]]
    _, ended = step(1.5, (12,))
    assert ended == []                                 # within the hang time (last heard at 1.1)
    assert step(2.0, (12,))[1] == []
    _, ended = step(2.2, (12,))
    assert len(ended) == 1 and ended[0].freq_hz == freqs[5] and ended[0].start == 0.0 and ended[0].end == pytest.approx(1.1)
    assert ended[0].peak_snr_db == 20.0
    assert [k.freq_hz for k in det.close()] == [freqs[12]]
    # a very strong carrier lifts its neighbour over the threshold: that is a skirt, not a key-up
    det = KeyupDetector(freqs, hang_s=1.0)
    p = noise.copy()
    p[5], p[6], p[9] = 1e5, 30.0, 30.0          # 50 dB carrier, its neighbour at 15 dB, a real 15 dB carrier elsewhere
    det.update(0.0, p)
    assert det.update(0.1, p)[0] == [freqs[5], freqs[9]]


def test_instance_planner_pins_preempts_and_cools_down():
    p = InstancePlanner(pinned=[1], limit=3, max_hold_s=30)
    assert p.plan(0, {}) == ([1], [])
    assert p.plan(1, {10: 1, 20: 1.5, 30: 2}) == ([30, 20], [])           # the most recent key-ups first
    assert p.waiting({10: 1, 20: 1.5, 30: 2}) == [10]
    assert p.plan(10, {10: 1, 20: 1.5, 30: 2}) == ([], [])               # nobody has held long enough
    assert p.plan(40, {10: 1, 20: 1.5, 30: 2}) == ([10], [20])           # held for 39 s: gives way
    assert p.plan(41, {10: 1, 20: 1.5, 30: 2}) == ([], [])               # and is not taken back while still keyed
    assert p.plan(42, {20: 1.5}) == ([], [30, 10])
    assert p.plan(43, {20: 1.5}) == ([], [])                              # still keyed since before: still waits
    assert p.plan(44, {20: 44}) == ([], [])
    assert p.plan(45, {}) == ([], [])
    assert p.plan(46, {20: 46}) == ([20], [])                             # keyed again: welcome back


def test_the_networks_traffic_channels_come_first():
    """Battery: one pinned control channel, two decoders for the rest. Two carriers of other systems
    are on the air all the time; a call on the network's traffic channel must still be heard."""
    p = InstancePlanner(pinned=[1], limit=3, max_hold_s=30, priority={50: 0, 60: 2, 70: 2})
    assert p.plan(0, {60: 0, 70: 0}) == ([1, 60, 70], [])
    start, stop = p.plan(5, {60: 0, 70: 0, 50: 5})
    assert start == [50] and len(stop) == 1                               # at once, not after 30 s
    assert p.plan(6, {60: 0, 70: 0, 50: 5}) == ([], [])                   # the one that gave way waits for its key-down
    assert p.plan(9, {60: 0, 70: 0}) == ([], [50])


def test_channel_power_finds_the_carrier():
    src = SyntheticSource(2_400_000, 165_000_000, [SynthCarrier(165_300_000, 25, None)])
    ch = Channelizer(src.sample_rate, src.center_hz)
    X, _ = ch.transform(src.read(ch.hop))
    freqs = np.arange(164_900_000, 165_400_001, 12_500)
    p = ch.channel_power(X, freqs)
    assert freqs[int(np.argmax(p))] == 165_300_000
    assert 10 * np.log10(p.max() / np.median(p)) > 20


def test_pcm_server_keeps_audio_until_the_client_connects():
    srv = PcmServer(7499, backlog_bytes=1000)
    try:
        srv.write(np.arange(300, dtype=np.int16))            # 600 bytes, nobody listening yet
        srv.write(np.arange(300, 600, dtype=np.int16))       # the oldest block is dropped past 1000 bytes
        c = socket.create_connection(("127.0.0.1", 7499), timeout=2)
        assert srv.wait_client(2)
        assert srv.write(np.arange(600, 610, dtype=np.int16))
        data = b""
        c.settimeout(2)
        while len(data) < 620:
            data += c.recv(4096)
        got = np.frombuffer(data, dtype=np.int16)
        assert list(got[:3]) == [300, 301, 302] and list(got[-3:]) == [607, 608, 609] and len(got) == 310
        c.close()
    finally:
        srv.close()


@pytest.fixture
def watch_cfg(cfg):
    c = copy.deepcopy(cfg)
    c["decode"].update({"dsd_fme_bin": str(FAKE), "tcp_base_port": 7555})
    c["daemon"].update({"tick_s": 0.2, "sdr_probe_interval_s": 1, "default_budget_min": 1})
    c["watch"] = {"hang_s": 1.0, "max_hold_s": 30}
    c["power"]["state_file"] = ""
    c["survey"]["bands"] = [{"name": "test", "start_hz": 164_000_000, "stop_hz": 166_000_000, "raster_hz": 12_500}]
    return c


def test_watch_session_decodes_channels_when_they_key_up(tmp_path, watch_cfg):
    db = tmp_path / "w.db"
    with Store(db) as s:
        _tier3(s, 165_300_000, "slc_c_sys_parms")
        _tier3(s, 165_600_000, "slc_p_sys_parms")
        s.commit()
    audio = _audio()
    src = SyntheticSource(2_400_000, 165_000_000, [
        SynthCarrier(165_300_000, 28, audio),                                  # control channel: always on
        SynthCarrier(165_600_000, 25, audio.copy(), on=[(1.5, 5.0), (7.0, 9.5)]),   # a traffic channel, two calls
        SynthCarrier(165_112_500, 22, audio.copy(), on=[(3.0, 6.0)]),          # a carrier nobody knew about
    ], realtime=True)
    d = Daemon(db, watch_cfg, SyntheticBackend(src))
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    client = Store(db)
    try:
        time.sleep(0.5)
        c = _wait(client, st.post_command(client, "test", "start_watch", {"budget_s": 13, "max_instances": 3}))
        assert c["status"] == "done", c
        deadline = time.monotonic() + 8
        seen_decoding = False
        while time.monotonic() < deadline:
            s = st.read_status(client, stale_s=5)
            chans = {x["freq_hz"]: x["state"] for x in s.progress.get("channels", [])}
            if chans.get(165_600_000) == "decoding":
                seen_decoding = True
                break
            time.sleep(0.2)
        assert s.mode == "watch" and s.progress.get("phase") == "watching", s.to_dict()
        assert seen_decoding and chans.get(165_300_000) == "pinned", s.progress
        _wait_idle(client, timeout=60)
        sess = st.sessions(client)[0]
        assert sess["kind"] == "watch" and sess["status"] == "done", sess
        summ = sess["summary"]
        assert summ["plan"]["pinned"] == [165_300_000] and summ["instances"] == 3
        rows = client.rows("SELECT c.freq_hz, k.start, k.end, k.decoded FROM keyups k JOIN carriers c ON c.id = k.carrier_id ORDER BY k.start")
        by = {}
        for r in rows:
            by.setdefault(int(r["freq_hz"]), []).append(r)
        assert len(by[165_600_000]) == 2 and all(r["decoded"] for r in by[165_600_000]), [dict(r) for r in rows]
        first = by[165_600_000][0]
        # keyed for 3.5 s; times are the wall clock when a block is processed, and the synthetic receiver
        # cannot always keep real time on a loaded test machine
        assert 2.5 <= first["end"] - first["start"] <= 6.5
        assert len(by[165_112_500]) == 1 and by[165_112_500][0]["decoded"]
        assert 165_300_000 in by                                              # the control channel, one long key-up
        assert client.scalar("SELECT COUNT(*) FROM carriers WHERE freq_hz = 165112500") == 1    # found by watching
        carriers = summ["carriers"]
        assert carriers["165600000"]["keyups"] == 2 and carriers["165600000"]["syncs"] > 50
        assert carriers["165300000"]["syncs"] > carriers["165600000"]["syncs"]
        assert summ["watch"]["missed"] == 0 and "correlate" in summ
        assert summ["watch"]["block_ms_avg"] > 0 and summ["watch"]["block_ms_budget"] == 100.0 and summ["watch"]["overflows"] == 0
        assert client.scalar("SELECT COUNT(*) FROM ingest_runs WHERE source = 'collector'") == 3   # one per carrier, not per key-up
        assert src.closed
        c = _wait(client, st.post_command(client, "test", "start_watch", {"freqs": [160_000_000, 165_300_000]}))
        assert c["status"] == "failed" and "does not fit" in c["result"], c
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()
