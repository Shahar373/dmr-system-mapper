"""Where the receiver is. The Pi is driven around, so every session records a track and every
survey hit the position where it was measured.

Sources, in order (``gps.source: auto``):

* **gpsd** (``gps.gpsd_host``/``gpsd_port``): made for sharing one receiver between programs, so
  it is always safe to use;
* **the serial device directly** (a VK-162 is a u-blox 7 on ``/dev/ttyACM*``, NMEA): only when no
  other process has the device open. Two programs reading one tty steal each other's lines, and
  the rule on this Pi is that whatever runs first is not disturbed.

The GPS is read only while a session runs, never while the daemon is idle. Nothing here writes to
the database: the session thread takes ``track()`` and ``fix()`` and stores them.
"""

from __future__ import annotations

import json
import math
import os
import select
import socket
import termios
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Fix:
    ts: int
    lat: float
    lon: float
    alt_m: float | None = None
    speed_mps: float | None = None
    fix: str = "2d"              # 2d | 3d
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "lat": round(self.lat, 6), "lon": round(self.lon, 6), "alt_m": self.alt_m,
                "speed_mps": self.speed_mps, "fix": self.fix, "source": self.source}


def distance_m(a: Fix, b: Fix) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# -- NMEA ------------------------------------------------------------------------------------------
def _nmea_ok(sentence: str) -> bool:
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, csum = sentence[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(csum[:2], 16)
    except ValueError:
        return False


def _coord(value: str, hemi: str) -> float | None:
    if not value or not hemi:
        return None
    try:
        dot = value.index(".")
        deg = float(value[:dot - 2])
        minutes = float(value[dot - 2:])
    except (ValueError, IndexError):
        return None
    v = deg + minutes / 60.0
    return -v if hemi in ("S", "W") else v


class NmeaState:
    """Combines RMC (position, speed, validity) and GGA (fix quality, altitude)."""

    def __init__(self, source: str = "serial"):
        self.source = source
        self.alt_m: float | None = None
        self.quality = 0
        self.sats = 0

    def feed(self, line: str) -> Fix | None:
        line = line.strip()
        if not _nmea_ok(line):
            return None
        f = line[1:].split("*")[0].split(",")
        kind = f[0][2:]
        if kind == "GGA" and len(f) > 9:
            self.quality = int(f[6] or 0)
            self.sats = int(f[7] or 0)
            self.alt_m = float(f[9]) if f[9] else None
            return None
        if kind == "RMC" and len(f) > 7:
            if f[2] != "A":
                return None
            lat, lon = _coord(f[3], f[4]), _coord(f[5], f[6])
            if lat is None or lon is None:
                return None
            speed = float(f[7]) * 0.514444 if f[7] else None
            return Fix(int(time.time()), lat, lon, self.alt_m, speed, "3d" if self.alt_m is not None and self.sats >= 4 else "2d", self.source)
        return None


# -- device sharing --------------------------------------------------------------------------------
def device_users(device: str) -> list[str]:
    """Other processes that have ``device`` open (as far as /proc lets us see)."""
    try:
        target = os.path.realpath(device)
    except OSError:
        return []
    me = os.getpid()
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") == target:
                    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                    out.append(f"{cmd[:60]} (pid {pid})")
                    break
            except OSError:
                continue
    return out


def find_device(cfg: dict[str, Any]) -> str | None:
    g = cfg.get("gps", {}) or {}
    if g.get("device"):
        return str(g["device"]) if Path(str(g["device"])).exists() else None
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        for p in sorted(by_id.iterdir()):
            name = p.name.lower()
            if any(k in name for k in ("u-blox", "ublox", "gps", "gnss")):
                return str(p)
    for cand in ("/dev/ttyACM0", "/dev/gps0"):
        if Path(cand).exists():
            return cand
    return None


def gpsd_available(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# -- readers ---------------------------------------------------------------------------------------
class GpsReader:
    """Background reader. ``fix()`` is the newest fix if it is younger than ``max_age_s``;
    ``track()`` returns and clears the points collected since the last call (one every
    ``track_every_s`` while moving more than ``track_min_move_m``, at least every
    ``track_idle_s`` when standing still)."""

    def __init__(self, cfg: dict[str, Any], kind: str, where: str):
        g = cfg.get("gps", {}) or {}
        self.kind = kind
        self.where = where
        self.max_age_s = float(g.get("max_age_s", 10))
        self.every_s = float(g.get("track_every_s", 10))
        self.min_move_m = float(g.get("track_min_move_m", 25))
        self.idle_s = float(g.get("track_idle_s", 60))
        self._fix: Fix | None = None
        self._points: list[Fix] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.fixes = 0

    def describe(self) -> str:
        return f"{self.kind} {self.where}"

    def start(self) -> "GpsReader":
        self._thread = threading.Thread(target=self._run, name=f"gps-{self.kind}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def fix(self) -> Fix | None:
        with self._lock:
            f = self._fix
        if f is None or time.time() - f.ts > self.max_age_s:
            return None
        return f

    def track(self) -> list[Fix]:
        with self._lock:
            pts, self._points = self._points, []
        return pts

    def status(self) -> dict[str, Any]:
        f = self.fix()
        return {"source": self.describe(), "fix": f.to_dict() if f else None, "error": self.error}

    def _new_fix(self, f: Fix) -> None:
        with self._lock:
            last = self._points[-1] if self._points else self._last_point
            self._fix = f
            self.fixes += 1
            if last is None or (f.ts - last.ts >= self.every_s and distance_m(last, f) >= self.min_move_m) or f.ts - last.ts >= self.idle_s:
                self._points.append(f)
                self._last_point = f

    _last_point: Fix | None = None

    def _run(self) -> None:
        try:
            if self.kind == "gpsd":
                self._run_gpsd()
            else:
                self._run_serial()
        except Exception as exc:  # noqa: BLE001 - a GPS problem must never end a session
            self.error = f"{type(exc).__name__}: {exc}"

    def _run_gpsd(self) -> None:
        host, _, port = self.where.rpartition(":")
        while not self._stop.is_set():
            try:
                with socket.create_connection((host, int(port)), timeout=3) as s:
                    s.sendall(b'?WATCH={"enable":true,"json":true}\n')
                    s.settimeout(1.0)
                    buf = b""
                    while not self._stop.is_set():
                        try:
                            chunk = s.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._gpsd_line(line)
            except OSError as exc:
                self.error = f"gpsd: {exc}"
                self._stop.wait(2)

    def _gpsd_line(self, line: bytes) -> None:
        try:
            m = json.loads(line)
        except ValueError:
            return
        if m.get("class") != "TPV" or int(m.get("mode", 0)) < 2 or "lat" not in m or "lon" not in m:
            return
        self._new_fix(Fix(int(time.time()), float(m["lat"]), float(m["lon"]), m.get("altMSL", m.get("alt")), m.get("speed"),
                          "3d" if int(m.get("mode", 0)) >= 3 else "2d", "gpsd"))

    def _run_serial(self) -> None:
        fd = os.open(self.where, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0                                   # iflag: raw
            attrs[1] = 0                                   # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0                                   # lflag: no echo, not canonical
            attrs[4] = attrs[5] = termios.B9600            # ignored by USB ACM, needed by real UARTs
            try:
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            except termios.error:
                pass                                       # a pty in tests, or a device that refuses: read anyway
            nmea = NmeaState("serial")
            buf = b""
            while not self._stop.is_set():
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    self._stop.wait(0.2)
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    f = nmea.feed(line.decode("ascii", "replace"))
                    if f is not None:
                        self._new_fix(f)
                if len(buf) > 4096:
                    buf = b""
        finally:
            os.close(fd)


class NoGps:
    kind = "none"
    error = None

    def __init__(self, why: str):
        self.why = why

    def describe(self) -> str:
        return f"none ({self.why})"

    def start(self) -> "NoGps":
        return self

    def stop(self) -> None:
        pass

    def fix(self) -> Fix | None:
        return None

    def track(self) -> list[Fix]:
        return []

    def status(self) -> dict[str, Any]:
        return {"source": self.describe(), "fix": None, "error": None}


def open_gps(cfg: dict[str, Any]) -> GpsReader | NoGps:
    """Choose a source without disturbing anyone; never raises."""
    g = cfg.get("gps", {}) or {}
    mode = str(g.get("source", "auto")).lower()
    if mode == "none":
        return NoGps("disabled in config")
    host, port = str(g.get("gpsd_host", "127.0.0.1")), int(g.get("gpsd_port", 2947))
    if mode in ("auto", "gpsd") and gpsd_available(host, port):
        return GpsReader(cfg, "gpsd", f"{host}:{port}")
    if mode == "gpsd":
        return NoGps(f"gpsd not reachable at {host}:{port}")
    dev = find_device(cfg)
    if dev is None:
        return NoGps("no GPS device found")
    users = device_users(dev)
    if users:
        return NoGps(f"{dev} is in use by {'; '.join(users)}; run gpsd to share it")
    return GpsReader(cfg, "serial", dev)
