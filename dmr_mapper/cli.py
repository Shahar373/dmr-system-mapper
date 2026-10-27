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
    from .ingest import ingest_path, ingest_stream, parse_log_filename
    from .ingest.report import format_report
    from .ingest.runner import parse_start

    cfg = load_config(args.config)
    try:
        start = parse_start(args.start)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with _open_store(args, cfg) as store:
        if args.path == "-" or args.stream:
            freq = args.freq
            if args.path != "-":
                name_freq, name_start = parse_log_filename(args.path)
                freq = freq or name_freq
                start = start or name_start
            if not freq:
                print("error: --freq is required when ingesting a stream", file=sys.stderr)
                return 2
            if args.path == "-":
                results = [ingest_stream(store, sys.stdin, cfg, freq, args.source or "collector", start)]
            else:
                with Path(args.path).open("r", encoding="utf-8", errors="replace") as fh:
                    results = [ingest_stream(store, fh, cfg, freq, args.source or "collector", start)]
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


def _parse_now(text: str | None) -> int | None:
    if not text:
        return None
    from .ingest.runner import parse_start
    dt = parse_start(text)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def cmd_correlate(args: argparse.Namespace) -> int:
    from .correlate import run

    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        rep = run(store, cfg, now=_parse_now(args.now), window_days=args.window_days)
    if args.json:
        print(json.dumps(rep.to_dict(), indent=2))
        return 0
    print(f"correlated {rep.carriers} carriers over the last {rep.window_days} days")
    collected = ", ".join(f"{k}={v}" for k, v in rep.evidence_collected.items() if v)
    print(f"evidence collected: {collected or 'none'}")
    print(f"links scored: {rep.links_scored}  " + "  ".join(f"{k}={v}" for k, v in sorted(rep.links_by_label.items())))
    print(f"sites: {rep.sites}  systems: {rep.systems}")
    return 0


def cmd_systems(args: argparse.Namespace) -> int:
    from .correlate.model import LABELS
    from .export.graph import LABEL_RANK

    cfg = load_config(args.config)
    max_rank = LABEL_RANK.get(args.min_label, 2)
    with _open_store(args, cfg) as store:
        systems = [dict(r) for r in store.rows("SELECT * FROM systems ORDER BY id")]
        sites = [dict(r) for r in store.rows("SELECT * FROM sites ORDER BY system_id, directly_observed DESC, id")]
        members = [dict(r) for r in store.rows(
            "SELECT cs.site_id, cs.lcn, cs.confidence, c.id AS carrier_id, c.freq_hz, c.variant, c.dominant_cc, c.last_seen"
            " FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id ORDER BY c.freq_hz")]
        links = [dict(r) for r in store.rows(
            "SELECT l.*, a.freq_hz AS a_freq, b.freq_hz AS b_freq FROM links l JOIN carriers a ON a.id = l.a_carrier_id"
            " JOIN carriers b ON b.id = l.b_carrier_id ORDER BY l.score DESC")]
    links = [l for l in links if LABEL_RANK.get(l["label"], 3) <= max_rank]
    if args.json:
        print(json.dumps({"systems": systems, "sites": sites, "carriers": members, "links": links}, indent=2))
        return 0
    if not systems:
        print("no systems yet: run `mapper correlate` after ingesting")
        return 0
    for sy in systems:
        print(f"system {sy['id']}: {sy['label']}  [{sy['variant']}]" + (f"  sys={sy['sys_code']}" if sy["sys_code"] else "")
              + (f"  net={sy['net_id']}" if sy["net_id"] is not None else ""))
        for si in [x for x in sites if x["system_id"] == sy["id"]]:
            flag = "" if si["directly_observed"] else "  (announced only)"
            print(f"  site {si['id']}: {si['label']}{flag}")
            for m in [x for x in members if x["site_id"] == si["id"]]:
                lcn = f" LCN {m['lcn']}" if m["lcn"] else ""
                print(f"    {m['freq_hz'] / 1e6:10.4f} MHz  {m['variant']:8s} cc={m['dominant_cc']}{lcn}  last {datetime.fromtimestamp(m['last_seen'], tz=timezone.utc):%Y-%m-%d}")
    print(f"links (>= {args.min_label}): {len(links)}")
    for l in links:
        t2 = "  Tier II only" if l["tier2_only"] else ""
        print(f"  #{l['id']:<4d} {l['a_freq'] / 1e6:.4f} - {l['b_freq'] / 1e6:.4f}  {l['kind']:14s} {l['label']:9s} {l['score']:.2f}{t2}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from .correlate import explain, find_link

    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        if len(args.target) == 1 and args.target[0].isdigit():
            ids = [int(args.target[0])]
        elif len(args.target) == 2:
            ids = find_link(store, int(args.target[0]), int(args.target[1]))
            if not ids:
                print("no link between those carriers", file=sys.stderr)
                return 1
        else:
            print("usage: mapper explain <link-id> | <freqA_hz> <freqB_hz>", file=sys.stderr)
            return 2
        out = [explain(store, i, cfg, now=_parse_now(args.now)) for i in ids]
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    for x in out:
        print(f"link #{x['link_id']}: {x['a_freq_hz'] / 1e6:.4f} MHz - {x['b_freq_hz'] / 1e6:.4f} MHz  {x['kind']}")
        print(f"  score {x['score']:.3f} -> {x['label']}" + ("  (Tier II only, capped)" if x["tier2_only"] else "")
              + f"   noisy-OR {x['positive']:.3f} x penalty {x['penalty']:.2f} ({x['contradictions']} contradictions)")
        for e in x["evidence"]:
            print(f"  - {e['type']:34s} raw {e['raw']:.2f} x w {e['weight']:.2f} x decay {e['decay']:.2f} = {e['contribution']:.3f}"
                  f"  support {e['support']}  seen {datetime.fromtimestamp(e['observed_at'], tz=timezone.utc):%Y-%m-%d}  [{e['source']}]")
            if e["details"]:
                print(f"      {json.dumps(e['details'], ensure_ascii=False)[:160]}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import build, to_json, write, write_graphml

    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        g = build(store, min_label=args.min_label)
    exports = resolve_path(cfg, "exports")
    if not (args.html or args.graphml or args.json_path):
        args.html = str(exports / "map.html")
    if args.html:
        write(g, args.html)
        print(f"wrote {args.html}")
    if args.graphml:
        write_graphml(g, args.graphml)
        print(f"wrote {args.graphml}")
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(json.dumps(to_json(g), indent=2), encoding="utf-8")
        print(f"wrote {args.json_path}")
    print(f"{g.number_of_nodes()} nodes, {g.number_of_edges()} edges (>= {args.min_label})")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    from .correlate import propose

    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        res = propose(store, args.a_freq, args.b_freq, args.kind, args.confidence, args.note or "", cfg)
    print(json.dumps(res))
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

    co = sub.add_parser("correlate", help="run the evidence engine and derive sites/systems")
    co.add_argument("--window-days", type=int, help="look back this many days (default: correlator.window_days)")
    co.add_argument("--now", help="pretend it is this ISO time (for replaying old captures)")
    co.add_argument("--json", action="store_true")
    co.set_defaults(func=cmd_correlate)

    sy = sub.add_parser("systems", help="list systems, sites, carriers and links")
    sy.add_argument("--min-label", default="possible", choices=["confirmed", "probable", "possible", "weak"])
    sy.add_argument("--json", action="store_true")
    sy.set_defaults(func=cmd_systems)

    ex = sub.add_parser("explain", help="show the evidence behind a link")
    ex.add_argument("target", nargs="+", help="link id, or two frequencies in Hz")
    ex.add_argument("--now", help="evaluate decay at this ISO time")
    ex.add_argument("--json", action="store_true")
    ex.set_defaults(func=cmd_explain)

    xp = sub.add_parser("export", help="write the network graph (static HTML by default)")
    xp.add_argument("--html", help="output HTML path (default: paths.exports/map.html)")
    xp.add_argument("--graphml", help="also write GraphML")
    xp.add_argument("--json-path", help="also write the graph as JSON")
    xp.add_argument("--min-label", default="possible", choices=["confirmed", "probable", "possible", "weak"])
    xp.set_defaults(func=cmd_export)

    pr = sub.add_parser("propose", help="record manual evidence for a link between two carriers")
    pr.add_argument("a_freq", type=int)
    pr.add_argument("b_freq", type=int)
    pr.add_argument("kind", choices=["same_site", "same_system", "neighbor_site"])
    pr.add_argument("confidence", type=float)
    pr.add_argument("--note", default="")
    pr.set_defaults(func=cmd_propose)

    for name, phase in (("collect", 3), ("serve-mcp", 4)):
        sp = sub.add_parser(name, help=f"(Phase {phase})")
        sp.add_argument("rest", nargs="*")
        sp.set_defaults(func=_not_yet(str(phase)))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
