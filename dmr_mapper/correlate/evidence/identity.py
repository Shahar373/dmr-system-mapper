"""Identity evidence: the same radio IDs / talkgroups heard on two carriers within a day."""

from __future__ import annotations

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
