"""End-to-end ingest of samples/surveyor."""

from __future__ import annotations

from datetime import datetime

from dmr_mapper.ingest.surveyor import pick_log, read_candidates, scan

from conftest import SURVEYOR_ROOT, TZ


def test_scan_finds_15_attempts_on_8_carriers():
    attempts = list(scan(SURVEYOR_ROOT, TZ))
    assert len(attempts) == 15
    assert len({a.freq_hz for a in attempts}) == 8
    assert all(a.log_path is not None and a.log_path.name == "dsd_fme_normal_stderr.log" for a in attempts)
    c13 = next(a for a in attempts if a.candidate_id == "C0013" and a.recording_id == "20260713_150256")
    assert c13.freq_hz == 164_537_500
    assert c13.start_time == datetime(2026, 7, 13, 15, 2, 56, tzinfo=TZ)
    assert 11.0 < c13.duration_s < 11.3
    assert c13.status == "dmr_confirmed_clean" and c13.best_inversion == "normal"


def test_pick_log_prefers_non_empty_stderr():
    d = SURVEYOR_ROOT / "C0013" / "20260713_150256" / "iq" / "decoder"
    assert pick_log(d, "normal").name == "dsd_fme_normal_stderr.log"
    assert pick_log(d, "missing") is None


def test_no_candidates_file_in_samples():
    assert read_candidates(SURVEYOR_ROOT) == []


def test_ingest_results(surveyor_store):
    s = surveyor_store
    results = [r for r in s.results if not r.skipped]
    assert len(results) == 15
    assert sum(r.unrecognized_total for r in results) == 0
    assert len(s.carriers()) == 8
    by_freq = {c["freq_hz"]: c for c in s.carriers()}
    assert by_freq[162_525_000]["variant"] == "capplus" and by_freq[162_525_000]["dominant_cc"] == 8
    assert by_freq[162_587_500]["variant"] == "capplus" and by_freq[162_587_500]["dominant_cc"] == 5
    for f in (164_300_000, 164_325_000, 164_537_500, 164_725_000, 165_625_000, 167_137_500):
        assert by_freq[f]["variant"] == "tier2", f
    sites = {r["freq_hz"]: r["site"] for r in s.rows(
        "SELECT c.freq_hz, json_extract(e.payload, '$.site') AS site FROM csbk_events e JOIN carriers c ON c.id = e.carrier_id"
        " WHERE e.kind = 'capplus_site'")}
    assert sites == {162_525_000: 0, 162_587_500: 5}


def test_surveyor_time_is_recording_time(surveyor_store):
    s = surveyor_store
    row = s.rows("SELECT bucket_start, seconds_observed, source FROM observations WHERE carrier_id = "
                 "(SELECT id FROM carriers WHERE freq_hz = 164537500)")
    assert len(row) == 1 and row[0]["source"] == "surveyor"
    start = int(datetime(2026, 7, 13, 15, 0, 0, tzinfo=TZ).timestamp())
    assert row[0]["bucket_start"] == start
    assert 17 < row[0]["seconds_observed"] < 19          # 6.8 s + 11.2 s recordings in the same bucket
    c13 = next(r for r in s.results if r.meta.get("candidate_id") == "C0013" and r.meta.get("recording_id") == "20260713_150256")
    assert c13.first_ts == int(datetime(2026, 7, 13, 15, 2, 56, tzinfo=TZ).timestamp())
    assert c13.structural["activity_update"] == 36 and c13.bursts["voice"] == 47
