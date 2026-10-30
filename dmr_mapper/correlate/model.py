"""Shared types for the evidence engine (DESIGN.md §4–§5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LINK_KINDS = ("same_site", "same_system", "neighbor_site")
LABELS = ("confirmed", "probable", "possible", "weak")

# Evidence type → class. The class carries the config weight and decay constant.
EVIDENCE_CLASS: dict[str, str] = {
    "explicit_same_site": "explicit",
    "explicit_same_system": "explicit",
    "explicit_neighbor": "explicit",
    "explicit_lcn_freq": "explicit",
    "explicit_lcn_grant": "explicit",
    "explicit_same_site_contradiction": "explicit",
    "explicit_same_system_contradiction": "explicit",
    "behavioral_rest_hop": "behavioral",
    "behavioral_lsn_table": "behavioral",
    "behavioral_lcn_keyup": "behavioral",
    "identity_radio": "identity",
    "identity_talkgroup": "identity",
    "identity_slco": "identity",
    "rf_profile": "rf",
    "manual": "manual",
}

# Evidence types whose presence lets a link reach "confirmed" (DESIGN §5).
STRONG_TYPES = {t for t, c in EVIDENCE_CLASS.items() if c in ("explicit", "manual") and not t.endswith("_contradiction")}

# Evidence types that only Tier II carriers can produce; a link with nothing else is "tier2_only".
TIER2_TYPES = {"identity_radio", "identity_talkgroup", "identity_slco", "rf_profile"}


@dataclass(slots=True)
class Evidence:
    """One piece of evidence between two carriers (frequencies), as produced by a collector."""

    a_freq: int
    b_freq: int
    kind: str                       # link kind: same_site | same_system | neighbor_site
    type: str                       # evidence type, key of EVIDENCE_CLASS
    raw: float                      # 0..1 strength before weight and decay (0 = contradiction)
    observed_at: int                # newest supporting observation (epoch)
    first_observed_at: int
    support: int = 1
    details: dict[str, Any] = field(default_factory=dict)
    lcn_hints: dict[int, int] = field(default_factory=dict)   # freq → LCN learned while collecting

    def ordered(self) -> "Evidence":
        if self.a_freq <= self.b_freq:
            return self
        return Evidence(self.b_freq, self.a_freq, self.kind, self.type, self.raw, self.observed_at,
                        self.first_observed_at, self.support, self.details, self.lcn_hints)


@dataclass(slots=True)
class ScoredEvidence:
    type: str
    cls: str
    weight: float
    raw: float
    observed_at: int
    first_observed_at: int
    support: int
    source: str
    decay: float            # exp(−age/τ), 1.0 when no decay
    contribution: float     # weight × raw × decay
    details: dict[str, Any]


@dataclass(slots=True)
class LinkScore:
    score: float
    label: str
    tier2_only: bool
    positive: float
    penalty: float
    contradictions: int
    rows: list[ScoredEvidence]


@dataclass
class CorrelateReport:
    now: int
    window_days: int
    evidence_collected: dict[str, int] = field(default_factory=dict)
    links_scored: int = 0
    links_by_label: dict[str, int] = field(default_factory=dict)
    sites: int = 0
    systems: int = 0
    carriers: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "now": self.now, "window_days": self.window_days, "evidence_collected": self.evidence_collected,
            "links_scored": self.links_scored, "links_by_label": self.links_by_label, "sites": self.sites,
            "systems": self.systems, "carriers": self.carriers, "notes": self.notes,
        }
