"""Thin sqlite3 wrapper: migrations, upserts, and the queries the CLI needs."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{3})_.*\.sql$")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    """One SQLite database. Use as a context manager or call ``close()``."""

    def __init__(self, path: str | Path | None, create: bool = True):
        self.path = Path(path) if path not in (None, ":memory:") else None
        if self.path is not None:
            if not create and not self.path.exists():
                raise FileNotFoundError(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path) if self.path else ":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path is not None:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    # -- lifecycle -------------------------------------------------------------------------
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # -- migrations ------------------------------------------------------------------------
    def schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if row is None:
            return 0
        return int(self.conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] or 0)

    def migrate(self) -> int:
        current = self.schema_version()
        scripts = sorted(
            (int(m.group(1)), p)
            for p in MIGRATIONS_DIR.glob("*.sql")
            if (m := _MIGRATION_RE.match(p.name))
        )
        for version, path in scripts:
            if version <= current:
                continue
            with self.conn:
                self.conn.executescript(path.read_text(encoding="utf-8"))
                if version > 1:
                    self.conn.execute("UPDATE schema_meta SET version = ?", (version,))
            current = version
        return current

    # -- carriers --------------------------------------------------------------------------
    def upsert_carrier(self, freq_hz: int, seen_at: int, band: str | None = None) -> int:
        row = self.conn.execute("SELECT id FROM carriers WHERE freq_hz = ?", (freq_hz,)).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO carriers (freq_hz, band, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (freq_hz, band, seen_at, seen_at),
            )
            return int(cur.lastrowid)
        self.conn.execute(
            "UPDATE carriers SET first_seen = MIN(first_seen, ?), last_seen = MAX(last_seen, ?),"
            " band = COALESCE(band, ?) WHERE id = ?",
            (seen_at, seen_at, band, row["id"]),
        )
        return int(row["id"])

    def update_carrier_summary(self, carrier_id: int) -> None:
        """Recompute dominant colour code and variant from stored data."""
        hist: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT cc_histogram FROM observations WHERE carrier_id = ?", (carrier_id,)
        ):
            for cc, n in json.loads(row["cc_histogram"]).items():
                hist[cc] = hist.get(cc, 0) + n
        dominant = int(max(hist, key=hist.get)) if hist else None
        kinds = {
            r["kind"]: r["n"]
            for r in self.conn.execute(
                "SELECT kind, SUM(count) AS n FROM csbk_events WHERE carrier_id = ? GROUP BY kind",
                (carrier_id,),
            )
        }
        bursts = self.scalar("SELECT COALESCE(SUM(bursts), 0) FROM observations WHERE carrier_id = ?", (carrier_id,))
        variant, conf = classify_variant(kinds, int(bursts or 0))
        self.conn.execute(
            "UPDATE carriers SET dominant_cc = ?, variant = ?, variant_confidence = ? WHERE id = ?",
            (dominant, variant, conf, carrier_id),
        )

    def carriers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM carriers ORDER BY freq_hz").fetchall()

    # -- observations ----------------------------------------------------------------------
    def add_observation(self, carrier_id: int, bucket_start: int, source: str, agg: dict[str, Any]) -> None:
        existing = self.conn.execute(
            "SELECT * FROM observations WHERE carrier_id = ? AND bucket_start = ? AND source = ?",
            (carrier_id, bucket_start, source),
        ).fetchone()
        hist = dict(agg.get("cc_histogram", {}))
        if existing is not None:
            for cc, n in json.loads(existing["cc_histogram"]).items():
                hist[cc] = hist.get(cc, 0) + n
        total_cc = sum(hist.values())
        cc_dominant = int(max(hist, key=hist.get)) if hist else None
        cc_consistency = (hist[str(cc_dominant)] / total_cc) if hist else None
        counters = ("bursts", "csbk", "voice", "data", "idle", "crc_err", "no_sync", "slot1_syncs", "slot2_syncs")
        if existing is None:
            self.conn.execute(
                "INSERT INTO observations (carrier_id, bucket_start, source, seconds_observed,"
                " bursts, csbk, voice, data, idle, crc_err, no_sync, cc_dominant, cc_consistency,"
                " slot1_syncs, slot2_syncs, snr_db_max, snr_db_avg, rest_lsn_last, cc_histogram)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    carrier_id, bucket_start, source, float(agg.get("seconds_observed", 0.0)),
                    *[int(agg.get(c, 0)) for c in counters[:7]],
                    cc_dominant, cc_consistency,
                    int(agg.get("slot1_syncs", 0)), int(agg.get("slot2_syncs", 0)),
                    agg.get("snr_db_max"), agg.get("snr_db_avg"), agg.get("rest_lsn_last"),
                    canonical_json(hist),
                ),
            )
            return
        sets = ", ".join(f"{c} = {c} + ?" for c in counters)
        self.conn.execute(
            f"UPDATE observations SET seconds_observed = seconds_observed + ?, {sets},"
            " cc_dominant = ?, cc_consistency = ?, snr_db_max = MAX(COALESCE(snr_db_max, -999), COALESCE(?, -999)),"
            " snr_db_avg = COALESCE(?, snr_db_avg), rest_lsn_last = COALESCE(?, rest_lsn_last), cc_histogram = ?"
            " WHERE carrier_id = ? AND bucket_start = ? AND source = ?",
            (
                float(agg.get("seconds_observed", 0.0)),
                *[int(agg.get(c, 0)) for c in counters],
                cc_dominant, cc_consistency, agg.get("snr_db_max"), agg.get("snr_db_avg"),
                agg.get("rest_lsn_last"), canonical_json(hist),
                carrier_id, bucket_start, source,
            ),
        )
        # MAX() with the -999 sentinel must not leave a sentinel behind
        self.conn.execute(
            "UPDATE observations SET snr_db_max = NULL WHERE snr_db_max <= -999 AND carrier_id = ?"
            " AND bucket_start = ? AND source = ?",
            (carrier_id, bucket_start, source),
        )

    # -- events ----------------------------------------------------------------------------
    def add_csbk_event(
        self, carrier_id: int, ts: int, slot: int | None, cc: int | None, kind: str,
        payload: dict[str, Any], dedup_key: str,
    ) -> bool:
        """Insert or bump a de-duplicated structural event. Returns True when a new row was made."""
        cur = self.conn.execute(
            "UPDATE csbk_events SET count = count + 1, last_ts = MAX(last_ts, ?), ts = MIN(ts, ?)"
            " WHERE carrier_id = ? AND kind = ? AND dedup_key = ?",
            (ts, ts, carrier_id, kind, dedup_key),
        )
        if cur.rowcount:
            return False
        self.conn.execute(
            "INSERT INTO csbk_events (carrier_id, ts, slot, cc, kind, payload, count, last_ts, dedup_key)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (carrier_id, ts, slot, cc, kind, canonical_json(payload), ts, dedup_key),
        )
        return True

    def add_rest_event(self, carrier_id: int, ts: int, site: int | None, rest_lsn: int, prev_lsn: int | None) -> None:
        self.conn.execute(
            "INSERT INTO rest_events (carrier_id, ts, site, rest_lsn, prev_lsn) VALUES (?, ?, ?, ?, ?)",
            (carrier_id, ts, site, rest_lsn, prev_lsn),
        )

    # -- identities ------------------------------------------------------------------------
    def upsert_identity(self, kind: str, value: int, seen_at: int) -> int:
        row = self.conn.execute(
            "SELECT id FROM identities WHERE kind = ? AND value = ?", (kind, value)
        ).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO identities (kind, value, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (kind, value, seen_at, seen_at),
            )
            return int(cur.lastrowid)
        self.conn.execute(
            "UPDATE identities SET first_seen = MIN(first_seen, ?), last_seen = MAX(last_seen, ?) WHERE id = ?",
            (seen_at, seen_at, row["id"]),
        )
        return int(row["id"])

    def add_sighting(self, identity_id: int, carrier_id: int, day: int, count: int, roles: Iterable[str]) -> None:
        existing = self.conn.execute(
            "SELECT count, roles FROM identity_sightings WHERE identity_id = ? AND carrier_id = ? AND day = ?",
            (identity_id, carrier_id, day),
        ).fetchone()
        role_set = set(roles)
        if existing is None:
            self.conn.execute(
                "INSERT INTO identity_sightings (identity_id, carrier_id, day, count, roles) VALUES (?, ?, ?, ?, ?)",
                (identity_id, carrier_id, day, count, ",".join(sorted(role_set))),
            )
            return
        role_set.update(r for r in existing["roles"].split(",") if r)
        self.conn.execute(
            "UPDATE identity_sightings SET count = count + ?, roles = ? WHERE identity_id = ? AND carrier_id = ? AND day = ?",
            (count, ",".join(sorted(role_set)), identity_id, carrier_id, day),
        )

    # -- ingest bookkeeping ------------------------------------------------------------------
    def find_ingest_run(self, source: str, path: str, fingerprint: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ingest_runs WHERE source = ? AND path = ? AND fingerprint = ? AND finished IS NOT NULL",
            (source, path, fingerprint),
        ).fetchone()

    def start_ingest_run(self, source: str, path: str | None, carrier_id: int | None, started: int, fingerprint: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingest_runs (source, path, carrier_id, started, fingerprint) VALUES (?, ?, ?, ?, ?)",
            (source, path, carrier_id, started, fingerprint),
        )
        return int(cur.lastrowid)

    def finish_ingest_run(self, run_id: int, finished: int, lines: int, parsed: int, unrecognized: dict[str, tuple[int, str]]) -> None:
        self.conn.execute(
            "UPDATE ingest_runs SET finished = ?, lines = ?, parsed = ?, unrecognized = ? WHERE id = ?",
            (finished, lines, parsed, sum(c for c, _ in unrecognized.values()), run_id),
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO unrecognized_lines (run_id, pattern, count, example) VALUES (?, ?, ?, ?)",
            [(run_id, pat, cnt, ex) for pat, (cnt, ex) in unrecognized.items()],
        )

    # -- survey ----------------------------------------------------------------------------
    def add_survey_run(self, band: str, started: int, finished: int | None, device: str | None, settings: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO survey_runs (band, started, finished, device, settings) VALUES (?, ?, ?, ?, ?)",
            (band, started, finished, device, canonical_json(settings)),
        )
        return int(cur.lastrowid)

    def add_survey_hit(self, run_id: int, freq_hz: int, **fields: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO survey_hits (run_id, freq_hz, snr_db, p95_snr_db, occupancy_pct, width_hz, class)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, freq_hz, fields.get("snr_db"), fields.get("p95_snr_db"), fields.get("occupancy_pct"),
             fields.get("width_hz"), fields.get("class")),
        )

    # -- generic ---------------------------------------------------------------------------
    def scalar(self, sql: str, params: tuple = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()


_VARIANT_MARKERS: dict[str, tuple[str, ...]] = {
    "capplus": ("capplus_channel_status", "capplus_site", "capplus_adjacent", "capplus_rest_change"),
    "conplus": ("conplus_sys", "conplus_adjacent"),
    "tier3": ("tier3_sys_parms", "tier3_bcast"),
    "xpt": ("xpt_site_status", "xpt_adjacent", "xpt_slco"),
}


def classify_variant(kind_counts: dict[str, int], bursts: int = 0) -> tuple[str, float]:
    """Pick the DMR variant from structural event counts. Tier II is the default when only
    generic events (activity updates, voice LC) or plain bursts were seen."""
    scores = {
        variant: sum(kind_counts.get(k, 0) for k in kinds) for variant, kinds in _VARIANT_MARKERS.items()
    }
    best = max(scores, key=scores.get)
    total_explicit = sum(scores.values())
    if total_explicit == 0:
        generic = sum(kind_counts.get(k, 0) for k in ("activity_update", "protected_lc", "call"))
        if generic:
            return "tier2", min(0.6, 0.3 + 0.05 * generic)
        if bursts >= 20:
            return "tier2", 0.3
        return "unknown", 0.0
    share = scores[best] / total_explicit
    conf = min(0.99, 0.5 + 0.1 * min(scores[best], 4)) * share
    return best, round(conf, 3)
