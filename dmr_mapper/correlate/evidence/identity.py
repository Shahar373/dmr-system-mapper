"""Identity evidence: the same radio IDs / talkgroups heard on two carriers within a day, and the
same undecoded short-LC content repeated on two carriers (a Tier II fingerprint)."""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations

from ..context import Context
from ..model import Evidence
from ..scoring import ScoringConfig

RADIO_SATURATION = 5
TG_SATURATION = 3
ADJACENT_DAYS = 1


def _collect(ctx: Context, kind: str, saturation: int, etype: str) -> list[Evidence]:
    rows = ctx.store.rows(
        "SELECT s.identity_id, s.carrier_id, s.day, s.count, i.value FROM identity_sightings s JOIN identities i ON i.id = s.identity_id"
        " WHERE i.kind = ? AND s.day * 86400 >= ? AND i.value > 0", (kind, ctx.since))
    per_carrier: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))   # carrier → value → days
    for r in rows:
        per_carrier[int(r["carrier_id"])][int(r["value"])].add(int(r["day"]))
    out: list[Evidence] = []
    for a_id, b_id in combinations(sorted(per_carrier), 2):
        a, b = ctx.carriers.get(a_id), ctx.carriers.get(b_id)
        if a is None or b is None:
            continue
        shared: list[dict] = []
        newest_day = 0
        first_day = None
        for value, days_a in per_carrier[a_id].items():
            days_b = per_carrier[b_id].get(value)
            if not days_b:
                continue
            close = [(da, db) for da in days_a for db in days_b if abs(da - db) <= ADJACENT_DAYS]
            if not close:
                continue
            day = max(max(da, db) for da, db in close)
            newest_day = max(newest_day, day)
            first_day = day if first_day is None else min(first_day, min(min(da, db) for da, db in close))
            shared.append({"value": value, "days_apart": min(abs(da - db) for da, db in close)})
        if not shared:
            continue
        raw = min(1.0, len(shared) / saturation)
        out.append(Evidence(a.freq_hz, b.freq_hz, "same_system", etype, round(raw, 4), newest_day * 86400 + 86399,
                            (first_day or newest_day) * 86400, len(shared),
                            {"shared": len(shared), "ids": [s["value"] for s in shared[:10]]}).ordered())
    return out


def radio(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    return _collect(ctx, "radio", RADIO_SATURATION, "identity_radio")


def talkgroup(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    return _collect(ctx, "tg", TG_SATURATION, "identity_talkgroup")


SLCO_SATURATION = 3      # distinct shared payloads for raw = 1
SLCO_MIN_REPEATS = 3     # a payload must repeat on a carrier: one-off lines can be decode errors


def slco_pattern(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    """Two carriers repeating the same short-LC payload that DSD-FME does not decode
    (``SLCO Completed Block [..]``, ``SLCO Unknown``), with the same colour code.

    Seen in the field run of 2026-10-30: two VHF Tier II repeaters (CC 6) each sent
    ``[18][00][70][07][60]`` more than 100 times and shared another payload. Identical vendor
    signalling suggests the same programming, i.e. the same system, but a vendor default would look
    the same, so this is Tier II evidence: low weight, capped, never "confirmed". A payload present
    on many carriers is treated as a generic default and ignored."""
    rows = ctx.store.rows(
        "SELECT carrier_id, payload, count, ts, last_ts FROM csbk_events WHERE kind = 'slco_other' AND last_ts >= ?", (ctx.since,))
    by_text: dict[str, dict[int, tuple[int, int, int]]] = defaultdict(dict)   # text → carrier → (count, first, last)
    carriers_with_slco: set[int] = set()
    for r in rows:
        cid = int(r["carrier_id"])
        if cid not in ctx.carriers:
            continue
        carriers_with_slco.add(cid)
        text = str(json.loads(r["payload"]).get("text", "")).strip()
        if not text:
            continue
        n, first, last = by_text[text].get(cid, (0, int(r["ts"]), int(r["last_ts"])))
        by_text[text][cid] = (n + int(r["count"]), min(first, int(r["ts"])), max(last, int(r["last_ts"])))
    common_limit = max(4, len(carriers_with_slco) // 2)
    pairs: dict[tuple[int, int], dict] = {}
    for text, per in by_text.items():
        repeated = {cid: v for cid, v in per.items() if v[0] >= SLCO_MIN_REPEATS}
        if len(repeated) < 2 or len(per) > common_limit:
            continue
        for a_id, b_id in combinations(sorted(repeated), 2):
            a, b = ctx.carriers[a_id], ctx.carriers[b_id]
            if a.cc is None or a.cc != b.cc:
                continue
            m = pairs.setdefault((a_id, b_id), {"texts": [], "newest": 0, "first": None})
            m["texts"].append(text)
            m["newest"] = max(m["newest"], repeated[a_id][2], repeated[b_id][2])
            first = min(repeated[a_id][1], repeated[b_id][1])
            m["first"] = first if m["first"] is None else min(m["first"], first)
    out: list[Evidence] = []
    for (a_id, b_id), m in pairs.items():
        a, b = ctx.carriers[a_id], ctx.carriers[b_id]
        raw = round(min(1.0, len(m["texts"]) / SLCO_SATURATION), 4)
        out.append(Evidence(a.freq_hz, b.freq_hz, "same_system", "identity_slco", raw, m["newest"], m["first"], len(m["texts"]),
                            {"shared": len(m["texts"]), "cc": a.cc, "payloads": sorted(m["texts"])[:5]}).ordered())
    return out
