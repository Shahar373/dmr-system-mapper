"""The first field run on the Pi (2026-10-30): a Motorola Tier III network (likely Cap Max) on
469 MHz, net 2, Large model, sites 1.2 and 1.3, whose SYS code changes with the site, plus a
Capacity Plus repeater. DSD-FME also printed stray "Capacity Plus Site: 0" lines on the Tier III
carriers. Payloads below are copied from that database."""

from __future__ import annotations

from dmr_mapper.correlate import explain, run
from dmr_mapper.db import Store

NOW = 1_793_380_000

T3 = {
    469_062_500: ("2102", 3, "slc_p_sys_parms", 72),
    469_350_000: ("2101", 2, "slc_c_sys_parms", 395),
    469_650_000: ("2102", 3, "slc_c_sys_parms", 465),
}


def _field_store(store: Store) -> dict[int, int]:
    ids = {}
    for f, (sys_code, sub, src, n) in T3.items():
        cid = store.upsert_carrier(f, NOW - 600)
        store.upsert_carrier_seen(cid, NOW - 600, NOW - 60)
        store.conn.execute("UPDATE carriers SET variant = 'tier3', variant_confidence = 0.9, dominant_cc = 1 WHERE id = ?", (cid,))
        payload = {"cat": None, "csc": 0, "model": "Large", "net_id": 2, "reg": 1, "site_id": 1, "source": src, "sub_site": sub, "sys_code": sys_code}
        for i in range(n):
            store.add_csbk_event(cid, NOW - 120, 1, 1, "tier3_sys_parms", payload, "x")
        ids[f] = cid
    for f in (469_062_500, 469_650_000):    # the stray Cap+ lines
        for i in range(17):
            store.add_csbk_event(ids[f], NOW - 120, 1, 1, "capplus_site", {"rest_lsn": 0, "rs": "00", "site": 0}, "0")
    cap = store.upsert_carrier(469_800_000, NOW - 600)
    store.upsert_carrier_seen(cap, NOW - 600, NOW - 60)
    store.conn.execute("UPDATE carriers SET variant = 'capplus', variant_confidence = 0.9, dominant_cc = 1 WHERE id = ?", (cap,))
    for lsn in (1, 2, 3, 4):
        store.add_csbk_event(cap, NOW - 120, 1, 1, "capplus_site", {"rest_lsn": lsn, "rs": "00", "site": 2}, str(lsn))
    store.add_csbk_event(cap, NOW - 120, 1, 1, "capplus_adjacent", {"sites": [{"site": 3, "rest_lsn": 1}]}, "a")
    ids[469_800_000] = cap
    store.commit()
    return ids


def test_one_tier3_network_with_two_sites(store, cfg):
    _field_store(store)
    run(store, cfg, now=NOW)
    links = {(r["a"], r["b"], r["kind"]): dict(r) for r in store.rows(
        "SELECT a.freq_hz AS a, b.freq_hz AS b, l.* FROM links l JOIN carriers a ON a.id = l.a_carrier_id JOIN carriers b ON b.id = l.b_carrier_id")}
    same_site = links[(469_062_500, 469_650_000, "same_site")]
    assert same_site["label"] in ("probable", "confirmed") and same_site["score"] >= 0.6, same_site
    types = {e["type"] for e in explain(store, same_site["id"], cfg, now=NOW)["evidence"]}
    assert "explicit_same_site" in types
    ev = next(e for e in explain(store, same_site["id"], cfg, now=NOW)["evidence"] if e["type"] == "explicit_same_site")
    assert ev["details"]["variant"] == "tier3" and ev["details"]["site"] == "1.3" and ev["raw"] == 1.0
    for f in (469_062_500, 469_650_000):
        l = links[(f, 469_350_000, "same_system")] if f < 469_350_000 else links[(469_350_000, f, "same_system")]
        assert l["label"] == "probable" and not l["tier2_only"], l
    # the Cap+ repeater is not tied to the Tier III network by the stray "site 0" lines
    assert not any(469_800_000 in (a, b) for a, b, _ in links)
    systems = [dict(r) for r in store.rows("SELECT * FROM systems")]
    t3 = [s for s in systems if s["variant"] == "tier3"]
    assert len(t3) == 1 and t3[0]["label"] == "Tier III net 2 (Large)" and t3[0]["sys_code"] == "2101,2102" and t3[0]["net_id"] == 2
    sites = {r["label"].split(" (")[0].split(" @")[0] for r in store.rows("SELECT * FROM sites WHERE system_id = ?", (t3[0]["id"],))}
    assert sites == {"site 1.2", "site 1.3"}, sites
    cap_sys = next(s for s in systems if s["variant"] == "capplus")
    cap_sites = {r["site_number"]: r["directly_observed"] for r in store.rows("SELECT * FROM sites WHERE system_id = ?", (cap_sys["id"],))}
    assert cap_sites == {2: 1, 3: 0}


def test_different_net_ids_are_different_networks(store, cfg):
    ids = _field_store(store)
    store.conn.execute("UPDATE csbk_events SET payload = REPLACE(payload, '\"net_id\":2', '\"net_id\":5') WHERE carrier_id = ?", (ids[469_350_000],))
    store.commit()
    run(store, cfg, now=NOW)
    kinds = {r["kind"] for r in store.rows(
        "SELECT l.kind FROM links l JOIN carriers a ON a.id = l.a_carrier_id JOIN carriers b ON b.id = l.b_carrier_id"
        " WHERE 469350000 IN (a.freq_hz, b.freq_hz)")}
    assert "same_system" not in kinds


def _slco(store, cid, text, n, ts=NOW - 120):
    for _ in range(n):
        store.add_csbk_event(cid, ts, 1, 6, "slco_other", {"text": text}, text)


def test_tier2_repeaters_sharing_slco_payloads_get_weak_capped_evidence(store, cfg):
    """164.325 and 165.625 MHz (CC 6) from the field run: the same two undecoded SLCO payloads."""
    a = store.upsert_carrier(164_325_000, NOW - 600)
    b = store.upsert_carrier(165_625_000, NOW - 600)
    c = store.upsert_carrier(166_000_000, NOW - 600)      # same payloads, other colour code
    for cid, cc in ((a, 6), (b, 6), (c, 9)):
        store.upsert_carrier_seen(cid, NOW - 600, NOW - 60)
        store.conn.execute("UPDATE carriers SET variant = 'tier2', variant_confidence = 0.6, dominant_cc = ? WHERE id = ?", (cc, cid))
    _slco(store, a, "SLCO Completed Block [18][00][70][07][60]", 111)
    _slco(store, b, "SLCO Completed Block [18][00][70][07][60]", 110)
    _slco(store, a, "SLCO Completed Block [10][A0][04][96][90]", 3)
    _slco(store, b, "SLCO Completed Block [10][A0][04][96][90]", 6)
    _slco(store, a, "SLCO Completed Block [10][B0][07][E8][70]", 2)     # too rare on a: not counted
    _slco(store, b, "SLCO Completed Block [10][B0][07][E8][70]", 9)
    _slco(store, c, "SLCO Completed Block [18][00][70][07][60]", 50)
    store.commit()
    run(store, cfg, now=NOW)
    rows = [dict(r) for r in store.rows(
        "SELECT a.freq_hz AS fa, b.freq_hz AS fb, l.* FROM links l JOIN carriers a ON a.id = l.a_carrier_id JOIN carriers b ON b.id = l.b_carrier_id")]
    ab = next(r for r in rows if (r["fa"], r["fb"]) == (164_325_000, 165_625_000))
    assert ab["kind"] == "same_system" and ab["tier2_only"] and ab["label"] != "confirmed" and ab["score"] <= 0.6
    ev = next(e for e in explain(store, ab["id"], cfg, now=NOW)["evidence"] if e["type"] == "identity_slco")
    assert ev["details"]["shared"] == 2 and ev["raw"] == round(2 / 3, 4) and ev["weight"] == 0.2
    assert not any(166_000_000 in (r["fa"], r["fb"]) for r in rows)    # different colour code
