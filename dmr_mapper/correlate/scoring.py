"""Confidence combination (DESIGN.md §5): weight × raw × decay, noisy-OR, contradiction penalty,
Tier II cap, labels."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .model import EVIDENCE_CLASS, STRONG_TYPES, TIER2_TYPES, LinkScore, ScoredEvidence

DEFAULT_WEIGHTS = {
    "explicit_neighbor": 0.90, "explicit_lcn_freq": 0.95, "explicit_same_site": 0.70, "explicit_same_system": 0.80,
    "behavioral_rest_hop": 0.50, "behavioral_lsn_table": 0.60, "behavioral_lcn_keyup": 0.70, "identity_radio": 0.30, "identity_talkgroup": 0.15, "identity_slco": 0.20,
    "rf_profile": 0.10, "manual": 1.00,
}
DEFAULT_DECAY_DAYS = {"explicit": 30, "behavioral": 14, "identity": 30, "rf": 7, "manual": 0}
DEFAULT_LABELS = {"confirmed": 0.85, "probable": 0.60, "possible": 0.30}
CONTRADICTION_PENALTY = 0.2
CONTRADICTION_MIN_WEIGHT = 0.1


class ScoringConfig:
    def __init__(self, cfg: Mapping[str, Any] | None = None):
        corr = dict((cfg or {}).get("correlator", {}) or {})
        self.weights = {**DEFAULT_WEIGHTS, **(corr.get("weights") or {})}
        self.decay_days = {**DEFAULT_DECAY_DAYS, **(corr.get("decay_days") or {})}
        self.labels = {**DEFAULT_LABELS, **(corr.get("labels") or {})}
        self.tier2_max = float(corr.get("tier2_max_confidence", 0.60))
        self.window_days = int(corr.get("window_days", 30))
        self.same_site_max_span_hz = int((cfg or {}).get("survey", {}).get("same_site_max_span_hz", 5_000_000))

    def weight(self, etype: str) -> float:
        base = etype.removesuffix("_contradiction")
        return float(self.weights.get(base, self.weights.get(etype, 0.5)))

    def decay(self, etype: str, observed_at: int, now: int) -> float:
        cls = EVIDENCE_CLASS.get(etype, "identity")
        tau = float(self.decay_days.get(cls, 30))
        if tau <= 0:
            return 1.0
        age_days = max(0.0, (now - observed_at) / 86400.0)
        return math.exp(-age_days / tau)

    def label(self, score: float, has_strong: bool) -> str:
        if score >= self.labels["confirmed"] and has_strong:
            return "confirmed"
        if score >= self.labels["probable"]:
            return "probable"
        if score >= self.labels["possible"]:
            return "possible"
        return "weak"


def score_rows(rows: Iterable[Mapping[str, Any]], cfg: ScoringConfig, now: int) -> LinkScore:
    """``rows`` are evidence rows (type, weight?, raw_score, observed_at, first_observed_at,
    support_count, source, details). Returns the combined link score."""
    scored: list[ScoredEvidence] = []
    positive = 1.0
    contradictions = 0
    has_strong = False
    only_tier2 = True
    for r in rows:
        etype = str(r["type"])
        raw = float(r["raw_score"])
        weight = float(r["weight"]) if r.get("weight") is not None else cfg.weight(etype)
        decay = cfg.decay(etype, int(r["observed_at"]), now)
        contribution = weight * raw * decay
        cls = EVIDENCE_CLASS.get(etype, "identity")
        details = r.get("details") or {}
        if isinstance(details, str):
            import json
            details = json.loads(details)
        scored.append(ScoredEvidence(etype, cls, weight, raw, int(r["observed_at"]), int(r["first_observed_at"]),
                                     int(r.get("support_count", 1)), str(r.get("source", "auto")), decay, contribution, details))
        if etype.endswith("_contradiction"):
            if weight * decay > CONTRADICTION_MIN_WEIGHT:
                contradictions += 1
            continue
        if raw <= 0:
            continue
        positive *= 1.0 - min(0.999, contribution)
        if etype in STRONG_TYPES:
            has_strong = True
        if etype not in TIER2_TYPES:
            only_tier2 = False
    positive = 1.0 - positive
    penalty = CONTRADICTION_PENALTY ** contradictions
    score = positive * penalty
    if only_tier2:
        score = min(score, cfg.tier2_max)
    score = round(score, 4)
    return LinkScore(score, cfg.label(score, has_strong), only_tier2, round(positive, 4), penalty, contradictions, scored)
