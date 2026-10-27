"""Synthetic DSD-FME log generator for correlator tests.

Real captures of several carriers at once do not exist in ``samples/`` (only one receiver, and the
Cap+ network in the sample is gone), so the behavioural and identity evidence is exercised on
generated logs whose line formats match CAPTURE.md §4. Everything here is invented; the point is
that the *relationships* between the carriers are known, so the correlator's output can be checked.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
START = datetime(2026, 8, 1, 10, 0, 0, tzinfo=TZ)


def _clock(t: datetime) -> str:
    return t.strftime("%H:%M:%S")


def _sync(t: datetime, slot: int, cc: int, kind: str) -> str:
    s1, s2 = ("[slot1]", " slot2 ") if slot == 1 else (" slot1 ", "[slot2]")
    return f"{_clock(t)} Sync: +DMR  {s1} {s2} | Color Code={cc:02d} | {kind}\n"


def _lsn_list(rest_lsn: int, active: dict[int, int], n: int = 8) -> str:
    parts = []
    for lsn in range(1, n + 1):
        if lsn == rest_lsn:
            parts.append(f"LSN {lsn:02d}:  Rest;")
        elif lsn in active:
            parts.append(f"LSN {lsn:02d}: {active[lsn]};")
        else:
            parts.append(f"LSN {lsn:02d}:  Idle;")
    return "  " + "  ".join(parts) + "\n"


def capplus_site(
    out_dir: Path, site: int, cc: int, freqs: list[int], minutes: int = 30, hop_every_s: int = 30,
    calls: list[tuple[int, int, int]] | None = None, seed: int = 1, neighbors: list[tuple[int, int]] | None = None,
    start: datetime = START,
) -> list[Path]:
    """One Capacity Plus site with ``len(freqs)`` repeaters (LCN 1..n). The rest channel hops
    LCN→LCN every ``hop_every_s`` seconds; each carrier's log shows the shared channel status,
    and only the carrier that currently IS the rest channel shows its own LSN as Rest.
    ``calls`` = [(offset_s, tg, src)] group calls placed on a non-rest LCN."""
    rnd = random.Random(seed)
    n = len(freqs)
    logs = {f: [] for f in freqs}
    rest_lcn = 1
    t = start
    end = start + timedelta(minutes=minutes)
    calls = sorted(calls or [])
    active: dict[int, int] = {}
    call_until: dict[int, datetime] = {}
    while t < end:
        off = int((t - start).total_seconds())
        if off % hop_every_s == 0 and off > 0:
            rest_lcn = rest_lcn % n + 1
        rest_lsn = 2 * rest_lcn - (off // hop_every_s) % 2
        # calls starting now
        for c_off, tg, src in calls:
            if c_off == off:
                lcn = (rest_lcn % n) + 1   # the next repeater
                lsn = 2 * lcn - 1
                active[lsn] = tg
                call_until[lsn] = t + timedelta(seconds=8)
                logs[freqs[lcn - 1]].append(_sync(t, 1, cc, "VLC  ") + f" SLOT 1 TGT={tg} SRC={src} Cap+ Group Call  Rest LSN: {rest_lsn} \n")
        for lsn, until in list(call_until.items()):
            if t >= until:
                del call_until[lsn]
                active.pop(lsn, None)
        for i, f in enumerate(freqs):
            lcn = i + 1
            slot = 1 + (off % 2)
            if lcn == rest_lcn:
                logs[f].append(_sync(t, slot, cc, "CSBK"))
                logs[f].append(f" Capacity Plus Channel Status - FL: 3 TS: {slot - 1} RS: 0 - Rest LSN: {rest_lsn} - Single Block\n")
                logs[f].append(_lsn_list(rest_lsn, active))
                if off % 4 == 0:
                    logs[f].append(f" SLCO Capacity Plus Site: {site} - Rest LSN: {rest_lsn} - RS: 00\n")
                if neighbors and off % 10 == 5:
                    logs[f].append(_sync(t, slot, cc, "CSBK"))
                    logs[f].append(" Capacity Plus Adjacent Sites\n  " + " ".join(f"Site: {s} Rest: {r};" for s, r in neighbors) + " \n")
            else:
                busy = any(2 * lcn - 1 == l or 2 * lcn == l for l in active)
                if busy:
                    logs[f].append(_sync(t, 1, cc, f"VC{1 + off % 6} "))
                else:
                    logs[f].append(_sync(t, slot, cc, "IDLE "))
                    if off % 4 == 0:
                        logs[f].append(f" SLCO Capacity Plus Site: {site} - Rest LSN: {rest_lsn} - RS: 00\n")
            if rnd.random() < 0.02:
                logs[f].append(" SLCO CRC ERR\n")
        t += timedelta(seconds=1)
    paths = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for f, lines in logs.items():
        p = out_dir / f"dsd_fme_{f}_{start.strftime('%Y%m%d_%H%M%S')}.log"
        p.write_text("Build Version: AW synthetic\n" + "".join(lines), encoding="utf-8")
        paths.append(p)
    return paths


def tier2_carrier(out_dir: Path, freq: int, cc: int, radios: list[int], tgs: list[int], minutes: int = 30,
                  seed: int = 2, start: datetime = START) -> Path:
    """A conventional repeater: idle bursts, activity updates and group calls from ``radios``."""
    rnd = random.Random(seed)
    lines = ["Build Version: AW synthetic\n"]
    t = start
    end = start + timedelta(minutes=minutes)
    while t < end:
        off = int((t - start).total_seconds())
        if off % 45 == 0:
            src, tg = rnd.choice(radios), rnd.choice(tgs)
            lines.append(_sync(t, 1, cc, "VLC  "))
            lines.append(f" SLOT 1 TGT={tg} SRC={src} Group Call  \n")
            lines.append(f" Activity Update TS1: Group Voice; Hash: {tg % 100}; TS2: Idle; Hash: 0;\n")
            for k in range(6):
                lines.append(_sync(t + timedelta(seconds=k + 1), 1, cc, f"VC{k + 1} "))
            lines.append(_sync(t + timedelta(seconds=7), 1, cc, "TLC  "))
            lines.append(f" SLOT 1 TGT={tg} SRC={src} Group Call  \n")
        else:
            lines.append(_sync(t, 1 + off % 2, cc, "IDLE "))
            if off % 8 == 0:
                lines.append(" SLCO NULL \n")
        t += timedelta(seconds=1)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"dsd_fme_{freq}_{start.strftime('%Y%m%d_%H%M%S')}.log"
    p.write_text("".join(lines), encoding="utf-8")
    return p


def tier3_site(out_dir: Path, sys_code: str, net: int, site: int, cc: int, control_freq: int,
               channels: dict[int, int], adjacent: list[int], minutes: int = 5, start: datetime = START) -> Path:
    """A Tier III control channel announcing its system, its channel→frequency table and its
    adjacent sites. ``channels`` = {lpcn: rx_freq_hz}."""
    lines = ["Build Version: AW synthetic\n"]
    t = start
    end = start + timedelta(minutes=minutes)
    i = 0
    while t < end:
        lines.append(_sync(t, 1 + i % 2, cc, "CSBK"))
        kind = i % 4
        if kind == 0:
            lines.append(f" C_ALOHA_SYS_PARMS: Small; Net ID: {net}; Site ID: {site}; SYS: {sys_code};\n")
        elif kind == 1:
            lpcn, freq = list(channels.items())[(i // 4) % len(channels)]
            lines.append(" Announcements (C_BCAST) Announce Logical Channel/Frequency Relationship (Chan_Freq)\n")
            lines.append(f" APCN: {lpcn:04d}; RX Base: 160000000; RX Step: 12500; RX Freq: {freq};\n")
            lines.append(f"             TX Base: 165000000; TX Step: 12500; TX Freq: {freq + 5000000};\n")
        elif kind == 2 and adjacent:
            adj = adjacent[i % len(adjacent)]
            lines.append(" Announcements (C_BCAST) Adjacent Site Information (Adjacent_Site)\n")
            lines.append("  Online; CC Pri: 1; AC Pri: 2; LPCN: 1234;\n")
            lines.append(f"  Small; Net ID: {net}; Site ID: {adj}; SYS: {sys_code};\n")
        else:
            lines.append(f" SLC_C_SYS_PARMS: Small; Net ID: {net}; Site ID: {site}; Reg Req: 1;\n")
        t += timedelta(seconds=1)
        i += 1
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"dsd_fme_{control_freq}_{start.strftime('%Y%m%d_%H%M%S')}.log"
    p.write_text("".join(lines), encoding="utf-8")
    return p


def tier3_traffic(out_dir: Path, sys_code: str, net: int, site: int, cc: int, freq: int, minutes: int = 2,
                  start: datetime = START) -> Path:
    """A Tier III traffic channel that only repeats its short-LC system parameters."""
    lines = ["Build Version: AW synthetic\n"]
    t = start
    for i in range(minutes * 60):
        lines.append(_sync(t, 1 + i % 2, cc, "IDLE "))
        if i % 3 == 0:
            lines.append(f" SLC_P_SYS_PARMS: Small; Net ID: {net}; Site ID: {site};\n")
        t += timedelta(seconds=1)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"dsd_fme_{freq}_{start.strftime('%Y%m%d_%H%M%S')}.log"
    p.write_text("".join(lines), encoding="utf-8")
    return p
