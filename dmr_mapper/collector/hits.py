"""Survey hits as decode planning and the reports see them: the recent sweeps merged, near the
receiver. No numpy here, so `mapper export` and the MCP server need no collector extras."""

from __future__ import annotations

import math
import time
from typing import Any

from ..db import Store


def run_position(store: Store, run_id: int) -> tuple[float, float] | None:
    """Where a sweep was made: the mean position of its hits (each carries the position of the step
    that heard it strongest), or None when none has one."""
    r = store.conn.execute("SELECT AVG(lat) AS lat, AVG(lon) AS lon FROM survey_hits WHERE run_id = ? AND lat IS NOT NULL"
                           " AND lon IS NOT NULL", (int(run_id),)).fetchone()
    return None if r is None or r["lat"] is None else (float(r["lat"]), float(r["lon"]))


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance (gps.distance_m does the same; gps imports termios, which Windows lacks)."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def recent_hits(store: Store, window_s: int, max_age_s: int | None = None, near: tuple[float, float] | None = None,
                radius_km: float | None = None, far_runs: list[int] | None = None) -> list[dict[str, Any]]:
    """Hits of the recent sweeps, merged per frequency.

    Per band (a configured band, or a start..stop range of one, which is recorded under its own
    name) the newest run counts (if younger than ``max_age_s``) and with it every run of that band
    started up to ``window_s`` before it. A carrier heard in any of them is kept, with the values of
    the sweep that heard it strongest, ``heard_in`` (how many of those sweeps heard it), ``sweeps``
    (how many there were) and ``run_started`` (the last sweep that heard it). Several short sweeps
    therefore add up: a conventional repeater quiet during one sweep is still planned for when an
    earlier one heard it. ``window_s = 0`` is the newest run per band only.

    With ``near`` (the receiver's position now) and ``radius_km`` > 0, sweeps made farther away are
    left out before any of this, as if they had not happened (their ids go to ``far_runs``): after a
    drive to the next stop, the plan is made from what was heard here. Sweeps with no position
    always count."""
    runs = store.rows("SELECT id, band, started FROM survey_runs ORDER BY started DESC, id DESC")
    newest: dict[str, int] = {}
    chosen: dict[str, list[dict[str, Any]]] = {}
    now = int(time.time())
    for r in runs:
        band = str(r["band"])
        if band in newest and not (window_s > 0 and newest[band] - int(r["started"]) <= window_s):
            continue
        if band not in newest and max_age_s is not None and now - int(r["started"]) > max_age_s:
            continue
        if near is not None and radius_km:
            pos = run_position(store, int(r["id"]))
            if pos is not None and distance_km(near, pos) > float(radius_km):
                if far_runs is not None:
                    far_runs.append(int(r["id"]))
                continue
        if band not in newest:
            newest[band] = int(r["started"])
            chosen[band] = [dict(r)]
        else:
            chosen[band].append(dict(r))
    merged: dict[int, dict[str, Any]] = {}
    for band, band_runs in chosen.items():
        heard: dict[int, int] = {}
        for run in band_runs:
            for h in store.rows("SELECT * FROM survey_hits WHERE run_id = ? ORDER BY freq_hz", (run["id"],)):
                d = dict(h)
                f = int(d["freq_hz"])
                heard[f] = heard.get(f, 0) + 1
                d["band"] = band
                d["run_started"] = int(run["started"])
                prev = merged.get(f)
                if prev is None:
                    merged[f] = {**d, "heard_in": 0, "sweeps": 0}
                    continue
                last = max(int(prev["run_started"]), int(run["started"]))
                if (d["snr_db"] or -999) > (prev["snr_db"] or -999):
                    merged[f] = {**d, "heard_in": prev["heard_in"], "sweeps": prev["sweeps"]}
                merged[f]["run_started"] = last
        for f, n in heard.items():
            merged[f]["heard_in"] += n
            merged[f]["sweeps"] += len(band_runs)
    return [merged[f] for f in sorted(merged)]


def latest_hits(store: Store, max_age_s: int | None = None) -> list[dict[str, Any]]:
    """Hits of the most recent run per band (optionally only runs younger than ``max_age_s``)."""
    return recent_hits(store, 0, max_age_s)


def plan_radius_km(cfg: dict[str, Any]) -> float:
    """Sweeps made farther than this from the receiver's position are left out of decode planning (0: never)."""
    return float((cfg.get("survey", {}) or {}).get("plan_radius_km", 3))


def plan_window_s(cfg: dict[str, Any]) -> int:
    """How far back, before a band's newest sweep, decode planning and the hit lists look."""
    return int(float((cfg.get("survey", {}) or {}).get("plan_window_minutes", 180)) * 60)
