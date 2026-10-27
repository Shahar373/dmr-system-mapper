"""Ingest orchestration: files, directories, surveyor trees and live streams → Store."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from ..db import Store
from .dsdfme import LineParser, ParseContext, parse_log_filename
from .surveyor import is_surveyor_tree, read_candidates, scan
from .writer import Aggregator, IngestStats


def _tz(cfg: dict[str, Any]) -> ZoneInfo:
    return ZoneInfo(str(cfg.get("timezone") or "UTC"))


def _bucket_minutes(cfg: dict[str, Any]) -> int:
    return int(cfg.get("ingest", {}).get("bucket_minutes", 15))


def _fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def _band_for(cfg: dict[str, Any], freq_hz: int) -> str | None:
    for band in cfg.get("survey", {}).get("bands", []) or []:
        if int(band.get("start_hz", 0)) <= freq_hz <= int(band.get("stop_hz", 0)):
            return str(band.get("name"))
    return None


def ingest_lines(
    store: Store, lines: Iterable[str], ctx: ParseContext, cfg: dict[str, Any], path: str | None = None,
    fingerprint: str | None = None, seconds_observed: float | None = None, commit_every: int = 20000,
) -> IngestStats:
    """Parse ``lines`` for one carrier and write to the store. Works for files and live pipes."""
    if ctx.freq_hz is None:
        raise ValueError("carrier frequency is required (use --freq or a dsd_fme_<freqHz>_<date>_<time>.log name)")
    now = int(time.time())
    seen_at = int(ctx.start_time.timestamp()) if ctx.start_time else now
    carrier_id = store.upsert_carrier(ctx.freq_hz, seen_at, _band_for(cfg, ctx.freq_hz))
    run_id = store.start_ingest_run(ctx.source, path, carrier_id, now, fingerprint)
    stats = IngestStats(source=ctx.source, path=path, freq_hz=ctx.freq_hz, carrier_id=carrier_id)
    agg = Aggregator(store, carrier_id, ctx.source, _bucket_minutes(cfg), stats, seconds_observed)
    parser = LineParser(ctx)
    n = 0
    for raw in lines:
        for ev in parser.feed(raw):
            agg.feed(ev)
        n += 1
        if n % commit_every == 0:
            store.commit()
    for ev in parser.finish():
        agg.feed(ev)
    agg.flush()
    stats.lines, stats.parsed = parser.lines, parser.parsed
    store.finish_ingest_run(run_id, int(time.time()), stats.lines, stats.parsed, stats.unrecognized)
    store.commit()
    return stats


def ingest_log_file(
    store: Store, path: Path, cfg: dict[str, Any], freq_hz: int | None = None, start: datetime | None = None,
    source: str = "dsdfme_log", force: bool = False,
) -> IngestStats:
    tz = _tz(cfg)
    name_freq, name_start = parse_log_filename(path.name, tz)
    freq = freq_hz or name_freq
    start_time = start or name_start
    if start_time is not None and start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=tz)
    if freq is None:
        raise ValueError(f"{path}: cannot determine frequency; pass --freq")
    fp = _fingerprint(path)
    if not force and store.find_ingest_run(source, str(path), fp) is not None:
        st = IngestStats(source=source, path=str(path), freq_hz=freq, carrier_id=None, skipped=True,
                         note="already ingested (same size and mtime); use --force to re-ingest")
        return st
    ctx = ParseContext(freq_hz=freq, start_time=start_time, source=source, time_is_rf=True, tz=tz)
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return ingest_lines(store, fh, ctx, cfg, path=str(path), fingerprint=fp)


def ingest_surveyor(store: Store, root: Path, cfg: dict[str, Any], force: bool = False) -> list[IngestStats]:
    tz = _tz(cfg)
    results: list[IngestStats] = []
    attempts = list(scan(root, tz))
    for att in attempts:
        label = f"{att.candidate_id}/{att.recording_id}"
        if att.log_path is None:
            results.append(IngestStats(source="surveyor", path=str(att.attempt_dir), freq_hz=att.freq_hz, carrier_id=None,
                                       skipped=True, note=f"{label}: no non-empty DSD-FME log"))
            continue
        fp = _fingerprint(att.log_path)
        if not force and store.find_ingest_run("surveyor", str(att.log_path), fp) is not None:
            results.append(IngestStats(source="surveyor", path=str(att.log_path), freq_hz=att.freq_hz, carrier_id=None,
                                       skipped=True, note=f"{label}: already ingested"))
            continue
        ctx = ParseContext(freq_hz=att.freq_hz, start_time=att.start_time, source="surveyor", time_is_rf=False, tz=tz)
        with att.log_path.open("r", encoding="utf-8", errors="replace") as fh:
            st = ingest_lines(store, fh, ctx, cfg, path=str(att.log_path), fingerprint=fp, seconds_observed=att.duration_s)
        st.meta["candidate_id"] = att.candidate_id
        st.meta["recording_id"] = att.recording_id
        st.meta["decoder_status"] = att.status
        st.meta["best_inversion"] = att.best_inversion
        results.append(st)
    hits = read_candidates(root)
    if hits:
        started = min((int(a.start_time.timestamp()) for a in attempts if a.start_time), default=int(time.time()))
        run_id = store.add_survey_run("import", started, None, "surveyor", {"root": str(root)})
        for h in hits:
            store.add_survey_hit(run_id, h["freq_hz"], **{k: h.get(k) for k in ("snr_db", "p95_snr_db", "occupancy_pct", "width_hz", "class")})
        store.commit()
        results.append(IngestStats(source="surveyor_candidates", path=str(root), freq_hz=None, carrier_id=None,
                                   note=f"{len(hits)} survey hits imported as run {run_id}"))
    return results


def ingest_path(
    store: Store, path: Path, cfg: dict[str, Any], freq_hz: int | None = None, start: datetime | None = None,
    source: str | None = None, force: bool = False,
) -> list[IngestStats]:
    """Dispatch on what ``path`` is: a DSD-FME log, a directory of logs, or a surveyor tree."""
    if path.is_file():
        return [ingest_log_file(store, path, cfg, freq_hz, start, source or "dsdfme_log", force)]
    if not path.is_dir():
        raise FileNotFoundError(path)
    if is_surveyor_tree(path):
        return ingest_surveyor(store, path, cfg, force)
    results: list[IngestStats] = []
    for log in sorted(path.rglob("*.log")):
        if log.stat().st_size == 0:
            continue
        try:
            results.append(ingest_log_file(store, log, cfg, freq_hz, start, source or "dsdfme_log", force))
        except ValueError as exc:
            results.append(IngestStats(source=source or "dsdfme_log", path=str(log), freq_hz=None, carrier_id=None,
                                       skipped=True, note=str(exc)))
    return results


def ingest_stream(store: Store, lines: Iterator[str], cfg: dict[str, Any], freq_hz: int, source: str = "collector",
                  start: datetime | None = None) -> IngestStats:
    """Live mode: parse a pipe as it arrives. Observations are flushed at the end of the stream;
    the collector (Phase 3) restarts the stream per decode dwell so buckets stay small."""
    tz = _tz(cfg)
    ctx = ParseContext(freq_hz=freq_hz, start_time=start or datetime.now(tz=tz), source=source, time_is_rf=True, tz=tz)
    return ingest_lines(store, lines, ctx, cfg, path=None, fingerprint=None)
