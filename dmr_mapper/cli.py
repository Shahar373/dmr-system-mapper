"""``mapper`` command line interface."""

from __future__ import annotations

import argparse
import json
import os
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
        holder = None if ds.alive or store.path is None else st.lock_holder(store.path)
        if holder is not None:
            print(f"daemon: STALE: {_stale_daemon_text(holder, ds)}")
        else:
            print(f"daemon: {'alive, mode ' + ds.mode if ds.alive else 'not running'}" + (f"  ({ds.message})" if ds.message else ""))
        for c in st.recent_commands(store, 10):
            if c["status"] in ("pending", "taken"):
                print(f"  command #{c['id']} {c['kind']} from {c['source']}: {c['status']} since"
                      f" {datetime.fromtimestamp(c['taken_at'] or c['created'], tz=timezone.utc).astimezone():%H:%M:%S}")
        for sess in st.sessions(store, 3):
            print(f"  session {sess['id']} {sess['kind']} {sess['status']} budget {sess['budget_s']}s elapsed {sess['summary'].get('elapsed_s', '-')}s")
        for name in ("carriers", "observations", "csbk_events", "rest_events", "identities", "identity_sightings",
                     "links", "evidence", "ingest_runs", "survey_hits", "keyups", "names"):
            print(f"  {name:20s} {store.scalar(f'SELECT COUNT(*) FROM {name}')}")
        print("carriers:")
        for c in store.carriers():
            print(f"  {c['freq_hz'] / 1e6:10.4f} MHz  cc={c['dominant_cc']}  variant={c['variant']} ({c['variant_confidence']:.2f})"
                  f"  seen {datetime.fromtimestamp(c['first_seen'], tz=timezone.utc):%Y-%m-%d} .. {datetime.fromtimestamp(c['last_seen'], tz=timezone.utc):%Y-%m-%d}")
    return 0


def _session_view(sess: dict, tz, progress: dict | None, with_traceback: bool = False) -> dict:
    """One session for `mapper session --json`: the row, its params and the summary digest."""
    import time

    def iso(ts):
        return datetime.fromtimestamp(int(ts), tz=tz).isoformat(timespec="seconds") if ts else None

    elapsed = sess["summary"].get("elapsed_s")
    if elapsed is None and sess["status"] == "running" and sess.get("started"):
        elapsed = int(time.time()) - int(sess["started"])     # a running session's summary has no elapsed_s yet
    out = {"id": sess["id"], "kind": sess["kind"], "status": sess["status"], "requested_by": sess["requested_by"],
           "budget_s": sess["budget_s"], "created": iso(sess.get("created")), "started": iso(sess.get("started")),
           "finished": iso(sess.get("finished")), "elapsed_s": elapsed, "params": sess["params"],
           "summary": _summary_digest(sess["summary"], with_traceback)}
    if progress is not None:
        out["progress"] = progress
    return out


def cmd_session(args: argparse.Namespace) -> int:
    from zoneinfo import ZoneInfo

    from .collector import state as st

    cfg = load_config(args.config)
    if args.id is not None and args.last is not None:
        print("error: give a session id or --last N, not both", file=sys.stderr)
        return 2
    if args.last is not None and args.last < 1:
        print("error: --last needs a positive number", file=sys.stderr)
        return 2
    tz = ZoneInfo(str(cfg.get("timezone") or "UTC"))
    with _open_store(args, cfg) as store:
        found = st.sessions(store, args.last or 1, session_id=args.id)
        if not found:
            print(f"no session #{args.id}" if args.id is not None else "no session has run yet", file=sys.stderr)
            return 1
        ds = st.read_status(store, int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10)))
    views = [_session_view(x, tz, ds.progress if ds.alive and ds.session_id == x["id"] else None, args.traceback)
             for x in found]
    if args.json:
        print(json.dumps(views if args.last is not None else views[0], indent=2))
        return 0
    for i, v in enumerate(views):
        if i:
            print()
        elapsed = v["elapsed_s"]
        el = "-" if elapsed is None else f"{elapsed} s" + (" so far" if v["status"] == "running" else "")
        print(f"session {v['id']}  {v['kind']}  {v['status']}  requested_by {v['requested_by']}  budget {v['budget_s']} s"
              f"  started {v['started'] or '-'}  elapsed {el}")
        if v["params"]:
            print(f"params: {json.dumps(v['params'], sort_keys=True)}")
        if "progress" in v:
            print(f"progress: {_progress_line(v['progress'])}")
        _print_summary(v["summary"], with_traceback=args.traceback)
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
        rep = run(store, cfg, now=_parse_now(args.now), window_days=args.window_days, rebuild=args.rebuild)
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
    from .export.report import systems_report

    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        data, text = systems_report(store, args.min_label)
    print(json.dumps(data, indent=2) if args.json else text)
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
        th = x["thresholds"]
        print(f"  labels: confirmed >= {th['confirmed']:.2f} with explicit or manual evidence, probable >= {th['probable']:.2f},"
              f" possible >= {th['possible']:.2f}; Tier II only: at most {th['tier2_max']:.2f}")
        for e in x["evidence"]:
            print(f"  - {e['type']:34s} raw {e['raw']:.2f} x w {e['weight']:.2f} x decay {e['decay']:.2f} = {e['contribution']:.3f}"
                  f"  support {e['support']}  seen {datetime.fromtimestamp(e['observed_at'], tz=timezone.utc):%Y-%m-%d}  [{e['source']}]")
            if e["details"]:
                print(f"      {json.dumps(e['details'], ensure_ascii=False)[:160]}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import build, to_json, write, write_graphml
    from .export.report import export_all

    cfg = load_config(args.config)
    if args.all:
        with _open_store(args, cfg) as store:
            paths, g = export_all(store, cfg, Path(args.dir) if args.dir else None, args.min_label)
        for p in paths:
            print(f"wrote {p}")
        print(f"{g.number_of_nodes()} nodes, {g.number_of_edges()} edges (>= {args.min_label})")
        return 0
    from .export.coverage import coverage

    with _open_store(args, cfg) as store:
        g = build(store, min_label=args.min_label)
        cov = coverage(store, cfg)
    exports = resolve_path(cfg, "exports")
    if not (args.html or args.graphml or args.json_path):
        args.html = str(exports / "map.html")
    if args.html:
        write(g, args.html, coverage=cov)
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


def cmd_prune(args: argparse.Namespace) -> int:
    from .db.retention import prune

    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        out = prune(store, cfg)
    print("pruned: " + (", ".join(f"{k}={v}" for k, v in out.items()) or "nothing to prune"))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from .db.snapshot import snapshot

    cfg = load_config(args.config)
    keep = args.keep if args.keep is not None else int((cfg.get("archive", {}) or {}).get("keep_local", 3))
    out = Path(args.dir) if args.dir else resolve_path(cfg, "snapshots")
    path, removed = snapshot(_db_path(args, cfg), out, keep, str(cfg.get("timezone") or "UTC"))
    print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")
    for r in removed:
        print(f"removed {r}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    from .correlate import propose

    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        res = propose(store, args.a_freq, args.b_freq, args.kind, args.confidence, args.note or "", cfg)
    print(json.dumps(res))
    return 0


# -- Phase 3: daemon, console, one-shot sessions -------------------------------------------------
def _db_path(args: argparse.Namespace, cfg: dict) -> Path:
    return Path(args.db) if getattr(args, "db", None) else resolve_path(cfg, "db")


def cmd_daemon(args: argparse.Namespace) -> int:
    import logging

    from .collector.daemon import run_daemon

    # systemd's journal adds the time; a terminal run gets it from the format
    fmt = "%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID") else "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stderr)
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


COMMAND_WAIT_S = 30     # how long the CLI waits for the daemon to answer a session command


def _stale_daemon_text(holder: str, status) -> str:
    """A daemon holds the database's lock (so its process is alive) but its heartbeat is stale."""
    import time

    age = f"{int(time.time()) - int(status.heartbeat)} s ago" if status.heartbeat else "never"
    said = f", it last said: {status.message}" if status.message else ""
    return (f"the daemon (pid {holder}) is running but its heartbeat is stale (last one {age}{said}). It is probably stuck;"
            " nothing was asked of it. See `mapper status` and `journalctl -u dmr-mapper`, and"
            " `sudo systemctl restart dmr-mapper` if it does not recover.")


def _daemon_down_text(path: Path, status) -> str:
    from .collector import state as st

    holder = st.lock_holder(path)
    return _stale_daemon_text(holder, status) if holder is not None else "daemon is not running"


def _answer(kind: str, cid: int, c: dict | None) -> bool:
    """Print what became of a session command; True when the daemon answered it (done or failed)."""
    from .collector import state as st

    if c and c["status"] in ("done", "failed"):
        print(f"{kind}: {c['status']}: {c['result']}")
        return True
    print(st.explain_unanswered(kind, cid, c, COMMAND_WAIT_S), file=sys.stderr)
    return False


def _session_command(args: argparse.Namespace, kind: str, params: dict) -> int:
    """Post a command to the running daemon, or run a temporary in-process daemon when none is
    running (so `mapper survey` works on a Pi before the service is installed)."""
    import threading
    import time

    from .collector import state as st
    from .collector.daemon import Daemon

    cfg = load_config(args.config)
    path = _db_path(args, cfg)
    stale_s = int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10))
    if args.minutes:
        params["budget_s"] = int(args.minutes * 60)
    with Store(path) as store:
        status = st.read_status(store, stale_s)
        if status.alive:
            cid = st.post_command(store, "cli", kind, params)
            c = st.wait_for_command(store, cid, timeout_s=COMMAND_WAIT_S)
            if not _answer(kind, cid, c) or c["status"] != "done":
                return 1
            if not args.wait:
                return 0
            return _follow(store, cfg, int(c["result"].split()[1]))
    # No heartbeat. A daemon may still be alive and stuck: then it holds the lock, a temporary daemon
    # could not start, and a command posted now would wait for whoever restarts it.
    holder = st.lock_holder(path)
    if holder is not None:
        print(f"error: {_stale_daemon_text(holder, status)}", file=sys.stderr)
        return 1
    d = Daemon(path, cfg)
    failed: list[BaseException] = []

    def run() -> None:
        try:
            d.run()
        except BaseException as exc:  # noqa: BLE001 - reported below, without a thread traceback
            failed.append(exc)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    with Store(path) as store:
        cid = None
        try:
            deadline = time.monotonic() + 10
            while th.is_alive() and time.monotonic() < deadline:
                s = st.read_status(store, stale_s)
                if s.alive and s.pid == os.getpid():
                    break
                time.sleep(0.1)
            if not th.is_alive():
                print(f"error: the temporary daemon could not start: {failed[0] if failed else 'it stopped'}", file=sys.stderr)
                return 1
            cid = st.post_command(store, "cli", kind, params)
            c = st.wait_for_command(store, cid, timeout_s=COMMAND_WAIT_S)
            if not _answer(kind, cid, c) or c["status"] != "done":
                return 1
            return _follow(store, cfg, int(c["result"].split()[1]))
        except KeyboardInterrupt:
            if cid is not None:
                st.post_command(store, "cli", "stop")
                time.sleep(2)
            return 130
        finally:
            d.stop_event.set()
            th.join(timeout=20)


def _summary_digest(summary: dict, with_traceback: bool = False) -> dict:
    """A session summary as the CLI shows it: everything but the traceback of a failed session.
    The summary holds counts, never radio ids (identities are counted as ``radios``/``talkgroups``),
    so nothing here needs the pseudonyms the MCP layer gives them."""
    return {k: v for k, v in (summary or {}).items() if with_traceback or k != "traceback"}


def _print_summary(summary: dict, limit: int | None = None, with_traceback: bool = False) -> None:
    text = json.dumps(_summary_digest(summary, with_traceback), indent=2)
    print(text[:limit] if limit else text)


def _progress_line(progress: dict | None) -> str:
    """The daemon's progress for the running session, one line (the carrier table left out)."""
    return json.dumps({k: v for k, v in (progress or {}).items() if k != "carriers"}, sort_keys=True)


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
            _print_summary(sess["summary"], limit=4000)
            sm = sess["summary"]
            if sess["status"] != "failed" and (sm.get("then_decode_s") or sm.get("campaign_next")):
                # the one-button flow or a campaign: the daemon queued the next session; follow that one too
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    nxt = next((x for x in st.sessions(store, 5) if x["id"] > session_id
                                and (sm.get("campaign_next") or x["kind"] == "decode")), None)
                    if nxt is not None:
                        if sm.get("campaign_next"):
                            print(f"session {nxt['id']} follows: {sm['campaign_next']}")
                        else:
                            print(f"decode session {nxt['id']} follows ({sm['then_decode_s']} s)")
                        return _follow(store, cfg, int(nxt["id"]))
                    time.sleep(0.5)
                print("the queued session did not start: see `mapper status` and the command log", file=sys.stderr)
                return 1
            if sm.get("campaign_done"):
                print("campaign done: the map is being exported (var/exports/map.html, with its coverage section)")
            return 0 if sess["status"] != "failed" else 1
        if not s.alive:
            print("daemon went away", file=sys.stderr)
            return 1
        line = _progress_line(s.progress)
        if line != last:
            print(line, file=sys.stderr)
            last = line
        time.sleep(1)


def cmd_survey(args: argparse.Namespace) -> int:
    from .collector.survey import check_survey_params, sweep_seconds, sweeps_that_fit

    params: dict = {}
    if args.band:
        params["bands"] = args.band
    if args.dwell is not None:
        params["dwell_s"] = float(args.dwell)
    if args.sweeps is not None:
        params["sweeps"] = int(args.sweeps)
    cfg = load_config(args.config)
    try:
        dwell, sweeps = check_survey_params(params, cfg)
    except ValueError as exc:
        print(f"survey: {exc}", file=sys.stderr)
        return 2
    bands = [b for b in (cfg.get("survey", {}) or {}).get("bands", []) or [] if not args.band or b.get("name") in args.band]
    sdr = cfg.get("sdr", {}) or {}
    one = sweep_seconds(bands, float(sdr.get("sample_rate_hz", 2_000_000)), float(sdr.get("usable_fraction", 0.8)), dwell)
    print(f"survey: {sweeps} sweep(s) of {', '.join(str(b.get('name')) for b in bands) or 'no band'} at {dwell:g} s per step,"
          f" about {one / 60:.1f} min each", file=sys.stderr)
    if args.then_decode and sweeps > 1:
        total = float(args.minutes * 60) if args.minutes else float((cfg.get("daemon", {}) or {}).get("default_budget_min", 60)) * 60
        fit = sweeps_that_fit(sweeps, one, total)
        if fit < sweeps:
            print(f"survey: only about {fit} of the {sweeps} sweeps fit while leaving half of the {total / 60:.0f} min to the decode",
                  file=sys.stderr)
    if args.then_decode:
        params["then_decode"] = True
        if args.minutes:
            params["total_budget_s"] = int(args.minutes * 60)
    return _session_command(args, "start_survey", params)


def cmd_campaign(args: argparse.Namespace) -> int:
    from .collector.campaign import campaign_cfg, new_campaign, one_sweep_s, survey_step
    from .collector.survey import check_survey_params

    cfg = load_config(args.config)
    params: dict = {"watch": not args.no_watch}
    if args.dwell is not None:
        params["dwell_s"] = float(args.dwell)
        try:
            check_survey_params({"dwell_s": params["dwell_s"]}, cfg)
        except ValueError as exc:
            print(f"campaign: {exc}", file=sys.stderr)
            return 2
    if args.minutes:
        camp = new_campaign(int(args.minutes * 60), cfg, dwell_s=params.get("dwell_s"), watch=params["watch"])
        _, first = survey_step(camp, cfg)
        mix = campaign_cfg(cfg)["mix"]
        print(f"campaign: {camp['cycles']} cycle(s) of about {camp['cycle_s'] / 60:.0f} min: {first['sweeps']} sweep(s) of about"
              f" {one_sweep_s(cfg, params.get('dwell_s')) / 60:.1f} min, then decode ({mix['decode']:.0%}) and watch"
              f" ({mix['watch']:.0%})" + ("" if params["watch"] else ", without watch"), file=sys.stderr)
    return _session_command(args, "start_campaign", params)


def cmd_decode(args: argparse.Namespace) -> int:
    params: dict = {}
    if args.freq:
        params["freqs"] = [int(f) for f in args.freq]
    if args.instances:
        params["max_instances"] = int(args.instances)
    return _session_command(args, "start_decode", params)


def _freq_arg(text: str) -> int:
    """A frequency in Hz (469350000) or MHz with a dot (469.35)."""
    t = str(text).strip()
    return int(round(float(t) * 1e6)) if "." in t else int(t)


def cmd_watch(args: argparse.Namespace) -> int:
    params: dict = {}
    if args.freq:
        params["freqs"] = [_freq_arg(f) for f in args.freq]
    if args.center:
        params["center_hz"] = _freq_arg(args.center)
    if args.instances:
        params["max_instances"] = int(args.instances)
    return _session_command(args, "start_watch", params)


def cmd_name(args: argparse.Namespace) -> int:
    from .correlate.names import set_name

    if not args.clear and not args.name:
        print("give a name, or --clear to remove one", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        try:
            res = set_name(store, args.kind, args.target, None if args.clear else " ".join(args.name))
        except (KeyError, ValueError) as exc:
            print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
            return 1
    anchor = f"anchored on {res['anchor']}" if res["anchor"] else "anchored on its carriers " + ", ".join(f"{f / 1e6:.4f}" for f in res["carriers"])
    print(f"{res['kind']} {res['id']}: " + (f"named {res['name']!r} ({anchor})" if res["name"] else "name removed"))
    return 0


def cmd_names(args: argparse.Namespace) -> int:
    from .correlate.names import list_names

    cfg = load_config(args.config)
    with _open_store(args, cfg) as store:
        names = list_names(store)
    if args.json:
        print(json.dumps(names, indent=2, ensure_ascii=False))
        return 0
    if not names:
        print("no names yet: mapper name system|site <id or frequency> <name>")
    for n in names:
        where = f"{n['kind']} {', '.join(map(str, n['now_on']))}" if n["now_on"] else f"no {n['kind']} matches it right now"
        anchor = n["anchor"] or "carriers " + ", ".join(f"{f / 1e6:.4f}" for f in n["carriers"])
        print(f"  {n['kind']:6s} {n['name']!r:30s} {anchor:30s} -> {where}")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    from .collector import state as st

    cfg = load_config(args.config)
    with Store(_db_path(args, cfg)) as store:
        status = st.read_status(store, int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10)))
        if not status.alive:
            print(_daemon_down_text(_db_path(args, cfg), status), file=sys.stderr)
            return 1
        cid = st.post_command(store, "cli", args.kind, {})
        c = st.wait_for_command(store, cid, timeout_s=120)
        if c and c["status"] in ("done", "failed"):
            print(f"{args.kind}: {c['status']}: {c['result']}")
        else:
            print(st.explain_unanswered(args.kind, cid, c, 120), file=sys.stderr)
        return 0 if c and c["status"] == "done" else 1


def _approval_timeout_s(cfg: dict) -> int:
    return int(float((cfg.get("mcp", {}) or {}).get("approval_timeout_min", 10)) * 60)


def cmd_requests(args: argparse.Namespace) -> int:
    from .collector import state as st

    cfg = load_config(args.config)
    with Store(_db_path(args, cfg)) as store:
        waiting = st.awaiting_approval(store, _approval_timeout_s(cfg))
    if not waiting:
        print("no request is waiting for approval")
    for c in waiting:
        when = datetime.fromtimestamp(c["created"], tz=timezone.utc).astimezone().strftime("%H:%M:%S")
        print(f"  #{c['id']}  {when}  from {c['source']}: {st.describe_request(c['kind'], c['params'])}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """approve / refuse a session Claude asked for (the newest waiting one when no id is given)."""
    from .collector import state as st

    cfg = load_config(args.config)
    approve = args.command == "approve"
    with Store(_db_path(args, cfg)) as store:
        timeout = _approval_timeout_s(cfg)
        waiting = st.awaiting_approval(store, timeout)
        cid = args.id or (waiting[-1]["id"] if waiting else None)
        if cid is None:
            print("no request is waiting for approval", file=sys.stderr)
            return 1
        ds = st.read_status(store, int(cfg.get("daemon", {}).get("heartbeat_stale_s", 10)))
        if approve and not ds.alive:
            down = _daemon_down_text(_db_path(args, cfg), ds)
            print(down if down != "daemon is not running" else "the daemon is not running: start it first (sudo systemctl start dmr-mapper)",
                  file=sys.stderr)
            return 1
        try:
            c = st.decide(store, int(cid), approve, "cli", timeout)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc.args[0]}", file=sys.stderr)
            return 1
        params = json.loads(c["params"] or "{}")
        print(f"#{cid} {'approved' if approve else 'refused'}: {st.describe_request(c['kind'], params)}")
        if approve:
            done = st.wait_for_command(store, int(cid), timeout_s=30)
            if done and done["status"] in ("done", "failed"):
                print(f"  {done['status']}: {done['result']}")
            else:
                print("  " + st.explain_unanswered(c["kind"], int(cid), done, 30))
    return 0


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    import logging

    fmt = "%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID") else "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stderr)
    cfg = load_config(args.config)
    try:
        from .mcp.server import serve
    except ImportError as exc:
        print(f"error: the MCP SDK is missing ({exc}); pip install -e '.[mcp]'", file=sys.stderr)
        return 1
    try:
        serve(cfg, _db_path(args, cfg), args.bind, args.port)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


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
    import time

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
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from .ingest.runner import system_tz
    sys_tz, cfg_tz = system_tz(), ZoneInfo(str(cfg.get("timezone") or "UTC"))
    same = _dt.now(sys_tz).utcoffset() == _dt.now(cfg_tz).utcoffset()
    line(same or None, "time zone", f"system {sys_tz}, config {cfg_tz}" + ("" if same else
         f": different. Live decoding is right either way; raw logs from here must be ingested with timezone: {sys_tz}"
         f" (or set this machine's zone: sudo timedatectl set-timezone {cfg_tz})"))
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
    from .collector.gps import open_gps
    g = open_gps(cfg)
    if g.kind == "none":
        line(None, "gps", g.describe())
    else:
        g.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and g.fix() is None and not g.error:
            time.sleep(0.2)
        f = g.fix()
        g.stop()
        line(f is not None or None, "gps", f"{g.describe()}: " + (f"{f.lat:.5f}, {f.lon:.5f} ({f.fix})" if f else (g.error or "no fix within 5 s")))
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
        holder = None if status.alive else st.lock_holder(path)
        line(False if holder is not None else None, "daemon",
             f"alive, mode {status.mode}" if status.alive else (f"stale heartbeat: pid {holder} holds the lock" if holder else "not running"))
    mcfg = cfg.get("mcp", {}) or {}
    if mcfg.get("enabled"):
        try:
            from importlib.metadata import version as _version

            from .mcp.server import check_bind
            check_bind(str(mcfg.get("bind")), bool(mcfg.get("allow_any_bind", False)))
            loop = str(mcfg.get("bind")).startswith("127.") or mcfg.get("bind") == "localhost"
            line(None if loop else True, "MCP server", f"http://{mcfg.get('bind')}:{mcfg.get('port', 8765)}/mcp, mcp {_version('mcp')},"
                 f" approval {mcfg.get('approval', 'ask')}" + (": loopback, the laptop cannot reach it" if loop else ""))
        except ImportError as exc:
            line(False, "MCP server", f"the mcp SDK is missing ({exc}): pip install -e '.[mcp]' (deploy.sh does it)")
        except ValueError as exc:
            line(False, "MCP server", str(exc))
    else:
        line(None, "MCP server", "off (mcp.enabled: false)")
    if shutil.which("systemctl"):
        for unit in ("dmr-mapper.service", "dmr-mapper-snapshot.timer") + (("dmr-mapper-mcp.service",) if mcfg.get("enabled") else ()):
            r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
            state = r.stdout.strip() or "unknown"
            line(state == "active" or None, unit, state + ("" if state == "active" else " (install with deploy/deploy.sh)"))
    snaps = sorted(resolve_path(cfg, "snapshots").glob("mapper-*.db"))
    line(None, "snapshots", f"{len(snaps)}, newest {snaps[-1].name}" if snaps else "none yet")
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

    se = sub.add_parser("session", help="a session's full summary (the latest when no id is given), running or finished")
    se.add_argument("id", nargs="?", type=int, help="session id from `mapper status` (default: the latest)")
    se.add_argument("--last", type=int, metavar="N", help="the N latest sessions, newest first")
    se.add_argument("--json", action="store_true", help="print the sessions as JSON")
    se.add_argument("--traceback", action="store_true", help="include the traceback of a failed session")
    se.set_defaults(func=cmd_session)

    co = sub.add_parser("correlate", help="run the evidence engine and derive sites/systems")
    co.add_argument("--window-days", type=int, help="look back this many days (default: correlator.window_days)")
    co.add_argument("--now", help="pretend it is this ISO time (for replaying old captures)")
    co.add_argument("--rebuild", action="store_true", help="forget the automatic evidence and collect it again (manual evidence and names stay)")
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
    xp.add_argument("--all", action="store_true", help="map.html, map.json, map.graphml and systems.txt into the exports folder")
    xp.add_argument("--dir", help="with --all: write here instead of paths.exports")
    xp.set_defaults(func=cmd_export)

    pu = sub.add_parser("prune", help="delete radio IDs, talkgroups and their events older than retention.identity_days")
    pu.set_defaults(func=cmd_prune)

    sn = sub.add_parser("snapshot", help="consistent copy of the database for the archive (VACUUM INTO), keeps the newest N")
    sn.add_argument("--dir", help="default: paths.snapshots")
    sn.add_argument("--keep", type=int, help="snapshots kept locally (default: archive.keep_local)")
    sn.set_defaults(func=cmd_snapshot)

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
    sv.add_argument("--then-decode", action="store_true", help="decode right after the survey; --minutes is then the total for both")
    sv.add_argument("--dwell", type=float, metavar="S", help="seconds per tuner step for this session only (default survey.dwell_s;"
                    " at most 11 with the default 1 s segments and 9 dB gate)")
    sv.add_argument("--sweeps", type=int, metavar="N", help="sweep the bands N times in this session (default 1); a decode plans"
                    " from every sweep of the last survey.plan_window_minutes, so N short sweeps catch more than one long")
    sv.add_argument("--wait", action="store_true", default=True, help="follow the session until it ends (default)")
    sv.add_argument("--no-wait", dest="wait", action="store_false")
    sv.set_defaults(func=cmd_survey)

    cp = sub.add_parser("campaign", help="survey, decode and watch in cycles at one stop, in one budget (a chain of sessions)")
    cp.add_argument("--minutes", type=float, help="the whole campaign (default daemon.default_budget_min, or the battery one)")
    cp.add_argument("--dwell", type=float, metavar="S", help="seconds per survey step for this campaign (default survey.dwell_s)")
    cp.add_argument("--no-watch", action="store_true", help="survey and decode only")
    cp.add_argument("--wait", action="store_true", default=True, help="follow every step until the campaign ends (default)")
    cp.add_argument("--no-wait", dest="wait", action="store_false")
    cp.set_defaults(func=cmd_campaign)

    dc = sub.add_parser("decode", help="start a decode session (recent survey hits, or the given frequencies)")
    dc.add_argument("freq", nargs="*", help="frequencies in Hz (default: plan from the recent sweeps, survey.plan_window_minutes)")
    dc.add_argument("--minutes", type=float, help="session budget")
    dc.add_argument("--instances", type=int, help="DSD-FME processes at once (default from power state)")
    dc.add_argument("--wait", action="store_true", default=True)
    dc.add_argument("--no-wait", dest="wait", action="store_false")
    dc.set_defaults(func=cmd_decode)

    wa = sub.add_parser("watch", help="watch one slice: control channels decoded throughout, traffic channels when they key up")
    wa.add_argument("freq", nargs="*", help="carriers the slice must hold, Hz or MHz (default: the Tier III network with the most carriers)")
    wa.add_argument("--center", help="tune here instead (Hz or MHz)")
    wa.add_argument("--minutes", type=float, help="session budget")
    wa.add_argument("--instances", type=int, help="DSD-FME processes at once (default from power state: watch.max_instances / power.battery_watch_instances)")
    wa.add_argument("--wait", action="store_true", default=True)
    wa.add_argument("--no-wait", dest="wait", action="store_false")
    wa.set_defaults(func=cmd_watch)

    na = sub.add_parser("name", help="give a system or site a name (kept across correlator runs)")
    na.add_argument("kind", choices=["system", "site"])
    na.add_argument("target", help="its id from `mapper systems`, or the frequency of one of its carriers (Hz or MHz)")
    na.add_argument("name", nargs="*", help="the name (several words are joined)")
    na.add_argument("--clear", action="store_true", help="remove the name instead")
    na.set_defaults(func=cmd_name)

    ns = sub.add_parser("names", help="list the names given to systems and sites")
    ns.add_argument("--json", action="store_true")
    ns.set_defaults(func=cmd_names)

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

    sp = sub.add_parser("serve-mcp", help="the MCP server for Claude Code (HTTP on mcp.bind:mcp.port; systemd runs it)")
    sp.add_argument("--bind", help="listen address (default mcp.bind: the Pi's Tailscale address)")
    sp.add_argument("--port", type=int, help="default mcp.port (8765)")
    sp.set_defaults(func=cmd_serve_mcp)

    rq = sub.add_parser("requests", help="sessions Claude asked for that wait for your approval")
    rq.set_defaults(func=cmd_requests)
    for verb, help_ in (("approve", "start a session Claude asked for"), ("refuse", "refuse a session Claude asked for")):
        dp = sub.add_parser(verb, help=help_ + " (the newest waiting request when no id is given)")
        dp.add_argument("id", nargs="?", type=int, help="request id from `mapper requests`")
        dp.set_defaults(func=cmd_decide)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:      # `mapper status | head`
        return 0


if __name__ == "__main__":
    sys.exit(main())
