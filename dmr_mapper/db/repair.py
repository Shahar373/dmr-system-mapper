"""One-off repair of carriers' seen range (``mapper repair-seen``).

Until 2026-11-03 a decode set ``last_seen`` (and, for a new carrier, ``first_seen``) to the time
it started listening, whether or not DSD-FME heard anything, and the offline ingest did the same
with a log's start time (DESIGN.md §12). This recomputes the range from what was really heard:

- observation buckets with DMR bursts (never pruned; to the bucket: ``bucket_start`` up to the
  end of the bucket),
- structural events (``csbk_events.ts`` / ``last_ts``) and rest-channel transitions,
- watch key-ups (RF evidence of a carrier, DMR or not).

It only takes back what listening added: ``last_seen`` can move back and ``first_seen`` forward,
never the other way. A carrier with none of that evidence was only listened to and becomes 0 / 0
("never heard"). Key-ups are pruned after ``retention.keyup_days``: a value older than that may
rest on a key-up that is gone, so it is left as it is. Running it again changes nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .store import Store


@dataclass(slots=True)
class SeenFix:
    carrier_id: int
    freq_hz: int
    old: tuple[int, int]
    new: tuple[int, int]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"carrier_id": self.carrier_id, "freq_hz": self.freq_hz, "old": list(self.old), "new": list(self.new),
                "note": self.note}


def _heard_range(store: Store, cid: int, bucket_s: int) -> tuple[int | None, int | None]:
    firsts: list[int] = []
    lasts: list[int] = []
    for q, args in (
        ("SELECT MIN(bucket_start), MAX(bucket_start) + ? FROM observations WHERE carrier_id = ? AND bursts > 0", (bucket_s - 1, cid)),
        ("SELECT MIN(ts), MAX(last_ts) FROM csbk_events WHERE carrier_id = ?", (cid,)),
        ("SELECT MIN(ts), MAX(ts) FROM rest_events WHERE carrier_id = ?", (cid,)),
        ("SELECT MIN(start), MAX(end) FROM keyups WHERE carrier_id = ?", (cid,)),
    ):
        lo, hi = store.conn.execute(q, args).fetchone()
        if lo is not None:
            firsts.append(int(lo))
            lasts.append(int(hi))
    return (min(firsts) if firsts else None), (max(lasts) if lasts else None)


def repair_seen(store: Store, cfg: dict[str, Any], now: int | None = None, apply: bool = True) -> list[SeenFix]:
    """The carriers whose seen range changes, and (with ``apply``) the change written."""
    now = int(now if now is not None else time.time())
    bucket_s = 60 * int((cfg.get("ingest", {}) or {}).get("bucket_minutes", 15))
    keyup_days = int((cfg.get("retention", {}) or {}).get("keyup_days", 180) or 0)
    cutoff = now - keyup_days * 86400 if keyup_days > 0 else None    # key-ups before this may be gone
    fixes: list[SeenFix] = []
    for c in store.carriers():
        cid, first, last = int(c["id"]), int(c["first_seen"]), int(c["last_seen"])
        if last <= 0:
            continue                                         # never heard already
        lo, hi = _heard_range(store, cid, bucket_s)
        new_first, new_last = first, last
        if hi is None:
            if cutoff is not None and last < cutoff:
                continue                                     # may rest on a pruned key-up: unknown, kept
            new_first, new_last, note = 0, 0, "never heard: only listened to"
        else:
            if cutoff is None or last >= cutoff:
                new_last = min(last, hi)
            if cutoff is None or first >= cutoff:
                new_first = max(first, lo) if first > 0 else lo
            new_first = min(new_first, new_last)
            note = "listened to after it was last heard" if new_last < last else "listened to before it was first heard"
        if (new_first, new_last) == (first, last):
            continue
        fixes.append(SeenFix(cid, int(c["freq_hz"]), (first, last), (new_first, new_last), note))
        if apply:
            store.conn.execute("UPDATE carriers SET first_seen = ?, last_seen = ? WHERE id = ?", (new_first, new_last, cid))
    if apply:
        store.commit()
    return fixes
