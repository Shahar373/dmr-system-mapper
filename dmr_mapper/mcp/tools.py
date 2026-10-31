"""What the MCP tools do, without the MCP SDK: plain functions over the database and the daemon's
command queue that return JSON-ready dicts. ``server.py`` only binds them to the protocol, so all
of this is testable offline.

Rules (DESIGN.md §10):
- Reading never touches the receiver. The MCP server never opens it either: a scan is a command
  for the daemon, and only after a person approved it (``mcp.approval``: a dialog from this
  server, the client's own permission prompt, or the console on the Pi).
- Claude writes only proposals (``evidence.source = 'mcp'``, which scoring never lets confirm a
  link or lift the Tier II cap by itself) and correlator runs.
- Radio IDs are personal: unless ``mcp.show_radio_ids`` is true they are replaced by pseudonyms
  that stay the same while the server runs, so "the same radio on both carriers" still shows.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..collector import state as st
from ..config import band_for_range, resolve_path
from ..correlate import explain, find_link, propose, withdraw
from ..correlate.names import display
from ..db import Store

SESSION_KINDS = {"survey": "start_survey", "decode": "start_decode", "watch": "start_watch"}
FINISHED = ("done", "stopped", "failed")
# payload keys that hold a radio's own id (or address) rather than a talkgroup
RADIO_KEYS = {"src", "radio", "source_id", "ip"}
# event kinds whose target is a radio too (data to or from one radio, its position reports)
PRIVATE_KINDS = {"lrrp", "data_header", "data_response", "ip_map"}


def to_hz(value: float | int | str) -> int:
    """A frequency as Hz (469350000) or MHz (469.35): anything under 10 000 is taken as MHz."""
    v = float(value)
    return int(round(v * 1e6)) if v < 10_000 else int(round(v))


class MapperTools:
    def __init__(self, cfg: dict[str, Any], db_path: str | Path | None = None):
        self.cfg = cfg
        self.db_path = Path(db_path) if db_path else resolve_path(cfg, "db")
        m = cfg.get("mcp", {}) or {}
        self.approval = {"none": "client"}.get(str(m.get("approval", "ask")), str(m.get("approval", "ask")))
        if self.approval not in ("ask", "client", "console"):
            raise ValueError(f"mcp.approval must be ask, client or console, not {self.approval!r}")
        self.approval_timeout_s = int(float(m.get("approval_timeout_min", 10)) * 60)
        self.max_minutes = float(m.get("max_session_minutes", 30))
        self.default_minutes = float(m.get("default_session_minutes", 10))
        self.show_radio_ids = bool(m.get("show_radio_ids", False))
        self.max_proposal = float(m.get("max_proposal_confidence", 0.8))
        self.stale_s = int((cfg.get("daemon", {}) or {}).get("heartbeat_stale_s", 10))
        self.tolerance_hz = int((cfg.get("survey", {}) or {}).get("raster_tolerance_hz", 2500))
        self.tz = ZoneInfo(str(cfg.get("timezone") or "UTC"))
        self._salt = secrets.token_bytes(16)

    # -- helpers ----------------------------------------------------------------------------------
    def store(self) -> Store:
        return Store(self.db_path)

    def iso(self, ts: float | int | None) -> str | None:
        return datetime.fromtimestamp(float(ts), tz=self.tz).isoformat(timespec="seconds") if ts else None

    def radio(self, value: Any) -> Any:
        if self.show_radio_ids or value in (None, "", 0):
            return value
        return "radio-" + hashlib.sha256(self._salt + str(value).encode()).hexdigest()[:6]

    def redact(self, obj: Any, radio_list: bool = False, private: bool = False) -> Any:
        """Radio ids in event payloads and evidence details become pseudonyms."""
        if isinstance(obj, dict):
            private = private or obj.get("group") is False or obj.get("kind") == "private" or str(obj.get("addressing", "")).lower().startswith("ind")
            out = {}
            for k, v in obj.items():
                if k in RADIO_KEYS or (private and k in ("tgt", "dst")):
                    out[k] = self.radio(v)
                elif k == "ids" and radio_list:
                    out[k] = [self.radio(x) for x in v]
                else:
                    out[k] = self.redact(v, radio_list)
            return out
        if isinstance(obj, list):
            return [self.redact(x, radio_list) for x in obj]
        return obj

    def carrier(self, s: Store, freq: float | int | str) -> dict[str, Any]:
        """The known carrier nearest to ``freq`` within the raster tolerance."""
        hz = to_hz(freq)
        row = s.conn.execute("SELECT * FROM carriers WHERE ABS(freq_hz - ?) <= ? ORDER BY ABS(freq_hz - ?) LIMIT 1",
                             (hz, self.tolerance_hz, hz)).fetchone()
        if row is None:
            raise KeyError(f"no known carrier at {hz / 1e6:.4f} MHz (list_carriers shows the known ones)")
        return dict(row)

    def _membership(self, s: Store) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for r in s.rows("SELECT cs.carrier_id, cs.lcn, cs.confidence, si.id AS site_id, si.site_number, si.label AS site_label, si.name AS site_name,"
                        " sy.id AS system_id, sy.label AS system_label, sy.name AS system_name, sy.variant AS system_variant"
                        " FROM carrier_sites cs JOIN sites si ON si.id = cs.site_id JOIN systems sy ON sy.id = si.system_id"):
            out[int(r["carrier_id"])] = {
                "site": {"id": r["site_id"], "name": display(r["site_name"], r["site_label"]), "number": r["site_number"]},
                "system": {"id": r["system_id"], "name": display(r["system_name"], r["system_label"]), "variant": r["system_variant"]},
                "lcn": r["lcn"], "membership_confidence": round(float(r["confidence"]), 3)}
        return out

    def _latest_snr(self, s: Store) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for r in s.rows("SELECT h.freq_hz, h.snr_db, h.p95_snr_db, h.occupancy_pct, r.started FROM survey_hits h JOIN survey_runs r ON r.id = h.run_id"
                        " ORDER BY r.started"):
            out[int(r["freq_hz"])] = {"snr_db": r["snr_db"], "p95_snr_db": r["p95_snr_db"], "occupancy_pct": r["occupancy_pct"], "at": r["started"]}
        return out

    def _snr_near(self, snr: dict[int, dict[str, Any]], freq: int) -> dict[str, Any] | None:
        best = [(abs(f - freq), v) for f, v in snr.items() if abs(f - freq) <= self.tolerance_hz]
        return min(best, key=lambda x: x[0])[1] if best else None

    # -- status -----------------------------------------------------------------------------------
    def get_status(self) -> dict[str, Any]:
        with self.store() as s:
            d = st.read_status(s, self.stale_s)
            sessions = [self._session_brief(x) for x in st.sessions(s, 5)]
            waiting = [{"request_id": c["id"], "what": st.describe_request(c["kind"], c["params"]), "asked": self.iso(c["created"]),
                        "by": c["source"]} for c in st.awaiting_approval(s, self.approval_timeout_s)]
            counts = {t: s.scalar(f"SELECT COUNT(*) FROM {t}") for t in ("carriers", "links", "sites", "systems", "keyups", "survey_runs")}
            labels = {r["label"]: r["n"] for r in s.rows("SELECT label, COUNT(*) AS n FROM links GROUP BY label")}
            surveys = {r["band"]: {"run_id": r["id"], "at": self.iso(r["started"]), "hits": r["hits"]} for r in s.rows(
                "SELECT r.band, r.id, r.started, (SELECT COUNT(*) FROM survey_hits h WHERE h.run_id = r.id) AS hits FROM survey_runs r"
                " WHERE r.id IN (SELECT MAX(id) FROM survey_runs GROUP BY band) ORDER BY r.band")}
            schema = s.schema_version()
        snaps = sorted(resolve_path(self.cfg, "snapshots").glob("mapper-*.db"))
        now = time.time()
        daemon = d.to_dict()
        daemon["heartbeat_age_s"] = int(now - d.heartbeat) if d.heartbeat else None
        for k in ("started", "heartbeat"):
            daemon[k] = self.iso(daemon.get(k))
        return {
            "now": self.iso(now),
            "daemon": daemon,
            "sessions": sessions,
            "awaiting_approval": waiting,
            "approval": {"mode": self.approval, "timeout_min": self.approval_timeout_s // 60, "max_session_minutes": self.max_minutes},
            "database": {"schema": schema, "size_mb": round(self.db_path.stat().st_size / 1e6, 2) if self.db_path.exists() else 0,
                         "counts": counts, "links_by_label": labels},
            "last_survey": surveys,
            "snapshots": {"count": len(snaps), "newest": snaps[-1].name if snaps else None,
                          "newest_age_h": round((now - snaps[-1].stat().st_mtime) / 3600, 1) if snaps else None},
            "bands": [{"name": b.get("name"), "start_hz": b.get("start_hz"), "stop_hz": b.get("stop_hz")}
                      for b in (self.cfg.get("survey", {}) or {}).get("bands", []) or []],
        }

    def _session_brief(self, x: dict[str, Any]) -> dict[str, Any]:
        sm = x["summary"] or {}
        out = {"session_id": x["id"], "kind": x["kind"], "status": x["status"], "requested_by": x["requested_by"],
               "budget_s": x["budget_s"], "started": self.iso(x["started"]), "finished": self.iso(x["finished"]),
               "elapsed_s": sm.get("elapsed_s")}
        if x["kind"] == "survey":
            out["hits"] = {b: v.get("hits") for b, v in (sm.get("bands") or {}).items()}
        elif x["kind"] == "watch":
            out["watch"] = {k: v for k, v in (sm.get("watch") or {}).items() if k in ("keyups", "decoded", "missed")}
        else:
            out["carriers"] = len(sm.get("carriers") or {})
        for k in ("error", "note", "stop_reason"):
            if sm.get(k):
                out[k] = sm[k]
        return out

    # -- carriers ---------------------------------------------------------------------------------
    def list_carriers(self, band: str | None = None, since_hours: float = 168, min_snr_db: float | None = None,
                      variant: str | None = None, limit: int = 200) -> dict[str, Any]:
        since = int(time.time() - float(since_hours) * 3600)
        with self.store() as s:
            member = self._membership(s)
            snr = self._latest_snr(s)
            rows = s.rows(
                "SELECT c.*, (SELECT COALESCE(SUM(seconds_observed), 0) FROM observations o WHERE o.carrier_id = c.id) AS secs,"
                " (SELECT COALESCE(SUM(bursts), 0) FROM observations o WHERE o.carrier_id = c.id) AS bursts,"
                " (SELECT MAX(snr_db_max) FROM observations o WHERE o.carrier_id = c.id) AS obs_snr,"
                " (SELECT COUNT(*) FROM keyups k WHERE k.carrier_id = c.id) AS keyups"
                " FROM carriers c WHERE c.last_seen >= ? ORDER BY c.freq_hz", (since,))
        out = []
        for r in rows:
            if band and r["band"] != band:
                continue
            if variant and r["variant"] != variant:
                continue
            hit = self._snr_near(snr, int(r["freq_hz"]))
            best_snr = max([v for v in ((hit or {}).get("snr_db"), r["obs_snr"]) if v is not None], default=None)
            if min_snr_db is not None and (best_snr is None or best_snr < float(min_snr_db)):
                continue
            m = member.get(int(r["id"]), {})
            out.append({
                "freq_hz": r["freq_hz"], "mhz": round(r["freq_hz"] / 1e6, 5), "band": r["band"], "variant": r["variant"],
                "variant_confidence": round(float(r["variant_confidence"]), 2), "cc": r["dominant_cc"],
                "first_seen": self.iso(r["first_seen"]), "last_seen": self.iso(r["last_seen"]),
                "decoded_s": round(float(r["secs"] or 0)), "bursts": int(r["bursts"] or 0), "keyups": int(r["keyups"] or 0),
                "snr_db": None if best_snr is None else round(float(best_snr), 1),
                "site": m.get("site"), "system": m.get("system"), "lcn": m.get("lcn")})
        return {"count": len(out), "carriers": out[:max(1, int(limit))], "truncated": len(out) > int(limit)}

    def get_fingerprint(self, freq: float | int, days: float = 7) -> dict[str, Any]:
        since = int(time.time() - float(days) * 86400)
        with self.store() as s:
            c = self.carrier(s, freq)
            cid = int(c["id"])
            obs = s.conn.execute(
                "SELECT COUNT(*) AS buckets, SUM(seconds_observed) AS secs, SUM(bursts) AS bursts, SUM(voice) AS voice, SUM(data) AS data,"
                " SUM(csbk) AS csbk, SUM(idle) AS idle, SUM(crc_err) AS crc_err, SUM(slot1_syncs) AS slot1, SUM(slot2_syncs) AS slot2,"
                " MAX(snr_db_max) AS snr_max FROM observations WHERE carrier_id = ? AND bucket_start >= ?", (cid, since)).fetchone()
            cc_hist: dict[str, int] = {}
            for r in s.rows("SELECT cc_histogram FROM observations WHERE carrier_id = ? AND bucket_start >= ?", (cid, since)):
                for k, v in json.loads(r["cc_histogram"] or "{}").items():
                    cc_hist[k] = cc_hist.get(k, 0) + int(v)
            events = []
            for k in s.rows("SELECT kind, SUM(count) AS n, MIN(ts) AS first, MAX(last_ts) AS last FROM csbk_events"
                            " WHERE carrier_id = ? AND last_ts >= ? GROUP BY kind ORDER BY n DESC", (cid, since)):
                examples = [self.redact(json.loads(p["payload"]), private=k["kind"] in PRIVATE_KINDS) for p in s.rows(
                    "SELECT payload FROM csbk_events WHERE carrier_id = ? AND kind = ? AND last_ts >= ? ORDER BY count DESC LIMIT 3",
                    (cid, k["kind"], since))]
                events.append({"kind": k["kind"], "count": int(k["n"]), "first": self.iso(k["first"]), "last": self.iso(k["last"]),
                               "top_payloads": examples})
            idents: dict[str, Any] = {}
            for kind in ("tg", "radio"):
                rows = s.rows("SELECT i.value, SUM(x.count) AS n, COUNT(DISTINCT x.day) AS days FROM identity_sightings x"
                              " JOIN identities i ON i.id = x.identity_id WHERE x.carrier_id = ? AND i.kind = ? AND x.day >= ?"
                              " GROUP BY i.value ORDER BY n DESC", (cid, kind, since // 86400))
                top = [{"value": r["value"] if kind == "tg" else self.radio(r["value"]), "count": int(r["n"]), "days": int(r["days"])} for r in rows[:15]]
                idents["talkgroups" if kind == "tg" else "radios"] = {"distinct": len(rows), "top": top}
            ku = s.conn.execute("SELECT COUNT(*) AS n, SUM(decoded) AS decoded, AVG(end - start) AS avg_s, MAX(start) AS last, MAX(peak_snr_db) AS peak"
                                " FROM keyups WHERE carrier_id = ? AND start >= ?", (cid, since)).fetchone()
            runs = s.conn.execute("SELECT COUNT(*) AS n, MAX(finished) AS last, SUM(finished - started) AS secs FROM ingest_runs WHERE carrier_id = ?",
                                  (cid,)).fetchone()
            unrec = [{"pattern": r["pattern"], "count": int(r["n"])} for r in s.rows(
                "SELECT u.pattern, SUM(u.count) AS n FROM unrecognized_lines u JOIN ingest_runs r ON r.id = u.run_id WHERE r.carrier_id = ?"
                " GROUP BY u.pattern ORDER BY n DESC LIMIT 5", (cid,))]
            links = self._links_of(s, cid)
            m = self._membership(s).get(cid, {})
            hit = self._snr_near(self._latest_snr(s), int(c["freq_hz"]))
        return {
            "freq_hz": c["freq_hz"], "mhz": round(c["freq_hz"] / 1e6, 5), "band": c["band"], "variant": c["variant"],
            "variant_confidence": round(float(c["variant_confidence"]), 2), "cc": c["dominant_cc"],
            "first_seen": self.iso(c["first_seen"]), "last_seen": self.iso(c["last_seen"]), "window_days": days,
            "site": m.get("site"), "system": m.get("system"), "lcn": m.get("lcn"),
            "activity": {"buckets": obs["buckets"], "seconds_observed": round(float(obs["secs"] or 0)), "bursts": obs["bursts"] or 0,
                         "voice": obs["voice"] or 0, "data": obs["data"] or 0, "csbk": obs["csbk"] or 0, "idle": obs["idle"] or 0,
                         "crc_err": obs["crc_err"] or 0, "slot1_syncs": obs["slot1"] or 0, "slot2_syncs": obs["slot2"] or 0,
                         "snr_db_max": obs["snr_max"], "cc_histogram": cc_hist},
            "events": events,
            "identities": idents,
            "keyups": {"count": ku["n"], "decoded": int(ku["decoded"] or 0), "avg_s": round(float(ku["avg_s"]), 1) if ku["avg_s"] else None,
                       "last": self.iso(ku["last"]), "peak_snr_db": ku["peak"]},
            "decoding": {"runs": runs["n"], "last": self.iso(runs["last"]), "seconds": round(float(runs["secs"] or 0))},
            "latest_survey": {**hit, "at": self.iso(hit["at"])} if hit else None,
            "unrecognized_patterns": unrec,
            "links": links,
        }

    def _links_of(self, s: Store, cid: int) -> list[dict[str, Any]]:
        out = []
        for l in s.rows(
                "SELECT l.*, CASE WHEN l.a_carrier_id = ? THEN b.freq_hz ELSE a.freq_hz END AS other,"
                " (SELECT GROUP_CONCAT(e.type || ':' || e.source) FROM evidence e WHERE e.link_id = l.id) AS types"
                " FROM links l JOIN carriers a ON a.id = l.a_carrier_id JOIN carriers b ON b.id = l.b_carrier_id"
                " WHERE l.a_carrier_id = ? OR l.b_carrier_id = ? ORDER BY l.score DESC LIMIT 40", (cid, cid, cid)):
            out.append({"link_id": l["id"], "other_freq_hz": l["other"], "kind": l["kind"], "score": round(float(l["score"]), 3),
                        "label": l["label"], "tier2_only": bool(l["tier2_only"]), "evidence": sorted(set((l["types"] or "").split(",")) - {""})})
        return out

    def list_survey_hits(self, band: str | None = None, run_id: int | None = None, min_snr_db: float | None = None,
                         limit: int = 200) -> dict[str, Any]:
        from ..collector.survey import latest_hits

        with self.store() as s:
            if run_id is not None:
                run = s.conn.execute("SELECT * FROM survey_runs WHERE id = ?", (int(run_id),)).fetchone()
                if run is None:
                    raise KeyError(f"no survey run {run_id}")
                hits = [{**dict(h), "band": run["band"], "run_started": run["started"]} for h in
                        s.rows("SELECT * FROM survey_hits WHERE run_id = ? ORDER BY freq_hz", (int(run_id),))]
            else:
                hits = latest_hits(s)
            known = {int(r["freq_hz"]): r["variant"] for r in s.carriers()}
        out = []
        for h in hits:
            if band and not str(h["band"]).startswith(band):
                continue
            if min_snr_db is not None and (h["snr_db"] is None or h["snr_db"] < float(min_snr_db)):
                continue
            f = int(h["freq_hz"])
            variant = next((v for k, v in known.items() if abs(k - f) <= self.tolerance_hz), None)
            out.append({"freq_hz": f, "mhz": round(f / 1e6, 5), "band": h["band"], "run_id": h["run_id"], "at": self.iso(h["run_started"]),
                        "snr_db": h["snr_db"], "p95_snr_db": h["p95_snr_db"], "occupancy_pct": h["occupancy_pct"], "width_hz": h["width_hz"],
                        "class": h["class"], "known_as": variant})
        return {"count": len(out), "hits": out[:max(1, int(limit))], "truncated": len(out) > int(limit)}

    # -- systems and links ------------------------------------------------------------------------
    def query_systems(self, system_id: int | None = None, freq: float | int | None = None, min_confidence: float = 0.3) -> dict[str, Any]:
        with self.store() as s:
            if freq is not None:
                cid = int(self.carrier(s, freq)["id"])
                row = s.conn.execute("SELECT si.system_id FROM carrier_sites cs JOIN sites si ON si.id = cs.site_id WHERE cs.carrier_id = ?",
                                     (cid,)).fetchone()
                if row is None:
                    return {"systems": [], "note": "that carrier belongs to no site or system yet (see get_fingerprint for its links)"}
                system_id = int(row["system_id"])
            systems = [dict(r) for r in s.rows("SELECT * FROM systems" + (" WHERE id = ?" if system_id is not None else "") + " ORDER BY id",
                                                (int(system_id),) if system_id is not None else ())]
            sites = [dict(r) for r in s.rows("SELECT * FROM sites ORDER BY system_id, directly_observed DESC, id")]
            members = [dict(r) for r in s.rows(
                "SELECT cs.site_id, cs.lcn, cs.confidence, c.id AS carrier_id, c.freq_hz, c.variant, c.dominant_cc, c.last_seen"
                " FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id ORDER BY c.freq_hz")]
            links = [dict(r) for r in s.rows(
                "SELECT l.*, a.freq_hz AS a_freq, b.freq_hz AS b_freq FROM links l JOIN carriers a ON a.id = l.a_carrier_id"
                " JOIN carriers b ON b.id = l.b_carrier_id WHERE l.score >= ? ORDER BY l.score DESC", (float(min_confidence),))]
        out = []
        for sy in systems:
            sy_sites = []
            cids: set[int] = set()
            for si in [x for x in sites if x["system_id"] == sy["id"]]:
                carriers = [{"freq_hz": m["freq_hz"], "mhz": round(m["freq_hz"] / 1e6, 5), "variant": m["variant"], "cc": m["dominant_cc"],
                             "lcn": m["lcn"], "confidence": round(float(m["confidence"]), 3), "last_seen": self.iso(m["last_seen"])}
                            for m in members if m["site_id"] == si["id"]]
                cids.update(m["carrier_id"] for m in members if m["site_id"] == si["id"])
                sy_sites.append({"site_id": si["id"], "name": display(si["name"], si["label"]), "number": si["site_number"], "cc": si["cc"],
                                 "directly_observed": bool(si["directly_observed"]), "carriers": carriers})
            sy_links = [{"link_id": l["id"], "a_freq_hz": l["a_freq"], "b_freq_hz": l["b_freq"], "kind": l["kind"], "score": round(float(l["score"]), 3),
                         "label": l["label"], "tier2_only": bool(l["tier2_only"])}
                        for l in links if l["a_carrier_id"] in cids or l["b_carrier_id"] in cids]
            out.append({"system_id": sy["id"], "name": display(sy["name"], sy["label"]), "variant": sy["variant"], "sys_code": sy["sys_code"],
                        "net_id": sy["net_id"], "confidence": round(float(sy["confidence"]), 3), "sites": sy_sites, "links": sy_links})
        return {"systems": out, "min_confidence": min_confidence,
                "note": "Tier II-only links are capped and never certain; a proposal (source mcp) never confirms a link by itself"}

    def explain_link(self, link_id: int | None = None, a_freq: float | int | None = None, b_freq: float | int | None = None,
                     kind: str | None = None) -> dict[str, Any]:
        with self.store() as s:
            if link_id is not None:
                ids = [int(link_id)]
            elif a_freq is not None and b_freq is not None:
                a, b = self.carrier(s, a_freq), self.carrier(s, b_freq)
                ids = find_link(s, int(a["freq_hz"]), int(b["freq_hz"]), kind)
                if not ids:
                    return {"links": [], "note": f"no link between {a['freq_hz'] / 1e6:.4f} and {b['freq_hz'] / 1e6:.4f} MHz"}
            else:
                raise ValueError("give link_id, or a_freq and b_freq")
            out = []
            for i in ids:
                x = explain(s, i, self.cfg)
                for e in x["evidence"]:
                    e["details"] = self.redact(e["details"], radio_list=e["type"] == "identity_radio")
                    e["observed_at"], e["first_observed_at"] = self.iso(e["observed_at"]), self.iso(e["first_observed_at"])
                out.append(x)
        return {"links": out}

    # -- writers ----------------------------------------------------------------------------------
    def propose_link(self, a_freq: float | int, b_freq: float | int, kind: str, confidence: float, note: str) -> dict[str, Any]:
        if not str(note or "").strip():
            raise ValueError("a proposal needs a note: the reasoning behind it, for the operator to check")
        if not 0.0 < float(confidence) <= 1.0:
            raise ValueError("confidence must be in (0, 1]; use withdraw_proposal to take one back")
        used = min(float(confidence), self.max_proposal)
        with self.store() as s:
            a, b = self.carrier(s, a_freq), self.carrier(s, b_freq)
            res = propose(s, int(a["freq_hz"]), int(b["freq_hz"]), kind, used, str(note).strip(), self.cfg, source="mcp")
        return {**res, "a_freq_hz": a["freq_hz"], "b_freq_hz": b["freq_hz"], "kind": kind, "confidence_recorded": used,
                "note": "recorded as a proposal" + (f" (confidence capped at {self.max_proposal:g}, mcp.max_proposal_confidence)" if used < float(confidence) else "")
                        + ": it counts as evidence, but alone it never makes a link confirmed nor lifts the Tier II cap."
                        " Sites and systems follow at the next correlator run (correlate_now)."}

    def withdraw_proposal(self, a_freq: float | int, b_freq: float | int, kind: str | None = None) -> dict[str, Any]:
        with self.store() as s:
            a, b = self.carrier(s, a_freq), self.carrier(s, b_freq)
            res = withdraw(s, int(a["freq_hz"]), int(b["freq_hz"]), kind, self.cfg, source="mcp")
        return {"withdrawn": res} if res else {"withdrawn": [], "note": "no proposal of yours on those carriers"}

    def correlate_now(self, wait_s: float = 300) -> dict[str, Any]:
        """Through the daemon when it runs (it may be correlating after a session), else here."""
        from ..correlate import run

        with self.store() as s:
            if st.read_status(s, self.stale_s).alive:
                cid = st.post_command(s, "mcp", "correlate")
                c = st.wait_for_command(s, cid, timeout_s=wait_s)
                if c and c["status"] in ("done", "failed"):
                    return {"by": "daemon", "status": c["status"], "result": c["result"]}
                return {"by": "daemon", "status": "running", "request_id": cid, "note": "still running; get_status shows the links when it is done"}
            rep = run(s, self.cfg)
        return {"by": "mcp server (daemon not running)", "status": "done", **rep.to_dict()}

    # -- sessions ---------------------------------------------------------------------------------
    def prepare_session(self, kind: str, minutes: float | None = None, bands: list[str] | None = None,
                        start: float | int | None = None, stop: float | int | None = None, freqs: list[float | int] | None = None,
                        center: float | int | None = None, then_decode: bool = False) -> tuple[str, dict[str, Any], str]:
        """Check that a session could start now and build its command; raises ValueError if not."""
        if kind not in SESSION_KINDS:
            raise ValueError(f"kind must be one of {sorted(SESSION_KINDS)}")
        minutes = float(minutes) if minutes else self.default_minutes
        if not 1 <= minutes <= self.max_minutes:
            raise ValueError(f"minutes must be between 1 and {self.max_minutes:g} (mcp.max_session_minutes)")
        params: dict[str, Any] = {"budget_s": int(minutes * 60)}
        cfg_bands = (self.cfg.get("survey", {}) or {}).get("bands", []) or []
        if kind == "survey":
            if bands:
                unknown = set(bands) - {str(b.get("name")) for b in cfg_bands}
                if unknown:
                    raise ValueError(f"unknown band(s) {sorted(unknown)}; configured: {[b.get('name') for b in cfg_bands]}")
                params["bands"] = list(bands)
            if start is not None or stop is not None:
                if start is None or stop is None:
                    raise ValueError("give both start and stop, or neither")
                lo, hi = to_hz(start), to_hz(stop)
                band_for_range(cfg_bands, lo, hi)          # raises when outside the band plan
                params["range"] = {"start_hz": lo, "stop_hz": hi}
            if then_decode:
                params["then_decode"] = True
                params["total_budget_s"] = params["budget_s"]
        else:
            if freqs:
                params["freqs"] = sorted({to_hz(f) for f in freqs})
            if kind == "watch" and center is not None:
                params["center_hz"] = to_hz(center)
        with self.store() as s:
            d = st.read_status(s, self.stale_s)
            if not d.alive:
                raise ValueError("the daemon is not running on the Pi (systemctl status dmr-mapper); nothing can be scanned")
            if d.mode in ("survey", "decode", "watch", "stopping"):
                raise ValueError(f"a {d.mode} session is running (#{d.session_id}); wait for it or stop_session first")
            sdr = (d.sdr or {}).get("status")
            if sdr in ("absent", "busy"):
                raise ValueError(f"the receiver is {sdr}" + (f" (held by {d.sdr.get('holder')})" if d.sdr.get("holder") else ""))
            if st.awaiting_approval(s, self.approval_timeout_s):
                raise ValueError("another request is already waiting for the operator's approval (get_status)")
        command = SESSION_KINDS[kind]
        return command, params, st.describe_request(command, params)

    def submit_session(self, command: str, params: dict[str, Any], approved_by: str | None) -> dict[str, Any]:
        """Post the command: at once when approved (in a dialog, or by the client's permission prompt
        with ``mcp.approval: client``), else waiting for the console."""
        with self.store() as s:
            if approved_by or self.approval == "client":
                params = {**params, "approved_by": approved_by or "operator, in the client's permission prompt (mcp.approval: client)"}
                cid = st.post_command(s, "mcp", command, params)
                c = st.wait_for_command(s, cid, timeout_s=30)
                result = self._request(s, cid, c)
            else:
                cid = st.post_command(s, "mcp", command, params, awaiting_approval=True)
                result = self._request(s, cid, st.command(s, cid))
                result["note"] = (f"waiting for the operator: `y` in `mapper tui` or `mapper approve {cid}` on the Pi, within "
                                  f"{self.approval_timeout_s // 60} min. Follow it with get_session(request_id={cid}).")
        return result

    def refuse_note(self, what: str) -> dict[str, Any]:
        return {"status": "declined", "what": what,
                "note": "the operator declined; nothing was started. Ask the person before trying again. If they saw no dialog, their"
                        " client refuses dialogs by itself: mcp.approval on the Pi should then be client (Claude Desktop) or console."}

    def _request(self, s: Store, cid: int, c: dict[str, Any] | None) -> dict[str, Any]:
        c = c or st.command(s, cid) or {}
        params = json.loads(c["params"]) if isinstance(c.get("params"), str) else (c.get("params") or {})
        out: dict[str, Any] = {"request_id": cid, "status": c.get("status"), "what": st.describe_request(str(c.get("kind")), params),
                               "result": c.get("result")}
        res = str(c.get("result") or "")
        if c.get("status") == "done" and res.startswith("session "):
            out["session_id"] = int(res.split()[1])
        return out

    def stop_session(self) -> dict[str, Any]:
        with self.store() as s:
            if not st.read_status(s, self.stale_s).alive:
                raise ValueError("the daemon is not running: nothing to stop")
            cid = st.post_command(s, "mcp", "stop")
            c = st.wait_for_command(s, cid, timeout_s=15)
        return {"request_id": cid, "status": c["status"] if c else "pending", "result": c["result"] if c else None}

    def get_session(self, session_id: int | None = None, request_id: int | None = None, wait_s: float = 0) -> dict[str, Any]:
        """A session (the latest when no id is given), or what became of a request. ``wait_s`` (at
        most 55) waits for a running session to finish or a request to be decided."""
        deadline = time.monotonic() + max(0.0, min(float(wait_s or 0), 55.0))
        with self.store() as s:
            while True:
                req = None
                if request_id is not None:
                    st.expire_requests(s, self.approval_timeout_s)
                    c = st.command(s, int(request_id))
                    if c is None:
                        raise KeyError(f"no request #{request_id}")
                    req = self._request(s, int(request_id), c)
                    session_id = req.get("session_id")
                    if session_id is None:
                        if req["status"] in ("awaiting_approval", "pending", "taken") and time.monotonic() < deadline:
                            time.sleep(1)
                            continue
                        return {"request": req}
                if session_id is None:
                    row = s.conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
                    if row is None:
                        return {"session": None, "note": "no session has run yet"}
                    session_id = int(row["id"])
                x = next((x for x in st.sessions(s, 50) if x["id"] == int(session_id)), None)
                if x is None:
                    raise KeyError(f"no session #{session_id} among the last 50")
                if x["status"] not in FINISHED and time.monotonic() < deadline:
                    time.sleep(1)
                    continue
                d = st.read_status(s, self.stale_s)
                out: dict[str, Any] = {"session": {**self._session_brief(x), "params": x["params"],
                                                   "summary": self._trim(self.redact({k: v for k, v in x["summary"].items() if k != "traceback"}))}}
                if d.alive and d.session_id == x["id"]:
                    out["progress"] = d.progress
                if req:
                    out["request"] = req
                return out

    @staticmethod
    def _trim(summary: dict[str, Any], limit: int = 24_000) -> dict[str, Any]:
        """Decode summaries list every slice; keep the answer within a sensible size."""
        if len(json.dumps(summary, default=str)) <= limit:
            return summary
        out = dict(summary)
        if isinstance(out.get("slices"), list):
            out["slices"] = [{k: v for k, v in sl.items() if k != "result"} for sl in out["slices"]]
        if len(json.dumps(out, default=str)) > limit:
            out = {k: (v if len(json.dumps(v, default=str)) < 2000 else f"<{len(json.dumps(v, default=str))} bytes left out>") for k, v in out.items()}
        out["trimmed"] = True
        return out
