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
 Raspberry Pi 5 (2x SDRplay RSP1B, headless, Tailscale)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ collector (systemd)                                                  │
 │   band survey ──► carrier list ──► channelizer ──► dsd-fme ×4        │
 │                                        48 kHz PCM / TCP     │ stderr │
 │                                                             ▼        │
 │   streaming parser ──► aggregated events ──► SQLite (var/mapper.db) │
 │                                                             │        │
 │   correlator (periodic) ◄───────────────────────────────────┘        │
 │     explicit · behavioural · identity · rf evidence ──► links+score  │
 │                                                                      │
 │   MCP server (HTTP over Tailscale) ── scan_band · list_carriers ·    │
 │       decode_channel · get_fingerprint · query_systems · propose_link│
 └───────────────────────────┬──────────────────────────────────────────┘
                             │ Tailscale                 nightly rsync of the DB
                             ▼                                   ▼
            Claude Code on the laptop (.mcp.json)          archive Pi
```

Deterministic code does all decoding (DSD-FME) and correlation. Claude's role through MCP is
orchestration and judgement over evidence, never DSP. SDRangel on the laptop is for manual
investigation only.

## Documents

- [DESIGN.md](DESIGN.md): database schema, evidence types with weights and time decay, confidence
  combination, component interfaces, configuration, storage budget, MCP tool list.
- [CAPTURE.md](CAPTURE.md): the exact DSD-FME command line for the Pi and for manual captures,
  and the log line formats the parser depends on.
- [config.example.yaml](config.example.yaml): all settings with comments. Copy to
  `config.local.yaml` (git-ignored) for machine-specific values; never commit Tailscale addresses,
  device serials or paths.

## Phases

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Sample inspection, DESIGN.md, CAPTURE.md, README, .gitignore, config example | done |
| 1 | `mapper ingest <path>`: DSD-FME log and surveyor output → SQLite, with coverage report | planned |
| 2 | `mapper correlate` / `systems` / `explain` / `export`: evidence engine, NetworkX graph, static HTML | planned |
| 3 | Collector service for the Pi: survey, streaming decode, systemd, log rotation, nightly rsync, `deploy.sh` | planned |
| 4 | MCP server (HTTP over Tailscale) and `.mcp.json` snippet | planned |

Each phase ends with a commit to `main` and waits for approval.

## Requirements

- Python 3.11+, SQLite (stdlib), `pyyaml`, `networkx`; `mcp` for Phase 4.
- On the Pi: `dsd-fme` (build `AW 2026-26` or newer), SoapySDR with SoapySDRPlay3, `numpy`.
- Development is on Windows and in a cloud sandbox; the collector and MCP server run on Linux.

Install for development (from Phase 1 on):

```sh
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
pytest
```

## Samples (`samples/`)

Checked-in DSD-FME output used as offline test fixtures. Everything must work on these before
hardware is involved.

| Path | What it is |
|---|---|
| `samples/capplus/dsd_fme_168863_20260509_163344.log` | 17-minute live capture (2026-05-09, 168.863 MHz) of a Motorola Linked Capacity Plus site: site 2, colour code 2, neighbour site 1, rest-channel hops every 30 s, group calls on TG 1 / 2102 / 2103, LRRP data. 2.5 MB, raw ANSI colour. |
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

No Connect Plus, Tier III or Hytera XPT capture exists in the samples yet. Those parsers are
written from the DSD-FME source formats and tested on synthetic lines; a real capture of each,
named per CAPTURE.md, is the most useful thing to add to `samples/`.

## Licences of the reference projects

dmr-iq-surveyor declares MIT. DMR-DECREP-SHAHAR has no licence file, so its parser logic is
re-implemented here from its behaviour rather than copied. The P25 mapper is private.
