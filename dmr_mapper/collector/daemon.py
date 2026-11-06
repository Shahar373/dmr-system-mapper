"""The collector daemon: always running, idle until asked.

* holds no receiver while idle; before a session it probes the receiver and refuses politely when
  it is absent or held by another program;
* a session (survey or decode) runs in a worker thread with a time budget and ends by itself;
* the main loop keeps a heartbeat, serves the command queue, watches the power state and stops a
  session on undervoltage;
* it is the single writer of decoded data (one ``Store`` per thread; SQLite WAL in between);
* once a session has loaded the SDR driver into the process, it restarts itself (``exec``, same pid)
  after ``daemon.release_driver_idle_s`` of idleness: SoapySDRPlay3 keeps the SDRplay API open until
  the process exits, and the API's threads cost most of a core on the Pi while nothing is decoded.

Clients never call into this module; they post rows to ``commands`` (see ``state.py``).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

from ..config import band_for_range
from ..db import Store
from . import state as st
from .bridge import PcmServer
from .channelizer import AUDIO_RATE, Channelizer
from .decoder import DecoderProcess, find_dsd_fme
from . import campaign as camp_mod
from .gps import NoGps, open_gps
from .ingest import CarrierIngest
from .power import PowerState, budget_for, read_power
from .scheduler import Slice, plan, targets_from_store
from .sdr import Backend, SdrStatus, guess_holder, make_backend
from .survey import check_survey_params, plan_radius_km, plan_window_s, recent_hits, record, sweep, sweep_seconds
from .watch import InstancePlanner, KeyupDetector, ListenLog, plan_watch, watch_cfg, watch_instances


log = logging.getLogger("dmr_mapper.daemon")


class SessionStop(Exception):
    """The operator (or undervoltage, or the daemon shutting down) ended the session early."""


class BudgetDone(SessionStop):
    """The session used its time budget: a normal end."""


class Daemon:
    def __init__(self, db_path: str | Path, cfg: dict[str, Any], backend: Backend | None = None, source_tag: str = "daemon"):
        self.db_path = Path(db_path)
        self.cfg = cfg
        self.backend = backend or make_backend(cfg)
        dcfg = cfg.get("daemon", {}) or {}
        self.tick_s = float(dcfg.get("tick_s", 1))
        self.probe_interval_s = float(dcfg.get("sdr_probe_interval_s", 30))
        self.full_probe_interval_s = float(dcfg.get("sdr_full_probe_interval_s", 600))
        self.release_driver_idle_s = float(dcfg.get("release_driver_idle_s", 60))
        self.restart_requested = False           # run() returned to be exec'd again (see run_daemon)
        self._last_busy = time.monotonic()
        self._last_full_probe = float("-inf")
        self.pid = os.getpid()
        self.started = int(time.time())
        self.stop_event = threading.Event()
        self.session_stop = threading.Event()
        self.session_thread: threading.Thread | None = None
        self.session_id: int | None = None
        self.session_kind: str | None = None
        self.mode = "idle"
        self.sdr = SdrStatus("unknown")
        self.power = PowerState()
        self.progress: dict[str, Any] = {}
        self.message: str | None = None
        self._lock = threading.Lock()
        self._last_probe = 0.0
        self._last_power = 0.0
        self._store: Store | None = None
        self.gps: Any = NoGps("no session running")
        self._closing = threading.Event()        # the session thread is past its work, writing up
        self._stop_reason: str | None = None
        self._job: threading.Thread | None = None   # correlate / export, off the main loop
        self._last_checkpoint = 0.0

    # -- main loop ---------------------------------------------------------------------------------
    def _acquire_lock(self) -> None:
        """One daemon per database: an exclusive flock on ``<db>.daemon.lock``. The kernel drops it
        when the process dies, so a crash never leaves a stale lock behind."""
        import fcntl

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(str(self.db_path) + ".daemon.lock", "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "?"
            fh.close()
            raise RuntimeError(f"another daemon (pid {holder}) is already running on {self.db_path}")
        fh.seek(0)
        fh.truncate()
        fh.write(str(self.pid))
        fh.flush()
        self._lock_fh = fh

    def _release_lock(self) -> None:
        fh = getattr(self, "_lock_fh", None)
        if fh is not None:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
            self._lock_fh = None

    def run(self) -> int:
        self._acquire_lock()
        try:
            self._store = Store(self.db_path)
        except Exception:
            self._release_lock()
            raise
        self._install_signals()
        self.message = "started"
        log.info("daemon started: pid %s, database %s, receiver backend %s", self.pid, self.db_path, type(self.backend).__name__)
        try:
            while not self.stop_event.is_set():
                try:
                    self._tick()
                except sqlite3.OperationalError as exc:
                    # a transient lock (a client writing a command at the wrong moment): try again next tick
                    self.message = f"database busy: {exc}"
                    self._store.rollback()
                self.stop_event.wait(self.tick_s)
            if self.session_thread is not None and self.session_thread.is_alive():
                self.session_stop.set()
                self.session_thread.join(timeout=15)
            if self._job is not None and self._job.is_alive():
                self._job.join(timeout=30)
            if self.restart_requested:
                # not offline: the same pid is back within a few seconds, inside heartbeat_stale_s
                st.write_heartbeat(self._store, self.pid, self.started, "idle", None, self.sdr.to_dict(), self.power.to_dict(),
                                   {}, self.message)
                log.info("restarting to release the SDR driver")
            else:
                st.mark_offline(self._store, self.pid)
                log.info("daemon stopped")
        finally:
            self._store.close()
            self._release_lock()
        return 0

    def _install_signals(self) -> None:
        def _stop(signum: int, frame: Any) -> None:
            self.restart_requested = False     # systemd's stop wins over a pending self-restart
            self.stop_event.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _stop)
            except ValueError:
                pass   # not in the main thread (tests): the caller sets stop_event

    def _tick(self) -> None:
        assert self._store is not None
        now = time.monotonic()
        if now - self._last_power >= 10:
            before = self.power.short() if self.power.checked else None
            self.power = read_power(self.cfg)
            self._last_power = now
            if before != self.power.short():
                log.info("power: %s (source %s)", self.power.short(), self.power.source)
            if self.session_thread is not None and self.session_thread.is_alive() and self.power.undervolt_now \
                    and (self.cfg.get("power", {}) or {}).get("stop_on_undervoltage", True):
                self.message = "stopping session: undervoltage reported by the firmware"
                self._stop_reason = "undervoltage"
                self.session_stop.set()
        if self.session_thread is None or not self.session_thread.is_alive():
            if self.session_thread is not None:
                self.session_thread = None
                self.session_id = None
                self.mode = "idle"
                self.progress = {}
            if now - self._last_probe >= self.probe_interval_s:
                self._idle_probe(now)
                self._last_probe = now
        else:
            self._last_busy = now
        if self._job is not None and self._job.is_alive():
            self._last_busy = now
        for _ in range(5):
            cmd = st.take_command(self._store)
            if cmd is None:
                break
            try:
                res = self._handle(cmd)
            except Exception as exc:  # noqa: BLE001 - a bad command must not kill the daemon
                res = (False, f"{type(exc).__name__}: {exc}")
                log.exception("command %s failed", cmd["kind"])
            self._last_busy = now
            if res is None:
                continue                      # a worker finishes it
            ok, result = res
            (log.info if ok else log.warning)("command %s from %s: %s", cmd["kind"], cmd.get("source"), result)
            st.finish_command(self._store, int(cmd["id"]), ok, result)
        if now - self._last_checkpoint >= 30:
            # the session threads leave checkpoints to this loop (see _session_main)
            self._store.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._last_checkpoint = now
        with self._lock:
            progress = dict(self.progress)
        st.write_heartbeat(self._store, self.pid, self.started, self.mode, self.session_id, self.sdr.to_dict(), self.power.to_dict(),
                           progress, self.message)
        if self._should_release_driver(now):
            self.restart_requested = True
            self.message = "restarting to release the SDR driver (idle)"
            self.stop_event.set()

    def _should_release_driver(self, now: float) -> bool:
        """Idle for ``release_driver_idle_s`` with the SDR driver loaded and nothing queued."""
        if self.release_driver_idle_s <= 0 or not getattr(self.backend, "driver_loaded", False):
            return False
        if now - self._last_busy < self.release_driver_idle_s:
            return False
        if (self.session_thread is not None and self.session_thread.is_alive()) or (self._job is not None and self._job.is_alive()):
            return False
        assert self._store is not None
        return self._store.conn.execute("SELECT 1 FROM commands WHERE status = 'pending' LIMIT 1").fetchone() is None

    def _idle_probe(self, now: float) -> None:
        """While idle, only look at whether the receiver is plugged in (no driver call): asking the
        SDRplay driver every few seconds is load on a shared receiver and a syslog line each time.
        The driver itself is asked every ``sdr_full_probe_interval_s``, when the receiver is
        plugged in again, and always before a session."""
        light = getattr(self.backend, "connected", None)
        plugged = light() if callable(light) else None
        if plugged is None or now - self._last_full_probe >= self.full_probe_interval_s or self.sdr.status in ("unknown", "error"):
            self.sdr = self._probe()
            self._last_full_probe = now
        elif not plugged:
            if self.sdr.status != "absent":
                log.info("receiver: unplugged")
            self.sdr = SdrStatus("absent", self.sdr.driver, self.sdr.serial, None, None, int(time.time()))
        elif self.sdr.status == "absent":
            self.sdr = self._probe()          # plugged in again: see what the driver says
            self._last_full_probe = now
        else:
            self.sdr.checked = int(time.time())

    def _probe(self) -> SdrStatus:
        try:
            status = self.backend.probe()
        except Exception as exc:  # noqa: BLE001
            status = SdrStatus("error", None, None, None, str(exc)[:200], int(time.time()))
        if status.status != self.sdr.status or status.holder != self.sdr.holder:
            log.info("receiver: %s%s", status.short(), f" [{status.detail}]" if status.detail else "")
        return status

    # -- commands ----------------------------------------------------------------------------------
    def _handle(self, cmd: dict[str, Any]) -> tuple[bool, str] | None:
        assert self._store is not None
        kind, params = cmd["kind"], cmd.get("params") or {}
        if kind in ("start_survey", "start_decode", "start_watch", "start_campaign"):
            mcp_timeout = int(float((self.cfg.get("mcp", {}) or {}).get("approval_timeout_min", 10)) * 60) + 120
            if cmd.get("source") == "mcp" and time.time() - int(cmd.get("created") or 0) > mcp_timeout:
                # asked for by Claude long ago (the daemon was down): the place and the question have moved on
                return False, "request too old; ask again"
            if self.session_thread is not None and self.session_thread.is_alive() and self._closing.is_set():
                # the previous session is only writing its summary (the one-button flow queues the
                # decode from there): wait for it instead of refusing
                self.session_thread.join(timeout=30)
            if self.session_thread is not None and self.session_thread.is_alive():
                return False, f"a {self.session_kind} session is already running (#{self.session_id}); stop it first"
            if kind in ("start_decode", "start_watch", "start_campaign") and find_dsd_fme(self.cfg) is None:
                return False, "dsd-fme not found (decode.dsd_fme_bin)"
            if kind == "start_campaign":
                # a campaign is a chain of ordinary sessions: it starts as the survey of its first cycle
                try:
                    if params.get("dwell_s") is not None:
                        check_survey_params({"dwell_s": params["dwell_s"]}, self.cfg)
                    first_budget = int(params.get("budget_s") or budget_for(read_power(self.cfg), self.cfg)[1])
                    camp = camp_mod.new_campaign(first_budget, self.cfg, dwell_s=params.get("dwell_s"),
                                                 watch=bool(params.get("watch", True)))
                except (TypeError, ValueError) as exc:
                    return False, str(exc)
                kind, params = camp_mod.survey_step(camp, self.cfg)
            if kind == "start_survey":
                try:      # a dwell the hit test cannot honour is a refusal, before the receiver is touched
                    check_survey_params(params, self.cfg)
                except (TypeError, ValueError) as exc:
                    return False, str(exc)
            self.sdr = self._probe()
            self._last_probe = self._last_full_probe = time.monotonic()
            if self.sdr.status != "present":
                return False, self.sdr.short() + (f" ({self.sdr.detail})" if self.sdr.detail and self.sdr.detail not in self.sdr.short() else "")
            # Open the receiver here, before reporting "started": a refusal from the driver then
            # reaches the operator as the command's result, with the driver's own words.
            try:
                source = self.backend.open()
            except Exception as exc:  # noqa: BLE001
                holder = self.sdr.holder or guess_holder(self.cfg)
                self.sdr = SdrStatus("busy", self.sdr.driver, self.sdr.serial, holder, str(exc)[:200], int(time.time()))
                return False, f"could not open the receiver: {exc}" + (f" (in use by {holder})" if holder else "")
            self.power = read_power(self.cfg)
            instances, default_budget = budget_for(self.power, self.cfg)
            budget = int(params.get("budget_s") or default_budget)
            session_kind = kind.removeprefix("start_")
            if session_kind == "watch":
                instances = watch_instances(self.power, self.cfg)
                try:      # nothing to watch is a refusal, like a busy receiver, not a failed session
                    plan_watch(self._store, self.cfg, source.sample_rate, int(params.get("max_instances") or instances),
                               center_hz=params.get("center_hz"), freqs=params.get("freqs"))
                except ValueError as exc:
                    source.close()
                    return False, str(exc)
            params = dict(params)
            params.setdefault("max_instances", instances)
            sid = st.create_session(self._store, session_kind, str(cmd.get("source", "?")), params, budget)
            self.session_id, self.session_kind, self.mode = sid, session_kind, session_kind
            self.session_stop.clear()
            self._closing.clear()
            self._stop_reason = None
            self.progress = {"kind": session_kind, "phase": "starting"}
            if params.get("campaign"):
                c = params["campaign"]
                self.progress["campaign"] = {"cycle": c["cycle"], "cycles": c["cycles"], "step": c["step"], "deadline": c["deadline"]}
            self.message = f"{session_kind} session #{sid} started ({budget // 60} min budget, {self.power.short()})"
            self.session_thread = threading.Thread(target=self._session_main, args=(sid, session_kind, params, budget, source),
                                                   name=f"session-{sid}", daemon=True)
            self.session_thread.start()
            return True, f"session {sid} started"
        if kind == "stop":
            if self.session_thread is None or not self.session_thread.is_alive():
                return True, "nothing running"
            self._stop_reason = f"stopped by {cmd.get('source', '?')}"
            self.session_stop.set()
            self.message = "stop requested"
            return True, f"stopping session {self.session_id}"
        if kind in ("correlate", "export"):
            # can take a while on a big database: run it beside the main loop so the heartbeat and
            # the stop key keep working; the worker finishes the command itself
            if self._job is not None and self._job.is_alive():
                return False, "a correlate or export is already running"
            self._job = threading.Thread(target=self._job_main, args=(cmd,), name=f"job-{kind}", daemon=True)
            self._job.start()
            return None
        return False, f"unknown command {kind}"

    def _job_main(self, cmd: dict[str, Any]) -> None:
        kind, params = cmd["kind"], cmd.get("params") or {}
        store = Store(self.db_path)
        try:
            if kind == "correlate":
                from ..correlate import run as correlate
                rep = correlate(store, self.cfg)
                ok, result = True, f"links {rep.links_scored} ({rep.links_by_label}), sites {rep.sites}, systems {rep.systems}"
            else:
                from ..export.report import export_all
                paths, g = export_all(store, self.cfg, Path(params["dir"]) if params.get("dir") else None,
                                      str(params.get("min_label", "weak")))
                ok, result = True, (f"wrote {paths[0].parent}/ (map.html, map.json, map.graphml, systems.txt;"
                                    f" {g.number_of_nodes()} nodes, {g.number_of_edges()} edges)")
        except Exception as exc:  # noqa: BLE001
            store.rollback()
            ok, result = False, f"{type(exc).__name__}: {exc}"
            log.exception("%s failed", kind)
        try:
            (log.info if ok else log.warning)("command %s from %s: %s", kind, cmd.get("source"), result)
            st.finish_command(store, int(cmd["id"]), ok, result)
        finally:
            store.close()

    # -- sessions ----------------------------------------------------------------------------------
    def _set_progress(self, **kw: Any) -> None:
        with self._lock:
            self.progress.update(kw)

    def _session_main(self, sid: int, kind: str, params: dict[str, Any], budget_s: int, source: Any) -> None:
        store = Store(self.db_path)
        # no checkpoints from the thread that reads the receiver: on the Pi's SD card one took 1.8 s
        # inside a commit and the receiver overflowed every 20-40 s. Commits stay cheap (WAL,
        # synchronous=NORMAL); the main loop's connection checkpoints instead, beside the session
        store.conn.execute("PRAGMA wal_autocheckpoint = 0")
        summary: dict[str, Any] = {"budget_s": budget_s}
        status = "done"
        t0 = time.monotonic()
        self.gps = open_gps(self.cfg).start()
        summary["gps"] = self.gps.describe()
        self._last_track_flush = time.monotonic()
        self._session_track = 0
        try:
            st.start_session(store, sid)
            summary["source"] = source.describe()
            if kind == "survey":
                self._survey(store, source, params, budget_s, t0, summary)
            elif kind == "watch":
                self._watch(store, source, params, budget_s, t0, summary, sid)
            else:
                self._decode(store, source, params, budget_s, t0, summary)
        except BudgetDone:
            summary["ended"] = "time budget used"
        except SessionStop:
            status = "stopped"
            summary["stopped"] = self._stop_reason or ("daemon shutting down" if self.stop_event.is_set() else "stop requested")
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            summary["error"] = f"{type(exc).__name__}: {exc}"
            summary["traceback"] = traceback.format_exc()[-1500:]
        finally:
            self._closing.set()
            if source is not None:
                summary.setdefault("overflows", int(getattr(source, "overflows", 0) or 0))   # samples the receiver dropped
                try:
                    source.close()
                except Exception:  # noqa: BLE001
                    pass
            summary["elapsed_s"] = round(time.monotonic() - t0, 1)
            try:
                summary["track_points"] = self._session_track + self._flush_track(store, sid)
                fix = self.gps.fix()
                summary["position_end"] = fix.to_dict() if fix else None
                if getattr(self.gps, "error", None):
                    summary["gps_error"] = self.gps.error
            except Exception as exc:  # noqa: BLE001
                summary["gps_error"] = str(exc)
            finally:
                self.gps.stop()
                self.gps = NoGps("no session running")
            try:
                self._after_session(store, kind, params, summary)
            except Exception as exc:  # noqa: BLE001
                summary["after_error"] = str(exc)
            st.finish_session(store, sid, status, summary)
            store.close()
            self.message = f"{kind} session #{sid} {status} after {summary['elapsed_s']:.0f} s"
            log.info("%s%s", self.message, f": {summary.get('error')}" if summary.get("error") else "")
            self.mode = "idle"

    def _flush_track(self, store: Store, sid: int) -> int:
        """Write the GPS points collected since the last call; returns how many."""
        pts = self.gps.track()
        for f in pts:
            store.conn.execute(
                "INSERT INTO positions (session_id, ts, lat, lon, alt_m, speed_mps, fix, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, f.ts, f.lat, f.lon, f.alt_m, f.speed_mps, f.fix, f.source))
        if pts:
            store.commit()
        self._last_track_flush = time.monotonic()
        return len(pts)

    def _gps_progress(self) -> dict[str, Any]:
        f = self.gps.fix()
        return {"gps": f.to_dict() if f else None, "gps_source": self.gps.describe()}

    def _check(self, t0: float, budget_s: int) -> None:
        if self.stop_event.is_set() or self.session_stop.is_set():
            raise SessionStop()
        if time.monotonic() - t0 >= budget_s:
            raise BudgetDone()

    def _after_session(self, store: Store, kind: str, params: dict[str, Any], summary: dict[str, Any]) -> None:
        dcfg = self.cfg.get("daemon", {}) or {}
        keep = int(dcfg.get("survey_hits_keep_days", 14))
        store.conn.execute("DELETE FROM survey_hits WHERE run_id IN (SELECT id FROM survey_runs WHERE started < ?)",
                           (int(time.time()) - keep * 86400,))
        store.commit()
        from ..db.retention import prune
        pruned = {k: v for k, v in prune(store, self.cfg).items() if v}
        if pruned:
            summary["pruned"] = pruned
        if kind in ("decode", "watch") and dcfg.get("correlate_after_decode", True):
            from ..correlate import run as correlate
            rep = correlate(store, self.cfg)
            summary["correlate"] = {"links": rep.links_scored, "by_label": rep.links_by_label, "sites": rep.sites, "systems": rep.systems}
        if (params.get("campaign") and "error" not in summary and not self.session_stop.is_set()
                and not self.stop_event.is_set()):
            pos = summary.get("position_end") or None
            near = (float(pos["lat"]), float(pos["lon"])) if pos else None
            step = camp_mod.next_step(store, self.cfg, params["campaign"], kind, near=near)
            if step is None:
                summary["campaign_done"] = True
                st.post_command(store, "daemon", "export", {})
            else:
                st.post_command(store, "daemon", step[0], step[1])
                summary["campaign_next"] = st.describe_request(step[0], step[1])
        if (kind == "survey" and params.get("then_decode") and "error" not in summary
                and not self.session_stop.is_set() and not self.stop_event.is_set()):
            # one button, one budget: the decode gets what the survey left over
            total = int(params.get("total_budget_s") or summary.get("budget_s") or 0)
            remaining = int(params.get("decode_budget_s") or (total - summary.get("elapsed_s", 0)))
            if remaining >= 60:
                st.post_command(store, "daemon", "start_decode", {"budget_s": remaining})
                summary["then_decode_s"] = remaining
            else:
                summary["then_decode_s"] = 0
                summary["note"] = "the survey used the whole budget: no time left to decode"

    # -- survey ------------------------------------------------------------------------------------
    def _survey(self, store: Store, source: Any, params: dict[str, Any], budget_s: int, t0: float, summary: dict[str, Any]) -> None:
        bands = self.cfg.get("survey", {}).get("bands", []) or []
        wanted = params.get("bands")
        if wanted:
            bands = [b for b in bands if b.get("name") in wanted]
        if params.get("range"):
            bands = [band_for_range(bands, int(params["range"]["start_hz"]), int(params["range"]["stop_hz"]))]
        if not bands:
            raise ValueError("no survey bands configured (survey.bands)")
        dwell, sweeps = check_survey_params(params, self.cfg)
        scfg = {**(self.cfg.get("survey", {}) or {}), "dwell_s": dwell}
        cfg = {**self.cfg, "survey": scfg}           # this session's dwell; the configured one is untouched
        usable = float((self.cfg.get("sdr", {}) or {}).get("usable_fraction", 0.8))
        summary.update(dwell_s=dwell, sweeps_planned=sweeps,
                       sweep_s_estimate=round(sweep_seconds(bands, source.sample_rate, usable, dwell)), bands={}, sweeps=[])
        heard: set[int] = set()
        # one budget for a survey and the decode after it: a further sweep starts only while it
        # leaves at least half of that budget to the decode (the first always runs)
        total = int(params.get("total_budget_s") or budget_s) if params.get("then_decode") else 0
        took: list[float] = []
        for n in range(1, sweeps + 1):
            self._check(t0, budget_s)
            if total and n > 1:
                need = max(float(summary["sweep_s_estimate"]), max(took))
                if time.monotonic() - t0 + need > total / 2:
                    summary["sweeps_skipped"] = sweeps - n + 1
                    summary["sweeps_note"] = (f"{n - 1} of {sweeps} sweeps: another one ({need / 60:.1f} min) would leave the decode"
                                       f" less than half of the {total // 60} min")
                    break
            sweep_t0 = time.monotonic()
            this: dict[str, int] = {}
            for band in bands:
                self._check(t0, budget_s)
                started = int(time.time())

                def progress(p: dict[str, Any]) -> None:
                    self._set_progress(phase="sweeping", sweep=n, sweeps=sweeps, elapsed_s=int(time.monotonic() - t0), budget_s=budget_s,
                                       **p, **self._gps_progress())
                    if time.monotonic() - self._last_track_flush >= 30:
                        self._session_track += self._flush_track(store, self.session_id or 0)

                def here() -> tuple[float, float] | None:
                    f = self.gps.fix()
                    return (f.lat, f.lon) if f else None

                cut: list[BaseException] = []

                def stop() -> bool:
                    try:
                        self._check(t0, budget_s)
                    except (SessionStop, BudgetDone) as exc:
                        cut.append(exc)      # record what this band's sweep heard so far, then end the session
                        return True
                    return False

                hits = sweep(source, band, cfg, stop=stop, progress=progress, position=here)
                settings = {k: v for k, v in scfg.items() if k != "bands"}
                if sweeps > 1:
                    settings["sweep"] = f"{n}/{sweeps}"
                if cut:
                    settings["partial"] = True
                run_id = record(store, str(band.get("name")), started, hits, source.describe(), settings)
                heard.update(h.freq_hz for h in hits)
                this[str(band.get("name"))] = len(hits)
                summary["bands"][str(band.get("name"))] = {"run_id": run_id, "hits": len(hits),
                                                           "top": [h.to_dict() for h in sorted(hits, key=lambda h: -h.snr_db)[:10]]}
                self._set_progress(phase="band done", band=band.get("name"), hits=len(hits))
                if cut:
                    summary["sweeps"].append({"sweep": n, "seconds": round(time.monotonic() - sweep_t0), "hits": this, "partial": True})
                    summary["distinct_hits"] = len(heard)
                    raise cut[0]
            took.append(time.monotonic() - sweep_t0)
            summary["sweeps"].append({"sweep": n, "seconds": round(took[-1]), "hits": this})
            summary["distinct_hits"] = len(heard)

    # -- decode ------------------------------------------------------------------------------------
    def _decode(self, store: Store, source: Any, params: dict[str, Any], budget_s: int, t0: float, summary: dict[str, Any]) -> None:
        dcfg = self.cfg.get("decode", {}) or {}
        max_instances = int(params.get("max_instances") or 1)
        only = [int(f) for f in (params.get("freqs") or [])]
        # carriers asked for by name with a budget given (MCP, `mapper decode F --minutes N`): one pass
        # that listens for the whole budget, shared out among the slices; without a budget, one dwell
        spread = bool(only and params.get("budget_s"))
        summary["carriers"] = {}
        summary["slices"] = []
        rounds = 0
        radius = plan_radius_km(self.cfg)
        if radius and not only:
            # the plan follows the receiver: sweeps made at an earlier stop are left out. A fresh
            # session's GPS may need a moment for its first fix.
            deadline = time.monotonic() + 5
            while self.gps.fix() is None and not isinstance(self.gps, NoGps) and time.monotonic() < deadline:
                self._check(t0, budget_s)
                time.sleep(0.25)
        while True:
            self._check(t0, budget_s)
            f = self.gps.fix() if radius and not only else None
            here = (f.lat, f.lon) if f else None
            far: list[int] = []
            hits = recent_hits(store, plan_window_s(self.cfg), max_age_s=7 * 86400, near=here, radius_km=radius, far_runs=far)
            summary["plan"] = ({"position": [round(here[0], 5), round(here[1], 5)], "radius_km": radius, "far_sweeps_left_out": len(far)}
                               if here else {"position": None, "note": "no GPS fix: sweeps from every place count" if radius and not only else
                                             "plan_radius_km 0 or carriers given: sweeps from every place count"})
            targets = targets_from_store(store, self.cfg, hits, only_freqs=only or None, near=here, radius_km=radius)
            if not targets:
                summary["note"] = "no carriers to decode: run a survey first"
                return
            # ordered for what is left of the budget: never-decoded carriers keep decode.new_share of it
            left_s = None if spread else max(0.0, budget_s - (time.monotonic() - t0))
            slices = plan(targets, self.cfg, max_instances, source.sample_rate, budget_s=left_s)
            rounds += 1
            for i, sl in enumerate(slices):
                self._check(t0, budget_s)
                remaining = budget_s - (time.monotonic() - t0)
                if spread:
                    dwell = int(max(10, remaining / (len(slices) - i)))
                else:
                    dwell = int(min(sl.dwell_s, max(10, remaining)))
                self._set_progress(phase="decoding", round=rounds, slice=i + 1, slices=len(slices), center_hz=sl.center_hz,
                                   freqs=[t.freq_hz for t in sl.targets], reason=sl.reason, dwell_s=dwell,
                                   elapsed_s=int(time.monotonic() - t0), budget_s=budget_s)
                res = self._decode_slice(store, source, sl, dwell, t0, budget_s)
                summary["slices"].append({**sl.to_dict(), "dwell_s": dwell, "result": res})
                for f, r in res.items():
                    summary["carriers"][str(f)] = r
            if only:
                return   # explicit frequencies: one pass is what was asked for

    def _decode_slice(self, store: Store, source: Any, sl: Slice, dwell_s: int, t0: float, budget_s: int) -> dict[int, dict[str, Any]]:
        dcfg = self.cfg.get("decode", {}) or {}
        base_port = int(dcfg.get("tcp_base_port", 7355))
        invert_after = float(dcfg.get("invert_retry_s", 10))
        source.tune(sl.center_hz)
        chan = Channelizer(source.sample_rate, source.center_hz)
        freqs = [t.freq_hz for t in sl.targets]
        chan.set_channels(freqs)
        lines: "queue.Queue[tuple[int, str | None]]" = queue.Queue()
        servers: dict[int, PcmServer] = {}
        decoders: dict[int, DecoderProcess] = {}
        ingests: dict[int, CarrierIngest] = {}
        result: dict[int, dict[str, Any]] = {}
        stopping: list[threading.Thread] = []
        try:
            for i, t in enumerate(sl.targets):
                servers[t.freq_hz] = PcmServer(base_port + i)
                decoders[t.freq_hz] = DecoderProcess(self.cfg, t.freq_hz, base_port + i, lines)
                decoders[t.freq_hz].start()
                ingests[t.freq_hz] = CarrierIngest(store, self.cfg, t.freq_hz, snr_db=t.snr_db)
            store.commit()
            for f, srv in servers.items():
                if not srv.wait_client(5.0):
                    result[f] = {"error": "dsd-fme did not connect to the PCM bridge"}
            source.read(int(source.sample_rate * 0.1))   # settle after retune
            slice_t0 = time.monotonic()
            last_flush = slice_t0
            last_progress = slice_t0
            inverted: set[int] = set()
            while time.monotonic() - slice_t0 < dwell_s:
                self._check(t0, budget_s)
                block = source.read(chan.hop)
                audio = chan.process(block)
                for f, pcm in audio.items():
                    servers[f].write(pcm)
                self._drain(lines, ingests)
                if store.conn.in_transaction:
                    store.commit()   # never hold the write lock across blocks: the main thread needs it
                now = time.monotonic()
                if now - slice_t0 >= invert_after:
                    for f, ing in ingests.items():
                        if f not in inverted and ing.pos_syncs == 0 and ing.neg_syncs > 20:
                            inverted.add(f)
                            stopping.append(decoders[f].stop_async())     # the new one takes over the bridge
                            decoders[f] = DecoderProcess(self.cfg, f, servers[f].port, lines, inverted=True)
                            decoders[f].start()
                            ing.stats.note = "restarted with -xr (inverted polarity)"
                if now - last_flush >= 60:
                    for ing in ingests.values():
                        ing.flush_partial()
                    store.commit()
                    self._session_track += self._flush_track(store, self.session_id or 0)
                    last_flush = now
                if now - last_progress >= 1:
                    self._set_progress(slice_elapsed_s=int(now - slice_t0), elapsed_s=int(now - t0), **self._gps_progress(),
                                       carriers=[{"freq_hz": f, "syncs": ing.pos_syncs, "lines": ing.lines, "level_db": round(chan.channels[f].level_db, 1)}
                                                 for f, ing in ingests.items()])
                    last_progress = now
        finally:
            # bridges first (DSD-FME's read returns 0 and it exits by itself), then every process at once
            for srv in servers.values():
                srv.close()
            stopping += [d.stop_async() for d in decoders.values()]
            for th in stopping:
                th.join(timeout=10)
            self._drain(lines, ingests, final=True)
            for f, ing in ingests.items():
                stats = ing.finish()
                r = ing.summary()
                if stats.note:
                    r["note"] = stats.note
                r["level_db"] = round(chan.channels[f].level_db, 1) if f in chan.channels else None
                result.setdefault(f, {}).update(r)
        return result

    # -- watch -------------------------------------------------------------------------------------
    def _watch(self, store: Store, source: Any, params: dict[str, Any], budget_s: int, t0: float, summary: dict[str, Any],
               sid: int) -> None:
        """One slice for the whole session: pinned control channel(s), a DSD-FME for every channel
        that keys up (see watch.py), every key-up recorded.

        Nothing slow happens in the block loop: a decoder is retired (bridge closed, process
        stopped) in a thread of its own, and its port only comes back when that is done. A carrier
        is created when its DSD-FME prints a DMR sync or its key-up lasts ``min_keyup_s``, not by a
        blip. Key-up times are the wall clock when the block is processed: DSD-FME stamps its lines
        when it processes the same audio, so both carry the same lag behind the air, whatever the
        receiver buffers or drops. Overflows and the time a block takes are reported."""
        from collections import deque

        from ..ingest.runner import _band_for

        w = watch_cfg(self.cfg)
        limit = int(params.get("max_instances") or 1)
        plan = plan_watch(store, self.cfg, source.sample_rate, limit, center_hz=params.get("center_hz"), freqs=params.get("freqs"))
        summary["plan"] = plan.to_dict()
        summary["instances"] = limit
        source.tune(plan.center_hz)
        chan = Channelizer(source.sample_rate, source.center_hz)
        block_s = chan.hop / source.sample_rate
        history: "deque[tuple[np.ndarray, int]]" = deque(maxlen=max(1, int(round(float(w["preroll_s"]) / block_s)) + 1))
        detector = KeyupDetector(plan.channels, float(w["keyup_snr_db"]), float(w["keydown_snr_db"]), float(w["hang_s"]))
        planner = InstancePlanner(plan.pinned, limit, float(w["max_hold_s"]), plan.priority, min_hold_s=float(w["min_decoder_s"]))
        heard = ListenLog(no_dmr_after_s=float(w["no_dmr_after_s"]))
        base_port = int((self.cfg.get("decode", {}) or {}).get("tcp_base_port", 7355))
        # twice as many ports as decoders, used in turn, each back only once its old DSD-FME is gone
        free_ports = deque(base_port + i for i in range(2 * limit))
        lines: "queue.Queue[tuple[int, str | None]]" = queue.Queue()
        servers: dict[int, PcmServer] = {}
        decoders: dict[int, DecoderProcess] = {}
        ingests: dict[int, CarrierIngest] = {}
        unheard: dict[int, list[str]] = {}         # lines of a channel whose DSD-FME has not printed a DMR sync yet
        retiring: list[threading.Thread] = []
        restarts: dict[int, int] = {}
        per: dict[str, dict[str, Any]] = {}
        # decoded: DMR heard during the key-up; listened: a connected DSD-FME had its audio
        totals = {"keyups": 0, "decoded": 0, "listened": 0, "missed": 0, "no_decoder": 0, "blips": 0, "preempted": 0}
        timing = {"blocks": 0, "busy_s": 0.0, "max_ms": 0.0}
        slow: list[dict[str, Any]] = []          # the slowest blocks, with where the time went
        overflow_at: list[int] = []              # when the receiver dropped samples
        no_dmr: set[int] = set()                 # listened to for a while without DMR: last in line for the session
        overflows0 = int(getattr(source, "overflows", 0) or 0)
        backlog = int(AUDIO_RATE * 2 * (float(w["preroll_s"]) + 3.0))   # pre-roll + DSD-FME start-up, int16

        def retire(f: int, final: bool = False) -> None:
            d, srv = decoders.pop(f, None), servers.pop(f, None)
            chan.remove_channel(f)
            if srv is not None and not final:
                heard.retired(f, srv.ever_connected)

            def run() -> None:
                if srv is not None:
                    srv.close()          # DSD-FME's read returns 0: it exits by itself
                if d is not None:
                    d.stop()
                if srv is not None:
                    free_ports.append(srv.port)
            th = threading.Thread(target=run, name=f"retire-{f}", daemon=True)
            th.start()
            retiring.append(th)

        def start_decoder(f: int) -> None:
            port = free_ports.popleft()
            srv = PcmServer(port, backlog_bytes=backlog)
            servers[f] = srv
            decoders[f] = DecoderProcess(self.cfg, f, port, lines)
            decoders[f].start()
            chan.add_channel(f)
            for Xh, n0h in history:                  # the moments before the key-up, oldest first
                pcm = chan.extract(Xh, n0h, only=[f]).get(f)
                if pcm is not None:
                    srv.write(pcm)

        def drain() -> None:
            while True:
                try:
                    f, line = lines.get_nowait()
                except queue.Empty:
                    return
                if line is None:
                    continue
                if "Sync:" in line and "DMR" in line:
                    heard.heard_dmr(f)
                ing = ingests.get(f)
                if ing is None:
                    buf = unheard.setdefault(f, [])
                    buf.append(line)
                    if "Sync:" in line and "DMR" in line:
                        ingests[f] = ing = CarrierIngest(store, self.cfg, f)
                        for held in unheard.pop(f):
                            ing.feed(held)
                    elif len(buf) > 400:
                        del buf[:200]
                    continue
                ing.feed(line)

        def record_keyup(k: Any) -> None:
            listened, dmr = heard.keyup_ended(k.freq_hz)
            if k.duration < float(w["min_keyup_s"]) and k.freq_hz not in plan.pinned:
                totals["blips"] += 1
                return
            stats = per.setdefault(str(k.freq_hz), {"keyups": 0, "airtime_s": 0.0, "decoded": 0, "listened": 0})
            cid = store.upsert_carrier(k.freq_hz, int(k.start), _band_for(self.cfg, k.freq_hz))
            store.upsert_carrier_seen(cid, int(k.start), int(k.end))
            store.conn.execute("INSERT INTO keyups (carrier_id, session_id, start, end, peak_snr_db, decoded, dmr_heard)"
                               " VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (cid, sid, round(k.start, 2), round(k.end, 2), k.peak_snr_db, int(listened), int(dmr)))
            stats["keyups"] += 1
            stats["airtime_s"] = round(stats["airtime_s"] + k.duration, 1)
            stats["decoded"] += int(dmr)
            stats["listened"] += int(listened)
            totals["keyups"] += 1
            totals["decoded" if dmr else "missed"] += 1
            totals["listened"] += int(listened)
            totals["no_decoder"] += int(not listened)

        def load() -> dict[str, Any]:
            n = max(1, timing["blocks"])
            return {"overflows": int(getattr(source, "overflows", 0) or 0) - overflows0,
                    "block_ms_avg": round(1000 * timing["busy_s"] / n, 1), "block_ms_max": round(timing["max_ms"], 1),
                    "block_ms_budget": round(1000 * block_s, 1)}

        def syncs(f: int) -> int:
            if f in ingests:
                return ingests[f].pos_syncs
            return sum(1 for line in unheard.get(f, []) if "Sync:" in line and "DMR" in line)

        last_flush = last_progress = last_check = time.monotonic()
        try:
            source.read(int(source.sample_rate * 0.1))   # settle after retune
            while True:
                self._check(t0, budget_s)
                block = source.read(chan.hop)
                t = time.time()
                busy0 = time.monotonic()
                ov = int(getattr(source, "overflows", 0) or 0) - overflows0
                if ov > len(overflow_at) and len(overflow_at) < 50:
                    overflow_at.append(int(t))
                X, n0 = chan.transform(block)
                history.append((X, n0))
                started, ended = detector.update(t, chan.channel_power(X, plan.channels, float(w["channel_width_hz"])))
                lap = {"dsp": time.monotonic() - busy0}
                for f in started:
                    heard.keyup_started(f)
                for k in ended:
                    record_keyup(k)
                to_start, to_stop = planner.plan(t, detector.active())
                totals["preempted"] += sum(1 for f in to_stop if f in detector.active())
                for f in to_stop:
                    retire(f)
                for f in to_start:
                    start_decoder(f)
                lap["decoders"] = time.monotonic() - busy0
                fresh = set(to_start)
                for f, pcm in chan.extract(X, n0, only=[f for f in servers if f not in fresh]).items():
                    servers[f].write(pcm)
                for f, srv in servers.items():
                    heard.tick(f, block_s, srv.connected)
                lap["audio"] = time.monotonic() - busy0
                drain()
                lap["lines"] = time.monotonic() - busy0
                if store.conn.in_transaction:
                    store.commit()
                lap["commit"] = time.monotonic() - busy0
                now = time.monotonic()
                if now - last_check >= 2:
                    # a channel listened to for ``no_dmr_after_s`` without DMR (analogue, another protocol),
                    # counted over all its key-ups (short ones never last that long): it gives its decoder
                    # back and is last in line for the rest of the session
                    for f in list(servers):
                        if f not in plan.pinned and f not in no_dmr and heard.no_dmr(f, syncs(f)):
                            no_dmr.add(f)
                            planner.priority[f] = 3
                            planner.assigned.pop(f, None)
                            planner.cooldown.add(f)
                            retire(f)
                    # a pinned control channel's DSD-FME must not stay dead for the rest of a long session
                    for f in plan.pinned:
                        d = decoders.get(f)
                        if d is not None and not d.alive and restarts.get(f, 0) < 3:
                            restarts[f] = restarts.get(f, 0) + 1
                            decoders[f] = DecoderProcess(self.cfg, f, servers[f].port, lines)
                            decoders[f].start()
                    retiring[:] = [th for th in retiring if th.is_alive()]
                    last_check = now
                if now - last_flush >= 60:
                    for ing in ingests.values():
                        ing.flush_partial()
                    store.commit()
                    self._session_track += self._flush_track(store, self.session_id or 0)
                    last_flush = now
                total = time.monotonic() - busy0
                timing["blocks"] += 1
                timing["busy_s"] += total
                timing["max_ms"] = max(timing["max_ms"], 1000 * total)
                if total > block_s and (len(slow) < 5 or total * 1000 > slow[-1]["ms"]):
                    parts, prev = {}, 0.0
                    for k, v in {**lap, "rest": total}.items():
                        parts[k] = round(1000 * (v - prev), 1)
                        prev = v
                    slow.append({"ts": int(t), "ms": round(1000 * total, 1), **parts, "started": len(to_start), "stopped": len(to_stop)})
                    slow.sort(key=lambda x: -x["ms"])
                    del slow[5:]
                if now - last_progress >= 1:
                    active = detector.active()
                    chans = [{"freq_hz": f, "snr_db": detector.snr_of(f),
                              "state": "pinned" if f in plan.pinned else ("decoding" if f in servers else "waiting"),
                              "syncs": ingests[f].pos_syncs if f in ingests else 0}
                             for f in sorted(set(active) | set(plan.pinned))]
                    self._set_progress(phase="watching", center_hz=plan.center_hz, span_hz=plan.to_dict()["span_hz"], channels=chans,
                                       instances=f"{len(servers)}/{limit}", elapsed_s=int(now - t0), budget_s=budget_s,
                                       **totals, **load(), **self._gps_progress())
                    last_progress = now
        finally:
            # processes and ports first, whatever happens to the writes below
            for f in list(servers):
                retire(f, final=True)
            for th in retiring:
                th.join(timeout=10)
            drain()
            try:
                for k in detector.close():
                    record_keyup(k)
                for f, ing in ingests.items():
                    ing.finish()
                    r = ing.summary()
                    per.setdefault(str(f), {"keyups": 0, "airtime_s": 0.0, "decoded": 0, "listened": 0}).update(
                        {k: r[k] for k in ("lines", "syncs", "structural", "variant", "identities")})
                store.commit()
            finally:
                summary["watch"] = {**totals, **load(), "restarts": restarts, "no_dmr": sorted(set(unheard) | no_dmr),
                                    "never_connected": {str(f): n for f, n in sorted(heard.never_connected.items())},
                                    "slow_blocks": slow, "overflow_at": overflow_at}
                summary["carriers"] = per

    @staticmethod
    def _drain(lines: "queue.Queue[tuple[int, str | None]]", ingests: dict[int, CarrierIngest], final: bool = False) -> None:
        """Feed queued lines to their carriers. The final call comes after every decoder was stopped
        and its reader thread joined, so everything is already queued: empty means done."""
        while True:
            try:
                f, line = lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                continue
            ing = ingests.get(f)
            if ing is not None:
                ing.feed(line)


def run_daemon(db_path: str | Path, cfg: dict[str, Any], backend: Backend | None = None) -> int:
    d = Daemon(db_path, cfg, backend)
    rc = d.run()
    if d.restart_requested:
        _reexec()
    return rc


def _reexec() -> None:
    """Replace this process with a fresh copy of itself (same pid, same arguments): the only way
    to unload the SDR driver. The lock file is released by run() and the database is closed."""
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
