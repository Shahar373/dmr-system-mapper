"""Phase 3c: GPS track and positions, radio-position privacy, retention, one-button survey+decode."""

from __future__ import annotations

import copy
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from dmr_mapper.collector import state as st
from dmr_mapper.collector.daemon import Daemon
from dmr_mapper.collector.gps import Fix, GpsReader, NmeaState, distance_m, device_users, open_gps
from dmr_mapper.collector.sdr import SynthCarrier, SyntheticBackend, SyntheticSource
from dmr_mapper.db import Store
from dmr_mapper.db.retention import prune


def _nmea(body: str) -> str:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"${body}*{c:02X}\r\n"


RMC = _nmea("GPRMC,135000.00,A,3205.4078,N,03453.7564,E,12.3,90.0,301026,,,A")
GGA = _nmea("GPGGA,135000.00,3205.4078,N,03453.7564,E,1,08,1.0,45.2,M,18.0,M,,")


def test_nmea_parsing():
    n = NmeaState()
    assert n.feed(GGA) is None and n.alt_m == 45.2 and n.sats == 8
    f = n.feed(RMC)
    assert f is not None and abs(f.lat - 32.09013) < 1e-5 and abs(f.lon - 34.89594) < 1e-5 and f.fix == "3d"
    assert abs(f.speed_mps - 12.3 * 0.514444) < 1e-6
    assert n.feed(RMC.replace("A,3205", "V,3205")) is None                  # no fix (and bad checksum)
    assert n.feed(_nmea("GPRMC,135000.00,V,,,,,,,301026,,,N")) is None
    assert n.feed("$GPRMC,garbage*00") is None
    assert 490 < distance_m(Fix(0, 32.0, 34.0), Fix(0, 32.0045, 34.0)) < 510


def test_gpsd_source_is_preferred_and_read(cfg):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        while True:                       # like gpsd: many clients (open_gps probes first)
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=client, args=(conn,), daemon=True).start()

    def client(conn):
        if not conn.recv(100):
            conn.close()
            return
        for i in range(30):
            msg = {"class": "TPV", "mode": 3, "lat": 32.09 + i * 0.0001, "lon": 34.89, "altMSL": 40.0, "speed": 10.0}
            try:
                conn.sendall((json.dumps(msg) + "\n").encode())
            except OSError:
                break
            time.sleep(0.05)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    c = copy.deepcopy(cfg)
    c["gps"] = {"source": "auto", "gpsd_host": "127.0.0.1", "gpsd_port": port, "track_every_s": 0, "track_min_move_m": 50}
    g = open_gps(c)
    assert g.kind == "gpsd"
    g.start()
    deadline = time.time() + 5
    while time.time() < deadline and g.fixes < 20:
        time.sleep(0.05)
    f = g.fix()
    g.stop()
    srv.close()
    assert f is not None and f.source == "gpsd" and f.fix == "3d"
    pts = g.track()
    assert 2 <= len(pts) < g.fixes                                   # thinned: one per >= 50 m
    assert g.track() == []


def test_serial_source_reads_a_tty_and_backs_off_when_someone_else_has_it(cfg, tmp_path):
    master, slave = os.openpty()
    dev = os.ttyname(slave)
    c = copy.deepcopy(cfg)
    c["gps"] = {"source": "serial", "device": dev, "track_every_s": 0, "track_min_move_m": 0}
    os.close(slave)
    # another program holding the device: we stay away
    holder = subprocess.Popen([sys.executable, "-c", f"import time; f = open({dev!r}, 'rb', buffering=0); time.sleep(20)"])
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not device_users(dev):
            time.sleep(0.1)
        g = open_gps(c)
        assert g.kind == "none" and "in use" in g.describe() and "gpsd" in g.describe()
    finally:
        holder.terminate()
        holder.wait()
    g = open_gps(c)
    assert g.kind == "serial"
    g.start()
    time.sleep(0.3)
    for _ in range(5):
        os.write(master, (GGA + RMC).encode())
        time.sleep(0.1)
    deadline = time.time() + 3
    while time.time() < deadline and g.fix() is None:
        time.sleep(0.05)
    f = g.fix()
    g.stop()
    os.close(master)
    assert f is not None and f.source == "serial" and abs(f.lat - 32.09013) < 1e-5, g.error


def test_no_gps_never_blocks(cfg):
    c = copy.deepcopy(cfg)
    c["gps"] = {"source": "auto", "gpsd_port": 1, "device": "/nonexistent/gps"}
    g = open_gps(c)
    assert g.kind == "none" and g.start().fix() is None and g.track() == []
    c["gps"] = {"source": "none"}
    assert "disabled" in open_gps(c).describe()


def test_migration_removes_radio_coordinates(tmp_path):
    db = tmp_path / "old.db"
    s = Store(db)
    s.conn.execute("DROP TABLE positions")              # pretend the database is at version 3
    for stmt in ("DROP TABLE keyups", "DROP TABLE names", "ALTER TABLE systems DROP COLUMN anchor", "ALTER TABLE systems DROP COLUMN name",
                 "ALTER TABLE sites DROP COLUMN anchor", "ALTER TABLE sites DROP COLUMN name"):
        s.conn.execute(stmt)                            # (and undo version 5)
    s.conn.execute("UPDATE schema_meta SET version = 3")
    s.conn.commit()
    s.conn.execute("ALTER TABLE survey_hits DROP COLUMN lat")
    s.conn.execute("ALTER TABLE survey_hits DROP COLUMN lon")
    cid = s.upsert_carrier(469_350_000, 1)
    s.add_csbk_event(cid, 1, 1, 1, "lrrp", {"src": 1, "tgt": 2, "direction": "response", "lat": 32.09, "lon": 34.89}, "a")
    s.add_csbk_event(cid, 1, 1, 1, "lrrp", {"src": 3, "tgt": 2, "direction": "response", "lat": None, "lon": None}, "b")
    s.commit()
    s.close()
    with Store(db) as s:
        assert s.schema_version() == 5
        payloads = [json.loads(r["payload"]) for r in s.rows("SELECT payload FROM csbk_events ORDER BY id")]
    assert payloads == [{"src": 1, "tgt": 2, "direction": "response", "has_position": True},
                        {"src": 3, "tgt": 2, "direction": "response", "has_position": False}]


def test_retention_deletes_old_identity_data_only(store, cfg):
    now = 1_800_000_000
    old, new = now - 100 * 86400, now - 86400
    cid = store.upsert_carrier(469_350_000, old)
    for ts, key in ((old, "old"), (new, "new")):
        store.add_csbk_event(cid, ts, 1, 1, "call", {"slot": 1, "src": 7, "tgt": 9, "group": True}, key)
        store.add_csbk_event(cid, ts, 1, 1, "tier3_sys_parms", {"net_id": 2, "site_id": 1}, key)
    a = store.upsert_identity("radio", 7, old)
    b = store.upsert_identity("radio", 8, new)
    store.add_sighting(a, cid, old // 86400, 1, ["src"])
    store.add_sighting(b, cid, new // 86400, 1, ["src"])
    store.conn.execute("INSERT INTO positions (session_id, ts, lat, lon) VALUES (1, ?, 32, 34)", (old,))
    for started, ex in ((old, "SLOT 1 TGT=9 SRC=7 Weird Call"), (new, "SLOT 1 TGT=9 SRC=8 Weird Call")):
        run = store.start_ingest_run("collector", None, cid, started, None)
        store.finish_ingest_run(run, started + 60, 1, 0, {"SLOT # TGT=# SRC=# Weird Call": (1, ex)})
    store.commit()
    out = prune(store, cfg, now=now)
    examples = [r["example"] for r in store.rows("SELECT example FROM unrecognized_lines ORDER BY run_id")]
    assert examples == ["SLOT # TGT=# SRC=# Weird Call", "SLOT 1 TGT=9 SRC=8 Weird Call"]   # old example reduced to its pattern
    assert out["identity_events"] == 1 and out["identity_sightings"] == 1 and out["identities"] == 1
    kinds = sorted(r["kind"] for r in store.rows("SELECT kind FROM csbk_events"))
    assert kinds == ["call", "tier3_sys_parms", "tier3_sys_parms"]          # networks are kept, old calls are not
    assert [r["value"] for r in store.rows("SELECT value FROM identities")] == [8]
    assert store.scalar("SELECT COUNT(*) FROM positions") == 1              # the receiver's own track stays (track_days 0)


def test_one_button_survey_then_decode_with_gps(tmp_path, cfg, monkeypatch):
    from conftest import REPO
    c = copy.deepcopy(cfg)
    c["survey"]["bands"] = [{"name": "t", "start_hz": 469_000_000, "stop_hz": 470_000_000, "raster_hz": 12_500}]
    c["survey"]["dwell_s"] = 0.5
    c["decode"].update({"dsd_fme_bin": str(REPO / "tests" / "fake_dsd_fme.py"), "seconds_per_carrier": 3, "max_instances": 2,
                        "tcp_base_port": 7555})
    c["daemon"].update({"tick_s": 0.2, "sdr_probe_interval_s": 1})
    c["power"]["state_file"] = ""
    fake = GpsFake()
    monkeypatch.setattr("dmr_mapper.collector.daemon.open_gps", lambda cfg: fake)
    src = SyntheticSource(2_000_000, 469_500_000, [SynthCarrier(469_350_000, 25, None), SynthCarrier(469_650_000, 22, None)])
    db = tmp_path / "a.db"
    d = Daemon(db, c, SyntheticBackend(src))
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    client = Store(db)
    try:
        time.sleep(0.5)
        cmd = st.wait_for_command(client, st.post_command(client, "tui", "start_survey", {"then_decode": True, "budget_s": 90, "total_budget_s": 90}), 10)
        assert cmd["status"] == "done", cmd
        deadline = time.time() + 60
        stopped = False
        while time.time() < deadline:
            ss = st.sessions(client)
            if len(ss) == 2 and ss[0]["status"] == "running" and not stopped:
                time.sleep(4)                                     # let it decode one slice, then stop it
                st.post_command(client, "test", "stop")
                stopped = True
            if len(ss) == 2 and ss[0]["status"] in ("done", "stopped", "failed"):
                break
            time.sleep(0.3)
        decode, survey = st.sessions(client)[:2]
        assert survey["kind"] == "survey" and survey["status"] == "done" and 60 <= survey["summary"]["then_decode_s"] <= 90
        assert decode["kind"] == "decode" and decode["budget_s"] == survey["summary"]["then_decode_s"], decode
        assert decode["status"] in ("done", "stopped"), decode["summary"]
        hits = [dict(r) for r in client.rows("SELECT freq_hz, lat, lon FROM survey_hits")]
        assert {h["freq_hz"] for h in hits} >= {469_350_000, 469_650_000}
        assert all(h["lat"] is not None and 32 < h["lat"] < 33 for h in hits)
        assert client.scalar("SELECT COUNT(*) FROM positions") >= 2
        assert survey["summary"]["gps"] == "fake gps" and survey["summary"]["position_end"]["lat"] > 32
    finally:
        d.stop_event.set()
        th.join(timeout=20)
        client.close()


class GpsFake:
    kind = "fake"
    error = None

    def __init__(self):
        self.n = 0

    def describe(self):
        return "fake gps"

    def start(self):
        return self

    def stop(self):
        pass

    def fix(self):
        self.n += 1
        return Fix(int(time.time()), 32.09 + self.n * 1e-4, 34.89, 40.0, 12.0, "3d", "fake")

    def track(self):
        return [self.fix()]
