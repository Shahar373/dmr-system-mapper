"""Coverage report: what was swept, where, how often, and what is still missing.

Stops are the places sweeps were made from: survey runs grouped by the mean position of their hits,
a run joining the first stop within ``survey.plan_radius_km`` (3 km) of it; runs without positions
form one stop of their own ("no position"). Per stop: when, how many full and partial sweeps per
band at what dwell, the distinct carriers heard (strongest first, with how many of that stop's
sweeps heard each), the intermittent ones (heard in only some sweeps: watch candidates), the decode
and watch sessions that ended there, and the gaps (configured bands never swept fully there, or
only once). Across stops: sites announced on air but never heard, with the stop where the carriers
of their system were heard strongest. That is a hint where to listen next, not a direction: a
stronger signal from a neighbour site says little about where the unheard site is.

Read by MCP ``get_coverage`` and the coverage section of map.html. Positions are the receiver's own
(rounded to 4 decimals, about 10 m); no radio identity is in the report.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..collector.hits import distance_km, plan_radius_km, run_position
from ..db import Store

MAX_CARRIERS_PER_STOP = 40


def _settings(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _stops(store: Store, since: int, radius_km: float) -> list[dict[str, Any]]:
    stops: list[dict[str, Any]] = []
    nowhere: dict[str, Any] | None = None
    for r in store.rows("SELECT * FROM survey_runs WHERE started >= ? ORDER BY started, id", (since,)):
        run = dict(r)
        run["settings"] = _settings(run.get("settings"))
        pos = run_position(store, int(run["id"]))
        run["position"] = pos
        if pos is None:
            if nowhere is None:
                nowhere = {"position": None, "runs": []}
            nowhere["runs"].append(run)
            continue
        for s in stops:
            if distance_km(s["position"], pos) <= radius_km:
                s["runs"].append(run)
                n = len([x for x in s["runs"] if x["position"]])
                s["position"] = ((s["position"][0] * (n - 1) + pos[0]) / n, (s["position"][1] * (n - 1) + pos[1]) / n)
                break
        else:
            stops.append({"position": pos, "runs": [run]})
    return stops + ([nowhere] if nowhere else [])


def _known(store: Store, tolerance_hz: int) -> dict[int, str]:
    return {int(r["freq_hz"]): str(r["variant"]) for r in store.rows("SELECT freq_hz, variant FROM carriers")}


def _known_as(known: dict[int, str], f: int, tolerance_hz: int) -> str | None:
    if f in known:
        return known[f]
    near = [k for k in known if abs(k - f) <= tolerance_hz]
    return known[min(near, key=lambda k: abs(k - f))] if near else None


def coverage(store: Store, cfg: dict[str, Any], days: float = 7, now: int | None = None) -> dict[str, Any]:
    now = int(now or time.time())
    since = now - int(days * 86400)
    radius = plan_radius_km(cfg) or 3.0
    scfg = cfg.get("survey", {}) or {}
    bands = [{"name": str(b.get("name")), "start_hz": int(b["start_hz"]), "stop_hz": int(b["stop_hz"])} for b in scfg.get("bands", []) or []]
    tolerance = int(scfg.get("raster_tolerance_hz", 2500))
    known = _known(store, tolerance)
    stops = _stops(store, since, radius)
    sessions = []
    for r in store.rows("SELECT id, kind, params, started, finished, status, summary FROM sessions WHERE kind IN ('decode', 'watch')"
                        " AND started >= ?", (since,)):
        sm = _settings(r["summary"])
        pos = sm.get("position_end")
        sessions.append({"id": int(r["id"]), "kind": r["kind"], "minutes": round(float(sm.get("elapsed_s") or 0) / 60, 1),
                         "position": (float(pos["lat"]), float(pos["lon"])) if pos else None,
                         "campaign": (_settings(r["params"]).get("campaign") or {}).get("id")})
    best_at: dict[int, list[tuple[int, float]]] = {}       # freq -> [(stop no, best snr)]
    out_stops = []
    for no, s in enumerate(stops, start=1):
        runs = s["runs"]
        per_band: dict[str, dict[str, Any]] = {}
        heard: dict[int, dict[str, Any]] = {}
        for run in runs:
            name = str(run["band"])
            b = per_band.setdefault(name, {"full": 0, "partial": 0, "dwell_s": set(), "listen_s_per_step": 0.0})
            partial = bool(run["settings"].get("partial"))
            b["partial" if partial else "full"] += 1
            dwell = run["settings"].get("dwell_s")
            if dwell is not None:
                b["dwell_s"].add(float(dwell))
                if not partial:
                    b["listen_s_per_step"] += float(dwell)
            for h in store.rows("SELECT freq_hz, snr_db FROM survey_hits WHERE run_id = ?", (int(run["id"]),)):
                f = int(h["freq_hz"])
                d = heard.setdefault(f, {"freq_hz": f, "best_snr_db": None, "heard_in": 0, "band": name})
                d["heard_in"] += 1
                if h["snr_db"] is not None and (d["best_snr_db"] is None or h["snr_db"] > d["best_snr_db"]):
                    d["best_snr_db"] = round(float(h["snr_db"]), 1)
        carriers = []
        for f, d in heard.items():
            d["sweeps"] = per_band[d["band"]]["full"] + per_band[d["band"]]["partial"]
            d["known_as"] = _known_as(known, f, tolerance)
            carriers.append(d)
            if d["best_snr_db"] is not None:
                best_at.setdefault(f, []).append((no, d["best_snr_db"]))
        carriers.sort(key=lambda d: -(d["best_snr_db"] or -99))
        intermittent = sorted(d["freq_hz"] for d in carriers if d["sweeps"] > 1 and d["heard_in"] < d["sweeps"])
        gaps = []
        for b in bands:
            n = per_band.get(b["name"], {}).get("full", 0)
            if n == 0:
                gaps.append({"band": b["name"], "why": "never swept in full here"})
            elif n == 1:
                gaps.append({"band": b["name"], "why": "swept once: an intermittent repeater is easily missed"})
        here = s["position"]
        mine = [x for x in sessions if (here is None and x["position"] is None)
                or (here is not None and x["position"] is not None and distance_km(here, x["position"]) <= radius)]
        out_stops.append({
            "stop": no,
            "position": [round(here[0], 4), round(here[1], 4)] if here else None,
            "first": min(int(r["started"]) for r in runs), "last": max(int(r["finished"] or r["started"]) for r in runs),
            "bands": {k: {**v, "dwell_s": sorted(v["dwell_s"])} for k, v in sorted(per_band.items())},
            "carriers_heard": len(carriers), "carriers": carriers[:MAX_CARRIERS_PER_STOP],
            "intermittent": intermittent, "gaps": gaps,
            "decode_min": round(sum(x["minutes"] for x in mine if x["kind"] == "decode"), 1),
            "watch_min": round(sum(x["minutes"] for x in mine if x["kind"] == "watch"), 1),
            "campaigns": len({x["campaign"] for x in mine if x["campaign"]}),
        })
    return {"generated": now, "days": days, "radius_km": radius, "bands": bands, "stops": out_stops,
            "unheard_sites": _unheard_sites(store, best_at, tolerance)}


def _unheard_sites(store: Store, best_at: dict[int, list[tuple[int, float]]], tolerance: int) -> list[dict[str, Any]]:
    out = []
    for s in store.rows("SELECT s.id, s.system_id, s.site_number, s.label, s.name, sy.label AS system_label, sy.name AS system_name"
                        " FROM sites s JOIN systems sy ON sy.id = s.system_id WHERE s.directly_observed = 0 ORDER BY s.system_id, s.site_number"):
        members = []
        for c in store.rows("SELECT DISTINCT c.freq_hz FROM carrier_sites cs JOIN sites si ON si.id = cs.site_id"
                            " JOIN carriers c ON c.id = cs.carrier_id WHERE si.system_id = ?", (int(s["system_id"]),)):
            f = int(c["freq_hz"])
            seen = best_at.get(f) or next((v for k, v in best_at.items() if abs(k - f) <= tolerance), None) or []
            best = max(seen, key=lambda x: x[1]) if seen else None
            members.append({"freq_hz": f, "best_stop": best[0] if best else None, "best_snr_db": best[1] if best else None})
        members.sort(key=lambda m: -(m["best_snr_db"] if m["best_snr_db"] is not None else -99))
        out.append({"system": s["system_name"] or s["system_label"], "site_number": s["site_number"],
                    "site": s["name"] or s["label"], "system_carriers": members,
                    "hint": (f"its system was heard strongest from stop {members[0]['best_stop']} ({members[0]['freq_hz'] / 1e6:.4f} MHz,"
                             f" {members[0]['best_snr_db']} dB); a hint where to listen, not a direction")
                    if members and members[0]["best_stop"] else "its system's carriers were not heard by a survey in the window"})
    return out
