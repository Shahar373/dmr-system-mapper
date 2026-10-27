"""``mapper`` command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import load_config, resolve_path
from .db import Store


def _open_store(args: argparse.Namespace, cfg: dict) -> Store:
    path = Path(args.db) if getattr(args, "db", None) else resolve_path(cfg, "db")
    return Store(path)


def cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest import ingest_path, ingest_stream
    from .ingest.report import format_report

    cfg = load_config(args.config)
    start = datetime.fromisoformat(args.start) if args.start else None
    with _open_store(args, cfg) as store:
        if args.path == "-" or args.stream:
            if not args.freq:
                print("error: --freq is required when ingesting a stream", file=sys.stderr)
                return 2
            lines = sys.stdin if args.path == "-" else Path(args.path).open("r", encoding="utf-8", errors="replace")
            results = [ingest_stream(store, lines, cfg, args.freq, args.source or "collector", start)]
        else:
            results = ingest_path(store, Path(args.path), cfg, args.freq, start, args.source, args.force)
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(format_report(results))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        print(f"db: {store.path}  schema v{store.schema_version()}")
        for name in ("carriers", "observations", "csbk_events", "rest_events", "identities", "identity_sightings",
                     "links", "evidence", "ingest_runs", "survey_hits"):
            print(f"  {name:20s} {store.scalar(f'SELECT COUNT(*) FROM {name}')}")
        print("carriers:")
        for c in store.carriers():
            print(f"  {c['freq_hz'] / 1e6:10.4f} MHz  cc={c['dominant_cc']}  variant={c['variant']} ({c['variant_confidence']:.2f})"
                  f"  seen {datetime.fromtimestamp(c['first_seen'], tz=timezone.utc):%Y-%m-%d} .. {datetime.fromtimestamp(c['last_seen'], tz=timezone.utc):%Y-%m-%d}")
    return 0


def _not_yet(phase: str):
    def run(args: argparse.Namespace) -> int:
        print(f"this command arrives in Phase {phase}", file=sys.stderr)
        return 1
    return run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mapper", description="Passive DMR network topology mapper")
    p.add_argument("--version", action="version", version=f"mapper {__version__}")
    p.add_argument("--config", help="config file (default: config.local.yaml over config.example.yaml)")
    p.add_argument("--db", help="SQLite path (default: paths.db from config)")
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="parse DSD-FME logs or surveyor output into the database")
    ing.add_argument("path", help="log file, directory of logs, surveyor decode tree, or '-' for stdin")
    ing.add_argument("--freq", type=int, help="carrier frequency in Hz (overrides the filename)")
    ing.add_argument("--start", help="capture start time, ISO 8601 (overrides the filename)")
    ing.add_argument("--source", help="source tag: dsdfme_log (default), collector, import")
    ing.add_argument("--stream", action="store_true", help="treat the input as a live stream (no idempotence check)")
    ing.add_argument("--force", action="store_true", help="re-ingest files already recorded in ingest_runs")
    ing.add_argument("--json", action="store_true", help="print the report as JSON")
    ing.set_defaults(func=cmd_ingest)

    st = sub.add_parser("status", help="database summary")
    st.set_defaults(func=cmd_status)

    for name, phase in (("correlate", 2), ("systems", 2), ("explain", 2), ("export", 2), ("collect", 3), ("serve-mcp", 4)):
        sp = sub.add_parser(name, help=f"(Phase {phase})")
        sp.add_argument("rest", nargs="*")
        sp.set_defaults(func=_not_yet(str(phase)))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
