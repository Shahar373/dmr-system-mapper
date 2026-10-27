"""Ingest orchestration: files, directories, surveyor trees and live streams → Store."""

from __future__ import annotations

import hashlib
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from ..db import Store
from .dsdfme import SYNC_RE, LineParser, ParseContext, parse_log_filename, strip_ansi
from .surveyor import is_surveyor_tree, read_candidates, scan
from .writer import Aggregator, IngestStats

_HEAD_BYTES = 65536


def _tz(cfg: dict[str, Any]) -> ZoneInfo:
    return ZoneInfo(str(cfg.get("timezone") or "UTC"))


def _bucket_minutes(cfg: dict[str, Any]) -> int:
    return int(cfg.get("ingest", {}).get("bucket_minutes", 15))


def file_fingerprint(path: Path) -> str:
    """Content hash of the first 64 KB: stable while a log is being appended to, so a growing
    file is recognised as the same file and only its new tail is ingested."""
    with path.open("rb") as fh:
        head = fh.read(_HEAD_BYTES)
    return "head1:" + hashlib.sha1(head).hexdigest()


def _band_for(cfg: dict[str, Any], freq_hz: int) -> str | None:
    for band in cfg.get("survey", {}).get("bands", []) or []:
        if int(band.get("start_hz", 0)) <= freq_hz <= int(band.get("stop_hz", 0)):
            return str(band.get("name"))
    return None


def _localise(start: datetime | None, tz: ZoneInfo) -> datetime | None:
    if start is not None and start.tzinfo is None:
        return start.replace(tzinfo=tz)
    return start


class _Stop(Exception):
    """Raised from the SIGTERM handler so a live ingest flushes and exits cleanly."""


def ingest_lines(
    store: Store, lines: Iterable[str], ctx: ParseContext, cfg: dict[str, Any], path: str | None = None,
    fingerprint: str | None = None, seconds_observed: float | None = None, live: bool = False,
    flush_every_s: int = 60, byte_counter: list[int] | None = None,
) -> IngestStats:
    """Parse ``lines`` for one carrier and write to the store.

    File mode (``live=False``): one transaction for the whole file, so an interrupted ingest leaves
    nothing behind (``Store.__exit__`` rolls back) and the next run starts clean.
    Live mode: closed buckets and identities are flushed and committed every ``flush_every_s``
    seconds and on SIGTERM, so a killed collector loses at most the open bucket.
    """
    if ctx.freq_hz is None:
        raise ValueError("carrier frequency is required (use --freq or a dsd_fme_<freqHz>_<date>_<time>.log name)")
    ctx.start_time = _localise(ctx.start_time, ctx.tz)
    now = int(time.time())
    seen_at = int(ctx.start_time.timestamp()) if ctx.start_time else now
    carrier_id = store.upsert_carrier(ctx.freq_hz, seen_at, _band_for(cfg, ctx.freq_hz))
    run_id = store.start_ingest_run(ctx.source, path, carrier_id, now, fingerprint)
    stats = IngestStats(source=ctx.source, path=path, freq_hz=ctx.freq_hz, carrier_id=carrier_id)
    agg = Aggregator(store, carrier_id, ctx.source, _bucket_minutes(cfg), stats, seconds_observed)
    parser = LineParser(ctx)
    if live:
        store.commit()
    last_flush = time.monotonic()
    previous = signal.getsignal(signal.SIGTERM) if live else None

    def _on_term(signum: int, frame: Any) -> None:
        raise _Stop()

    if live:
        signal.signal(signal.SIGTERM, _on_term)
    try:
        for raw in lines:
            if byte_counter is not None:
                byte_counter[0] += len(raw.encode("utf-8", errors="replace"))
            for ev in parser.feed(raw):
                agg.feed(ev)
            if live and time.monotonic() - last_flush >= flush_every_s:
                agg.flush(partial_before=int(time.time()))
                store.commit()
                last_flush = time.monotonic()
    except (_Stop, KeyboardInterrupt):
        stats.note = "stream stopped by signal"
    finally:
        if live:
            signal.signal(signal.SIGTERM, previous or signal.SIG_DFL)
    for ev in parser.finish():
        agg.feed(ev)
    agg.flush()
    stats.lines, stats.parsed = parser.lines, parser.parsed
    store.finish_ingest_run(run_id, int(time.time()), stats.lines, stats.parsed, stats.unrecognized,
                            byte_offset=byte_counter[0] if byte_counter else 0)
    store.commit()
    return stats


def ingest_log_file(
    store: Store, path: Path, cfg: dict[str, Any], freq_hz: int | None = None, start: datetime | None = None,
    source: str = "dsdfme_log", force: bool = False,
) -> IngestStats:
    tz = _tz(cfg)
    path = path.resolve()
    name_freq, name_start = parse_log_filename(path.name, tz)
    freq = freq_hz or name_freq
    start_time = _localise(start or name_start, tz)
    if freq is None:
        raise ValueError(f"{path}: cannot determine frequency; pass --freq")
    size = path.stat().st_size
    fp = file_fingerprint(path)
    offset = 0
    note = ""
    if not force:
        prev = store.find_ingest_run(source, str(path), fp)
        if prev is not None:
            if int(prev["byte_offset"]) >= size:
                return IngestStats(source=source, path=str(path), freq_hz=freq, carrier_id=None, skipped=True,
                                   note="already ingested (same content); use --force to re-ingest")
            offset = int(prev["byte_offset"])
            note = f"resumed at byte {offset} (file grew since the last ingest)"
    if start_time is None:
        start_time = datetime.fromtimestamp(path.stat().st_mtime, tz=tz)
        note = (note + "; " if note else "") + "no date in the file name: using the file's modification time"
    ctx = ParseContext(freq_hz=freq, start_time=start_time, source=source, time_is_rf=True, tz=tz)
    counter = [offset]
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        if offset:
            fh.seek(offset)
        st = ingest_lines(store, fh, ctx, cfg, path=str(path), fingerprint=fp, byte_counter=counter)
    if note:
        st.note = note
    return st


def _clock_scale(log_path: Path, duration_s: float | None) -> float:
    """Surveyor decode logs carry the decode wall clock, which runs much faster than the recording
    (11 s of audio decode in ~1 s). Map the clock span onto the recording duration."""
    if not duration_s:
        return 1.0
    first = last = None
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            m = SYNC_RE.match(strip_ansi(raw).strip())
            if m:
                h, mi, s = (int(x) for x in m.group("clock").split(":"))
                v = h * 3600 + mi * 60 + s
                first = v if first is None else first
                last = v
    if first is None or last is None:
        return 1.0
    span = last - first
    if span < 0:
        span += 86400
    if span <= 0:
        return 0.0   # everything decoded within one clock second: stamp all events at the start
    return float(duration_s) / span


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
        log_path = att.log_path.resolve()
        fp = file_fingerprint(log_path)
        if not force:
            prev = store.find_ingest_run("surveyor", str(log_path), fp)
            if prev is not None and int(prev["byte_offset"]) >= log_path.stat().st_size:
                results.append(IngestStats(source="surveyor", path=str(log_path), freq_hz=att.freq_hz, carrier_id=None,
                                           skipped=True, note=f"{label}: already ingested"))
                continue
        ctx = ParseContext(freq_hz=att.freq_hz, start_time=att.start_time, source="surveyor", time_is_rf=False, tz=tz,
                           clock_scale=_clock_scale(log_path, att.duration_s))
        counter = [0]
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            st = ingest_lines(store, fh, ctx, cfg, path=str(log_path), fingerprint=fp, seconds_observed=att.duration_s,
                              byte_counter=counter)
        st.meta["candidate_id"] = att.candidate_id
        st.meta["recording_id"] = att.recording_id
        st.meta["decoder_status"] = att.status
        st.meta["best_inversion"] = att.best_inversion
        results.append(st)
    hits = read_candidates(root)
    if hits:
        by_file: dict[str, list[dict[str, Any]]] = {}
        for h in hits:
            by_file.setdefault(h["source_file"], []).append(h)
        for src_file, rows in by_file.items():
            csv_path = Path(src_file).resolve()
            fp = file_fingerprint(csv_path)
            if not force and store.find_ingest_run("surveyor_candidates", str(csv_path), fp) is not None:
                results.append(IngestStats(source="surveyor_candidates", path=str(csv_path), freq_hz=None, carrier_id=None,
                                           skipped=True, note="candidates already imported"))
                continue
            started = min((int(a.start_time.timestamp()) for a in attempts if a.start_time), default=int(time.time()))
            run_id = store.add_survey_run("import", started, None, "surveyor", {"root": str(root), "file": str(csv_path)})
            for h in rows:
                store.add_survey_hit(run_id, h["freq_hz"], **{k: h.get(k) for k in ("snr_db", "p95_snr_db", "occupancy_pct", "width_hz", "class")})
            ingest_id = store.start_ingest_run("surveyor_candidates", str(csv_path), None, int(time.time()), fp)
            store.finish_ingest_run(ingest_id, int(time.time()), len(rows), len(rows), {}, byte_offset=csv_path.stat().st_size)
            store.commit()
            results.append(IngestStats(source="surveyor_candidates", path=str(csv_path), freq_hz=None, carrier_id=None,
                                       note=f"{len(rows)} survey hits imported as run {run_id}"))
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
                  start: datetime | None = None, flush_every_s: int = 60) -> IngestStats:
    """Live mode: parse a pipe as it arrives, flushing closed buckets periodically and on SIGTERM."""
    tz = _tz(cfg)
    ctx = ParseContext(freq_hz=freq_hz, start_time=_localise(start, tz) or datetime.now(tz=tz), source=source,
                       time_is_rf=True, tz=tz)
    return ingest_lines(store, lines, ctx, cfg, path=None, fingerprint=None, live=True, flush_every_s=flush_every_s)


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_start(text: str | None) -> datetime | None:
    """ISO 8601 ``--start`` value; naive values are later localised to the configured time zone."""
    if not text:
        return None
    if not _ISO_RE.match(text):
        raise ValueError(f"--start must be ISO 8601 (YYYY-MM-DDTHH:MM:SS[+TZ]), got {text!r}")
    return datetime.fromisoformat(text)
