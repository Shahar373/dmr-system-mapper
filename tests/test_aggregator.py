"""Aggregator behaviour that the samples do not exercise (rest-channel suppression, live flush)."""

from __future__ import annotations

from datetime import datetime

from dmr_mapper.ingest import ParseContext, parse_lines
from dmr_mapper.ingest.writer import Aggregator, IngestStats

from conftest import TZ


def _run(store, text: str, bucket_minutes: int = 15, partial_before: int | None = None):
    ctx = ParseContext(freq_hz=168863000, start_time=datetime(2026, 5, 9, 12, 0, 0, tzinfo=TZ), tz=TZ)
    cid = store.upsert_carrier(168863000, 0)
    stats = IngestStats(source="dsdfme_log", path=None, freq_hz=168863000, carrier_id=cid)
    agg = Aggregator(store, cid, "dsdfme_log", bucket_minutes, stats)
    for ev in parse_lines(text.splitlines(), ctx):
        agg.feed(ev)
    agg.flush(partial_before=partial_before)
    return stats


def _status(clock: str, rest: int) -> str:
    return (f"{clock} Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n"
            f" Capacity Plus Channel Status - FL: 3 TS: 0 RS: 0 - Rest LSN: {rest} - Single Block\n")


def _slco(clock: str, rest: int) -> str:
    return (f"{clock} Sync: +DMR  [slot1]  slot2  | Color Code=02 | IDLE \n"
            f" SLCO Capacity Plus Site: 2 - Rest LSN: {rest} - RS: 00\n")


def test_stray_slco_is_suppressed_while_status_flows(store):
    # H2: a steady status for 21 s plus one lagging SLCO at second 20 must not create a 5→6→5 pair
    text = "".join(_status(f"12:00:{i:02d}", 5) for i in range(22)) + _slco("12:00:20", 6) + _status("12:00:21", 5)
    stats = _run(store, text)
    rows = store.rows("SELECT rest_lsn, prev_lsn FROM rest_events ORDER BY id")
    assert [(r["rest_lsn"], r["prev_lsn"]) for r in rows] == [(5, None)]
    assert stats.rest_transitions == 1


def test_slco_counts_when_status_is_absent(store):
    # Cap+ data channels announce the rest LSN only in the short LC
    text = _slco("12:00:00", 4) + _slco("12:00:30", 4) + _slco("12:01:00", 2)
    _run(store, text)
    rows = store.rows("SELECT rest_lsn, prev_lsn, site FROM rest_events ORDER BY id")
    assert [(r["rest_lsn"], r["prev_lsn"], r["site"]) for r in rows] == [(4, None, 2), (2, 4, 2)]


def test_rest_lsn_last_ignores_suppressed_slco(store):
    # M3
    text = _status("12:00:00", 5) + _status("12:00:01", 6) + _slco("12:00:02", 5)
    _run(store, text)
    assert store.scalar("SELECT rest_lsn_last FROM observations") == 6


def test_initial_block_does_not_churn_status_rows(store):
    # L1: Initial Block (no LSN list) + Final Block (full list) alternate without creating new rows
    text = ""
    for i in range(6):
        text += (f"12:00:{i:02d} Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n"
                 " Capacity Plus Channel Status - FL: 2 TS: 0 RS: 0 - Rest LSN: 5 - Initial Block\n"
                 f"12:00:{i:02d} Sync: +DMR   slot1  [slot2] | Color Code=02 | CSBK\n"
                 " Capacity Plus Channel Status - FL: 1 TS: 1 RS: 0 - Rest LSN: 5 - Final Block\n"
                 "  LSN 01:  Idle;  LSN 02:  Idle;  LSN 03: 1;  LSN 04:  Idle;  LSN 05:  Rest;\n")
    _run(store, text)
    assert store.scalar("SELECT COUNT(*) FROM csbk_events WHERE kind = 'capplus_channel_status'") <= 2
    assert store.scalar("SELECT SUM(count) FROM csbk_events WHERE kind = 'capplus_channel_status'") == 12


def test_partial_flush_writes_closed_buckets_only(store):
    text = _status("12:00:00", 5) + _status("12:20:00", 5)
    cutoff = int(datetime(2026, 5, 9, 12, 20, 0, tzinfo=TZ).timestamp())
    stats = _run(store, text, partial_before=cutoff)
    rows = store.rows("SELECT bucket_start FROM observations")
    assert len(rows) == 1 and rows[0]["bucket_start"] == int(datetime(2026, 5, 9, 12, 0, 0, tzinfo=TZ).timestamp())
    assert stats.structural["capplus_channel_status"] == 2
