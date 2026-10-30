"""Evidence collectors. Each ``collect(ctx, cfg) -> list[Evidence]``."""

from __future__ import annotations

from typing import Callable

from ..context import Context
from ..model import Evidence
from ..scoring import ScoringConfig
from . import behavioral, capmax, explicit, identity, rf

Collector = Callable[[Context, ScoringConfig], list[Evidence]]

COLLECTORS: dict[str, Collector] = {
    "explicit_same_site": explicit.same_site,
    "explicit_same_system": explicit.same_system,
    "explicit_neighbor": explicit.neighbor,
    "explicit_lcn_freq": explicit.lcn_freq,
    "behavioral_rest_hop": behavioral.rest_hop,
    "behavioral_lsn_table": behavioral.lsn_table,
    "behavioral_lcn_keyup": capmax.lcn_keyup,
    "identity_radio": identity.radio,
    "identity_talkgroup": identity.talkgroup,
    "identity_slco": identity.slco_pattern,
    "rf_profile": rf.profile,
}

__all__ = ["COLLECTORS", "Collector"]
