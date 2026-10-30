"""Phase 4: names given to systems and sites survive correlator runs (which rebuild those tables)."""

from __future__ import annotations

from dmr_mapper.cli import main
from dmr_mapper.correlate import propose, run
from dmr_mapper.correlate.names import list_names, set_name
from dmr_mapper.export.report import export_all, systems_report

from test_capmax_correlate import CC_A, CC_B, NOW, T7, capmax_store  # noqa: F401  (fixture)


def _system_of(store, freq):
    return store.conn.execute(
        "SELECT sy.* FROM systems sy JOIN sites s ON s.system_id = sy.id JOIN carrier_sites cs ON cs.site_id = s.id"
        " JOIN carriers c ON c.id = cs.carrier_id WHERE c.freq_hz = ?", (freq,)).fetchone()


def _site_of(store, freq):
    return store.conn.execute(
        "SELECT s.* FROM sites s JOIN carrier_sites cs ON cs.site_id = s.id JOIN carriers c ON c.id = cs.carrier_id WHERE c.freq_hz = ?",
        (freq,)).fetchone()


def test_tier3_names_follow_the_network_identity(capmax_store, cfg, tmp_path):
    store = capmax_store
    res = set_name(store, "system", "469.35", "רשת צפון")              # by a carrier's frequency in MHz
    assert res["anchor"] == "tier3:Large:2"
    set_name(store, "site", str(CC_B), "Hilltop <b>")
    assert _system_of(store, T7)["name"] == "רשת צפון"                 # every carrier of the network
    # the tables are rebuilt with new ids: the names come back
    store.conn.execute("UPDATE sites SET anchor = NULL")               # (proves derive writes them again)
    run(store, cfg, now=NOW + 60)
    assert _system_of(store, CC_A)["name"] == "רשת צפון"
    assert _site_of(store, CC_B)["name"] == "Hilltop <b>" and _site_of(store, CC_A)["name"] is None
    _, text = systems_report(store, "possible")
    assert "רשת צפון (Tier III net 2 (Large))" in text and "Hilltop <b> (site 1.3" in text
    paths, _ = export_all(store, cfg, tmp_path / "exp", "weak")
    html = paths[0].read_text(encoding="utf-8")
    data_line = next(line for line in html.splitlines() if line.startswith("const DATA"))
    assert "Hilltop <b> (site 1.3" in data_line                         # in the data, shown through esc()
    assert "Hilltop <b>" not in html.replace(data_line, "") and "esc(v)" in html
    # renaming replaces, --clear removes
    set_name(store, "system", str(_system_of(store, CC_A)["id"]), "North")
    assert [n["name"] for n in list_names(store) if n["kind"] == "system"] == ["North"]
    set_name(store, "site", "469650000", None)
    assert _site_of(store, CC_B)["name"] is None and [n["kind"] for n in list_names(store)] == ["system"]


def test_a_system_without_identity_is_named_by_its_carriers(store, cfg):
    a, b, c = 164_325_000, 165_625_000, 166_000_000
    for f in (a, b, c):
        cid = store.upsert_carrier(f, NOW - 600)
        store.conn.execute("UPDATE carriers SET variant = 'tier2', dominant_cc = 6 WHERE id = ?", (cid,))
    store.commit()
    propose(store, a, b, "same_system", 0.9, "same dispatcher", cfg, now=NOW)
    run(store, cfg, now=NOW)
    res = set_name(store, "system", str(a), "Municipal")
    assert res["anchor"] is None and res["carriers"] == [a, b]
    # a third repeater joins the system later: the name stays with it
    propose(store, b, c, "same_system", 0.9, "same dispatcher", cfg, now=NOW)
    run(store, cfg, now=NOW + 60)
    assert _system_of(store, c)["name"] == "Municipal"
    assert store.scalar("SELECT COUNT(*) FROM systems WHERE name IS NOT NULL") == 1


def test_name_cli(capmax_store, cfg, tmp_path, capsys, monkeypatch):
    db = capmax_store.path
    assert main(["--db", str(db), "name", "system", "469350000", "North", "network"]) == 0
    assert "named 'North network' (anchored on tier3:Large:2)" in capsys.readouterr().out
    assert main(["--db", str(db), "names"]) == 0
    assert "North network" in capsys.readouterr().out
    assert main(["--db", str(db), "name", "site", "999"]) == 2
    assert main(["--db", str(db), "name", "site", "999", "x"]) == 1
    assert "no site 999" in capsys.readouterr().err
