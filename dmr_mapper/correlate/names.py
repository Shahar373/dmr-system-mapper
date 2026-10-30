"""Names the operator gives to systems and sites.

``systems`` and ``sites`` are rebuilt on every correlator run (their ids change), so a name is not
stored on the row. It is stored in ``names`` with what will find the system or site again:

* the **anchor**, the on-air identity, when the variant has one: ``tier3:Large:2`` for a Tier III
  network (model + Net ID), ``tier3:Large:2:1.3`` for one of its sites, ``conplus:3`` and
  ``conplus:3:12`` for Connect Plus;
* the **carriers** it had when it was named (frequencies), for everything else (Capacity Plus,
  XPT and Tier II have no network identity on air) and to choose between two systems that happen
  to share an anchor.

After every correlator run (and right after naming) each name goes to the one system or site
with the same anchor, else to the one sharing most carriers with it.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..db import Store
from .context import format_site

KINDS = ("system", "site")


def system_anchor(variant: str, net_id: int | None, model: str | None) -> str | None:
    if variant == "tier3" and net_id is not None:
        return f"tier3:{model or '?'}:{net_id}"
    if variant == "conplus" and net_id is not None:
        return f"conplus:{net_id}"
    return None


def site_anchor(system: str | None, variant: str, site_number: int | None) -> str | None:
    if system is None or site_number is None:
        return None
    return f"{system}:{format_site(site_number) if variant == 'tier3' else site_number}"


def _members(store: Store, kind: str) -> dict[int, dict[str, Any]]:
    table = "systems" if kind == "system" else "sites"
    out = {int(r["id"]): {"anchor": r["anchor"], "carriers": set()} for r in store.rows(f"SELECT id, anchor FROM {table}")}
    col = "s.system_id" if kind == "system" else "s.id"
    for r in store.rows(f"SELECT {col} AS owner, c.freq_hz FROM carrier_sites cs JOIN sites s ON s.id = cs.site_id"
                        " JOIN carriers c ON c.id = cs.carrier_id"):
        if int(r["owner"]) in out:
            out[int(r["owner"])]["carriers"].add(int(r["freq_hz"]))
    return out


def apply_names(store: Store) -> int:
    """Write ``name`` on the current systems/sites rows. Returns how many names found a home."""
    placed = 0
    for kind in KINDS:
        table = "systems" if kind == "system" else "sites"
        store.conn.execute(f"UPDATE {table} SET name = NULL")
        targets = _members(store, kind)
        taken: set[int] = set()
        names = [dict(r) for r in store.rows("SELECT * FROM names WHERE kind = ? ORDER BY anchor IS NULL, updated DESC", (kind,))]
        for n in names:
            freqs = set(json.loads(n["carriers"] or "[]"))
            best: tuple[tuple[int, int], int] | None = None
            for tid, t in targets.items():
                if tid in taken:
                    continue
                same_anchor = n["anchor"] is not None and t["anchor"] == n["anchor"]
                overlap = len(freqs & t["carriers"])
                if not same_anchor and (n["anchor"] is not None or overlap == 0):
                    continue          # an anchored name only goes where its anchor is
                score = (int(same_anchor), overlap)
                if best is None or score > best[0]:
                    best = (score, tid)
            if best is not None:
                taken.add(best[1])
                store.conn.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (n["name"], best[1]))
                placed += 1
    return placed


def _resolve(store: Store, kind: str, target: str) -> int:
    """A system/site id, or the one a carrier frequency (Hz, or MHz with a dot) belongs to."""
    table = "systems" if kind == "system" else "sites"
    t = str(target).strip()
    if "." in t:
        freq = int(round(float(t) * 1e6))
    elif t.isdigit() and int(t) >= 1_000_000:
        freq = int(t)
    elif t.isdigit():
        if store.scalar(f"SELECT id FROM {table} WHERE id = ?", (int(t),)) is None:
            raise KeyError(f"no {kind} {t} (ids change with every correlate run: `mapper systems` shows the current ones)")
        return int(t)
    else:
        raise ValueError(f"{target!r} is neither a {kind} id nor a frequency")
    col = "s.system_id" if kind == "system" else "s.id"
    row = store.conn.execute(f"SELECT {col} FROM carrier_sites cs JOIN sites s ON s.id = cs.site_id JOIN carriers c ON c.id = cs.carrier_id"
                             " WHERE c.freq_hz = ?", (freq,)).fetchone()
    if row is None:
        raise KeyError(f"no {kind} with a carrier on {freq / 1e6:.4f} MHz (run `mapper correlate` first)")
    return int(row[0])


def set_name(store: Store, kind: str, target: str, name: str | None, now: int | None = None) -> dict[str, Any]:
    """Name (or with ``name=None`` un-name) the system/site ``target`` refers to."""
    if kind not in KINDS:
        raise ValueError(f"kind must be system or site, got {kind!r}")
    now = int(now or time.time())
    tid = _resolve(store, kind, target)
    t = _members(store, kind)[tid]
    anchor, freqs = t["anchor"], sorted(t["carriers"])
    # whatever name this system/site has now, by anchor or by carriers, is replaced
    for n in store.rows("SELECT id, anchor, carriers FROM names WHERE kind = ?", (kind,)):
        if (anchor is not None and n["anchor"] == anchor) or (n["anchor"] is None and set(json.loads(n["carriers"])) & set(freqs)):
            store.conn.execute("DELETE FROM names WHERE id = ?", (n["id"],))
    if name:
        store.conn.execute("INSERT INTO names (kind, anchor, carriers, name, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                           (kind, anchor, json.dumps(freqs), name.strip(), now, now))
    apply_names(store)
    store.commit()
    return {"kind": kind, "id": tid, "name": name.strip() if name else None, "anchor": anchor, "carriers": freqs}


def list_names(store: Store) -> list[dict[str, Any]]:
    out = []
    for n in store.rows("SELECT * FROM names ORDER BY kind, name"):
        d = dict(n)
        d["carriers"] = json.loads(d["carriers"] or "[]")
        table = "systems" if d["kind"] == "system" else "sites"
        d["now_on"] = [int(r["id"]) for r in store.rows(f"SELECT id FROM {table} WHERE name = ?", (d["name"],))]
        out.append(d)
    return out


def display(name: str | None, label: str | None) -> str:
    return f"{name} ({label})" if name else (label or "")
