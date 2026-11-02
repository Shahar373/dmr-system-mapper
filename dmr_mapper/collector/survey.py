"""Band survey: step the receiver across a band, average a PSD per step, and report the raster
channels whose SNR clears the threshold (same meaning as the surveyor's p95 gate).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from scipy import fft as sfft

from ..db import Store
from .hits import distance_km, latest_hits, plan_radius_km, plan_window_s, recent_hits, run_position  # noqa: F401 (re-exported)
from .sdr import IQSource


@dataclass
class SurveyHit:
    freq_hz: int
    snr_db: float
    p95_snr_db: float
    occupancy_pct: float
    width_hz: float
    klass: str
    lat: float | None = None          # where the receiver was when this hit was measured
    lon: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"freq_hz": self.freq_hz, "snr_db": round(self.snr_db, 1), "p95_snr_db": round(self.p95_snr_db, 1),
             "occupancy_pct": round(self.occupancy_pct, 1), "width_hz": round(self.width_hz), "class": self.klass}
        if self.lat is not None:
            d["lat"], d["lon"] = round(self.lat, 5), round(self.lon, 5)
        return d


def step_centres(start_hz: int, stop_hz: int, sample_rate: float, usable_fraction: float) -> list[float]:
    usable = sample_rate * usable_fraction
    centres = []
    # half a 12.5 kHz channel off the raster: the receiver's DC point falls between two channels
    c = start_hz + usable / 2 + 6_250
    while c - usable / 2 < stop_hz:
        centres.append(c)
        c += usable
    return centres


def analyse_block(x: np.ndarray, sample_rate: float, center_hz: float, raster_hz: int, usable_fraction: float,
                  segment_s: float, snr_threshold_db: float, nfft: int = 4096) -> list[SurveyHit]:
    """PSD per segment (Welch, Hann), channel power per raster bin, noise floor = median channel
    power across the usable window, hits where the 95th percentile SNR over segments clears the gate."""
    seg = int(sample_rate * segment_s)
    nseg = max(1, len(x) // seg)
    win = np.hanning(nfft).astype(np.float32)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate)) + center_hz
    usable = sample_rate * usable_fraction / 2
    inband = np.abs(freqs - center_hz) <= usable
    psds = []
    for s in range(nseg):
        chunk = x[s * seg:(s + 1) * seg]
        nblk = len(chunk) // nfft
        if nblk == 0:
            continue
        frames = chunk[:nblk * nfft].reshape(nblk, nfft) * win
        P = np.abs(sfft.fft(frames, axis=1, workers=1)) ** 2
        psds.append(np.fft.fftshift(P.mean(axis=0)))
    if not psds:
        return []
    # the DC spike / LO leakage of a zero-IF receiver: replace the few bins around the tuned
    # frequency with the local noise level so it is not reported as a carrier
    dc = np.abs(freqs - center_hz) <= 1_500
    if dc.any():
        ring = (np.abs(freqs - center_hz) > 1_500) & (np.abs(freqs - center_hz) <= 12_000)
        for psd in psds:
            psd[dc] = np.median(psd[ring]) if ring.any() else np.median(psd)
    P = np.array(psds)                                   # (nseg, nfft)
    # raster channels inside the usable window
    first = int(np.ceil((center_hz - usable) / raster_hz)) * raster_hz
    chans = np.arange(first, center_hz + usable + 1, raster_hz)
    half = raster_hz / 2
    chan_power = np.empty((P.shape[0], len(chans)))
    for i, c in enumerate(chans):
        sel = (freqs >= c - half) & (freqs < c + half)
        chan_power[:, i] = P[:, sel].mean(axis=1) if sel.any() else 0.0
    mean_power = chan_power.mean(axis=0)
    noise = np.median(mean_power[mean_power > 0]) if (mean_power > 0).any() else 1e-30
    snr = 10 * np.log10(mean_power / noise + 1e-30)
    p95 = 10 * np.log10(np.percentile(chan_power, 95, axis=0) / noise + 1e-30)
    occ = (10 * np.log10(chan_power / noise + 1e-30) >= snr_threshold_db).mean(axis=0) * 100
    hits: list[SurveyHit] = []
    bin_hz = sample_rate / nfft
    mean_psd = P.mean(axis=0)
    noise_bin = np.median(mean_psd[inband])
    for i, c in enumerate(chans):
        if p95[i] < snr_threshold_db:
            continue
        # width: contiguous bins above noise + 6 dB around the channel centre
        ci = int(np.argmin(np.abs(freqs - c)))
        thr = noise_bin * 4
        lo = ci
        while lo > 0 and mean_psd[lo - 1] > thr:
            lo -= 1
        hi = ci
        while hi < nfft - 1 and mean_psd[hi + 1] > thr:
            hi += 1
        width = (hi - lo + 1) * bin_hz
        klass = "narrow" if width <= 1.5 * raster_hz else "wide"
        hits.append(SurveyHit(int(c), float(snr[i]), float(p95[i]), float(occ[i]), float(width), klass))
    # a strong carrier leaks into its neighbours: keep only local maxima of the mean SNR
    kept: list[SurveyHit] = []
    by_freq = {h.freq_hz: h for h in hits}
    for h in hits:
        left, right = by_freq.get(h.freq_hz - raster_hz), by_freq.get(h.freq_hz + raster_hz)
        if (left and left.snr_db > h.snr_db) or (right and right.snr_db >= h.snr_db and right is not h and right.snr_db > h.snr_db):
            continue
        kept.append(h)
    return kept


def sweep(source: IQSource, band: dict[str, Any], cfg: dict[str, Any], stop: Callable[[], bool] | None = None,
          progress: Callable[[dict[str, Any]], None] | None = None,
          position: Callable[[], tuple[float, float] | None] | None = None) -> list[SurveyHit]:
    """``position()`` gives the receiver's (lat, lon) at each step; a hit keeps the position of
    the step where it was strongest (the receiver may be moving)."""
    scfg = cfg.get("survey", {}) or {}
    sdr = cfg.get("sdr", {}) or {}
    usable_fraction = float(sdr.get("usable_fraction", 0.8))
    dwell = float(scfg.get("dwell_s", 3))
    segment = float(scfg.get("segment_s", 1))
    thr = float(scfg.get("snr_threshold_db", 9))
    raster = int(band.get("raster_hz", 12_500))
    centres = step_centres(int(band["start_hz"]), int(band["stop_hz"]), source.sample_rate, usable_fraction)
    hits: dict[int, SurveyHit] = {}
    for i, c in enumerate(centres):
        if stop and stop():
            break
        source.tune(c)
        source.read(int(source.sample_rate * 0.1))          # settle after retune
        x = source.read(int(source.sample_rate * dwell))
        # label hits with the frequency the source actually reports (a replay cannot retune)
        here = position() if position else None
        for h in analyse_block(x, source.sample_rate, source.center_hz, raster, usable_fraction, segment, thr):
            if here is not None:
                h.lat, h.lon = here
            if int(band["start_hz"]) <= h.freq_hz <= int(band["stop_hz"]):
                prev = hits.get(h.freq_hz)
                if prev is None or h.snr_db > prev.snr_db:
                    hits[h.freq_hz] = h
        if progress:
            progress({"band": band.get("name"), "step": i + 1, "steps": len(centres), "center_hz": int(c), "hits": len(hits)})
    return [hits[f] for f in sorted(hits)]


def record(store: Store, band: str, started: int, hits: list[SurveyHit], device: str, settings: dict[str, Any]) -> int:
    run_id = store.add_survey_run(band, started, int(time.time()), device, settings)
    for h in hits:
        store.add_survey_hit(run_id, h.freq_hz, snr_db=h.snr_db, p95_snr_db=h.p95_snr_db, occupancy_pct=h.occupancy_pct,
                             width_hz=h.width_hz, lat=h.lat, lon=h.lon, **{"class": h.klass})
    store.commit()
    return run_id


MAX_SWEEPS = 20


def max_dwell_s(cfg: dict[str, Any]) -> float:
    """The longest dwell per step at which a carrier 3 dB over the gate, transmitting during one
    segment only, still clears the gate.

    The hit test is the 95th percentile of the per-segment channel power (``np.percentile``,
    linear interpolation): the more segments a dwell holds, the less one loud segment weighs, and
    from 21 segments on it does not count at all. With the defaults (1 s segments, 9 dB gate) this
    is 11 s; a longer dwell would lose short transmissions of weak carriers."""
    scfg = cfg.get("survey", {}) or {}
    segment = float(scfg.get("segment_s", 1))
    thr = float(scfg.get("snr_threshold_db", 9))
    loud = 10 ** ((thr + 3) / 10)
    n = 1
    while n < 200:
        v = np.ones(n + 1)
        v[0] = loud
        if 10 * np.log10(np.percentile(v, 95)) < thr:
            break
        n += 1
    return n * segment


def check_survey_params(params: dict[str, Any], cfg: dict[str, Any]) -> tuple[float, int]:
    """(dwell per step, number of sweeps) a survey session will use; raises ValueError for values
    the hit test cannot honour. ``dwell_s`` in the params overrides ``survey.dwell_s`` for that
    session only."""
    scfg = cfg.get("survey", {}) or {}
    dwell = params.get("dwell_s")
    if dwell is None:
        dwell = float(scfg.get("dwell_s", 3))
    else:
        dwell = float(dwell)
        segment = float(scfg.get("segment_s", 1))
        top = max_dwell_s(cfg)
        if not segment <= dwell <= top:
            raise ValueError(f"dwell must be between {segment:g} and {top:g} s per step: longer, the p95 test over"
                             f" {segment:g} s segments loses short transmissions of weak carriers")
    sweeps = 1 if params.get("sweeps") is None else int(params["sweeps"])
    if not 1 <= sweeps <= MAX_SWEEPS:
        raise ValueError(f"sweeps must be between 1 and {MAX_SWEEPS}")
    return dwell, sweeps


def sweep_seconds(bands: list[dict[str, Any]], sample_rate: float, usable_fraction: float, dwell_s: float) -> float:
    """Roughly how long one sweep of these bands takes (dwell plus the settle read after a retune)."""
    steps = sum(len(step_centres(int(b["start_hz"]), int(b["stop_hz"]), sample_rate, usable_fraction)) for b in bands)
    return steps * (dwell_s + 0.1)


def sweeps_that_fit(requested: int, sweep_s: float, total_s: float) -> int:
    """How many sweeps a survey followed by a decode runs: as many as asked for while they leave at
    least half of the one budget to the decode, and always the first."""
    if sweep_s <= 0:
        return requested
    return max(1, min(requested, int((total_s / 2) // sweep_s)))


