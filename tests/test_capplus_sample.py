"""End-to-end ingest of samples/capplus with the counts established in Phase 0."""

from __future__ import annotations

import json
from datetime import datetime

from dmr_mapper.ingest import ingest_log_file

from conftest import CAPPLUS_LOG, TZ


def test_counts(capplus_store):
    st = capplus_store.stats
    assert st.lines == 35975
    assert st.unrecognized_total == 0, st.unrecognized
    assert st.freq_hz == 168_863_000
    assert st.bursts["csbk"] == 9761
    assert st.bursts["voice"] == 545 + 163 + 132 + 131 + 131 + 131 + 130 + 115 + 17 + 1   # TLC VLC VC2..VC6 VC1 VC1* VC*
    assert st.bursts["data"] == 104 + 32 + 9 + 5
    assert st.no_sync == 295
    assert st.cc_histogram["2"] == 11404
    assert st.structural["capplus_channel_status"] == 9245
    assert st.structural["capplus_adjacent"] == 149
    assert st.structural["capplus_site"] == 2338
    assert st.structural["preamble"] == 89
    assert st.structural["lrrp"] == 25
    assert st.structural["call"] == 822
    assert st.rest_transitions == 46
    assert st.variant == "capplus" and st.variant_confidence >= 0.8
    assert st.untested_kinds == []


def test_time_range_is_local_to_utc(capplus_store):
    st = capplus_store.stats
    assert st.first_ts == int(datetime(2026, 5, 9, 16, 33, 44, tzinfo=TZ).timestamp())
    assert st.last_ts == int(datetime(2026, 5, 9, 16, 50, 30, tzinfo=TZ).timestamp())


def test_dedup_keeps_tables_small(capplus_store):
    s = capplus_store
    assert s.scalar("SELECT COUNT(*) FROM csbk_events") < 200
    status_rows = s.scalar("SELECT COUNT(*) FROM csbk_events WHERE kind = 'capplus_channel_status'")
    assert 46 <= status_rows <= 100          # initial blocks no longer churn the signature
    assert s.scalar("SELECT SUM(count) FROM csbk_events WHERE kind = 'capplus_channel_status'") == 9245
    assert s.scalar("SELECT COUNT(*) FROM csbk_events WHERE kind = 'capplus_adjacent'") == 2
    adj = [json.loads(r["payload"]) for r in s.rows("SELECT payload FROM csbk_events WHERE kind = 'capplus_adjacent'")]
    assert {"sites": [{"site": 1, "rest_lsn": 3}]} in adj and {"sites": [{"site": 1, "rest_lsn": 1}]} in adj
    assert s.scalar("SELECT COUNT(*) FROM csbk_events WHERE kind = 'capplus_site'") == 4   # site 2 x rest LSN 3,4,5,6


def test_rest_events(capplus_store):
    rows = capplus_store.rows("SELECT ts, site, rest_lsn, prev_lsn FROM rest_events ORDER BY ts, id")
    assert len(rows) == 46
    assert rows[0]["prev_lsn"] is None and rows[0]["rest_lsn"] == 5
    assert {r["rest_lsn"] for r in rows} == {3, 4, 5, 6}          # LSN 5/6 most of the time, 3/4 late in the log
    assert sum(r["rest_lsn"] in (5, 6) for r in rows) >= 40
    assert all(r["site"] == 2 for r in rows[2:])      # site known once the first SLCO was seen
    # the regular 30 s hop cadence: most transitions land on :14 or :44 seconds
    secs = [datetime.fromtimestamp(r["ts"], tz=TZ).second for r in rows]
    assert sum(sec in (14, 44) for sec in secs) >= 25


def test_observations_and_identities(capplus_store):
    s = capplus_store
    obs = s.rows("SELECT * FROM observations ORDER BY bucket_start")
    assert len(obs) == 2                       # 16:33-16:50 spans two 15-minute buckets
    assert sum(o["bursts"] for o in obs) == 11745
    assert sum(o["csbk"] for o in obs) == 9761
    assert all(o["cc_dominant"] == 2 and o["cc_consistency"] > 0.99 for o in obs)
    assert abs(sum(o["seconds_observed"] for o in obs) - 1007) < 5
    assert obs[-1]["rest_lsn_last"] in (3, 4, 5, 6)
    carrier = s.rows("SELECT * FROM carriers")[0]
    assert carrier["dominant_cc"] == 2 and carrier["variant"] == "capplus" and carrier["band"] == "vhf-high"
    # H3: first/last seen follow the data, not the start time
    assert carrier["first_seen"] == s.stats.first_ts and carrier["last_seen"] == s.stats.last_ts
    assert carrier["last_seen"] - carrier["first_seen"] == 1006
    ids = {(r["kind"], r["value"]) for r in s.rows("SELECT kind, value FROM identities")}
    # L10: TG 1 is a talkgroup, never a radio (DSD-FME writes TGT in Bank lines for group calls too)
    assert ("radio", 1) not in ids and ("tg", 1) in ids
    assert {("tg", 1), ("tg", 2102), ("radio", 199), ("radio", 64250), ("radio", 2102), ("radio", 2103)} <= ids
    roles = s.scalar("SELECT roles FROM identity_sightings s JOIN identities i ON i.id = s.identity_id WHERE i.kind='radio' AND i.value=199")
    assert "lrrp" in roles and "src" in roles


def test_ingest_is_idempotent(store, cfg, tmp_path):
    first = ingest_log_file(store, CAPPLUS_LOG, cfg)
    second = ingest_log_file(store, CAPPLUS_LOG, cfg)
    assert not first.skipped and second.skipped
    assert store.scalar("SELECT SUM(bursts) FROM observations") == 11745
    # H6: a copy at another path with the same content is recognised as already ingested
    copy = tmp_path / "dsd_fme_168863_20260509_163344.log"
    copy.write_bytes(CAPPLUS_LOG.read_bytes())
    assert ingest_log_file(store, copy, cfg).skipped
    third = ingest_log_file(store, CAPPLUS_LOG, cfg, force=True)
    assert not third.skipped
    assert store.scalar("SELECT SUM(bursts) FROM observations") == 2 * 11745   # --force re-adds on purpose


def test_growing_file_is_resumed_from_offset(store, cfg, tmp_path):
    # H6: a raw log still being appended to (tee -a) is ingested incrementally, never twice
    data = CAPPLUS_LOG.read_bytes()
    cut = data.index(b"\n", len(data) // 2) + 1
    part = tmp_path / "dsd_fme_168863_20260509_163344.log"
    part.write_bytes(data[:cut])
    first = ingest_log_file(store, part, cfg)
    assert not first.skipped
    part.write_bytes(data)
    second = ingest_log_file(store, part, cfg)
    assert not second.skipped and second.note.startswith("resumed at byte")
    assert first.lines + second.lines == 35975
    assert store.scalar("SELECT SUM(bursts) FROM observations") == 11745
    assert store.scalar("SELECT SUM(count) FROM csbk_events WHERE kind = 'capplus_channel_status'") == 9245
    assert ingest_log_file(store, part, cfg).skipped
