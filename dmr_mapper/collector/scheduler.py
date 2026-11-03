"""Decode planning: which carriers to decode together, in what order, for how long.

Inputs are the hits of the recent sweeps (``survey.recent_hits``) plus the carriers already in the database. Each target gets a
score (never decoded, strong, stale, evidence-producing variant) and a group bonus when another
target that looks like the same site fits in the same capture slice: decoding the carriers of
one Capacity Plus site *together* is what produces rest-hop evidence, so those slices get a
longer dwell. The bonus is only for a pair that can still add something: not one whose
``same_site`` link is already confirmed, and not a carrier that was silent at its last decode from
around here. Slices are chosen greedily by total score.

With a budget, the slices are then ordered for it: first the best slice of every band that has
never-decoded hits, and never-decoded slices keep at least ``decode.new_share`` of the budget,
so carriers already mapped cannot use a whole decode while new ones wait.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Store
from ..ingest.runner import _band_for
from .hits import distance_km

EVIDENCE_VARIANTS = ("capplus", "conplus", "tier3", "xpt")
GROUP_BONUS = 2.0
SILENT_MIN_S = 20          # a last decode at least this long with nothing parsed: the carrier was silent
DEFAULT_NEW_SHARE = 0.4


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
    bursts: int = 0
    events: int = 0                     # structural DMR messages ever decoded: one is proof of DMR
    band: str | None = None
    silent_last: bool = False           # its last decode (from around here) parsed nothing
    confirmed_with: set[int] = field(default_factory=set)   # freqs it has a confirmed same_site link with
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def group_key(self) -> tuple | None:
        if self.variant == "capplus" and self.cap_site is not None:
            return ("capplus", self.cap_site, self.cc)
        if self.variant == "tier3" and self.t3_sys:
            return ("tier3", self.t3_sys)
        return None

    @property
    def never_decoded(self) -> bool:
        return self.last_decoded is None

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

    @property
    def new(self) -> bool:
        """Holds a carrier never decoded."""
        return any(t.never_decoded for t in self.targets)

    @property
    def band(self) -> str | None:
        return self.targets[0].band if self.targets else None

    def to_dict(self) -> dict[str, Any]:
        return {"center_hz": self.center_hz, "freqs": [t.freq_hz for t in self.targets], "dwell_s": self.dwell_s,
                "reason": self.reason, "score": round(self.score, 2)}


def _coarse_band(freq_hz: int) -> str:
    return "vhf" if freq_hz < 300_000_000 else "uhf"


def _run_position(store: Store, started: int, finished: int) -> tuple[float, float] | None:
    """Where the receiver was during a decode run (mean of its GPS track), or None."""
    r = store.conn.execute("SELECT AVG(lat) AS lat, AVG(lon) AS lon FROM positions WHERE ts BETWEEN ? AND ?",
                           (int(started) - 60, int(finished) + 60)).fetchone()
    return None if r is None or r["lat"] is None else (float(r["lat"]), float(r["lon"]))


def targets_from_store(store: Store, cfg: dict[str, Any], hits: list[dict[str, Any]], now: int | None = None,
                       only_freqs: list[int] | None = None, near: tuple[float, float] | None = None,
                       radius_km: float | None = None) -> list[Target]:
    """``near``/``radius_km``: the receiver's position now; a last decode made farther away (by the
    GPS track) does not mark a carrier silent. A decode with no track always counts."""
    now = now or int(time.time())
    by_freq: dict[int, Target] = {}
    hit_band: dict[int, str] = {}
    for h in hits:
        f = int(h["freq_hz"])
        by_freq[f] = Target(f, snr_db=h.get("snr_db") if h.get("snr_db") is not None else h.get("p95_snr_db"))
        if h.get("band"):
            hit_band[f] = str(h["band"])
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
        t.bursts = int(store.scalar("SELECT COALESCE(SUM(bursts), 0) FROM observations WHERE carrier_id = ?", (cid,)) or 0)
        t.events = int(store.scalar("SELECT COALESCE(SUM(count), 0) FROM csbk_events WHERE carrier_id = ?", (cid,)) or 0)
        if t.last_decoded is not None and t.bursts < 20:
            # observations exist only for buckets with lines, so a silent decode leaves no seconds:
            # count the decode time from the ingest runs instead
            t.decode_seconds = max(t.decode_seconds, float(store.scalar(
                "SELECT COALESCE(SUM(finished - started), 0) FROM ingest_runs WHERE carrier_id = ? AND source = 'collector'", (cid,)) or 0))
        if t.snr_db is None:
            t.snr_db = store.scalar("SELECT MAX(snr_db_max) FROM observations WHERE carrier_id = ?", (cid,))
        last = store.conn.execute("SELECT started, finished, parsed FROM ingest_runs WHERE carrier_id = ? AND source = 'collector'"
                                  " AND finished IS NOT NULL ORDER BY finished DESC, id DESC LIMIT 1", (cid,)).fetchone()
        if last is not None and int(last["parsed"]) == 0 and int(last["finished"]) - int(last["started"]) >= SILENT_MIN_S:
            pos = _run_position(store, int(last["started"]), int(last["finished"])) if near is not None and radius_km else None
            t.silent_last = pos is None or distance_km(near, pos) <= float(radius_km)   # type: ignore[arg-type]
        for r in store.rows("SELECT kind, payload, count FROM csbk_events WHERE carrier_id = ? AND kind IN ('capplus_site', 'tier3_sys_parms') ORDER BY count DESC", (cid,)):
            p = json.loads(r["payload"])
            if r["kind"] == "capplus_site" and t.cap_site is None and t.variant == "capplus":
                t.cap_site = int(p["site"])
            elif r["kind"] == "tier3_sys_parms" and t.t3_sys is None and p.get("sys_code"):
                t.t3_sys = str(p["sys_code"])
    for r in store.rows("SELECT a.freq_hz AS fa, b.freq_hz AS fb FROM links l JOIN carriers a ON a.id = l.a_carrier_id"
                        " JOIN carriers b ON b.id = l.b_carrier_id WHERE l.kind = 'same_site' AND l.label = 'confirmed'"):
        fa, fb = int(r["fa"]), int(r["fb"])
        if fa in by_freq:
            by_freq[fa].confirmed_with.add(fb)
        if fb in by_freq:
            by_freq[fb].confirmed_with.add(fa)
    targets = list(by_freq.values())
    if only_freqs:
        wanted = set(int(f) for f in only_freqs)
        targets = [t for t in targets if t.freq_hz in wanted]
        for f in wanted - {t.freq_hz for t in targets}:
            targets.append(Target(f, reasons=["requested"]))
    for t in targets:
        c = carriers.get(t.freq_hz)
        t.band = _band_for(cfg, t.freq_hz) or hit_band.get(t.freq_hz) or (c or {}).get("band") or _coarse_band(t.freq_hz)
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
    if t.last_decoded is not None and t.decode_seconds >= 30 and t.bursts < 20 and t.events == 0:
        # listened to for half a minute or more without DMR: analogue, another protocol, or too
        # weak. Revisit rarely (staleness still lifts it back over a day or two).
        s -= 3.0
        t.reasons.append(f"no DMR in {t.decode_seconds:.0f} s of decoding")
    if "requested" in t.reasons:
        s += 5.0
    t.score = s


DC_GUARD_HZ = 25_000     # no carrier closer than this to the tuned frequency


def off_dc(center: int, freqs: list[int], usable: float) -> int:
    """Move the tuned frequency so no carrier sits on the receiver's DC point (a zero-IF receiver
    like the RSP1B has a spike and LO leakage there), keeping every carrier inside the slice."""
    half = usable / 2
    for shift in (0, 100_000, -100_000, 200_000, -200_000, 300_000, -300_000, 50_000, -50_000):
        c = center + shift
        if all(abs(f - c) >= DC_GUARD_HZ and abs(f - c) <= half for f in freqs):
            return c
    return center


def _bonuses(window: list[Target]) -> dict[int, float]:
    """The group bonus of each target in a window: another target of its group is there, neither was
    silent at its last decode (no rest-hop evidence comes from a carrier not heard), and the pair is
    not settled yet. Settled: linked by a confirmed same_site link, or both already in confirmed
    sites (two sites of one Tier III network share a group key, and their carriers are placed)."""
    out: dict[int, float] = {}
    for t in window:
        k = t.group_key()
        if k is None or t.silent_last:
            continue
        if any(o is not t and o.group_key() == k and not o.silent_last and not (t.confirmed_with and o.confirmed_with)
               for o in window):
            out[id(t)] = GROUP_BONUS
    return out


def plan(targets: list[Target], cfg: dict[str, Any], max_instances: int, sample_rate: float | None = None,
         budget_s: float | None = None) -> list[Slice]:
    """Slices covering every target, greedily by total score; with ``budget_s``, ordered for that
    budget (see ``order_for_budget``)."""
    sdr = cfg.get("sdr", {}) or {}
    dcfg = cfg.get("decode", {}) or {}
    fs = float(sample_rate or sdr.get("sample_rate_hz", 2_000_000))
    usable = fs * float(sdr.get("usable_fraction", 0.8))
    dwell = int(dcfg.get("seconds_per_carrier", 60))
    # carriers that scored <= 0 (no DMR heard and not stale yet) sit this round out, unless they
    # are all there is
    remaining = sorted([t for t in targets if t.score > 0] or targets, key=lambda t: t.freq_hz)
    slices: list[Slice] = []
    while remaining:
        best: tuple[float, list[Target]] | None = None
        for i, left in enumerate(remaining):
            window = [t for t in remaining[i:] if t.freq_hz - left.freq_hz <= usable]
            bonus = _bonuses(window)
            scored = sorted(((t.score + bonus.get(id(t), 0.0), t) for t in window), key=lambda x: -x[0])
            chosen = scored[:max(1, max_instances)]
            total = sum(x[0] for x in chosen)
            if best is None or total > best[0]:
                best = (total, [x[1] for x in chosen])
        assert best is not None
        chosen = sorted(best[1], key=lambda t: t.freq_hz)
        span = chosen[-1].freq_hz - chosen[0].freq_hz
        center = (chosen[0].freq_hz + chosen[-1].freq_hz) // 2
        if span > usable:
            chosen = chosen[:1]
            center = chosen[0].freq_hz
        center = off_dc(center, [t.freq_hz for t in chosen], usable)
        # a long dwell only for a pair that still earns the bonus within the chosen slice
        grouped = len(_bonuses(chosen)) >= 2
        reason = "same-site group, long dwell" if grouped else ("never decoded" if any(t.never_decoded for t in chosen) else "refresh")
        slices.append(Slice(int(center), chosen, dwell * (2 if grouped else 1), reason, best[0]))
        ids = {id(t) for t in chosen}
        remaining = [t for t in remaining if id(t) not in ids]
    if budget_s is not None:
        slices = order_for_budget(slices, float(budget_s), float(dcfg.get("new_share", DEFAULT_NEW_SHARE)))
    return slices


def order_for_budget(slices: list[Slice], budget_s: float, new_share: float = DEFAULT_NEW_SHARE) -> list[Slice]:
    """Order greedy slices (best first) so a budget too short for all of them is spent fairly.

    * Never-decoded slices ("new": holding a carrier never decoded) keep a reserve of
      ``new_share`` of the budget (less if they need less), and the best new slice of each band
      with never-decoded hits is inside that reserve when the budget allows: a band of new hits
      is never left out because carriers already mapped elsewhere score higher.
    * Otherwise the better slice goes first, as before.

    Slices past the budget follow in score order (the daemon re-plans if it gets there)."""
    new = [s for s in slices if s.new]
    known = [s for s in slices if not s.new]
    firsts: list[Slice] = []
    seen: set[str | None] = set()
    for s in new:
        if s.band not in seen:
            seen.add(s.band)
            firsts.append(s)
    first_ids = {id(s) for s in firsts}
    new = firsts + [s for s in new if id(s) not in first_ids]
    share = max(0.0, min(1.0, new_share))
    reserve = min(share * budget_s, sum(s.dwell_s for s in new))
    reserve = max(reserve, min(budget_s, sum(s.dwell_s for s in firsts)))
    out: list[Slice] = []
    used = new_used = 0.0
    while new or known:
        left = budget_s - used
        if not known:
            pick = new.pop(0)
        elif not new or left <= 0:
            pick = known.pop(0) if not new or known[0].score >= new[0].score else new.pop(0)
        else:
            owed = reserve - new_used                  # what new slices are still owed of the reserve
            band_waits = id(new[0]) in first_ids
            eats_reserve = owed > 0 and left - known[0].dwell_s < owed
            pick = new.pop(0) if band_waits or eats_reserve or new[0].score >= known[0].score else known.pop(0)
        out.append(pick)
        dwell = min(float(pick.dwell_s), max(0.0, left))
        used += dwell
        if pick.new:
            new_used += dwell
    return out
