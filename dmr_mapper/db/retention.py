"""How long identifying data is kept (``retention.identity_days``, default 90).

Radio IDs and talkgroups, and the structural events that carry them (calls, preambles, data
headers, IP maps, LRRP reports, grants), are deleted once they are older than that. Site, system
and neighbour announcements, observations, links and evidence stay: they describe networks, not
people. The receiver's own GPS track is kept unless ``retention.track_days`` is set.
"""

from __future__ import annotations

import time
from typing import Any

from .store import Store

IDENTITY_EVENT_KINDS = ("call", "preamble", "data_header", "ip_map", "lrrp", "data_response", "grant")


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
    track = int(r.get("track_days", 0) or 0)
    if track > 0:
        cur = store.conn.execute("DELETE FROM positions WHERE ts < ?", (now - track * 86400,))
        out["positions"] = cur.rowcount
    store.commit()
    return out
