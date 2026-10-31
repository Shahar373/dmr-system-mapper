# dmr-system-mapper

Passive DMR network topology mapper. It listens to VHF/UHF DMR carriers with DSD-FME, extracts
the system identity each variant leaks on air (Capacity Plus rest channels, Connect Plus and
Tier III site/neighbour announcements, Hytera XPT free-repeater lists, shared radio IDs and
talkgroups), and links carriers into logical sites and systems. **Every link carries a
confidence score and the evidence that produced it**; Tier II conventional links are never
presented as certain.

Companion to [dmr-iq-surveyor](https://github.com/Shahar373/dmr-iq-surveyor) (wideband carrier
detection), [DMR-DECREP-SHAHAR](https://github.com/Shahar373/DMR-DECREP-SHAHAR) (Capacity Plus
monitor) and the P25 topology mapper. Those projects are read-only references; this repository
stands alone.

## Architecture

```
 Raspberry Pi 5 (1x SDRplay RSP1B shared with other SDR services, headless, often on battery)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ mapper daemon (systemd, always running, holds NO receiver while idle)    │
 │   command queue ◄── mapper tui (SSH) · mapper survey/decode (CLI) · MCP  │
 │   session with a time budget, only when asked:                           │
 │     survey: step the band, PSD per step ──► survey_hits                  │
 │     decode: plan slices ──► 2 MS/s ──► FFT channelizer ──► dsd-fme ×N    │
 │             48 kHz PCM / TCP │ stderr ──► one writer ──► SQLite          │
 │     watch:  one slice, power of every channel per 0.1 s ──► key-ups;     │
 │             dsd-fme on the control channel + each channel that keys up   │
 │   checks before each session: receiver present and free, power state    │
 │   after a decode session: correlator ──► links + score + evidence        │
 └───────────────────────────────┬──────────────────────────────────────────┘
                                 │ Tailscale              nightly snapshot of the DB
                                 ▼                                   ▼
          SSH terminal (mapper tui), Claude Code (MCP, Phase 5)   archive Pi
```

Deterministic code does all decoding (DSD-FME) and correlation. Claude's role through MCP is
orchestration and judgement over evidence, never DSP. SDRangel on the laptop is for manual
investigation only.

## Documents

- [DESIGN.md](DESIGN.md): database schema, evidence types with weights and time decay, confidence
  combination, component interfaces, configuration, storage budget, MCP tool list.
- [CAPTURE.md](CAPTURE.md): the exact DSD-FME command line for the Pi and for manual captures,
  and the log line formats the parser depends on.
- [DEPLOY.md](DEPLOY.md): installing and updating the service on the Pi, daily use, getting data off,
  the archive Pi, debugging, restoring.
- [config.example.yaml](config.example.yaml): all settings with comments. Copy to
  `config.local.yaml` (git-ignored) for machine-specific values; never commit Tailscale addresses,
  device serials or paths.

## Phases

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Sample inspection, DESIGN.md, CAPTURE.md, README, .gitignore, config example | done |
| 1 | `mapper ingest <path>`: DSD-FME log and surveyor output → SQLite, with coverage report | done |
| 2 | `mapper correlate` / `systems` / `explain` / `export`: evidence engine, NetworkX graph, static HTML | done |
| 2.1 | Review fixes: site ids on the upsert path, live ingest commits per event, `correlate` output, `window_days` in config | done |
| 3a | `mapper daemon` / `tui` / `survey` / `decode` / `doctor`: on-demand sessions with a time budget, band survey, FFT channelizer, DSD-FME decode, single-writer live ingest, Textual console | done |
| 3b | Deployment: `dmr-mapper` systemd service, `deploy/deploy.sh` (pull, install, restart), nightly snapshot + export timer, read-only archive pull, size-capped raw debug logs, `mapper snapshot` / `export --all` | done (first install on the Pi pending) |
| 3c | GPS track and position of every survey hit (gpsd, or the device when nobody else has it open), one-button survey+decode (`a`), radios' LRRP positions no longer kept, radio IDs and talkgroups kept 90 days (`mapper prune`) | done |
| 4 | Capacity Max in depth: `mapper watch` (a whole slice, DSD-FME on the control channel and on every traffic channel when it keys up, with pre-roll), Cap Max channel updates and neighbour LCNs from DSD-FME's raw bytes, LCN learning, Activity Updates from `SLCO Completed Block`, one Tier III numbering whatever DSD-FME prints, `mapper name` for systems and sites | done, checked on air 2026-10-31 (the 469 MHz network: two sites and their LCNs, an unheard third site; DESIGN.md §12) |
| 5 | `mapper serve-mcp`: MCP server (streamable HTTP on the Tailscale address, `dmr-mapper-mcp` service), 14 tools; Claude may look and propose, scans only with a person's approval (Claude Desktop's own tool prompt, a dialog in Claude Code, or `y` in the console / `mapper approve`); proposals never confirm a link by themselves; radio IDs as pseudonyms | done, used from Claude Desktop 2026-10-31 (a survey asked for by Claude, approved and read back) |

Each phase ends with a commit to `main` and waits for approval.

## Requirements

- Python 3.11+, SQLite (stdlib), `pyyaml`, `networkx`; `mcp` 2.x for the MCP server (`pip install -e '.[mcp]'`).
- Collector and console: `numpy`, `scipy`, `textual` (`pip install -e '.[collector,tui]'`).
- On the Pi: `dsd-fme` (build `AW 2026-26` or newer), SoapySDR with SoapySDRPlay3 and its Python
  bindings. `mapper doctor` checks all of these.
- Development is on Windows and in a cloud sandbox; the collector and MCP server run on Linux.

Install for development:

```sh
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
pytest
```

## Usage (Phase 1)

```sh
mapper ingest samples/capplus/dsd_fme_168863_20260509_163344.log   # one DSD-FME log (freq + start from the name)
mapper ingest samples/surveyor                                       # a dmr-iq-surveyor decode tree
mapper ingest captures/ --force                                      # a directory of logs, re-ingest even if seen
mapper ingest some.log --freq 451237500 --start 2026-08-01T12:00:00+03:00
dsd-fme -fs -i tcp -o null -Z -D 0 2>&1 | mapper ingest - --freq 168863000   # live stream
mapper ingest ... --json                                             # machine-readable report
mapper status                                                        # table counts and carriers
```

## Usage (Phase 2)

```sh
mapper correlate                         # run the evidence engine, derive sites and systems
mapper correlate --now 2026-05-10T00:00:00Z --window-days 90   # replay old captures as if it were then
mapper systems                           # systems → sites → carriers, then the links with scores
mapper systems --min-label weak --json   # everything, machine-readable
mapper explain 3                         # every evidence row behind link #3 and how it combines
mapper explain 168500000 168512500       # the same, by frequencies
mapper export                            # var/exports/map.html (static, single file, no CDN)
mapper export --html map.html --graphml map.graphml --json-path map.json --min-label probable
mapper propose 164300000 167137500 same_system 0.9 --note "heard the dispatcher on both"
```

The correlator collects ten evidence types (explicit site/system/neighbour/channel announcements,
Capacity Plus rest-channel hops and LSN-list alignment, shared radio IDs, talkgroups and undecoded SLCO
payloads, RF activity profiles), stores every row in `evidence`, combines them per link with noisy-OR after weight and
time decay, and labels each link `confirmed` / `probable` / `possible` / `weak`. A link supported
only by Tier II evidence is capped at 0.6 and marked `T2` in the map; `confirmed` needs explicit or
manual evidence. `propose` records manual evidence that the automatic pass never overwrites or
decays. Sites are connected components of `same_site` links, systems of `same_system` and
`neighbor_site` links; sites named only in neighbour announcements appear as hollow diamonds.

The database defaults to `var/mapper.db` (override with `--db` or `paths.db` in `config.local.yaml`).
Every ingest prints a coverage report: bursts by type, colour codes, structural events (and how many
rows survived de-duplication), rest-channel transitions, identities, the variant guess, parsers that
have no real capture yet (`untested_on_air`), and every unrecognized line pattern with an example, so a
new DSD-FME build shows up as a short list rather than silent loss. A file whose content was already
ingested (keyed on a hash of its first 64 KB, so copies at other paths count) is skipped unless
`--force` is given; a file that has grown since, such as a raw log still being appended to, is
resumed from the byte where the last ingest stopped.

What ingest extracts from a log: burst header per frame (colour code, slot, burst type, CRC state),
Capacity Plus channel status / LSN lists / site / adjacent sites / preambles / calls, Connect Plus
site, neighbours and grants, Tier III system parameters, C_BCAST broadcasts and grants, Hytera XPT
site status, adjacent sites and free LCN, Tier II activity updates and LC headers, data headers,
IP maps and LRRP (with position when present), plus radio IDs and talkgroups with their roles.

## Usage (Phase 3a)

The daemon runs all the time but touches the receiver only during a session you start. Every
session has a time budget and releases the receiver when it ends, when you stop it, or when the
firmware reports undervoltage. Before starting, it checks that the receiver is connected and not
held by another program (`sdr.known_users`), and reads the power state to choose how many DSD-FME
processes it can afford (fewer on battery).

```sh
mapper doctor                      # what is installed, is the receiver there, power, database
mapper daemon                      # foreground; systemd runs this in Phase 3b
mapper tui                         # the console, over SSH: a survey+decode · s survey · d decode · w watch · x stop · c correlate · e export · n name · y/v approve/refuse Claude · q quit
mapper survey --minutes 10         # same from the shell; without a running daemon a temporary one is used
mapper survey --band uhf --then-decode --minutes 20   # one budget for both
mapper decode 168863000 168875000 --minutes 5   # specific carriers, or none: plan from the latest survey
mapper stop
mapper status                      # now also shows the daemon and the last sessions
mapper capture-iq --center 165000000 --seconds 11 --out /tmp/x.cf32   # dev tool: raw IQ for replay
```

On the Pi the daemon is a systemd service installed by `deploy/deploy.sh` (see [DEPLOY.md](DEPLOY.md)),
and `mapper` works from any folder. Exports and archive copies:

```sh
mapper export --all              # map.html, map.json, map.graphml, systems.txt in var/exports/
mapper snapshot                  # consistent copy of the database in var/snapshots/ (nightly by timer)
```

A decode session plans from the latest survey: carriers never decoded, strong, stale, or of a
variant that announces its identity come first, and carriers that look like the same Capacity Plus
site and fit in one 1.6 MHz slice (at 2 MS/s) are decoded together with a double dwell, because that is what
produces rest-hop evidence. After a decode session the correlator runs. Closing the console never
stops a session. Development without hardware uses `sdr.source: file` (replay of an IQ recording)
or the synthetic receiver in the tests.

## Usage (Phase 4)

A Capacity Max traffic channel transmits only during a call, so a survey dwell misses it and a
decode slice listens to silence. A watch session tunes once and listens to the whole slice:

```sh
mapper watch --minutes 20                    # the Tier III network with the most carriers in one slice
mapper watch 469.35 469.65 --minutes 20      # a slice holding these carriers (Hz or MHz)
mapper watch --center 469400000              # or simply here
mapper tui                                   # w: watch, n: name the system/site selected on the Systems tab
```

Every 0.1 s the power of every 12.5 kHz channel in the slice is measured from the channelizer's
FFT. The control channel keeps one DSD-FME for the whole session; a channel that keys up gets one
at once, fed first with the half second before the key-up (rebuilt from kept FFT blocks) so the
call header is not lost, and loses it 2 s after the key-down. At most `watch.max_instances` (6)
run at once on mains, `power.battery_watch_instances` (3) on battery, the control channels taking
at most half. Every key-up is stored (`keyups`), and a carrier nobody knew about is added. After
the session the correlator runs.

What the control channel says that DSD-FME does not print is read from the bytes it prints with
`-Z` (`DMR PDU Payload`, `SLCO Completed Block`): Capacity Max channel updates (talkgroup T on LCN
L, timeslot s), the LCN of a neighbour site's control channel when DSD-FME leaves it out, and the
content of short LCs it misnames. Lining up channel updates with the calls and key-ups of the
carriers in the slice gives each traffic channel its LCN (`behavioral_lcn_keyup`); a neighbour
site never heard shows as an announced site with its control channel's LCN.

Tier III networks and sites are shown with the numbers sent on air, as SDRTrunk shows them (net 1
site 1 is what DSD-FME prints as `Net ID: 2; Site ID: 1.2`).

Names survive correlator runs (they follow the on-air identity, e.g. Tier III net 1, or the
carriers when there is none) and show in `mapper systems`, the map and the console:

```sh
mapper name system 469.35 North network     # the system holding that carrier (or its id from mapper systems)
mapper name site 469650000 Hilltop
mapper name site 469650000 --clear
mapper names
```

## Usage (Phase 5)

Claude Code on the laptop talks to the Pi through `mapper serve-mcp` (a service beside the daemon,
set up in [DEPLOY.md §6](DEPLOY.md#6-claude-code-mcp)): `mcp.enabled: true` and the Pi's Tailscale
address in `config.local.yaml`, `deploy/deploy.sh`, then on the laptop:

```sh
claude mcp add --transport http --scope user dmr-mapper http://<pi-tailscale-ip>:8765/mcp
```

(or, for Claude Desktop, a `.mcp.json` in the folder of a Local Code session; DEPLOY.md §6). Claude reads (`get_status`, `list_carriers`, `get_fingerprint`, `list_survey_hits`,
`query_systems`, `explain_link`, `get_session`), proposes (`propose_link`, `withdraw_proposal`,
`correlate_now`), stops a session (`stop_session`), and asks for scans (`scan_band`,
`decode_carriers`, `watch_slice`). A scan starts only when a person approves it: with Claude
Desktop (Code tab, Local session) its own "allow this tool?" prompt (`mcp.approval: client`); with
Claude Code in a terminal a dialog from the server (`ask`); or on the Pi (`console`):

```sh
mapper requests          # what Claude asked for
mapper approve           # the newest request (or: mapper approve 12)
mapper refuse 12
mapper tui               # the status bar shows "Claude asks #12: ..."; y approves, v refuses
```

A proposal is evidence with source `mcp`: it raises a link's score (at most
`mcp.max_proposal_confidence`, 0.8) but never makes it `confirmed` and never lifts the Tier II cap;
`mapper explain` shows it with its note, and `mapper propose` (yours) still counts in full. Radio
IDs reach Claude as pseudonyms unless `mcp.show_radio_ids: true`.

## Samples (`samples/`)

Checked-in DSD-FME output used as offline test fixtures. Everything must work on these before
hardware is involved.

| Path | What it is |
|---|---|
| `samples/capplus/dsd_fme_168863_20260509_163344.log` | 17-minute live capture (2026-05-09, 168.863 MHz) of a Motorola Linked Capacity Plus site: site 2, colour code 2, neighbour site 1, rest-channel hops every 30 s, group calls on TG 1 / 2102 / 2103, LRRP data. 2.5 MB, raw ANSI colour. |
| `samples/capmax/dsd_fme_469650000_20261031_003613_excerpt.log` | Two seconds of the Capacity Max site-2 control channel (469.65 MHz, 2026-10-31, `-Z`): every CSBK flagged `(CRC ERR)` by DSD-FME (a keyed CRC) but readable from its bytes; Aloha SYS 2102, neighbours site 3 on LCN 5 and site 1 on LCN 2. No radio IDs. |
| `samples/surveyor/decodes_20260713/` | dmr-iq-surveyor decode batch of 8 VHF carriers (162.5–167.1 MHz) from two 11-second IQ recordings on 2026-07-13: per-carrier `extraction_report.json`, `decoder/decoder_report*.json`, and the DSD-FME logs. Two carriers are further Capacity Plus sites (0 and 5), six are Tier II conventional repeaters. |

Things to know about the surveyor samples:

- **`dsd_fme_*_stdout.log` files are empty on purpose.** DSD-FME prints to stderr; the decode text
  is in `dsd_fme_normal_stderr.log` (use `_inverted_` only when `decoder_report.json` says
  `best_inversion: inverted`).
- Line timestamps there are the decode wall clock, not the RF time; the RF time is the recording
  id in the path.
- The batch has no carrier list with SNR; only the decode summaries are included.
- The original batch also contained 15 discriminator WAVs and 15 baseband previews (24 MB).
  They were removed in Phase 0 except
  `C0013/20260713_150256/iq/discriminator.wav`, kept as an optional DSD-FME regression input.
  The repository history still contains the removed blobs.

No Connect Plus, Tier III or Hytera XPT capture exists in the samples yet, and of Capacity Max only the control-channel excerpt above. Those
parsers are written from the DSD-FME source formats (and SDRTrunk's bit layouts for the Motorola
messages) and tested on synthetic lines; a real capture of each, named per CAPTURE.md, is the most
useful thing to add to `samples/`. For the 469 MHz Capacity Max network: set
`decode.keep_raw_logs: true`, run `mapper decode 469350000 469650000 --minutes 5`, and copy the
files from `var/raw/`.

## Licences of the reference projects

dmr-iq-surveyor declares MIT. DMR-DECREP-SHAHAR has no licence file, so its parser logic is
re-implemented here from its behaviour rather than copied. The P25 mapper is private.
