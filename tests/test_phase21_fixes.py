"""Phase 2.1 review fixes: site ids on the upsert path, live commits, correlate output, config."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time

from dmr_mapper.config import load_config
from dmr_mapper.correlate.context import Context
from dmr_mapper.correlate.derive import derive_sites_and_systems
from dmr_mapper.correlate.scoring import ScoringConfig
from dmr_mapper.db import Store, canonical_json
from dmr_mapper.ingest import ingest_stream

from conftest import CAPPLUS_LOG, REPO


def _cap_site_event(store: Store, cid: int, ts: int, site: int) -> None:
    store.add_csbk_event(cid, ts, 1, 2, "capplus_site", {"site": site, "rest_lsn": 1, "rs": "00"}, f"{site}")


def test_two_components_with_the_same_site_number_share_one_site_row(store, cfg):
    """A, B are linked same_site; C announces the same Cap+ site number but is only reached through a
    same_system link, so it is a second component of the same system. Both components announce
    site 2 and must land on the SAME site row, with carrier_sites pointing at that row's real id
    (cursor.lastrowid is stale on the ON CONFLICT path)."""
    now = 1_800_000_000
    a = store.upsert_carrier(168_500_000, now)
    b = store.upsert_carrier(168_512_500, now)
    c = store.upsert_carrier(168_525_000, now)
    for cid in (a, b, c):
        _cap_site_event(store, cid, now, 2)
    store.conn.execute("UPDATE carriers SET variant = 'capplus', dominant_cc = 2 WHERE id IN (?, ?)", (a, b))
    store.conn.execute("UPDATE carriers SET variant = 'capplus', dominant_cc = 3 WHERE id = ?", (c,))
    # another, unrelated system first so that the previous INSERT's rowid differs from the shared site's id
    d = store.upsert_carrier(453_000_000, now)
    store.conn.execute("UPDATE carriers SET variant = 'tier2' WHERE id = ?", (d,))
    for a_id, b_id, kind in ((a, b, "same_site"), (a, c, "same_system")):
        store.conn.execute(
            "INSERT INTO links (a_carrier_id, b_carrier_id, kind, score, label, tier2_only, updated_at) VALUES (?, ?, ?, 0.9, 'probable', 0, ?)",
            (a_id, b_id, kind, now))
    derive_sites_and_systems(store, Context(store, now, 30), ScoringConfig(cfg), now, {})
    sites = {r["id"]: dict(r) for r in store.rows("SELECT * FROM sites")}
    members = {r["carrier_id"]: r["site_id"] for r in store.rows("SELECT carrier_id, site_id FROM carrier_sites")}
    assert members[a] == members[b] == members[c], (members, sites)
    assert sites[members[a]]["site_number"] == 2
    assert all(sid in sites for sid in members.values())          # no dangling site ids
    assert len(sites) == 2 and members[d] != members[a]


def test_live_ingest_commits_structural_events_before_the_flush(tmp_path, cfg):
    """Another connection must see structural rows while the stream is still running: the live
    ingest must not hold a write transaction open until its periodic flush."""
    db = tmp_path / "live.db"
    seen: list[int] = []

    def lines():
        with CAPPLUS_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            for i, raw in enumerate(fh):
                yield raw
                if i == 3000:
                    other = sqlite3.connect(str(db), timeout=1)
                    seen.append(other.execute("SELECT COUNT(*) FROM csbk_events").fetchone()[0])
                    # a concurrent writer must get in too (busy_timeout would otherwise expire)
                    t0 = time.monotonic()
                    other.execute("INSERT INTO carriers (freq_hz, first_seen, last_seen) VALUES (1, 1, 1)")
                    other.commit()
                    seen.append(int(time.monotonic() - t0 < 1))
                    other.close()
                if i > 6000:
                    break

    with Store(db) as s:
        ingest_stream(s, lines(), cfg, 168_863_000, "collector", flush_every_s=3600)
    assert seen and seen[0] > 0 and seen[1] == 1, seen


def test_cli_correlate_prints_none_when_nothing_was_collected(tmp_path):
    db = tmp_path / "c.db"
    env = {"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--db", str(db), "correlate"],
                       capture_output=True, text=True, cwd=REPO, env=env)
    assert r.returncode == 0 and "evidence collected: none" in r.stdout, r.stdout


def test_example_config_carries_window_days():
    cfg = load_config(REPO / "config.example.yaml")
    assert cfg["correlator"]["window_days"] == 30
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
