"""Campaign: survey, decode and watch in cycles at one stop, as a chain of ordinary sessions.

`mapper campaign --minutes 45` (console key m) gives one budget for one stop. It is split into
cycles of about ``campaign.cycle_minutes``; each cycle is a survey of a few sweeps, a decode of
what the recent sweeps near the receiver heard, and a watch of one slice, by default 15 / 50 / 35 %
of the cycle. Every step is an ordinary session (its own row in ``sessions``, its own summary,
correlated after a decode or a watch); the daemon posts the next step when one ends, as it does for
``survey --then-decode``. The campaign's state travels in the params of each step
(``params.campaign``), so nothing new is stored. A stop, a failure or undervoltage ends the chain;
a step the daemon refuses (receiver taken by another program) ends it too.

Which slice to watch: the Tier III network with the most carriers in one slice, then the slice
holding the most carriers heard in only some of the recent sweeps (intermittent: a conventional
repeater, a traffic channel), then the busiest known slice; one not watched yet in this campaign
first. With nothing to watch, the decode gets the cycle's watch time too.
"""

from __future__ import annotations

import math
import time
from typing import Any

from ..db import Store
from .scheduler import DC_GUARD_HZ
from .hits import plan_radius_km, plan_window_s, recent_hits
from .survey import MAX_SWEEPS, sweep_seconds

DEFAULT_MIX = {"survey": 0.15, "decode": 0.50, "watch": 0.35}


def campaign_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    c = cfg.get("campaign", {}) or {}
    mix = {**DEFAULT_MIX, **(c.get("mix") or {})}
    total = sum(float(v) for v in mix.values()) or 1.0
    return {"cycle_minutes": float(c.get("cycle_minutes", 45)), "mix": {k: float(v) / total for k, v in mix.items()},
            "min_decode_s": int(c.get("min_decode_s", 60)), "min_watch_s": int(c.get("min_watch_s", 120))}


def _bands(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return list((cfg.get("survey", {}) or {}).get("bands", []) or [])


def _sample_rate(cfg: dict[str, Any]) -> float:
    return float((cfg.get("sdr", {}) or {}).get("sample_rate_hz", 2_000_000))


def _usable(cfg: dict[str, Any]) -> float:
    return _sample_rate(cfg) * float((cfg.get("sdr", {}) or {}).get("usable_fraction", 0.8))


def one_sweep_s(cfg: dict[str, Any], dwell_s: float | None = None) -> float:
    d = float(dwell_s if dwell_s is not None else (cfg.get("survey", {}) or {}).get("dwell_s", 3))
    usable_fraction = float((cfg.get("sdr", {}) or {}).get("usable_fraction", 0.8))
    return sweep_seconds(_bands(cfg), _sample_rate(cfg), usable_fraction, d)


def new_campaign(budget_s: int, cfg: dict[str, Any], now: int | None = None, dwell_s: float | None = None,
                 watch: bool = True) -> dict[str, Any]:
    now = int(now or time.time())
    cc = campaign_cfg(cfg)
    cycles = max(1, round(budget_s / (cc["cycle_minutes"] * 60)))
    return {"id": now, "started": now, "deadline": now + int(budget_s), "cycles": cycles, "cycle_s": int(budget_s) / cycles,
            "cycle": 1, "step": "survey", "dwell_s": dwell_s, "watch": bool(watch), "watched": []}


def cycle_end(camp: dict[str, Any]) -> float:
    return min(float(camp["deadline"]), float(camp["started"]) + int(camp["cycle"]) * float(camp["cycle_s"]))


def describe(camp: dict[str, Any], now: int | None = None) -> str:
    now = int(now or time.time())
    left = max(0, int(camp["deadline"]) - now)
    return f"campaign cycle {camp['cycle']}/{camp['cycles']}, {camp['step']}, {left // 60} min left"


def survey_step(camp: dict[str, Any], cfg: dict[str, Any], now: int | None = None) -> tuple[str, dict[str, Any]]:
    """The survey that opens a cycle: as many sweeps as the cycle's survey share holds (at least one)."""
    now = int(now or time.time())
    cc = campaign_cfg(cfg)
    one = one_sweep_s(cfg, camp.get("dwell_s"))
    share = cc["mix"]["survey"] * float(camp["cycle_s"])
    sweeps = max(1, min(MAX_SWEEPS, int(share // one))) if one > 0 else 1
    budget = int(min(max(60, int(camp["deadline"]) - now), math.ceil(sweeps * one * 1.25 + 20)))
    params: dict[str, Any] = {"budget_s": budget, "sweeps": sweeps, "campaign": {**camp, "step": "survey"}}
    if camp.get("dwell_s") is not None:
        params["dwell_s"] = float(camp["dwell_s"])
    return "start_survey", params


def watch_candidates(store: Store, cfg: dict[str, Any], near: tuple[float, float] | None = None,
                     now: int | None = None) -> list[dict[str, Any]]:
    """Slices worth a watch, best first: {center_hz, reason}."""
    from .watch import _best_window, plan_watch

    usable = _usable(cfg)
    out: list[dict[str, Any]] = []
    default = None
    try:
        default = plan_watch(store, cfg, _sample_rate(cfg), 6, now=now)
    except ValueError:
        pass
    if default is not None and default.reason.startswith("Tier III"):
        out.append({"center_hz": int(default.center_hz), "reason": f"{default.reason} ({default.system})"})
    hits = recent_hits(store, plan_window_s(cfg), max_age_s=7 * 86400, near=near, radius_km=plan_radius_km(cfg))
    intermittent = [(int(h["freq_hz"]), 1.0) for h in hits if int(h.get("sweeps") or 1) > 1 and int(h.get("heard_in") or 1) < int(h["sweeps"])]
    if intermittent:
        weight, inside = _best_window(intermittent, usable - 2 * DC_GUARD_HZ)
        if inside:
            out.append({"center_hz": (min(inside) + max(inside)) // 2,
                        "reason": f"{len(inside)} carrier(s) heard in only some of the recent sweeps"})
    if default is not None and not default.reason.startswith("Tier III"):
        out.append({"center_hz": int(default.center_hz), "reason": default.reason})
    return out


def choose_watch(store: Store, cfg: dict[str, Any], camp: dict[str, Any], near: tuple[float, float] | None = None,
                 now: int | None = None) -> dict[str, Any] | None:
    cands = watch_candidates(store, cfg, near, now)
    if not cands:
        return None
    half = _usable(cfg) / 2
    fresh = [c for c in cands if not any(abs(c["center_hz"] - int(w)) < half for w in camp.get("watched") or [])]
    return (fresh or cands)[0]


def next_step(store: Store, cfg: dict[str, Any], camp: dict[str, Any], finished: str, now: int | None = None,
              near: tuple[float, float] | None = None) -> tuple[str, dict[str, Any]] | None:
    """What follows the step ``finished`` (survey, decode or watch): (command, params), or None when
    the campaign is over."""
    now = int(now or time.time())
    cc = campaign_cfg(cfg)
    end = cycle_end(camp)
    camp = dict(camp)
    if finished == "survey":
        target = choose_watch(store, cfg, camp, near, now) if camp.get("watch", True) else None
        watch_s = cc["mix"]["watch"] * float(camp["cycle_s"]) if target else 0.0
        if watch_s and watch_s < cc["min_watch_s"]:
            target, watch_s = None, 0.0
        dec = int(end - now - watch_s)
        if dec >= cc["min_decode_s"]:
            return "start_decode", {"budget_s": dec, "campaign": {**camp, "step": "decode", "watch_target": target}}
        camp["watch_target"] = target
        finished = "decode"
    if finished == "decode":
        target = camp.get("watch_target")
        left = int(end - now)
        if target and left >= cc["min_watch_s"]:
            watched = list(camp.get("watched") or []) + [int(target["center_hz"])]
            return "start_watch", {"budget_s": left, "center_hz": int(target["center_hz"]),
                                   "campaign": {**camp, "step": "watch", "watched": watched}}
        finished = "watch"
    # the cycle is over: another one while a sweep and a decode still fit before the deadline
    left = int(camp["deadline"]) - now
    if left < one_sweep_s(cfg, camp.get("dwell_s")) + cc["min_decode_s"]:
        return None
    elapsed_cycles = int((now - int(camp["started"])) // float(camp["cycle_s"])) + 1
    nxt = {k: v for k, v in camp.items() if k != "watch_target"}
    nxt["cycle"] = min(int(camp["cycles"]), max(int(camp["cycle"]) + 1, elapsed_cycles))
    if nxt["cycle"] == int(camp["cycle"]):
        # past the last planned cycle with time left (a step ended early): stretch the last one
        nxt["cycles"] = nxt["cycle"] = int(camp["cycles"]) + 1
        nxt["cycle_s"] = (int(camp["deadline"]) - int(camp["started"])) / nxt["cycles"]
    return survey_step(nxt, cfg, now)
