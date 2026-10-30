"""Aggregator: turns parser events into aggregated SQLite rows (DESIGN.md §3)."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..db import Store, canonical_json
from .events import UNTESTED_ON_AIR, Burst, Event, Meta, NoSync, Quality, RawOpcode, Structural, Unrecognized

_DIGITS = re.compile(r"\d+")

# dedup granularity per structural kind (seconds); None = once per distinct payload for ever
_DEDUP_WINDOW: dict[str, int | None] = {
    "capplus_site": 86400,
    "capplus_adjacent": 86400, "conplus_adjacent": 86400, "conplus_sys": 86400,
    "tier3_sys_parms": 86400, "tier3_bcast": 86400, "xpt_site_status": 86400, "xpt_adjacent": 86400, "xpt_slco": 86400,
    "capplus_rest_change": 3600,
    "preamble": 3600, "data_header": 3600, "grant": 3600, "ip_map": 3600, "data_response": 3600, "lrrp": 3600,
    "activity_update": 3600, "protected_lc": 3600,
    "moto_data_channel": 86400, "slco_other": 86400, "unknown_csbk": 86400, "capmax_slco": 86400,
    "capmax_activity": 3600,
    "call": 60,
    # capmax_channel_update: one row per assignment (see Aggregator._assignment), not per window
}
ASSIGNMENT_GAP_S = 15    # Capacity Max channel updates for one TG/LCN/slot this close together are one assignment


def pattern_of(text: str) -> str:
    return _DIGITS.sub("#", text)[:100]


@dataclass
class IngestStats:
    source: str
    path: str | None
    freq_hz: int | None
    carrier_id: int | None
    lines: int = 0
    parsed: int = 0
    unrecognized_total: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    bursts: Counter = field(default_factory=Counter)        # category -> count
    burst_kinds: Counter = field(default_factory=Counter)   # DSD-FME burst label -> count
    cc_histogram: Counter = field(default_factory=Counter)
    structural: Counter = field(default_factory=Counter)    # kind -> count (all, before dedup)
    stored_events: Counter = field(default_factory=Counter) # kind -> new rows
    quality: Counter = field(default_factory=Counter)
    no_sync: int = 0
    rest_transitions: int = 0
    identities: Counter = field(default_factory=Counter)    # kind -> distinct values
    unrecognized: dict[str, tuple[int, str]] = field(default_factory=dict)
    csbk_opcodes: Counter = field(default_factory=Counter)  # "FID 10 opcode 21" -> raw CSBKs seen (-Z)
    meta: dict[str, str] = field(default_factory=dict)
    variant: str = "unknown"
    variant_confidence: float = 0.0
    skipped: bool = False
    note: str = ""

    @property
    def untested_kinds(self) -> list[str]:
        return sorted(k for k in self.structural if k in UNTESTED_ON_AIR)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "path": self.path, "freq_hz": self.freq_hz, "carrier_id": self.carrier_id,
            "lines": self.lines, "parsed": self.parsed, "unrecognized_total": self.unrecognized_total,
            "first_ts": self.first_ts, "last_ts": self.last_ts,
            "bursts": dict(self.bursts), "burst_kinds": dict(self.burst_kinds), "cc_histogram": dict(self.cc_histogram),
            "structural": dict(self.structural), "stored_events": dict(self.stored_events), "quality": dict(self.quality),
            "no_sync": self.no_sync, "rest_transitions": self.rest_transitions, "identities": dict(self.identities),
            "unrecognized": {k: {"count": c, "example": e} for k, (c, e) in self.unrecognized.items()},
            "csbk_opcodes": dict(self.csbk_opcodes),
            "meta": self.meta, "variant": self.variant, "variant_confidence": self.variant_confidence,
            "untested_on_air": self.untested_kinds, "skipped": self.skipped, "note": self.note,
        }


class Aggregator:
    """Feed parser events for ONE carrier; ``flush()`` writes everything to the store."""

    def __init__(self, store: Store, carrier_id: int, source: str, bucket_minutes: int, stats: IngestStats,
                 seconds_observed: float | None = None, snr_db: float | None = None):
        self.store = store
        self.snr_db = snr_db                 # survey SNR of this carrier, copied into every bucket
        self.carrier_id = carrier_id
        self.source = source
        self.bucket_s = max(60, int(bucket_minutes) * 60)
        self.stats = stats
        self.fixed_seconds = seconds_observed
        self._buckets: dict[int, dict[str, Any]] = {}
        self._bucket_span: dict[int, list[int]] = {}
        self._identities: dict[tuple[str, int], dict[str, Any]] = {}
        self._last_rest: int | None = None
        self._last_site: int | None = None
        self._status_key: str | None = None
        self._status_ts: int | None = None          # ts at which the current status signature started
        self._last_status_seen_ts: int | None = None  # ts of the most recent Channel Status CSBK
        self._distinct_values: dict[str, set[int]] = defaultdict(set)
        self._assignments: dict[tuple, list] = {}     # (lcn, slot, tg, mode) → [dedup key, last ts]

    # -- helpers ---------------------------------------------------------------------------
    def _bucket(self, ts: int) -> dict[str, Any]:
        start = ts - (ts % self.bucket_s)
        b = self._buckets.get(start)
        if b is None:
            b = self._buckets[start] = {"cc_histogram": Counter()}
            self._bucket_span[start] = [ts, ts]
        span = self._bucket_span[start]
        span[0] = min(span[0], ts)
        span[1] = max(span[1], ts)
        return b

    def _inc(self, ts: int | None, key: str, n: int = 1) -> None:
        if ts is None:
            return
        b = self._bucket(ts)
        b[key] = b.get(key, 0) + n

    def _note_ts(self, ts: int | None) -> None:
        if ts is None:
            return
        self.stats.first_ts = ts if self.stats.first_ts is None else min(self.stats.first_ts, ts)
        self.stats.last_ts = ts if self.stats.last_ts is None else max(self.stats.last_ts, ts)

    def _sight(self, kind: str, value: int, ts: int, role: str) -> None:
        if value <= 0:
            return
        day = ts // 86400
        rec = self._identities.setdefault((kind, value), {"first": ts, "last": ts, "days": defaultdict(lambda: [0, set()])})
        rec["first"] = min(rec["first"], ts)
        rec["last"] = max(rec["last"], ts)
        d = rec["days"][day]
        d[0] += 1
        d[1].add(role)
        self._distinct_values[kind].add(value)

    def _dedup_key(self, ev: Structural) -> str:
        window = _DEDUP_WINDOW.get(ev.kind)
        payload = dict(ev.payload)
        if ev.kind == "moto_data_channel":
            payload = {"opcode": payload.get("opcode"), "prefix": " ".join(str(payload.get("bytes", "")).split()[:3])}
        if ev.kind == "capplus_site":
            payload = {"site": payload.get("site"), "rest_lsn": payload.get("rest_lsn")}
        if ev.kind == "lrrp":
            payload = {k: payload.get(k) for k in ("src", "tgt", "direction")}
        if ev.kind == "call":
            payload = {k: payload.get(k) for k in ("slot", "src", "tgt", "group")}
        if ev.kind == "capmax_slco":
            payload = {"data": payload.get("data")}
        if ev.kind == "tier3_sys_parms":
            payload.pop("csc", None)     # the common slot counter changes every frame: not a new announcement
        body = canonical_json(payload)
        if window is None:
            return body
        return f"{ev.ts // window}|{body}"

    # -- event handling --------------------------------------------------------------------
    def feed(self, ev: Event) -> None:
        st = self.stats
        if isinstance(ev, Burst):
            self._note_ts(ev.ts)
            st.burst_kinds[ev.kind] += 1
            if ev.kind == "OTHER_PROTO":
                st.bursts["other_proto"] += 1
                return
            cat = ev.category
            st.bursts[cat] += 1
            self._inc(ev.ts, "bursts")
            if cat in ("csbk", "voice", "data", "idle"):
                self._inc(ev.ts, cat)
            if ev.crc_err or ev.kind == "ERR":
                self._inc(ev.ts, "crc_err")
                st.quality["burst_err"] += 1
            if ev.slot == 1:
                self._inc(ev.ts, "slot1_syncs")
            elif ev.slot == 2:
                self._inc(ev.ts, "slot2_syncs")
            if ev.cc is not None:
                self._bucket(ev.ts)["cc_histogram"][str(ev.cc)] += 1
                st.cc_histogram[str(ev.cc)] += 1
            return
        if isinstance(ev, NoSync):
            st.no_sync += 1
            self._inc(ev.ts, "no_sync")
            return
        if isinstance(ev, Quality):
            st.quality[ev.counter] += 1
            if ev.counter in ("crc_err", "slco_crc"):
                self._inc(ev.ts, "crc_err")
            return
        if isinstance(ev, Meta):
            st.meta.setdefault(ev.key, ev.value)
            return
        if isinstance(ev, RawOpcode):
            st.csbk_opcodes[ev.key] += 1
            if not ev.understood:
                # a message nobody decodes yet: report it once per opcode with an example
                pat = f"raw CSBK {ev.key} (not decoded)"
                cnt, ex = st.unrecognized.get(pat, (0, f"{pat}: {ev.example}"))
                st.unrecognized[pat] = (cnt + 1, ex)
                st.unrecognized_total += 1
            return
        if isinstance(ev, Unrecognized):
            pat = pattern_of(ev.text)
            cnt, ex = st.unrecognized.get(pat, (0, ev.text))
            st.unrecognized[pat] = (cnt + 1, ex)
            st.unrecognized_total += 1
            return
        if isinstance(ev, Structural):
            self._structural(ev)

    def _structural(self, ev: Structural) -> None:
        st = self.stats
        st.structural[ev.kind] += 1
        self._note_ts(ev.ts)
        for kind, value, role in ev.identities:
            self._sight(kind, value, ev.ts, role)

        if ev.kind == "capplus_channel_status":
            self._last_status_seen_ts = ev.ts
            self._rest(ev.ts, ev.payload["rest_lsn"])
            states = {k: v for k, v in ev.payload.get("lsn_states", {}).items() if v != "idle"}
            has_list = bool(states) or bool(ev.payload.get("banks")) or ev.payload.get("block") not in ("initial",)
            if not has_list and self._status_key is not None and f'"rest":{ev.payload["rest_lsn"]}' in self._status_key:
                # an Initial Block carries no LSN list: it does not change the current signature
                key = f"{self._status_key}@{self._status_ts}"
            else:
                sig = canonical_json({"rest": ev.payload["rest_lsn"], "states": states, "banks": ev.payload.get("banks")})
                if sig != self._status_key:
                    self._status_key = sig
                    self._status_ts = ev.ts
                key = f"{sig}@{self._status_ts}"
        elif ev.kind == "capmax_channel_update" or (ev.kind == "grant" and ev.payload.get("lpcn") is not None):
            key = self._assignment(ev)
        elif ev.kind == "capplus_site":
            self._last_site = ev.payload["site"]
            self._rest(ev.ts, ev.payload["rest_lsn"], from_slco=True)
            key = self._dedup_key(ev)
        else:
            key = self._dedup_key(ev)

        new = self.store.add_csbk_event(self.carrier_id, ev.ts, ev.slot, ev.cc, ev.kind, ev.payload, key)
        if new:
            st.stored_events[ev.kind] += 1

    def _assignment(self, ev: Structural) -> str:
        """One row per assignment of a talkgroup to an LCN and slot: its ``ts`` is when the control
        channel first announced it, ``last_ts`` the last update. The correlator lines up the start
        with key-ups; a row per time window would make every window edge look like a new call."""
        p = ev.payload
        if ev.kind == "grant":       # a Tier III channel grant: the same, as the grant announces it
            k = ("grant", p.get("lpcn"), p.get("ts"), p.get("tgt"), p.get("code"))
        else:
            k = (p.get("lcn"), p.get("slot"), p.get("tg"), p.get("mode"))
        open_ = self._assignments.get(k)
        if open_ is None or ev.ts - open_[1] > ASSIGNMENT_GAP_S:
            open_ = self._assignments[k] = [f"{ev.ts}|{canonical_json(dict(p))}", ev.ts]
        open_[1] = ev.ts
        return open_[0]

    def _rest(self, ts: int, rest_lsn: int, from_slco: bool = False) -> None:
        if rest_lsn == self._last_rest:
            self._bucket(ts)["rest_lsn_last"] = rest_lsn
            return
        if (from_slco and self._last_rest is not None and self._last_status_seen_ts is not None
                and ts - self._last_status_seen_ts < 10):
            # the CSBK channel status is authoritative while it is flowing; the short LC lags it
            return
        self._bucket(ts)["rest_lsn_last"] = rest_lsn
        self.store.add_rest_event(self.carrier_id, ts, self._last_site, rest_lsn, self._last_rest)
        self._last_rest = rest_lsn
        self.stats.rest_transitions += 1

    # -- flush -----------------------------------------------------------------------------
    def flush(self, partial_before: int | None = None) -> IngestStats:
        """Write accumulated rows. With ``partial_before``, only buckets that end before that
        epoch are written (and dropped from memory); identities are always written and reset.
        Live streams call this periodically so a killed collector loses at most one bucket."""
        buckets = self._buckets
        if partial_before is not None:
            buckets = {st: b for st, b in self._buckets.items() if st + self.bucket_s <= partial_before}
        total_span = 0
        for start, b in buckets.items():
            lo, hi = self._bucket_span[start]
            span = min(hi - lo + 1, self.bucket_s)
            total_span += span
            b["seconds_observed"] = span
        if self.fixed_seconds is not None and total_span > 0 and partial_before is None:
            scale = self.fixed_seconds / total_span
            for b in buckets.values():
                b["seconds_observed"] = round(b["seconds_observed"] * scale, 3)
        for start, b in sorted(buckets.items()):
            agg = dict(b)
            agg["cc_histogram"] = dict(b["cc_histogram"])
            if self.snr_db is not None:
                agg.setdefault("snr_db_max", self.snr_db)
                agg.setdefault("snr_db_avg", self.snr_db)
            self.store.add_observation(self.carrier_id, start, self.source, agg)
            if partial_before is not None:
                del self._buckets[start]
                del self._bucket_span[start]
        for (kind, value), rec in self._identities.items():
            ident_id = self.store.upsert_identity(kind, value, rec["first"])
            self.store.upsert_identity(kind, value, rec["last"])
            for day, (count, roles) in rec["days"].items():
                self.store.add_sighting(ident_id, self.carrier_id, day, count, roles)
        self._identities.clear()
        for kind, values in self._distinct_values.items():
            self.stats.identities[kind] = len(values)
        self.store.update_carrier_summary(self.carrier_id)
        if self.stats.first_ts is not None:
            self.store.upsert_carrier_seen(self.carrier_id, self.stats.first_ts, self.stats.last_ts or self.stats.first_ts)
        row = self.store.conn.execute("SELECT variant, variant_confidence FROM carriers WHERE id = ?", (self.carrier_id,)).fetchone()
        self.stats.variant, self.stats.variant_confidence = row["variant"], row["variant_confidence"]
        return self.stats
