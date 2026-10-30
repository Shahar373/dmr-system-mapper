"""IQ sources and receiver probing.

``IQSource`` is the only thing the survey and the channelizer see: tune, read a block of
complex samples, close. Three implementations:

* ``SoapySource``: the real receiver through SoapySDR (SoapySDRPlay3 for the RSP1B);
* ``FileSource``: replay of a raw IQ recording (cf32 / cs16), for development without hardware;
* ``SyntheticSource``: FM-modulated test carriers on a noise floor, for unit tests.

``Backend`` bundles ``probe()`` (is a receiver present and free?) with ``open()``; the daemon
only ever talks to a backend, so tests inject a synthetic one.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np


@dataclass
class SdrStatus:
    status: str                     # present|absent|busy|error|unknown
    driver: str | None = None
    serial: str | None = None
    holder: str | None = None       # best guess at the process holding the receiver
    detail: str | None = None
    checked: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def short(self) -> str:
        if self.status == "present":
            return f"receiver ready ({self.driver}{' ' + self.serial if self.serial else ''})"
        if self.status == "busy":
            return f"receiver busy{': ' + self.holder if self.holder else ''}"
        if self.status == "absent":
            return "no receiver connected"
        return f"receiver {self.status}: {self.detail or ''}".strip()


class IQSource(Protocol):
    sample_rate: float
    center_hz: float

    def tune(self, center_hz: float) -> None: ...
    def read(self, n: int) -> np.ndarray: ...
    def close(self) -> None: ...
    def describe(self) -> str: ...


class Backend(Protocol):
    def probe(self) -> SdrStatus: ...
    def open(self) -> IQSource: ...


# -- synthetic ----------------------------------------------------------------------------------
@dataclass
class SynthCarrier:
    freq_hz: float
    snr_db: float = 30.0
    audio: np.ndarray | None = None      # float32 in -1..1 at audio_rate; None = unmodulated tone-free carrier
    audio_rate: float = 48000.0
    deviation_hz: float = 2500.0         # peak deviation for |audio| = 1
    phase: float = 0.0
    audio_pos: float = 0.0


class SyntheticSource:
    """Carriers FM-modulated with the given audio on a flat noise floor (unit power per sample).
    Deterministic given ``seed``; generated on demand so any amount can be read."""

    def __init__(self, sample_rate: float = 2_400_000.0, center_hz: float = 165_000_000.0,
                 carriers: list[SynthCarrier] | None = None, seed: int = 1, noise_db: float = -20.0):
        self.sample_rate = float(sample_rate)
        self.center_hz = float(center_hz)
        self.carriers = carriers or []
        self._rng = np.random.default_rng(seed)
        self._noise_amp = 10 ** (noise_db / 20)
        self.closed = False
        self.reads = 0

    def tune(self, center_hz: float) -> None:
        self.center_hz = float(center_hz)

    def describe(self) -> str:
        return f"synthetic {self.sample_rate / 1e6:.1f} MS/s, {len(self.carriers)} carriers"

    def read(self, n: int) -> np.ndarray:
        self.reads += 1
        out = (self._rng.standard_normal(n) + 1j * self._rng.standard_normal(n)).astype(np.complex64) * (self._noise_amp / np.sqrt(2))
        half = self.sample_rate / 2
        for c in self.carriers:
            off = c.freq_hz - self.center_hz
            if abs(off) > half:
                continue
            amp = self._noise_amp * 10 ** (c.snr_db / 20) * np.sqrt(12_500 / self.sample_rate)  # SNR referred to a 12.5 kHz channel
            if c.audio is None:
                inst = np.full(n, 2 * np.pi * off / self.sample_rate, dtype=np.float64)
            else:
                idx = c.audio_pos + np.arange(n) * (c.audio_rate / self.sample_rate)
                a = c.audio[(idx.astype(np.int64)) % len(c.audio)].astype(np.float64)
                c.audio_pos = (idx[-1] + c.audio_rate / self.sample_rate) % len(c.audio)
                inst = 2 * np.pi * (off + c.deviation_hz * a) / self.sample_rate
            # float64 throughout: a float32 cumsum over a block loses ~0.01 rad per sample, which
            # the discriminator turns into kilohertz of noise
            ph = c.phase + np.cumsum(inst, dtype=np.float64)
            c.phase = float(ph[-1] % (2 * np.pi))
            out += (amp * np.exp(1j * ph)).astype(np.complex64)
        return out

    def close(self) -> None:
        self.closed = True


class SyntheticBackend:
    def __init__(self, source: SyntheticSource | None = None, status: str = "present", holder: str | None = None):
        self.source = source or SyntheticSource()
        self.status = status
        self.holder = holder
        self.opened = 0

    def probe(self) -> SdrStatus:
        return SdrStatus(self.status, "synthetic", None, self.holder, None, int(time.time()))

    def open(self) -> IQSource:
        if self.status != "present":
            raise RuntimeError(f"receiver {self.status}")
        self.opened += 1
        self.source.closed = False
        return self.source


# -- file replay ----------------------------------------------------------------------------------
class FileSource:
    """Loops over a raw IQ file. ``cf32``: interleaved float32 I/Q; ``cs16``: interleaved int16."""

    def __init__(self, path: str | Path, sample_rate: float, center_hz: float, fmt: str = "cf32"):
        self.path = Path(path)
        self.sample_rate = float(sample_rate)
        self.center_hz = float(center_hz)
        self.fmt = fmt
        self._dtype = np.float32 if fmt == "cf32" else np.int16
        self._item = np.dtype(self._dtype).itemsize * 2
        self._size = self.path.stat().st_size // self._item
        if self._size == 0:
            raise ValueError(f"{self.path}: empty IQ file")
        self._pos = 0
        self._fh = self.path.open("rb")

    def tune(self, center_hz: float) -> None:
        pass   # a recording cannot retune: center_hz stays the recording's, and callers must use it

    def describe(self) -> str:
        return f"replay {self.path.name} ({self._size / self.sample_rate:.1f} s at {self.sample_rate / 1e6:.2f} MS/s)"

    def read(self, n: int) -> np.ndarray:
        out = np.empty(n, dtype=np.complex64)
        got = 0
        while got < n:
            if self._pos >= self._size:
                self._pos = 0
            take = min(n - got, self._size - self._pos)
            self._fh.seek(self._pos * self._item)
            raw = np.frombuffer(self._fh.read(take * self._item), dtype=self._dtype)
            if self.fmt == "cs16":
                raw = raw.astype(np.float32) / 32768.0
            out[got:got + take] = raw[0::2] + 1j * raw[1::2]
            got += take
            self._pos += take
        time.sleep(n / self.sample_rate * 0.5)   # pace a little so a replay session behaves like radio
        return out

    def close(self) -> None:
        self._fh.close()


class FileBackend:
    def __init__(self, cfg: dict[str, Any]):
        r = cfg.get("sdr", {}).get("replay", {}) or {}
        self.path = Path(str(r.get("path") or ""))
        self.fmt = str(r.get("format", "cf32"))
        self.center_hz = float(r.get("center_hz") or 0)
        self.sample_rate = float(cfg.get("sdr", {}).get("sample_rate_hz", 2_000_000))

    def probe(self) -> SdrStatus:
        if not self.path.is_file():
            return SdrStatus("absent", "file", None, None, f"replay file not found: {self.path}", int(time.time()))
        return SdrStatus("present", "file", self.path.name, None, None, int(time.time()))

    def open(self) -> IQSource:
        return FileSource(self.path, self.sample_rate, self.center_hz, self.fmt)


# -- SoapySDR ---------------------------------------------------------------------------------------
def device_args_string(args: dict[str, Any]) -> str:
    """{"driver": "sdrplay", "serial": "X"} → "driver=sdrplay,serial=X" (driver first, empty values dropped)."""
    keys = sorted(args, key=lambda k: (k != "driver", k))
    return ",".join(f"{k}={args[k]}" for k in keys if str(args[k]) != "" and k != "label")


class SoapySource:
    def __init__(self, cfg: dict[str, Any], device_args: dict[str, str]):
        import SoapySDR  # type: ignore[import-not-found]
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX  # type: ignore[import-not-found]

        self._soapy = SoapySDR
        scfg = cfg.get("sdr", {}) or {}
        self.sample_rate = float(scfg.get("sample_rate_hz", 2_000_000))
        self.center_hz = 0.0
        # Device() must get the "key=value,..." string: with a dict, some SoapySDR Python builds
        # (the Pi's, Debian trixie) fail with "make() no match" although enumerate() finds the device
        # with the same dict. The surveyor hit the same thing (capture/device.py).
        self.dev = SoapySDR.Device(device_args_string(device_args))
        try:
            rates = [float(r) for r in self.dev.listSampleRates(SOAPY_SDR_RX, 0)]
        except Exception:  # noqa: BLE001
            rates = []
        if rates and self.sample_rate not in rates:
            # e.g. the SDRplay driver offers 2, 2.048, 3 ... MS/s but not 2.4: take the closest it has
            from .channelizer import pick_decimation
            usable = []
            for r in rates:
                try:
                    pick_decimation(r)
                    usable.append(r)
                except ValueError:
                    pass
            if not usable:
                raise RuntimeError(f"none of the driver's sample rates {rates} can be channelised")
            self.requested_rate = self.sample_rate
            self.sample_rate = min(usable, key=lambda r: abs(r - self.requested_rate))
        self.dev.setSampleRate(SOAPY_SDR_RX, 0, self.sample_rate)
        gain = scfg.get("gain", {}) or {}
        if gain.get("agc"):
            self.dev.setGainMode(SOAPY_SDR_RX, 0, True)
        else:
            self.dev.setGainMode(SOAPY_SDR_RX, 0, False)
            for name, key in (("IFGR", "ifgr"), ("RFGR", "lna_state")):
                if key in gain and name in list(self.dev.listGains(SOAPY_SDR_RX, 0)):
                    self.dev.setGain(SOAPY_SDR_RX, 0, name, float(gain[key]))
        self.stream = self.dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.dev.activateStream(self.stream)
        self._mtu = int(self.dev.getStreamMTU(self.stream))
        self._buf = np.empty(self._mtu, dtype=np.complex64)
        self.overflows = 0

    def describe(self) -> str:
        return f"{self.dev.getDriverKey()} {self.dev.getHardwareKey()} at {self.sample_rate / 1e6:.2f} MS/s"

    def tune(self, center_hz: float) -> None:
        from SoapySDR import SOAPY_SDR_RX  # type: ignore[import-not-found]
        self.dev.setFrequency(SOAPY_SDR_RX, 0, float(center_hz))
        self.center_hz = float(center_hz)

    def read(self, n: int) -> np.ndarray:
        out = np.empty(n, dtype=np.complex64)
        got = 0
        timeout_us = int(1_000_000 * max(0.5, 2 * n / self.sample_rate))
        while got < n:
            want = min(self._mtu, n - got)
            sr = self.dev.readStream(self.stream, [self._buf], want, timeoutUs=timeout_us)
            if sr.ret > 0:
                out[got:got + sr.ret] = self._buf[:sr.ret]
                got += sr.ret
            elif sr.ret == -4:   # SOAPY_SDR_OVERFLOW
                self.overflows += 1
            elif sr.ret == -1:   # SOAPY_SDR_TIMEOUT
                raise TimeoutError("receiver stream timed out")
            elif sr.ret < 0:
                raise RuntimeError(f"receiver stream error {sr.ret}")
        return out

    def close(self) -> None:
        try:
            self.dev.deactivateStream(self.stream)
            self.dev.closeStream(self.stream)
        finally:
            self.dev = None


class SoapyBackend:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        scfg = cfg.get("sdr", {}) or {}
        self.driver = str(scfg.get("driver", "sdrplay"))
        devs = scfg.get("devices") or [{}]
        self.serial = str((devs[0] or {}).get("serial") or "")

    def _args(self) -> dict[str, str]:
        args = {"driver": self.driver}
        if self.serial:
            args["serial"] = self.serial
        return args

    def probe(self) -> SdrStatus:
        """Non-invasive check: never opens the device. Opening it, even for a moment, could make a
        program that starts at that instant fail, and the receiver is shared. The device is opened
        only when a session starts (``open``)."""
        now = int(time.time())
        try:
            import SoapySDR  # type: ignore[import-not-found]
        except ImportError as exc:
            return SdrStatus("error", self.driver, None, None, f"SoapySDR python bindings not importable: {exc}", now)
        try:
            found = SoapySDR.Device.enumerate(self._args())
        except Exception as exc:  # noqa: BLE001 - driver errors are strings
            return SdrStatus("error", self.driver, None, None, f"enumerate failed: {exc}"[:200], now)
        users = sdr_users(self.cfg)
        holder = "; ".join(users) if users else None
        if found:
            serial = str(dict(found[0]).get("serial", "")) or (self.serial or None)
            return SdrStatus("present", self.driver, serial, holder, "another SDR program is running" if holder else None, now)
        if usb_present(self.cfg):
            # plugged in, but the driver does not offer it: the SDRplay API hides a device that
            # another program has selected
            return SdrStatus("busy", self.driver, self.serial or None, holder,
                             "connected, but the driver does not offer it: another program is using it", now)
        return SdrStatus("absent", self.driver, self.serial or None, None, None, now)

    def open(self) -> IQSource:
        """Open exactly the device the driver lists (its full argument set, as SoapySDRUtil does):
        a bare ``driver=sdrplay`` can fail to match on some SoapySDRPlay builds."""
        import SoapySDR  # type: ignore[import-not-found]

        found = [dict(f) for f in SoapySDR.Device.enumerate(self._args())]
        if not found:
            users = sdr_users(self.cfg)
            raise RuntimeError("the driver lists no free receiver"
                               + (f"; in use by {'; '.join(users)}" if users else "; another program may have it open"))
        last: Exception | None = None
        for args in found:
            try:
                return SoapySource(self.cfg, {k: str(v) for k, v in args.items()})
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise RuntimeError(f"{last} (tried {found})")


SDR_LIBRARIES = ("libsdrplay_api", "libsdrPlaySupport", "librtlsdr", "libairspy", "libhackrf", "libSoapySDR")
SERVICE_NAMES = ("sdrplay_apiService",)


def usb_present(cfg: dict[str, Any]) -> bool:
    """Is a receiver of the configured USB vendor plugged in (sysfs, no device access)?"""
    vendor = str((cfg.get("sdr", {}) or {}).get("usb_vendor", "1df7")).lower()
    root = Path("/sys/bus/usb/devices")
    try:
        for d in root.iterdir():
            try:
                if (d / "idVendor").read_text().strip().lower() == vendor:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _proc_name(pid: int) -> tuple[str, list[str]]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    names = [os.path.basename(a) for a in argv[:2]]          # the program, and the script of an interpreter
    return " ".join(argv)[:60], names


def sdr_users(cfg: dict[str, Any]) -> list[str]:
    """Other processes that use an SDR right now. A process counts when it has an SDR driver library
    loaded (``/proc/<pid>/maps``, readable for our own user) or, for processes of other users, when
    its program or script name is in ``sdr.known_users`` (exact name, not a substring of a path).
    The SDRplay API service itself and our own process tree do not count."""
    known = {str(n).lower() for n in ((cfg.get("sdr", {}) or {}).get("known_users") or []) if n}
    me = os.getpid()
    family = {me, os.getppid()}
    out: list[str] = []
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        if pid in family:
            continue
        try:
            cmd, names = _proc_name(pid)
        except OSError:
            continue
        if not names or any(n in SERVICE_NAMES for n in names):
            continue
        uses = False
        try:
            maps = Path(f"/proc/{pid}/maps").read_text(errors="replace")
            uses = any(lib in maps for lib in SDR_LIBRARIES)
        except OSError:
            uses = any(n.lower() in known or n.lower().rsplit(".", 1)[0] in known for n in names)
        if uses:
            out.append(f"{cmd} (pid {pid})")
    return out


def guess_holder(cfg: dict[str, Any]) -> str | None:
    users = sdr_users(cfg)
    return "; ".join(users) if users else None


def make_backend(cfg: dict[str, Any]) -> Backend:
    kind = str(cfg.get("sdr", {}).get("source", "soapy")).lower()
    if kind == "file":
        return FileBackend(cfg)
    if kind == "synthetic":
        return SyntheticBackend()
    return SoapyBackend(cfg)


def capture_iq(source: IQSource, center_hz: float, seconds: float, path: Path) -> int:
    """Write ``seconds`` of cf32 IQ to ``path`` (a development tool; the collector never keeps IQ)."""
    source.tune(center_hz)
    source.read(int(source.sample_rate * 0.2))   # settle
    total = int(source.sample_rate * seconds)
    written = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        while written < total:
            n = min(total - written, int(source.sample_rate * 0.1))
            fh.write(source.read(n).astype(np.complex64).tobytes())
            written += n
    return written
