"""Derive sites and systems from scored links (DESIGN.md §5, last paragraph).

* a **site** is a connected component of ``same_site`` links with score ≥ ``possible``; a carrier
  with no such link is a site of its own;
* a **system** is a connected component of sites joined by ``same_system`` or ``neighbor_site``
  links (again ≥ possible);
* sites that are only *announced* (neighbour lists naming a site number no carrier reports) exist
  with ``directly_observed = 0`` and no carriers, so the graph can show them as hollow nodes.

Tables ``systems``/``sites``/``carrier_sites`` are rebuilt on every run, except rows with
``created_by = 'manual'`` which are preserved by label.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import networkx as nx

from ..db import Store
from .context import CarrierInfo, Context
from .scoring import ScoringConfig


def _threshold(cfg: ScoringConfig) -> float:
    return float(cfg.labels["possible"])


def derive_sites_and_systems(store: Store, ctx: Context, cfg: ScoringConfig, now: int, lcn_hints: dict[int, int]) -> tuple[int, int]:
    thr = _threshold(cfg)
    links = store.rows(
        "SELECT l.*, a.freq_hz AS a_freq, b.freq_hz AS b_freq FROM links l JOIN carriers a ON a.id = l.a_carrier_id"
        " JOIN carriers b ON b.id = l.b_carrier_id WHERE l.score >= ?", (thr,))

    g_site = nx.Graph()
    g_site.add_nodes_from(ctx.carriers)
    for l in links:
        if l["kind"] == "same_site":
            g_site.add_edge(int(l["a_carrier_id"]), int(l["b_carrier_id"]), score=float(l["score"]))

    site_of: dict[int, int] = {}           # carrier id → site index
    site_members: list[list[int]] = []
    for comp in nx.connected_components(g_site):
        idx = len(site_members)
        members = sorted(comp, key=lambda cid: ctx.carriers[cid].freq_hz)
        site_members.append(members)
        for cid in members:
            site_of[cid] = idx

    g_sys = nx.Graph()
    g_sys.add_nodes_from(range(len(site_members)))
    for l in links:
        if l["kind"] in ("same_system", "neighbor_site"):
            sa, sb = site_of[int(l["a_carrier_id"])], site_of[int(l["b_carrier_id"])]
            if sa != sb:
                g_sys.add_edge(sa, sb)
    system_of: dict[int, int] = {}
    system_members: list[list[int]] = []
    for comp in nx.connected_components(g_sys):
        idx = len(system_members)
        system_members.append(sorted(comp))
        for s in comp:
            system_of[s] = idx

    # preserve manual labels
    manual_sys = {r["label"]: dict(r) for r in store.rows("SELECT * FROM systems WHERE created_by = 'manual'")}
    manual_sites = {(r["system_id"], r["site_number"]): dict(r) for r in store.rows("SELECT * FROM sites")}

    store.conn.execute("DELETE FROM carrier_sites")
    store.conn.execute("DELETE FROM sites")
    store.conn.execute("DELETE FROM systems")

    site_db_ids: dict[int, int] = {}
    for sys_idx, sites in enumerate(system_members):
        carriers_in_sys = [cid for s in sites for cid in site_members[s]]
        variant, sys_code, net_id = _system_identity(ctx, carriers_in_sys)
        label = _system_label(ctx, carriers_in_sys, variant, sys_code, net_id)
        confidence = _component_confidence(links, carriers_in_sys)
        cur = store.conn.execute(
            "INSERT INTO systems (label, variant, sys_code, net_id, confidence, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'auto', ?, ?)",
            (label, variant, sys_code, net_id, confidence, now, now))
        sys_id = int(cur.lastrowid)
        announced: set[Any] = set()
        for s in sites:
            members = site_members[s]
            site_number, cc = _site_identity(ctx, members)
            first = min(ctx.carriers[c].first_seen for c in members)
            last = max(ctx.carriers[c].last_seen for c in members)
            site_label = _site_label(ctx, members, variant, site_number)
            cur = store.conn.execute(
                "INSERT INTO sites (system_id, site_number, label, cc, directly_observed, first_seen, last_seen) VALUES (?, ?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(system_id, site_number) DO UPDATE SET last_seen = MAX(last_seen, excluded.last_seen), first_seen = MIN(first_seen, excluded.first_seen)",
                (sys_id, site_number, site_label, cc, first, last))
            site_id = int(cur.lastrowid) if cur.lastrowid else int(store.scalar(
                "SELECT id FROM sites WHERE system_id = ? AND site_number IS ?", (sys_id, site_number)))
            site_db_ids[s] = site_id
            comp_conf = _component_confidence(links, members) if len(members) > 1 else 1.0
            for cid in members:
                c = ctx.carriers[cid]
                store.conn.execute("INSERT INTO carrier_sites (carrier_id, site_id, lcn, confidence) VALUES (?, ?, ?, ?)",
                                   (cid, site_id, lcn_hints.get(c.freq_hz), comp_conf))
                for v, keys in c.neighbors.items():
                    for k in keys:
                        announced.add((v, k))
        # announced-only sites of this system
        known_numbers = {store.scalar("SELECT site_number FROM sites WHERE id = ?", (sid,)) for sid in site_db_ids.values()}
        for v, k in sorted(announced, key=str):
            number = k[2] if isinstance(k, tuple) else k
            if number in known_numbers or ctx.carriers_for_site(v, k):
                continue
            store.conn.execute(
                "INSERT OR IGNORE INTO sites (system_id, site_number, label, cc, directly_observed, first_seen, last_seen) VALUES (?, ?, ?, NULL, 0, ?, ?)",
                (sys_id, number, f"site {number}", now, now))
            known_numbers.add(number)
    return int(store.scalar("SELECT COUNT(*) FROM sites")), len(system_members)


def _system_identity(ctx: Context, cids: list[int]) -> tuple[str, str | None, int | None]:
    variants = defaultdict(int)
    sys_code = net_id = None
    for cid in cids:
        c = ctx.carriers[cid]
        variants[c.variant] += 1
        ident = ctx.t3_identity(c)
        if ident is not None:
            sys_code = sys_code or ident[0]
            net_id = net_id if net_id is not None else ident[1]
        con = ctx.primary_con_site(c)
        if con is not None and net_id is None:
            net_id = con[0]
    variant = max(variants, key=variants.get) if variants else "unknown"
    return variant, sys_code, net_id


def _system_label(ctx: Context, cids: list[int], variant: str, sys_code: str | None, net_id: int | None) -> str:
    if sys_code:
        return f"Tier III SYS {sys_code}"
    if variant == "conplus" and net_id is not None:
        return f"Connect Plus net {net_id}"
    freqs = sorted(ctx.carriers[c].freq_hz for c in cids)
    span = f"{freqs[0] / 1e6:.4f}" if len(freqs) == 1 else f"{freqs[0] / 1e6:.4f}-{freqs[-1] / 1e6:.4f}"
    return f"{variant} {span} MHz"


def _site_identity(ctx: Context, cids: list[int]) -> tuple[int | None, int | None]:
    numbers: dict[int, int] = defaultdict(int)
    ccs: dict[int, int] = defaultdict(int)
    for cid in cids:
        c = ctx.carriers[cid]
        if c.cc is not None:
            ccs[c.cc] += 1
        s = ctx.primary_cap_site(c)
        if s is not None:
            numbers[s] += 1
        con = ctx.primary_con_site(c)
        if con is not None:
            numbers[con[1]] += 1
        t3 = ctx.t3_identity(c)
        if t3 is not None:
            numbers[t3[2]] += 1
        for x in c.xpt_sites:
            numbers[x] += 1
    number = max(numbers, key=numbers.get) if numbers else None
    cc = max(ccs, key=ccs.get) if ccs else None
    return number, cc


def _site_label(ctx: Context, cids: list[int], variant: str, site_number: int | None) -> str:
    freqs = sorted(ctx.carriers[c].freq_hz for c in cids)
    base = f"site {site_number}" if site_number is not None else "site"
    if len(freqs) == 1:
        return f"{base} @ {freqs[0] / 1e6:.4f} MHz"
    return f"{base} ({len(freqs)} carriers, {freqs[0] / 1e6:.4f}-{freqs[-1] / 1e6:.4f} MHz)"


def _component_confidence(links, cids: list[int]) -> float:
    members = set(cids)
    scores = [float(l["score"]) for l in links if int(l["a_carrier_id"]) in members and int(l["b_carrier_id"]) in members]
    return round(max(scores), 4) if scores else 1.0


def carrier_info(ctx: Context, cid: int) -> CarrierInfo:
    return ctx.carriers[cid]
