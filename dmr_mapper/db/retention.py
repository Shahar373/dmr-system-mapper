"""How long identifying data is kept (``retention.identity_days``, default 90).

Radio IDs and talkgroups, and the structural events that carry them (calls, preambles, data
headers, IP maps, LRRP reports, grants, Capacity Max channel updates and activity updates), are
deleted once they are older than that, and the raw example lines kept for unrecognized patterns
are reduced to the pattern. Site, system
and neighbour announcements, observations, links and evidence stay: they describe networks, not
people. The receiver's own GPS track is kept unless ``retention.track_days`` is set; watch key-ups
for ``retention.keyup_days`` (180).
"""

from __future__ import annotations

import time
from typing import Any

from .store import Store

IDENTITY_EVENT_KINDS = ("call", "preamble", "data_header", "ip_map", "lrrp", "data_response", "grant",
                        "capmax_channel_update", "capmax_activity")


def prune(store: Store, cfg: dict[str, Any], now: int | None = None) -> dict[str, int]:
    r = cfg.get("retention", {}) or {}
    now = int(now if now is not None else time.time())
    out: dict[str, int] = {}
    days = int(r.get("identity_days", 90) or 0)
    if days > 0:
        cutoff = now - days * 86400
        cur = store.conn.execute("DELETE FROM identity_sightings WHERE day < ?", (cutoff // 86400,))
        out["identity_sightings"] = cur.rowcount
        cur = store.conn.execute(
            "DELETE FROM identities WHERE last_seen < ? AND id NOT IN (SELECT DISTINCT identity_id FROM identity_sightings)", (cutoff,))
        out["identities"] = cur.rowcount
        q = ",".join("?" for _ in IDENTITY_EVENT_KINDS)
        cur = store.conn.execute(f"DELETE FROM csbk_events WHERE kind IN ({q}) AND last_ts < ?", (*IDENTITY_EVENT_KINDS, cutoff))
        out["identity_events"] = cur.rowcount
        # the example kept with each unrecognized pattern is a raw line (or raw CSBK bytes) and may
        # hold IDs: after the same time only the pattern (digits masked) stays
        cur = store.conn.execute(
            "UPDATE unrecognized_lines SET example = pattern WHERE example != pattern"
            " AND run_id IN (SELECT id FROM ingest_runs WHERE started < ?)", (cutoff,))
        out["unrecognized_examples"] = cur.rowcount
    keyup = int(r.get("keyup_days", 180) or 0)
    if keyup > 0:
        cur = store.conn.execute("DELETE FROM keyups WHERE start < ?", (now - keyup * 86400,))
        out["keyups"] = cur.rowcount
    track = int(r.get("track_days", 0) or 0)
    if track > 0:
        cur = store.conn.execute("DELETE FROM positions WHERE ts < ?", (now - track * 86400,))
        out["positions"] = cur.rowcount
    store.commit()
    return out
