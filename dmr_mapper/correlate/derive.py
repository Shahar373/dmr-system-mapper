"""Derive sites and systems from scored links (DESIGN.md §5, last paragraph).

* a **site** is a connected component of ``same_site`` links with score ≥ ``possible``; a carrier
  with no such link is a site of its own;
* a **system** is a connected component of sites joined by ``same_system`` or ``neighbor_site``
  links (again ≥ possible);
* sites that are only *announced* (neighbour lists naming a site number no carrier reports) exist
  with ``directly_observed = 0`` and no carriers, so the graph can show them as hollow nodes.

Tables ``systems``/``sites``/``carrier_sites`` are rebuilt on every run. Manual knowledge lives in
``evidence`` rows with ``source = 'manual'`` (see ``engine.propose``) and in ``names`` (see
``names.py``: each system and site carries an ``anchor``, its on-air identity, that names follow),
never in these tables.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import networkx as nx

from ..db import Store
from .context import CarrierInfo, Context, format_site
from .names import apply_names, site_anchor, system_anchor_for
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

    store.conn.execute("DELETE FROM carrier_sites")
    store.conn.execute("DELETE FROM sites")
    store.conn.execute("DELETE FROM systems")

    site_db_ids: dict[int, int] = {}
    for sys_idx, sites in enumerate(system_members):
        carriers_in_sys = [cid for s in sites for cid in site_members[s]]
        variant, sys_code, net_id = _system_identity(ctx, carriers_in_sys)
        label = _system_label(ctx, carriers_in_sys, variant, sys_code, net_id)
        confidence = _component_confidence(links, carriers_in_sys)
        sys_anchor = system_anchor_for(ctx, carriers_in_sys)
        cur = store.conn.execute(
            "INSERT INTO systems (label, variant, sys_code, net_id, confidence, created_by, created_at, updated_at, anchor)"
            " VALUES (?, ?, ?, ?, ?, 'auto', ?, ?, ?)",
            (label, variant, sys_code, net_id, confidence, now, now, sys_anchor))
        sys_id = int(cur.lastrowid)
        announced: set[Any] = set()
        for s in sites:
            members = site_members[s]
            site_number, cc = _site_identity(ctx, members)
            first = min(ctx.carriers[c].first_seen for c in members)
            last = max(ctx.carriers[c].last_seen for c in members)
            site_label = _site_label(ctx, members, variant, site_number)
            # Two same_site components of one system may announce the same site number (a carrier
            # that could not be linked to its siblings): they share one site row. RETURNING gives the
            # row's id on both the insert and the update path; cursor.lastrowid does not (it keeps
            # the id of the previous insert when the conflict branch runs).
            row = store.conn.execute(
                "INSERT INTO sites (system_id, site_number, label, cc, directly_observed, first_seen, last_seen, anchor) VALUES (?, ?, ?, ?, 1, ?, ?, ?)"
                " ON CONFLICT(system_id, site_number) DO UPDATE SET last_seen = MAX(last_seen, excluded.last_seen), first_seen = MIN(first_seen, excluded.first_seen)"
                " RETURNING id",
                (sys_id, site_number, site_label, cc, first, last, site_anchor(sys_anchor, variant, site_number))).fetchone()
            site_id = int(row[0])
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
            label = f"site {format_site(number) if v == 'tier3' else number}"
            if v == "tier3" and (lcn := ctx.announced_channel(k[1], number)) is not None:
                label += f" (control channel on LCN {lcn}, frequency not heard yet)"
            store.conn.execute(
                "INSERT OR IGNORE INTO sites (system_id, site_number, label, cc, directly_observed, first_seen, last_seen, anchor)"
                " VALUES (?, ?, ?, NULL, 0, ?, ?, ?)",
                (sys_id, number, label, now, now, site_anchor(sys_anchor, v, number)))
            known_numbers.add(number)
    apply_names(store)
    return int(store.scalar("SELECT COUNT(*) FROM sites")), len(system_members)


def _system_identity(ctx: Context, cids: list[int]) -> tuple[str, str | None, int | None]:
    variants = defaultdict(int)
    nets: dict[int, int] = defaultdict(int)
    con_nets: dict[int, int] = defaultdict(int)
    codes: set[str] = set()
    for cid in cids:
        c = ctx.carriers[cid]
        variants[c.variant] += 1
        ident = ctx.t3_identity(c)
        if ident is not None:
            codes.update(k[0] for k in c.t3_sites if k[0])
            if ident[1] is not None:
                nets[ident[1]] += 1
        con = ctx.primary_con_site(c)
        if con is not None:
            con_nets[con[0]] += 1
    variant = max(variants, key=variants.get) if variants else "unknown"
    # carriers linked by behaviour alone (a traffic channel heard keying up) may outnumber the ones
    # that announce the system: what is announced on air names the variant
    if variant in ("unknown", "tier2") and nets:
        variant = "tier3"
    elif variant in ("unknown", "tier2") and con_nets:
        variant = "conplus"
    net_id = max(nets, key=nets.get) if nets else (max(con_nets, key=con_nets.get) if con_nets else None)
    # the SYS code differs per site, so a multi-site network stores all of them
    sys_code = ",".join(sorted(codes)) if codes else None
    return variant, sys_code, net_id


def _system_label(ctx: Context, cids: list[int], variant: str, sys_code: str | None, net_id: int | None) -> str:
    if variant == "tier3" and net_id is not None:
        models = {ctx.t3_model(ctx.carriers[c]) for c in cids} - {None}
        return f"Tier III net {net_id}" + (f" ({'/'.join(sorted(models))})" if models else "")
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
    shown = format_site(site_number) if variant == "tier3" else site_number
    base = f"site {shown}" if site_number is not None else "site"
    if len(freqs) == 1:
        return f"{base} @ {freqs[0] / 1e6:.4f} MHz"
    return f"{base} ({len(freqs)} carriers, {freqs[0] / 1e6:.4f}-{freqs[-1] / 1e6:.4f} MHz)"


def _component_confidence(links, cids: list[int]) -> float:
    members = set(cids)
    scores = [float(l["score"]) for l in links if int(l["a_carrier_id"]) in members and int(l["b_carrier_id"]) in members]
    return round(max(scores), 4) if scores else 1.0


def carrier_info(ctx: Context, cid: int) -> CarrierInfo:
    return ctx.carriers[cid]
