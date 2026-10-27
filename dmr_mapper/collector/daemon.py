"""The collector daemon: always running, idle until asked.

* holds no receiver while idle; before a session it probes the receiver and refuses politely when
  it is absent or held by another program;
* a session (survey or decode) runs in a worker thread with a time budget and ends by itself;
* the main loop keeps a heartbeat, serves the command queue, watches the power state and stops a
  session on undervoltage;
* it is the single writer of decoded data (one ``Store`` per thread; SQLite WAL in between).

Clients never call into this module; they post rows to ``commands`` (see ``state.py``).
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sqlite3
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

from ..db import Store
from . import state as st
from .bridge import PcmServer
from .channelizer import AUDIO_RATE, Channelizer
from .decoder import DecoderProcess, find_dsd_fme
from .ingest import CarrierIngest
from .power import PowerState, budget_for, read_power
from .scheduler import Slice, plan, targets_from_store
from .sdr import Backend, SdrStatus, make_backend
from .survey import latest_hits, record, sweep


class SessionStop(Exception):
    pass


class Daemon:
    def __init__(self, db_path: str | Path, cfg: dict[str, Any], backend: Backend | None = None, source_tag: str = "daemon"):
        self.db_path = Path(db_path)
        self.cfg = cfg
        self.backend = backend or make_backend(cfg)
        dcfg = cfg.get("daemon", {}) or {}
        self.tick_s = float(dcfg.get("tick_s", 1))
        self.probe_interval_s = float(dcfg.get("sdr_probe_interval_s", 30))
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
            st.mark_offline(self._store, self.pid)
        finally:
            self._store.close()
            self._release_lock()
        return 0

    def _install_signals(self) -> None:
        def _stop(signum: int, frame: Any) -> None:
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
            self.power = read_power(self.cfg)
            self._last_power = now
            if self.session_thread is not None and self.session_thread.is_alive() and self.power.undervolt_now \
                    and (self.cfg.get("power", {}) or {}).get("stop_on_undervoltage", True):
                self.message = "stopping session: undervoltage reported by the firmware"
                self.session_stop.set()
        if self.session_thread is None or not self.session_thread.is_alive():
            if self.session_thread is not None:
                self.session_thread = None
                self.session_id = None
                self.mode = "idle"
                self.progress = {}
            if now - self._last_probe >= self.probe_interval_s:
                self.sdr = self._probe()
                self._last_probe = now
        for _ in range(5):
            cmd = st.take_command(self._store)
            if cmd is None:
                break
            ok, result = self._handle(cmd)
            st.finish_command(self._store, int(cmd["id"]), ok, result)
        with self._lock:
            progress = dict(self.progress)
        st.write_heartbeat(self._store, self.pid, self.started, self.mode, self.session_id, self.sdr.to_dict(), self.power.to_dict(),
                           progress, self.message)

    def _probe(self) -> SdrStatus:
        try:
            return self.backend.probe()
        except Exception as exc:  # noqa: BLE001
            return SdrStatus("error", None, None, None, str(exc)[:200], int(time.time()))

    # -- commands ----------------------------------------------------------------------------------
    def _handle(self, cmd: dict[str, Any]) -> tuple[bool, str]:
        assert self._store is not None
        kind, params = cmd["kind"], cmd.get("params") or {}
        if kind in ("start_survey", "start_decode"):
            if self.session_thread is not None and self.session_thread.is_alive():
                return False, f"a {self.session_kind} session is already running (#{self.session_id}); stop it first"
            if kind == "start_decode" and find_dsd_fme(self.cfg) is None:
                return False, "dsd-fme not found (decode.dsd_fme_bin)"
            self.sdr = self._probe()
            self._last_probe = time.monotonic()
            if self.sdr.status != "present":
                return False, self.sdr.short()
            self.power = read_power(self.cfg)
            instances, default_budget = budget_for(self.power, self.cfg)
            budget = int(params.get("budget_s") or default_budget)
            session_kind = "survey" if kind == "start_survey" else "decode"
            params = dict(params)
            params.setdefault("max_instances", instances)
            sid = st.create_session(self._store, session_kind, str(cmd.get("source", "?")), params, budget)
            self.session_id, self.session_kind, self.mode = sid, session_kind, session_kind
            self.session_stop.clear()
            self.progress = {"kind": session_kind, "phase": "starting"}
            self.message = f"{session_kind} session #{sid} started ({budget // 60} min budget, {self.power.short()})"
            self.session_thread = threading.Thread(target=self._session_main, args=(sid, session_kind, params, budget),
                                                   name=f"session-{sid}", daemon=True)
            self.session_thread.start()
            return True, f"session {sid} started"
        if kind == "stop":
            if self.session_thread is None or not self.session_thread.is_alive():
                return True, "nothing running"
            self.session_stop.set()
            self.message = "stop requested"
            return True, f"stopping session {self.session_id}"
        if kind == "correlate":
            from ..correlate import run as correlate
            rep = correlate(self._store, self.cfg)
            return True, f"links {rep.links_scored} ({rep.links_by_label}), sites {rep.sites}, systems {rep.systems}"
        if kind == "export":
            from ..config import resolve_path
            from ..export import build, write
            path = params.get("path") or str(resolve_path(self.cfg, "exports") / "map.html")
            g = build(self._store, min_label=str(params.get("min_label", "possible")))
            write(g, path)
            return True, f"wrote {path} ({g.number_of_nodes()} nodes, {g.number_of_edges()} edges)"
        return False, f"unknown command {kind}"

    # -- sessions ----------------------------------------------------------------------------------
    def _set_progress(self, **kw: Any) -> None:
        with self._lock:
            self.progress.update(kw)

    def _session_main(self, sid: int, kind: str, params: dict[str, Any], budget_s: int) -> None:
        store = Store(self.db_path)
        summary: dict[str, Any] = {"budget_s": budget_s}
        status = "done"
        source = None
        t0 = time.monotonic()
        try:
            st.start_session(store, sid)
            source = self.backend.open()
            summary["source"] = source.describe()
            if kind == "survey":
                self._survey(store, source, params, budget_s, t0, summary)
            else:
                self._decode(store, source, params, budget_s, t0, summary)
        except SessionStop:
            status = "stopped"
            summary["stopped"] = self.message or "stop requested"
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            summary["error"] = f"{type(exc).__name__}: {exc}"
            summary["traceback"] = traceback.format_exc()[-1500:]
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:  # noqa: BLE001
                    pass
            summary["elapsed_s"] = round(time.monotonic() - t0, 1)
            try:
                self._after_session(store, kind, params, summary)
            except Exception as exc:  # noqa: BLE001
                summary["after_error"] = str(exc)
            st.finish_session(store, sid, status, summary)
            store.close()
            self.message = f"{kind} session #{sid} {status} after {summary['elapsed_s']:.0f} s"
            self.mode = "idle"

    def _check(self, t0: float, budget_s: int) -> None:
        if self.stop_event.is_set() or self.session_stop.is_set():
            raise SessionStop()
        if time.monotonic() - t0 >= budget_s:
            raise SessionStop()

    def _after_session(self, store: Store, kind: str, params: dict[str, Any], summary: dict[str, Any]) -> None:
        dcfg = self.cfg.get("daemon", {}) or {}
        keep = int(dcfg.get("survey_hits_keep_days", 14))
        store.conn.execute("DELETE FROM survey_hits WHERE run_id IN (SELECT id FROM survey_runs WHERE started < ?)",
                           (int(time.time()) - keep * 86400,))
        store.commit()
        if kind == "decode" and dcfg.get("correlate_after_decode", True):
            from ..correlate import run as correlate
            rep = correlate(store, self.cfg)
            summary["correlate"] = {"links": rep.links_scored, "by_label": rep.links_by_label, "sites": rep.sites, "systems": rep.systems}
        if kind == "survey" and params.get("then_decode"):
            st.post_command(store, "daemon", "start_decode", {"budget_s": params.get("decode_budget_s")})

    # -- survey ------------------------------------------------------------------------------------
    def _survey(self, store: Store, source: Any, params: dict[str, Any], budget_s: int, t0: float, summary: dict[str, Any]) -> None:
        bands = self.cfg.get("survey", {}).get("bands", []) or []
        wanted = params.get("bands")
        if wanted:
            bands = [b for b in bands if b.get("name") in wanted]
        if not bands:
            raise ValueError("no survey bands configured (survey.bands)")
        summary["bands"] = {}
        scfg = dict(self.cfg.get("survey", {}) or {})
        for band in bands:
            self._check(t0, budget_s)
            started = int(time.time())

            def progress(p: dict[str, Any]) -> None:
                self._set_progress(phase="sweeping", elapsed_s=int(time.monotonic() - t0), budget_s=budget_s, **p)

            def stop() -> bool:
                try:
                    self._check(t0, budget_s)
                except SessionStop:
                    return True
                return False

            hits = sweep(source, band, self.cfg, stop=stop, progress=progress)
            run_id = record(store, str(band.get("name")), started, hits, source.describe(), {k: v for k, v in scfg.items() if k != "bands"})
            summary["bands"][str(band.get("name"))] = {"run_id": run_id, "hits": len(hits), "top": [h.to_dict() for h in sorted(hits, key=lambda h: -h.snr_db)[:10]]}
            self._set_progress(phase="band done", band=band.get("name"), hits=len(hits))

    # -- decode ------------------------------------------------------------------------------------
    def _decode(self, store: Store, source: Any, params: dict[str, Any], budget_s: int, t0: float, summary: dict[str, Any]) -> None:
        dcfg = self.cfg.get("decode", {}) or {}
        max_instances = int(params.get("max_instances") or 1)
        only = [int(f) for f in (params.get("freqs") or [])]
        summary["carriers"] = {}
        summary["slices"] = []
        rounds = 0
        while True:
            self._check(t0, budget_s)
            hits = latest_hits(store, max_age_s=7 * 86400)
            targets = targets_from_store(store, self.cfg, hits, only_freqs=only or None)
            if not targets:
                summary["note"] = "no carriers to decode: run a survey first"
                return
            slices = plan(targets, self.cfg, max_instances, source.sample_rate)
            rounds += 1
            for i, sl in enumerate(slices):
                self._check(t0, budget_s)
                remaining = budget_s - (time.monotonic() - t0)
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
                            decoders[f].stop()
                            decoders[f] = DecoderProcess(self.cfg, f, servers[f].port, lines, inverted=True)
                            decoders[f].start()
                            ing.stats.note = "restarted with -xr (inverted polarity)"
                if now - last_flush >= 60:
                    for ing in ingests.values():
                        ing.flush_partial()
                    store.commit()
                    last_flush = now
                if now - last_progress >= 1:
                    self._set_progress(slice_elapsed_s=int(now - slice_t0), elapsed_s=int(now - t0),
                                       carriers=[{"freq_hz": f, "syncs": ing.pos_syncs, "lines": ing.lines, "level_db": round(chan.channels[f].level_db, 1)}
                                                 for f, ing in ingests.items()])
                    last_progress = now
        finally:
            for d in decoders.values():
                d.stop()
            self._drain(lines, ingests, final=True)
            for srv in servers.values():
                srv.close()
            for f, ing in ingests.items():
                stats = ing.finish()
                r = ing.summary()
                if stats.note:
                    r["note"] = stats.note
                r["level_db"] = round(chan.channels[f].level_db, 1) if f in chan.channels else None
                result.setdefault(f, {}).update(r)
        return result

    @staticmethod
    def _drain(lines: "queue.Queue[tuple[int, str | None]]", ingests: dict[int, CarrierIngest], final: bool = False) -> None:
        deadline = time.monotonic() + (2.0 if final else 0.0)
        while True:
            try:
                f, line = lines.get(timeout=0.2 if final else 0.0) if final else lines.get_nowait()
            except queue.Empty:
                if final and time.monotonic() < deadline:
                    continue
                break
            if line is None:
                continue
            ing = ingests.get(f)
            if ing is not None:
                ing.feed(line)


def run_daemon(db_path: str | Path, cfg: dict[str, Any], backend: Backend | None = None) -> int:
    return Daemon(db_path, cfg, backend).run()
