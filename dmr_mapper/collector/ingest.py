"""Live ingest of one decoder's lines into the store (the daemon is the single writer: every
decoder's lines arrive on one queue and are written from one thread)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ..db import Store
from ..ingest.dsdfme import LineParser, ParseContext
from ..ingest.runner import _band_for, _bucket_minutes, system_tz
from ..ingest.writer import Aggregator, IngestStats


class CarrierIngest:
    def __init__(self, store: Store, cfg: dict[str, Any], freq_hz: int, snr_db: float | None = None, source: str = "collector"):
        tz = system_tz()          # the clock DSD-FME prints on this machine, whatever config.timezone says
        self.store = store
        self.freq_hz = freq_hz
        now = int(time.time())
        self.carrier_id = store.upsert_carrier(freq_hz, now, _band_for(cfg, freq_hz))
        self.run_id = store.start_ingest_run(source, None, self.carrier_id, now, None)
        ctx = ParseContext(freq_hz=freq_hz, start_time=datetime.now(tz=tz), source=source, time_is_rf=True, tz=tz)
        self.stats = IngestStats(source=source, path=None, freq_hz=freq_hz, carrier_id=self.carrier_id)
        self.parser = LineParser(ctx)
        self.agg = Aggregator(store, self.carrier_id, source, _bucket_minutes(cfg), self.stats, snr_db=snr_db)
        self.raw = None
        if (cfg.get("decode", {}) or {}).get("keep_raw_logs"):
            from .rawlog import RawLog, raw_dir
            self.raw = RawLog(raw_dir(cfg), freq_hz, cfg)
        self.pos_syncs = 0
        self.neg_syncs = 0
        self.lines = 0
        self.finished = False

    def feed(self, line: str) -> None:
        self.lines += 1
        if self.raw is not None:
            self.raw.write(line if line.endswith("\n") else line + "\n")
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
        if self.raw is not None:
            self.raw.close()
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
            "csbk_opcodes": dict(st.csbk_opcodes.most_common(12)),
            "quality": {k: v for k, v in st.quality.items() if k.startswith("pdu_") or k in ("burst_err", "crc_err")},
        }
