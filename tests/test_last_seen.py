"""A carrier's seen range means "DMR heard", never "listened to" (field campaign 2026-11-03, decode
session 14: six carriers at the noise floor, 20 lines and 0 syncs each in 120 s, were shown as
`last 2026-11-03`).

Listening alone (a live decode, an offline log) must not move ``first_seen`` / ``last_seen``; decoded
DMR moves them to its burst times; a key-up of the watch is RF evidence and still moves them."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime

from dmr_mapper.collector.ingest import CarrierIngest
from dmr_mapper.collector.scheduler import targets_from_store
from dmr_mapper.ingest import ParseContext
from dmr_mapper.ingest.runner import ingest_lines, system_tz

from conftest import REPO, TZ

FREQ = 469_062_500
DAY = 86400

NOISE = ["Sync: no sync\n"] * 20
DSTAR_ON_NOISE = "16:50:30 Sync: +DSTAR HEADER  RPT 2: x RPT 1: y DST: z SRC: w\n"   # a false sync, not DMR


def _dmr(clock: str) -> list[str]:
    return [f"{clock} Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n",
            " Capacity Plus Channel Status - FL: 3 TS: 0 RS: 0 - Rest LSN: 1 - Single Block\n"]


def _carrier(store, freq: int = FREQ):
    return store.rows("SELECT * FROM carriers WHERE freq_hz = ?", (freq,))[0]


def _live(store, cfg, lines: list[str]):
    ing = CarrierIngest(store, cfg, FREQ)
    for line in lines:
        ing.feed(line)
    return ing.finish()


# -- live decode (collector) -----------------------------------------------------------------------
def test_silent_live_decode_leaves_last_seen(store, cfg):
    old = int(time.time()) - 10 * DAY
    cid = store.upsert_carrier(FREQ, old)
    store.commit()
    st = _live(store, cfg, NOISE)
    c = _carrier(store)
    assert st.lines == 20 and st.heard_first_ts is None
    assert c["id"] == cid and c["first_seen"] == old and c["last_seen"] == old
    # the decode is still on record: the scheduler reads "last decoded" from the ingest run
    assert store.scalar("SELECT COUNT(*) FROM ingest_runs WHERE carrier_id = ? AND source = 'collector'", (cid,)) == 1


def test_silent_live_decode_of_a_new_carrier_is_never_heard(store, cfg):
    _live(store, cfg, NOISE)
    c = _carrier(store)
    assert c["first_seen"] == 0 and c["last_seen"] == 0


def test_live_decode_with_syncs_advances_to_the_bursts(store, cfg):
    old = int(time.time()) - 10 * DAY
    store.upsert_carrier(FREQ, old)
    store.commit()
    now = datetime.now(tz=system_tz())
    lines = NOISE + _dmr(f"{now:%H:%M:%S}") + NOISE
    t0 = int(time.time())
    st = _live(store, cfg, lines)
    c = _carrier(store)
    assert st.bursts and st.heard_last_ts is not None
    assert abs(st.heard_last_ts - t0) <= 2                 # the burst's own time, not the start of the decode
    assert c["first_seen"] == old and c["last_seen"] == st.heard_last_ts


def test_scheduler_lets_a_silent_carrier_go_stale(store, cfg):
    """A carrier last heard 40 days ago is out of the 30-day target list; a silent decode does not
    bring it back (before the fix it did, so a dead carrier was decoded again and again)."""
    now = int(time.time())
    store.upsert_carrier(FREQ, now - 40 * DAY)
    store.commit()
    _live(store, cfg, NOISE)
    assert FREQ not in {t.freq_hz for t in targets_from_store(store, cfg, [], now=now)}
    # a survey hit still makes it a target, as for any carrier
    assert FREQ in {t.freq_hz for t in targets_from_store(store, cfg, [{"freq_hz": FREQ, "snr_db": 12.0}], now=now)}


# -- offline log ingest -----------------------------------------------------------------------------
def _offline(store, cfg, lines: list[str]):
    ctx = ParseContext(freq_hz=FREQ, start_time=datetime(2026, 5, 9, 16, 45, 0, tzinfo=TZ), source="dsdfme_log",
                       time_is_rf=True, tz=TZ)
    return ingest_lines(store, lines, ctx, cfg, path="x.log")


def test_offline_log_without_dmr_leaves_last_seen(store, cfg):
    old = int(datetime(2026, 5, 1, tzinfo=TZ).timestamp())
    store.upsert_carrier(FREQ, old)
    store.commit()
    st = _offline(store, cfg, NOISE + [DSTAR_ON_NOISE] + NOISE)
    c = _carrier(store)
    assert st.first_ts is not None and st.heard_first_ts is None    # the D-STAR line has a time, but is no DMR
    assert c["first_seen"] == old and c["last_seen"] == old


def test_offline_log_with_dmr_sets_the_burst_times(store, cfg):
    st = _offline(store, cfg, NOISE + _dmr("16:46:10") + NOISE + _dmr("16:48:20") + [DSTAR_ON_NOISE])
    c = _carrier(store)
    first = int(datetime(2026, 5, 9, 16, 46, 10, tzinfo=TZ).timestamp())
    last = int(datetime(2026, 5, 9, 16, 48, 20, tzinfo=TZ).timestamp())
    # not the log's start time (16:45:00), not the trailing false sync (16:50:30)
    assert (st.heard_first_ts, st.heard_last_ts) == (first, last)
    assert (c["first_seen"], c["last_seen"]) == (first, last)


# -- the store and the readers --------------------------------------------------------------------
def test_carrier_id_and_seen_range(store):
    cid = store.carrier_id(FREQ, "uhf")
    assert store.carrier_id(FREQ) == cid
    c = _carrier(store)
    assert (c["first_seen"], c["last_seen"], c["band"]) == (0, 0, "uhf")
    store.upsert_carrier_seen(cid, 1000, 2000)                # the first sighting replaces "never heard"
    store.upsert_carrier_seen(cid, 1500, 1800)
    c = _carrier(store)
    assert (c["first_seen"], c["last_seen"]) == (1000, 2000)
    assert store.upsert_carrier(FREQ, 3000) == cid             # a key-up: RF evidence moves it
    assert _carrier(store)["last_seen"] == 3000


def test_status_shows_never_heard(tmp_path, cfg):
    from dmr_mapper.db import Store

    db = tmp_path / "s.db"
    with Store(db) as s:
        s.carrier_id(FREQ)
        s.commit()
    r = subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--db", str(db), "status"], capture_output=True, text=True,
                       cwd=REPO, check=True)
    line = next(ln for ln in r.stdout.splitlines() if "469.0625" in ln)
    assert "never heard" in line and "1970" not in line
