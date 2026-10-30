"""Connect Plus, Tier III and Hytera XPT parsers, tested on synthetic lines (no real capture yet)."""

from __future__ import annotations

from dmr_mapper.ingest import ingest_log_file
from dmr_mapper.ingest.events import UNTESTED_ON_AIR

from conftest import SYNTHETIC


def _events(store, kind=None):
    rows = store.rows("SELECT kind, payload FROM csbk_events ORDER BY id")
    import json
    out = [(r["kind"], json.loads(r["payload"])) for r in rows]
    return [p for k, p in out if k == kind] if kind else out


def test_connect_plus(store, cfg):
    st = ingest_log_file(store, SYNTHETIC / "dsd_fme_451237500_20260801_120000.log", cfg)
    assert st.unrecognized_total == 0 and st.variant == "conplus"
    assert st.structural["conplus_sys"] == 1 and st.structural["conplus_adjacent"] == 2 and st.structural["grant"] == 2
    sys_ = _events(store, "conplus_sys")[0]
    assert sys_ == {"role": "control", "net_id": 3, "site_id": 12}
    adj = _events(store, "conplus_adjacent")
    assert {"sites": [11, 13, 14]} in adj and {"sites": []} in adj
    grants = _events(store, "grant")
    assert {"variant": "conplus", "kind": "group", "tgt": 101, "src": 2001, "lcn": 4, "ts": 2} in grants
    assert {"variant": "conplus", "kind": "data", "tgt": 2002, "lcn": 5, "ts": 1} in grants
    assert set(st.untested_kinds) == {"conplus_adjacent", "conplus_sys"}
    assert st.identities == {"radio": 2, "tg": 1}


def test_tier3(store, cfg):
    st = ingest_log_file(store, SYNTHETIC / "dsd_fme_165125000_20260801_120000.log", cfg)
    assert st.unrecognized_total == 0 and st.variant == "tier3"
    sysp = _events(store, "tier3_sys_parms")
    aloha = next(p for p in sysp if p["source"] == "c_aloha_sys_parms")
    # "Net ID: 1; Site ID: 7" without a sub-site is DSD-FME's raw numbering: kept as -D 0 prints it (net 2, site 1.8)
    assert (aloha["net_id"], aloha["site_id"], aloha["sub_site"]) == (2, 1, 8) and aloha["sys_code"] == "1A2B" and aloha["model"] == "Small"
    slc = next(p for p in sysp if p["source"] == "slc_c_sys_parms")
    assert slc["reg"] == 1
    bcasts = {p["a_type"]: p for p in _events(store, "tier3_bcast")}
    adj = bcasts["adjacent_site"]
    assert adj["online"] is True and adj["cc_pri"] == 1 and adj["ac_pri"] == 2 and adj["lpcn"] == 1234
    assert (adj["adj_net_id"], adj["adj_site_id"], adj["adj_sub_site"]) == (2, 1, 9) and adj["sys_code"] == "1A2B"
    cf = bcasts["chan_freq"]
    assert cf["apcn"] == 410 and cf["rx_freq_hz"] == 165_125_000 and cf["tx_freq_hz"] == 170_125_000 and cf["rx_step_hz"] == 12500
    assert bcasts["vote_now"]["lpcn"] is None
    assert "gen_site_params" in bcasts and bcasts["gen_site_params"]["sys_code"] == "1A2B"
    grant = _events(store, "grant")[0]
    assert grant == {"variant": "tier3", "kind": "group", "code": "TV_GRANT", "lpcn": 12, "ts": 1, "lpcn_ts": 23,
                     "tgt": 101, "src": 2001, "freq_hz": 165_125_000}


def test_xpt(store, cfg):
    st = ingest_log_file(store, SYNTHETIC / "dsd_fme_410012500_20260801_120000.log", cfg)
    assert st.unrecognized_total == 0 and st.variant == "xpt"
    status = _events(store, "xpt_site_status")[0]
    assert status["free_lcn"] == 3 and status["sn"] == 1 and status["lcns"] == [1, 2]
    assert status["lsn_states"] == {"1": "idle", "2": "idle", "3": "priv", "4": "null"}
    adj = _events(store, "xpt_adjacent")[0]
    assert adj == {"sn": 1, "sites": [{"site": 2, "free": 1}, {"site": 3, "free": 4}]}
    assert _events(store, "xpt_slco")[0] == {"free_lcn": 3, "pri_lcn": 1, "pri_hash": "2A"}
    call = _events(store, "call")[0]
    assert call["xpt"] and call["free_lcn"] == 3 and call["tgt"] == 101


def test_untested_flag_is_only_for_variants_without_samples():
    assert "capplus_channel_status" not in UNTESTED_ON_AIR
    assert {"conplus_sys", "tier3_bcast", "xpt_site_status"} <= UNTESTED_ON_AIR
