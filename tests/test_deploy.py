"""Phase 3b: snapshot, export bundle, raw debug logs, deploy.sh (dry run) and the archive pull."""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from dmr_mapper.collector.ingest import CarrierIngest
from dmr_mapper.collector.rawlog import RawLog, prune
from dmr_mapper.correlate import run
from dmr_mapper.db import Store
from dmr_mapper.db.snapshot import snapshot
from dmr_mapper.export.report import export_all
from dmr_mapper.ingest import ingest_log_file, ingest_path

from conftest import CAPPLUS_LOG, REPO


def test_snapshot_is_consistent_and_pruned(tmp_path, cfg):
    db = tmp_path / "live.db"
    with Store(db) as s:
        ingest_log_file(s, CAPPLUS_LOG, cfg)
        live = Store(db)                     # an open writer during the snapshot
        live.conn.execute("INSERT INTO carriers (freq_hz, first_seen, last_seen) VALUES (1, 1, 1)")
        out = tmp_path / "snaps"
        out.mkdir()
        for day in ("20260101", "20260102", "20260103"):
            (out / f"mapper-{day}.db").write_bytes(b"old")
        path, removed = snapshot(db, out, keep=2)
        live.conn.rollback()
        live.close()
    assert path.name.startswith("mapper-") and len(list(out.glob("mapper-*.db"))) == 2 and len(removed) == 2
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM carriers").fetchone()[0] == 1          # the uncommitted row is not in it
    assert c.execute("SELECT COUNT(*) FROM csbk_events").fetchone()[0] > 100
    assert not list(out.glob(".*.tmp"))
    path2, _ = snapshot(db, out, keep=2)                                          # same day: replaced, not duplicated
    assert path2 == path and len(list(out.glob("mapper-*.db"))) == 2


def test_export_all_writes_the_bundle(capplus_store, cfg, tmp_path):
    run(capplus_store, cfg, now=1778400000)
    paths, g = export_all(capplus_store, cfg, tmp_path / "x")
    assert [p.name for p in paths] == ["map.html", "map.json", "map.graphml", "systems.txt"]
    assert all(p.stat().st_size > 0 for p in paths)
    assert "168.8630 MHz" in (tmp_path / "x" / "systems.txt").read_text()


def test_raw_logs_rotate_compress_cap_and_reingest(tmp_path, cfg, store):
    c = dict(cfg)
    c["decode"] = dict(cfg["decode"], raw_log_max_mb=0.05, raw_log_keep=3)
    lines = CAPPLUS_LOG.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[:6000]
    log = RawLog(tmp_path, 168_863_000, c)
    for line in lines:
        log.write(line)
    log.close()
    gz = sorted(tmp_path.glob("dsd_fme_168863000_*.log.gz"))
    assert gz and not list(tmp_path.glob("*.log"))
    assert sum(p.stat().st_size for p in gz) <= 0.05e6 * 3
    assert prune(tmp_path, 0) and not list(tmp_path.glob("*.gz"))
    # a kept raw log is a normal capture for `mapper ingest`
    log = RawLog(tmp_path, 168_863_000, dict(cfg))
    for line in lines[:2000]:
        log.write(line)
    log.close()
    one = next(tmp_path.glob("*.log.gz"))
    with gzip.open(one, "rt") as fh:
        assert fh.read().startswith(lines[0])
    st = ingest_log_file(store, one, cfg)
    assert not st.skipped and st.freq_hz == 168_863_000 and st.bursts["csbk"] > 100
    again = ingest_path(store, tmp_path, cfg)
    assert all(r.skipped for r in again)


def test_collector_ingest_keeps_raw_only_when_asked(tmp_path, cfg, store):
    c = dict(cfg)
    c["paths"] = dict(cfg["paths"], raw_logs=str(tmp_path / "raw"))
    c["decode"] = dict(cfg["decode"], keep_raw_logs=False)
    ing = CarrierIngest(store, c, 168_863_000)
    ing.feed("16:33:44 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n")
    ing.finish()
    assert not (tmp_path / "raw").exists()
    c["decode"]["keep_raw_logs"] = True
    ing = CarrierIngest(store, c, 168_863_000)
    ing.feed("16:33:44 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\n")
    ing.finish()
    assert len(list((tmp_path / "raw").glob("*.log.gz"))) == 1


def test_cli_snapshot_and_export_all(tmp_path):
    db = tmp_path / "c.db"
    env = {"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"}
    def mapper(*a):
        return subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--db", str(db), *a], capture_output=True, text=True, cwd=REPO, env=env)
    assert mapper("ingest", str(CAPPLUS_LOG)).returncode == 0
    r = mapper("snapshot", "--dir", str(tmp_path / "s"), "--keep", "1")
    assert r.returncode == 0 and len(list((tmp_path / "s").glob("mapper-*.db"))) == 1, r.stderr
    r = mapper("export", "--all", "--dir", str(tmp_path / "e"))
    assert r.returncode == 0 and len(list((tmp_path / "e").iterdir())) == 4, r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_deploy_dry_run_renders_units_without_machine_values_in_the_repo(tmp_path):
    out = tmp_path / "units"
    r = subprocess.run(["bash", str(REPO / "deploy" / "deploy.sh"), "--dry-run", "--no-pull", "--render-dir", str(out)],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    svc = (out / "dmr-mapper.service").read_text()
    assert f"WorkingDirectory={REPO}" in svc and f"ExecStart={REPO}/.venv/bin/mapper daemon" in svc
    assert "User=" in svc and "@" not in svc.replace("@REPO@", "")
    assert "OnCalendar=*-*-* 03:30:00" in (out / "dmr-mapper-snapshot.timer").read_text()
    assert "mapper snapshot" in (out / "dmr-mapper-snapshot.service").read_text()
    assert "[dry-run] sudo systemctl enable --now dmr-mapper" in r.stdout
    for tpl in (REPO / "deploy").glob("*.in"):                 # templates carry placeholders only
        text = tpl.read_text()
        assert "/home/" not in text and "shahar" not in text


@pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")
def test_archive_pull_keeps_daily_and_sunday_snapshots(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    days = [f"202610{d:02d}" for d in range(1, 31)]      # 2026-10-04, 11, 18, 25 are Sundays
    for d in days:
        c = sqlite3.connect(src / f"mapper-{d}.db")
        c.execute("CREATE TABLE t (x)")
        c.commit()
        c.close()
    (src / "mapper-20261031.db").write_bytes(b"this is not a database at all" * 200)
    (src / "notes.txt").write_text("not pulled")
    dest = tmp_path / "dest"
    r = subprocess.run(["bash", str(REPO / "deploy" / "archive-pull.sh"), f"{src}/", str(dest), "5", "2"],
                       capture_output=True, text=True, env={**os.environ, "ARCHIVE_KEY": "/nonexistent"})
    assert r.returncode == 0, r.stdout + r.stderr
    kept = sorted(p.name for p in dest.glob("mapper-*.db"))
    assert kept == [f"mapper-{d}.db" for d in ("20261018", "20261025", "20261026", "20261027", "20261028", "20261029", "20261030")]
    assert (dest / "mapper-20261031.db.bad").exists() and not (dest / "notes.txt").exists()
