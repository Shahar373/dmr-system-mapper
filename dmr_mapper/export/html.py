"""Static single-file HTML map of the network graph (no CDN, inline SVG + a little JS).

Visual language, modelled on the P25 mapper:
* filled circle = carrier that was decoded; hollow dashed diamond = site known only from a
  neighbour announcement;
* solid accent edge = confirmed link; dashed = probable / possible; faint grey = weak;
  Tier II-only links are always dashed and carry a "T2" mark, never solid;
* nodes are laid out in rings per system (one ring per system, systems spread on a circle);
* clicking a node shows its details and the evidence of each link.
"""

from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone
from typing import Any

import networkx as nx

from .graph import to_json

PALETTE = {
    "bg": "#0b0f14", "panel": "#11171f", "raised": "#171f29", "fg": "#dce3ea", "muted": "#8b98a7", "dim": "#5d6975",
    "line": "#202a36", "accent": "#4aa3df", "accent_dim": "#2a6a94", "ok": "#4bb377", "warn": "#d9a441", "danger": "#d9534f",
}
EDGE_STYLE = {
    "confirmed": {"stroke": PALETTE["accent"], "width": 2.4, "dash": ""},
    "probable": {"stroke": PALETTE["ok"], "width": 1.8, "dash": "7 4"},
    "possible": {"stroke": PALETTE["warn"], "width": 1.4, "dash": "4 4"},
    "weak": {"stroke": PALETTE["dim"], "width": 1.0, "dash": "2 4"},
    "announced": {"stroke": PALETTE["dim"], "width": 1.0, "dash": "1 4"},
}
VARIANT_COLOR = {"capplus": "#4aa3df", "conplus": "#d9a441", "tier3": "#a978d8", "xpt": "#4bb377", "tier2": "#8b98a7", "unknown": "#5d6975"}


def layout(g: nx.Graph, width: int = 1100, height: int = 760) -> dict[str, tuple[float, float]]:
    """Deterministic ring layout: systems on an outer circle, each system's nodes on a small
    ring around its centre. No force layout, so the picture is stable between exports."""
    groups: dict[Any, list[str]] = {}
    for n, d in g.nodes(data=True):
        groups.setdefault(d.get("system_id"), []).append(n)
    pos: dict[str, tuple[float, float]] = {}
    cx, cy = width / 2, height / 2
    k = len(groups)
    big_r = min(width, height) * 0.36 if k > 1 else 0
    for i, (sys_id, nodes) in enumerate(sorted(groups.items(), key=lambda kv: (kv[0] is None, str(kv[0])))):
        ang = -math.pi / 2 + 2 * math.pi * i / max(k, 1)
        gx, gy = cx + big_r * math.cos(ang), cy + big_r * math.sin(ang)
        nodes = sorted(nodes, key=lambda n: (g.nodes[n].get("kind") != "carrier", g.nodes[n].get("freq_hz") or 0))
        m = len(nodes)
        small_r = 0 if m == 1 else min(28 + 14 * m, big_r * 0.75 if k > 1 else min(width, height) * 0.4)
        for j, n in enumerate(nodes):
            a = -math.pi / 2 + 2 * math.pi * j / max(m, 1)
            pos[n] = (gx + small_r * math.cos(a), gy + small_r * math.sin(a))
    return pos


def _fmt_ts(v: Any) -> str:
    if not v:
        return "-"
    return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render(g: nx.Graph, title: str = "DMR network map", generated_at: int | None = None) -> str:
    width, height = 1100, 760
    pos = layout(g, width, height)
    data = to_json(g)
    for node in data["nodes"]:
        node["x"], node["y"] = pos[node["id"]]
        node["first_seen_h"], node["last_seen_h"] = _fmt_ts(node.get("first_seen")), _fmt_ts(node.get("last_seen"))
    edges_svg = []
    for a, b, d in g.edges(data=True):
        (x1, y1), (x2, y2) = pos[a], pos[b]
        st = EDGE_STYLE.get(d.get("label", "weak"), EDGE_STYLE["weak"])
        dash = st["dash"]
        if d.get("tier2_only") and not dash:
            dash = "7 4"
        mid = ((x1 + x2) / 2, (y1 + y2) / 2)
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1
        bow = 0.12 * norm
        ctrl = (mid[0] - dy / norm * bow, mid[1] + dx / norm * bow)
        path = f"M{x1:.1f} {y1:.1f} Q{ctrl[0]:.1f} {ctrl[1]:.1f} {x2:.1f} {y2:.1f}"
        tag = "T2" if d.get("tier2_only") else ""
        edges_svg.append(
            f'<path class="edge" data-a="{a}" data-b="{b}" d="{path}" fill="none" stroke="{st["stroke"]}" '
            f'stroke-width="{st["width"]}" stroke-dasharray="{dash}" opacity="0.9"><title>'
            f'{html.escape(d.get("kind", ""))} {html.escape(str(d.get("label", "")))} {d.get("score", 0):.2f}</title></path>'
            + (f'<text x="{ctrl[0]:.1f}" y="{ctrl[1]:.1f}" class="t2" text-anchor="middle">{tag}</text>' if tag else "")
        )
    nodes_svg = []
    for node in data["nodes"]:
        x, y = node["x"], node["y"]
        if node.get("kind") == "site":
            shape = (f'<rect x="{x - 9:.1f}" y="{y - 9:.1f}" width="18" height="18" transform="rotate(45 {x:.1f} {y:.1f})" '
                     f'fill="{PALETTE["bg"]}" stroke="{PALETTE["muted"]}" stroke-width="1.6" stroke-dasharray="4 3"/>')
        else:
            color = VARIANT_COLOR.get(node.get("variant") or "unknown", VARIANT_COLOR["unknown"])
            shape = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" fill="{color}" fill-opacity="0.85" stroke="{PALETTE["fg"]}" stroke-width="1.2"/>'
        nodes_svg.append(
            f'<g class="node" data-id="{node["id"]}" transform="translate(0,0)">{shape}'
            f'<text x="{x:.1f}" y="{y + 24:.1f}" text-anchor="middle" class="lbl">{html.escape(str(node.get("label", "")))}</text></g>'
        )
    systems = data["systems"]
    legend = "".join(
        f'<li><svg width="60" height="16"><line x1="2" y1="8" x2="58" y2="8" stroke="{EDGE_STYLE[k]["stroke"]}" '
        f'stroke-width="{EDGE_STYLE[k]["width"]}" stroke-dasharray="{EDGE_STYLE[k]["dash"]}"/></svg>{k}</li>'
        for k in ("confirmed", "probable", "possible", "weak")
    )
    variants = "".join(f'<li><span class="sw" style="background:{c}"></span>{v}</li>' for v, c in VARIANT_COLOR.items())
    gen = _fmt_ts(generated_at) if generated_at else _fmt_ts(int(datetime.now(tz=timezone.utc).timestamp()))
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:{PALETTE["bg"]};--panel:{PALETTE["panel"]};--raised:{PALETTE["raised"]};--fg:{PALETTE["fg"]};--muted:{PALETTE["muted"]};--dim:{PALETTE["dim"]};--line:{PALETTE["line"]};--accent:{PALETTE["accent"]}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}}
header{{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}}
header h1{{font-size:16px;margin:0}} header .m{{color:var(--muted);font-size:12px}}
main{{display:grid;grid-template-columns:1fr 24rem;min-height:calc(100vh - 44px)}}
#map{{background:var(--bg);overflow:auto}} svg{{display:block}}
aside{{background:var(--panel);border-left:1px solid var(--line);padding:12px 14px;overflow:auto;font-size:13px}}
.lbl{{fill:var(--fg);font:11px ui-monospace,monospace;paint-order:stroke;stroke:var(--bg);stroke-width:3px}}
.t2{{fill:var(--muted);font:9px ui-monospace,monospace;paint-order:stroke;stroke:var(--bg);stroke-width:3px}}
.node{{cursor:pointer}} .node.sel circle,.node.sel rect{{stroke:#fff;stroke-width:2.4}}
.edge.faded,.node.faded{{opacity:0.22}} .edge.hi{{stroke-width:3.2!important;opacity:1}}
ul{{list-style:none;margin:0;padding:0}} .legend li{{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:12px}}
.sw{{display:inline-block;width:12px;height:12px;border-radius:50%}}
h2{{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}}
table{{border-collapse:collapse;width:100%}} td{{padding:2px 4px;vertical-align:top;border-bottom:1px solid var(--line)}} td:first-child{{color:var(--muted);white-space:nowrap}}
.ev{{background:var(--raised);border-radius:6px;padding:6px 8px;margin:6px 0;font-size:12px}} .ev b{{color:var(--fg)}}
.sys{{margin:4px 0;padding:4px 6px;border-left:3px solid var(--accent);background:var(--raised);border-radius:4px;cursor:pointer}}
code{{font:12px ui-monospace,monospace;color:var(--fg)}}
@media (max-width:900px){{main{{grid-template-columns:1fr}} aside{{border-left:none;border-top:1px solid var(--line)}}}}
</style></head>
<body>
<header><h1>{html.escape(title)}</h1><span class="m">{len([n for n in data["nodes"] if n.get("kind") == "carrier"])} carriers · {len(systems)} systems · {len(data["edges"])} links shown (≥ {html.escape(str(data.get("min_label")))}) · generated {gen}</span></header>
<main>
<div id="map"><svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
<g id="edges">{"".join(edges_svg)}</g>
<g id="nodes">{"".join(nodes_svg)}</g>
</svg></div>
<aside>
<div id="detail"><h2>Legend</h2><ul class="legend">{legend}<li><svg width="60" height="16"><circle cx="30" cy="8" r="6" fill="{PALETTE["accent"]}"/></svg>decoded carrier</li>
<li><svg width="60" height="16"><rect x="24" y="2" width="12" height="12" transform="rotate(45 30 8)" fill="none" stroke="{PALETTE["muted"]}" stroke-dasharray="3 2"/></svg>site known only from an announcement</li>
<li><svg width="60" height="16"><text x="30" y="12" text-anchor="middle" fill="{PALETTE["muted"]}" font-size="10">T2</text></svg>Tier II only: never certain (capped)</li></ul>
<h2>Variants</h2><ul class="legend">{variants}</ul>
<h2>Systems</h2><div id="systems"></div>
<p class="m" style="color:var(--muted);font-size:12px">Click a node for its details and the evidence behind each link.</p></div>
</aside></main>
<script>
const DATA = {payload};
const byId = Object.fromEntries(DATA.nodes.map(n => [n.id, n]));
const edgesOf = id => DATA.edges.filter(e => e.source === id || e.target === id);
const esc = v => String(v).replace(/[&<>"']/g, c => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}})[c]);
function fmtEv(e) {{ return (e.evidence || []).map(x => '<code>' + esc(x) + '</code>').join(' '); }}
function show(id) {{
  const n = byId[id]; if (!n) return;
  document.querySelectorAll('.node').forEach(el => {{ el.classList.toggle('sel', el.dataset.id === id); el.classList.toggle('faded', el.dataset.id !== id && !edgesOf(id).some(e => e.source === el.dataset.id || e.target === el.dataset.id)); }});
  document.querySelectorAll('.edge').forEach(el => {{ const inc = el.dataset.a === id || el.dataset.b === id; el.classList.toggle('hi', inc); el.classList.toggle('faded', !inc); }});
  let h = '<h2>' + (n.kind === 'site' ? 'Announced site' : 'Carrier') + '</h2><table>';
  const row = (k, v) => '<tr><td>' + k + '</td><td>' + (v === null || v === undefined ? '-' : esc(v)) + '</td></tr>';
  if (n.kind === 'carrier') {{
    h += row('frequency', n.label + ' MHz') + row('variant', n.variant + ' (' + (n.variant_confidence || 0).toFixed(2) + ')') + row('colour code', n.cc) + row('band', n.band)
       + row('site', n.site_label) + row('LCN', n.lcn) + row('system', n.system_label) + row('first seen', n.first_seen_h) + row('last seen', n.last_seen_h)
       + row('observed', (n.activity ? n.activity.seconds + ' s, ' + n.activity.bursts + ' bursts, ' + n.activity.voice + ' voice' : '-'));
  }} else {{ h += row('site', n.site_label || n.label) + row('system', n.system_label) + row('status', 'announced by a neighbour; no carrier decoded yet'); }}
  h += '</table><h2>Links</h2>';
  const es = edgesOf(id).sort((a, b) => (b.score || 0) - (a.score || 0));
  if (!es.length) h += '<p style="color:var(--muted)">none at this threshold</p>';
  for (const e of es) {{
    const other = byId[e.source === id ? e.target : e.source];
    h += '<div class="ev"><b>' + (other ? esc(other.label) : '?') + '</b> · ' + e.kind + ' · <b>' + e.label + '</b> ' + (e.score || 0).toFixed(2) + (e.tier2_only ? ' · Tier II only' : '') + '<br>' + fmtEv(e)
       + (e.also ? '<br><span style="color:var(--muted)">also: ' + e.also.map(a => a.kind + ' ' + a.label + ' ' + a.score.toFixed(2)).join(', ') + '</span>' : '') + '</div>';
  }}
  document.getElementById('detail').innerHTML = h + '<p><a href="#" onclick="reset();return false" style="color:var(--accent)">back to legend</a></p>';
}}
const legendHtml = document.getElementById('detail').innerHTML;
function reset() {{ document.getElementById('detail').innerHTML = legendHtml; document.querySelectorAll('.faded,.hi,.sel').forEach(el => el.classList.remove('faded', 'hi', 'sel')); renderSystems(); }}
function renderSystems() {{
  const box = document.getElementById('systems'); if (!box) return;
  box.innerHTML = DATA.systems.map(s => {{
    const members = DATA.nodes.filter(n => n.system_id === s.id && n.kind === 'carrier');
    return '<div class="sys" onclick="show(\\'' + (members[0] ? members[0].id : '') + '\\')"><b>' + esc(s.name ? s.name + ' (' + s.label + ')' : s.label) + '</b> <span style="color:var(--muted)">' + s.variant + ' · ' + members.length + ' carrier' + (members.length === 1 ? '' : 's') + '</span></div>';
  }}).join('');
}}
document.querySelectorAll('.node').forEach(el => el.addEventListener('click', () => show(el.dataset.id)));
renderSystems();
</script></body></html>
"""


def write(g: nx.Graph, path: str, title: str = "DMR network map", generated_at: int | None = None) -> None:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(g, title, generated_at), encoding="utf-8")
