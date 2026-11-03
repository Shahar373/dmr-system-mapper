"""Decode planning under a budget: carriers already mapped must not use a whole decode while
never-decoded ones wait (field case, campaign decode session 14, 2026-11-03)."""

from __future__ import annotations

import time

from dmr_mapper.collector.scheduler import GROUP_BONUS, Slice, Target, _bonuses, order_for_budget, plan, targets_from_store

H = 3600
TIER3 = {1: (465_012_500, 465_037_500), 2: (465_900_000, 465_925_000), 3: (467_500_000, 467_525_000)}   # net 1, sites 1-3
CAPPLUS_SITE2 = (455_100_000, 455_112_500)
SILENT = {465_037_500, 465_900_000, 465_925_000, 467_525_000, 455_112_500}       # five of the eight: 0 syncs last time
UHF_NEW = {420_500_000: 27, 421_000_000: 26, 422_400_000: 23, 423_000_000: 21}   # wide, not DMR, never decoded yet
VHF_NEW = {162_525_000: 14, 162_587_500: 12, 164_725_000: 10, 167_137_500: 16}   # Cap+ sites 0 and 5, Tier II CC 7


def _known(store, freq: int, now: int, variant: str, cc: int, silent: bool) -> int:
    cid = store.upsert_carrier(freq, now - 40 * H)
    store.conn.execute("UPDATE carriers SET variant = ?, dominant_cc = ? WHERE id = ?", (variant, cc, cid))
    store.conn.execute("INSERT INTO observations (carrier_id, bucket_start, source, seconds_observed, bursts, snr_db_max)"
                       " VALUES (?, ?, 'collector', 600, 900, 25)", (cid, now - 72 * H))
    run = store.start_ingest_run("collector", None, cid, now - 72 * H, None)
    store.finish_ingest_run(run, now - 72 * H + 600, 2000, 1800, {})
    # the last decode, 40 h ago from here: 120 s, nothing parsed for the silent ones
    run = store.start_ingest_run("collector", None, cid, now - 40 * H - 120, None)
    store.finish_ingest_run(run, now - 40 * H, 0 if silent else 400, 0 if silent else 380, {})
    return cid


def _confirm(store, a: int, b: int, now: int) -> None:
    a, b = min(a, b), max(a, b)
    store.conn.execute("INSERT INTO links (a_carrier_id, b_carrier_id, kind, score, label, tier2_only, updated_at)"
                       " VALUES (?, ?, 'same_site', 0.97, 'confirmed', 0, ?)", (a, b, now))


def _field_store(store, now: int) -> list[dict]:
    for site, pair in TIER3.items():
        ids = []
        for f in pair:
            cid = _known(store, f, now, "tier3", 1, f in SILENT)
            store.add_csbk_event(cid, now - 72 * H, 1, 1, "tier3_sys_parms", {"sys_code": "net1", "site": site}, f"net1-{site}")
            ids.append(cid)
        _confirm(store, *ids, now)
    ids = []
    for f in CAPPLUS_SITE2:
        cid = _known(store, f, now, "capplus", 3, f in SILENT)
        store.add_csbk_event(cid, now - 72 * H, 1, 3, "capplus_site", {"site": 2, "rest_lsn": 1, "rs": "00"}, "2")
        ids.append(cid)
    _confirm(store, *ids, now)
    store.commit()
    hits = [{"freq_hz": f, "snr_db": 25.0} for pair in (*TIER3.values(), CAPPLUS_SITE2) for f in pair]
    hits += [{"freq_hz": f, "snr_db": float(s), "band": "uhf"} for f, s in UHF_NEW.items()]
    hits += [{"freq_hz": f, "snr_db": float(s), "band": "vhf-high"} for f, s in VHF_NEW.items()]
    return hits


def _run(slices: list[Slice], budget_s: float) -> list[tuple[Slice, int]]:
    """What the daemon decodes: slices in order, each for min(dwell, what is left, at least 10 s)
    until the budget is used."""
    out, used = [], 0.0
    for sl in slices:
        left = budget_s - used
        if left <= 0:
            break
        dwell = int(min(sl.dwell_s, max(10, left)))
        out.append((sl, dwell))
        used += dwell
    return out


def test_field_case_campaign_decode_reaches_vhf(store, cfg):
    now = int(time.time())
    targets = targets_from_store(store, cfg, _field_store(store, now), now)
    by = {t.freq_hz: t for t in targets}
    assert {f for f, t in by.items() if t.silent_last} == SILENT
    assert by[465_012_500].confirmed_with == {465_037_500} and by[162_525_000].band == "vhf-high"
    assert all(t.never_decoded for f, t in by.items() if f in VHF_NEW)
    budget = 613
    done = _run(plan(targets, cfg, max_instances=2, sample_rate=2_000_000, budget_s=budget), budget)
    decoded = {t.freq_hz for sl, _ in done for t in sl.targets}
    # a VHF slice with its full dwell (the old planner left VHF the last 13 s of the budget)
    vhf = [sl for sl, d in done if sl.band == "vhf-high" and d >= sl.dwell_s]
    assert vhf, [(sl.to_dict(), d) for sl, d in done]
    # every VHF hit fits: three slices (162.525 + 162.5875 together, 164.725, 167.1375)
    assert set(VHF_NEW) <= decoded
    # confirmed sites earn no long dwell any more
    assert not any(sl.reason.startswith("same-site") for sl, _ in done)
    new_s = sum(d for sl, d in done if sl.new)
    assert new_s >= 0.4 * budget


def test_unconfirmed_site_keeps_long_dwell_until_confirmed(store, cfg):
    """A Cap+ site not yet confirmed and heard last time still gets the pair bonus and long dwell."""
    now = int(time.time())
    ids = []
    for f in CAPPLUS_SITE2:
        cid = _known(store, f, now, "capplus", 3, silent=False)
        store.add_csbk_event(cid, now - 72 * H, 1, 3, "capplus_site", {"site": 2, "rest_lsn": 1, "rs": "00"}, "2")
        ids.append(cid)
    store.commit()
    targets = targets_from_store(store, cfg, [{"freq_hz": f, "snr_db": 25.0} for f in CAPPLUS_SITE2], now)
    s = plan(targets, cfg, 2, 2_000_000, budget_s=600)
    assert s[0].reason.startswith("same-site") and s[0].dwell_s == 2 * cfg["decode"]["seconds_per_carrier"]
    # once confirmed: a plain refresh
    _confirm(store, *ids, now)
    store.commit()
    targets = targets_from_store(store, cfg, [{"freq_hz": f, "snr_db": 25.0} for f in CAPPLUS_SITE2], now)
    s = plan(targets, cfg, 2, 2_000_000, budget_s=600)
    assert s[0].reason == "refresh" and s[0].dwell_s == cfg["decode"]["seconds_per_carrier"]


def test_group_bonus_rules():
    a = Target(455_100_000, variant="capplus", cap_site=2, cc=3)
    b = Target(455_112_500, variant="capplus", cap_site=2, cc=3)
    assert _bonuses([a, b]) == {id(a): GROUP_BONUS, id(b): GROUP_BONUS}
    b.silent_last = True
    assert _bonuses([a, b]) == {}
    b.silent_last = False
    a.confirmed_with, b.confirmed_with = {b.freq_hz}, {a.freq_hz}
    assert _bonuses([a, b]) == {}
    # a third carrier of the site, not yet placed, still pairs with either
    c = Target(455_125_000, variant="capplus", cap_site=2, cc=3)
    assert set(_bonuses([a, b, c])) == {id(a), id(b), id(c)}
    # two carriers of different confirmed Tier III sites of one network: settled
    x = Target(465_037_500, variant="tier3", t3_sys="net1", confirmed_with={465_012_500})
    y = Target(465_900_000, variant="tier3", t3_sys="net1", confirmed_with={465_925_000})
    assert _bonuses([x, y]) == {}


def test_silent_only_counts_from_around_here(store, cfg):
    """A carrier silent at a decode made at another stop (by the GPS track) keeps its bonus here."""
    now = int(time.time())
    for f in CAPPLUS_SITE2:
        cid = _known(store, f, now, "capplus", 3, silent=True)
        store.add_csbk_event(cid, now - 72 * H, 1, 3, "capplus_site", {"site": 2, "rest_lsn": 1, "rs": "00"}, "2")
    store.conn.execute("INSERT INTO positions (session_id, ts, lat, lon) VALUES (1, ?, 32.0, 34.8)", (now - 40 * H - 60,))
    store.commit()
    hits = [{"freq_hz": f, "snr_db": 25.0} for f in CAPPLUS_SITE2]
    here_far = (32.5, 35.0)                      # about 60 km from where the silent decode was made
    far = targets_from_store(store, cfg, hits, now, near=here_far, radius_km=3)
    assert not any(t.silent_last for t in far)
    near = targets_from_store(store, cfg, hits, now, near=(32.0, 34.8), radius_km=3)
    assert all(t.silent_last for t in near)
    assert all(t.silent_last for t in targets_from_store(store, cfg, hits, now))   # no position: counts


def _sl(freq: int, score: float, dwell: int, new: bool, band: str) -> Slice:
    t = Target(freq, last_decoded=None if new else 1, band=band, score=score)
    return Slice(freq, [t], dwell, "never decoded" if new else "refresh", score)


def test_every_band_with_new_hits_gets_a_slice():
    # strong never-decoded UHF hits alone would fill the budget; the weak VHF one still gets its slice
    uhf = [_sl(420_000_000 + i * 2_000_000, 10.0 - i * 0.01, 60, True, "uhf") for i in range(12)]
    vhf = [_sl(167_137_500, 8.8, 60, True, "vhf-high")]
    out = order_for_budget(uhf + vhf, 613)
    assert vhf[0] in [s for s, _ in _run(out, 613)]
    assert out[0] is uhf[0]                      # the best slice still goes first


def test_new_share_reserve_and_config(cfg):
    # unconfirmed Cap+ groups score ~12 per slice; never-decoded weak pairs ~8.8
    known = [_sl(455_000_000 + i * 2_000_000, 12.0 - i * 0.1, 120, False, "uhf") for i in range(6)]
    new = [_sl(162_525_000 + i * 2_000_000, 8.8 - i * 0.1, 60, True, "vhf-high") for i in range(6)]
    done = _run(order_for_budget(known + new, 613, 0.4), 613)
    assert sum(d for s, d in done if s.new) >= 0.4 * 613
    assert sum(d for s, d in done if not s.new) >= 240        # known slices still get the rest
    # new_share 0: only the band's first new slice is guaranteed
    done = _run(order_for_budget(known + new, 613, 0.0), 613)
    assert [s for s, _ in done if s.new] == [new[0]]
    # plan reads decode.new_share and orders only when given a budget
    c = {**cfg, "decode": {**cfg["decode"], "new_share": 0.0}}
    t = [Target(455_100_000, last_decoded=1, score=6.0, band="uhf"), Target(162_525_000, score=4.0, band="vhf-high")]
    assert [s.targets[0].freq_hz for s in plan(t, c, 1, 2_000_000)] == [455_100_000, 162_525_000]
    assert [s.targets[0].freq_hz for s in plan(t, c, 1, 2_000_000, budget_s=60)] == [162_525_000, 455_100_000]
