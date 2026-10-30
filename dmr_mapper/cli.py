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
    from .collector import state as st

    with _open_store(args, cfg) as store:
        print(f"db: {store.path}  schema v{store.schema_version()}")
        ds = st.read_status(store, int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10)))
        print(f"daemon: {'alive, mode ' + ds.mode if ds.alive else 'not running'}" + (f"  ({ds.message})" if ds.message else ""))
        for sess in st.sessions(store, 3):
            print(f"  session {sess['id']} {sess['kind']} {sess['status']} budget {sess['budget_s']}s elapsed {sess['summary'].get('elapsed_s', '-')}s")
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


# -- Phase 3: daemon, console, one-shot sessions -------------------------------------------------
def _db_path(args: argparse.Namespace, cfg: dict) -> Path:
    return Path(args.db) if getattr(args, "db", None) else resolve_path(cfg, "db")


def cmd_daemon(args: argparse.Namespace) -> int:
    from .collector.daemon import run_daemon

    cfg = load_config(args.config)
    path = _db_path(args, cfg)
    print(f"daemon on {path} (config {cfg.get('_source')}); Ctrl-C or SIGTERM to stop", file=sys.stderr)
    try:
        return run_daemon(path, cfg)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui.app import run_tui

    cfg = load_config(args.config)
    run_tui(_db_path(args, cfg), cfg)
    return 0


def _session_command(args: argparse.Namespace, kind: str, params: dict) -> int:
    """Post a command to the running daemon, or run a temporary in-process daemon when none is
    running (so `mapper survey` works on a Pi before the service is installed)."""
    import threading
    import time

    from .collector import state as st
    from .collector.daemon import Daemon

    cfg = load_config(args.config)
    path = _db_path(args, cfg)
    if args.minutes:
        params["budget_s"] = int(args.minutes * 60)
    with Store(path) as store:
        status = st.read_status(store, int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10)))
        if status.alive:
            cid = st.post_command(store, "cli", kind, params)
            c = st.wait_for_command(store, cid, timeout_s=30)
            print(f"{kind}: {c['status']}: {c['result']}" if c else f"{kind}: posted as command {cid}")
            if not args.wait or not c or c["status"] != "done":
                return 0 if c and c["status"] == "done" else 1
            return _follow(store, cfg, int(c["result"].split()[1]))
    d = Daemon(path, cfg)
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    with Store(path) as store:
        try:
            time.sleep(0.5)
            cid = st.post_command(store, "cli", kind, params)
            c = st.wait_for_command(store, cid, timeout_s=30)
            print(f"{kind}: {c['status']}: {c['result']}" if c else "no answer from the temporary daemon")
            if not c or c["status"] != "done":
                return 1
            return _follow(store, cfg, int(c["result"].split()[1]))
        except KeyboardInterrupt:
            st.post_command(store, "cli", "stop")
            time.sleep(2)
            return 130
        finally:
            d.stop_event.set()
            th.join(timeout=20)


def _follow(store: Store, cfg: dict, session_id: int) -> int:
    """Print progress until the session ends, then its summary."""
    import time

    from .collector import state as st

    last = ""
    while True:
        s = st.read_status(store, int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10)))
        sess = next((x for x in st.sessions(store, 5) if x["id"] == session_id), None)
        if sess and sess["status"] in ("done", "stopped", "failed"):
            print(f"session {session_id} {sess['status']} after {sess['summary'].get('elapsed_s', 0)} s")
            print(json.dumps(sess["summary"], indent=2)[:4000])
            return 0 if sess["status"] != "failed" else 1
        if not s.alive:
            print("daemon went away", file=sys.stderr)
            return 1
        pr = s.progress or {}
        line = json.dumps({k: v for k, v in pr.items() if k != "carriers"}, sort_keys=True)
        if line != last:
            print(line, file=sys.stderr)
            last = line
        time.sleep(1)


def cmd_survey(args: argparse.Namespace) -> int:
    params: dict = {}
    if args.band:
        params["bands"] = args.band
    if args.then_decode:
        params["then_decode"] = True
    return _session_command(args, "start_survey", params)


def cmd_decode(args: argparse.Namespace) -> int:
    params: dict = {}
    if args.freq:
        params["freqs"] = [int(f) for f in args.freq]
    if args.instances:
        params["max_instances"] = int(args.instances)
    return _session_command(args, "start_decode", params)


def cmd_control(args: argparse.Namespace) -> int:
    from .collector import state as st

    cfg = load_config(args.config)
    with Store(_db_path(args, cfg)) as store:
        status = st.read_status(store, int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10)))
        if not status.alive:
            print("daemon is not running", file=sys.stderr)
            return 1
        cid = st.post_command(store, "cli", args.kind, {})
        c = st.wait_for_command(store, cid, timeout_s=120)
        print(f"{args.kind}: {c['status']}: {c['result']}" if c else "no answer")
        return 0 if c and c["status"] == "done" else 1


def cmd_capture_iq(args: argparse.Namespace) -> int:
    from .collector.sdr import capture_iq, make_backend

    cfg = load_config(args.config)
    backend = make_backend(cfg)
    status = backend.probe()
    if status.status != "present":
        print(f"error: {status.short()}", file=sys.stderr)
        return 1
    src = backend.open()
    try:
        n = capture_iq(src, args.center, args.seconds, Path(args.out))
    finally:
        src.close()
    print(f"wrote {n} samples ({n / src.sample_rate:.1f} s, cf32, {src.sample_rate / 1e6:.2f} MS/s, centre {args.center / 1e6:.4f} MHz) to {args.out}")
    print("replay it with sdr.source: file and sdr.replay.{path, center_hz} in config.local.yaml")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import platform
    import shutil
    import subprocess

    from .collector import state as st
    from .collector.decoder import find_dsd_fme
    from .collector.power import read_power
    from .collector.sdr import make_backend

    cfg = load_config(args.config)
    ok_all = True

    def line(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal ok_all
        mark = "ok " if ok else ("-- " if ok is None else "BAD")
        if ok is False:
            ok_all = False
        print(f"[{mark}] {what}{': ' + detail if detail else ''}")

    line(True, "python", f"{platform.python_version()} on {platform.machine()} {platform.system()}")
    line(True, "config", str(cfg.get("_source")))
    for mod, why in (("numpy", "collector"), ("scipy", "collector"), ("textual", "tui"), ("networkx", "correlator"), ("yaml", "config")):
        try:
            m = __import__(mod)
            line(True, mod, getattr(m, "__version__", ""))
        except ImportError:
            line(False, mod, f"missing (needed by {why}); pip install -e '.[collector,tui]'")
    try:
        import SoapySDR  # type: ignore[import-not-found]
        line(True, "SoapySDR python bindings", str(SoapySDR.getAPIVersion()))
    except ImportError:
        line(cfg.get("sdr", {}).get("source") == "file" or None, "SoapySDR python bindings", "not importable: apt install python3-soapysdr (or run inside the surveyor's venv)")
    exe = find_dsd_fme(cfg)
    if exe:
        try:
            out = subprocess.run([exe, "-h"], capture_output=True, text=True, timeout=5)
            text = (out.stdout + out.stderr).splitlines()
            ver = next((l.strip() for l in text if l.strip().lower().startswith("build version")), "")
        except (OSError, subprocess.SubprocessError) as exc:
            ver = f"found but failed to run: {exc}"
        line(True, "dsd-fme", f"{exe} {ver}".strip())
    else:
        line(False, "dsd-fme", f"not found ({cfg.get('decode', {}).get('dsd_fme_bin')}); set decode.dsd_fme_bin")
    try:
        status = make_backend(cfg).probe()
        line(status.status == "present", "receiver", status.short() + (f" [{status.detail}]" if status.detail and status.detail not in status.short() else ""))
    except Exception as exc:  # noqa: BLE001
        line(False, "receiver", str(exc))
    ps = read_power(cfg)
    line(None if ps.source == "unknown" else True, "power", f"{ps.short()} (source: {ps.source})")
    pfile = cfg.get("power", {}).get("state_file")
    if pfile:
        line(Path(pfile).exists(), "power state file", pfile)
    line(shutil.which("vcgencmd") is not None or None, "vcgencmd", "present" if shutil.which("vcgencmd") else "absent (not a Pi?)")
    path = _db_path(args, cfg)
    with Store(path) as store:
        size = path.stat().st_size if path.exists() else 0
        line(True, "database", f"{path} schema v{store.schema_version()} {size / 1e6:.1f} MB")
        status = st.read_status(store, int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10)))
        line(None, "daemon", f"{'alive' if status.alive else 'not running'}" + (f", mode {status.mode}" if status.alive else ""))
    bands = cfg.get("survey", {}).get("bands", [])
    line(bool(bands), "survey bands", ", ".join(f"{b.get('name')} {b.get('start_hz', 0) / 1e6:.1f}-{b.get('stop_hz', 0) / 1e6:.1f} MHz" for b in bands))
    return 0 if ok_all else 1


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

    dm = sub.add_parser("daemon", help="run the collector daemon in the foreground (systemd runs this)")
    dm.set_defaults(func=cmd_daemon)

    tu = sub.add_parser("tui", help="operator console (Textual)")
    tu.set_defaults(func=cmd_tui)

    sv = sub.add_parser("survey", help="start a band survey session (through the daemon, or a temporary one)")
    sv.add_argument("--band", action="append", help="band name from config (repeatable; default: all)")
    sv.add_argument("--minutes", type=float, help="session budget")
    sv.add_argument("--then-decode", action="store_true", help="queue a decode session when the survey ends")
    sv.add_argument("--wait", action="store_true", default=True, help="follow the session until it ends (default)")
    sv.add_argument("--no-wait", dest="wait", action="store_false")
    sv.set_defaults(func=cmd_survey)

    dc = sub.add_parser("decode", help="start a decode session (latest survey hits, or the given frequencies)")
    dc.add_argument("freq", nargs="*", help="frequencies in Hz (default: plan from the latest survey)")
    dc.add_argument("--minutes", type=float, help="session budget")
    dc.add_argument("--instances", type=int, help="DSD-FME processes at once (default from power state)")
    dc.add_argument("--wait", action="store_true", default=True)
    dc.add_argument("--no-wait", dest="wait", action="store_false")
    dc.set_defaults(func=cmd_decode)

    for kind, help_ in (("stop", "stop the running session"), ("correlate-now", "ask the daemon to run the correlator")):
        cp = sub.add_parser(kind, help=help_)
        cp.set_defaults(func=cmd_control, kind=kind.replace("-now", ""))

    ci = sub.add_parser("capture-iq", help="record raw IQ for replay (development tool; never done by the collector)")
    ci.add_argument("--center", type=int, required=True, help="centre frequency in Hz")
    ci.add_argument("--seconds", type=float, default=11.0)
    ci.add_argument("--out", required=True, help="output file (cf32)")
    ci.set_defaults(func=cmd_capture_iq)

    dr = sub.add_parser("doctor", help="check this machine: python packages, SoapySDR, dsd-fme, receiver, power, database")
    dr.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("serve-mcp", help="(Phase 4)")
    sp.add_argument("rest", nargs="*")
    sp.set_defaults(func=_not_yet("4"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:      # `mapper status | head`
        return 0


if __name__ == "__main__":
    sys.exit(main())
