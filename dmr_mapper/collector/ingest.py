"""Live ingest of one decoder's lines into the store (the daemon is the single writer: every
decoder's lines arrive on one queue and are written from one thread)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..db import Store
from ..ingest.dsdfme import LineParser, ParseContext
from ..ingest.writer import Aggregator, IngestStats


class CarrierIngest:
    def __init__(self, store: Store, cfg: dict[str, Any], freq_hz: int, snr_db: float | None = None, source: str = "collector"):
        tz = ZoneInfo(str(cfg.get("timezone") or "UTC"))
        self.store = store
        self.freq_hz = freq_hz
        now = int(time.time())
        band = next((str(b.get("name")) for b in cfg.get("survey", {}).get("bands", []) or []
                     if int(b.get("start_hz", 0)) <= freq_hz <= int(b.get("stop_hz", 0))), None)
        self.carrier_id = store.upsert_carrier(freq_hz, now, band)
        self.run_id = store.start_ingest_run(source, None, self.carrier_id, now, None)
        ctx = ParseContext(freq_hz=freq_hz, start_time=datetime.now(tz=tz), source=source, time_is_rf=True, tz=tz)
        self.stats = IngestStats(source=source, path=None, freq_hz=freq_hz, carrier_id=self.carrier_id)
        self.parser = LineParser(ctx)
        self.agg = Aggregator(store, self.carrier_id, source, int(cfg.get("ingest", {}).get("bucket_minutes", 15)), self.stats, snr_db=snr_db)
        self.pos_syncs = 0
        self.neg_syncs = 0
        self.lines = 0
        self.finished = False

    def feed(self, line: str) -> None:
        self.lines += 1
        if "Sync: +DMR" in line:
            self.pos_syncs += 1
        elif "Sync: -DMR" in line:
            self.neg_syncs += 1
        for ev in self.parser.feed(line):
            self.agg.feed(ev)

    def flush_partial(self) -> None:
        self.agg.flush(partial_before=int(time.time()))

    def finish(self) -> IngestStats:
        if self.finished:
            return self.stats
        self.finished = True
        for ev in self.parser.finish():
            self.agg.feed(ev)
        self.agg.flush()
        self.stats.lines, self.stats.parsed = self.parser.lines, self.parser.parsed
        self.store.finish_ingest_run(self.run_id, int(time.time()), self.stats.lines, self.stats.parsed, self.stats.unrecognized)
        self.store.commit()
        return self.stats

    def summary(self) -> dict[str, Any]:
        st = self.stats
        return {
            "freq_hz": self.freq_hz, "lines": self.lines, "syncs": self.pos_syncs, "inverted_syncs": self.neg_syncs,
            "bursts": dict(st.bursts), "structural": dict(st.structural), "variant": st.variant,
            "variant_confidence": st.variant_confidence, "unrecognized": st.unrecognized_total, "identities": dict(st.identities),
        }
