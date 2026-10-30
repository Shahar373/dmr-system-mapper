"""Human-readable ingest report."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .writer import IngestStats


def _ts(v: int | None) -> str:
    if v is None:
        return "-"
    return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _mhz(freq: int | None) -> str:
    return f"{freq / 1e6:.4f} MHz" if freq else "?"


def format_report(results: Iterable[IngestStats], top_unrecognized: int = 15) -> str:
    out: list[str] = []
    results = list(results)
    for st in results:
        head = f"== {st.source}: {st.path or '<stream>'}"
        out.append(head)
        if st.skipped:
            out.append(f"   skipped: {st.note}")
            continue
        if st.note and st.freq_hz is None:
            out.append(f"   {st.note}")
            continue
        label = ""
        if st.meta.get("candidate_id"):
            label = f" [{st.meta['candidate_id']} {st.meta.get('recording_id', '')} {st.meta.get('decoder_status', '')}]"
        out.append(f"   carrier {_mhz(st.freq_hz)} (id {st.carrier_id}){label}  variant={st.variant} ({st.variant_confidence:.2f})")
        out.append(f"   lines {st.lines}  parsed {st.parsed}  unrecognized {st.unrecognized_total}  time {_ts(st.first_ts)} .. {_ts(st.last_ts)}")
        b = st.bursts
        out.append(
            f"   bursts {sum(v for k, v in b.items() if k != 'other_proto')}: csbk {b.get('csbk', 0)} voice {b.get('voice', 0)}"
            f" data {b.get('data', 0)} idle {b.get('idle', 0)} other {b.get('other', 0)}  no_sync {st.no_sync}"
            f"  errors {st.quality.get('burst_err', 0) + st.quality.get('crc_err', 0) + st.quality.get('slco_crc', 0)}"
        )
        if st.cc_histogram:
            cc = ", ".join(f"CC{k}:{v}" for k, v in st.cc_histogram.most_common(4))
            out.append(f"   colour codes: {cc}")
        if st.structural:
            parts = [f"{k}={v}" + (f"→{st.stored_events.get(k, 0)} rows" if st.stored_events.get(k, 0) != v else "")
                     for k, v in sorted(st.structural.items(), key=lambda kv: -kv[1])]
            out.append("   structural: " + ", ".join(parts))
        if st.csbk_opcodes:
            out.append("   raw CSBKs (-Z): " + ", ".join(f"{k}={v}" for k, v in st.csbk_opcodes.most_common(8)))
        if st.rest_transitions:
            out.append(f"   rest-channel transitions: {st.rest_transitions}")
        if st.identities:
            out.append("   identities: " + ", ".join(f"{k}={v}" for k, v in sorted(st.identities.items())))
        if st.untested_kinds:
            out.append("   NOTE untested_on_air parsers produced: " + ", ".join(st.untested_kinds))
        if st.unrecognized:
            out.append(f"   unrecognized patterns (top {top_unrecognized}):")
            for pat, (cnt, ex) in sorted(st.unrecognized.items(), key=lambda kv: -kv[1][0])[:top_unrecognized]:
                out.append(f"     {cnt:6d}  {ex[:90]}")
    if len(results) > 1:
        done = [r for r in results if not r.skipped and r.freq_hz]
        out.append(f"== total: {len(done)} ingested, {len(results) - len(done)} skipped, "
                   f"{sum(r.lines for r in done)} lines, {sum(r.unrecognized_total for r in done)} unrecognized")
    return "\n".join(out)
