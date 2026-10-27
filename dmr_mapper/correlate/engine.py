"""Correlator run: collect evidence → upsert into ``evidence``/``links`` → score → derive
sites and systems. Manual evidence (source='manual') is never touched by the automatic pass."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from ..db import Store, canonical_json
from .context import Context
from .derive import derive_sites_and_systems
from .evidence import COLLECTORS
from .model import EVIDENCE_CLASS, CorrelateReport, Evidence
from .scoring import ScoringConfig, score_rows


def _link_id(store: Store, a_id: int, b_id: int, kind: str, now: int) -> int:
    a_id, b_id = min(a_id, b_id), max(a_id, b_id)
    row = store.conn.execute("SELECT id FROM links WHERE a_carrier_id = ? AND b_carrier_id = ? AND kind = ?",
                             (a_id, b_id, kind)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = store.conn.execute(
        "INSERT INTO links (a_carrier_id, b_carrier_id, kind, score, label, tier2_only, updated_at) VALUES (?, ?, ?, 0, 'weak', 0, ?)",
        (a_id, b_id, kind, now))
    return int(cur.lastrowid)


def upsert_evidence(store: Store, link_id: int, ev: Evidence, cfg: ScoringConfig, source: str = "auto") -> None:
    cls = EVIDENCE_CLASS.get(ev.type, "identity")
    store.conn.execute(
        "INSERT INTO evidence (link_id, type, class, weight, raw_score, observed_at, first_observed_at, support_count, details, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(link_id, type, source) DO UPDATE SET weight = excluded.weight, raw_score = excluded.raw_score,"
        " observed_at = MAX(observed_at, excluded.observed_at), first_observed_at = MIN(first_observed_at, excluded.first_observed_at),"
        " support_count = excluded.support_count, details = excluded.details",
        (link_id, ev.type, cls, cfg.weight(ev.type), ev.raw, ev.observed_at, ev.first_observed_at, ev.support,
         canonical_json(ev.details), source))


def score_link(store: Store, link_id: int, cfg: ScoringConfig, now: int) -> dict[str, Any]:
    rows = store.rows("SELECT * FROM evidence WHERE link_id = ?", (link_id,))
    ls = score_rows([dict(r) for r in rows], cfg, now)
    store.conn.execute("UPDATE links SET score = ?, label = ?, tier2_only = ?, updated_at = ? WHERE id = ?",
                       (ls.score, ls.label, int(ls.tier2_only), now, link_id))
    return {"score": ls.score, "label": ls.label, "tier2_only": ls.tier2_only}


def run(store: Store, config: dict[str, Any] | None, now: int | None = None, window_days: int | None = None) -> CorrelateReport:
    cfg = ScoringConfig(config)
    now = int(now if now is not None else time.time())
    window = int(window_days or cfg.window_days)
    ctx = Context(store, now, window)
    report = CorrelateReport(now=now, window_days=window, carriers=len(ctx.carriers))

    collected: list[Evidence] = []
    for name, collector in COLLECTORS.items():
        evs = collector(ctx, cfg)
        report.evidence_collected[name] = len(evs)
        collected.extend(evs)

    lcn_hints: dict[int, Counter] = {}
    touched: set[int] = set()
    for ev in collected:
        a, b = ctx.by_freq.get(ev.a_freq), ctx.by_freq.get(ev.b_freq)
        if a is None or b is None:
            continue
        link_id = _link_id(store, a.id, b.id, ev.kind, now)
        upsert_evidence(store, link_id, ev, cfg)
        touched.add(link_id)
        for freq, lcn in ev.lcn_hints.items():
            lcn_hints.setdefault(freq, Counter())[lcn] += 1

    # Score every link (old evidence keeps decaying even when it was not collected again).
    labels: Counter = Counter()
    for r in store.rows("SELECT id FROM links"):
        res = score_link(store, int(r["id"]), cfg, now)
        labels[res["label"]] += 1
    report.links_scored = sum(labels.values())
    report.links_by_label = dict(labels)

    sites, systems = derive_sites_and_systems(store, ctx, cfg, now, {f: c.most_common(1)[0][0] for f, c in lcn_hints.items()})
    report.sites, report.systems = sites, systems
    store.commit()
    return report


def explain(store: Store, link_id: int, config: dict[str, Any] | None, now: int | None = None) -> dict[str, Any]:
    cfg = ScoringConfig(config)
    now = int(now if now is not None else time.time())
    link = store.conn.execute(
        "SELECT l.*, a.freq_hz AS a_freq, b.freq_hz AS b_freq FROM links l JOIN carriers a ON a.id = l.a_carrier_id"
        " JOIN carriers b ON b.id = l.b_carrier_id WHERE l.id = ?", (link_id,)).fetchone()
    if link is None:
        raise KeyError(f"no link with id {link_id}")
    rows = [dict(r) for r in store.rows("SELECT * FROM evidence WHERE link_id = ? ORDER BY id", (link_id,))]
    ls = score_rows(rows, cfg, now)
    return {
        "link_id": link_id, "a_freq_hz": link["a_freq"], "b_freq_hz": link["b_freq"], "kind": link["kind"],
        "score": ls.score, "label": ls.label, "tier2_only": ls.tier2_only, "positive": ls.positive,
        "penalty": ls.penalty, "contradictions": ls.contradictions, "stored_score": link["score"], "stored_label": link["label"],
        "evidence": [
            {"type": e.type, "class": e.cls, "source": e.source, "weight": e.weight, "raw": e.raw, "decay": round(e.decay, 3),
             "contribution": round(e.contribution, 4), "observed_at": e.observed_at, "first_observed_at": e.first_observed_at,
             "support": e.support, "details": e.details}
            for e in ls.rows
        ],
    }


def find_link(store: Store, a_freq: int, b_freq: int, kind: str | None = None) -> list[int]:
    a, b = min(a_freq, b_freq), max(a_freq, b_freq)
    sql = ("SELECT l.id FROM links l JOIN carriers a ON a.id = l.a_carrier_id JOIN carriers b ON b.id = l.b_carrier_id"
           " WHERE a.freq_hz = ? AND b.freq_hz = ?")
    params: tuple = (a, b)
    if kind:
        sql += " AND l.kind = ?"
        params += (kind,)
    return [int(r["id"]) for r in store.rows(sql + " ORDER BY l.score DESC", params)]


def propose(store: Store, a_freq: int, b_freq: int, kind: str, confidence: float, note: str,
            config: dict[str, Any] | None, now: int | None = None) -> dict[str, Any]:
    """Manual evidence from an operator or from Claude via MCP. Stored with source='manual' and
    never decayed or overwritten by the automatic pass."""
    cfg = ScoringConfig(config)
    now = int(now if now is not None else time.time())
    a = store.conn.execute("SELECT id FROM carriers WHERE freq_hz = ?", (a_freq,)).fetchone()
    b = store.conn.execute("SELECT id FROM carriers WHERE freq_hz = ?", (b_freq,)).fetchone()
    if a is None or b is None:
        raise KeyError("both carriers must exist in the database before a link can be proposed")
    if kind not in ("same_site", "same_system", "neighbor_site"):
        raise ValueError(f"kind must be same_site, same_system or neighbor_site, got {kind!r}")
    link_id = _link_id(store, int(a["id"]), int(b["id"]), kind, now)
    ev = Evidence(a_freq, b_freq, kind, "manual", max(0.0, min(1.0, float(confidence))), now, now, 1, {"note": note})
    upsert_evidence(store, link_id, ev, cfg, source="manual")
    res = score_link(store, link_id, cfg, now)
    store.commit()
    return {"link_id": link_id, **res}
