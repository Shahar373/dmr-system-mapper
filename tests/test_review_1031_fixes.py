"""Faults found reading the evidence of 2026-10-31 (DESIGN.md §12), fixed afterwards."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from dmr_mapper.correlate import run
from dmr_mapper.db import Store
from dmr_mapper.ingest import ParseContext, parse_lines
from dmr_mapper.ingest.events import Structural
from dmr_mapper.ingest.runner import ingest_stream

from conftest import TZ

T0 = datetime(2026, 10, 31, 14, 20, 0, tzinfo=TZ)
GATEWAY = 64250


def _status(t: datetime, tg: int, lsn: int = 5) -> list[str]:
    return [
        f"{t:%H:%M:%S} Sync: +DMR   slot1  [slot2] | Color Code=01 | CSBK",
        " Capacity Plus Channel Status - FL: 3 TS: 1 RS: 0 - Rest LSN: 6 - Single Block",
        f" Bank One F80 Private or Data Call(s) -  LSN {lsn:02d}: TGT {tg};",
        f"  LSN 01:  Idle;  LSN 02:  Idle;  LSN 03:  Idle;  LSN 04:  Idle;  ",
        f"  LSN {lsn:02d}: {tg};  LSN 06:  Rest;  LSN 07:  Idle;  LSN 08:  Idle;  ",
    ]


def _data_response(t: datetime, src: int, tgt: int) -> list[str]:
    return [f"{t:%H:%M:%S} Sync: +DMR  [slot1]  slot2  | Color Code=01 | DATA",
            f" DATA RESP TGT: {tgt}; SRC: {src}; ACK - Success"]


def _log(radio: int) -> list[str]:
    """A Cap+ rest channel: the gateway shows in channel statuses first (as DSD-FME prints it, like a
    talkgroup), then answers a radio in a data response; a real talkgroup 1 is active too."""
    out: list[str] = []
    t = T0
    for i in range(12):
        out += _status(t, GATEWAY)
        t += timedelta(seconds=1)
        out += _status(t, 1, lsn=3)
        t += timedelta(seconds=1)
    out += _data_response(t, GATEWAY, radio)
    t += timedelta(seconds=1)
    out += _status(t, GATEWAY)
    out.append(f"{t + timedelta(seconds=1):%H:%M:%S} Sync: +DMR   slot1  [slot2] | Color Code=01 | CSBK")
    return out


def test_a_data_response_makes_both_ids_radios():
    ctx = ParseContext(freq_hz=469_800_000, start_time=T0, tz=TZ)
    evs = [e for e in parse_lines(_log(1201), ctx) if isinstance(e, Structural)]
    statuses = [e for e in evs if e.kind == "capplus_channel_status"]
    assert ("tg", GATEWAY, "lsn_active") in statuses[0].identities          # not known yet
    assert ("radio", GATEWAY, "lsn_active") in statuses[-1].identities       # after its data response
    assert all(("radio", 1, "lsn_active") not in e.identities for e in statuses)   # a talkgroup stays one
    assert any(("tg", 1, "lsn_active") in e.identities for e in statuses)
    resp = next(e for e in evs if e.kind == "data_response")
    assert {("radio", GATEWAY), ("radio", 1201)} <= {(k, v) for k, v, _ in resp.identities}


def test_a_gateway_seen_as_a_talkgroup_does_not_link_carriers_as_one(tmp_path, cfg):
    db = tmp_path / "m.db"
    with Store(db) as s:
        ingest_stream(s, iter(_log(1201)), cfg, 469_800_000, "collector", start=T0.replace(tzinfo=None))
        ingest_stream(s, iter(_log(1202)), cfg, 469_462_500, "collector", start=T0.replace(tzinfo=None))
        now = int((T0 + timedelta(minutes=5)).timestamp())
        run(s, cfg, now=now)
        ev = [(r["type"], json.loads(r["details"])) for r in s.rows("SELECT type, details FROM evidence")]
    tg_ids = {i for t, d in ev if t == "identity_talkgroup" for i in d.get("ids", [])}
    radio_ids = {i for t, d in ev if t == "identity_radio" for i in d.get("ids", [])}
    assert 1 in tg_ids                       # the real talkgroup still counts
    assert GATEWAY not in tg_ids             # the gateway is not a talkgroup...
    assert GATEWAY in radio_ids              # ...but a radio heard on both


def test_mcp_pseudonymises_radio_ids_inside_text(tmp_path, cfg):
    from dmr_mapper.mcp.tools import MapperTools

    db = tmp_path / "m.db"
    with Store(db) as s:
        ingest_stream(s, iter(_log(1201)), cfg, 469_800_000, "collector", start=T0.replace(tzinfo=None))
    tools = MapperTools(cfg, db)
    fp = tools.get_fingerprint(469.8, days=100000)
    resp = next(e for e in fp["events"] if e["kind"] == "data_response")["top_payloads"][0]
    assert "1201" not in resp["text"] and str(GATEWAY) not in resp["text"], resp
    assert resp["tgt"] in resp["text"] and resp["src"] in resp["text"]     # the same pseudonyms as the fields
    assert resp["text"].endswith("ACK - Success")
    # numbers that are not this record's radios stay; with show_radio_ids nothing changes
    assert tools.redact({"src": 5, "text": "SRC: 5; LSN 15; 55"})["text"] == f"SRC: {tools.radio(5)}; LSN 15; 55"
    tools.show_radio_ids = True
    assert tools.redact({"src": 5, "text": "SRC: 5"}) == {"src": 5, "text": "SRC: 5"}


def test_crc_err_counts_bursts_not_error_lines(tmp_path, cfg):
    lines = [
        "14:20:00 Sync: +DMR   slot1  [slot2] | Color Code=01 | DATA\x1b[0m\x1b[31m (CRC ERR) \x1b[0m",   # a burst with an error
        "14:20:00 Sync: +DMR  [slot1]  slot2  | Color Code=01 | VLC ",
        " Protected Control Signalling Block(s) (CRC ERR) ",                                           # line-level
        "\x1b[31m SLCO CRC ERR\x1b[0m",
        "14:20:01 Sync: +DMR  [slot1]  slot2  | Color Code=01 | IDLE ",
    ]
    with Store(tmp_path / "m.db") as s:
        stats = ingest_stream(s, iter(lines), cfg, 469_800_000, "collector", start=T0.replace(tzinfo=None))
        bursts, errs = s.conn.execute("SELECT SUM(bursts), SUM(crc_err) FROM observations").fetchone()
    assert (bursts, errs) == (3, 1)
    assert stats.quality["crc_err"] == 1 and stats.quality["slco_crc"] == 1 and stats.quality["burst_err"] == 1


def test_identity_counts_in_a_session_summary_are_not_pseudonymised(tmp_path, cfg):
    """Session 12, 2026-10-31: "radio-69eed6" on 456.8 and 457.6 was the pseudonym of the count 1."""
    from dmr_mapper.mcp.tools import MapperTools, _legacy_identity_counts

    db = tmp_path / "m.db"
    with Store(db) as s:
        stats = ingest_stream(s, iter(_log(1201)), cfg, 469_800_000, "collector", start=T0.replace(tzinfo=None))
    assert stats.to_dict()["identities"] == {"radios": 2, "talkgroups": 2}     # the gateway, 1201; TG 1, the gateway early on
    tools = MapperTools(cfg, db)
    old = {"carriers": {"456800000": {"identities": {"radio": 1, "tg": 3}}, "457600000": {"identities": {"radio": 1}}}}
    out = tools.redact(_legacy_identity_counts(old))
    assert out["carriers"]["456800000"]["identities"] == {"radios": 1, "talkgroups": 3}
    assert out["carriers"]["457600000"]["identities"] == {"radios": 1}
    assert str(tools.redact({"radio": 1})["radio"]).startswith("radio-")   # a real radio key still is one
