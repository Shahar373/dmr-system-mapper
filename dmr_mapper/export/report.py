"""Text report of systems, sites, carriers and links, and the "export everything" bundle used by
``mapper export --all``, the console's export key and the nightly job."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from ..db import Store
from .graph import LABEL_RANK, build, to_json, write_graphml
from .html import write


def systems_report(store: Store, min_label: str = "possible") -> tuple[dict[str, Any], str]:
    max_rank = LABEL_RANK.get(min_label, 2)
    systems = [dict(r) for r in store.rows("SELECT * FROM systems ORDER BY id")]
    sites = [dict(r) for r in store.rows("SELECT * FROM sites ORDER BY system_id, directly_observed DESC, id")]
    members = [dict(r) for r in store.rows(
        "SELECT cs.site_id, cs.lcn, cs.confidence, c.id AS carrier_id, c.freq_hz, c.variant, c.dominant_cc, c.last_seen"
        " FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id ORDER BY c.freq_hz")]
    links = [dict(r) for r in store.rows(
        "SELECT l.*, a.freq_hz AS a_freq, b.freq_hz AS b_freq FROM links l JOIN carriers a ON a.id = l.a_carrier_id"
        " JOIN carriers b ON b.id = l.b_carrier_id ORDER BY l.score DESC")]
    links = [l for l in links if LABEL_RANK.get(l["label"], 3) <= max_rank]
    data = {"systems": systems, "sites": sites, "carriers": members, "links": links}
    if not systems:
        return data, "no systems yet: run `mapper correlate` after ingesting"
    out: list[str] = []
    for sy in systems:
        out.append(f"system {sy['id']}: {sy['label']}  [{sy['variant']}]" + (f"  sys={sy['sys_code']}" if sy["sys_code"] else "")
                   + (f"  net={sy['net_id']}" if sy["net_id"] is not None else ""))
        for si in [x for x in sites if x["system_id"] == sy["id"]]:
            flag = "" if si["directly_observed"] else "  (announced only)"
            out.append(f"  site {si['id']}: {si['label']}{flag}")
            for m in [x for x in members if x["site_id"] == si["id"]]:
                lcn = f" LCN {m['lcn']}" if m["lcn"] else ""
                out.append(f"    {m['freq_hz'] / 1e6:10.4f} MHz  {m['variant']:8s} cc={m['dominant_cc']}{lcn}"
                           f"  last {datetime.fromtimestamp(m['last_seen'], tz=timezone.utc):%Y-%m-%d}")
    out.append(f"links (>= {min_label}): {len(links)}")
    for l in links:
        t2 = "  Tier II only" if l["tier2_only"] else ""
        out.append(f"  #{l['id']:<4d} {l['a_freq'] / 1e6:.4f} - {l['b_freq'] / 1e6:.4f}  {l['kind']:14s} {l['label']:9s} {l['score']:.2f}{t2}")
    return data, "\n".join(out)


def export_all(store: Store, cfg: dict[str, Any], out_dir: Path | None = None, min_label: str = "weak") -> tuple[list[Path], nx.Graph]:
    """map.html, map.json, map.graphml and systems.txt in one folder (default ``paths.exports``)."""
    from ..config import resolve_path

    out = Path(out_dir) if out_dir else resolve_path(cfg, "exports")
    out.mkdir(parents=True, exist_ok=True)
    g = build(store, min_label=min_label)
    html, js, gml, txt = out / "map.html", out / "map.json", out / "map.graphml", out / "systems.txt"
    write(g, str(html))
    js.write_text(json.dumps(to_json(g), indent=2), encoding="utf-8")
    write_graphml(g, str(gml))
    _, text = systems_report(store, min_label)
    txt.write_text(text + "\n", encoding="utf-8")
    return [html, js, gml, txt], g
