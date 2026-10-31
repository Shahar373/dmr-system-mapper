"""Identity evidence: the same radio IDs / talkgroups heard on two carriers within a day, and the
same undecoded short-LC content repeated on two carriers (a Tier II fingerprint)."""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations

from ...ingest.rawpdu import activity_key, address_hash, decode_slco_block, hex_bytes
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
        # sightings are kept per UTC day: the end of today would be in the future
        out.append(Evidence(a.freq_hz, b.freq_hz, "same_system", etype, round(raw, 4), min(newest_day * 86400 + 86399, ctx.now),
                            (first_day or newest_day) * 86400, len(shared),
                            {"shared": len(shared), "ids": [s["value"] for s in shared[:10]]}).ordered())
    return out


def radio(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    return _collect(ctx, "radio", RADIO_SATURATION, "identity_radio")


def talkgroup(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    return _collect(ctx, "tg", TG_SATURATION, "identity_talkgroup")


SLCO_SATURATION = 3      # distinct shared payloads for raw = 1
SLCO_MIN_REPEATS = 3     # a payload must repeat on a carrier: one-off lines can be decode errors


def slco_key(kind: str, payload: dict, variant: str) -> tuple[str, set[int]] | None:
    """One comparable text per short-LC content, and the hashed addresses in it (Activity Updates
    only), or None when it carries nothing to compare.

    Activity Updates (the hashed address of what each slot carries) give the same text whether
    DSD-FME's decoded line or the raw ``SLCO Completed Block`` was stored (databases before
    Phase 4 kept the block as ``slco_other`` next to the decoded line). An SLCO nothing decodes
    gives ``slco <opcode> <data>`` from its bits, old or new. Short LCs decoded explicitly
    elsewhere (system parameters, Connect Plus, XPT, a Cap+ site on a Cap+ carrier) are left out."""
    if kind == "activity_update":
        if payload.get("ts1") == "Idle" and payload.get("ts2") in ("Idle", None):
            return None        # nothing active: no address to compare
        hashes = {int(payload[h]) for t, h in (("ts1", "hash1"), ("ts2", "hash2"))
                  if payload.get(t) not in (None, "Idle") and payload.get(h) is not None}
        return activity_key(payload.get("ts1"), payload.get("hash1"), payload.get("ts2"), payload.get("hash2")), hashes
    if kind == "capmax_slco":
        return (f"slco F {payload['data']}", set()) if payload.get("data") else None
    text = str(payload.get("text", "")).strip()
    if not text:
        return None
    if text.startswith("SLCO Completed Block"):
        d = decode_slco_block(hex_bytes(text))
        if d is None:
            return None
        if d["opcode"] == 0x1:
            return slco_key("activity_update", d, variant)
        if d["opcode"] in (0x0, 0x2, 0x3, 0x8, 0x9, 0xA) or (d["opcode"] == 0xF and variant == "capplus"):
            return None
        return f"slco {d['opcode']:X} {d['data']}", set()
    if payload.get("data") and payload.get("opcode") is not None:
        return f"slco {int(payload['opcode']):X} {payload['data']}", set()
    return text, set()


def slco_pattern(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    """Two carriers repeating the same short-LC content, with the same colour code: the same
    Activity Update (slot activity and hashed address) or the same short LC that nothing decodes.

    Seen in the field run of 2026-10-30: two VHF Tier II repeaters (CC 6) each sent
    ``[18][00][70][07][60]`` (an Activity Update: slot 1 group voice, hash 7) more than 100 times
    and shared another one. The same hashed talkgroup active on both suggests the same system, but
    a hash has 256 values and a vendor default would look the same, so this is Tier II evidence:
    low weight, capped, never "confirmed". Content present on many carriers is treated as generic
    and ignored. An Activity Update whose hashes are those of talkgroups both carriers were heard
    carrying adds nothing to ``identity_talkgroup`` and is left out. A carrier's count of one
    content is the largest over the kinds it was stored as (old databases hold both the decoded
    line and its block)."""
    rows = ctx.store.rows(
        "SELECT carrier_id, kind, payload, count, ts, last_ts FROM csbk_events"
        " WHERE kind IN ('slco_other', 'activity_update', 'capmax_slco') AND last_ts >= ?", (ctx.since,))
    # a carrier with SLCO bits (-Z) has its unknown SLCOs as "slco <op> <data>": its text-only rows are the same SLCOs again
    with_bits = {int(r["carrier_id"]) for r in rows if r["kind"] == "slco_other"
                 and ('"data"' in r["payload"] or "Completed Block" in r["payload"])}
    per_kind: dict[str, dict[int, dict[str, list[int]]]] = defaultdict(lambda: defaultdict(dict))   # text → carrier → kind → [n, first, last]
    hashes_of: dict[str, set[int]] = {}
    carriers_with_slco: set[int] = set()
    for r in rows:
        cid = int(r["carrier_id"])
        if cid not in ctx.carriers:
            continue
        carriers_with_slco.add(cid)
        p = json.loads(r["payload"])
        key = slco_key(str(r["kind"]), p, ctx.carriers[cid].variant)
        if not key:
            continue
        text, hashes = key
        if text.startswith("SLCO Unknown") and cid in with_bits:
            continue
        hashes_of[text] = hashes
        kind = "activity" if text.startswith("activity") else str(r["kind"])
        kind = f"{kind}:{'block' if 'Completed Block' in str(p.get('text', '')) else 'line'}"
        v = per_kind[text][cid].setdefault(kind, [0, int(r["ts"]), int(r["last_ts"])])
        v[0] += int(r["count"])
        v[1], v[2] = min(v[1], int(r["ts"])), max(v[2], int(r["last_ts"]))
    by_text: dict[str, dict[int, tuple[int, int, int]]] = {
        text: {cid: (max(v[0] for v in kinds.values()), min(v[1] for v in kinds.values()), max(v[2] for v in kinds.values()))
               for cid, kinds in per.items()}
        for text, per in per_kind.items()}
    tgs = _talkgroups(ctx)
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
            known = {address_hash(t) for t in tgs.get(a_id, set()) & tgs.get(b_id, set())}
            if hashes_of[text] and hashes_of[text] <= known:
                continue        # the same talkgroups, already counted by identity_talkgroup
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


def _talkgroups(ctx: Context) -> dict[int, set[int]]:
    out: dict[int, set[int]] = defaultdict(set)
    for r in ctx.store.rows(
            "SELECT s.carrier_id, i.value FROM identity_sightings s JOIN identities i ON i.id = s.identity_id"
            " WHERE i.kind = 'tg' AND s.day * 86400 >= ?", (ctx.since,)):
        out[int(r["carrier_id"])].add(int(r["value"]))
    return out
