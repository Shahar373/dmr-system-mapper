"""Confidence combination rules (DESIGN.md §5)."""

from __future__ import annotations

from dmr_mapper.correlate.scoring import ScoringConfig, score_rows

NOW = 1_800_000_000


def _row(etype, raw, age_days=0.0, source="auto", weight=None):
    return {"type": etype, "raw_score": raw, "observed_at": int(NOW - age_days * 86400), "first_observed_at": int(NOW - age_days * 86400),
            "support_count": 1, "source": source, "details": "{}", "weight": weight}


def test_single_explicit_evidence():
    cfg = ScoringConfig({})
    ls = score_rows([_row("explicit_same_site", 0.8)], cfg, NOW)
    assert abs(ls.score - 0.56) < 1e-6 and ls.label == "possible" and not ls.tier2_only


def test_noisy_or_combination_from_design_example():
    cfg = ScoringConfig({})
    rows = [_row("explicit_same_site", 0.8), _row("behavioral_rest_hop", 1 - 0.75 ** 6), _row("identity_radio", 0.6)]
    ls = score_rows(rows, cfg, NOW)
    assert 0.78 <= ls.score <= 0.80 and ls.label == "probable"


def test_confirmed_requires_strong_evidence():
    cfg = ScoringConfig({})
    # three behavioural/identity rows can push the number past 0.85 but never the label
    rows = [_row("behavioral_rest_hop", 1.0), _row("behavioral_lsn_table", 1.0), _row("identity_radio", 1.0)]
    ls = score_rows(rows, cfg, NOW)
    assert ls.score >= 0.85 and ls.label == "probable"
    rows.append(_row("explicit_neighbor", 1.0))
    assert score_rows(rows, cfg, NOW).label == "confirmed"


def test_tier2_cap():
    cfg = ScoringConfig({})
    rows = [_row("identity_radio", 1.0), _row("identity_talkgroup", 1.0), _row("rf_profile", 1.0)]
    ls = score_rows(rows, cfg, NOW)
    assert ls.tier2_only and abs(ls.score - 0.4645) < 1e-3 and ls.label == "possible"   # below the cap: untouched
    strong = ScoringConfig({"correlator": {"weights": {"identity_radio": 1.0}}})
    ls = score_rows(rows, strong, NOW)
    assert ls.tier2_only and ls.score == 0.6 and ls.label == "probable"                # capped, never confirmed
    rows.append(_row("behavioral_rest_hop", 0.25))
    ls = score_rows(rows, strong, NOW)
    assert not ls.tier2_only and ls.score > 0.6


def test_decay_and_no_decay_for_manual():
    cfg = ScoringConfig({})
    fresh = score_rows([_row("behavioral_rest_hop", 1.0)], cfg, NOW).score
    old = score_rows([_row("behavioral_rest_hop", 1.0, age_days=14)], cfg, NOW).score
    assert abs(old / fresh - 0.3679) < 0.01          # one e-folding (14 days)
    manual_old = score_rows([_row("manual", 0.9, age_days=365, source="manual")], cfg, NOW)
    assert manual_old.score == 0.9 and manual_old.label == "confirmed"


def test_contradiction_penalty():
    cfg = ScoringConfig({})
    rows = [_row("explicit_same_site", 1.0), _row("explicit_same_site_contradiction", 0.0)]
    ls = score_rows(rows, cfg, NOW)
    assert ls.contradictions == 1 and abs(ls.score - 0.7 * 0.2) < 1e-6 and ls.label == "weak"


def test_config_overrides():
    cfg = ScoringConfig({"correlator": {"weights": {"explicit_same_site": 1.0}, "labels": {"possible": 0.5}, "tier2_max_confidence": 0.4}})
    assert score_rows([_row("explicit_same_site", 0.8)], cfg, NOW).score == 0.8
    assert score_rows([_row("identity_radio", 1.0)], cfg, NOW).score == 0.3
    assert score_rows([_row("explicit_same_site", 0.45)], cfg, NOW).label == "weak"
