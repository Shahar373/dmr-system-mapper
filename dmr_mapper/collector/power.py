"""Power awareness. The Pi runs in the field on a battery; a power guard service already logs the
input voltage and leaves a JSON state file. We read that file when it exists (its path is a
config value, never hardcoded), fall back to ``vcgencmd get_throttled``, and derive how many
DSD-FME instances and how long a session the supply can afford. We never stop other services.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# vcgencmd get_throttled bits (Raspberry Pi firmware)
UNDERVOLT_NOW = 0x1
FREQ_CAPPED_NOW = 0x2
THROTTLED_NOW = 0x4
UNDERVOLT_EVER = 0x10000


@dataclass
class PowerState:
    source: str = "unknown"              # file|vcgencmd|unknown
    supply_full: bool | None = None      # the supply negotiated the full current (Pi 5: 5 A)
    capped: bool | None = None           # CPU frequency capped by the power guard or firmware
    volts: float | None = None
    throttled: int | None = None
    undervolt_now: bool = False
    undervolt_ever: bool = False
    checked: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def on_battery(self) -> bool:
        """Conservative reading: anything that is not a confirmed full supply counts as battery."""
        return self.supply_full is not True

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d.pop("raw", None)
        d["on_battery"] = self.on_battery
        return d

    def short(self) -> str:
        parts = ["mains" if self.supply_full else ("battery" if self.supply_full is False else "supply?")]
        if self.volts is not None:
            parts.append(f"{self.volts:.2f}V")
        if self.capped:
            parts.append("cpu capped")
        if self.undervolt_now:
            parts.append("UNDERVOLTAGE")
        return " ".join(parts)


_KEY_VOLT = re.compile(r"(ext5v|volt|vin|input)", re.IGNORECASE)
_KEY_THROTTLED = re.compile(r"throttl", re.IGNORECASE)
_KEY_CAP = re.compile(r"(cap|powersave|saving)", re.IGNORECASE)
_KEY_SUPPLY = re.compile(r"(max_current|supply|full_supply|negotiat|ma$)", re.IGNORECASE)


def _walk(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_walk(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out.append((prefix, obj))
    return out


def parse_state_file(data: dict[str, Any]) -> PowerState:
    """Best-effort reading of the power guard's JSON. We only rely on the *meaning* of keys
    (voltage, throttled bits, a cap flag, a supply/current figure), not on exact names, so a
    guard upgrade that renames a field degrades to 'unknown' instead of crashing."""
    ps = PowerState(source="file", raw=data)
    for key, value in _walk(data):
        leaf = key.rsplit(".", 1)[-1]
        if _KEY_THROTTLED.search(leaf) and ps.throttled is None:
            ps.throttled = _parse_throttled(value)
        elif _KEY_VOLT.search(leaf) and isinstance(value, (int, float)) and ps.volts is None and 3.0 <= float(value) <= 6.5:
            ps.volts = float(value)
        elif _KEY_CAP.search(leaf) and isinstance(value, bool) and ps.capped is None:
            ps.capped = value
        elif _KEY_CAP.search(leaf) and isinstance(value, (int, float)) and leaf.endswith("mhz") and ps.capped is None:
            ps.capped = float(value) < 2000
        elif _KEY_SUPPLY.search(leaf) and ps.supply_full is None:
            if isinstance(value, bool):
                ps.supply_full = value
            elif isinstance(value, (int, float)):
                ps.supply_full = float(value) >= 5000 if float(value) > 100 else float(value) >= 5.0
            elif isinstance(value, str) and value.strip().lower() in ("full", "5a", "5000", "ok", "true", "yes"):
                ps.supply_full = True
            elif isinstance(value, str) and value.strip().lower() in ("low", "3a", "3000", "battery", "false", "no"):
                ps.supply_full = False
    if ps.throttled is not None:
        ps.undervolt_now = bool(ps.throttled & UNDERVOLT_NOW)
        ps.undervolt_ever = bool(ps.throttled & UNDERVOLT_EVER)
        if ps.capped is None:
            ps.capped = bool(ps.throttled & FREQ_CAPPED_NOW)
    return ps


def _parse_throttled(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"0x([0-9a-fA-F]+)", value)
        if m:
            return int(m.group(1), 16)
        if value.isdigit():
            return int(value)
    return None


def read_vcgencmd() -> PowerState | None:
    exe = shutil.which("vcgencmd")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "get_throttled"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    t = _parse_throttled(out.strip())
    if t is None:
        return None
    ps = PowerState(source="vcgencmd", throttled=t, undervolt_now=bool(t & UNDERVOLT_NOW), undervolt_ever=bool(t & UNDERVOLT_EVER),
                    capped=bool(t & (FREQ_CAPPED_NOW | THROTTLED_NOW)))
    try:
        mc = Path("/proc/device-tree/chosen/power/max_current").read_bytes()
        ps.supply_full = int.from_bytes(mc[:4], "big") >= 5000
    except OSError:
        pass
    return ps


def read_power(cfg: dict[str, Any]) -> PowerState:
    pcfg = cfg.get("power", {}) or {}
    path = pcfg.get("state_file")
    ps: PowerState | None = None
    if path:
        p = Path(path)
        try:
            if p.exists() and time.time() - p.stat().st_mtime < float(pcfg.get("state_max_age_s", 120)):
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    ps = parse_state_file(data)
        except (OSError, ValueError):
            ps = None
    if ps is None:
        ps = read_vcgencmd() or PowerState()
    ps.checked = int(time.time())
    return ps


def budget_for(ps: PowerState, cfg: dict[str, Any]) -> tuple[int, int]:
    """(max DSD-FME instances, default session seconds) the current supply affords."""
    dcfg = cfg.get("decode", {}) or {}
    pcfg = cfg.get("power", {}) or {}
    full_instances = int(dcfg.get("max_instances", 4))
    full_session = int(cfg.get("daemon", {}).get("default_budget_min", 60)) * 60
    if ps.undervolt_now:
        return 1, int(pcfg.get("battery_session_s", 1200))
    if ps.on_battery or ps.capped:
        return max(1, int(pcfg.get("battery_max_instances", 2))), int(pcfg.get("battery_session_s", 1200))
    return full_instances, full_session
