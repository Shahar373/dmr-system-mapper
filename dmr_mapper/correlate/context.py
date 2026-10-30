"""A read-only snapshot of what the store knows, shared by all evidence collectors."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..db import Store


@dataclass(slots=True)
class CarrierInfo:
    id: int
    freq_hz: int
    variant: str
    cc: int | None
    first_seen: int
    last_seen: int
    # explicit identity seen on this carrier
    cap_sites: dict[int, int] = field(default_factory=dict)       # site number → count (Cap+)
    con_sites: dict[tuple[int, int], int] = field(default_factory=dict)   # (net, site) → count (Con+)
    t3_sites: dict[tuple[str | None, int | None, int], int] = field(default_factory=dict)  # (sys, net, site) on air (t3_on_air) → count
    t3_models: dict[str, int] = field(default_factory=dict)        # Tier III network model (Tiny/Small/Large/Huge) → count
    t3_control: int = 0                                            # Tier III announcements only a control channel makes
    xpt_sites: set[int] = field(default_factory=set)
    neighbors: dict[str, set[Any]] = field(default_factory=lambda: defaultdict(set))   # variant → announced site keys
    announced_freqs: dict[int, dict[str, Any]] = field(default_factory=dict)  # freq_hz → {lpcn, sys, net, site}
    neighbor_channels: dict[tuple[int | None, int], int] = field(default_factory=dict)  # (net, site) → its control channel's LPCN/LCN
    last_seen_structural: int = 0

    @property
    def site_keys(self) -> set[tuple[str, Any]]:
        keys: set[tuple[str, Any]] = set()
        keys.update(("capplus", s) for s in self.cap_sites)
        keys.update(("conplus", k) for k in self.con_sites)
        keys.update(("tier3", k) for k in self.t3_sites)
        keys.update(("xpt", s) for s in self.xpt_sites)
        return keys


def t3_on_air(net_id, site_id, sub_site) -> tuple[int | None, int]:
    """The Net and Site a Tier III network sends on air, as SDRTrunk shows them; everything the
    correlator derives and shows uses these. Stored payloads keep what DSD-FME printed: with
    ``-D 0`` (and by default) ``Net ID: net+1; Site ID: 1.site+1`` (raw net 1 site 1 prints
    ``Net ID: 2; Site ID: 1.2``); a print without a sub-site is already raw (rawpdu.printed_ids)."""
    net = None if net_id is None else int(net_id)
    if sub_site is None:
        return net, int(site_id)
    return (None if net is None else net - 1), (int(site_id) - 1) * 1024 + int(sub_site) - 1


def format_site(number) -> str:
    return "?" if number is None else str(int(number))


class Context:
    def __init__(self, store: Store, now: int, window_days: int):
        self.store = store
        self.now = now
        self.since = now - window_days * 86400
        self.carriers: dict[int, CarrierInfo] = {}
        self.by_freq: dict[int, CarrierInfo] = {}
        self._load_carriers()
        self._load_events()

    # -- loading ---------------------------------------------------------------------------------
    def _load_carriers(self) -> None:
        for r in self.store.carriers():
            c = CarrierInfo(int(r["id"]), int(r["freq_hz"]), str(r["variant"]), r["dominant_cc"], int(r["first_seen"]), int(r["last_seen"]))
            self.carriers[c.id] = c
            self.by_freq[c.freq_hz] = c

    def _load_events(self) -> None:
        rows = self.store.rows(
            "SELECT carrier_id, kind, payload, count, last_ts FROM csbk_events WHERE last_ts >= ? AND kind IN ("
            "'capplus_site','capplus_adjacent','conplus_sys','conplus_adjacent','tier3_sys_parms','tier3_bcast',"
            "'xpt_site_status','xpt_adjacent','grant')", (self.since,))
        for r in rows:
            c = self.carriers.get(int(r["carrier_id"]))
            if c is None:
                continue
            p = json.loads(r["payload"])
            n = int(r["count"])
            c.last_seen_structural = max(c.last_seen_structural, int(r["last_ts"]))
            kind = r["kind"]
            if kind == "capplus_site":
                c.cap_sites[int(p["site"])] = c.cap_sites.get(int(p["site"]), 0) + n
            elif kind == "capplus_adjacent":
                for s in p.get("sites", []):
                    c.neighbors["capplus"].add(int(s["site"]))
            elif kind == "conplus_sys":
                key = (int(p["net_id"]), int(p["site_id"]))
                c.con_sites[key] = c.con_sites.get(key, 0) + n
            elif kind == "conplus_adjacent":
                for s in p.get("sites", []):
                    c.neighbors["conplus"].add(int(s))
            elif kind == "tier3_sys_parms":
                key = (p.get("sys_code"), *t3_on_air(p.get("net_id"), p.get("site_id"), p.get("sub_site")))
                c.t3_sites[key] = c.t3_sites.get(key, 0) + n
                if p.get("model"):
                    c.t3_models[str(p["model"])] = c.t3_models.get(str(p["model"]), 0) + n
                if p.get("source") in ("c_aloha_sys_parms", "slc_c_sys_parms"):
                    c.t3_control += n
            elif kind == "tier3_bcast":
                if p.get("a_type") == "adjacent_site" and p.get("adj_site_id") is not None:
                    net, number = t3_on_air(p.get("adj_net_id"), p["adj_site_id"], p.get("adj_sub_site"))
                    c.neighbors["tier3"].add((p.get("sys_code"), net, number))
                    if p.get("lpcn"):
                        c.neighbor_channels[(net, number)] = int(p["lpcn"])
                if p.get("a_type") == "chan_freq" and p.get("rx_freq_hz"):
                    c.announced_freqs[int(p["rx_freq_hz"])] = {"lpcn": p.get("apcn") or p.get("lpcn"), "sys": p.get("sys_code")}
            elif kind == "xpt_adjacent":
                for s in p.get("sites", []):
                    c.neighbors["xpt"].add(int(s["site"]))
            elif kind == "grant" and p.get("freq_hz"):
                c.announced_freqs[int(p["freq_hz"])] = {"lpcn": p.get("lpcn"), "lcn": p.get("lcn"), "sys": None}

        # A carrier that is not Capacity Plus can still print an occasional "Capacity Plus Site"
        # line: on the Pi, Cap Max (Tier III) short LCs were decoded as "Site: 0 - Rest LSN: 0".
        # Cap+ identity counts only on carriers classified as Cap+.
        for c in self.carriers.values():
            if c.variant != "capplus":
                c.cap_sites.clear()
                c.neighbors.pop("capplus", None)

    # -- helpers ---------------------------------------------------------------------------------
    def t3_model(self, c: CarrierInfo) -> str | None:
        return max(c.t3_models, key=c.t3_models.get) if c.t3_models else None

    def t3_identity(self, c: CarrierInfo) -> tuple[str | None, int | None, int] | None:
        """(sys_code, net_id, site_id) of a Tier III carrier. Traffic channels only carry Net/Site
        in their short LC, so the system code is taken from any record on the carrier that has it."""
        if not c.t3_sites:
            return None
        by_site: dict[tuple[int | None, int], int] = {}
        sys_for: dict[tuple[int | None, int], dict[str, int]] = {}
        for (sys_code, net, site), n in c.t3_sites.items():
            key = (net, site)
            by_site[key] = by_site.get(key, 0) + n
            if sys_code:
                sys_for.setdefault(key, {})[sys_code] = sys_for.setdefault(key, {}).get(sys_code, 0) + n
        net, site = max(by_site, key=by_site.get)
        codes = sys_for.get((net, site))
        return (max(codes, key=codes.get) if codes else None, net, site)

    def cache(self, key: str, make):
        """A value computed once per run (``make(self)``), shared by the collectors."""
        store = self.__dict__.setdefault("_cache", {})
        if key not in store:
            store[key] = make(self)
        return store[key]

    def announced_channel(self, net: int | None, site: int) -> int | None:
        """The LPCN (on Capacity Max: the LCN) neighbours announce for a site's control channel."""
        for c in self.carriers.values():
            for (n, s), lcn in c.neighbor_channels.items():
                if s == site and (n is None or net is None or n == net):
                    return lcn
        return None

    def primary_cap_site(self, c: CarrierInfo) -> int | None:
        if not c.cap_sites:
            return None
        return max(c.cap_sites, key=c.cap_sites.get)

    def primary_con_site(self, c: CarrierInfo) -> tuple[int, int] | None:
        if not c.con_sites:
            return None
        return max(c.con_sites, key=c.con_sites.get)

    def carriers_for_site(self, variant: str, key: Any) -> list[CarrierInfo]:
        out = []
        for c in self.carriers.values():
            if variant == "capplus" and self.primary_cap_site(c) == key:
                out.append(c)
            elif variant == "conplus" and self.primary_con_site(c) == key:
                out.append(c)
            elif variant == "tier3":
                ident = self.t3_identity(c)
                if ident is not None and ident[2] == key[2] and (ident[1] is None or key[1] is None or ident[1] == key[1]):
                    out.append(c)
            elif variant == "xpt" and key in c.xpt_sites:
                out.append(c)
        return out
