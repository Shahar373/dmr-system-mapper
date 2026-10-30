"""Capacity Max: which carrier is which LCN.

The control channel's channel updates (CSBK FID 0x10 opcode 0x21/0x22, read from DSD-FME's raw
bytes) say "talkgroup T is on LCN L, timeslot s" while a call is on. A watch session hears the
traffic channel key up at that moment, and the DSD-FME it started on it prints the call to T.
Lining the two up gives the traffic channel's LCN and ties it to the control channel's site:

* a call to T on carrier F, same slot, during the assignment: a full match;
* only a key-up of F starting within ±3 s of the assignment's first update: half a match, shared
  between every carrier that keyed up in that window (another system's carrier in the slice may
  key up too).

Each assignment (one row: the updates for one TG/LCN/slot while they keep coming, see
``writer.Aggregator._assignment``) counts at most once per carrier, however long the call.

A group call goes out on every site with members, so a traffic channel whose own short LC names a
different site than the control channel's is never matched with it. The LCN a carrier gets is the
one most of its matches agree on; ``raw = (1 − 0.6^n) × agreement``: a 24-bit talkgroup on the
announced LCN and slot at that moment is a strong coincidence, so three calls (with the site's own
short LC) make the link confirmed.
"""

from __future__ import annotations

import bisect
import json
from collections import defaultdict

from ..context import Context
from ..model import Evidence
from ..scoring import ScoringConfig

CALL_WINDOW_S = 5        # call rows are kept per minute: overlap generously
KEYUP_WINDOW_S = 3
PER_MATCH = 0.4


def _saturating(n: float) -> float:
    return round(1.0 - (1.0 - PER_MATCH) ** n, 4)


def lcn_keyup(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    updates = ctx.store.rows(
        "SELECT carrier_id, ts, last_ts, payload FROM csbk_events WHERE kind = 'capmax_channel_update' AND last_ts >= ?", (ctx.since,))
    if not updates:
        return []
    calls: dict[int, list[tuple[int, int, int, int | None]]] = defaultdict(list)   # tg → [(carrier, ts, last_ts, slot)]
    for r in ctx.store.rows("SELECT carrier_id, ts, last_ts, payload FROM csbk_events WHERE kind = 'call' AND last_ts >= ?", (ctx.since,)):
        p = json.loads(r["payload"])
        if p.get("group") and p.get("tgt"):
            calls[int(p["tgt"])].append((int(r["carrier_id"]), int(r["ts"]), int(r["last_ts"]), p.get("slot")))
    keyups = sorted((float(r["start"]), int(r["carrier_id"]))
                    for r in ctx.store.rows("SELECT carrier_id, start FROM keyups WHERE end >= ?", (ctx.since,)))
    starts = [k[0] for k in keyups]

    # votes[(control, carrier)][lcn] = weight; plus bookkeeping for details
    votes: dict[tuple[int, int], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    info: dict[tuple[int, int], dict] = {}
    for r in updates:
        c_id = int(r["carrier_id"])
        cc = ctx.carriers.get(c_id)
        if cc is None:
            continue
        p = json.loads(r["payload"])
        lcn, slot, tg = int(p["lcn"]), p.get("slot"), int(p["tg"])
        ts, last = int(r["ts"]), int(r["last_ts"])
        matched_by_call: set[int] = set()
        if p.get("mode") == "open":                        # Advantage mode carries short TG ids, not the call's
            for f_id, cts, clast, cslot in calls.get(tg, []):
                if f_id == c_id or f_id in matched_by_call or not _may_belong(ctx, cc, f_id):
                    continue
                if cts - CALL_WINDOW_S <= last and clast + CALL_WINDOW_S >= ts and (cslot is None or slot is None or int(cslot) == int(slot)):
                    matched_by_call.add(f_id)
                    _vote(votes, info, c_id, f_id, lcn, 1.0, {"ts": ts, "tg": tg, "slot": slot, "lcn": lcn, "by": "call"})
        lo, hi = bisect.bisect_left(starts, ts - KEYUP_WINDOW_S), bisect.bisect_right(starts, ts + KEYUP_WINDOW_S)
        near = {f_id for _, f_id in keyups[lo:hi] if f_id != c_id and _may_belong(ctx, cc, f_id)}
        near -= matched_by_call
        for f_id in near:
            _vote(votes, info, c_id, f_id, lcn, 0.5 / len(near), {"ts": ts, "tg": tg, "slot": slot, "lcn": lcn, "by": "key-up"})

    out: list[Evidence] = []
    for (c_id, f_id), per_lcn in votes.items():
        c, f = ctx.carriers[c_id], ctx.carriers[f_id]
        lcn, weight = max(per_lcn.items(), key=lambda kv: kv[1])
        agreement = weight / sum(per_lcn.values())
        m = info[(c_id, f_id)]
        n = m["n_by_lcn"][lcn]
        ev = Evidence(c.freq_hz, f.freq_hz, "same_site", "behavioral_lcn_keyup", round(_saturating(n) * agreement, 4),
                      m["newest"], m["first"], m["count"],
                      {"control": c.freq_hz, "carrier": f.freq_hz, "lcn": lcn, "matches": round(n, 2), "agreement": round(agreement, 2),
                       "other_lcns": sorted(k for k in per_lcn if k != lcn), "examples": m["examples"][:5]}).ordered()
        ev.lcn_hints[f.freq_hz] = lcn
        out.append(ev)
    return out


def _vote(votes, info, c_id: int, f_id: int, lcn: int, weight: float, example: dict) -> None:
    votes[(c_id, f_id)][lcn] += weight
    m = info.setdefault((c_id, f_id), {"n_by_lcn": defaultdict(float), "count": 0, "newest": 0, "first": example["ts"], "examples": []})
    m["n_by_lcn"][lcn] += weight
    m["count"] += 1
    m["newest"] = max(m["newest"], int(example["ts"]))
    m["first"] = min(m["first"], int(example["ts"]))
    if len(m["examples"]) < 5:
        m["examples"].append(example)


def _may_belong(ctx: Context, control, f_id: int) -> bool:
    """Carrier ``f_id`` could be a traffic channel of ``control``'s site: not known to be another
    site or network, not another variant, and not far away in frequency."""
    f = ctx.carriers.get(f_id)
    if f is None or f.variant not in ("tier3", "unknown", "tier2"):
        return False
    if abs(f.freq_hz - control.freq_hz) > 10_000_000:
        return False
    tc, tf = ctx.t3_identity(control), ctx.t3_identity(f)
    if tc is not None and tf is not None and (tc[1] != tf[1] or tc[2] != tf[2]):
        return False
    return True
