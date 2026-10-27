"""Reader for dmr-iq-surveyor output trees.

The stable interface is the per-attempt decode directory::

    <root>/.../<Cxxxx>/<recording_id>/<iq|qi>/extraction_report.json
                                            decoder/decoder_report.json
                                            decoder/dsd_fme_<inversion>_stderr.log   (stdout is empty)

Candidate ids (``C0013``) are renumbered on every surveyor run and are kept only as labels; the
carrier identity is the frequency.  Optional ``candidates.csv`` files (Phase 3 output of the
surveyor) are imported as survey hits when present.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

RECORDING_ID_RE = re.compile(r"(?P<date>\d{8})_(?P<time>\d{6})")


@dataclass(slots=True)
class SurveyorAttempt:
    candidate_id: str
    recording_id: str
    freq_hz: int
    start_time: datetime | None      # recording start (local tz), None if not derivable
    duration_s: float | None
    best_inversion: str
    status: str
    quality_score: float | None
    log_path: Path | None
    attempt_dir: Path


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def recording_start(recording_id: str, tz: ZoneInfo) -> datetime | None:
    m = RECORDING_ID_RE.search(recording_id)
    if not m:
        return None
    return datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S").replace(tzinfo=tz)


def pick_log(decoder_dir: Path, inversion: str) -> Path | None:
    """Prefer the stderr log (where DSD-FME prints); fall back to stdout if it is the non-empty one."""
    for name in (f"dsd_fme_{inversion}_stderr.log", f"dsd_fme_{inversion}_stdout.log"):
        p = decoder_dir / name
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def scan(root: Path, tz: ZoneInfo) -> Iterator[SurveyorAttempt]:
    for report_path in sorted(root.rglob("extraction_report.json")):
        attempt_dir = report_path.parent
        try:
            report = _load_json(report_path)
        except (OSError, json.JSONDecodeError):
            continue
        freq = report.get("candidate_frequency_hz")
        if freq is None:
            continue
        recording_id = str(report.get("recording_id") or attempt_dir.parent.name)
        candidate_id = str(report.get("candidate_id") or attempt_dir.parent.parent.name)
        best_inv, status, score = "normal", "unknown", None
        decoder_dir = attempt_dir / "decoder"
        dr = decoder_dir / "decoder_report.json"
        if dr.exists():
            try:
                d = _load_json(dr)
                best_inv = str(d.get("best_inversion") or "normal")
                status = str(d.get("status") or "unknown")
                score = d.get("best_quality_score")
            except (OSError, json.JSONDecodeError):
                pass
        yield SurveyorAttempt(
            candidate_id=candidate_id,
            recording_id=recording_id,
            freq_hz=int(round(float(freq))),
            start_time=recording_start(recording_id, tz),
            duration_s=report.get("output_duration_seconds") or report.get("input_duration_seconds"),
            best_inversion=best_inv,
            status=status,
            quality_score=float(score) if score is not None else None,
            log_path=pick_log(decoder_dir, best_inv) if decoder_dir.exists() else None,
            attempt_dir=attempt_dir,
        )


def is_surveyor_tree(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any(path.rglob("extraction_report.json"))


def read_candidates(root: Path) -> list[dict[str, Any]]:
    """Import ``candidates.csv`` rows (surveyor Phase 3) found under ``root`` or its parents."""
    hits: list[dict[str, Any]] = []
    candidates: list[Path] = list(root.rglob("candidates.csv"))
    for parent in list(root.parents)[:2]:
        candidates.extend(parent.glob("candidates/candidates.csv"))
    for path in candidates:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                freq = row.get("frequency_hz_assuming_iq") or row.get("frequency_hz") or row.get("measured_center_hz")
                if not freq:
                    continue
                hits.append({
                    "freq_hz": int(round(float(freq))),
                    "snr_db": _f(row.get("average_snr_db") or row.get("snr_db")),
                    "p95_snr_db": _f(row.get("p95_snr_db")),
                    "occupancy_pct": _f(row.get("occupancy_pct")),
                    "width_hz": _f(row.get("width_90_hz") or row.get("bandwidth_hz")),
                    "class": row.get("preliminary_class") or row.get("spectral_class"),
                    "source_file": str(path),
                })
    return hits


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None
