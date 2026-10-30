"""Phase 4: Capacity Max LCNs and neighbours in the correlator, on a synthetic two-site network
(Large model, net 1, sites 1 and 2 on air, "1.2" and "1.3" as DSD-FME prints them; neighbour site
3, "1.4", announced on LCN 5 but never heard). Shown with the on-air numbers, as SDRTrunk."""

from __future__ import annotations

from datetime import timedelta

import pytest

from dmr_mapper.correlate import explain, run
from dmr_mapper.ingest import ingest_log_file

from synth import START, capmax_site

NOW = int((START + timedelta(hours=1)).timestamp())
CC_A, CC_B = 469_350_000, 469_650_000
T7, T9, T4 = 469_062_500, 469_125_000, 469_200_000
FOREIGN = 469_500_000


@pytest.fixture
def capmax_store(tmp_path, store, cfg):
    # site 1.2: traffic on LCN 7 and 9; one call on TG 2102 goes out on both sites at once
    calls_a = [(20, 2102, 1234, 7, 1, 8), (60, 2103, 1235, 9, 2, 6), (100, 2102, 1236, 7, 1, 10), (150, 2104, 1237, 9, 1, 8),
               (200, 2105, 1238, 7, 2, 6)]
    paths_a, keyups_a = capmax_site(tmp_path / "a", net=1, site=1, cc=1, control_freq=CC_A, traffic={7: T7, 9: T9},
                                    calls=calls_a, neighbors=[(2, 6), (3, 5)], minutes=5)
    calls_b = [(100, 2102, 1236, 4, 1, 10), (230, 2106, 1239, 4, 2, 6)]
    paths_b, keyups_b = capmax_site(tmp_path / "b", net=1, site=2, cc=1, control_freq=CC_B, traffic={4: T4},
                                    calls=calls_b, neighbors=[(1, 3)], minutes=5)
    for p in paths_a + paths_b:
        ingest_log_file(store, p, cfg)
    # what the watch session recorded; another network's carrier in the slice keys up with the first call
    ids = {int(r["freq_hz"]): int(r["id"]) for r in store.rows("SELECT id, freq_hz FROM carriers")}
    ids[FOREIGN] = store.upsert_carrier(FOREIGN, keyups_a[0][1])
    for f, a, b in keyups_a + keyups_b + [(FOREIGN, keyups_a[0][1] + 1, keyups_a[0][1] + 4)]:
        store.conn.execute("INSERT INTO keyups (carrier_id, session_id, start, end, peak_snr_db, decoded) VALUES (?, 1, ?, ?, 20, 1)",
                           (ids[f], a, b))
    store.commit()
    run(store, cfg, now=NOW)
    return store


def _links(store):
    return {(r["a"], r["b"], r["kind"]): dict(r) for r in store.rows(
        "SELECT a.freq_hz AS a, b.freq_hz AS b, l.* FROM links l JOIN carriers a ON a.id = l.a_carrier_id JOIN carriers b ON b.id = l.b_carrier_id")}


def _link(links, f1, f2, kind):
    return links.get((f1, f2, kind)) or links.get((f2, f1, kind))


def test_traffic_channels_get_their_lcn(capmax_store, cfg):
    links = _links(capmax_store)
    for f, lcn, label in ((T7, 7, "confirmed"), (T9, 9, "probable")):        # three calls on LCN 7, two on LCN 9
        link = _link(links, f, CC_A, "same_site")
        assert link["label"] == label, link
        ev = next(e for e in explain(capmax_store, link["id"], cfg, now=NOW)["evidence"] if e["type"] == "behavioral_lcn_keyup")
        assert ev["details"]["lcn"] == lcn and ev["details"]["agreement"] == 1.0 and ev["raw"] > 0.5, ev
    lcns = {int(r["freq_hz"]): r["lcn"] for r in capmax_store.rows(
        "SELECT c.freq_hz, cs.lcn FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id")}
    assert lcns[T7] == 7 and lcns[T9] == 9 and lcns[T4] == 4
    # the control channels' own LCNs, from what the other site announces about them
    assert lcns[CC_B] == 6 and lcns[CC_A] == 3


def test_a_multisite_call_does_not_mix_up_the_sites(capmax_store, cfg):
    links = _links(capmax_store)
    for key, link in links.items():
        if T4 in key[:2] and CC_A in key[:2]:
            types = {e["type"] for e in explain(capmax_store, link["id"], cfg, now=NOW)["evidence"]}
            assert "behavioral_lcn_keyup" not in types, (key, types)
    # the foreign carrier only keyed up at the same moment once: never more than weak
    for key, link in links.items():
        if FOREIGN in key[:2]:
            assert link["label"] == "weak", link


def test_the_unheard_neighbour_site_carries_its_lcn(capmax_store):
    sites = {r["label"]: dict(r) for r in capmax_store.rows("SELECT * FROM sites")}
    unheard = [s for label, s in sites.items() if label.startswith("site 3")]
    assert len(unheard) == 1 and unheard[0]["directly_observed"] == 0
    assert "LCN 5" in unheard[0]["label"]
    systems = capmax_store.rows("SELECT * FROM systems WHERE variant = 'tier3'")
    assert len(systems) == 1 and systems[0]["label"] == "Tier III net 1 (Large)"


def test_one_long_call_is_one_match_and_minute_marks_are_not_calls(tmp_path, store, cfg):
    """A 9-minute call on LCN 7 makes one assignment row; a foreign carrier that keys up just after
    several minute marks is not tied to the site, and the traffic channel gets one match, not nine."""
    paths, keyups = capmax_site(tmp_path, net=1, site=1, cc=1, control_freq=CC_A, traffic={7: T7},
                                calls=[(5, 2102, 1234, 7, 1, 540)], minutes=10)
    for p in paths:
        ingest_log_file(store, p, cfg)
    rows = store.rows("SELECT ts, last_ts FROM csbk_events WHERE kind = 'capmax_channel_update'")
    assert len(rows) == 1 and rows[0]["last_ts"] - rows[0]["ts"] > 500
    foreign = store.upsert_carrier(FOREIGN, keyups[0][1])
    start = (keyups[0][1] // 60 + 1) * 60
    for k in range(5):
        store.conn.execute("INSERT INTO keyups (carrier_id, session_id, start, end) VALUES (?, 1, ?, ?)",
                           (foreign, start + 60 * k + 1, start + 60 * k + 5))
    store.commit()
    run(store, cfg, now=NOW)
    links = _links(store)
    assert not any(FOREIGN in key[:2] for key in links), [k for k in links if FOREIGN in k[:2]]
    link = _link(links, T7, CC_A, "same_site")
    ev = next(e for e in explain(store, link["id"], cfg, now=NOW)["evidence"] if e["type"] == "behavioral_lcn_keyup")
    assert ev["details"]["matches"] == 1 and ev["details"]["lcn"] == 7


def test_a_carrier_that_only_keys_up_at_the_right_moments_joins_no_site(capmax_store, cfg):
    """Field run: 469.1 MHz (no DMR in 80 s of decoding) keyed up 59 times, often as calls started.
    Timing alone gave it 'probable' links to both sites and made sites 1 and 2 one."""
    store = capmax_store
    rows = store.rows("SELECT ts FROM csbk_events WHERE kind = 'capmax_channel_update'")
    stray = store.upsert_carrier(469_100_000, NOW - 3000)
    other_cc = store.upsert_carrier(469_025_000, NOW - 3000)
    store.conn.execute("UPDATE carriers SET dominant_cc = 9, variant = 'tier2' WHERE id = ?", (other_cc,))
    for r in rows:
        for cid in (stray, other_cc):
            store.conn.execute("INSERT INTO keyups (carrier_id, session_id, start, end, decoded) VALUES (?, 1, ?, ?, 1)",
                               (cid, r["ts"] + 1, r["ts"] + 9))
    store.commit()
    run(store, cfg, now=NOW, rebuild=True)
    links = _links(store)
    for key, link in links.items():
        if 469_100_000 in key[:2] or 469_025_000 in key[:2]:
            assert link["label"] == "weak", (key, link)
    lcns = {int(r["freq_hz"]): r["lcn"] for r in store.rows(
        "SELECT c.freq_hz, cs.lcn FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id")}
    assert lcns[469_100_000] is None and lcns[469_025_000] is None
    site_of = {int(r["freq_hz"]): r["site_id"] for r in store.rows(
        "SELECT c.freq_hz, cs.site_id FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id")}
    assert site_of[CC_A] != site_of[CC_B]


def test_sites_that_name_different_sites_are_never_merged(store, cfg):
    from test_watch import _tier3

    a = _tier3(store, 469_350_000, "slc_c_sys_parms", sub=2)       # site 1
    b = _tier3(store, 469_650_000, "slc_c_sys_parms", sub=3)       # site 2
    mid = store.upsert_carrier(469_500_000, NOW - 600)
    for cid in (a, b, mid):
        store.upsert_carrier_seen(cid, NOW - 600, NOW - 60)
    store.commit()
    from dmr_mapper.correlate import propose
    propose(store, 469_350_000, 469_500_000, "same_site", 0.9, "test", cfg, now=NOW)
    propose(store, 469_650_000, 469_500_000, "same_site", 0.8, "test", cfg, now=NOW)
    run(store, cfg, now=NOW)
    site_of = {int(r["freq_hz"]): r["site_id"] for r in store.rows(
        "SELECT c.freq_hz, cs.site_id FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id")}
    assert site_of[469_500_000] == site_of[469_350_000] != site_of[469_650_000]   # the stronger link wins
