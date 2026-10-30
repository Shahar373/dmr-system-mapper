"""Bit fields of the raw PDUs DSD-FME prints with ``-Z``.

DSD-FME does all the decoding (sync, deinterleaving, FEC); with ``-Z`` it also prints the result
as bytes: ``DMR PDU Payload [..]`` after every non-idle data burst (a CSBK is 10 bytes plus its
CRC-16) and ``SLCO Completed Block [..]`` after every short LC (28 bits, CRC-8, 4 pad bits). Most
of it DSD-FME has already printed as text. What it does not print is read here, from those bytes:

* Motorola Capacity Max channel updates (CSBK FID 0x10, opcodes 0x21 Open Mode and 0x22
  Advantage Mode): which talkgroup is active on which LCN and timeslot. DSD-FME skips them
  (it treats opcodes 32-35 with FID 0x10 as "not the ETSI acknowledgements" and prints nothing).
  Field layout from SDRTrunk (``CapacityMaxOpenModeVoiceChannelUpdate``,
  ``CapacityMaxAdvantageModeVoiceChannelUpdate``).
* the opcode and 24 data bits of every short LC, so an opcode DSD-FME misnames (0xF, printed as
  "Capacity Plus Site" on Capacity Max carriers) or does not know keeps its content.

Nothing here touches samples or symbols: it is bookkeeping on bytes DSD-FME already decoded, and
a CSBK is only used when its own CRC checks out.
"""

from __future__ import annotations

import re
from typing import Any

HEX_BYTE_RE = re.compile(r"\[([0-9A-Fa-f]{2})\]")
CSBK_CRC_MASK = 0xA5A5

# Activity Update activity IDs (ETSI TS 102 361-2 7.1.3.2), named as DSD-FME prints them
ACTIVITY = {0x0: "Idle", 0x2: "Group CSBK", 0x3: "Ind CSBK", 0x8: "Group Voice", 0x9: "Ind Voice", 0xA: "Ind Data",
            0xB: "Group Data", 0xC: "Group Emergency", 0xD: "Ind Emergency"}


def hex_bytes(text: str) -> bytes:
    return bytes(int(h, 16) for h in HEX_BYTE_RE.findall(text))


def field(value: int, nbits: int, start: int, length: int) -> int:
    """Bits ``start .. start+length-1`` (bit 0 = most significant) of an ``nbits`` wide value."""
    return (value >> (nbits - start - length)) & ((1 << length) - 1)


def crc16_ccitt(value: int, nbits: int) -> int:
    """CRC-CCITT as DMR uses it (ETSI TS 102 361-1 B.3.8): x^16+x^12+x^5+1, init 0, inverted."""
    crc = 0
    for i in range(nbits - 1, -1, -1):
        bit = (value >> i) & 1
        if ((crc >> 15) & 1) ^ bit:
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF
        else:
            crc = (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


def crc8(value: int, nbits: int) -> int:
    """CRC-8, x^8+x^2+x+1, init 0 (ETSI TS 102 361-1 B.3.7)."""
    crc = 0
    for i in range(nbits - 1, -1, -1):
        bit = (value >> i) & 1
        if ((crc >> 7) & 1) ^ bit:
            crc = ((crc << 1) ^ 0x07) & 0xFF
        else:
            crc = (crc << 1) & 0xFF
    return crc


def address_hash(address: int) -> int:
    """The 8-bit hashed address an Activity Update carries for a talkgroup or radio: CRC-8 of the
    16 least significant bits of the 24-bit address (as DSD-FME computes the Hytera XPT hash).
    Not verified on air yet; 256 values, so a match is a hint, never an identification."""
    return crc8(int(address) & 0xFFFF, 16)


MODELS = ("Tiny", "Small", "Large", "Huge")
_NET_BITS = (9, 7, 4, 2)          # the 12 bits after the model: net, then site (ETSI TS 102 361-4 7.2.19)


def system_identity(code: int) -> dict[str, Any]:
    """The 14-bit Tier III system identity code (the ``SYS`` DSD-FME prints): model, net, site."""
    model = (code >> 12) & 0x3
    site_bits = 12 - _NET_BITS[model]
    return {"model": MODELS[model], "net": (code >> site_bits) & ((1 << _NET_BITS[model]) - 1),
            "site": code & ((1 << site_bits) - 1), "sys_code": f"{code:04X}"}


def printed_ids(net: int, site: int) -> tuple[int, int, int]:
    """(Net ID, Site ID, sub-site) the way DSD-FME prints them with ``-D 0`` and the mapper stores
    them: ``Net ID: net+1; Site ID: 1.site+1``. ``-D`` is read as a character, so ``0`` becomes 48,
    clamped to 10 bits of sub-site; any Tier III model then prints as ``1.(site+1)``."""
    return net + 1, 1, site + 1


def csbk_crc_ok(b: bytes) -> bool:
    if len(b) != 12:
        return False
    data = int.from_bytes(b[:10], "big")
    return crc16_ccitt(data, 80) == (int.from_bytes(b[10:12], "big") ^ CSBK_CRC_MASK)


def decode_csbk(b: bytes) -> dict[str, Any]:
    """``{"fid", "opcode", "pf"}`` plus, for the opcodes read here, ``kind`` and the fields."""
    v = int.from_bytes(b[:10], "big")
    out: dict[str, Any] = {"pf": field(v, 80, 1, 1), "opcode": field(v, 80, 2, 6), "fid": field(v, 80, 8, 8)}
    if out["pf"]:
        return out
    if out["opcode"] == 25:                                   # C_ALOHA (Capacity Max sends it with FID 0x10)
        out.update(kind="aloha", own=system_identity(field(v, 80, 40, 14)))
    elif out["opcode"] == 40:                                 # C_BCAST
        out["a_type"] = field(v, 80, 16, 5)
        out["own"] = system_identity(field(v, 80, 40, 14))
        if out["a_type"] == 6:                                # Adjacent_Site; on Capacity Max the channel is an LCN
            out.update(kind="adjacent_site", neighbor=system_identity(field(v, 80, 21, 14)), channel=field(v, 80, 68, 12),
                       info_available=bool(field(v, 80, 56, 1)), online=bool(field(v, 80, 57, 1)))
    elif out["fid"] == 0x10 and out["opcode"] == 0x21:
        lcn = field(v, 80, 16, 12)
        updates = [{"lcn": lcn, "slot": slot, "tg": tg} for slot, tg in ((1, field(v, 80, 32, 24)), (2, field(v, 80, 56, 24))) if tg]
        out.update(kind="capmax_channel_update", mode="open", updates=updates)
    elif out["fid"] == 0x10 and out["opcode"] == 0x22:
        # two channels per message, 10-bit talkgroup fields (Advantage mode uses short TG ids)
        updates = []
        for lcn_at, tg1_at, tg2_at in ((16, 28, 38), (48, 60, 70)):
            lcn = field(v, 80, lcn_at, 12)
            for slot, at in ((1, tg1_at), (2, tg2_at)):
                tg = field(v, 80, at, 10)
                if lcn and tg:
                    updates.append({"lcn": lcn, "slot": slot, "tg": tg})
        out.update(kind="capmax_channel_update", mode="advantage", updates=updates)
    return out


def decode_slco_block(b: bytes) -> dict[str, Any] | None:
    """The 5 bytes of ``SLCO Completed Block``: 4-bit opcode, 24 data bits, CRC-8 (DSD-FME prints
    the block only after the CRC passed)."""
    if len(b) != 5:
        return None
    v = int.from_bytes(b, "big")
    opcode, data = field(v, 40, 0, 4), field(v, 40, 4, 24)
    out: dict[str, Any] = {"opcode": opcode, "data": f"{data:06X}"}
    if opcode in (0x2, 0x3):                                  # C_SYS_PARMS / P_SYS_PARMS
        out["own"] = system_identity(field(data, 24, 0, 14))
    if opcode == 0x1:
        a1, a2 = field(data, 24, 0, 4), field(data, 24, 4, 4)
        out.update(ts1=ACTIVITY.get(a1, f"Res {a1:X}"), hash1=field(data, 24, 8, 8),
                   ts2=ACTIVITY.get(a2, f"Res {a2:X}"), hash2=field(data, 24, 16, 8))
    return out


def activity_key(ts1: str | None, hash1: int | None, ts2: str | None, hash2: int | None) -> str:
    """One text per Activity Update content, the same whether it came from DSD-FME's decoded line
    or from a Completed Block (old databases kept only the latter)."""
    return f"activity TS1 {ts1}/{hash1} TS2 {ts2}/{hash2}"
