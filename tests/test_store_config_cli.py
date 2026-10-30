from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from dmr_mapper.config import deep_merge, load_config
from dmr_mapper.db import Store, classify_variant

from conftest import CAPPLUS_LOG, REPO


def test_deep_merge_keeps_nested_defaults():
    base = {"survey": {"dwell_s": 3, "bands": [1]}, "mcp": {"bind": "a", "port": 1}}
    out = deep_merge(base, {"mcp": {"bind": "b"}, "survey": {"bands": [2]}})
    assert out == {"survey": {"dwell_s": 3, "bands": [2]}, "mcp": {"bind": "b", "port": 1}}
    assert base["mcp"]["bind"] == "a"


def test_example_config_loads_and_local_overrides(tmp_path, monkeypatch):
    local = tmp_path / "config.local.yaml"
    local.write_text("mcp:\n  bind: 100.1.2.3\n", encoding="utf-8")
    cfg = load_config(local)
    assert cfg["mcp"]["bind"] == "100.1.2.3" and cfg["mcp"]["port"] == 8765
    assert [b["name"] for b in cfg["survey"]["bands"]] == ["vhf-high", "uhf"]
    monkeypatch.chdir(tmp_path)
    assert load_config()["mcp"]["bind"] == "100.1.2.3"


def test_store_migrates_and_reopens(tmp_path):
    p = tmp_path / "m.db"
    with Store(p) as s:
        assert s.schema_version() == 4
        cid = s.upsert_carrier(168863000, 100)
        assert s.upsert_carrier(168863000, 50) == cid
    with Store(p, create=False) as s:
        row = s.rows("SELECT first_seen, last_seen FROM carriers")[0]
        assert (row["first_seen"], row["last_seen"]) == (50, 100)


def test_missing_env_config_is_an_error(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("DMR_MAPPER_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        load_config()


def test_store_rolls_back_on_exception(tmp_path):
    p = tmp_path / "rb.db"
    try:
        with Store(p) as s:
            s.upsert_carrier(1, 0)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with Store(p, create=False) as s:
        assert s.scalar("SELECT COUNT(*) FROM carriers") == 0


def test_csbk_event_dedup(store):
    cid = store.upsert_carrier(1, 0)
    assert store.add_csbk_event(cid, 10, 1, 2, "capplus_adjacent", {"sites": []}, "k") is True
    assert store.add_csbk_event(cid, 20, 1, 2, "capplus_adjacent", {"sites": []}, "k") is False
    row = store.rows("SELECT ts, last_ts, count FROM csbk_events")[0]
    assert (row["ts"], row["last_ts"], row["count"]) == (10, 20, 2)


def test_classify_variant():
    assert classify_variant({"capplus_channel_status": 50, "activity_update": 3})[0] == "capplus"
    assert classify_variant({"tier3_bcast": 2}) == ("tier3", 0.7)
    assert classify_variant({"activity_update": 2}) == ("tier2", 0.4)
    assert classify_variant({}, bursts=100) == ("tier2", 0.3)
    assert classify_variant({}, bursts=5) == ("unknown", 0.0)


def test_cli_ingest_and_status(tmp_path):
    db = tmp_path / "cli.db"
    env = {"PYTHONPATH": str(REPO)}
    r = subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--db", str(db), "ingest", str(CAPPLUS_LOG), "--json"],
                       capture_output=True, text=True, cwd=REPO, env={**env, "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data[0]["structural"]["capplus_channel_status"] == 9245 and data[0]["unrecognized_total"] == 0
    r = subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--db", str(db), "status"], capture_output=True, text=True, cwd=REPO,
                       env={**env, "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and "168.8630 MHz" in r.stdout and "variant=capplus" in r.stdout


def test_cli_stream_mode(tmp_path):
    db = tmp_path / "stream.db"
    text = Path(CAPPLUS_LOG).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[:2000]
    r = subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--db", str(db), "ingest", "-", "--freq", "168863000",
                        "--start", "2026-05-09T16:33:44"], input="".join(text), capture_output=True, text=True, cwd=REPO,
                       env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin", "TZ": "UTC"})
    assert r.returncode == 0, r.stderr
    with Store(db, create=False) as s:
        assert s.scalar("SELECT source FROM observations") == "collector"
        assert s.scalar("SELECT SUM(bursts) FROM observations") > 500
        # H4: a naive --start is interpreted in the configured zone (Asia/Jerusalem), not the system one
        first = s.scalar("SELECT MIN(bucket_start) FROM observations")
        assert first == 1778333400   # 2026-05-09 13:30:00Z bucket containing 16:33:44+03:00
