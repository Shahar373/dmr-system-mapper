"""Streaming parser for DSD-FME console output (stderr).

The parser is a small state machine over lines:

* a ``Sync:`` line opens a burst and flushes any pending multi-line CSBK;
* payload lines (indented, after ANSI stripping) are matched against an ordered table;
* a few records span several lines (Cap+ channel status + LSN list, adjacent-site lists,
  Tier III broadcasts, grants, XPT status) and are collected in ``pending`` until the next
  burst or a new record starts.

Line formats come from the DSD-FME source (``dmr_csbk.c``, ``dmr_flco.c``, ``dmr_bs.c``) and
from ``samples/``; see CAPTURE.md §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from .events import Burst, Event, Meta, NoSync, Quality, RawOpcode, Structural, Unrecognized
from .rawpdu import csbk_crc_ok, decode_csbk, decode_slco_block, hex_bytes, printed_ids

ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\(.)")

FILENAME_RE = re.compile(r"dsd_fme_(?P<freq>\d{5,12})_(?P<date>\d{8})_(?P<time>\d{6})", re.IGNORECASE)


def strip_ansi(line: str) -> str:
    return ANSI_RE.sub("", line)


def parse_log_filename(name: str, tz: ZoneInfo | None = None) -> tuple[int | None, datetime | None]:
    """``dsd_fme_<freq>_<YYYYmmdd>_<HHMMSS>.log`` → (freq_hz, local start time).

    A frequency below 1 000 000 is taken as kHz (the first sample log uses kHz)."""
    m = FILENAME_RE.search(Path(name).name)
    if not m:
        return None, None
    freq = int(m.group("freq"))
    if freq < 1_000_000:
        freq *= 1000
    start = datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S")
    if tz is not None:
        start = start.replace(tzinfo=tz)
    return freq, start


@dataclass
class ParseContext:
    freq_hz: int | None = None
    start_time: datetime | None = None   # tz-aware; date source for HH:MM:SS lines
    source: str = "dsdfme_log"
    time_is_rf: bool = True              # False: line clocks are decode wall-clock, use offsets
    clock_scale: float = 1.0             # offset multiplier when time_is_rf is False (decode runs faster than RF)
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))


# --------------------------------------------------------------------------------------------
# Regular expressions
# --------------------------------------------------------------------------------------------

SYNC_RE = re.compile(
    r"^(?P<clock>\d{2}:\d{2}:\d{2}) Sync:\s*(?P<pol>[+-]?)(?P<proto>[A-Za-z0-9_]+)\s*(?P<rest>.*)$"
)
SLOT_RE = re.compile(r"(?P<b1>\[?)\s*(?:SLOT1|slot1)\s*\]?\s+(?P<b2>\[?)\s*(?:SLOT2|slot2)\s*\]?\s*(?P<tail>.*)$")
CC_KIND_RE = re.compile(r"\|\s*Color Code=(?P<cc>\d{2}|XX)\s*\|\s*(?P<kind>\S+)?\s*(?P<flags>.*)$")
NO_SYNC_RE = re.compile(r"^(?:\d{2}:\d{2}:\d{2} )?Sync: no sync")

# Capacity Plus
CAP_STATUS_RE = re.compile(
    r"Capacity Plus Channel Status - FL: (?P<fl>\d+) TS: (?P<ts>\d+) RS: (?P<rs>\d+) - Rest LSN: (?P<rest>\d+)"
    r"(?: - (?P<block>\w+) Block)?"
)
CAP_BANK_RE = re.compile(r"Bank (?P<bank>One|Two) (?P<flag>F[0-9A-Fa-f]+) (?P<desc>.+?) Call\(s\)\s*-\s*(?P<rest>.*)$")
LSN_STATE_RE = re.compile(r"LSN (?P<lsn>\d+):\s*(?:ST-\w\s*)?(?P<state>Rest|Idle|Priv|Null|Unk|\d+);")
LSN_TGT_RE = re.compile(r"LSN (?P<lsn>\d+): TGT (?P<tg>\d+);")
CAP_SITE_RE = re.compile(r"SLCO Capacity Plus Site: (?P<site>\d+) - Rest LSN: (?P<rest>\d+) - RS: (?P<rs>\w+)")
CAP_ADJ_HDR_RE = re.compile(r"Capacity Plus Adjacent Sites")
CAP_ADJ_SITE_RE = re.compile(r"Site: (?P<site>\d+) Rest: (?P<rest>\d+);")
CAP_REST_CHANGE_RE = re.compile(r"Cap\+ Rest LSN Change: (?P<rest>\d+); (?P<reason>.*)$")
PREAMBLE_RE = re.compile(
    r"Preamble CSBK - (?P<addr>Individual|Group) (?P<kind>Data|CSBK|Voice) - Source: (?P<src>\d+) - Target: (?P<tgt>\d+)"
    r"(?: - Rest LSN: (?P<rest>\d+))?"
)
CALL_RE = re.compile(r"SLOT (?P<slot>\d) TGT=(?P<tgt>\d+) SRC=(?P<src>\d+)\s*(?P<desc>.*)$")
CALL_REST_RE = re.compile(r"Rest LSN: (?P<rest>\d+)")
CALL_FREE_LCN_RE = re.compile(r"Free LCN (?P<lcn>\d+)")

# Connect Plus
CON_SYS_RE = re.compile(r"SLCO Connect Plus (?P<role>Control|Traffic) Channel - Net ID: (?P<net>\d+) Site ID: (?P<site>\d+)")
CON_ADJ_RE = re.compile(r"Connect Plus Adjacent Sites:\s*(?P<list>.*)$")
CON_VGRANT_RE = re.compile(
    r"Connect Plus (?P<kind>Group|Private|Unknown \w+) Voice Channel Grant; Target: (?P<tgt>\d+); Source: (?P<src>\d+);"
    r" LCN: (?P<lcn>\d+); TS: (?P<ts>\d+);"
)
CON_DGRANT_RE = re.compile(r"Connect Plus Data Channel Grant; Target: (?P<tgt>\d+); LCN: (?P<lcn>\d+); TS: (?P<ts>\d+);")
CON_TERM_RE = re.compile(r"Connect Plus Slot Termination|Con\+ Slot \d Termination")

# Tier III
T3_SYS_RE = re.compile(
    r"(?P<src>C_ALOHA_SYS_PARMS|SLC_C_SYS_PARMS|SLC_P_SYS_PARMS): (?P<model>\w+); Net ID: (?P<net>\d+); Site ID: (?P<site>\d+)(?:\.(?P<sub>\d+))?;"
    r"(?: Cat: (?P<cat>[^;]+);)?(?: Reg Req: (?P<reg>\d+);)?(?: CSC: (?P<csc>\d+);)?(?: Comp CC: (?P<compcc>\d+);)?(?: SYS: (?P<sys>[0-9A-Fa-f]{4});)?"
)
T3_BCAST_RE = re.compile(r"Announcements \(C_BCAST\)\s*(?P<what>.*)$")
T3_BCAST_TYPES = {
    "Ann_WD_TSCC": "ann_wd_tscc", "CallTimer_Parms": "call_timer", "Vote_Now": "vote_now", "Local_Time": "local_time",
    "MassReg": "mass_reg", "Chan_Freq": "chan_freq", "Adjacent_Site": "adjacent_site", "Gen_Site_Params": "gen_site_params",
}
T3_GRANT_RE = re.compile(r"(?P<what>(?:Private|Talkgroup|Broadcast|Duplex Private) (?:Voice|Data) Channel Grant[^(]*)\((?P<code>\w+)\)")
T3_GRANT_DETAIL_RE = re.compile(r"LPCN: (?P<lpcn>\d+); TS: (?P<ts>\d+); LPCN\+TS: (?P<lpcnts>\d+); Target: (?P<tgt>\d+) - Source: (?P<src>\d+)")
T3_FREQ_RE = re.compile(r"Frequency: (?P<mhz>[\d.]+) MHz")
T3_ONLINE_RE = re.compile(r"\b(?P<state>Online|Offline);")
T3_PRI_RE = re.compile(r"CC Pri: (?P<cc>\d+); AC Pri: (?P<ac>\d+);")
T3_LPCN_RE = re.compile(r"\bLPCN: (?P<lpcn>\d+|Null);")
T3_APCN_RE = re.compile(r"APCN: (?P<apcn>\d+);")
T3_RX_RE = re.compile(r"RX Base: (?P<base>\d+); RX Step: (?P<step>\d+); RX Freq: (?P<freq>\d+);")
T3_TX_RE = re.compile(r"TX Base: (?P<base>\d+); TX Step: (?P<step>\d+); TX Freq: (?P<freq>\d+);")
T3_SYSCODE_RE = re.compile(r"\bSYS: (?P<sys>[0-9A-Fa-f]{4});")
T3_ADJ_ID_RE = re.compile(r"^(?P<model>\w+); Net ID: (?P<net>\d+); Site ID: (?P<site>\d+)(?:\.(?P<sub>\d+))?;")
T3_ALOHA_MISC_RE = re.compile(r"Reg Window: |Aloha Mask: |Timers - |C_AHOY|Channel Timing CSBK|Reg: \d+; Backoff:")

# Hytera XPT
XPT_STATUS_RE = re.compile(r"Hytera XPT Site Status - Free LCN: (?P<free>\d+) SN: (?P<sn>\d+)")
XPT_LCN_RE = re.compile(r"LCN (?P<lcn>\d+) - ")
XPT_ADJ_HDR_RE = re.compile(r"Hytera XPT CSBK 0x0B - SN: (?P<sn>\d+)")
XPT_ADJ_SITE_RE = re.compile(r"Site:(?P<site>\d+) Free:(?P<free>\d+);")
XPT_SLCO_RE = re.compile(r"SLCO Hytera XPT - Free LCN (?P<free>\d+) - PRI LCN (?P<pri>\d+) - PRI HASH: (?P<hash>\w+)")
XPT_TUNE_RE = re.compile(r"LSN/TG to tune to|Tune to Freq|Dont Tune Freq")

# Common / Tier II
ACTIVITY_RE = re.compile(r"Activity Update TS1: (?P<ts1>[^;]+); Hash: (?P<h1>\d+);(?: TS2: (?P<ts2>[^;]+); Hash: (?P<h2>\d+);)?")
MOTO_DATA_RE = re.compile(r"Moto Data Channel: (?P<op>[0-9A-Fa-f]+); (?P<bytes>(?:[0-9A-Fa-f]{2} ?)+)")
SLCO_NULL_RE = re.compile(r"^SLCO NULL")
SLCO_CRC_RE = re.compile(r"^SLCO CRC ERR")
SLCO_OTHER_RE = re.compile(r"^SLCO (?:Caltta: (?P<caltta>\w+);|Unknown - (?P<unk>\d+))")
CAPMAX_ACT_RE = re.compile(r"^SLCO Cap Max Activity Update; TGT: (?P<tgt>\d+);")
# with -Z: the bytes of every short LC (after its decoded line) and of every non-idle data burst
SLCO_BLOCK_RE = re.compile(r"^SLCO Completed Block (?P<hex>(?:\[[0-9A-Fa-f]{2}\])+)")
PDU_RE = re.compile(r"^DMR PDU Payload (?P<hex>(?:\[[0-9A-Fa-f]{2}\])+)")
LC_RE = re.compile(r"SLOT (?P<slot>\d) (?P<kind>Protected|Unknown) LC\s+FLCO=(?P<flco>0x[0-9A-Fa-f]+) FID=(?P<fid>0x[0-9A-Fa-f]+)")
ERR_LINE_RE = re.compile(r"FLCO FEC ERR|SB/RC \(FEC ERR\)|Protected Control Signalling Block|Multi Block PDU Message CRC32 ERR|CRC32 ERR|VOICE CACH/EMB ERR|CACH/Burst FEC ERR|\(CRC ERR\)\s*$|Frame Sync Err")
DATA_HDR_RE = re.compile(r"Slot (?P<slot>\d) Data Header - (?P<addr>Indiv|Group) - (?P<delivery>.+?) - Source: (?P<src>\d+) Target: (?P<tgt>\d+)")
DATA_MISC_RE = re.compile(r"^(?:S[Ll][Oo][Tt] \d Data Terminator|UTF8 Text: |UTF16 Text: |ARS; |P25p2 LCH |SAP \d+ |UDP Protocol;|TXI Op:|DATA RESP TGT: (?P<rtgt>\d+); SRC: (?P<rsrc>\d+)|Multi Block PDU|Hytera PDU ENC Header|Motorola Network Interface Service Header|SLOT \d SB/RC)")
IP_MAP_RE = re.compile(r"(?P<role>SRC|DST)\((?P<bits>\d+)\): 0*(?P<radio>\d+); IP: (?P<ip>[\d.]+); Port: (?P<port>\d+);")
LRRP_RE = re.compile(r"LRRP SRC: (?P<src>\d+); (?P<dir>Request from|Response to) TGT: (?P<tgt>\d+);")
LATLON_RE = re.compile(r"Lat: (?P<lat>-?\d+\.\d+) Lon: (?P<lon>-?\d+\.\d+)")

META_RE = re.compile(
    r"^(?:Build Version: (?P<build>.*)|MBElib Version: (?P<mbe>.*)|CODEC2 Support Enabled|Low  Pass Filter|Audio In(?:/Out)? Device: (?P<audio>.*)|Decoding DMR|Pulse Input Device|"
    r"End of (?P<end>.*)|Total audio errors: (?P<aerr>\d+)|Total header errors: (?P<herr>\d+)|Total irrecoverable header errors: (?P<ierr>\d+)|Exiting\.|"
    r"\d{2}:\d{2}:\d{2}\s+P25|[█╚╔║╗╝═]+|Usage: dsd-fme|Decoding Mode:|Using .* as input|Frequency: .* Hz|Gain: |Squelch: )"
)
UNKNOWN_CSBK_RE = re.compile(r"Unknown CSBK|CSBK Opcode|MFID: |Opcode: ")

# Short-LC lines (decoded from the CACH of any burst)
_SLC_PREFIXES = ("SLCO", "SLC_", "Activity Update")

# Which pending records accept which continuation lines
_PENDING_KINDS_WITH_LSN = {"capplus_channel_status", "xpt_site_status"}

# Lines that always START a record: they are never treated as a continuation of a pending one.
_NEW_RECORD_RES = (
    CAP_STATUS_RE, CAP_SITE_RE, CAP_ADJ_HDR_RE, CAP_REST_CHANGE_RE, PREAMBLE_RE, CALL_RE,
    CON_SYS_RE, CON_ADJ_RE, CON_VGRANT_RE, CON_DGRANT_RE, T3_SYS_RE, T3_BCAST_RE, T3_GRANT_RE,
    XPT_STATUS_RE, XPT_ADJ_HDR_RE, XPT_SLCO_RE, ACTIVITY_RE, MOTO_DATA_RE, DATA_HDR_RE, IP_MAP_RE, LRRP_RE,
    CAPMAX_ACT_RE, SLCO_BLOCK_RE, PDU_RE,
)


def starts_record(s: str) -> bool:
    return any(r.search(s) for r in _NEW_RECORD_RES)


def _bucket_hex(bytes_str: str) -> str:
    return " ".join(bytes_str.split()[:3])


class LineParser:
    """Stateful line-by-line parser. Call ``feed(line)`` per line, ``finish()`` at EOF."""

    def __init__(self, ctx: ParseContext):
        self.ctx = ctx
        self._date_anchor: datetime | None = None   # tz-aware datetime of the current day's 00:00:00
        self._last_clock_s: int | None = None       # seconds since midnight of the last sync line
        self._first_clock_s: int | None = None
        self._ts: int | None = None                 # epoch of the last sync line
        self._slot: int | None = None
        self._cc: int | None = None
        self._pending: Structural | None = None
        self._start: datetime | None = None         # resolved start time (cached once)
        self._individual_targets: set[int] = set()  # IDs seen as Individual/Indiv targets (radios, not TGs)
        self._burst_kind: str | None = None         # label of the last burst (CSBK, VLC, ...)
        self._lines_since_burst = 0                 # payload lines DSD-FME printed for that burst
        self._slco: Structural | None = None        # decoded short LC waiting for its Completed Block line
        self._tier3_lines = 0                       # Tier III system parameters seen in this stream
        self._capplus_lines = 0                     # Cap+ channel status CSBKs seen in this stream
        self._raw_numbering = False                 # DSD-FME switched to raw Net/Site numbers (see _t3_ids)
        self.lines = 0
        self.parsed = 0
        self.unrecognized = 0

    # -- time ----------------------------------------------------------------------------------
    def _start_time(self) -> datetime:
        if self._start is None:
            start = self.ctx.start_time
            if start is None:
                start = datetime.now(tz=self.ctx.tz)
            if start.tzinfo is None:
                start = start.replace(tzinfo=self.ctx.tz)
            self._start = start.astimezone(self.ctx.tz)
        return self._start

    def _resolve_time(self, clock: str) -> int:
        h, m, s = (int(x) for x in clock.split(":"))
        clock_s = h * 3600 + m * 60 + s
        start = self._start_time()
        if not self.ctx.time_is_rf:
            # decode-time clock: only the (scaled) offset from the first line matters
            if self._first_clock_s is None:
                self._first_clock_s = clock_s
            offset = clock_s - self._first_clock_s
            if offset < 0:
                offset += 86400
            self._last_clock_s = clock_s
            return int((start + timedelta(seconds=offset * self.ctx.clock_scale)).timestamp())
        if self._date_anchor is None:
            self._date_anchor = start.replace(hour=0, minute=0, second=0, microsecond=0)
            start_clock_s = start.hour * 3600 + start.minute * 60 + start.second
            # The first line is compared with the start-of-capture clock: a line just after midnight
            # belongs to the next day, a buffered line from just before midnight to the previous one.
            if clock_s < start_clock_s - 3600:
                self._date_anchor += timedelta(days=1)
            elif clock_s > start_clock_s + 3600 and start_clock_s < 3600:
                self._date_anchor -= timedelta(days=1)
            self._first_clock_s = clock_s
        elif self._last_clock_s is not None and clock_s < self._last_clock_s - 3600:
            self._date_anchor += timedelta(days=1)   # midnight rollover
        self._last_clock_s = clock_s
        return int((self._date_anchor + timedelta(seconds=clock_s)).timestamp())

    # -- pending records -------------------------------------------------------------------------
    def _flush(self) -> Iterator[Event]:
        if self._pending is not None:
            ev = self._pending
            self._pending = None
            if ev.kind == "capplus_channel_status":
                self._finalize_channel_status(ev)
            elif ev.kind == "capplus_adjacent" and not ev.payload["sites"]:
                ev.payload["sites"] = []
            yield ev

    def _finalize_channel_status(self, ev: Structural) -> None:
        states = ev.payload.setdefault("lsn_states", {})
        for lsn, state in states.items():
            if state not in ("rest", "idle") and isinstance(state, int):
                # DSD-FME prints "TGT" in Bank lines for group calls too, so the bank cannot tell
                # radios from talkgroups; only an Individual-addressed record can.
                kind = "radio" if state in self._individual_targets else "tg"
                ev.identities.append((kind, state, "lsn_active"))

    def _open(self, kind: str, payload: dict[str, Any], **kw: Any) -> Iterator[Event]:
        yield from self._flush()
        self._pending = Structural(ts=self._ts or 0, kind=kind, payload=payload, slot=self._slot, cc=self._cc, **kw)

    def _event(self, kind: str, payload: dict[str, Any], identities: list[tuple[str, int, str]] | None = None) -> Structural:
        return Structural(ts=self._ts or 0, kind=kind, payload=payload, slot=self._slot, cc=self._cc, identities=identities or [])

    # -- main entry -------------------------------------------------------------------------------
    def feed(self, raw: str) -> Iterator[Event]:
        self.lines += 1
        line = strip_ansi(raw).rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            return
        if self._slco is not None and not SLCO_BLOCK_RE.match(stripped):
            # with -Z the bytes of a short LC follow its decoded line; anything else means there are none
            yield self._slco
            self._slco = None

        if NO_SYNC_RE.match(stripped):
            yield from self._flush()
            self.parsed += 1
            yield NoSync(self._ts)
            return

        m = SYNC_RE.match(stripped)
        if m:
            yield from self._flush()
            yield from self._burst(m)
            return

        if self._ts is None:
            # Nothing can be timestamped before the first Sync line: keep the DSD-FME header as
            # metadata, report anything else (a stream that started mid-record) as unrecognized.
            if mm := META_RE.match(stripped):
                self.parsed += 1
                key = next((k for k, v in mm.groupdict().items() if v is not None), "banner")
                yield Meta(None, key, (mm.groupdict().get(key) or stripped)[:200])
            else:
                self.unrecognized += 1
                yield Unrecognized(None, "(before first sync) " + stripped[:180])
            return

        handled = yield from self._payload(stripped)
        if not stripped.startswith(_SLC_PREFIXES):
            self._lines_since_burst += 1     # short LCs come from the CACH, not from the burst itself
        if handled:
            self.parsed += 1
        else:
            self.unrecognized += 1
            yield Unrecognized(self._ts, stripped[:200])

    def finish(self) -> Iterator[Event]:
        if self._slco is not None:
            yield self._slco
            self._slco = None
        yield from self._flush()

    # -- burst lines ------------------------------------------------------------------------------
    def _burst(self, m: re.Match[str]) -> Iterator[Event]:
        self.parsed += 1
        self._ts = self._resolve_time(m.group("clock"))
        if m.group("proto").upper() != "DMR":
            yield Burst(self._ts, m.group("pol"), None, None, "OTHER_PROTO", False, m.group(0)[:120])
            return
        rest = m.group("rest")
        slot: int | None = None
        cc: int | None = None
        kind = "ERR"
        crc = False
        sm = SLOT_RE.match(rest)
        tail = rest
        if sm:
            if sm.group("b1"):
                slot = 1
            elif sm.group("b2"):
                slot = 2
            tail = sm.group("tail")
        km = CC_KIND_RE.search(tail)
        if km:
            cc = None if km.group("cc") == "XX" else int(km.group("cc"))
            kind = (km.group("kind") or "----").rstrip()
            crc = "CRC ERR" in km.group("flags") or "FEC ERR" in km.group("flags")
        else:
            crc = "ERR" in tail
            kind = "ERR"
        self._slot = slot
        if cc is not None:
            self._cc = cc
        self._burst_kind = kind
        self._lines_since_burst = 0
        yield Burst(self._ts, m.group("pol"), slot, cc, kind, crc, tail.strip()[:120])

    # -- payload lines ----------------------------------------------------------------------------
    def _payload(self, s: str) -> Iterator[Event]:
        """Yield events for a non-sync line. Returns True (via StopIteration value) when recognised."""
        p = self._pending

        # --- continuation lines for pending multi-line records -------------------------------
        if p is not None and not starts_record(s):
            if p.kind in _PENDING_KINDS_WITH_LSN and (bm := CAP_BANK_RE.search(s)):
                p.payload.setdefault("banks", {})[bm.group("bank").lower()] = {
                    "flag": bm.group("flag"), "desc": bm.group("desc"),
                    "lsn_to_tg": {int(x.group("lsn")): int(x.group("tg")) for x in LSN_TGT_RE.finditer(bm.group("rest"))},
                }
                return True
            if p.kind in _PENDING_KINDS_WITH_LSN and LSN_STATE_RE.search(s) and "TGT" not in s:
                states = p.payload.setdefault("lsn_states", {})
                if p.kind == "xpt_site_status":
                    # "LCN 1 - LSN 01: ...; LSN 02: ...;  LCN 2 - LSN 03: ...": LSNs belong to the LCN before them
                    lcn_spans = [(x.start(), int(x.group("lcn"))) for x in XPT_LCN_RE.finditer(s)]
                    lcns = p.payload.setdefault("lcns", [])
                    lsn_lcn = p.payload.setdefault("lsn_lcn", {})
                    for _, lcn in lcn_spans:
                        if lcn not in lcns:
                            lcns.append(lcn)
                    for x in LSN_STATE_RE.finditer(s):
                        owner = None
                        for pos, lcn in lcn_spans:
                            if pos < x.start():
                                owner = lcn
                        if owner is not None:
                            lsn_lcn[int(x.group("lsn"))] = owner
                for x in LSN_STATE_RE.finditer(s):
                    st = x.group("state")
                    states[int(x.group("lsn"))] = int(st) if st.isdigit() else st.lower()
                return True
            if p.kind == "capplus_adjacent" and CAP_ADJ_SITE_RE.search(s):
                p.payload["sites"].extend(
                    {"site": int(x.group("site")), "rest_lsn": int(x.group("rest"))} for x in CAP_ADJ_SITE_RE.finditer(s)
                )
                return True
            if p.kind == "xpt_adjacent" and (XPT_ADJ_SITE_RE.search(s) or s.startswith("XPT Adjacent")):
                p.payload["sites"].extend(
                    {"site": int(x.group("site")), "free": int(x.group("free"))} for x in XPT_ADJ_SITE_RE.finditer(s)
                )
                return True
            if p.kind == "tier3_bcast":
                if self._t3_detail(p, s):
                    return True
            if p.kind == "grant":
                if gm := T3_GRANT_DETAIL_RE.search(s):
                    p.payload.update(lpcn=int(gm.group("lpcn")), ts=int(gm.group("ts")), lpcn_ts=int(gm.group("lpcnts")),
                                     tgt=int(gm.group("tgt")), src=int(gm.group("src")))
                    p.identities.append(("radio", int(gm.group("src")), "src"))
                    p.identities.append(("tg" if p.payload.get("kind") == "group" else "radio", int(gm.group("tgt")), "tgt"))
                    return True
                if fm := T3_FREQ_RE.search(s):
                    p.payload["freq_hz"] = int(round(float(fm.group("mhz")) * 1_000_000))
                    return True
                if s.startswith("Frequency Not Found") or s.startswith("MBC Channel Grant") or s.startswith("Invalid Channel"):
                    return True
            if p.kind == "lrrp" and LATLON_RE.search(s):
                # a radio's own position: only the fact is kept, never the coordinates (privacy)
                p.payload["has_position"] = True
                return True
            if p.kind in ("tier3_bcast", "grant", "tier3_sys_parms") and (sm := T3_SYSCODE_RE.search(s)):
                p.payload["sys_code"] = sm.group("sys").upper()
                return True

        # --- Capacity Plus -----------------------------------------------------------------------
        if m := CAP_STATUS_RE.search(s):
            self._capplus_lines += 1
            yield from self._open("capplus_channel_status", {
                "fl": int(m.group("fl")), "ts": int(m.group("ts")), "rs": int(m.group("rs")),
                "rest_lsn": int(m.group("rest")), "block": (m.group("block") or "Single").lower(), "lsn_states": {},
            })
            return True
        if m := CAP_SITE_RE.search(s):
            if self._tier3_lines >= 3 and self._tier3_lines > self._capplus_lines:
                # SLCO opcode 0xF on a Tier III (Capacity Max) carrier is a Motorola message that
                # DSD-FME prints as a Cap+ site; keep its bits, not the site number (one stray Tier
                # III line on a Cap+ carrier must not take its sites away, hence the counts)
                self._slco = self._event("capmax_slco", {"opcode": 15, "data": None, "printed_as": s[5:60].strip()})
            else:
                self._slco = self._event("capplus_site", {"site": int(m.group("site")), "rest_lsn": int(m.group("rest")), "rs": m.group("rs")})
            return True
        if CAP_ADJ_HDR_RE.search(s):
            yield from self._open("capplus_adjacent", {"sites": []})
            # sites may follow on the same line (defensive)
            self._pending.payload["sites"].extend(
                {"site": int(x.group("site")), "rest_lsn": int(x.group("rest"))} for x in CAP_ADJ_SITE_RE.finditer(s)
            )
            return True
        if CAP_BANK_RE.search(s) or (LSN_STATE_RE.search(s) and "TGT" not in s):
            return True   # orphan channel-list line (its header was lost); nothing to attach it to
        if m := CAP_REST_CHANGE_RE.search(s):
            yield self._event("capplus_rest_change", {"rest_lsn": int(m.group("rest")), "reason": m.group("reason").strip()})
            return True
        if m := PREAMBLE_RE.search(s):
            src, tgt = int(m.group("src")), int(m.group("tgt"))
            if m.group("addr") == "Individual":
                self._individual_targets.add(tgt)
            ids = [("radio", src, "src"), ("radio" if m.group("addr") == "Individual" else "tg", tgt, "tgt")]
            yield self._event("preamble", {
                "addressing": m.group("addr").lower(), "kind": m.group("kind").lower(), "src": src, "tgt": tgt,
                "rest_lsn": int(m.group("rest")) if m.group("rest") else None,
            }, ids)
            return True
        if m := CALL_RE.search(s):
            desc = m.group("desc")
            if "Call" in desc or "XPT" in desc or "Hytera" in desc:
                src, tgt = int(m.group("src")), int(m.group("tgt"))
                is_group = "Group" in desc or "Broadcast" in desc
                payload: dict[str, Any] = {
                    "slot": int(m.group("slot")), "src": src, "tgt": tgt, "group": is_group,
                    "capplus": "Cap+" in desc, "txi": "TXI" in desc, "xpt": "XPT" in desc, "desc": desc.strip(),
                }
                if rm := CALL_REST_RE.search(desc):
                    payload["rest_lsn"] = int(rm.group("rest"))
                if fm := CALL_FREE_LCN_RE.search(desc):
                    payload["free_lcn"] = int(fm.group("lcn"))
                ids = []
                if src:
                    ids.append(("radio", src, "src"))
                if tgt:
                    ids.append(("tg" if is_group else "radio", tgt, "tgt"))
                yield self._event("call", payload, ids)
                return True

        # --- Connect Plus ------------------------------------------------------------------------
        if m := CON_SYS_RE.search(s):
            self._slco = self._event("conplus_sys", {"role": m.group("role").lower(), "net_id": int(m.group("net")), "site_id": int(m.group("site"))})
            return True
        if m := CON_ADJ_RE.search(s):
            sites = [int(x) for x in re.findall(r"\d+", m.group("list"))]
            yield self._event("conplus_adjacent", {"sites": sites})
            return True
        if m := CON_VGRANT_RE.search(s):
            kind = m.group("kind").lower()
            kind = "group" if kind == "group" else "private" if kind == "private" else "unknown"
            src, tgt = int(m.group("src")), int(m.group("tgt"))
            yield self._event("grant", {
                "variant": "conplus", "kind": kind, "tgt": tgt, "src": src, "lcn": int(m.group("lcn")), "ts": int(m.group("ts")),
            }, [("radio", src, "src"), ("tg" if kind == "group" else "radio", tgt, "tgt")])
            return True
        if m := CON_DGRANT_RE.search(s):
            tgt = int(m.group("tgt"))
            yield self._event("grant", {"variant": "conplus", "kind": "data", "tgt": tgt, "lcn": int(m.group("lcn")), "ts": int(m.group("ts"))},
                              [("radio", tgt, "tgt")])
            return True
        if CON_TERM_RE.search(s):
            return True

        # --- Tier III ----------------------------------------------------------------------------
        if m := T3_SYS_RE.search(s):
            self._tier3_lines += 1
            if "Capacity Max" in s:
                self._raw_numbering = True      # this very line is already printed raw
            net, site, sub = self._t3_ids(int(m.group("net")), int(m.group("site")), int(m.group("sub")) if m.group("sub") else None)
            payload = {
                "source": m.group("src").lower(), "model": m.group("model"), "net_id": net,
                "site_id": site, "sub_site": sub,
                "sys_code": m.group("sys").upper() if m.group("sys") else None, "cat": m.group("cat"),
                "reg": int(m.group("reg")) if m.group("reg") else None, "csc": int(m.group("csc")) if m.group("csc") else None,
            }
            yield from self._open("tier3_sys_parms", payload)
            return True
        if m := T3_BCAST_RE.search(s):
            what = m.group("what")
            a_type = "other"
            for token, name in T3_BCAST_TYPES.items():
                if token in what:
                    a_type = name
                    break
            yield from self._open("tier3_bcast", {"a_type": a_type, "text": what.strip()})
            return True
        if m := T3_GRANT_RE.search(s):
            what = m.group("what")
            kind = "data" if "Data" in what else ("group" if "Talkgroup" in what or "Broadcast" in what else "private")
            yield from self._open("grant", {"variant": "tier3", "kind": kind, "code": m.group("code")})
            return True
        if T3_ALOHA_MISC_RE.search(s):
            return True

        # --- Hytera XPT ---------------------------------------------------------------------------
        if m := XPT_STATUS_RE.search(s):
            yield from self._open("xpt_site_status", {"free_lcn": int(m.group("free")), "sn": int(m.group("sn")), "lsn_states": {}})
            return True
        if m := XPT_ADJ_HDR_RE.search(s):
            yield from self._open("xpt_adjacent", {"sn": int(m.group("sn")), "sites": []})
            self._pending.payload["sites"].extend(
                {"site": int(x.group("site")), "free": int(x.group("free"))} for x in XPT_ADJ_SITE_RE.finditer(s)
            )
            return True
        if m := XPT_SLCO_RE.search(s):
            self._slco = self._event("xpt_slco", {"free_lcn": int(m.group("free")), "pri_lcn": int(m.group("pri")), "pri_hash": m.group("hash")})
            return True
        if XPT_TUNE_RE.search(s):
            return True

        # --- Common / Tier II ----------------------------------------------------------------------
        if m := ACTIVITY_RE.search(s):
            self._slco = self._event("activity_update", {
                "ts1": m.group("ts1").strip(), "hash1": int(m.group("h1")),
                "ts2": m.group("ts2").strip() if m.group("ts2") else None, "hash2": int(m.group("h2")) if m.group("h2") else None,
            })
            return True
        if m := MOTO_DATA_RE.search(s):
            yield self._event("moto_data_channel", {"opcode": m.group("op").upper(), "bytes": m.group("bytes").strip().upper()})
            return True
        if SLCO_NULL_RE.match(s):
            yield Quality(self._ts, "slco_null")
            return True
        if SLCO_CRC_RE.match(s):
            yield Quality(self._ts, "slco_crc")
            return True
        if m := CAPMAX_ACT_RE.match(s):
            tgt = int(m.group("tgt"))
            self._slco = self._event("capmax_activity", {"tgt": tgt}, [("tg", tgt, "activity")] if tgt else [])
            return True
        if m := SLCO_OTHER_RE.match(s):
            self._slco = self._event("slco_other", {"text": s[:80]})
            return True
        if m := SLCO_BLOCK_RE.match(s):
            self._slco_block(hex_bytes(m.group("hex")))
            yield Quality(self._ts, "slco_block")
            return True
        if m := PDU_RE.match(s):
            yield from self._raw_pdu(hex_bytes(m.group("hex")))   # may still correct the pending record
            yield from self._flush()
            return True
        if m := LC_RE.search(s):
            if "CRC ERR" in s:
                yield Quality(self._ts, "crc_err")   # corrupt LC: its FLCO/FID are not trustworthy
                return True
            yield self._event("protected_lc", {"slot": int(m.group("slot")), "lc": m.group("kind").lower(),
                                               "flco": m.group("flco").upper(), "fid": m.group("fid").upper()})
            return True
        if m := DATA_HDR_RE.search(s):
            src, tgt = int(m.group("src")), int(m.group("tgt"))
            if m.group("addr") == "Indiv":
                self._individual_targets.add(tgt)
            yield self._event("data_header", {
                "slot": int(m.group("slot")), "addressing": m.group("addr").lower(), "delivery": m.group("delivery").strip(),
                "src": src, "tgt": tgt, "response_requested": "Response Requested" in s,
            }, [("radio", src, "data"), ("radio" if m.group("addr") == "Indiv" else "tg", tgt, "data")])
            return True
        if m := IP_MAP_RE.search(s):
            radio = int(m.group("radio"))
            yield self._event("ip_map", {"role": m.group("role").lower(), "radio": radio, "ip": m.group("ip"), "port": int(m.group("port"))},
                              [("radio", radio, "data")])
            return True
        if m := LRRP_RE.search(s):
            src, tgt = int(m.group("src")), int(m.group("tgt"))
            yield from self._open("lrrp", {"src": src, "tgt": tgt, "direction": "request" if m.group("dir").startswith("Request") else "response",
                                           "has_position": False}, identities=[("radio", src, "lrrp"), ("radio", tgt, "lrrp")])
            return True
        if LATLON_RE.search(s):
            return True   # position without a preceding LRRP record: nothing to attach to
        if m := DATA_MISC_RE.match(s):
            if m.group("rtgt"):
                yield self._event("data_response", {"tgt": int(m.group("rtgt")), "src": int(m.group("rsrc")), "text": s[:80]},
                                  [("radio", int(m.group("rsrc")), "data"), ("radio", int(m.group("rtgt")), "data")])
            return True
        if ERR_LINE_RE.search(s):
            yield Quality(self._ts, "crc_err")
            return True
        if m := META_RE.match(s):
            key = next((k for k, v in m.groupdict().items() if v is not None), "banner")
            yield Meta(self._ts, key, (m.groupdict().get(key) or s)[:200])
            return True
        if UNKNOWN_CSBK_RE.search(s):
            yield self._event("unknown_csbk", {"text": s[:120]})
            return True
        return False

    # -- Tier III numbering ---------------------------------------------------------------------------
    def _t3_ids(self, net: int, site: int, sub: int | None) -> tuple[int, int, int | None]:
        """DSD-FME prints Tier III identities as ``Net ID: net+1; Site ID: 1.site+1`` (``-D 0``,
        see rawpdu.printed_ids), but once it has decoded one Motorola (FID 0x10) Aloha or broadcast
        it switches the whole process to raw ``Net ID: net; Site ID: site``. Raw prints after that
        point are converted back, so one site keeps one number."""
        if sub is None and self._raw_numbering:
            return printed_ids(net, site)
        return net, site, sub

    @staticmethod
    def _set_ids(payload: dict[str, Any], ident: dict[str, Any], prefix: str = "") -> None:
        net, site, sub = printed_ids(ident["net"], ident["site"])
        payload[prefix + "model"] = ident["model"]
        payload[prefix + "net_id"], payload[prefix + "site_id"], payload[prefix + "sub_site"] = net, site, sub
        payload["sys_code"] = ident["sys_code"]

    # -- raw bytes (-Z) ------------------------------------------------------------------------------
    def _slco_block(self, b: bytes) -> None:
        """Attach the short LC's own bits to the event DSD-FME's decoded line produced."""
        d = decode_slco_block(b)
        if d is None:
            return
        target = self._slco
        if target is not None and target.kind in ("capmax_slco", "slco_other"):
            target.payload["opcode"] = d["opcode"]
            target.payload["data"] = d["data"]
        p = self._pending
        if d.get("own") and target is None and p is not None and p.kind == "tier3_sys_parms" and str(p.payload.get("source", "")).startswith("slc_"):
            self._set_ids(p.payload, d["own"])      # the bits settle the numbering

    def _raw_pdu(self, b: bytes) -> Iterator[Event]:
        """``DMR PDU Payload``: only CSBKs are read, and only what DSD-FME printed nothing for."""
        if self._burst_kind != "CSBK" or len(b) != 12:
            return
        if not csbk_crc_ok(b):
            yield Quality(self._ts, "pdu_crc_err")
            return
        d = decode_csbk(b)
        key = f"FID {d['fid']:02X} opcode {d['opcode']:02X}"
        p = self._pending
        if d.get("kind") in ("aloha", "adjacent_site") or (d["opcode"] == 40 and d.get("a_type") == 2):
            if d["fid"] == 0x10:
                self._raw_numbering = True      # what DSD-FME prints from now on is raw
            if d.get("kind") == "aloha" and p is not None and p.kind == "tier3_sys_parms" and p.payload.get("source") == "c_aloha_sys_parms":
                self._set_ids(p.payload, d["own"])
            if d.get("kind") == "adjacent_site" and p is not None and p.kind == "tier3_bcast" and p.payload.get("a_type") == "adjacent_site":
                # DSD-FME leaves the neighbour's channel out when "connection information" is not
                # available; on Capacity Max that channel is the neighbour's LCN
                self._set_ids(p.payload, d["neighbor"], "adj_")
                if d["channel"] not in (0, 0xFFF):
                    p.payload["lpcn"] = d["channel"]
                p.payload["own_sys_code"] = d["own"]["sys_code"]
        if d.get("kind") == "capmax_channel_update":
            for u in d["updates"]:
                ids = [("tg", u["tg"], "tgt")] if d["mode"] == "open" else []
                yield self._event("capmax_channel_update", {"mode": d["mode"], "lcn": u["lcn"], "slot": u["slot"], "tg": u["tg"]}, ids)
            yield RawOpcode(self._ts, key, True, "")
        elif self._lines_since_burst == 0 and not d["pf"]:
            # DSD-FME printed nothing for this CSBK and it is not one read here either
            yield RawOpcode(self._ts, key, False, " ".join(f"{x:02X}" for x in b))
        else:
            yield RawOpcode(self._ts, key, True, "")

    # -- Tier III broadcast continuation ------------------------------------------------------------
    def _t3_detail(self, p: Structural, s: str) -> bool:
        hit = False
        if m := T3_ONLINE_RE.search(s):
            p.payload["online"] = m.group("state") == "Online"
            hit = True
        if m := T3_PRI_RE.search(s):
            p.payload["cc_pri"] = int(m.group("cc"))
            p.payload["ac_pri"] = int(m.group("ac"))
            hit = True
        if m := T3_LPCN_RE.search(s):
            p.payload["lpcn"] = None if m.group("lpcn") == "Null" else int(m.group("lpcn"))
            hit = True
        if m := T3_APCN_RE.search(s):
            p.payload["apcn"] = int(m.group("apcn"))
            hit = True
        if m := T3_RX_RE.search(s):
            p.payload["rx_freq_hz"] = int(m.group("freq"))
            p.payload["rx_base_hz"] = int(m.group("base"))
            p.payload["rx_step_hz"] = int(m.group("step"))
            hit = True
        if m := T3_TX_RE.search(s):
            p.payload["tx_freq_hz"] = int(m.group("freq"))
            hit = True
        if m := T3_ADJ_ID_RE.match(s):
            net, site, sub = self._t3_ids(int(m.group("net")), int(m.group("site")), int(m.group("sub")) if m.group("sub") else None)
            p.payload["adj_model"] = m.group("model")
            p.payload["adj_net_id"] = net
            p.payload["adj_site_id"] = site
            p.payload["adj_sub_site"] = sub
            hit = True
        if m := T3_SYSCODE_RE.search(s):
            p.payload["sys_code"] = m.group("sys").upper()
            hit = True
        if "Active Connection Information Not Available" in s or s.startswith("Res: "):
            hit = True
        return hit


def parse_lines(lines: Iterable[str], ctx: ParseContext) -> Iterator[Event]:
    """Parse an iterable of raw DSD-FME lines into events (streaming)."""
    parser = LineParser(ctx)
    for raw in lines:
        yield from parser.feed(raw)
    yield from parser.finish()


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)
