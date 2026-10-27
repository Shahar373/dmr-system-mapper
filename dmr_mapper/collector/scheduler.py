"""Decode planning: which carriers to decode together, in what order, for how long.

Inputs are the latest survey hits plus the carriers already in the database. Each target gets a
score (never decoded, strong, stale, evidence-producing variant) and a group bonus when another
target that looks like the same site fits in the same capture slice: decoding the carriers of
one Capacity Plus site *together* is what produces rest-hop evidence, so those slices get a
longer dwell. Slices are chosen greedily by total score.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Store

EVIDENCE_VARIANTS = ("capplus", "conplus", "tier3", "xpt")


@dataclass
class Target:
    freq_hz: int
    snr_db: float | None = None
    variant: str = "unknown"
    cc: int | None = None
    cap_site: int | None = None
    t3_sys: str | None = None
    last_decoded: int | None = None
    decode_seconds: float = 0.0
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def group_key(self) -> tuple | None:
        if self.variant == "capplus" and self.cap_site is not None:
            return ("capplus", self.cap_site, self.cc)
        if self.variant == "tier3" and self.t3_sys:
            return ("tier3", self.t3_sys)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"freq_hz": self.freq_hz, "snr_db": self.snr_db, "variant": self.variant, "cc": self.cc, "score": round(self.score, 2),
                "reasons": self.reasons}


@dataclass
class Slice:
    center_hz: int
    targets: list[Target]
    dwell_s: int
    reason: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"center_hz": self.center_hz, "freqs": [t.freq_hz for t in self.targets], "dwell_s": self.dwell_s,
                "reason": self.reason, "score": round(self.score, 2)}


def targets_from_store(store: Store, cfg: dict[str, Any], hits: list[dict[str, Any]], now: int | None = None,
                       only_freqs: list[int] | None = None) -> list[Target]:
    now = now or int(time.time())
    by_freq: dict[int, Target] = {}
    for h in hits:
        f = int(h["freq_hz"])
        by_freq[f] = Target(f, snr_db=h.get("snr_db") if h.get("snr_db") is not None else h.get("p95_snr_db"))
    carriers = {int(r["freq_hz"]): dict(r) for r in store.carriers()}
    window = now - 30 * 86400
    for f, c in carriers.items():
        if f not in by_freq:
            if int(c["last_seen"]) < window:
                continue
            by_freq[f] = Target(f)
        t = by_freq[f]
        t.variant, t.cc = str(c["variant"]), c["dominant_cc"]
        cid = int(c["id"])
        t.last_decoded = store.scalar("SELECT MAX(finished) FROM ingest_runs WHERE carrier_id = ? AND source = 'collector'", (cid,))
        t.decode_seconds = float(store.scalar("SELECT COALESCE(SUM(seconds_observed), 0) FROM observations WHERE carrier_id = ? AND source = 'collector'", (cid,)) or 0)
        if t.snr_db is None:
            t.snr_db = store.scalar("SELECT MAX(snr_db_max) FROM observations WHERE carrier_id = ?", (cid,))
        for r in store.rows("SELECT kind, payload, count FROM csbk_events WHERE carrier_id = ? AND kind IN ('capplus_site', 'tier3_sys_parms') ORDER BY count DESC", (cid,)):
            p = json.loads(r["payload"])
            if r["kind"] == "capplus_site" and t.cap_site is None:
                t.cap_site = int(p["site"])
            elif r["kind"] == "tier3_sys_parms" and t.t3_sys is None and p.get("sys_code"):
                t.t3_sys = str(p["sys_code"])
    targets = list(by_freq.values())
    if only_freqs:
        wanted = set(int(f) for f in only_freqs)
        targets = [t for t in targets if t.freq_hz in wanted]
        for f in wanted - {t.freq_hz for t in targets}:
            targets.append(Target(f, reasons=["requested"]))
    for t in targets:
        _score(t, now)
    return sorted(targets, key=lambda t: t.freq_hz)


def _score(t: Target, now: int) -> None:
    s = 1.0
    if t.last_decoded is None:
        s += 3.0
        t.reasons.append("never decoded")
    else:
        age_h = (now - int(t.last_decoded)) / 3600
        s += min(age_h, 48) / 48
        if age_h > 12:
            t.reasons.append(f"last decoded {age_h:.0f} h ago")
    if t.snr_db is not None:
        s += max(0.0, min(float(t.snr_db), 30.0)) / 30.0
    if t.variant in EVIDENCE_VARIANTS:
        s += 1.0
        t.reasons.append(f"{t.variant} announces identity")
    if "requested" in t.reasons:
        s += 5.0
    t.score = s


def plan(targets: list[Target], cfg: dict[str, Any], max_instances: int, sample_rate: float | None = None) -> list[Slice]:
    sdr = cfg.get("sdr", {}) or {}
    fs = float(sample_rate or sdr.get("sample_rate_hz", 2_400_000))
    usable = fs * float(sdr.get("usable_fraction", 0.8))
    dwell = int((cfg.get("decode", {}) or {}).get("seconds_per_carrier", 60))
    remaining = sorted(targets, key=lambda t: t.freq_hz)
    slices: list[Slice] = []
    while remaining:
        best: tuple[float, list[Target]] | None = None
        for i, left in enumerate(remaining):
            window = [t for t in remaining[i:] if t.freq_hz - left.freq_hz <= usable]
            groups: dict[tuple, int] = {}
            for t in window:
                k = t.group_key()
                if k is not None:
                    groups[k] = groups.get(k, 0) + 1
            scored = []
            for t in window:
                bonus = 2.0 if groups.get(t.group_key() or ("none",), 0) >= 2 else 0.0
                scored.append((t.score + bonus, bonus, t))
            scored.sort(key=lambda x: -x[0])
            chosen = scored[:max(1, max_instances)]
            total = sum(x[0] for x in chosen)
            if best is None or total > best[0]:
                best = (total, [x[2] for x in chosen])
        assert best is not None
        chosen = sorted(best[1], key=lambda t: t.freq_hz)
        span = chosen[-1].freq_hz - chosen[0].freq_hz
        center = (chosen[0].freq_hz + chosen[-1].freq_hz) // 2
        if span > usable:
            chosen = chosen[:1]
            center = chosen[0].freq_hz
        keys = [t.group_key() for t in chosen]
        grouped = any(k is not None and keys.count(k) >= 2 for k in keys)
        reason = "same-site group, long dwell" if grouped else ("never decoded" if any("never decoded" in t.reasons for t in chosen) else "refresh")
        slices.append(Slice(int(center), chosen, dwell * (2 if grouped else 1), reason, best[0]))
        ids = {id(t) for t in chosen}
        remaining = [t for t in remaining if id(t) not in ids]
    return slices
