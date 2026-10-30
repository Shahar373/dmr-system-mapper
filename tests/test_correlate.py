"""End-to-end correlator tests on synthetic multi-carrier logs and on samples/."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta

import pytest

from dmr_mapper.correlate import explain, find_link, propose, run
from dmr_mapper.db import Store
from dmr_mapper.export import build, render, to_json, write
from dmr_mapper.ingest import ingest_log_file

import synth
from conftest import REPO, SURVEYOR_ROOT, CAPPLUS_LOG

NOW = int((synth.START + timedelta(hours=2)).timestamp())


def _links(store):
    return [dict(r) for r in store.rows(
        "SELECT l.*, a.freq_hz AS a_freq, b.freq_hz AS b_freq FROM links l JOIN carriers a ON a.id = l.a_carrier_id"
        " JOIN carriers b ON b.id = l.b_carrier_id ORDER BY l.score DESC")]


def _link(links, fa, fb, kind=None):
    a, b = min(fa, fb), max(fa, fb)
    for l in links:
        if l["a_freq"] == a and l["b_freq"] == b and (kind is None or l["kind"] == kind):
            return l
    return None


@pytest.fixture(scope="module")
def synth_store(tmp_path_factory, cfg):
    d = tmp_path_factory.mktemp("synth")
    cap = synth.capplus_site(d / "cap", site=2, cc=2, freqs=[168_500_000, 168_512_500, 168_525_000], minutes=20,
                             calls=[(40, 1, 2102), (100, 1, 2103), (160, 7, 2102)], neighbors=[(1, 3)])
    # an unrelated Cap+ network re-using site number 2 but a different colour code, far away in frequency
    other = synth.capplus_site(d / "other", site=2, cc=9, freqs=[453_000_000, 453_012_500], minutes=5, seed=7)
    t2a = synth.tier2_carrier(d / "t2", 164_300_000, cc=7, radios=[500, 501, 502, 503, 504, 505], tgs=[10, 11], seed=3)
    t2b = synth.tier2_carrier(d / "t2", 164_325_000, cc=7, radios=[500, 501, 502, 503, 504, 505], tgs=[10, 11], seed=4)
    t2c = synth.tier2_carrier(d / "t2", 167_137_500, cc=6, radios=[900, 901], tgs=[55], seed=5)
    t3 = synth.tier3_site(d / "t3", "1A2B", net=1, site=7, cc=9, control_freq=165_125_000,
                          channels={410: 165_137_500, 411: 165_150_000}, adjacent=[8])
    t3tc = synth.tier3_traffic(d / "t3", "1A2B", net=1, site=7, cc=9, freq=165_137_500)
    t3s8 = synth.tier3_site(d / "t3", "1A2B", net=1, site=8, cc=10, control_freq=165_600_000, channels={420: 165_612_500}, adjacent=[7])
    s = Store(d / "synth.db")
    for p in cap + other + [t2a, t2b, t2c, t3, t3tc, t3s8]:
        st = ingest_log_file(s, p, cfg)
        assert st.unrecognized_total == 0, (p, st.unrecognized)
    s.report = run(s, cfg, now=NOW)  # type: ignore[attr-defined]
    yield s
    s.close()


def test_capplus_site_is_linked_by_explicit_and_behavioral_evidence(synth_store):
    links = _links(synth_store)
    for fa, fb in ((168_500_000, 168_512_500), (168_500_000, 168_525_000), (168_512_500, 168_525_000)):
        l = _link(links, fa, fb, "same_site")
        assert l is not None, (fa, fb)
        assert l["label"] in ("probable", "confirmed") and not l["tier2_only"], l
        ex = explain(synth_store, l["id"], None, now=NOW)
        types = {e["type"] for e in ex["evidence"]}
        assert "explicit_same_site" in types and "behavioral_rest_hop" in types, types
        hop = next(e for e in ex["evidence"] if e["type"] == "behavioral_rest_hop")
        assert hop["support"] >= 5


def test_lsn_table_evidence_and_lcn_learning(synth_store):
    links = _links(synth_store)
    with_lsn = [l for l in links if l["kind"] == "same_site" and l["a_freq"] >= 168_500_000 and l["b_freq"] <= 168_525_000
                and any(e["type"] == "behavioral_lsn_table" for e in explain(synth_store, l["id"], None, now=NOW)["evidence"])]
    assert with_lsn, "calls aligned with LSN lists should produce behavioral_lsn_table evidence"
    lcns = {r["freq_hz"]: r["lcn"] for r in synth_store.rows(
        "SELECT c.freq_hz, cs.lcn FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id WHERE c.freq_hz BETWEEN 168500000 AND 168525000")}
    assert lcns == {168_500_000: 1, 168_512_500: 2, 168_525_000: 3}


def test_same_site_number_other_network_is_not_linked(synth_store):
    links = _links(synth_store)
    assert _link(links, 168_500_000, 453_000_000) is None
    assert _link(links, 168_512_500, 453_012_500) is None
    # but the two carriers of the other network ARE linked to each other
    assert _link(links, 453_000_000, 453_012_500, "same_site") is not None


def test_tier2_links_are_capped_and_marked(synth_store):
    links = _links(synth_store)
    l = _link(links, 164_300_000, 164_325_000, "same_system")
    assert l is not None and l["tier2_only"] and l["score"] <= 0.6 and l["label"] != "confirmed"
    ex = explain(synth_store, l["id"], None, now=NOW)
    # the shared talkgroups also show in their Activity Updates (same hashed address): Tier II evidence too
    assert {e["type"] for e in ex["evidence"]} <= {"identity_radio", "identity_talkgroup", "identity_slco", "rf_profile"}
    assert _link(links, 164_300_000, 167_137_500) is None   # different radios, different CC


def test_tier3_system_sites_and_neighbors(synth_store):
    links = _links(synth_store)
    lcn = _link(links, 165_125_000, 165_137_500, "same_site")
    assert lcn is not None and lcn["label"] == "confirmed"
    types = {e["type"] for e in explain(synth_store, lcn["id"], None, now=NOW)["evidence"]}
    assert {"explicit_lcn_freq", "explicit_same_site"} <= types
    nb = _link(links, 165_125_000, 165_600_000, "neighbor_site")
    assert nb is not None and nb["label"] == "confirmed"
    det = next(e for e in explain(synth_store, nb["id"], None, now=NOW)["evidence"] if e["type"] == "explicit_neighbor")
    assert det["details"]["bidirectional"] is True
    sysrow = next(dict(r) for r in synth_store.rows("SELECT * FROM systems WHERE sys_code = '1A2B'"))
    sites = [dict(r) for r in synth_store.rows("SELECT * FROM sites WHERE system_id = ?", (sysrow["id"],))]
    assert {s["site_number"] for s in sites} == {7, 8}
    assert all(s["directly_observed"] for s in sites)


def test_announced_only_site_and_report(synth_store):
    rep = synth_store.report
    assert rep.links_by_label.get("confirmed", 0) >= 2
    cap_sys = next(dict(r) for r in synth_store.rows("SELECT s.* FROM systems s JOIN sites si ON si.system_id = s.id WHERE si.site_number = 2 AND si.cc = 2"))
    sites = [dict(r) for r in synth_store.rows("SELECT * FROM sites WHERE system_id = ?", (cap_sys["id"],))]
    announced = [s for s in sites if not s["directly_observed"]]
    assert [s["site_number"] for s in announced] == [1]
    assert synth_store.scalar("SELECT COUNT(*) FROM carrier_sites") == synth_store.scalar("SELECT COUNT(*) FROM carriers")


def test_rerun_is_stable_and_decays(synth_store, cfg):
    before = {l["id"]: l["score"] for l in _links(synth_store)}
    run(synth_store, cfg, now=NOW)
    after = {l["id"]: l["score"] for l in _links(synth_store)}
    assert before == after
    later = run(synth_store, cfg, now=NOW + 60 * 86400)
    decayed = {l["id"]: l["score"] for l in _links(synth_store)}
    assert all(decayed[i] <= before[i] for i in before) and any(decayed[i] < before[i] for i in before)
    run(synth_store, cfg, now=NOW)   # restore for other tests


def test_propose_manual_evidence(synth_store, cfg):
    res = propose(synth_store, 164_300_000, 167_137_500, "same_system", 0.9, "operator says so", cfg, now=NOW)
    assert res["label"] == "confirmed" and not res["tier2_only"]
    ex = explain(synth_store, res["link_id"], cfg, now=NOW)
    manual = next(e for e in ex["evidence"] if e["source"] == "manual")
    assert manual["details"]["note"] == "operator says so" and manual["decay"] == 1.0
    run(synth_store, cfg, now=NOW)   # the automatic pass must keep it
    assert explain(synth_store, res["link_id"], cfg, now=NOW)["label"] == "confirmed"
    with pytest.raises(ValueError):
        propose(synth_store, 164_300_000, 167_137_500, "friends", 0.5, "", cfg)
    with pytest.raises(KeyError):
        propose(synth_store, 1, 2, "same_site", 0.5, "", cfg)


def test_graph_and_html_export(synth_store, tmp_path):
    g = build(synth_store, min_label="possible")
    carriers = [n for n, d in g.nodes(data=True) if d["kind"] == "carrier"]
    announced = [n for n, d in g.nodes(data=True) if d["kind"] == "site"]
    assert len(carriers) == 11 and len(announced) == 1
    assert all("site_id" in d for _, d in g.nodes(data=True) if d["kind"] == "carrier")
    t2_edges = [d for _, _, d in g.edges(data=True) if d.get("tier2_only")]
    assert t2_edges and all(d["label"] != "confirmed" for d in t2_edges)
    j = to_json(g)
    assert j["systems"] and j["sites"]
    out = tmp_path / "map.html"
    write(g, str(out), generated_at=NOW)
    text = out.read_text(encoding="utf-8")
    assert "<svg" in text and "DATA = {" in text and "168.5000" in text and "T2" in text
    assert text.count('class="node"') == 12
    assert "http://" not in text.split("<script>")[1].split("</script>")[0].replace("http://www.w3.org", "")  # no CDN
    assert 'xmlns="http://www.w3.org/2000/svg"' in text


def test_samples_produce_no_false_links(tmp_path, cfg):
    from dmr_mapper.ingest import ingest_surveyor
    s = Store(tmp_path / "samples.db")
    ingest_log_file(s, CAPPLUS_LOG, cfg)
    ingest_surveyor(s, SURVEYOR_ROOT, cfg)
    rep = run(s, cfg, now=1784000000 + 86400, window_days=90)
    links = _links(s)
    assert all(l["label"] == "weak" for l in links), links
    assert rep.systems == 9
    announced = [dict(r) for r in s.rows("SELECT * FROM sites WHERE directly_observed = 0")]
    assert [a["site_number"] for a in announced] == [1]
    assert s.scalar("SELECT site_number FROM sites WHERE id = (SELECT site_id FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id WHERE c.freq_hz = 168863000)") == 2
    s.close()


def test_cli_phase2_commands(tmp_path):
    db = tmp_path / "cli2.db"
    env = {"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"}
    def mapper(*args):
        return subprocess.run([sys.executable, "-m", "dmr_mapper.cli", "--db", str(db), *args], capture_output=True, text=True, cwd=REPO, env=env)
    assert mapper("ingest", str(CAPPLUS_LOG)).returncode == 0
    r = mapper("correlate", "--now", "2026-05-10T00:00:00Z", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["systems"] == 1
    r = mapper("systems")
    assert r.returncode == 0 and "site 2" in r.stdout and "announced only" in r.stdout
    r = mapper("propose", "168863000", "168863000", "same_site", "0.5")
    assert r.returncode != 0   # a<b constraint: same carrier twice is rejected by SQLite
    html = tmp_path / "m.html"
    r = mapper("export", "--html", str(html), "--graphml", str(tmp_path / "m.graphml"), "--json-path", str(tmp_path / "m.json"))
    assert r.returncode == 0 and html.exists() and (tmp_path / "m.graphml").exists() and json.loads((tmp_path / "m.json").read_text())["nodes"]
    r = mapper("explain", "1")
    assert r.returncode != 0   # no links on a single carrier
