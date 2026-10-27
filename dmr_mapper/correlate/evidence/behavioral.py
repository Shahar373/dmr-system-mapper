"""Behavioural evidence: Capacity Plus rest-channel hops and LSN-list / call alignment across
carriers. These need two carriers decoded at the same moment, which a sequential collector only
achieves occasionally; corroboration therefore accumulates slowly by design."""

from __future__ import annotations

import json
from collections import defaultdict

from ..context import Context
from ..model import Evidence
from ..scoring import ScoringConfig

HOP_WINDOW_S = 2
LSN_CALL_WINDOW_S = 3
PER_HOP = 0.25   # raw = 1 − (1 − PER_HOP)^n


def _saturating(n: int) -> float:
    return round(1.0 - (1.0 - PER_HOP) ** n, 4)


def lcn_of_lsn(lsn: int) -> int:
    return (lsn + 1) // 2


def rest_hop(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    """Rest channel leaves carrier A at t (A announces a new rest LSN that is not its own), and
    carrier B reports being the rest channel within ±2 s with the same site number."""
    rows = ctx.store.rows(
        "SELECT carrier_id, ts, site, rest_lsn, prev_lsn FROM rest_events WHERE ts >= ? ORDER BY ts", (ctx.since,))
    by_carrier: dict[int, list] = defaultdict(list)
    for r in rows:
        by_carrier[int(r["carrier_id"])].append(r)
    # For each carrier we also need the LSN(s) it itself occupies: a carrier is "at rest" when its
    # own channel status lists its LSN as Rest. We learn it from the capplus_channel_status payloads.
    own_lsns = _own_lsns(ctx)
    pairs: dict[tuple[int, int], dict] = {}
    carriers = list(by_carrier)
    for i, a_id in enumerate(carriers):
        for b_id in carriers[i + 1:]:
            a, b = ctx.carriers.get(a_id), ctx.carriers.get(b_id)
            if a is None or b is None or a.cc != b.cc:
                continue
            hops = _count_hops(by_carrier[a_id], by_carrier[b_id], own_lsns.get(a_id), own_lsns.get(b_id))
            hops += _count_hops(by_carrier[b_id], by_carrier[a_id], own_lsns.get(b_id), own_lsns.get(a_id))
            if not hops:
                continue
            newest = max(max(int(r["ts"]) for r in by_carrier[a_id]), max(int(r["ts"]) for r in by_carrier[b_id]))
            first = min(min(int(r["ts"]) for r in by_carrier[a_id]), min(int(r["ts"]) for r in by_carrier[b_id]))
            site = next((r["site"] for r in by_carrier[a_id] if r["site"] is not None), None)
            ev = Evidence(a.freq_hz, b.freq_hz, "same_site", "behavioral_rest_hop", _saturating(len(hops)), newest, first,
                          len(hops), {"hops": len(hops), "site": site, "examples": hops[:5]}).ordered()
            for cid, lsns in ((a_id, own_lsns.get(a_id)), (b_id, own_lsns.get(b_id))):
                if lsns:
                    ev.lcn_hints[ctx.carriers[cid].freq_hz] = lcn_of_lsn(min(lsns))
            pairs[(a.freq_hz, b.freq_hz)] = ev
    return list(pairs.values())


def _count_hops(src_rows, dst_rows, src_lsns: set[int] | None, dst_lsns: set[int] | None) -> list[dict]:
    """Transitions on src whose new rest LSN belongs to dst, confirmed by dst reporting a rest LSN
    of its own within the window."""
    hops: list[dict] = []
    if not dst_lsns:
        return hops
    dst_times = [(int(r["ts"]), int(r["rest_lsn"])) for r in dst_rows]
    for r in src_rows:
        if r["prev_lsn"] is None:
            continue
        new_lsn = int(r["rest_lsn"])
        if src_lsns and new_lsn in src_lsns:
            continue            # rest stayed on src (slot change only)
        if new_lsn not in dst_lsns:
            continue
        ts = int(r["ts"])
        for dts, dlsn in dst_times:
            if abs(dts - ts) <= HOP_WINDOW_S and dlsn in dst_lsns and (r["site"] is None or _site_of(dst_rows, dts) in (None, r["site"])):
                hops.append({"ts": ts, "lsn": new_lsn, "dst_ts": dts})
                break
    return hops


def _site_of(rows, ts: int):
    for r in rows:
        if int(r["ts"]) == ts:
            return r["site"]
    return None


def _own_lsns(ctx: Context) -> dict[int, set[int]]:
    """LSNs each carrier has reported as 'Rest' in its own channel list (i.e. its own slots)."""
    out: dict[int, set[int]] = defaultdict(set)
    for r in ctx.store.rows(
            "SELECT carrier_id, payload FROM csbk_events WHERE kind = 'capplus_channel_status' AND last_ts >= ?", (ctx.since,)):
        p = json.loads(r["payload"])
        for lsn, state in (p.get("lsn_states") or {}).items():
            if state == "rest":
                out[int(r["carrier_id"])].add(int(lsn))
    return out


def lsn_table(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    """Carrier A's channel list shows talkgroup T active on LSN n while carrier B carries a call
    to T within ±3 s: B is LCN (n+1)//2 of A's site."""
    status = ctx.store.rows(
        "SELECT carrier_id, ts, last_ts, payload FROM csbk_events WHERE kind = 'capplus_channel_status' AND last_ts >= ?", (ctx.since,))
    calls = ctx.store.rows(
        "SELECT carrier_id, ts, last_ts, payload FROM csbk_events WHERE kind = 'call' AND last_ts >= ?", (ctx.since,))
    calls_by_tg: dict[int, list[tuple[int, int, int]]] = defaultdict(list)   # tg → [(carrier, ts, last_ts)]
    for r in calls:
        p = json.loads(r["payload"])
        if p.get("group") and p.get("tgt"):
            calls_by_tg[int(p["tgt"])].append((int(r["carrier_id"]), int(r["ts"]), int(r["last_ts"])))
    matches: dict[tuple[int, int], dict] = {}
    for r in status:
        a_id = int(r["carrier_id"])
        p = json.loads(r["payload"])
        for lsn, state in (p.get("lsn_states") or {}).items():
            if not isinstance(state, int):
                continue
            for b_id, cts, clast in calls_by_tg.get(state, []):
                if b_id == a_id:
                    continue
                if cts - LSN_CALL_WINDOW_S <= int(r["last_ts"]) and clast + LSN_CALL_WINDOW_S >= int(r["ts"]):
                    key = (min(a_id, b_id), max(a_id, b_id))
                    m = matches.setdefault(key, {"n": 0, "newest": 0, "first": cts, "examples": [], "lcn": {}})
                    m["n"] += 1
                    m["newest"] = max(m["newest"], cts)
                    m["first"] = min(m["first"], cts)
                    m["lcn"][b_id] = lcn_of_lsn(int(lsn))
                    if len(m["examples"]) < 5:
                        m["examples"].append({"tg": state, "lsn": int(lsn), "ts": cts})
    out: list[Evidence] = []
    for (a_id, b_id), m in matches.items():
        a, b = ctx.carriers.get(a_id), ctx.carriers.get(b_id)
        if a is None or b is None or a.cc != b.cc:
            continue
        ev = Evidence(a.freq_hz, b.freq_hz, "same_site", "behavioral_lsn_table", _saturating(m["n"]), m["newest"], m["first"],
                      m["n"], {"matches": m["n"], "examples": m["examples"]}).ordered()
        for cid, lcn in m["lcn"].items():
            ev.lcn_hints[ctx.carriers[cid].freq_hz] = lcn
        out.append(ev)
    return out
