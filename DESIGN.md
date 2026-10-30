# dmr-system-mapper — Design

Passive DMR network topology mapper. It links VHF/UHF carriers into logical systems and sites,
stores a confidence score and the evidence behind every link, and exposes the result to Claude Code
through an MCP server on the Raspberry Pi (Phase 5).

This document is the contract for Phases 1–5. Everything here is testable offline on `samples/`.

Contents

1. [Problem and approach](#1-problem-and-approach)
2. [Components and data flow](#2-components-and-data-flow)
3. [Database schema](#3-database-schema)
4. [Evidence model](#4-evidence-model)
5. [Confidence combination](#5-confidence-combination)
6. [What each DMR variant gives us](#6-what-each-dmr-variant-gives-us)
7. [Component interfaces](#7-component-interfaces)
8. [Configuration](#8-configuration)
9. [Storage budget](#9-storage-budget)
10. [MCP tools](#10-mcp-tools)
11. [Repository layout](#11-repository-layout)
12. [Open points and known limits](#12-open-points-and-known-limits)

---

## 1. Problem and approach

Unlike P25, DMR has no single control channel that names the system. What a carrier reveals about
its system depends on the variant:

| Variant | Explicit identity on air | Linking strategy |
|---|---|---|
| Tier II conventional | none (colour code, short LC, talkgroups, radio IDs) | probabilistic only, capped confidence |
| Capacity Plus (single site) | rest-channel CSBKs with LSN, per-LSN channel list, site number in short LC | learn LCN→frequency by watching rest-channel hops across carriers |
| Linked Capacity Plus | as above + `Adjacent Sites` CSBK (site, rest LSN) | explicit site neighbours + hop learning |
| Connect Plus | control channel with Net ID, Site ID, neighbour list, LCN grants | explicit site and neighbour evidence |
| Tier III (Hytera/Tait/Simoco) | `C_ALOHA` / `SLC_C_SYS_PARMS` (system code, net, site), `C_BCAST` adjacent sites, `Chan_Freq` announcements | explicit system/site/neighbour, sometimes absolute frequencies |
| Hytera XPT | free-repeater announcements, site ID, adjacent sites | explicit site + LCN list |

DSD-FME already decodes all of these into text. We never decode DMR ourselves. The pipeline is:

```
RF  →  survey (carrier list)  →  DSD-FME per carrier  →  line parser  →  SQLite (aggregated)
                                                                             ↓
                                   correlator (evidence engine)  →  systems / sites / links
                                                                             ↓
                                              CLI reports, HTML graph, MCP server (Claude)
```

Deterministic code does all decoding and correlation. Claude, through MCP, drives surveys,
inspects evidence and may *propose* links; a proposal is just one more evidence row with
`source = manual`.

## 2. Components and data flow

| Component | Runs on | Phase | Role |
|---|---|---|---|
| `mapper ingest` | laptop / Pi | 1 | Parse DSD-FME logs and surveyor output into SQLite |
| `mapper correlate` / `systems` / `explain` / `export` | laptop / Pi | 2 | Evidence engine, reports, NetworkX + static HTML |
| `mapper daemon` | Pi (systemd) | 3a | Idle until asked; survey and decode sessions with a time budget; single writer of decoded data |
| `mapper tui` | Pi, over SSH | 3a | Operator console: status, start/stop, carriers, survey hits, systems, sessions |
| deployment | Pi | 3b | `dmr-mapper` systemd service, `deploy/deploy.sh`, nightly snapshot/export timer, archive Pi pulls with a read-only `rrsync` key (DEPLOY.md) |
| `mapper watch` / `name` | Pi / laptop | 4 | Watch sessions (one slice, decoders on key-up), Capacity Max from raw bytes, names for systems and sites |
| MCP server | Pi (HTTP over Tailscale) | 5 | Tools for Claude Code on the laptop |

The daemon, the console and the CLI share one library (`dmr_mapper`) and one database. The
receiver is shared with other SDR services on the Pi, so nothing runs on a timer: work happens in
**sessions** that an operator (console, CLI) or Claude (MCP) starts.

### Daemon, sessions and the command queue (Phase 3a)

- **One control channel.** Clients only insert rows into `commands` (`start_survey`, `start_decode`,
  `start_watch`, `stop`, `correlate`, `export`) and read `daemon_state` and `sessions`. The daemon claims commands
  atomically, writes a heartbeat every tick, and is the only process that opens the receiver. A
  console, the CLI and the MCP server therefore can never fight over the tuner.
- **One daemon per database**, enforced by an `flock` on `<db>.daemon.lock` (released by the kernel
  if the process dies).
- **Before a session**: the receiver is probed (present / absent / busy, with the name of a running
  program from `sdr.known_users`), `dsd-fme` must be found for a decode, and the power state is read.
  A refusal is the command's result, shown in the console log.
- **Power**: the state file of the Pi's power guard (`power.state_file`, read by meaning, not by
  exact key names) or `vcgencmd get_throttled`. Not on a confirmed full supply → at most
  `power.battery_max_instances` DSD-FME processes and `power.battery_session_s` by default;
  undervoltage now → one process, and a running session is stopped.
- **Session budget**: every session ends by itself; `stop` ends it at the next block. The receiver
  is closed in every exit path.
- **Survey**: steps of `sample_rate × usable_fraction` (1.6 MHz at 2 MS/s), Welch PSD per
  `segment_s`, channel power on the raster, noise floor = median channel, hit when the 95th
  percentile SNR clears `snr_threshold_db`; neighbours of a strong carrier are suppressed.
  Hits go to `survey_runs` / `survey_hits` (pruned after `daemon.survey_hits_keep_days`).
- **Decode**: the scheduler scores targets (never decoded +3, staleness, SNR, identity-announcing
  variant +1, explicitly requested +5) and picks slices greedily; carriers of one Cap+ site or
  Tier III system that fit in one slice get a +2 group bonus and a double dwell. Per slice: retune,
  FFT channelizer (overlap-save, sample_rate/D per channel: 80 kHz at 2 MS/s, 7.5 kHz low-pass, discriminator, rational resampling to 48 kHz int16,
  slow level normalisation), one PCM TCP server and one DSD-FME per carrier, stderr of all of them
  into one queue, parsed and written by the session thread, committed after every block so no
  write lock is held. A carrier that shows only `-DMR` syncs after `decode.invert_retry_s` is
  restarted with `-xr`. Survey SNR is copied into `observations.snr_db_*`.
- **Watch** (Phase 4, `collector/watch.py`): for networks whose traffic channels transmit only during
  calls (Capacity Max). One slice for the whole session, planned around the Tier III network with
  the most known carriers that fit (control channels count double), or around given carriers or a
  centre, off the DC point. Every 0.1 s block: the power of every raster channel from the
  channelizer's FFT (mean over ±5 kHz), noise floor = median channel, key-up after two blocks over
  `watch.keyup_snr_db` (a channel 25 dB under its neighbour is that neighbour's skirt), key-down
  after `hang_s` under `keydown_snr_db`. Known carriers off the 12.5 kHz raster are watched at
  their own frequency instead of their raster twin; known carriers on the DC point are listed as
  `blind`. Only known control channels of the network are pinned (at most half the instances, one
  on battery); a pinned DSD-FME that dies is restarted (3 times). A channel that keys up gets a
  DSD-FME at once, and its audio is first rebuilt from the last `preroll_s` of kept FFT blocks and
  buffered in the PCM bridge until DSD-FME connects, so the call header before the detection is not
  lost. One DSD-FME process per transmission, so its lines can only belong to that carrier; one
  ingest run per carrier per session, opened when its DSD-FME prints a DMR sync. Who gets a decoder:
  the network's traffic channels first, then unknown carriers, then carriers known to belong
  elsewhere (other networks, unpinned control channels); within one class the most recent key-up;
  a waiting channel takes the decoder of a lower class at once and of its own class after
  `max_hold_s`, and the one that gave way waits for its key-down. A decoder is retired (bridge
  closed first, so DSD-FME's read ends and it exits, then the process) in a thread of its own: the
  block loop never waits. Instances: `watch.max_instances` (6) on mains,
  `power.battery_watch_instances` (3) on battery, 1 on undervoltage. Key-ups go to `keyups`; a
  carrier is created by a key-up of at least `min_keyup_s` or a DMR sync, never by a blip. Key-up
  times are the wall clock when a block is processed, as DSD-FME's own line clock (both lag the air
  by the same processing delay). The summary reports receiver overflows and the time a block takes
  (`block_ms_avg` / `block_ms_max` against the 100 ms budget). Polarity retry is not done in watch
  sessions.
- **After a decode or watch session** the correlator runs (`daemon.correlate_after_decode`).
- **One button** (`a` in the console, `mapper survey --then-decode`): a survey, then a decode
  session with whatever is left of the one budget. Sessions are short and random (driving, battery).
- **GPS** (Phase 3c), read only while a session runs: gpsd when it is running (shared safely), else
  the serial device only if no other process has it open (two readers of one tty steal each
  other's lines). A track point every 10 s while moving > 25 m, at least one a minute; survey hits
  carry the position of their strongest step. No GPS never blocks a session.
- **Retention** (`retention.identity_days`, 90): radio IDs, talkgroups and the events carrying them
  are deleted after each session and nightly (`mapper prune`); network announcements, observations,
  links and evidence stay. Radios' LRRP coordinates are never stored.

### Time handling

- The database stores **UTC epoch seconds** (integers). Display conversion uses `timezone` from
  config.
- DSD-FME log lines carry only `HH:MM:SS`. The ingest context supplies the date: from the log
  filename (`dsd_fme_<freqHz>_<YYYYmmdd_HHMMSS>.log`), from the surveyor `recording_id`, or from
  the collector's own clock. The parser handles midnight rollover (time going backwards → +1 day).
- Surveyor decode logs carry the *decode* wall clock, not the RF time, and DSD-FME decodes a
  recording much faster than real time (11 s of audio in about 1 s). For them the RF time is the
  recording start, and line clocks are offsets scaled by `duration / (last clock − first clock)`.
- A capture whose first line is just after midnight while its start time is just before it (or
  the reverse, for a buffered line) is placed on the right day by comparing the first line's
  clock with the start-of-capture clock.

### Carrier identity

A carrier is identified by **frequency** (Hz, snapped to the configured raster with
`raster_tolerance_hz`). Surveyor candidate IDs (`C0013`) are renumbered on every run and are kept
only as a label in `ingest_runs`.

## 3. Database schema

SQLite, WAL mode, foreign keys on. All timestamps are UTC epoch seconds. `schema_meta.version`
drives forward-only migrations in `dmr_mapper/db/migrations/NNN_*.sql`.

```sql
-- One row per RF carrier (frequency). Updated on every ingest.
CREATE TABLE carriers (
  id                  INTEGER PRIMARY KEY,
  freq_hz             INTEGER NOT NULL UNIQUE,
  band                TEXT,                 -- survey band name from config, or NULL
  first_seen          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL,
  dominant_cc         INTEGER,              -- colour code 0-15, NULL if unknown
  variant             TEXT NOT NULL DEFAULT 'unknown',  -- tier2|capplus|conplus|tier3|xpt|unknown
  variant_confidence  REAL NOT NULL DEFAULT 0,
  label               TEXT,                 -- free text (manual)
  notes               TEXT
);

-- Aggregated activity per carrier per time bucket (ingest.bucket_minutes, default 15).
-- A row exists only for buckets in which the carrier was actually observed.
CREATE TABLE observations (
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  bucket_start        INTEGER NOT NULL,
  source              TEXT NOT NULL,        -- dsdfme_log|surveyor|collector|import
  seconds_observed    REAL NOT NULL DEFAULT 0,   -- how long a decoder was listening in this bucket
  bursts              INTEGER NOT NULL DEFAULT 0,   -- all "Sync: +DMR" lines
  csbk                INTEGER NOT NULL DEFAULT 0,
  voice               INTEGER NOT NULL DEFAULT 0,   -- VLC + VCn + TLC
  data                INTEGER NOT NULL DEFAULT 0,   -- DATA + R12U/R34U/R_1U bursts
  idle                INTEGER NOT NULL DEFAULT 0,
  crc_err             INTEGER NOT NULL DEFAULT 0,   -- CRC/FEC error lines
  no_sync             INTEGER NOT NULL DEFAULT 0,
  cc_dominant         INTEGER,
  cc_consistency      REAL,                 -- share of bursts carrying cc_dominant
  slot1_syncs         INTEGER NOT NULL DEFAULT 0,
  slot2_syncs         INTEGER NOT NULL DEFAULT 0,
  snr_db_max          REAL,                 -- from survey hits in the same bucket, if any
  snr_db_avg          REAL,
  rest_lsn_last       INTEGER,              -- Cap+ only; last ACCEPTED rest LSN in the bucket
  cc_histogram        TEXT NOT NULL DEFAULT '{}',   -- JSON {cc: bursts}, merged across ingests
  PRIMARY KEY (carrier_id, bucket_start, source)
);

-- Structural events only (never voice frames). De-duplicated: see §3.1.
CREATE TABLE csbk_events (
  id                  INTEGER PRIMARY KEY,
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  ts                  INTEGER NOT NULL,
  slot                INTEGER,              -- 1|2|NULL
  cc                  INTEGER,
  kind                TEXT NOT NULL,        -- see §3.2
  payload             TEXT NOT NULL,        -- JSON, canonical key order
  count               INTEGER NOT NULL DEFAULT 1,
  last_ts             INTEGER NOT NULL,
  dedup_key           TEXT NOT NULL,        -- see §3.1; one row per (carrier, kind, key)
  UNIQUE (carrier_id, kind, dedup_key)
);
CREATE INDEX csbk_events_carrier_kind ON csbk_events(carrier_id, kind, ts);

-- Capacity Plus rest-channel transitions, the raw material for behavioural evidence.
CREATE TABLE rest_events (
  id                  INTEGER PRIMARY KEY,
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  ts                  INTEGER NOT NULL,
  site                INTEGER,              -- from SLCO "Capacity Plus Site", NULL if not seen yet
  rest_lsn            INTEGER NOT NULL,
  prev_lsn            INTEGER               -- NULL on the first observation of a session
);
CREATE INDEX rest_events_ts ON rest_events(ts);
CREATE INDEX rest_events_carrier ON rest_events(carrier_id, ts);

-- Radio IDs and talkgroups.
CREATE TABLE identities (
  id                  INTEGER PRIMARY KEY,
  kind                TEXT NOT NULL,        -- radio|tg
  value               INTEGER NOT NULL,
  first_seen          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL,
  alias               TEXT,
  UNIQUE (kind, value)
);
CREATE TABLE identity_sightings (
  identity_id         INTEGER NOT NULL REFERENCES identities(id),
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  day                 INTEGER NOT NULL,     -- UTC day number (epoch // 86400)
  count               INTEGER NOT NULL DEFAULT 0,
  roles               TEXT NOT NULL DEFAULT '',  -- comma set: src,tgt,lrrp,data,lsn_active
  PRIMARY KEY (identity_id, carrier_id, day)
);

-- Logical systems and sites produced by the correlator (or created manually).
CREATE TABLE systems (
  id                  INTEGER PRIMARY KEY,
  label               TEXT,
  variant             TEXT NOT NULL,        -- as carriers.variant
  sys_code            TEXT,                 -- Tier III SYS (hex), Con+ Net ID, Cap+ NULL
  net_id              INTEGER,
  confidence          REAL NOT NULL DEFAULT 0,
  created_by          TEXT NOT NULL DEFAULT 'auto',   -- auto|manual
  created_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL
);
CREATE TABLE sites (
  id                  INTEGER PRIMARY KEY,
  system_id           INTEGER NOT NULL REFERENCES systems(id),
  site_number         INTEGER,              -- as announced on air
  label               TEXT,
  cc                  INTEGER,
  directly_observed   INTEGER NOT NULL DEFAULT 0,   -- 0 = known only from a neighbour announcement
  first_seen          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL,
  UNIQUE (system_id, site_number)
);
CREATE TABLE carrier_sites (
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  site_id             INTEGER NOT NULL REFERENCES sites(id),
  lcn                 INTEGER,              -- logical channel number on that site, if learned
  confidence          REAL NOT NULL,
  PRIMARY KEY (carrier_id, site_id)
);

-- Pairwise links between carriers, always stored with a_carrier_id < b_carrier_id.
CREATE TABLE links (
  id                  INTEGER PRIMARY KEY,
  a_carrier_id        INTEGER NOT NULL REFERENCES carriers(id),
  b_carrier_id        INTEGER NOT NULL REFERENCES carriers(id),
  kind                TEXT NOT NULL,        -- same_site|same_system|neighbor_site
  score               REAL NOT NULL,        -- combined confidence 0..1
  label               TEXT NOT NULL,        -- confirmed|probable|possible|weak
  tier2_only          INTEGER NOT NULL DEFAULT 0,
  updated_at          INTEGER NOT NULL,
  UNIQUE (a_carrier_id, b_carrier_id, kind),
  CHECK (a_carrier_id < b_carrier_id)
);

-- Every piece of evidence that fed a link. Never deleted; the correlator recomputes decay.
CREATE TABLE evidence (
  id                  INTEGER PRIMARY KEY,
  link_id             INTEGER NOT NULL REFERENCES links(id),
  type                TEXT NOT NULL,        -- see §4
  class               TEXT NOT NULL,        -- explicit|behavioral|identity|rf|manual
  weight              REAL NOT NULL,        -- config weight at the time of computation
  raw_score           REAL NOT NULL,        -- 0..1 strength before weight and decay
  observed_at         INTEGER NOT NULL,     -- newest supporting observation
  first_observed_at   INTEGER NOT NULL,
  support_count       INTEGER NOT NULL DEFAULT 1,
  details             TEXT NOT NULL,        -- JSON: what exactly was seen (human-readable by `explain`)
  source              TEXT NOT NULL DEFAULT 'auto',   -- auto|manual
  UNIQUE (link_id, type, source)
);

-- Ingest bookkeeping and parser coverage.
CREATE TABLE ingest_runs (
  id                  INTEGER PRIMARY KEY,
  source              TEXT NOT NULL,        -- dsdfme_log|surveyor|collector
  path                TEXT,
  carrier_id          INTEGER REFERENCES carriers(id),
  started             INTEGER NOT NULL,
  finished            INTEGER,
  lines               INTEGER NOT NULL DEFAULT 0,
  parsed              INTEGER NOT NULL DEFAULT 0,
  unrecognized        INTEGER NOT NULL DEFAULT 0,
  fingerprint         TEXT,                 -- sha1 of the file's first 64 KB: idempotence key (path-independent)
  byte_offset         INTEGER NOT NULL DEFAULT 0   -- bytes consumed; a file that grew is resumed from here
);
CREATE TABLE unrecognized_lines (
  run_id              INTEGER NOT NULL REFERENCES ingest_runs(id),
  pattern             TEXT NOT NULL,        -- line with digits replaced by '#', truncated
  count               INTEGER NOT NULL,
  example             TEXT NOT NULL,
  PRIMARY KEY (run_id, pattern)
);

-- Survey results (collector Phase 3; surveyor imports in Phase 1 when a candidates file exists).
CREATE TABLE survey_runs (
  id                  INTEGER PRIMARY KEY,
  band                TEXT NOT NULL,
  started             INTEGER NOT NULL,
  finished            INTEGER,
  device              TEXT,
  settings            TEXT NOT NULL         -- JSON snapshot of survey config
);
CREATE TABLE survey_hits (
  run_id              INTEGER NOT NULL REFERENCES survey_runs(id),
  freq_hz             INTEGER NOT NULL,
  snr_db              REAL,
  p95_snr_db          REAL,
  occupancy_pct       REAL,
  width_hz            REAL,
  class               TEXT,                 -- surveyor spectral class
  PRIMARY KEY (run_id, freq_hz)
);

-- Phase 3a (migration 003): the daemon's control surface.
CREATE TABLE daemon_state (id INTEGER PRIMARY KEY CHECK (id = 1), pid, started, heartbeat, mode,   -- idle|survey|decode|offline
                           session_id, sdr, power, progress, message);                            -- JSON blobs
CREATE TABLE sessions (id, kind, requested_by, params, budget_s, created, started, finished,
                       status,                                                -- pending|running|done|stopped|failed
                       summary);                                              -- JSON: hits, carriers, slices, errors
CREATE TABLE commands (id, created, source, kind, params, status, taken_at, done_at, result);

-- Phase 3c (migration 004): where the receiver was. It is driven around, so a carrier heard
-- strongly in one place and weakly in another is no contradiction.
CREATE TABLE positions (id, session_id, ts, lat, lon, alt_m, speed_mps, fix, source);   -- GPS track per session
-- survey_hits gains lat, lon: where the receiver was at the step where the hit was strongest.
-- LRRP payloads keep only has_position; radios' coordinates are never stored (and were removed).

-- Phase 4 (migration 005): what a watch session heard, and the names the operator gives.
CREATE TABLE keyups (id, carrier_id, session_id, start REAL, end REAL, peak_snr_db, decoded);   -- one row per transmission
CREATE TABLE names (id, kind,                -- system|site
                    anchor,                  -- on-air identity: tier3:<model>:<net>[:<site>], conplus:<net>[:<site>]; NULL = by carriers
                    carriers,                -- JSON: carrier frequencies when it was named
                    name, created, updated);
-- systems and sites gain anchor (the same identity strings) and name (filled from names after every derive).

CREATE TABLE schema_meta (version INTEGER NOT NULL);   -- currently 5
```

Migrations run inside one transaction each (`BEGIN … COMMIT`), so a failed migration leaves no
half-created schema. Every ingest of a file is one transaction: an interrupted run rolls back and
the next run starts clean. Live streams commit closed buckets every 60 s and on SIGTERM.

### 3.1 De-duplication rules for `csbk_events`

Storing every CSBK would blow the budget (the Cap+ sample has 9 245 `Channel Status` CSBKs in
17 minutes). Rules:

- `capplus_channel_status`: stored only when the **rest LSN changes** or the **LSN list changes**
  (new TG on an LSN, LSN leaving Idle). Unchanged repeats increment `count`/`last_ts` on the open
  row; `Initial Block` records (which carry no LSN list) never change the signature. Rest LSN
  changes also create a `rest_events` row. A rest LSN from the short LC (`capplus_site`) is
  accepted only when no Channel Status CSBK was seen in the last 10 s, because the short LC lags
  the CSBK and would otherwise create spurious A→B→A transitions.
- `capplus_site`, `capplus_adjacent`, `conplus_adjacent`, `conplus_sys`, `tier3_bcast`,
  `tier3_sys_parms`, `xpt_site_status`, `xpt_adjacent`, `xpt_slco`, `moto_data_channel`, `capmax_slco`:
  one row per distinct `payload` per carrier per UTC day, with `count` incremented on repeats.
  `tier3_sys_parms` ignores the common slot counter (`csc`), which changes every frame (before
  Phase 4 a control channel made hundreds of rows a day).
- `preamble`, `data_header`, `ip_map`, `lrrp`, `grant`, `activity_update`, `protected_lc`,
  `capplus_rest_change`: one row per distinct payload per carrier per hour.
- `call` (voice/data call start from the LC: slot, src, tgt): one row per minute, so
  `behavioral_lsn_table` / `behavioral_lcn_keyup` can align calls with LSN lists and channel updates.
- `capmax_channel_update`: one row per **assignment** (TG, LCN, slot): updates less than 15 s apart
  extend it (`last_ts`), so `ts` is when the control channel first announced it. A row per time
  window would make every window edge look like a new call. `capmax_activity`: per hour.
- `SLCO Completed Block` and `DMR PDU Payload` lines (with `-Z`) are never stored: they correct or
  complete the event of the line DSD-FME printed before them, or are decoded into one of the kinds
  below; a CSBK nobody decodes is counted per FID/opcode (`csbk_opcodes` in the ingest report) and
  reported once with its bytes as an unrecognized pattern.
- Voice frames, IDLE, `SLCO NULL`, CRC errors are **never** stored as events; they only feed
  `observations` counters.

### 3.2 `csbk_events.kind` values and payloads

| kind | payload keys |
|---|---|
| `capplus_channel_status` | `fl, ts, rs, rest_lsn, block, lsn_states{lsn: "rest"|"idle"|id}, banks{one|two: {flag, desc, lsn_to_tg}}`. An id in the list is a talkgroup unless an Individual-addressed preamble/data header named it (DSD-FME prints `TGT` in Bank lines for group calls too) |
| `capplus_site` | `site, rest_lsn, rs` (from short LC) |
| `capplus_adjacent` | `sites[{site, rest_lsn}]` |
| `capplus_rest_change` | `rest_lsn, reason` (only with `-Z`) |
| `preamble` | `addressing, kind, src, tgt, rest_lsn` |
| `conplus_sys` | `role: control|traffic, net_id, site_id` |
| `conplus_adjacent` | `sites[int]` |
| `grant` | `variant, kind: group|private|data, tgt, src, lcn, ts, lpcn, freq_hz` |
| `tier3_sys_parms` | `source: c_aloha_sys_parms|slc_c_sys_parms|slc_p_sys_parms, model, net_id, site_id, sub_site, sys_code, cat, reg, csc` |
| `tier3_bcast` | `a_type, text, lpcn, apcn, rx_freq_hz, rx_base_hz, rx_step_hz, tx_freq_hz, online, cc_pri, ac_pri, sys_code, adj_model, adj_net_id, adj_site_id, adj_sub_site` |
| `xpt_site_status` | `free_lcn, sn, lcns[], lsn_states, lsn_lcn{lsn: lcn}` |
| `xpt_adjacent` | `sn, sites[{site, free}]` |
| `xpt_slco` | `free_lcn, pri_lcn, pri_hash` |
| `call` | `slot, src, tgt, group, capplus, txi, xpt, rest_lsn, free_lcn, desc` (from `SLOT n TGT= SRC= … Call`) |
| `activity_update` | `ts1, hash1, ts2, hash2` (Tier II fingerprint; stored per distinct payload per hour) |
| `moto_data_channel` | `opcode, bytes` (stored per distinct bytes[0:3] prefix per day) |
| `data_header` | `slot, addressing, delivery, src, tgt, response_requested` |
| `ip_map` | `role, radio, ip, port` |
| `lrrp` | `src, tgt, direction, lat, lon` |
| `protected_lc` | `slot, lc: protected|unknown, flco, fid` (lines with `(CRC ERR)` only feed the error counter) |
| `data_response` | `tgt, src, text` (`DATA RESP` acknowledgements) |
| `slco_other` | `text`, `opcode`, `data` (Caltta / unknown SLCO, per distinct text per day; data from its Completed Block) |
| `capmax_channel_update` | `mode: open|advantage, lcn, slot, tg` (CSBK FID 0x10 opcode 0x21/0x22 from `DMR PDU Payload`; Advantage mode carries 10-bit short TG ids) |
| `capmax_activity` | `tgt` (`SLCO Cap Max Activity Update`) |
| `capmax_slco` | `opcode: 15, data, printed_as` (SLCO 0xF on a stream that carried Tier III system parameters: DSD-FME prints it as a Cap+ site) |
| `unknown_csbk` | `text` (first 120 chars; one row per distinct text per day; what `ingest` reports as unrecognized CSBK) |

## 4. Evidence model

Each evidence **type** is a function `collect(store, window) -> list[Evidence]` that looks at the
last `window` of data and emits `(carrier_a, carrier_b, kind, raw_score, details)` tuples. Types
belong to a **class** that carries the config weight and decay constant.

| Type | Class | Default weight | Raw score | Link kind | What it needs |
|---|---|---|---|---|---|
| `explicit_neighbor` | explicit | 0.90 | 1.0 if both sites announce each other, 0.65 one-way | `neighbor_site` (between carriers mapped to those sites) | Linked Cap+ `Adjacent Sites`, Con+ `Adjacent Sites`, Tier III `Adjacent_Site`, XPT adjacent |
| `explicit_lcn_freq` | explicit | 0.95 | 1.0 | `same_site` | Tier III `Chan_Freq` / grants with absolute frequency naming this carrier |
| `explicit_same_site` | explicit | 0.70 | 1.0 if same site number **and** same system code/net, 0.85 when one side (a Tier III traffic channel) only knows Net/Site, 0.8 if same site number and same CC only (Cap+ has no system code), 0.9 for a shared XPT site | `same_site` | Cap+ `SLCO Site`, Con+ `Net/Site`, Tier III sys parms, XPT site |
| `explicit_same_system` | explicit | 0.80 | Tier III: 1.0 when the sites announce each other, 0.8 on the Net ID alone; Con+ 1.0 | `same_system` | Tier III: same Net ID and network model on different sites (the `SYS` code encodes the site, so it is NOT a network identity: field run 2026-10-30, net 1 site 1 = `2101`, site 2 = `2102`); Con+: same Net ID |
| `behavioral_rest_hop` | behavioral | 0.50 | per corroborated hop 0.25, saturating `1 - 0.75^n` | `same_site` | Cap+: rest LSN leaves carrier A at `t` and a rest LSN appears on carrier B within ±2 s with the same site number, repeatedly; also builds the LCN↔frequency table (`carrier_sites.lcn`) |
| `behavioral_lsn_table` | behavioral | 0.60 | per match 0.25, saturating `1 - 0.75^n` | `same_site` | Cap+ channel list on A shows a TG active on LSN *n*; carrier B carries that TG call within ±3 s → B is LCN `(n+1)//2` of A's site |
| `behavioral_lcn_keyup` | behavioral | 0.70 | `(1 − 0.6^n) × agreement`; per assignment at most one match per carrier: a call to the TG on the same slot during it = 1, else a key-up within ±3 s of its first update = ½ shared by all carriers that keyed up then | `same_site` | Cap Max: control channel A announces TG T on LCN L (channel update); carrier B carries the call to T, or keys up, at that moment → B is LCN L of A's site. Carriers whose own short LC names another site are never matched (a group call goes out on every site) |
| `identity_radio` | identity | 0.30 | `min(1, shared_radios / 5)` | `same_system` | the same radio ID seen as `src` on both carriers within 24 h |
| `identity_talkgroup` | identity | 0.15 | `min(1, shared_tgs / 3)` | `same_system` | the same TG on both carriers within 24 h (low: TG numbers are reused across unrelated systems) |
| `identity_slco` | identity | 0.20 | `min(1, shared_payloads / 3)` | `same_system` | the same short-LC content repeated ≥ 3 times on both carriers, same colour code: an Activity Update (slot activity + hashed address, from DSD-FME's line or from an old `SLCO Completed Block` row: both give one text), an SLCO nothing decodes (`slco <opcode> <data>`, old or new rows; Cap Max 0xF bits); all-idle updates and short LCs decoded explicitly elsewhere are left out, and so is an Activity Update whose hashes are those of talkgroups both carriers were heard carrying (already `identity_talkgroup`); a carrier's count of one content is the largest over the forms it was stored in (old databases hold the line and its block); content present on more than max(4, half) of the carriers counts as a vendor default. Field run 2026-10-30: two VHF Tier II repeaters shared two Activity Updates (the same hashed talkgroups) |
| `rf_profile` | rf | 0.10 | correlation of activity-per-bucket profiles (Pearson over ≥ 24 shared buckets, clipped to 0..1), only when colour codes match | `same_system` | observations |
| `manual` | manual | 1.00 | as given by `propose_link` (0..1) | any | MCP / CLI |

Tier II specific rule: a link whose evidence is only `identity_*` (radio, talkgroup, SLCO) and `rf_profile` is marked
`tier2_only = 1` and its score is capped at `tier2_max_confidence` (0.60). Such links can never be
labelled `confirmed`, and the HTML export draws them dashed.

Negative evidence: `explicit_same_site` with **different** site numbers on the same system, or
Tier III sys parms with a different `SYS`, produce an evidence row with `raw_score = 0` and type
suffix `_contradiction`; the combiner treats it as a multiplicative penalty (score × 0.2). Colour
code mismatch is *not* negative evidence (sites of one system commonly differ in CC) but it blocks
`rf_profile`.

### Time decay

```
decayed_i = weight_class × raw_i × exp(−age_days / decay_days_class)
age_days  = (now − evidence.observed_at) / 86400
```

`decay_days = 0` means no decay (manual evidence). Each `support_count` observation refreshes
`observed_at`, so a link that keeps being corroborated never decays.

## 5. Confidence combination

Evidence rows for a link are combined with noisy-OR, which rewards independent corroboration
without letting many weak hints add up to certainty:

```
positive = 1 − Π (1 − decayed_i)         over evidence with raw_score > 0
penalty  = 0.2 ^ (number of contradiction rows with decayed weight > 0.1)
score    = positive × penalty
if tier2_only: score = min(score, tier2_max_confidence)
```

Labels (lower bounds from config): `confirmed ≥ 0.85` **and at least one explicit or manual
evidence row**, `probable ≥ 0.60`, `possible ≥ 0.30`, otherwise `weak`. `weak` links are stored
(so `explain` can show why they are weak) but hidden from the default graph.

Worked example from the samples. Carriers A (168.863 MHz, Cap+ site 2) and B (hypothetical
second LCN of site 2):

- `explicit_same_site` (both `SLCO Capacity Plus Site: 2`, CC 02): 0.70 × 0.8 = 0.56
- `behavioral_rest_hop`, 6 corroborated hops: 0.50 × (1 − 0.75⁶ = 0.82) = 0.41
- `identity_radio`, 3 shared radios: 0.30 × 0.6 = 0.18

score = 1 − (0.44 × 0.59 × 0.82) = 0.79 → **probable**. A `Linked Cap+ Adjacent Sites` row that
names the neighbour site, once the neighbour's carriers are known, would push it to `confirmed`.

Systems and sites are derived after links: connected components of `same_site` links with
score ≥ `possible` become sites; `same_system` and `neighbor_site` links join sites into systems.
Explicit announcements (`site_number`, `sys_code`, `net_id`) label them. A site that is only
announced by a neighbour exists with `directly_observed = 0` and no carriers. `carrier_sites.lcn`
is the Cap+ LCN learned from rest hops / LSN lists (`LCN = (LSN+1)//2`) or the Tier III LPCN from a
`Chan_Freq` announcement. The tables are rebuilt on every `mapper correlate`; links and evidence
persist and keep decaying, and `source = 'manual'` evidence is never touched by the automatic pass.

Implementation (Phase 2): `dmr_mapper/correlate/` (`context.py` snapshot of carriers and their
announcements, `evidence/*.py` collectors, `scoring.py`, `engine.py`, `derive.py`) and
`dmr_mapper/export/` (`graph.py` NetworkX, `html.py` static map). Verified on synthetic
multi-carrier logs (`tests/synth.py`): a three-repeater Cap+ site with coordinated rest hops comes
out `probable` on explicit + behavioural evidence with the right LCNs; a Tier III control channel
links its traffic channel `confirmed` through `Chan_Freq` and its neighbour site through
bidirectional `Adjacent_Site`; two Tier II repeaters sharing six radios reach `possible` (capped,
Tier II only); an unrelated Cap+ network reusing site number 2 is not linked.

## 6. What each DMR variant gives us

The exact DSD-FME line formats are catalogued in `CAPTURE.md` §4 and are the basis of the Phase 1
parser. Summary of what the correlator can use:

| Variant | Site identity | System identity | Neighbours | Channel map |
|---|---|---|---|---|
| Tier II | — (CC + `Activity Update` hash pattern is a weak fingerprint) | — | — | — |
| Cap+ | `SLCO Capacity Plus Site: n` | — (site numbers are per-network; Linked Cap+ neighbours tie them) | `Capacity Plus Adjacent Sites` | LSN 1–16 per site, learned from rest hops; `LCN = (LSN+1)//2`, `slot = (LSN−1)%2+1` |
| Con+ | `Site ID` | `Net ID` | `Connect Plus Adjacent Sites` | LCN from grants; frequency only via observation |
| Tier III | `Site ID` + sub-site (DSD-FME prints site 2 as `1.3`; payloads keep the print, the correlator and every display use the on-air number, as SDRTrunk) | `Net ID` + model (the `SYS` code changes with the site) | `C_BCAST Adjacent_Site` with LPCN | `Chan_Freq`/APCN give absolute RX/TX frequency; otherwise ETSI TS 102 361-4 channel numbering requires a band plan from config |
| Capacity Max (Tier III, Motorola) | as Tier III | as Tier III | `Adjacent_Site` whose channel is the neighbour control channel's LCN (read from the bytes: DSD-FME omits it when "connection information" is not available) | channel updates (FID 0x10 opcode 0x21/0x22): TG per LCN and slot, matched with calls and key-ups (`behavioral_lcn_keyup`) |
| XPT | `XPT Adjacent Site:n` | — | `XPT Adjacent` | `Free LCN`, LSN status list |

Phase 1 result on `samples/`: 0 unrecognized lines on all 16 logs; the Cap+ capture yields 9 245
channel-status CSBKs (105 rows after de-duplication), 149 adjacent-site CSBKs (2 rows), 46
rest-channel transitions, 25 LRRP responses and 12 identities, in a 200 KB database.

**Tier III numbering.** `-D 0` does not do what its help text suggests: DSD-FME reads the value
as a character, so `0` is 48, clamped to 10. Every Tier III identity then prints as
`Net ID: net+1; Site ID: 1.site+1` (raw net 1 site 1 = `Net ID: 2; Site ID: 1.2`, SYS `2101`; this is
the offset against SDRTrunk; without `-D` the default DMRLA prints the same). But once DSD-FME has
decoded one Motorola (FID 0x10) Aloha or broadcast it switches the whole process to raw
`Net ID: net; Site ID: site`. Only the raw print lacks a sub-site, so the parser converts every
print without one (wherever a log starts), and with `-Z` takes the identities of Alohas, Adjacent_Site
and Vote_Now from the bytes (the 14-bit system identity code: 2 bits model, then net and site).
With `-Z` a broadcast is followed by ` SYS: <own>; Reg: …; BParms1: …` (the broadcasting site's own
code, stored as `own_sys_code`) and an Aloha by ` Ver: …; SF: …; Backoff: …`.
Payloads keep that one print (`Net ID: 2; Site ID: 1.2`); the correlator turns it into the on-air
numbers (`context.t3_on_air`: net 1, site 1, as SDRTrunk shows them), and systems, sites, anchors,
evidence details, the map and the console show only those. (Before Phase 4.1 sites were kept as
site×100+sub, which also broke for raw sites from 99 up.)

**RAS.** On a system with RAS the CSBK CRC carries a MAC: DSD-FME decodes on FEC alone (it marks the
burst `{RAS Skip}`), and so does the parser for the raw bytes (`pdu_ras` in the quality counters).

**Time zone.** DSD-FME stamps lines in the local time of the machine it runs on. The collector reads
its decoders in the system zone; log files given to `mapper ingest` are read in `timezone` from
config. `mapper doctor` warns when the two differ.

Only Cap+ (three sites) and Tier II are present in `samples/`. Con+, Tier III and XPT parsers are
written from the DSD-FME source print formats and unit-tested on synthetic lines; they are flagged
`untested_on_air` in the parser coverage report until a real capture exists.

## 7. Component interfaces

Package `dmr_mapper` (Python 3.11+, stdlib + `pyyaml`, `networkx`; `mcp` only for Phase 4;
`numpy`/`SoapySDR` only on the Pi for Phase 3).

```python
# dmr_mapper/ingest/dsdfme.py
@dataclass
class ParseContext:
    freq_hz: int | None          # carrier, from filename/CLI/collector
    start_time: datetime | None  # date source for HH:MM:SS lines
    source: str                  # dsdfme_log | surveyor | collector
    time_is_rf: bool             # False for surveyor decode logs

def parse_lines(lines: Iterable[str], ctx: ParseContext) -> Iterator[Event]
    # Streams. Strips ANSI. Keeps a small state machine for multi-line CSBKs
    # (Channel Status + LSN list, Adjacent Sites + Site list, LRRP + IP map).
    # Yields Burst, CsbkEvent, RestChange, Identity, Unrecognized events.

# dmr_mapper/ingest/surveyor.py
def scan(root: Path) -> Iterator[SurveyorAttempt]   # finds decode dirs, picks best inversion log
def read_candidates(root: Path) -> list[SurveyHit]  # candidates.csv / rf_observations if present

# dmr_mapper/ingest/writer.py
class Aggregator:
    def __init__(self, store: Store, bucket_minutes: int) ...
    def feed(self, event: Event) -> None   # updates in-memory buckets, dedup caches
    def flush(self) -> IngestStats         # writes observations / csbk_events / identities

# dmr_mapper/db/store.py
class Store:                                  # thin sqlite3 wrapper, migrations, upserts
    def open(path, create=True) ...
    def upsert_carrier(freq_hz, seen_at, **fields) -> int
    ...

# dmr_mapper/correlate/evidence/<type>.py
def collect(store: Store, window: TimeWindow, cfg: CorrelatorConfig) -> list[Evidence]

# dmr_mapper/correlate/engine.py
def run(store, cfg, now=None) -> CorrelateReport   # collect → upsert evidence → score links → derive sites/systems

# dmr_mapper/export/graph.py
def build(store, min_label="possible") -> networkx.Graph   # nodes: carriers (+ announced-only sites), edges: links
# dmr_mapper/export/html.py
def write(graph, path)                                      # single static HTML, inline SVG + JS, no CDN

# dmr_mapper/collector/   (Phase 3a)  state.py (queue), daemon.py, sdr.py (Soapy / file / synthetic), survey.py,
#                         channelizer.py, bridge.py (PCM→TCP), decoder.py, ingest.py, scheduler.py, power.py
# dmr_mapper/tui/app.py   (Phase 3a)  Textual console
# dmr_mapper/collector/watch.py (Phase 4)  plan_watch, KeyupDetector, InstancePlanner; Daemon._watch drives them
# dmr_mapper/ingest/rawpdu.py   (Phase 4)  CRCs, CSBK / short-LC bit fields from DSD-FME's -Z bytes
# dmr_mapper/correlate/evidence/capmax.py, correlate/names.py (Phase 4)
# dmr_mapper/mcp/server.py (Phase 5)
```

CLI (`mapper`, argparse):

```
mapper ingest <path> [--freq HZ] [--start ISO] [--source ...] [--stream] [--report]
mapper correlate [--window DAYS] [--now ISO]
mapper systems [--min-label possible] [--json]
mapper explain <link-id | freqA freqB>
mapper export [--html PATH] [--graphml PATH] [--min-label possible]
mapper daemon | tui | survey | decode | stop | doctor | capture-iq   (Phase 3a)
mapper watch [FREQ ...] [--center HZ] [--minutes N] | name system|site TARGET NAME [--clear] | names   (Phase 4)
mapper serve-mcp (Phase 5)
```

## 8. Configuration

`config.example.yaml` is committed; `config.local.yaml` (git-ignored) is deep-merged over it.
Lookup order: `--config PATH`, `$DMR_MAPPER_CONFIG`, `./config.local.yaml`, `./config.example.yaml`.
Keys: `paths`, `timezone`, `sdr`, `survey.bands[]`, `survey.*`, `decode.*`, `watch.*`, `ingest.bucket_minutes`,
`correlator.{weights,decay_days,labels,tier2_max_confidence,interval_minutes,window_days}`, `archive.*`, `mcp.*`.
See the example file for every key and its meaning. Machine-specific values (Tailscale address,
device serials, archive host, band plans) are only ever set in `config.local.yaml`.

## 9. Storage budget

Assumptions: 50 active carriers, 15-minute buckets, sequential survey so each carrier is decoded a
few minutes per hour, 90 days.

| Table | Rows / day | Bytes / row | 90 days |
|---|---|---|---|
| observations | ≤ 50 × 96 = 4 800, realistically ~1 500 (only observed buckets) | ~120 | ~16 MB worst, ~5 MB typical |
| rest_events | 3 Cap+ sites × ~2/min × observed fraction (~10 %) ≈ 900 | ~40 | ~3 MB |
| csbk_events (deduped) | ~500 | ~200 | ~9 MB |
| identity_sightings | ~300 | ~40 | ~1 MB |
| evidence + links | recomputed in place | — | < 1 MB |
| survey_hits | 2 bands × 48 runs × ~60 hits ≈ 5 800 | ~50 | ~26 MB → prune to last 14 days (~4 MB) |

Total ≈ **20–35 MB for 90 days** with `survey_hits` pruning (after every session); SQLite reuses the freed pages. Raw DSD-FME logs
are kept only when `decode.keep_raw_logs` is true, with rotation `raw_log_max_mb × raw_log_keep`
(default 250 MB cap), compressed, named like manual captures so `mapper ingest` replays them. Every night
(or at the next boot) a timer writes a consistent `VACUUM INTO` snapshot, checked with `quick_check`,
to `var/snapshots/` (newest `archive.keep_local` kept on the SD card); the archive Pi **pulls** that
folder with a key restricted to read-only rsync of it, and keeps 14 daily + 8 weekly copies.

## 10. MCP tools (Phase 5)

Transport: streamable HTTP (`mcp` Python SDK), bound to the Pi's Tailscale address only, no
token (the tailnet is the trust boundary). All tools return JSON; long operations return a job id
that `get_status` reports on.

| Tool | Signature | Returns |
|---|---|---|
| `scan_band` | `(band: str \| None, start_hz: int \| None, stop_hz: int \| None, dwell_s: float = 3)` | `{run_id, hits: [{freq_hz, snr_db, occupancy_pct, class}], duration_s}` |
| `list_carriers` | `(band: str \| None = None, since_hours: int = 24, min_snr_db: float \| None = None, variant: str \| None = None)` | `[{freq_hz, variant, dominant_cc, last_seen, activity_summary, site, system}]` |
| `decode_channel` | `(freq_hz: int, seconds: int = 60)` | fingerprint summary of a fresh decode: bursts, CSBK kinds seen, site/system IDs, identities, unrecognized lines |
| `get_fingerprint` | `(freq_hz: int, days: int = 7)` | everything the DB knows about the carrier: variant, CC, site, LCN, neighbours, identities, activity profile |
| `query_systems` | `(system_id: int \| None = None, freq_hz: int \| None = None, min_confidence: float = 0.3)` | systems → sites → carriers with link scores and labels |
| `explain_link` | `(link_id: int \| None, a_freq_hz: int \| None, b_freq_hz: int \| None)` | evidence rows with raw, weight, decay, contribution |
| `propose_link` | `(a_freq_hz: int, b_freq_hz: int, kind: str, confidence: float, note: str)` | writes a `manual` evidence row, reruns scoring for that pair, returns the new link |
| `correlate_now` | `()` | `CorrelateReport` |
| `get_status` | `()` | collector state, current decode queue, DB size, last survey, last archive sync |

Guard rails: `propose_link` and `correlate_now` are the only writers; `decode_channel` and
`scan_band` preempt the collector's queue for the requested duration and then hand the tuner back.

`.mcp.json` on the laptop (Phase 5):

```json
{ "mcpServers": { "dmr-mapper": { "type": "http", "url": "http://<pi-tailscale-ip>:8765/mcp" } } }
```

## 11. Repository layout

```
dmr_mapper/            package (phases 1–4)
  cli.py  config.py
  db/        store.py  migrations/
  ingest/    dsdfme.py  surveyor.py  writer.py  events.py
  correlate/ engine.py  evidence/*.py  derive.py
  export/    graph.py  html.py
  collector/ (Phase 3a; watch.py Phase 4)  tui/ (Phase 3a)  mcp/ (Phase 5)
tests/                 pytest, fixtures from samples/ + tests/fixtures/synthetic/
samples/               checked-in DSD-FME logs (see README)
deploy/                systemd unit templates, deploy.sh, archive-pull.sh (Phase 3b)
DESIGN.md  CAPTURE.md  README.md  config.example.yaml  pyproject.toml
```

## 12. Open points and known limits

- **No rf evidence fixture.** `samples/surveyor` has no candidate list with SNR. `rf_profile`
  is implemented against `observations` and tested synthetically; its weight is deliberately low.
- **Con+ / Tier III / Capacity Max / XPT untested on air.** Parsers follow the DSD-FME source
  formats (and SDRTrunk's bit layouts for the Motorola messages); the ingest report labels their
  counts `untested_on_air` until a real log is added to `samples/`. A 5-minute raw log of the
  469 MHz control channels (`decode.keep_raw_logs: true`) is the next thing to check them against;
  `csbk_opcodes` in its ingest report will show which Motorola opcodes the network really sends.
- **The Capacity Max CSBK CRC is keyed** (raw log of 2026-10-31, `samples/capmax/`). Every CSBK on
  both control channels is flagged `(CRC ERR)` and DSD-FME prints nothing for it, yet the same
  bytes repeat exactly and the fields are right: the Aloha carries SYS 2102 on the site-2 control
  channel, the Adjacent_Site broadcasts name site 3 on LCN 5 (as SDRTrunk showed) and site 1 on
  LCN 2. No CRC-16 variant (any polynomial, init, bit order) fits: it is a key, like RAS without
  DSD-FME's RAS mark. A CSBK whose CRC fails is therefore trusted when its FEC passed and either
  the same bytes came before or the stream is clearly keyed (30 CSBKs, at least 90 % failing);
  Aloha, neighbours, channel updates and grants are then read from the bytes (`pdu_keyed_crc`
  in the quality counters, `from_bytes` in the payloads). A burst with `(FEC ERR)` is never used.
  Seen in 5 minutes: Aloha (FID 0x00 and 0x10), C_BCAST, Cap Max channel updates (0x21/FID 0x10),
  data and voice grants, C_ACKD.
- **Activity Update hash** (`rawpdu.address_hash`): CRC-8 of the 16 least significant address
  bits, as DSD-FME computes the Hytera XPT hash; not verified on air.
- **Advantage mode** channel updates carry 10-bit short talkgroup ids, not the ones calls show:
  there only key-ups (half matches) teach LCNs. Which mode the 469 MHz network runs is not known.
- **Names** follow the network identity the announcing carriers give (Tier III model + Net ID,
  Connect Plus net), whatever the other members are; when no system has it now, the carriers. A
  system or site with neither cannot be named. Ids change with every correlator run (the daemon
  runs one after each decode or watch session), so `mapper name` is best given a frequency.
- **Retention**: unrecognized-line examples (raw lines, raw CSBK bytes) are reduced to their
  digit-masked pattern after `retention.identity_days`, like the identities they may contain.
- **Cap+ site numbers are not globally unique.** Two unrelated Cap+ networks can both say
  `Site: 2`. `explicit_same_site` for Cap+ therefore requires the same colour code and a
  frequency distance below `survey.same_site_max_span_hz` (default 5 MHz), and reaches
  `confirmed` only together with behavioural or Linked Cap+ neighbour evidence.
- **Stray Capacity Plus lines on Tier III carriers.** On the Pi, DSD-FME printed
  `SLCO Capacity Plus Site: 0 - Rest LSN: 0` on Cap Max carriers. Cap+ site numbers and Cap+
  neighbours count only on carriers classified as Cap+.
- **Carriers without DMR** (analogue, another protocol, too weak) stay in `carriers` with variant
  `unknown`; after 30 s of decoding with no DMR the scheduler leaves them out of a round until they
  are stale.
- **Capacity Max, from SDRTrunk logs of the same network (2026-10-30).** SDRTrunk printed
  `ALOHA LARGE NETWORK:n SITE:m`, `LARGE NEIGHBOR NETWORK:n SITE:m LCN:l` (the standard
  Adjacent_Site broadcast, whose channel field is an LCN on Capacity Max) and channel updates with
  LCNs (`CHANID = 2 × LCN + slot`). Its numbering is the on-air one, which the mapper now shows too
  (net 1 site 1 = DSD-FME's net 2 site 1.2, SYS 2101; see "Tier III numbering" in §6). Each control
  channel announced a neighbour site 3 (DSD-FME: 1.4) on LCN 5 whose frequency is not known yet; it now appears as an announced
  site with that LCN. Traffic channels transmit only during calls: `mapper watch` (Phase 4).
- **`SLCO Completed Block [..]`** is not undecoded content: with `-Z` DSD-FME prints it after
  *every* short LC it decoded (opcode, 24 data bits, CRC-8, 4 pad bits), right after the decoded
  line. Since Phase 4 it completes that line's event instead of being stored on its own; old
  `slco_other` rows holding it are still read by `identity_slco`. Opcode 0xF on a Tier III carrier
  is a Motorola message DSD-FME prints as a Cap+ site: it is kept as `capmax_slco` with its bits.
- **Watch sessions and the decode planner.** A traffic channel found by watching is a carrier like
  any other, and a decode session may still plan 60 s on it while it is silent (it then scores
  "no DMR" and is revisited rarely). Watching is the way to hear it.
- **Tier III channel numbering** without `Chan_Freq` needs a band plan (`survey.tier3_bandplans`)
  to convert LPCN to frequency; absent that, LPCN is stored and the link stays at `possible`.
- **Sequential survey blind spots.** A rest hop whose destination carrier was not being decoded
  at that moment is simply not corroborated; behavioural evidence accumulates slowly and that is
  expected. A wideband channelizer is a later optimisation.
- **Git history** still contains the removed sample binaries (~24 MB). Rewriting history is the
  owner's call; the working tree is 6.7 MB.
