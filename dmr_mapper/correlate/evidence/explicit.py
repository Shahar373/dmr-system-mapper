"""Explicit evidence: what the air interface says outright (site numbers, system codes,
neighbour lists, channel/frequency announcements)."""

from __future__ import annotations

from itertools import combinations

from ..context import CarrierInfo, Context, format_site
from ..model import Evidence
from ..scoring import ScoringConfig


def _pair_ev(a: CarrierInfo, b: CarrierInfo, kind: str, etype: str, raw: float, details: dict) -> Evidence:
    first = max(min(a.first_seen, b.first_seen), 0)
    newest = min(a.last_seen_structural or a.last_seen, b.last_seen_structural or b.last_seen)
    return Evidence(a.freq_hz, b.freq_hz, kind, etype, raw, newest, first, 1, details).ordered()


def same_site(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    out: list[Evidence] = []
    cs = list(ctx.carriers.values())
    for a, b in combinations(cs, 2):
        # Capacity Plus: site numbers are only unique within one network, so demand the same colour
        # code and a plausible frequency span as well.
        sa, sb = ctx.primary_cap_site(a), ctx.primary_cap_site(b)
        if sa is not None and sb is not None:
            if sa == sb and a.cc == b.cc and abs(a.freq_hz - b.freq_hz) <= cfg.same_site_max_span_hz:
                out.append(_pair_ev(a, b, "same_site", "explicit_same_site", 0.8,
                                    {"variant": "capplus", "site": sa, "cc": a.cc, "note": "same Cap+ site number and colour code"}))
            continue
        ca, cb = ctx.primary_con_site(a), ctx.primary_con_site(b)
        if ca is not None and cb is not None:
            if ca == cb:
                out.append(_pair_ev(a, b, "same_site", "explicit_same_site", 1.0,
                                    {"variant": "conplus", "net_id": ca[0], "site_id": ca[1]}))
            elif ca[0] == cb[0]:
                out.append(_pair_ev(a, b, "same_site", "explicit_same_site_contradiction", 0.0,
                                    {"variant": "conplus", "net_id": ca[0], "sites": [ca[1], cb[1]], "note": "same network, different sites"}))
            continue
        ta, tb = ctx.t3_identity(a), ctx.t3_identity(b)
        if ta is not None and tb is not None:
            same_sys = _t3_same_system(ctx, a, b, ta, tb)
            if same_sys and ta[2] == tb[2] and ta[0] and tb[0] and ta[0] != tb[0]:
                # same Net ID and site number but a different SYS code: the code is derived from net
                # and site, so these are two networks that happen to reuse the numbers
                continue
            if same_sys and ta[2] == tb[2]:
                # the SYS code carries the site too, so equal codes corroborate; a traffic channel
                # whose short LC has no SYS code only knows Net/Site
                raw = 1.0 if (ta[0] and tb[0] and ta[0] == tb[0]) else 0.85
                out.append(_pair_ev(a, b, "same_site", "explicit_same_site", raw,
                                    {"variant": "tier3", "sys_code": ta[0] or tb[0], "net_id": ta[1], "site": format_site(ta[2])}))
            elif same_sys:
                out.append(_pair_ev(a, b, "same_site", "explicit_same_site_contradiction", 0.0,
                                    {"variant": "tier3", "net_id": ta[1], "sites": [format_site(ta[2]), format_site(tb[2])],
                                     "note": "same network, different sites"}))
            continue
        if a.xpt_sites and b.xpt_sites and a.xpt_sites & b.xpt_sites:
            site = sorted(a.xpt_sites & b.xpt_sites)[0]
            out.append(_pair_ev(a, b, "same_site", "explicit_same_site", 0.9, {"variant": "xpt", "site": site}))
    return out


def same_system(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    """Different sites of one Tier III system (same SYS code) or Connect Plus network (same Net ID)."""
    out: list[Evidence] = []
    for a, b in combinations(list(ctx.carriers.values()), 2):
        ta, tb = ctx.t3_identity(a), ctx.t3_identity(b)
        if ta is not None and tb is not None and _t3_same_system(ctx, a, b, ta, tb) and ta[2] != tb[2]:
            # Net IDs are small numbers that unrelated operators may reuse: on their own they make a
            # link "probable"; a neighbour announcement between the sites makes it certain.
            linked = _announces(a, "tier3", tb) or _announces(b, "tier3", ta)
            out.append(_pair_ev(a, b, "same_system", "explicit_same_system", 1.0 if linked else 0.8,
                                {"variant": "tier3", "net_id": ta[1], "model": ctx.t3_model(a) or ctx.t3_model(b),
                                 "sites": sorted({format_site(ta[2]), format_site(tb[2])}),
                                 "sys_codes": sorted({x for x in (ta[0], tb[0]) if x}),
                                 "note": "same network ID" + (", sites announce each other" if linked else "")}))
            continue
        ca, cb = ctx.primary_con_site(a), ctx.primary_con_site(b)
        if ca is not None and cb is not None and ca[0] == cb[0] and ca[1] != cb[1]:
            out.append(_pair_ev(a, b, "same_system", "explicit_same_system", 1.0,
                                {"variant": "conplus", "net_id": ca[0], "sites": sorted({ca[1], cb[1]})}))
    return out


def neighbor(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    """Site A announces site B as a neighbour. Carriers of A and carriers of B get a neighbor_site
    link; raw 1.0 when B also announces A, 0.65 one-way. Sites that are announced but have no known
    carrier are handled by derive.py (announced-only sites)."""
    out: list[Evidence] = []
    cs = list(ctx.carriers.values())
    for a, b in combinations(cs, 2):
        for variant in ("capplus", "conplus", "tier3", "xpt"):
            ka = _site_key(ctx, a, variant)
            kb = _site_key(ctx, b, variant)
            if ka is None or kb is None or ka == kb:
                continue
            if variant == "capplus" and a.cc != b.cc and (a.cc is None or b.cc is None):
                pass
            a_says_b = _announces(a, variant, kb)
            b_says_a = _announces(b, variant, ka)
            if not (a_says_b or b_says_a):
                continue
            if variant == "capplus" and abs(a.freq_hz - b.freq_hz) > cfg.same_site_max_span_hz * 4:
                continue   # Cap+ site numbers repeat across unrelated networks; keep neighbours regional
            raw = 1.0 if (a_says_b and b_says_a) else 0.65
            out.append(_pair_ev(a, b, "neighbor_site", "explicit_neighbor", raw, {
                "variant": variant, "a_site": _key_str(ka), "b_site": _key_str(kb),
                "bidirectional": a_says_b and b_says_a, "a_announces_b": a_says_b, "b_announces_a": b_says_a,
            }))
    return out


def lcn_freq(ctx: Context, cfg: ScoringConfig) -> list[Evidence]:
    """A Tier III Chan_Freq announcement (or a grant with a channel map) on carrier A names the
    frequency of carrier B: B is a channel of A's site."""
    out: list[Evidence] = []
    for a in ctx.carriers.values():
        for freq, info in a.announced_freqs.items():
            b = ctx.by_freq.get(freq)
            if b is None or b.id == a.id:
                continue
            lcn = info.get("lcn") or info.get("lpcn")
            ev = _pair_ev(a, b, "same_site", "explicit_lcn_freq", 1.0,
                          {"announced_by": a.freq_hz, "freq_hz": freq, "lpcn": info.get("lpcn"), "lcn": info.get("lcn"), "sys_code": info.get("sys")})
            if lcn:
                ev.lcn_hints[b.freq_hz] = int(lcn)
            out.append(ev)
    return out


def _t3_same_system(ctx: Context, a: CarrierInfo, b: CarrierInfo, ta, tb) -> bool:
    """Same Tier III network: same Net ID and the same network model. The SYS code is NOT a
    network identity: it also encodes the site (on the Pi, net 2 site 1.2 sends 2101 and
    site 1.3 sends 2102)."""
    if ta[1] is None or ta[1] != tb[1]:
        return False
    ma, mb = ctx.t3_model(a), ctx.t3_model(b)
    return ma is None or mb is None or ma == mb


def _site_key(ctx: Context, c: CarrierInfo, variant: str):
    if variant == "capplus":
        return ctx.primary_cap_site(c)
    if variant == "conplus":
        k = ctx.primary_con_site(c)
        return k[1] if k else None
    if variant == "tier3":
        k = ctx.t3_identity(c)
        return k if k else None
    if variant == "xpt":
        return sorted(c.xpt_sites)[0] if c.xpt_sites else None
    return None


def _announces(c: CarrierInfo, variant: str, key) -> bool:
    announced = c.neighbors.get(variant, set())
    if variant == "tier3":
        return any(k[2] == key[2] and (k[1] is None or key[1] is None or k[1] == key[1]) for k in announced)
    return key in announced


def _key_str(k) -> str:
    if isinstance(k, tuple):
        return "/".join("-" if x is None else str(x) for x in k)
    return str(k)
