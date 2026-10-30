"""Event types produced by the DSD-FME line parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Burst categories used for observation counters.
VOICE_KINDS = {"VLC", "TLC", "VC1", "VC2", "VC3", "VC4", "VC5", "VC6", "VC1*", "VC*"}
DATA_KIND_PREFIXES = ("DATA", "R12", "R34", "R_1", "R_2", "R12U", "R34U", "R34C", "R12C")
IDLE_KINDS = {"IDLE"}
CSBK_KINDS = {"CSBK", "MBC"}

# Structural event kinds that have not been verified against a real capture yet.
UNTESTED_ON_AIR = {
    "conplus_sys", "conplus_adjacent", "tier3_sys_parms", "tier3_bcast", "xpt_site_status",
    "xpt_adjacent", "xpt_slco", "capplus_rest_change",
    "capmax_channel_update", "capmax_activity", "capmax_slco",
}


@dataclass(slots=True)
class Burst:
    """One ``Sync: +DMR`` line."""

    ts: int
    polarity: str          # '+', '-' or '' (unsigned error line)
    slot: int | None       # active slot 1/2 (bracketed), None when unknown
    cc: int | None         # colour code, None for XX / error lines
    kind: str              # CSBK, VLC, TLC, VCn, IDLE, DATA, R12U, ... or 'ERR'
    crc_err: bool = False
    text: str = ""

    @property
    def category(self) -> str:
        if self.kind in CSBK_KINDS:
            return "csbk"
        if self.kind in VOICE_KINDS:
            return "voice"
        if self.kind in IDLE_KINDS:
            return "idle"
        if self.kind.startswith(DATA_KIND_PREFIXES):
            return "data"
        return "other"


@dataclass(slots=True)
class NoSync:
    ts: int | None


@dataclass(slots=True)
class Structural:
    """A decoded CSBK / short-LC / data record worth keeping (see DESIGN.md §3.2)."""

    ts: int
    kind: str
    payload: dict[str, Any]
    slot: int | None = None
    cc: int | None = None
    identities: list[tuple[str, int, str]] = field(default_factory=list)  # (kind, value, role)


@dataclass(slots=True)
class Quality:
    """A recognised line that only feeds counters (CRC/FEC errors, SLCO NULL, ...)."""

    ts: int | None
    counter: str           # crc_err | slco_null | slco_crc | fec_err | meta


@dataclass(slots=True)
class Meta:
    ts: int | None
    key: str
    value: str


@dataclass(slots=True)
class RawOpcode:
    """A CSBK read from its raw bytes (``DMR PDU Payload``, with -Z): which FID/opcode, and whether
    anything (DSD-FME's text or our own decoder) understood it."""

    ts: int | None
    key: str               # "FID 10 opcode 21"
    understood: bool
    example: str           # the bytes, only when not understood


@dataclass(slots=True)
class Unrecognized:
    ts: int | None
    text: str


Event = Burst | NoSync | Structural | Quality | Meta | RawOpcode | Unrecognized
