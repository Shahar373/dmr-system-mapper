"""``mapper tui``: the operator's console.

Layout: a status bar (daemon, receiver, power, session progress), a column of actions, and tabs
for carriers, survey hits, systems, sessions and a log of commands. Everything is read from the
database every two seconds; actions insert rows into ``commands`` for the daemon.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static, TabbedContent, TabPane, Tree

from ..collector import state as st
from ..collector.survey import latest_hits
from ..db import Store

REFRESH_S = 2.0


def _ago(ts: int | None) -> str:
    if not ts:
        return "-"
    d = int(datetime.now(tz=timezone.utc).timestamp()) - int(ts)
    if d < 90:
        return f"{d}s ago"
    if d < 5400:
        return f"{d // 60}m ago"
    if d < 172800:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def _mhz(f: int | float | None) -> str:
    return f"{float(f) / 1e6:.4f}" if f else "-"


class StatusBar(Static):
    text: str = ""

    def update(self, content: Any = "") -> None:  # type: ignore[override]
        self.text = str(content)
        super().update(content)

    def update_status(self, s: st.DaemonStatus) -> None:
        if not s.alive:
            self.update(f"[b red]daemon offline[/]  {s.message or ''}   (start it with: mapper daemon)")
            return
        sdr = s.sdr or {}
        power = s.power or {}
        sdr_txt = {"present": "[green]ready[/]", "busy": "[yellow]busy[/]", "absent": "[red]not connected[/]"}.get(sdr.get("status"), f"[red]{sdr.get('status')}[/]")
        if sdr.get("holder"):
            sdr_txt += f" ({sdr['holder']})"
        p_txt = "mains" if power.get("supply_full") else ("[yellow]battery[/]" if power.get("supply_full") is False else "supply?")
        if power.get("volts"):
            p_txt += f" {power['volts']:.2f}V"
        if power.get("capped"):
            p_txt += " cpu capped"
        if power.get("undervolt_now"):
            p_txt += " [b red]UNDERVOLTAGE[/]"
        line1 = f"daemon [green]alive[/] pid {s.pid}   receiver {sdr_txt}   power {p_txt}   mode [b]{s.mode}[/]"
        pr = s.progress or {}
        line2 = s.message or ""
        if s.mode in ("survey", "decode") and pr:
            el, bu = pr.get("elapsed_s"), pr.get("budget_s")
            tail = f"  {el}/{bu} s" if el is not None and bu else ""
            if s.mode == "survey":
                line2 = f"survey #{s.session_id}: {pr.get('band', '')} step {pr.get('step', '?')}/{pr.get('steps', '?')} at {_mhz(pr.get('center_hz'))} MHz, {pr.get('hits', 0)} hits so far{tail}"
            else:
                cs = ", ".join(f"{_mhz(c['freq_hz'])} ({c.get('syncs', 0)} syncs)" for c in pr.get("carriers", []))
                line2 = f"decode #{s.session_id}: slice {pr.get('slice', '?')}/{pr.get('slices', '?')} ({pr.get('reason', '')}) {pr.get('slice_elapsed_s', 0)}/{pr.get('dwell_s', '?')} s: {cs}{tail}"
        self.update(line1 + "\n" + line2)


class MapperApp(App[None]):
    TITLE = "dmr-system-mapper"
    CSS = """
    #status { height: 3; padding: 0 1; background: $panel; }
    #actions { width: 26; padding: 1; }
    #actions Button { width: 100%; margin-bottom: 1; }
    #budget { margin-bottom: 1; }
    #detail { height: 8; padding: 0 1; background: $panel; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("s", "survey", "Survey"), Binding("d", "decode", "Decode"), Binding("x", "stop", "Stop"),
        Binding("c", "correlate", "Correlate"), Binding("e", "export", "Export"), Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, db_path: str | Path, cfg: dict[str, Any], source: str = "tui"):
        super().__init__()
        self.db_path = Path(db_path)
        self.cfg = cfg
        self.source = source
        self.store: Store | None = None
        self._seen_commands: set[int] = set()
        self._stale_s = int((cfg.get("daemon", {}) or {}).get("heartbeat_stale_s", 10))

    # -- layout -----------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar("starting…", id="status")
        with Horizontal():
            with Vertical(id="actions"):
                yield Label("session minutes")
                yield Input(placeholder="auto", id="budget", type="integer")
                yield Button("Survey (s)", id="survey", variant="primary")
                yield Button("Decode (d)", id="decode", variant="primary")
                yield Button("Stop (x)", id="stop", variant="error")
                yield Button("Correlate (c)", id="correlate")
                yield Button("Export map (e)", id="export")
            with TabbedContent(id="tabs"):
                with TabPane("Carriers", id="tab-carriers"):
                    yield DataTable(id="carriers", cursor_type="row")
                with TabPane("Survey", id="tab-survey"):
                    yield DataTable(id="hits", cursor_type="row")
                with TabPane("Systems", id="tab-systems"):
                    yield Tree("systems", id="systems")
                with TabPane("Sessions", id="tab-sessions"):
                    yield DataTable(id="sessions", cursor_type="row")
                with TabPane("Log", id="tab-log"):
                    yield RichLog(id="log", wrap=True, markup=True)
        yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.store = Store(self.db_path)
        t = self.query_one("#carriers", DataTable)
        t.add_columns("MHz", "variant", "conf", "CC", "site", "system", "last seen", "decoded s", "bursts")
        t = self.query_one("#hits", DataTable)
        t.add_columns("MHz", "band", "SNR dB", "p95", "occ %", "width", "class", "run")
        t = self.query_one("#sessions", DataTable)
        t.add_columns("#", "kind", "status", "by", "budget", "started", "elapsed", "summary")
        self.refresh_all()
        self.set_interval(REFRESH_S, self.refresh_all)
        # start on the table, not in the minutes box, so the single-key shortcuts work at once
        self.query_one("#carriers", DataTable).focus()

    def on_unmount(self) -> None:
        if self.store is not None:
            self.store.close()

    # -- refresh ----------------------------------------------------------------------------------
    def refresh_all(self) -> None:
        assert self.store is not None
        try:
            status = st.read_status(self.store, self._stale_s)
            self.query_one("#status", StatusBar).update_status(status)
            self._refresh_carriers()
            self._refresh_hits()
            self._refresh_systems()
            self._refresh_sessions()
            self._refresh_log()
        except Exception as exc:  # noqa: BLE001 - never let a refresh kill the console
            self.query_one("#log", RichLog).write(f"[red]refresh failed: {exc}[/]")

    def _refresh_carriers(self) -> None:
        assert self.store is not None
        t = self.query_one("#carriers", DataTable)
        rows = self.store.rows(
            "SELECT c.*, s.site_number, s.label AS site_label, sy.label AS system_label,"
            " (SELECT COALESCE(SUM(seconds_observed), 0) FROM observations o WHERE o.carrier_id = c.id) AS secs,"
            " (SELECT COALESCE(SUM(bursts), 0) FROM observations o WHERE o.carrier_id = c.id) AS bursts"
            " FROM carriers c LEFT JOIN carrier_sites cs ON cs.carrier_id = c.id LEFT JOIN sites s ON s.id = cs.site_id"
            " LEFT JOIN systems sy ON sy.id = s.system_id ORDER BY c.freq_hz")
        self._fill(t, [(
            _mhz(r["freq_hz"]), r["variant"], f"{r['variant_confidence']:.2f}", "-" if r["dominant_cc"] is None else str(r["dominant_cc"]),
            "-" if r["site_number"] is None else str(r["site_number"]), r["system_label"] or "-", _ago(r["last_seen"]),
            f"{float(r['secs'] or 0):.0f}", str(r["bursts"] or 0)) for r in rows], [str(r["freq_hz"]) for r in rows])

    def _refresh_hits(self) -> None:
        assert self.store is not None
        t = self.query_one("#hits", DataTable)
        hits = latest_hits(self.store)
        self._fill(t, [(
            _mhz(h["freq_hz"]), h["band"], f"{h['snr_db']:.1f}" if h["snr_db"] is not None else "-",
            f"{h['p95_snr_db']:.1f}" if h["p95_snr_db"] is not None else "-", f"{h['occupancy_pct']:.0f}" if h["occupancy_pct"] is not None else "-",
            f"{h['width_hz']:.0f}" if h["width_hz"] else "-", h["class"] or "-", _ago(h["run_started"])) for h in hits],
            [str(h["freq_hz"]) for h in hits])

    def _refresh_systems(self) -> None:
        assert self.store is not None
        tree = self.query_one("#systems", Tree)
        tree.clear()
        systems = self.store.rows("SELECT * FROM systems ORDER BY id")
        sites = self.store.rows("SELECT * FROM sites ORDER BY system_id, directly_observed DESC, id")
        members = self.store.rows(
            "SELECT cs.site_id, cs.lcn, c.freq_hz, c.variant, c.dominant_cc FROM carrier_sites cs JOIN carriers c ON c.id = cs.carrier_id ORDER BY c.freq_hz")
        tree.root.set_label(f"{len(systems)} systems")
        for sy in systems:
            n = tree.root.add(f"{sy['label']}  [{sy['variant']}]", expand=True)
            for si in [x for x in sites if x["system_id"] == sy["id"]]:
                label = si["label"] + ("" if si["directly_observed"] else "  (announced only)")
                sn = n.add(label, expand=True)
                for m in [x for x in members if x["site_id"] == si["id"]]:
                    lcn = f"  LCN {m['lcn']}" if m["lcn"] else ""
                    sn.add_leaf(f"{_mhz(m['freq_hz'])} MHz  {m['variant']}  cc={m['dominant_cc']}{lcn}")
        tree.root.expand()

    def _refresh_sessions(self) -> None:
        assert self.store is not None
        t = self.query_one("#sessions", DataTable)
        rows = st.sessions(self.store, 30)
        out = []
        for s in rows:
            sm = s["summary"]
            if s["kind"] == "survey":
                text = ", ".join(f"{b}: {v['hits']} hits" for b, v in (sm.get("bands") or {}).items())
            else:
                text = f"{len(sm.get('carriers') or {})} carriers, {len(sm.get('slices') or [])} slices"
            if sm.get("error"):
                text = f"ERROR {sm['error']}"
            elif sm.get("note"):
                text = sm["note"]
            elapsed = f"{sm.get('elapsed_s', 0):.0f}s" if s["finished"] else "running"
            out.append((str(s["id"]), s["kind"], s["status"], s["requested_by"], f"{s['budget_s'] // 60}m", _ago(s["started"]), elapsed, text))
        self._fill(t, out, [str(s["id"]) for s in rows])

    def _refresh_log(self) -> None:
        assert self.store is not None
        log = self.query_one("#log", RichLog)
        for c in reversed(st.recent_commands(self.store, 30)):
            if c["id"] in self._seen_commands or c["status"] in ("pending", "taken"):
                continue
            self._seen_commands.add(c["id"])
            colour = "green" if c["status"] == "done" else "red"
            when = datetime.fromtimestamp(c["done_at"] or c["created"], tz=timezone.utc).strftime("%H:%M:%S")
            log.write(f"{when} [{colour}]{c['kind']}[/] ({c['source']}): {c['result'] or ''}")

    @staticmethod
    def _fill(table: DataTable, rows: list[tuple], keys: list[str]) -> None:
        cursor = table.cursor_row
        table.clear()
        for key, row in zip(keys, rows):
            table.add_row(*row, key=key)
        if rows and cursor is not None:
            table.move_cursor(row=min(cursor, len(rows) - 1))

    # -- actions ----------------------------------------------------------------------------------
    def _budget_s(self) -> int | None:
        text = self.query_one("#budget", Input).value.strip()
        if not text:
            return None
        try:
            return max(1, int(float(text))) * 60
        except ValueError:
            return None

    def _post(self, kind: str, params: dict[str, Any] | None = None) -> None:
        assert self.store is not None
        cid = st.post_command(self.store, self.source, kind, params)
        self.query_one("#log", RichLog).write(f"[dim]→ {kind} (command #{cid})[/]")
        status = st.read_status(self.store, self._stale_s)
        if not status.alive:
            self.query_one("#log", RichLog).write("[yellow]the daemon is offline: the command waits until it starts[/]")

    def action_survey(self) -> None:
        params: dict[str, Any] = {}
        if (b := self._budget_s()) is not None:
            params["budget_s"] = b
        self._post("start_survey", params)

    def action_decode(self) -> None:
        params: dict[str, Any] = {}
        if (b := self._budget_s()) is not None:
            params["budget_s"] = b
        self._post("start_decode", params)

    def action_stop(self) -> None:
        self._post("stop")

    def action_correlate(self) -> None:
        self._post("correlate")

    def action_export(self) -> None:
        self._post("export")

    def action_refresh(self) -> None:
        self.refresh_all()

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        action = {"survey": self.action_survey, "decode": self.action_decode, "stop": self.action_stop,
                  "correlate": self.action_correlate, "export": self.action_export}.get(event.button.id or "")
        if action:
            action()

    @on(DataTable.RowHighlighted, "#carriers")
    def _carrier_detail(self, event: DataTable.RowHighlighted) -> None:
        assert self.store is not None
        if event.row_key is None or event.row_key.value is None:
            return
        freq = int(event.row_key.value)
        c = self.store.conn.execute("SELECT * FROM carriers WHERE freq_hz = ?", (freq,)).fetchone()
        if c is None:
            return
        kinds = self.store.rows("SELECT kind, SUM(count) AS n FROM csbk_events WHERE carrier_id = ? GROUP BY kind ORDER BY n DESC LIMIT 8", (c["id"],))
        idents = self.store.rows("SELECT i.kind, COUNT(DISTINCT i.id) AS n FROM identity_sightings s JOIN identities i ON i.id = s.identity_id WHERE s.carrier_id = ? GROUP BY i.kind", (c["id"],))
        links = self.store.rows(
            "SELECT l.kind, l.label, l.score, l.tier2_only, CASE WHEN l.a_carrier_id = ? THEN b.freq_hz ELSE a.freq_hz END AS other"
            " FROM links l JOIN carriers a ON a.id = l.a_carrier_id JOIN carriers b ON b.id = l.b_carrier_id"
            " WHERE (l.a_carrier_id = ? OR l.b_carrier_id = ?) AND l.label != 'weak' ORDER BY l.score DESC LIMIT 6", (c["id"], c["id"], c["id"]))
        lines = [f"[b]{_mhz(freq)} MHz[/]  {c['variant']} ({c['variant_confidence']:.2f})  cc={c['dominant_cc']}  "
                 f"first {_ago(c['first_seen'])}  last {_ago(c['last_seen'])}  band {c['band'] or '-'}"]
        if kinds:
            lines.append("events: " + ", ".join(f"{k['kind']}×{k['n']}" for k in kinds))
        if idents:
            lines.append("identities: " + ", ".join(f"{i['kind']}={i['n']}" for i in idents))
        if links:
            lines.append("links: " + "; ".join(f"{_mhz(l['other'])} {l['kind']} {l['label']} {l['score']:.2f}{' T2' if l['tier2_only'] else ''}" for l in links))
        self.query_one("#detail", Static).update("\n".join(lines))


def run_tui(db_path: str | Path, cfg: dict[str, Any]) -> None:
    MapperApp(db_path, cfg).run()
