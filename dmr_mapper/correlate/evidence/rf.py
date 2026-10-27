"""RF evidence: correlated activity profiles of two carriers with the same colour code.
Deliberately weak (weight 0.10): it only nudges, never decides."""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import combinations

from ..context import Context
from ..model import Evidence
from ..scoring import ScoringConfig

MIN_SHARED_BUCKETS = 24


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def profile(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    rows = ctx.store.rows(
        "SELECT carrier_id, bucket_start, SUM(bursts) AS bursts, SUM(voice) AS voice, SUM(seconds_observed) AS secs"
        " FROM observations WHERE bucket_start >= ? GROUP BY carrier_id, bucket_start", (ctx.since,))
    series: dict[int, dict[int, float]] = defaultdict(dict)
    for r in rows:
        secs = float(r["secs"] or 0)
        if secs <= 0:
            continue
        series[int(r["carrier_id"])][int(r["bucket_start"])] = float(r["voice"] or 0) / secs
    out: list[Evidence] = []
    for a_id, b_id in combinations(sorted(series), 2):
        a, b = ctx.carriers.get(a_id), ctx.carriers.get(b_id)
        if a is None or b is None or a.cc is None or a.cc != b.cc:
            continue
        shared = sorted(set(series[a_id]) & set(series[b_id]))
        if len(shared) < MIN_SHARED_BUCKETS:
            continue
        rho = _pearson([series[a_id][t] for t in shared], [series[b_id][t] for t in shared])
        if rho is None or rho <= 0:
            continue
        out.append(Evidence(a.freq_hz, b.freq_hz, "same_system", "rf_profile", round(min(1.0, rho), 4), shared[-1], shared[0],
                            len(shared), {"shared_buckets": len(shared), "pearson": round(rho, 3), "cc": a.cc}).ordered())
    return out
