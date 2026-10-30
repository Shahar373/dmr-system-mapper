"""Watching a whole slice (Phase 4).

A Capacity Max traffic channel transmits only while a call is on, so a survey dwell of a few
seconds misses it and a decode slice that picked it from the survey listens to silence. A watch
session instead tunes once to a slice that holds a system's carriers (1.6 MHz at 2 MS/s) and, every
0.1 s block, measures the power of every raster channel in it from the FFT the channelizer computes
anyway:

* the control channel(s) get a DSD-FME for the whole session (``pinned``);
* a channel that keys up (SNR over ``keyup_snr_db`` for two blocks) gets a DSD-FME started for it,
  fed first with the last ``preroll_s`` of its audio (rebuilt from stored FFT blocks) so the call
  header before the detection is not lost; the process is stopped ``hang_s`` after the key-down;
* at most ``limit`` DSD-FME run at once; a channel held longer than ``max_hold_s`` gives way when
  another one is waiting (a continuous carrier must not keep the traffic channels from being heard);
* every key-up is recorded (``keyups``): carrier, start, end, peak SNR, whether it was decoded. The
  correlator lines them up with the control channel's channel updates (which LCN carries which
  talkgroup) to learn each traffic channel's LCN.

Planning, detection and allocation are plain classes here; the daemon drives them with the
receiver, the channelizer and the decoders.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..correlate.context import t3_on_air
from ..db import Store
from .power import PowerState
from .scheduler import DC_GUARD_HZ, off_dc

CONTROL_SOURCES = ("c_aloha_sys_parms", "slc_c_sys_parms")


def watch_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    w = {"keyup_snr_db": 10.0, "keydown_snr_db": 6.0, "hang_s": 2.0, "min_keyup_s": 0.5, "max_hold_s": 30.0,
         "preroll_s": 0.5, "max_instances": 6, "channel_width_hz": 10_000}
    w.update(cfg.get("watch", {}) or {})
    return w


def watch_instances(ps: PowerState, cfg: dict[str, Any]) -> int:
    """DSD-FME processes a watch session may run at once. They run only while a channel is keyed
    (plus the pinned control channel), so the battery limit is its own (``battery_watch_instances``)."""
    pcfg = cfg.get("power", {}) or {}
    if ps.undervolt_now:
        return 1
    if ps.on_battery or ps.capped:
        return max(1, int(pcfg.get("battery_watch_instances", 3)))
    return max(1, int(watch_cfg(cfg)["max_instances"]))


@dataclass
class WatchPlan:
    center_hz: int
    channels: list[int]                 # channels watched for key-ups: the raster, with known carriers at their own frequency
    pinned: list[int]                   # control channels decoded the whole session
    reason: str
    raster_hz: int = 12_500
    system: str | None = None           # e.g. "tier3 Large net 2"
    priority: dict[int, int] = field(default_factory=dict)   # 0 the network's traffic channels, 1 unknown, 2 other systems
    blind: list[int] = field(default_factory=list)          # known carriers on the receiver's DC point: not watched

    def to_dict(self) -> dict[str, Any]:
        return {"center_hz": self.center_hz, "span_hz": [self.channels[0], self.channels[-1]] if self.channels else [],
                "channels": len(self.channels), "pinned": self.pinned, "reason": self.reason, "system": self.system,
                "network_carriers": sorted(f for f, p in self.priority.items() if p == 0), "blind": self.blind}


def _raster_for(cfg: dict[str, Any], freq: int) -> int:
    for b in (cfg.get("survey", {}) or {}).get("bands", []) or []:
        if int(b.get("start_hz", 0)) <= freq <= int(b.get("stop_hz", 0)):
            return int(b.get("raster_hz", 12_500))
    return 12_500


def _best_window(freqs: list[tuple[int, float]], usable: float) -> tuple[float, list[int]]:
    """The window of width ``usable`` holding the most weight: (weight, freqs in it)."""
    pts = sorted(freqs)
    best: tuple[float, list[int]] = (0.0, [])
    for i, (lo, _) in enumerate(pts):
        inside = [(f, w) for f, w in pts[i:] if f - lo <= usable]
        weight = sum(w for _, w in inside)
        if weight > best[0]:
            best = (weight, [f for f, _ in inside])
    return best


def tier3_carriers(store: Store, since: int) -> dict[int, dict[str, Any]]:
    """Tier III carriers with their network and whether they are control channels."""
    out: dict[int, dict[str, Any]] = {}
    for r in store.rows(
            "SELECT c.freq_hz, e.payload, e.count FROM csbk_events e JOIN carriers c ON c.id = e.carrier_id"
            " WHERE e.kind = 'tier3_sys_parms' AND e.last_ts >= ?", (since,)):
        p = json.loads(r["payload"])
        d = out.setdefault(int(r["freq_hz"]), {"control": 0, "payload": 0, "nets": {}})
        if p.get("source") in CONTROL_SOURCES:
            d["control"] += int(r["count"])
        else:
            d["payload"] += int(r["count"])
        key = (p.get("model"), t3_on_air(p.get("net_id"), p.get("site_id") or 0, p.get("sub_site"))[0])
        d["nets"][key] = d["nets"].get(key, 0) + int(r["count"])
    for d in out.values():
        d["net"] = max(d["nets"], key=d["nets"].get)
        d["is_control"] = d["control"] > 0 and d["control"] >= d["payload"]
    return out


def plan_watch(store: Store, cfg: dict[str, Any], sample_rate: float, limit: int, center_hz: int | None = None,
               freqs: list[int] | None = None, now: int | None = None) -> WatchPlan:
    sdr = cfg.get("sdr", {}) or {}
    usable = float(sample_rate) * float(sdr.get("usable_fraction", 0.8))
    now = int(now or time.time())
    t3 = tier3_carriers(store, now - 30 * 86400)
    controls = {f: d for f, d in t3.items() if d["is_control"]}
    known = {int(r["freq_hz"]): str(r["variant"]) for r in store.rows("SELECT freq_hz, variant FROM carriers WHERE last_seen >= ?",
                                                                         (now - 30 * 86400,))}
    system = None
    net = None
    if freqs:
        freqs = sorted(int(f) for f in freqs)
        if freqs[-1] - freqs[0] > usable - 2 * DC_GUARD_HZ:
            raise ValueError(f"{freqs[0] / 1e6:.4f}-{freqs[-1] / 1e6:.4f} MHz does not fit in one {usable / 1e6:.1f} MHz slice")
        center = (freqs[0] + freqs[-1]) // 2
        wanted = freqs
        reason = "requested carriers"
        nets = [controls[f]["net"] for f in freqs if f in controls]
        net = max(set(nets), key=nets.count) if nets else None
    elif center_hz:
        center = int(center_hz)
        wanted = []
        reason = "requested centre"
        nets = [d["net"] for f, d in controls.items() if abs(f - center) <= usable / 2]
        net = max(set(nets), key=nets.count) if nets else None
    else:
        # the Tier III network with the most carriers that fit in one slice; control channels count double
        best: tuple[float, list[int], Any] = (0.0, [], None)
        for candidate in {d["net"] for d in t3.values()}:
            pts = [(f, 2.0 if d["is_control"] else 1.0) for f, d in t3.items() if d["net"] == candidate]
            weight, inside = _best_window(pts, usable - 2 * DC_GUARD_HZ)
            if weight > best[0]:
                best = (weight, inside, candidate)
        if best[1]:
            wanted, net = best[1], best[2]
            reason = "Tier III network with the most carriers in one slice"
        else:
            weight, wanted = _best_window([(f, 1.0) for f in known], usable - 2 * DC_GUARD_HZ)
            reason = "busiest known slice"
        if not wanted:
            raise ValueError("nothing known to watch: run a survey first, or give a centre frequency or carriers")
        center = (min(wanted) + max(wanted)) // 2
    if net is not None:
        system = f"tier3 {net[0]} net {net[1]}"
    raster = _raster_for(cfg, center)
    # keep every known carrier of the slice off the receiver's DC point, not only the wanted ones
    span = [f for f in set(known) | set(wanted) if abs(f - center) <= usable / 2]
    center = off_dc(int(center), list(wanted) or span or [int(center) + DC_GUARD_HZ], usable)
    half = usable / 2
    explicit = sorted(f for f in set(known) | set(wanted) if abs(f - center) <= half)
    blind = [f for f in explicit if abs(f - center) < DC_GUARD_HZ]
    explicit = [f for f in explicit if f not in blind]
    # the raster, except where a known carrier sits off it: that one is watched at its own frequency
    # (a carrier 2.5 kHz off the raster would otherwise key up twice, as itself and as its raster twin)
    first = int(math.ceil((center - half) / raster)) * raster
    channels = [r for r in range(first, int(center + half) + 1, raster)
                if abs(r - center) >= DC_GUARD_HZ and not any(0 < abs(r - f) < raster / 2 for f in explicit)]
    channels = sorted(set(channels) | set(explicit))
    # only known control channels are pinned, at most half the decoders (one on battery)
    pin_from = [f for f in explicit if f in controls and (net is None or controls[f]["net"] == net)]
    if freqs and any(f in pin_from for f in freqs):
        # the control channels asked for come first (field run: `watch 469.35 469.15` pinned 469.65,
        # the other site's control channel, which simply had more history)
        pin_from = [f for f in freqs if f in pin_from]
    n_pin = min(len(pin_from), max(1, limit // 2))
    pinned = sorted(sorted(pin_from, key=lambda f: -controls[f]["control"])[:n_pin])
    # who gets a decoder first when several channels key up: the network's own traffic channels (and
    # carriers asked for), then anything unknown, then carriers known to belong elsewhere
    priority: dict[int, int] = {}
    for f in channels:
        if f in wanted and f not in controls:
            priority[f] = 0
        elif f in t3 and net is not None and t3[f]["net"] == net and not t3[f]["is_control"]:
            priority[f] = 0
        elif f in known and (known[f] not in ("unknown", "tier2") or f in controls):
            priority[f] = 2
        else:
            priority[f] = 1
    return WatchPlan(int(center), channels, pinned, reason, raster, system, priority, blind)


@dataclass
class Keyup:
    freq_hz: int
    start: float
    end: float
    peak_snr_db: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class _Chan:
    active: bool = False
    above_n: int = 0
    first_above: float = 0.0
    last_above: float = 0.0
    peak: float = 0.0


class KeyupDetector:
    """Per raster channel: key-up after ``min_blocks`` blocks over ``on_db``, key-down when under
    ``off_db`` for ``hang_s``. The noise floor is the median channel (most of a slice is empty).
    A channel whose neighbour is more than ``skirt_db`` stronger is that neighbour's skirt, not a
    carrier: a DMR signal leaves well under 1 % of its power in the next 12.5 kHz channel, so only
    a very strong neighbour (or the receiver's own spread around it) can lift an empty channel."""

    def __init__(self, freqs: list[int], on_db: float = 10.0, off_db: float = 6.0, hang_s: float = 2.0, min_blocks: int = 2,
                 skirt_db: float = 25.0):
        self.freqs = [int(f) for f in freqs]
        self.on_db, self.off_db, self.hang_s, self.min_blocks, self.skirt_db = on_db, off_db, hang_s, min_blocks, skirt_db
        self.state = {f: _Chan() for f in self.freqs}
        self.noise: float | None = None
        self.snr = np.zeros(len(self.freqs))

    def update(self, t: float, power: np.ndarray) -> tuple[list[int], list[Keyup]]:
        p = np.maximum(np.asarray(power, dtype=np.float64), 1e-30)
        med = float(np.median(p))
        self.noise = med if self.noise is None else 0.8 * self.noise + 0.2 * med
        snr = 10 * np.log10(p / self.noise)
        self.snr = snr
        left = np.concatenate([[-np.inf], snr[:-1]])
        right = np.concatenate([snr[1:], [-np.inf]])
        not_skirt = (left <= snr + self.skirt_db) & (right <= snr + self.skirt_db)
        started: list[int] = []
        ended: list[Keyup] = []
        for i, f in enumerate(self.freqs):
            c = self.state[f]
            s = float(snr[i])
            if not c.active:
                if s >= self.on_db and not_skirt[i]:
                    if c.above_n == 0:
                        c.first_above = t
                    c.above_n += 1
                    c.peak = max(c.peak, s) if c.above_n > 1 else s
                    if c.above_n >= self.min_blocks:
                        c.active, c.last_above = True, t
                        started.append(f)
                else:
                    c.above_n = 0
            else:
                if s >= self.off_db:
                    c.last_above = t
                    c.peak = max(c.peak, s)
                elif t - c.last_above >= self.hang_s:
                    ended.append(Keyup(f, c.first_above, c.last_above, round(c.peak, 1)))
                    self.state[f] = _Chan()
        return started, ended

    def active(self) -> dict[int, float]:
        return {f: c.first_above for f, c in self.state.items() if c.active}

    def close(self) -> list[Keyup]:
        """End every open key-up (the session is ending)."""
        out = [Keyup(f, c.first_above, c.last_above, round(c.peak, 1)) for f, c in self.state.items() if c.active]
        self.state = {f: _Chan() for f in self.freqs}
        return out

    def snr_of(self, freq: int) -> float | None:
        try:
            return round(float(self.snr[self.freqs.index(freq)]), 1)
        except (ValueError, IndexError):
            return None


@dataclass
class InstancePlanner:
    """Which channels have a DSD-FME. Pinned channels always. Then the waiting channels by
    ``priority`` (0 the network's traffic channels, 1 unknown, 2 known to belong elsewhere) and,
    within one priority, the most recent key-up first (a call is short; a carrier keyed since the
    start of the session is most likely a continuous one). A waiting channel takes the decoder of
    a lower-priority one at once, and of an equal one that has held it for ``max_hold_s``; the
    channel that gave way is not taken again until it has keyed down."""

    pinned: list[int]
    limit: int
    max_hold_s: float = 30.0
    priority: dict[int, int] = field(default_factory=dict)
    assigned: dict[int, float] = field(default_factory=dict)     # freq → since when it has a decoder
    cooldown: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.pinned = list(self.pinned)[:max(1, self.limit)]

    def _prio(self, f: int) -> int:
        return self.priority.get(f, 1)

    def _victim(self, t: float, prio: int) -> int | None:
        floating = [(g, since) for g, since in self.assigned.items() if g not in self.pinned]
        lower = [(self._prio(g), -since, g) for g, since in floating if self._prio(g) > prio]
        if lower:
            return max(lower)[2]                    # the lowest priority, longest held
        held = [(since, g) for g, since in floating if self._prio(g) == prio and t - since >= self.max_hold_s]
        return min(held)[1] if held else None

    def plan(self, t: float, active: dict[int, float]) -> tuple[list[int], list[int]]:
        start: list[int] = []
        stop: list[int] = []
        for f in self.pinned:
            if f not in self.assigned:
                self.assigned[f] = t
                start.append(f)
        for f in list(self.assigned):
            if f not in self.pinned and f not in active:
                del self.assigned[f]
                stop.append(f)
        self.cooldown &= set(active)
        waiting = sorted((f for f in active if f not in self.assigned and f not in self.cooldown and f not in self.pinned),
                         key=lambda f: (self._prio(f), -active[f]))
        for f in waiting:
            if len(self.assigned) >= self.limit:
                g = self._victim(t, self._prio(f))
                if g is None:
                    continue        # a later waiter may still have a lower-priority channel to take
                del self.assigned[g]
                stop.append(g)
                self.cooldown.add(g)
            self.assigned[f] = t
            start.append(f)
        return start, stop

    def waiting(self, active: dict[int, float]) -> list[int]:
        return sorted(f for f in active if f not in self.assigned)
