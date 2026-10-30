"""Build a NetworkX graph of carriers, sites and links from the store."""

from __future__ import annotations

import json
from typing import Any

import networkx as nx

from ..correlate.model import LABELS
from ..db import Store

LABEL_RANK = {label: i for i, label in enumerate(LABELS)}   # confirmed=0 … weak=3
# a system's or site's name, when the operator gave one, before its automatic label
_NAMED = "CASE WHEN {t}.name IS NOT NULL THEN {t}.name || ' (' || {t}.label || ')' ELSE {t}.label END"


def build(store: Store, min_label: str = "possible", include_announced: bool = True) -> nx.Graph:
    """Nodes: carriers (``kind='carrier'``) and announced-only sites (``kind='site'``).
    Edges: links with label at least ``min_label`` (``kind``, ``score``, ``label``, ``tier2_only``,
    ``evidence`` summary). Graph attributes carry the sites/systems tables for grouping."""
    max_rank = LABEL_RANK.get(min_label, LABEL_RANK["possible"])
    g = nx.Graph()
    site_of: dict[int, dict[str, Any]] = {}
    for r in store.rows(
            f"SELECT cs.carrier_id, cs.lcn, cs.confidence, s.id AS site_id, s.site_number, {_NAMED.format(t='s')} AS site_label, s.system_id,"
            f" {_NAMED.format(t='sy')} AS system_label, sy.variant AS system_variant FROM carrier_sites cs JOIN sites s ON s.id = cs.site_id"
            " JOIN systems sy ON sy.id = s.system_id"):
        site_of[int(r["carrier_id"])] = dict(r)
    for c in store.carriers():
        meta = site_of.get(int(c["id"]), {})
        g.add_node(f"c{c['id']}", kind="carrier", carrier_id=int(c["id"]), freq_hz=int(c["freq_hz"]),
                   label=f"{c['freq_hz'] / 1e6:.4f}", variant=c["variant"], variant_confidence=c["variant_confidence"],
                   cc=c["dominant_cc"], band=c["band"], first_seen=c["first_seen"], last_seen=c["last_seen"],
                   site_id=meta.get("site_id"), site_number=meta.get("site_number"), site_label=meta.get("site_label"),
                   system_id=meta.get("system_id"), system_label=meta.get("system_label"), lcn=meta.get("lcn"),
                   activity=_activity(store, int(c["id"])))
    if include_announced:
        for s in store.rows(f"SELECT s.*, {_NAMED.format(t='s')} AS shown, {_NAMED.format(t='sy')} AS system_label"
                            " FROM sites s JOIN systems sy ON sy.id = s.system_id WHERE s.directly_observed = 0"):
            node = f"s{s['id']}"
            g.add_node(node, kind="site", site_id=int(s["id"]), site_number=s["site_number"], label=s["name"] or s["label"].split(" (")[0],
                       site_label=s["shown"],
                       system_id=s["system_id"], system_label=s["system_label"], announced_only=True)
            # attach to every carrier of the same system that announced it
            for cid, meta in site_of.items():
                if meta.get("system_id") == s["system_id"]:
                    g.add_edge(f"c{cid}", node, kind="announced", score=0.0, label="announced", tier2_only=False, evidence=[])
    for l in store.rows(
            "SELECT l.*, (SELECT GROUP_CONCAT(type || ':' || ROUND(raw_score, 2), ', ') FROM evidence e WHERE e.link_id = l.id) AS ev"
            " FROM links l"):
        if LABEL_RANK.get(l["label"], 3) > max_rank:
            continue
        a, b = f"c{l['a_carrier_id']}", f"c{l['b_carrier_id']}"
        if not (g.has_node(a) and g.has_node(b)):
            continue
        existing = g.get_edge_data(a, b)
        if existing and existing.get("score", 0) >= float(l["score"]):
            existing.setdefault("also", []).append({"kind": l["kind"], "score": l["score"], "label": l["label"]})
            continue
        g.add_edge(a, b, kind=l["kind"], link_id=int(l["id"]), score=float(l["score"]), label=l["label"],
                   tier2_only=bool(l["tier2_only"]), evidence=(l["ev"] or "").split(", ") if l["ev"] else [])
    g.graph["systems"] = [dict(r) for r in store.rows("SELECT * FROM systems ORDER BY id")]
    g.graph["sites"] = [dict(r) for r in store.rows("SELECT * FROM sites ORDER BY system_id, id")]
    g.graph["min_label"] = min_label
    return g


def _activity(store: Store, carrier_id: int) -> dict[str, Any]:
    r = store.conn.execute(
        "SELECT SUM(seconds_observed) AS secs, SUM(bursts) AS bursts, SUM(voice) AS voice, SUM(csbk) AS csbk, COUNT(*) AS buckets"
        " FROM observations WHERE carrier_id = ?", (carrier_id,)).fetchone()
    return {"seconds": round(float(r["secs"] or 0), 1), "bursts": int(r["bursts"] or 0), "voice": int(r["voice"] or 0),
            "csbk": int(r["csbk"] or 0), "buckets": int(r["buckets"] or 0)}


def to_json(g: nx.Graph) -> dict[str, Any]:
    return {
        "nodes": [{"id": n, **d} for n, d in g.nodes(data=True)],
        "edges": [{"source": a, "target": b, **d} for a, b, d in g.edges(data=True)],
        "systems": g.graph.get("systems", []),
        "sites": g.graph.get("sites", []),
        "min_label": g.graph.get("min_label"),
    }


def write_graphml(g: nx.Graph, path: str) -> None:
    h = nx.Graph()
    for n, d in g.nodes(data=True):
        h.add_node(n, **{k: (json.dumps(v) if isinstance(v, (dict, list)) else ("" if v is None else v)) for k, v in d.items()})
    for a, b, d in g.edges(data=True):
        h.add_edge(a, b, **{k: (json.dumps(v) if isinstance(v, (dict, list)) else ("" if v is None else v)) for k, v in d.items()})
    nx.write_graphml(h, path)
