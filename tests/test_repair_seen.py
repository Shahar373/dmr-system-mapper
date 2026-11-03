"""`mapper repair-seen`: take back the seen range that listening added before 2026-11-03 (DESIGN.md §12)."""

from __future__ import annotations

import json
import subprocess
import sys

from dmr_mapper.db import Store
from dmr_mapper.db.repair import repair_seen

from conftest import REPO

DAY = 86400
NOW = 1_793_000_000 - (1_793_000_000 % 900)       # a bucket boundary, for readable expectations
BUCKET = 900                                       # config.example: ingest.bucket_minutes 15


def _polluted(store: Store, freq: int, first: int, last: int) -> int:
    """A carrier as the old code left it: its range set by a decode that heard nothing."""
    cid = store.carrier_id(freq)
    store.conn.execute("UPDATE carriers SET first_seen = ?, last_seen = ? WHERE id = ?", (first, last, cid))
    return cid


def _heard(store: Store, cid: int, bucket_start: int, bursts: int = 120) -> None:
    store.add_observation(cid, bucket_start, "collector", {"bursts": bursts, "csbk": bursts, "no_sync": 0 if bursts else 20,
                                                       "cc_histogram": {"2": bursts} if bursts else {}})


def _seen(store: Store, cid: int) -> tuple[int, int]:
    r = store.rows("SELECT first_seen, last_seen FROM carriers WHERE id = ?", (cid,))[0]
    return int(r["first_seen"]), int(r["last_seen"])


def test_last_seen_back_to_the_last_dmr(store, cfg):
    heard_at = NOW - 10 * DAY
    cid = _polluted(store, 469_350_000, heard_at - 3600, NOW - 60)        # silent decode a minute ago
    _heard(store, cid, heard_at)
    _heard(store, cid, NOW - 2 * BUCKET, bursts=0)                          # the silent decode's own bucket: no DMR
    fixes = repair_seen(store, cfg, now=NOW)
    assert [f.freq_hz for f in fixes] == [469_350_000]
    assert _seen(store, cid) == (heard_at, heard_at + BUCKET - 1)        # to the end of the last bucket with DMR
    assert "after it was last heard" in fixes[0].note


def test_precise_events_and_keyups_count(store, cfg):
    cid = _polluted(store, 469_062_500, NOW - 5 * DAY, NOW - 60)
    store.add_csbk_event(cid, NOW - 4 * DAY, 1, 2, "capplus_site", {"site": 1, "rest_lsn": 1}, "s1")
    store.conn.execute("INSERT INTO keyups (carrier_id, session_id, start, end, peak_snr_db, decoded, dmr_heard)"
                       " VALUES (?, NULL, ?, ?, 20, 0, 0)", (cid, NOW - 3 * DAY, NOW - 3 * DAY + 4.5))
    repair_seen(store, cfg, now=NOW)
    assert _seen(store, cid) == (NOW - 4 * DAY, NOW - 3 * DAY + 4)      # the first CSBK; a key-up is RF evidence, DMR or not


def test_first_seen_forward_to_the_first_dmr(store, cfg):
    cid = _polluted(store, 456_850_000, NOW - 20 * DAY, NOW - 2 * DAY)   # first decode heard nothing
    _heard(store, cid, NOW - 8 * DAY)
    _heard(store, cid, NOW - 2 * DAY - BUCKET)
    repair_seen(store, cfg, now=NOW)
    assert _seen(store, cid) == (NOW - 8 * DAY, NOW - 2 * DAY - 1)


def test_only_listened_to_becomes_never_heard(store, cfg):
    cid = _polluted(store, 456_912_500, NOW - DAY, NOW - DAY + 120)
    store.start_ingest_run("collector", None, cid, NOW - DAY, None)       # the run stays: "last decoded"
    _heard(store, cid, NOW - DAY, bursts=0)
    fixes = repair_seen(store, cfg, now=NOW)
    assert _seen(store, cid) == (0, 0) and fixes[0].note.startswith("never heard")


def test_older_than_keyup_retention_is_left(store, cfg):
    old = NOW - 400 * DAY                     # its key-ups may have been pruned (keyup_days 180)
    cid = _polluted(store, 469_800_000, old, old + 60)
    assert repair_seen(store, cfg, now=NOW) == [] and _seen(store, cid) == (old, old + 60)


def test_already_right_and_idempotent(store, cfg):
    a = store.carrier_id(469_462_500)
    _heard(store, a, NOW - DAY)
    store.upsert_carrier_seen(a, NOW - DAY + 30, NOW - DAY + 600)          # as the fixed writer stores it
    b = _polluted(store, 469_350_000, NOW - 9 * DAY, NOW - 60)
    _heard(store, b, NOW - 9 * DAY)
    store.carrier_id(469_650_000)                                           # never heard already
    assert [f.carrier_id for f in repair_seen(store, cfg, now=NOW)] == [b]
    assert repair_seen(store, cfg, now=NOW) == []
    assert _seen(store, a) == (NOW - DAY + 30, NOW - DAY + 600)


def test_dry_run_writes_nothing(store, cfg):
    cid = _polluted(store, 469_350_000, NOW - DAY, NOW - 60)
    assert len(repair_seen(store, cfg, now=NOW, apply=False)) == 1
    assert _seen(store, cid) == (NOW - DAY, NOW - 60)


def test_cli(tmp_path):
    import time

    db = tmp_path / "s.db"
    now = int(time.time())
    with Store(db) as s:
        cid = _polluted(s, 469_350_000, now - DAY, now - 60)
        s.commit()
    cmd = [sys.executable, "-m", "dmr_mapper.cli", "--db", str(db), "repair-seen"]
    r = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True, cwd=REPO, check=True)
    assert "469.3500" in r.stdout and "-> " in r.stdout and "never .. never" in r.stdout and "would change 1" in r.stdout
    r = subprocess.run(cmd + ["--json"], capture_output=True, text=True, cwd=REPO, check=True)
    out = json.loads(r.stdout)
    assert out["applied"] and out["carriers"][0]["new"] == [0, 0]
    with Store(db) as s:
        assert _seen(s, cid) == (0, 0)


def test_capplus_sample_after_a_silent_decode(store, cfg):
    """The real sample, then a silent decode as the old code recorded it: back to the sample's last bucket."""
    from dmr_mapper.ingest import ingest_log_file

    from conftest import CAPPLUS_LOG

    st = ingest_log_file(store, CAPPLUS_LOG, cfg)
    cid = st.carrier_id
    store.conn.execute("UPDATE carriers SET last_seen = ? WHERE id = ?", (NOW, cid))
    repair_seen(store, cfg, now=NOW + 60)
    first, last = _seen(store, cid)
    assert first == st.heard_first_ts                                      # already right: untouched
    assert st.heard_last_ts <= last < st.heard_last_ts + BUCKET            # to the bucket of the last DMR burst
