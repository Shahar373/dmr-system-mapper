"""``mapper serve-mcp``: the MCP server Claude Code on the laptop talks to (DESIGN.md §10).

Streamable HTTP, bound to the Pi's Tailscale address (or loopback) only, no token: the tailnet is
the trust boundary. The server is its own process beside the daemon; it reads the database and
posts commands, and never opens the receiver.

A scan (survey, decode, watch) needs the operator's approval, asked in one of two ways
(``mcp.approval``): ``ask`` shows a yes/no dialog in Claude Code itself (MCP elicitation: the person
answers, not the model) and falls back to the console when the client cannot show one;
``console`` always waits for ``y`` in ``mapper tui`` or ``mapper approve``. ``none`` leaves the gate
to Claude Code's own per-tool permission prompt.

The dialog is a resolver (``Resolve``/``Elicit``), so the SDK sends it the way the negotiated
protocol wants (a request in the middle of the call up to 2025-11-25, an ``InputRequiredResult``
round from 2026-07-28). A resolver that asks nothing still reaches the tool as an "accept" without
data, so only an answer that carries ``approve: true`` counts as approval.
"""

# No ``from __future__ import annotations`` here: the SDK reads the tool signatures, and the
# resolvers named in them are local to build_server.
import functools
import ipaddress
import logging
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

import anyio
from pydantic import BaseModel, Field

from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .. import __version__
from .tools import MapperTools

log = logging.getLogger("dmr_mapper.mcp")

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

INSTRUCTIONS = """\
dmr-system-mapper on a Raspberry Pi: a passive DMR network mapper. DSD-FME decodes; a deterministic
correlator links carriers into sites and systems, every link with a confidence score and the evidence
behind it. Your role is orchestration and judgement over that evidence, never decoding.

- Start with get_status, then list_carriers / query_systems / get_fingerprint / explain_link.
- Frequencies may be given in Hz (469350000) or MHz (469.35).
- Labels: confirmed needs explicit (on-air) or operator evidence; links supported only by Tier II
  evidence (shared radios, talkgroups, short-LC content, RF profile) are capped and never certain.
  Say so when you report them.
- propose_link records your judgement as evidence (source "mcp") with a note giving the reasoning.
  Alone it never confirms a link. Propose only what the evidence supports and say what would confirm it.
- scan_band, decode_carriers and watch_slice use the shared receiver. Each asks the operator first;
  ask for the shortest session that answers the question. A declined request is final: do not
  retry it without asking the person. Follow a started session with get_session(wait_s=...).
- Radio IDs appear as pseudonyms (radio-xxxxxx) unless the operator enabled them.
"""


Freq = Annotated[float, Field(description="Hz (469350000) or MHz (469.35)")]


class Approval(BaseModel):
    approve: bool = Field(default=False, description="Start this session on the Pi's receiver now")


def check_bind(host: str, allow_any: bool = False) -> None:
    """Loopback or a Tailscale address only, unless ``mcp.allow_any_bind`` says otherwise."""
    if host == "localhost":
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"mcp.bind must be an IP address (the Pi's Tailscale address), not {host!r}") from exc
    if ip.is_loopback or ip in TAILNET_V4 or ip in TAILNET_V6 or allow_any:
        return
    raise ValueError(f"refusing to listen on {host}: not a Tailscale (100.64.0.0/10) or loopback address. The server has no"
                     " token; set mcp.bind to the Pi's Tailscale address (tailscale ip -4), or mcp.allow_any_bind: true if you mean it")


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Database work in a thread (the event loop stays free); expected failures become tool errors
    with their own words."""
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    except (KeyError, ValueError) as exc:
        raise ToolError(str(exc.args[0]) if exc.args else str(exc)) from exc


def _decision(result: Any) -> str | None:
    """'approved', 'declined', or None when no dialog was shown (the console decides)."""
    if isinstance(result, AcceptedElicitation):
        if isinstance(result.data, Approval):
            return "approved" if result.data.approve else "declined"
        return None
    return "declined" if getattr(result, "action", None) in ("decline", "cancel") else None


def build_server(cfg: dict[str, Any], db_path: str | Path | None = None) -> tuple[MCPServer, MapperTools]:
    tools = MapperTools(cfg, db_path)
    mcp = MCPServer("dmr-mapper", title="DMR system mapper", instructions=INSTRUCTIONS, version=__version__)
    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    radio = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

    @mcp.tool(annotations=read)
    async def get_status() -> dict[str, Any]:
        """The daemon (mode, receiver, power, what the running session is doing), the last sessions,
        requests waiting for approval, database counts, the latest survey per band, snapshots and the
        configured bands."""
        return await _call(tools.get_status)

    @mcp.tool(annotations=read)
    async def list_carriers(
        band: Annotated[str | None, Field(description="band name from get_status")] = None,
        since_hours: Annotated[float, Field(description="heard within this many hours")] = 168,
        min_snr_db: float | None = None,
        variant: Annotated[str | None, Field(description="capplus, conplus, tier3, xpt, tier2, unknown ...")] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Known carriers: variant, colour code, last seen, decode time, bursts, key-ups, best SNR,
        and the site / system / LCN the correlator put them in."""
        return await _call(tools.list_carriers, band, since_hours, min_snr_db, variant, limit)

    @mcp.tool(annotations=read)
    async def get_fingerprint(freq: Freq, days: float = 7) -> dict[str, Any]:
        """Everything the database knows about one carrier: activity counters, colour codes, event
        kinds with their most frequent payloads (site and system announcements, neighbours, grants),
        talkgroups and (pseudonymous) radios, key-ups, unrecognized DSD-FME line patterns, and its links."""
        return await _call(tools.get_fingerprint, freq, days)

    @mcp.tool(annotations=read)
    async def list_survey_hits(band: str | None = None, run_id: int | None = None, min_snr_db: float | None = None,
                               limit: int = 200) -> dict[str, Any]:
        """Survey hits of the latest run per band (or of one run), with what each is known as."""
        return await _call(tools.list_survey_hits, band, run_id, min_snr_db, limit)

    @mcp.tool(annotations=read)
    async def query_systems(system_id: int | None = None, freq: Freq | None = None,
                            min_confidence: Annotated[float, Field(description="links scored below this are left out")] = 0.3) -> dict[str, Any]:
        """Systems → sites → carriers (with LCNs), and the links among them with scores and labels.
        Give a system id or a carrier's frequency for one system."""
        return await _call(tools.query_systems, system_id, freq, min_confidence)

    @mcp.tool(annotations=read)
    async def explain_link(link_id: int | None = None, a_freq: Freq | None = None, b_freq: Freq | None = None,
                           kind: Literal["same_site", "same_system", "neighbor_site"] | None = None) -> dict[str, Any]:
        """Every evidence row behind a link: raw score, weight, time decay, contribution, support,
        source (auto / manual / mcp) and details; how noisy-OR, contradictions and the Tier II cap combine."""
        return await _call(tools.explain_link, link_id, a_freq, b_freq, kind)

    @mcp.tool(annotations=read)
    async def get_session(session_id: int | None = None,
                          request_id: Annotated[int | None, Field(description="the request_id a scan tool returned")] = None,
                          wait_s: Annotated[float, Field(description="wait up to this long (max 55 s) for it to finish")] = 0) -> dict[str, Any]:
        """A session's state and summary (the latest when no id is given), or what became of a request."""
        return await _call(tools.get_session, session_id, request_id, wait_s)

    @mcp.tool(annotations=write)
    async def propose_link(a_freq: Freq, b_freq: Freq, kind: Literal["same_site", "same_system", "neighbor_site"],
                           confidence: Annotated[float, Field(gt=0, le=1)],
                           note: Annotated[str, Field(description="your reasoning, for the operator")]) -> dict[str, Any]:
        """Record your judgement that two carriers are linked, as evidence with source 'mcp'. It raises
        the score but alone never makes a link confirmed nor lifts the Tier II cap. Replaces your
        earlier proposal for the same link."""
        return await _call(tools.propose_link, a_freq, b_freq, kind, confidence, note)

    @mcp.tool(annotations=write)
    async def withdraw_proposal(a_freq: Freq, b_freq: Freq,
                                kind: Literal["same_site", "same_system", "neighbor_site"] | None = None) -> dict[str, Any]:
        """Take back your proposals between two carriers (the operator's and the automatic evidence stay)."""
        return await _call(tools.withdraw_proposal, a_freq, b_freq, kind)

    @mcp.tool(annotations=write)
    async def correlate_now() -> dict[str, Any]:
        """Run the evidence engine now (through the daemon when it runs): links, sites and systems."""
        return await _call(tools.correlate_now)

    def _ask(ctx: Context, kind: str, **kw: Any) -> Any:
        """Resolver: check that the session could start (a refusal needs no question), then ask the
        person in Claude Code when the client can show a dialog."""
        try:
            _, _, what = tools.prepare_session(kind, **kw)
        except (KeyError, ValueError) as exc:
            raise ToolError(str(exc.args[0]) if exc.args else str(exc)) from exc
        caps = ctx.client_capabilities
        if tools.approval != "ask" or caps is None or caps.elicitation is None:
            return None
        return Elicit(f"Claude asks to use the receiver on the Pi: {what}. Approve?", Approval)

    def ask_scan(ctx: Context, band: str | None = None, start: float | None = None, stop: float | None = None,
                 minutes: float | None = None, then_decode: bool = False) -> Any:
        return _ask(ctx, "survey", minutes=minutes, bands=[band] if band else None, start=start, stop=stop, then_decode=then_decode)

    def ask_decode(ctx: Context, freqs: list[float] | None = None, minutes: float | None = None) -> Any:
        return _ask(ctx, "decode", minutes=minutes, freqs=freqs)

    def ask_watch(ctx: Context, freqs: list[float] | None = None, center: float | None = None, minutes: float | None = None) -> Any:
        return _ask(ctx, "watch", minutes=minutes, freqs=freqs, center=center)

    async def _session(answer: Any, kind: str, **kw: Any) -> dict[str, Any]:
        command, params, what = await _call(tools.prepare_session, kind, **kw)
        decision = _decision(answer)
        if decision == "declined":
            return tools.refuse_note(what)
        return await _call(tools.submit_session, command, params, "operator in Claude Code" if decision == "approved" else None)

    @mcp.tool(annotations=radio)
    async def scan_band(band: Annotated[str | None, Field(description="one configured band; default all")] = None,
                        start: Freq | None = None, stop: Freq | None = None,
                        minutes: Annotated[float | None, Field(description="session budget (default mcp.default_session_minutes)")] = None,
                        then_decode: Annotated[bool, Field(description="decode what was found in the same budget")] = False,
                        answer: Annotated[ElicitationResult[Approval], Resolve(ask_scan)] = None) -> dict[str, Any]:
        """Survey a band (or start..stop inside a configured band) for carriers. Asks the operator first."""
        return await _session(answer, "survey", minutes=minutes, bands=[band] if band else None, start=start, stop=stop, then_decode=then_decode)

    @mcp.tool(annotations=radio)
    async def decode_carriers(freqs: Annotated[list[float] | None, Field(description="carriers to decode; none: plan from the latest survey")] = None,
                              minutes: float | None = None,
                              answer: Annotated[ElicitationResult[Approval], Resolve(ask_decode)] = None) -> dict[str, Any]:
        """Run DSD-FME on carriers (several at once when they fit in one slice). Asks the operator first.
        Then get_fingerprint shows what was heard."""
        return await _session(answer, "decode", minutes=minutes, freqs=freqs)

    @mcp.tool(annotations=radio)
    async def watch_slice(freqs: Annotated[list[float] | None, Field(description="carriers the slice must hold (default: the Tier III network with most carriers)")] = None,
                          center: Freq | None = None, minutes: float | None = None,
                          answer: Annotated[ElicitationResult[Approval], Resolve(ask_watch)] = None) -> dict[str, Any]:
        """Watch one receiver slice (Capacity Max): control channels decoded throughout, every traffic
        channel from its key-up. Asks the operator first."""
        return await _session(answer, "watch", minutes=minutes, freqs=freqs, center=center)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def stop_session() -> dict[str, Any]:
        """Stop the running session now and hand the receiver back (needs no approval)."""
        return await _call(tools.stop_session)

    return mcp, tools


def serve(cfg: dict[str, Any], db_path: str | Path | None = None, host: str | None = None, port: int | None = None) -> None:
    m = cfg.get("mcp", {}) or {}
    host = str(host or m.get("bind") or "127.0.0.1")
    port = int(port or m.get("port") or 8765)
    check_bind(host, bool(m.get("allow_any_bind", False)))
    names = [host, *[str(h) for h in (m.get("allowed_hosts") or [])]]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=[f"{n}:{port}" for n in names] + names)
    server, tools = build_server(cfg, db_path)
    log.info("MCP server on http://%s:%d/mcp (database %s, approval %s)", host, port, tools.db_path, tools.approval)
    server.run("streamable-http", host=host, port=port, streamable_http_path="/mcp", transport_security=security)
