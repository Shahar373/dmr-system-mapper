"""Campaigns (a chain of survey, decode and watch sessions at one stop) and the coverage report."""

from __future__ import annotations

import copy
import threading
import time

import pytest
from conftest import CAPPLUS_LOG
from test_collector_daemon import FAKE, _audio

from dmr_mapper.collector import campaign as cm
from dmr_mapper.collector import state as st
from dmr_mapper.collector.daemon import Daemon
from dmr_mapper.collector.sdr import SyntheticBackend, SyntheticSource, SynthCarrier
from dmr_mapper.correlate import run as correlate
from dmr_mapper.db import Store
from dmr_mapper.export.coverage import coverage
from dmr_mapper.export.html import render
from dmr_mapper.export.graph import build
from dmr_mapper.ingest import ingest_log_file

HERE = (32.0800, 34.7800)
THERE = (32.1700, 34.7800)          # 10 km north


def _run(store: Store, band: str, started: int, hits: dict[int, float], at=None, settings=None) -> int:
    rid = store.add_survey_run(band, started, started + 60, "test", settings or {"dwell_s": 3})
    for f, snr in hits.items():
        store.add_survey_hit(rid, f, snr_db=snr, p95_snr_db=snr, occupancy_pct=50, width_hz=8000, **{"class": "narrow"},
                             lat=at[0] if at else None, lon=at[1] if at else None)
    store.commit()
    return rid


# -- planning --------------------------------------------------------------------------------------
def test_cycles_and_the_survey_share(cfg):
    now = 1_800_000_000
    assert cm.new_campaign(180 * 60, cfg, now)["cycles"] == 4
    assert cm.new_campaign(45 * 60, cfg, now)["cycles"] == 1
    c = cm.new_campaign(20 * 60, cfg, now)
    assert c["cycles"] == 1 and c["deadline"] == now + 1200
    camp = cm.new_campaign(45 * 60, cfg, now)
    kind, params = cm.survey_step(camp, cfg, now)
    # 15 % of 45 min is 6.75 min; a sweep of both default bands at 3 s per step is 2.7 min
    assert kind == "start_survey" and params["sweeps"] == 2 and params["campaign"]["step"] == "survey"
    assert params["budget_s"] < 8 * 60
    _, p8 = cm.survey_step(cm.new_campaign(45 * 60, cfg, now, dwell_s=8), cfg, now)
    assert p8["sweeps"] == 1 and p8["dwell_s"] == 8


def test_nothing_to_watch_gives_the_decode_the_whole_cycle(store, cfg):
    now = int(time.time())
    camp = cm.new_campaign(45 * 60, cfg, now)
    kind, params = cm.next_step(store, cfg, camp, "survey", now + 300)
    assert kind == "start_decode" and params["campaign"]["watch_target"] is None
    assert abs(params["budget_s"] - (45 * 60 - 300)) <= 1
    # after the decode: no watch, the only cycle is over and nothing fits any more
    assert cm.next_step(store, cfg, params["campaign"], "decode", now + 45 * 60 - 30) is None


def test_an_intermittent_slice_is_watched_after_the_decode(store, cfg):
    now = int(time.time())
    # three sweeps here: 456.85 and 457.2 heard in only some of them, 458.0 in all
    _run(store, "uhf", now - 900, {456_850_000: 20, 458_000_000: 15}, at=HERE)
    _run(store, "uhf", now - 600, {457_200_000: 12, 458_000_000: 15}, at=HERE)
    _run(store, "uhf", now - 300, {458_000_000: 16}, at=HERE)
    camp = cm.new_campaign(45 * 60, cfg, now - 950)
    kind, params = cm.next_step(store, cfg, camp, "survey", now, near=HERE)
    target = params["campaign"]["watch_target"]
    assert kind == "start_decode" and target and "only some" in target["reason"]
    assert 456_850_000 <= target["center_hz"] <= 457_200_000
    watch_s = 0.35 * 45 * 60
    assert abs(params["budget_s"] - (45 * 60 - 950 - watch_s)) <= 2
    kind, wp = cm.next_step(store, cfg, params["campaign"], "decode", now + params["budget_s"], near=HERE)
    assert kind == "start_watch" and wp["center_hz"] == target["center_hz"] and wp["campaign"]["watched"] == [target["center_hz"]]
    # the same slice is not chosen again while another one is worth a look
    assert cm.choose_watch(store, cfg, wp["campaign"], HERE) == target          # nothing else here: the same one
    # far from those sweeps, nothing is intermittent
    assert cm.choose_watch(store, cfg, camp, THERE) is None


def test_the_next_cycle_starts_with_a_survey(store, cfg):
    now = int(time.time())
    camp = cm.new_campaign(90 * 60, cfg, now)
    assert camp["cycles"] == 2
    kind, params = cm.next_step(store, cfg, {**camp, "step": "watch"}, "watch", now + 45 * 60)
    assert kind == "start_survey" and params["campaign"]["cycle"] == 2
    # the last cycle ended early with time left: one more cycle in the time that is left
    last = {**camp, "cycle": 2}
    kind, params = cm.next_step(store, cfg, last, "watch", now + 60 * 60)
    assert kind == "start_survey" and params["campaign"]["cycle"] == 3 and params["campaign"]["cycles"] == 3


def test_a_campaign_runs_as_a_chain_of_sessions(tmp_path, cfg):
    c = copy.deepcopy(cfg)
    c["survey"]["bands"] = [{"name": "test", "start_hz": 164_400_000, "stop_hz": 166_000_000, "raster_hz": 12_500}]
    c["survey"].update(dwell_s=0.5, segment_s=0.25)
    c["decode"].update({"dsd_fme_bin": str(FAKE), "seconds_per_carrier": 4, "max_instances": 2, "tcp_base_port": 7555, "invert_retry_s": 2})
    c["daemon"].update({"tick_s": 0.2, "sdr_probe_interval_s": 1})
    c["power"]["state_file"] = ""
    c["campaign"] = {"cycle_minutes": 0.75, "min_decode_s": 5, "min_watch_s": 5}
    audio = _audio()
    src = SyntheticSource(2_400_000, 165_000_000, [SynthCarrier(165_300_000, 30, audio), SynthCarrier(164_512_500, 25, None)],
                          realtime=True)
    db = tmp_path / "c.db"
    with Store(db) as s:
        s.upsert_carrier(165_300_000, int(time.time()), "vhf-high")      # known: a slice worth a watch
        s.commit()
    d = Daemon(db, c, SyntheticBackend(src))
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    client = Store(db)
    try:
        time.sleep(0.6)
        cid = st.post_command(client, "test", "start_campaign", {"budget_s": 45})
        assert st.wait_for_command(client, cid, 30)["status"] == "done"
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            sessions = st.sessions(client, 10)
            last = sessions[0]
            if last["status"] not in ("running", "starting", "pending") and last["summary"].get("campaign_done"):
                break
            time.sleep(0.5)
        sessions = list(reversed(st.sessions(client, 10)))
        kinds = [x["kind"] for x in sessions]
        assert kinds[:3] == ["survey", "decode", "watch"], [(x["kind"], x["status"], x["summary"].get("campaign_next")) for x in sessions]
        ids = {x["params"]["campaign"]["id"] for x in sessions}
        assert len(ids) == 1
        assert sessions[-1]["summary"].get("campaign_done"), sessions[-1]["summary"]
        assert all(x["status"] == "done" for x in sessions), [(x["kind"], x["status"]) for x in sessions]
        assert sessions[0]["summary"]["campaign_next"].startswith("decode")
        # the campaign's last act: an export with the coverage section
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ex = client.conn.execute("SELECT status FROM commands WHERE kind = 'export' ORDER BY id DESC").fetchone()
            if ex and ex["status"] in ("done", "failed"):
                break
            time.sleep(0.3)
        assert ex and ex["status"] == "done"
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


def test_a_stopped_step_ends_the_campaign(tmp_path, cfg):
    c = copy.deepcopy(cfg)
    c["survey"]["bands"] = [{"name": "test", "start_hz": 164_400_000, "stop_hz": 174_000_000, "raster_hz": 12_500}]
    c["survey"].update(dwell_s=1, segment_s=0.5)
    c["decode"]["dsd_fme_bin"] = str(FAKE)
    c["daemon"].update({"tick_s": 0.2, "sdr_probe_interval_s": 1})
    c["power"]["state_file"] = ""
    src = SyntheticSource(2_400_000, 165_000_000, [SynthCarrier(165_300_000, 30, None)], realtime=True)
    db = tmp_path / "s.db"
    d = Daemon(db, c, SyntheticBackend(src))
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    client = Store(db)
    try:
        time.sleep(0.6)
        assert st.wait_for_command(client, st.post_command(client, "test", "start_campaign", {"budget_s": 600}), 30)["status"] == "done"
        time.sleep(1.5)
        st.wait_for_command(client, st.post_command(client, "test", "stop"), 30)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and st.sessions(client, 1)[0]["status"] in ("running", "starting"):
            time.sleep(0.2)
        time.sleep(1.5)
        sessions = st.sessions(client, 5)
        assert len(sessions) == 1 and sessions[0]["status"] == "stopped" and "campaign_next" not in sessions[0]["summary"]
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


# -- coverage --------------------------------------------------------------------------------------
def test_coverage_per_stop(tmp_path, cfg):
    with Store(tmp_path / "cov.db") as s:
        ingest_log_file(s, CAPPLUS_LOG, cfg)
        s.commit()
        correlate(s, cfg, now=1_778_400_000)          # Cap+ site 2 heard, its neighbour site 1 only announced
        now = int(time.time())
        cap = 168_863_000
        _run(s, "vhf-high", now - 7200, {cap: 14, 165_625_000: 11}, at=THERE)
        _run(s, "vhf-high", now - 3000, {cap: 25, 164_325_000: 10}, at=HERE)
        _run(s, "vhf-high", now - 2400, {cap: 24}, at=HERE)
        _run(s, "uhf", now - 2000, {458_575_000: 12}, at=HERE, settings={"dwell_s": 8, "partial": True})
        _run(s, "vhf-high", now - 100, {170_000_000: 9})
        rep = coverage(s, cfg)
    stops = rep["stops"]
    assert [x["position"] for x in stops] == [list(THERE), list(HERE), None]
    there, here, nowhere = stops
    why = {g["band"]: g["why"] for g in there["gaps"]}
    assert there["bands"]["vhf-high"]["full"] == 1 and why["vhf-high"].startswith("swept once") and why["uhf"].startswith("never")
    assert here["bands"]["vhf-high"] == {"full": 2, "partial": 0, "dwell_s": [3.0], "listen_s_per_step": 6.0}
    assert here["bands"]["uhf"] == {"full": 0, "partial": 1, "dwell_s": [8.0], "listen_s_per_step": 0.0}
    assert {g["band"] for g in here["gaps"]} == {"uhf"}                    # swept twice in full on VHF here
    assert here["intermittent"] == [164_325_000]
    top = here["carriers"][0]
    assert top["freq_hz"] == cap and top["best_snr_db"] == 25 and (top["heard_in"], top["sweeps"]) == (2, 2) and top["known_as"] == "capplus"
    assert nowhere["position"] is None and nowhere["carriers_heard"] == 1
    site1 = rep["unheard_sites"]
    assert len(site1) == 1 and site1[0]["site_number"] == 1
    assert site1[0]["system_carriers"][0] == {"freq_hz": cap, "best_stop": 2, "best_snr_db": 25.0}
    assert "stop 2" in site1[0]["hint"] and "not a direction" in site1[0]["hint"]


def test_map_html_has_the_coverage_section(tmp_path, cfg):
    with Store(tmp_path / "h.db") as s:
        ingest_log_file(s, CAPPLUS_LOG, cfg)
        s.commit()
        correlate(s, cfg, now=1_778_400_000)
        now = int(time.time())
        _run(s, "vhf-high", now - 600, {168_863_000: 20}, at=HERE)
        _run(s, "vhf-high", now - 300, {168_863_000: 21, 165_000_000: 10}, at=THERE)
        page = render(build(s), coverage=coverage(s, cfg))
        assert '<section id="coverage">' not in render(build(s))
    assert '<section id="coverage">' in page and "Sites announced but never heard" in page
    assert page.count('class="stop"') == 2 and "km across" in page
    assert "<script" in page.split('<section id="coverage">')[1]          # the section sits before the script


def test_mcp_get_coverage(tmp_path, cfg):
    from dmr_mapper.mcp.tools import MapperTools

    db = tmp_path / "m.db"
    with Store(db) as s:
        _run(s, "uhf", int(time.time()) - 60, {456_850_000: 20}, at=HERE)
    rep = MapperTools(cfg, db).get_coverage(days=1)
    assert rep["stops"][0]["first"].startswith("20") and rep["stops"][0]["carriers"][0]["freq_hz"] == 456_850_000
    with pytest.raises(ValueError):
        MapperTools(cfg, db).get_coverage(days=0)
