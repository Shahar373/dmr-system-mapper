"""Unit tests for the DSD-FME line parser (no database)."""

from __future__ import annotations

from datetime import datetime

from dmr_mapper.ingest import ParseContext, parse_lines, parse_log_filename, strip_ansi
from dmr_mapper.ingest.events import Burst, NoSync, Quality, Structural, Unrecognized

from conftest import TZ


def _ctx(**kw) -> ParseContext:
    base = dict(freq_hz=168863000, start_time=datetime(2026, 5, 9, 16, 33, 44, tzinfo=TZ), tz=TZ)
    base.update(kw)
    return ParseContext(**base)


def _parse(text: str, **kw):
    return list(parse_lines(text.splitlines(), _ctx(**kw)))


def test_strip_ansi():
    assert strip_ansi("\x1b[33m Capacity Plus\x1b[0m\x1b[0m") == " Capacity Plus"
    assert strip_ansi("CSBK\x1b[33m") == "CSBK"


def test_filename_khz_and_hz():
    freq, start = parse_log_filename("dsd_fme_168863_20260509_163344.log", TZ)
    assert freq == 168_863_000
    assert start == datetime(2026, 5, 9, 16, 33, 44, tzinfo=TZ)
    freq, _ = parse_log_filename("/x/dsd_fme_451237500_20260801_120000.log", TZ)
    assert freq == 451_237_500
    assert parse_log_filename("decoder_report.json") == (None, None)


def test_burst_line_fields():
    evs = _parse("16:33:44 Sync: +DMR   slot1  [slot2] | Color Code=02 | CSBK\x1b[33m")
    b = evs[0]
    assert isinstance(b, Burst)
    assert (b.polarity, b.slot, b.cc, b.kind, b.category) == ("+", 2, 2, "CSBK", "csbk")
    assert b.ts == int(datetime(2026, 5, 9, 16, 33, 44, tzinfo=TZ).timestamp())


def test_burst_variants():
    evs = _parse(
        "19:58:29 Sync: +DMR  [SLOT1]  slot2  | Color Code=XX | VC1*\n"
        "19:58:29 Sync: +DMR   slot1  [slot2] \x1b[31m| CACH/Burst FEC ERR\x1b[0m\n"
        "19:58:30 Sync: +DMR   slot1  [slot2] | Color Code=08 | DATA\x1b[0m\x1b[31m (CRC ERR) \x1b[0m\n"
        "19:58:30 Sync: +DMR  [slot1]  slot2  | Color Code=08 | IDLE \n"
        "Sync: no sync\n"
        "16:41:18 Sync: +YSF  FN: 10/10 FICH (CRC ERR) DCH\n"
    )
    kinds = [(e.kind, e.category, e.crc_err) for e in evs if isinstance(e, Burst)]
    assert kinds[0] == ("VC1*", "voice", False)
    assert evs[0].cc is None and evs[0].slot == 1
    assert kinds[1] == ("ERR", "other", True)
    assert kinds[2] == ("DATA", "data", True)
    assert kinds[3] == ("IDLE", "idle", False)
    assert any(isinstance(e, NoSync) for e in evs)
    assert kinds[4][0] == "OTHER_PROTO"


def test_capplus_channel_status_with_bank_and_lsn_list():
    text = (
        "16:33:44 Sync: +DMR   slot1  [slot2] | Color Code=02 | CSBK\n"
        " Capacity Plus Channel Status - FL: 3 TS: 1 RS: 0 - Rest LSN: 6 - Single Block\n"
        " Bank One F80 Private or Data Call(s) -  LSN 05: TGT 64250;\n"
        "  LSN 01:  Idle;  LSN 02:  Idle;  LSN 03:  Idle;  LSN 04:  Idle;  \n"
        "  LSN 05: 64250;  LSN 06:  Rest;  LSN 07:  Idle;  LSN 08:  Idle;  \n"
        " SLCO Capacity Plus Site: 2 - Rest LSN: 6 - RS: 00\n"
        "16:33:45 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n"
    )
    evs = [e for e in _parse(text) if isinstance(e, Structural)]
    assert [e.kind for e in evs] == ["capplus_site", "capplus_channel_status"]  # site line is single-line, status flushed on next sync
    status = evs[1]
    assert status.payload["rest_lsn"] == 6 and status.payload["block"] == "single"
    assert status.payload["fl"] == 3 and status.payload["ts"] == 1
    assert status.payload["lsn_states"] == {1: "idle", 2: "idle", 3: "idle", 4: "idle", 5: 64250, 6: "rest", 7: "idle", 8: "idle"}
    assert status.payload["banks"]["one"]["lsn_to_tg"] == {5: 64250}
    assert ("tg", 64250, "lsn_active") in status.identities   # nothing yet says 64250 is an individual
    assert status.slot == 2 and status.cc == 2
    site = evs[0]
    assert site.payload == {"site": 2, "rest_lsn": 6, "rs": "00"}


def test_capplus_adjacent_preamble_calls():
    text = (
        "16:33:48 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n"
        " Capacity Plus Adjacent Sites\n"
        "  Site: 1 Rest: 3; \n"
        "16:33:48 Sync: +DMR   slot1  [slot2] | Color Code=02 | CSBK\n"
        " Preamble CSBK - Individual Data - Source: 199 - Target: 64250 - Rest LSN: 6\n"
        "16:33:55 Sync: +DMR   slot1  [slot2] | Color Code=02 | TLC  \n"
        " SLOT 2 TGT=2102 SRC=64250 Group Call  \n"
        "16:37:28 Sync: +DMR   slot1  [slot2] | Color Code=02 | VLC  \n"
        " SLOT 2 TGT=1 SRC=2102 Cap+ Group Call  Rest LSN: 5 \n"
        "16:37:29 Sync: +DMR   slot1  [slot2] | Color Code=02 | VLC  \n"
        " SLOT 2 TGT=1 SRC=2102 Group TXI Call  \n"
    )
    evs = [e for e in _parse(text) if isinstance(e, Structural)]
    adj = evs[0]
    assert adj.kind == "capplus_adjacent" and adj.payload["sites"] == [{"site": 1, "rest_lsn": 3}]
    pre = evs[1]
    assert pre.kind == "preamble"
    assert pre.payload == {"addressing": "individual", "kind": "data", "src": 199, "tgt": 64250, "rest_lsn": 6}
    assert set(pre.identities) == {("radio", 199, "src"), ("radio", 64250, "tgt")}
    calls = [e for e in evs if e.kind == "call"]
    assert len(calls) == 3
    assert calls[1].payload["capplus"] and calls[1].payload["rest_lsn"] == 5 and calls[1].payload["group"]
    assert calls[2].payload["txi"]
    assert ("tg", 1, "tgt") in calls[1].identities and ("radio", 2102, "src") in calls[1].identities


def test_lrrp_with_position_and_ip_map():
    text = (
        "16:33:44 Sync: +DMR  [slot1]  slot2  | Color Code=02 | R12U \n"
        " SRC(24): 00000199; IP: 012.000.000.199; Port: 4001; \n"
        " DST(24): 00064250; IP: 013.000.250.250; Port: 4001; \n"
        " UDP Protocol; Datagram Len: 17; UDP Checksum: 8B87; LRRP;\n"
        " LRRP SRC: 199; Response to TGT: 64250;\n"
        " Lat: 32.101282 Lon: 34.871515\n"
        "16:33:45 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n"
    )
    evs = _parse(text)
    structural = [e for e in evs if isinstance(e, Structural)]
    kinds = [e.kind for e in structural]
    assert kinds == ["ip_map", "ip_map", "lrrp"]
    lrrp = structural[2]
    assert lrrp.payload == {"src": 199, "tgt": 64250, "direction": "response", "lat": 32.101282, "lon": 34.871515}
    assert structural[0].payload["radio"] == 199 and structural[0].payload["ip"] == "012.000.000.199"
    assert not any(isinstance(e, Unrecognized) for e in evs)


def test_tier2_lines():
    text = (
        "19:58:29 Sync: +DMR  [slot1]  slot2  | Color Code=08 | VLC \x1b[31m \n"
        " SLOT 1 Protected LC  FLCO=0x3F FID=0x7D \x1b[0m\n"
        "\x1b[33m Activity Update TS1: Group Voice; Hash: 7; TS2: Idle; Hash: 0;\x1b[0m\n"
        "\x1b[33m SLCO NULL \x1b[0m\n"
        "\x1b[31m SLCO CRC ERR\x1b[0m\n"
        " Moto Data Channel: 29; 04 00 00 3B 08 FC 00 00 \n"
        " Protected Control Signalling Block(s) (CRC ERR) \n"
        " SLOT 2 FLCO FEC ERR  (FEC ERR)\n"
    )
    evs = _parse(text)
    structural = {e.kind: e for e in evs if isinstance(e, Structural)}
    assert structural["protected_lc"].payload == {"slot": 1, "lc": "protected", "flco": "0X3F", "fid": "0X7D"}
    assert structural["activity_update"].payload == {"ts1": "Group Voice", "hash1": 7, "ts2": "Idle", "hash2": 0}
    assert structural["moto_data_channel"].payload == {"opcode": "29", "bytes": "04 00 00 3B 08 FC 00 00"}
    q = [e.counter for e in evs if isinstance(e, Quality)]
    assert q == ["slco_null", "slco_crc", "crc_err", "crc_err"]
    assert not any(isinstance(e, Unrecognized) for e in evs)


def test_midnight_rollover_and_decode_clock_offsets():
    text = (
        "23:59:59 Sync: +DMR  [slot1]  slot2  | Color Code=02 | IDLE \n"
        "00:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=02 | IDLE \n"
    )
    evs = [e for e in _parse(text, start_time=datetime(2026, 5, 9, 23, 59, 0, tzinfo=TZ)) if isinstance(e, Burst)]
    assert evs[1].ts - evs[0].ts == 2
    # decode wall-clock (surveyor): offsets from the first line are added to the RF start time
    evs = [e for e in _parse(text, start_time=datetime(2026, 7, 13, 15, 2, 42, tzinfo=TZ), time_is_rf=False) if isinstance(e, Burst)]
    assert evs[0].ts == int(datetime(2026, 7, 13, 15, 2, 42, tzinfo=TZ).timestamp())
    assert evs[1].ts - evs[0].ts == 2


def test_first_line_date_vs_start_clock():
    # H1: capture started at 23:59:58, first line is after midnight → next day, not 24 h earlier
    text = "00:00:03 Sync: +DMR  [slot1]  slot2  | Color Code=02 | IDLE \n"
    ev = _parse(text, start_time=datetime(2026, 5, 9, 23, 59, 58, tzinfo=TZ))[0]
    assert ev.ts == int(datetime(2026, 5, 10, 0, 0, 3, tzinfo=TZ).timestamp())
    # a buffered line from just before midnight when the stream started just after it → previous day
    text = "23:59:59 Sync: +DMR  [slot1]  slot2  | Color Code=02 | IDLE \n"
    ev = _parse(text, start_time=datetime(2026, 5, 10, 0, 0, 1, tzinfo=TZ))[0]
    assert ev.ts == int(datetime(2026, 5, 9, 23, 59, 59, tzinfo=TZ).timestamp())


def test_clock_scale_for_decode_logs():
    # M1: decode wall clock runs faster than the recording; offsets are scaled onto the RF duration
    text = (
        "19:58:29 Sync: +DMR  [slot1]  slot2  | Color Code=08 | IDLE \n"
        "19:58:30 Sync: +DMR  [slot1]  slot2  | Color Code=08 | IDLE \n"
    )
    evs = _parse(text, start_time=datetime(2026, 7, 13, 15, 2, 56, tzinfo=TZ), time_is_rf=False, clock_scale=11.0)
    assert evs[1].ts - evs[0].ts == 11


def test_payload_before_first_sync_is_not_timestamped_zero():
    # M2: a stream that starts mid-record must not produce epoch-0 events
    text = (
        "Build Version: AW 2026-26\n"
        " Preamble CSBK - Individual Data - Source: 199 - Target: 64250 - Rest LSN: 6\n"
        "16:33:44 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n"
    )
    evs = _parse(text)
    assert not any(isinstance(e, Structural) for e in evs)
    assert any(isinstance(e, Unrecognized) and "before first sync" in e.text for e in evs)
    assert any(type(e).__name__ == "Meta" and e.key == "build" for e in evs)


def test_two_banks_and_individual_targets():
    # M6 + L10: both banks are kept; an ID is a radio only once an Individual record named it
    text = (
        "16:33:44 Sync: +DMR   slot1  [slot2] | Color Code=02 | CSBK\n"
        " Preamble CSBK - Individual Data - Source: 199 - Target: 100 - Rest LSN: 6\n"
        "16:33:45 Sync: +DMR   slot1  [slot2] | Color Code=02 | CSBK\n"
        " Capacity Plus Channel Status - FL: 3 TS: 1 RS: 0 - Rest LSN: 6 - Single Block\n"
        " Bank One F80 Private or Data Call(s) -  LSN 05: TGT 100;\n"
        " Bank Two F90 Private or Data Call(s) -  LSN 12: TGT 200;\n"
        "  LSN 05: 100;  LSN 06:  Rest;  LSN 12: 200;\n"
        "16:33:46 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n"
    )
    evs = [e for e in _parse(text) if isinstance(e, Structural)]
    status = evs[-1]
    assert status.kind == "capplus_channel_status"
    assert set(status.payload["banks"]) == {"one", "two"}
    assert status.payload["banks"]["two"]["lsn_to_tg"] == {12: 200}
    assert ("radio", 100, "lsn_active") in status.identities
    assert ("tg", 200, "lsn_active") in status.identities
    assert not any(i[2] == "private_or_data" for i in status.identities)


def test_xpt_multi_lcn_line():
    # M5: several LCNs on one line, each LSN mapped to the LCN before it
    text = (
        "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
        " Hytera XPT Site Status - Free LCN: 3 SN: 1\n"
        " LCN 1 - LSN 01: Idle; LSN 02: Idle;  LCN 2 - LSN 03: Priv; LSN 04: Null;\n"
        "12:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=01 | CSBK\n"
    )
    ev = [e for e in _parse(text) if isinstance(e, Structural)][0]
    assert ev.payload["lcns"] == [1, 2]
    assert ev.payload["lsn_lcn"] == {1: 1, 2: 1, 3: 2, 4: 2}


def test_new_record_is_never_swallowed_by_pending_bcast():
    text = (
        "12:00:00 Sync: +DMR  [slot1]  slot2  | Color Code=09 | CSBK\n"
        " Announcements (C_BCAST) General Site Parameters (Gen_Site_Params)\n"
        " C_ALOHA_SYS_PARMS: Small; Net ID: 1; Site ID: 7; SYS: 1A2B;\n"
        "12:00:01 Sync: +DMR  [slot1]  slot2  | Color Code=09 | CSBK\n"
    )
    kinds = [e.kind for e in _parse(text) if isinstance(e, Structural)]
    assert kinds == ["tier3_bcast", "tier3_sys_parms"]


def test_corrupt_lc_is_a_counter_not_an_event():
    text = (
        "16:33:44 Sync: +DMR   slot1  [slot2] | Color Code=02 | VLC \n"
        " SLOT 2 Unknown LC  FLCO=0x28 FID=0x80  (CRC ERR)\n"
    )
    evs = _parse(text)
    assert not any(isinstance(e, Structural) for e in evs)
    assert [e.counter for e in evs if isinstance(e, Quality)] == ["crc_err"]


def test_unrecognized_is_reported_not_dropped():
    evs = _parse("16:33:44 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n Some Brand New CSBK Text 42\n")
    unk = [e for e in evs if isinstance(e, Unrecognized)]
    assert len(unk) == 1 and unk[0].text == "Some Brand New CSBK Text 42"
