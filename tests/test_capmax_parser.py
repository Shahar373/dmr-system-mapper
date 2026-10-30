"""Phase 4: Capacity Max from the raw bytes DSD-FME prints with -Z (``DMR PDU Payload``,
``SLCO Completed Block``), and Tier III numbering that stays the same whatever DSD-FME prints.
Line formats from the DSD-FME source (dmr_csbk.c, dmr_flco.c, dmr_dburst.c); bit layouts of the
Motorola messages from SDRTrunk. Not yet checked against a real capture (untested_on_air)."""

from __future__ import annotations

import json
from datetime import datetime

from dmr_mapper.ingest import ParseContext, ingest_log_file, parse_lines
from dmr_mapper.ingest.events import RawOpcode, Structural, Unrecognized
from dmr_mapper.ingest.rawpdu import (address_hash, crc8, crc16_ccitt, csbk_crc_ok, decode_csbk, decode_slco_block, hex_bytes,
                                      system_identity)

from conftest import TZ
from synth import capmax_site, csbk_pdu, large_syscode, slco_block


def _parse(text: str):
    ctx = ParseContext(freq_hz=469_350_000, start_time=datetime(2026, 10, 30, 12, 0, 0, tzinfo=TZ), tz=TZ, source="collector")
    return list(parse_lines(text.splitlines(), ctx))


def _structural(evs, kind):
    return [e for e in evs if isinstance(e, Structural) and e.kind == kind]


def test_checksums_match_the_standard_check_values():
    assert crc16_ccitt(int.from_bytes(b"123456789", "big"), 72) == 0xCE3C      # CRC-16/GSM
    assert crc8(int.from_bytes(b"123456789", "big"), 72) == 0xF4               # CRC-8/SMBUS
    assert 0 <= address_hash(2102) <= 255 and address_hash(2102) == address_hash(2102 + 0x10000)


def test_system_identity_code():
    # the field run: net 2 site 1.2 printed SYS 2101 → Large, raw net 1, raw site 1
    assert system_identity(0x2101) == {"model": "Large", "net": 1, "site": 1, "sys_code": "2101"}
    assert system_identity(large_syscode(1, 3))["sys_code"] == "2103"


def test_capmax_channel_update_from_the_payload_line():
    pdu = csbk_pdu(0x21, 0x10, [(16, 12, 7), (32, 24, 2102), (56, 24, 3001)])
    b = hex_bytes(pdu)
    assert csbk_crc_ok(b)
    d = decode_csbk(b)
    assert d["kind"] == "capmax_channel_update" and d["mode"] == "open"
    assert d["updates"] == [{"lcn": 7, "slot": 1, "tg": 2102}, {"lcn": 7, "slot": 2, "tg": 3001}]
    evs = _parse("12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n" + pdu)
    ups = _structural(evs, "capmax_channel_update")
    assert [e.payload for e in ups] == [{"mode": "open", "lcn": 7, "slot": 1, "tg": 2102}, {"mode": "open", "lcn": 7, "slot": 2, "tg": 3001}]
    assert ups[0].identities == [("tg", 2102, "tgt")]
    assert [e for e in evs if isinstance(e, RawOpcode)] == [RawOpcode(ups[0].ts, "FID 10 opcode 21", True, "")]


def test_advantage_mode_update_has_two_channels():
    pdu = csbk_pdu(0x22, 0x10, [(16, 12, 3), (28, 10, 101), (48, 12, 4), (70, 10, 202)])
    d = decode_csbk(hex_bytes(pdu))
    assert d["mode"] == "advantage"
    assert d["updates"] == [{"lcn": 3, "slot": 1, "tg": 101}, {"lcn": 4, "slot": 2, "tg": 202}]


def test_corrupt_and_unknown_csbks():
    good = csbk_pdu(0x2A, 0x10, [(16, 24, 0x123456)])
    bad = good.replace("[12]", "[13]", 1)
    text = ("12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n" + bad +
            "12:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n" + good +
            "12:00:02 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n Announcements (C_BCAST) Vote Now Advice (Vote_Now)\n" +
            csbk_pdu(40, 0, [(16, 5, 2)]) +
            # voice LC bytes are recognised and left alone: DSD-FME prints the call itself
            "12:00:03 Sync: +DMR  [slot1]  slot2  | Color Code=01 | VLC\n SLOT 1 TGT=2102 SRC=1234 Group Call\n"
            " DMR PDU Payload [00][10][00][00][08][36][00][04][D2][11][22][33]\n")
    evs = _parse(text)
    raw = [e for e in evs if isinstance(e, RawOpcode)]
    # the corrupt one is dropped, the unknown Motorola one is reported with its bytes, the
    # broadcast DSD-FME decoded itself counts as understood
    assert [(r.key, r.understood) for r in raw] == [("FID 10 opcode 2A", False), ("FID 00 opcode 28", True)]
    assert raw[0].example.startswith("AA 10 12 34 56")
    assert not any(isinstance(e, Unrecognized) for e in evs)


def test_numbering_is_kept_when_dsd_fme_switches_to_raw():
    """After its first Motorola broadcast DSD-FME prints raw Net/Site numbers; the parser converts
    them back to the -D 0 numbering, and the payload bytes settle every identity."""
    code, n_code = large_syscode(1, 1), large_syscode(1, 3)
    text = (
        "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
        f" SLC_C_SYS_PARMS: Large; Net ID: 2; Site ID: 1.2; Reg Req: 1; CSC: 5; SYS: {code:04X};\n"
        "12:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
        " Announcements (C_BCAST) Adjacent Site Information (Adjacent_Site)\n"
        f" Large; Net ID: 1; Site ID: 3; SYS: {n_code:04X}; Active Connection Information Not Available;\n"
        + csbk_pdu(40, 0x10, [(16, 5, 6), (21, 14, n_code), (40, 14, code), (68, 12, 5)]) +
        "12:00:02 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
        f" SLC_C_SYS_PARMS: Large; Net ID: 1; Site ID: 1; Reg Req: 1; SYS: {code:04X};\n"
        "12:00:03 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
        f" SLC_C_SYS_PARMS: Large; Net ID: 1; Site ID: 1; Reg Req: 1; SYS: {code:04X};\n"
        + slco_block(2, (code << 10) | (1 << 9) | 7) +
        "12:00:04 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
        f" C_ALOHA_SYS_PARMS: Large; Net ID: 1; Site ID: 1; SYS: {code:04X}; Capacity Max\n"
    )
    evs = _parse(text)
    sys_parms = _structural(evs, "tier3_sys_parms")
    assert [(e.payload["net_id"], e.payload["site_id"], e.payload["sub_site"]) for e in sys_parms] == [(2, 1, 2)] * 4
    adj = _structural(evs, "tier3_bcast")[0].payload
    assert (adj["adj_net_id"], adj["adj_site_id"], adj["adj_sub_site"], adj["lpcn"], adj["sys_code"]) == (2, 1, 4, 5, "2103")


def test_aloha_marked_capacity_max_switches_numbering_without_payload_lines():
    text = (
        "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
        " C_ALOHA_SYS_PARMS: Large; Net ID: 1; Site ID: 2; SYS: 2102; Capacity Max\n"
        "12:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
        " SLC_C_SYS_PARMS: Large; Net ID: 1; Site ID: 2; Reg Req: 1; SYS: 2102;\n"
    )
    ids = [(e.payload["net_id"], e.payload["site_id"], e.payload["sub_site"]) for e in _structural(_parse(text), "tier3_sys_parms")]
    assert ids == [(2, 1, 3), (2, 1, 3)]


def test_short_lc_blocks():
    text = (
        # a Cap+ carrier: the site line stays a Cap+ site, the block is not stored on its own
        "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | IDLE\n"
        " SLCO Capacity Plus Site: 2 - Rest LSN: 5 - RS: 00\n"
        " SLCO Completed Block [F0][05][28][00][00]\n"
        " Activity Update TS1: Group Voice; Hash: 7; TS2: Idle; Hash: 0;\n"
        " SLCO Completed Block [18][00][70][07][60]\n"
        " SLCO Cap Max Activity Update; TGT: 2102;\n"
        " SLCO Completed Block [E0][00][83][60][00]\n"
        " SLCO Unknown - 6 \n"
        " SLCO Completed Block [61][23][45][00][00]\n"
    )
    evs = _parse(text)
    assert _structural(evs, "capplus_site")[0].payload == {"site": 2, "rest_lsn": 5, "rs": "00"}
    assert _structural(evs, "activity_update")[0].payload == {"ts1": "Group Voice", "hash1": 7, "ts2": "Idle", "hash2": 0}
    assert _structural(evs, "capmax_activity")[0].payload == {"tgt": 2102}
    assert _structural(evs, "slco_other")[0].payload == {"text": "SLCO Unknown - 6", "opcode": 6, "data": "123450"}
    assert decode_slco_block(hex_bytes("[18][00][70][07][60]"))["hash1"] == 7
    # on a Tier III carrier the same opcode 0xF is a Motorola message with its own bits
    sys_parms = ("12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
                 " SLC_C_SYS_PARMS: Large; Net ID: 2; Site ID: 1.2; Reg Req: 1; CSC: 0; SYS: 2101;\n")
    stray = ("12:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
             " SLCO Capacity Plus Site: 0 - Rest LSN: 0 - RS: 00\n"
             " SLCO Completed Block [F0][0A][5C][30][00]\n")
    evs = _parse(sys_parms * 3 + stray)
    assert not _structural(evs, "capplus_site")
    assert _structural(evs, "capmax_slco")[0].payload["data"] == "00A5C3"
    # one stray Tier III line on a Cap+ carrier changes nothing
    assert _structural(_parse(sys_parms + stray), "capplus_site")


def test_synthetic_capmax_site_ingests_cleanly(tmp_path, store, cfg):
    paths, keyups = capmax_site(tmp_path, net=1, site=1, cc=1, control_freq=469_350_000, traffic={7: 469_062_500},
                                calls=[(20, 2102, 1234, 7, 1, 8)], neighbors=[(3, 5)], minutes=2)
    stats = [ingest_log_file(store, p, cfg) for p in paths]
    assert all(s.unrecognized_total == 0 for s in stats)
    cc = stats[0]
    assert cc.variant == "tier3" and cc.csbk_opcodes["FID 10 opcode 21"] == 8
    # one row per day for the control channel's own announcement, whatever its slot counter says
    # one row per day and source (short LC, Aloha) for the control channel's own announcement, whatever its slot counter says
    assert cc.stored_events["tier3_sys_parms"] == 2 and cc.structural["tier3_sys_parms"] > 120
    rows = store.rows("SELECT payload FROM csbk_events WHERE kind = 'tier3_sys_parms' AND carrier_id = 1")
    assert {(json.loads(r["payload"])["net_id"], json.loads(r["payload"])["sub_site"]) for r in rows} == {(2, 2)}   # one numbering
    assert {"capmax_channel_update", "capmax_slco"} <= set(cc.untested_kinds)
    assert keyups == [(469_062_500, keyups[0][1], keyups[0][1] + 8)]


def test_field_capture_of_the_469_mhz_control_channel(store, cfg):
    """Two seconds of the site-2 control channel (469.65 MHz, 2026-10-31, -Z). DSD-FME flags every
    CSBK (CRC ERR) and prints nothing for it, yet the same bytes repeat exactly and their fields are
    right: the CRC is keyed. The Aloha confirms the site, and the neighbour broadcasts name site 3 on
    LCN 5 (as SDRTrunk showed) and site 1 on LCN 2."""
    from conftest import SAMPLES

    st = ingest_log_file(store, SAMPLES / "capmax" / "dsd_fme_469650000_20261031_003613_excerpt.log", cfg)
    assert st.unrecognized_total == 0 and st.variant == "tier3"
    assert st.quality["pdu_keyed_crc"] > 30 and st.quality["pdu_crc_err"] == 4      # each distinct payload trusted from its repeat on
    assert st.csbk_opcodes == {"FID 10 opcode 19": 21, "FID 00 opcode 19": 10, "FID 00 opcode 28": 9}
    rows = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM csbk_events WHERE kind = 'tier3_bcast'")]
    neighbours = {(p["adj_sub_site"] - 1, p["lpcn"]) for p in rows if p["a_type"] == "adjacent_site"}
    assert neighbours == {(3, 5), (1, 2)}                                                # on-air site, LCN
    aloha = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM csbk_events WHERE kind = 'tier3_sys_parms'")]
    assert {(p["source"], p["sys_code"], p["sub_site"]) for p in aloha} == {("slc_c_sys_parms", "2102", 3), ("c_aloha_sys_parms", "2102", 3)}


def test_a_crc_failure_with_fec_errors_or_seen_once_is_not_trusted():
    good = csbk_pdu(0x21, 0x10, [(16, 12, 7), (32, 24, 2102)])
    bad = good.rsplit("[", 2)[0] + "[00][00]\n"                                          # the same fields, a CRC that fails
    once = "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK (CRC ERR)\n" + bad
    assert not _structural(_parse(once), "capmax_channel_update")
    assert _structural(_parse(once * 2), "capmax_channel_update")                         # repeated: trusted
    fec = "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK (FEC ERR)\n" + bad
    assert not _structural(_parse(fec * 3), "capmax_channel_update")


def test_a_single_corrupted_copy_is_never_used_however_keyed_the_stream():
    """Field run: after hundreds of clean keyed-CRC CSBKs, one copy with a flipped bit named a site
    9 that does not exist. Only payloads seen twice are used."""
    n_code, code = large_syscode(1, 1), large_syscode(1, 2)
    good = csbk_pdu(40, 0x10, [(16, 5, 6), (21, 14, n_code), (40, 14, code), (68, 12, 2)]).rsplit("[", 2)[0] + "[00][00]\n"
    flipped = csbk_pdu(40, 0x10, [(16, 5, 6), (21, 14, large_syscode(1, 9)), (40, 14, code), (68, 12, 2)]).rsplit("[", 2)[0] + "[00][00]\n"
    burst = "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK (CRC ERR)\n"
    evs = _parse((burst + good) * 60 + burst + flipped)
    sites = {e.payload["adj_sub_site"] - 1 for e in _structural(evs, "tier3_bcast")}
    assert sites == {1}


def test_payload_and_tcp_noise_is_recognised():
    text = (" TCP Direct Link: 127.0.0.1:7356\n TCP Connection Success!\n"
            "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | VC2\n"
            " AMBE F801A99F8CE080 err = [0] [0]\n SB: 00000000000 - 000;\n CAP+ Multi Block PDU\n"
            " [BE][10][A3][00][00][80][C0][FA][FA][FA][FA][00][00][00][00][00][00]\n"
            " Connection to TCP Server Interrupted. Trying again in 3 seconds.\n ERROR opening socket\n"
            "12:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK (CRC ERR)\n"
            " DMR PDU Payload [A0][00][05][C0][00][00][E5][00][00][FF][CF][7F]\n"
            "12:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK (CRC ERR)\n"
            " DMR PDU Payload [A0][00][05][C0][00][00][E5][00][00][FF][CF][7F]\n")
    evs = _parse(text)
    assert not any(isinstance(e, Unrecognized) for e in evs)
    assert all(r.understood for r in evs if isinstance(r, RawOpcode))      # an acknowledgement: known, not used
