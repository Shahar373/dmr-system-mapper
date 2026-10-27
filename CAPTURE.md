# CAPTURE.md — running DSD-FME so the logs contain everything the mapper needs

This is the exact way to run DSD-FME, on the Pi and for manual captures, so that Phase 1's parser
gets every field the correlator uses. The flag set was checked against the help text of the build
installed on your machine (`AW 2026-26`, captured in
`samples/surveyor/.../decoder_report_normal.json`).

## 1. Facts about DSD-FME output that shape everything below

1. **All decode text goes to stderr.** stdout stays empty (that is why every
   `dsd_fme_*_stdout.log` in `samples/surveyor` is 0 bytes). Always redirect `2>` or `2>&1`.
2. **Lines carry `HH:MM:SS` only, no date, and never the frequency.** The log file name (or the
   collector) must supply both: `dsd_fme_<freqHz>_<YYYYmmdd_HHMMSS>.log`.
3. **Do not use `-N` (ncurses) in a pipeline.** The ncurses screen repaints get interleaved with
   the stream and glue fragments onto log lines (the Cap+ monitor's parser had to work around
   `...09:04:59 Sync:` residue). Plain stderr output is clean.
4. Output is coloured with ANSI escapes (`\x1b[33m`, `\x1b[0m`). The parser strips them; there is
   no flag to disable colour, so do not try to filter them in the shell.
5. Multi-line records exist: `CSBK` header line, then one or more indented payload lines
   (`Capacity Plus Channel Status …` + `LSN 01: …`, `Capacity Plus Adjacent Sites` + `Site: …`,
   `SRC(24)… / DST(24)… / UDP … / LRRP …`). Keep lines in order; do not sort or dedupe a log.

## 2. The collector command (Pi, Phase 3)

One DSD-FME process per carrier being decoded, fed 48 kHz mono PCM over TCP by the collector's
channelizer bridge, parsed as a stream, raw text discarded unless debugging:

```sh
dsd-fme -fs -i tcp:127.0.0.1:7355 -o null -Z -D 0 2>&1 \
  | mapper ingest - --stream --source collector --freq 168863000
```

| Flag | Why |
|---|---|
| `-fs` | DMR TDMA BS/MS simplex decoder: the right mode for passive monitoring of a single carrier (both slots). Not `-ft`, which adds P25 and auto-detection noise. |
| `-i tcp:127.0.0.1:<port>` | PCM input from the collector's channelizer (the collector is the TCP *server*; DSD-FME connects as a client, same as SDR++ / GQRX mode). Ports `decode.tcp_base_port + instance`. |
| `-o null` | No audio output; no PulseAudio needed on the headless Pi. |
| `-Z` | "Log MBE/PDU payloads". Adds lines the parser wants: Tier III `SYS: XXXX; Reg: …` system code block after `C_ALOHA`/`C_BCAST`, XPT `Free LCN n;` on voice LC, `Cap+ Rest LSN Change: …`. Costs a few extra lines per CSBK, nothing else. |
| `-D 0` | Tier III: show the full Site ID instead of area/sub-area split, so site numbers are comparable across carriers. Harmless on other variants. |
| `2>&1` | Everything DSD-FME prints is on stderr. |

Not used, and why:

| Flag | Reason |
|---|---|
| `-N` | ncurses; corrupts the stream (see §1). |
| `-T`, `-C`, `-G`, `-U`, `-Y` | trunk-following and scanning. The mapper is passive and keeps each DSD-FME on one carrier; the collector does the tuning. Trunk-following would also make DSD-FME's own frequency guesses appear in the log, which the correlator must not take as evidence. |
| `-F` | relaxes CRC checks. It admits corrupt CSBKs, which would create false site/neighbour evidence. Keep CRC strict; the parser counts `(CRC ERR)` lines as quality data instead. |
| `-J`, `-L` | event log and LRRP file. The parser extracts the same information from the console stream; two more files per instance would only cost SD-card writes. |
| `-P`, `-7`, `-w`, `-6` | per-call / raw audio recording. Never on the Pi (storage rule). |
| `-xr` | inverted polarity. Only if a carrier shows `Sync: -DMR` dominance; the collector tries normal first and retries inverted when `Sync: +DMR` count is 0 after 10 s. |

Debug mode (`decode.keep_raw_logs: true`) uses `tee` into the rotation directory:

```sh
dsd-fme -fs -i tcp:127.0.0.1:7355 -o null -Z -D 0 2>&1 \
  | tee -a var/raw/dsd_fme_168863000_20260509_163344.log \
  | mapper ingest - --stream --source collector --freq 168863000
```

The collector rotates `var/raw/*.log` at `decode.raw_log_max_mb` and keeps `decode.raw_log_keep`
files (gzip), nothing else.

## 3. Manual captures (laptop with SDRangel / SDR++ / GQRX, or a Pi test)

Audio from the SDR application on TCP 7355 (SDR++ "network sink", GQRX UDP→TCP bridge, SDRangel
"Audio output → TCP"), 48 kHz mono:

```sh
dsd-fme -fs -i tcp -o null -Z -D 0 \
  2> dsd_fme_168863000_$(date +%Y%m%d_%H%M%S).log
```

PulseAudio loopback (as the Cap+ monitor does with a null sink called `dmr_capture`):

```sh
dsd-fme -fs -i pulse:dmr_capture.monitor -o null -Z -D 0 \
  2> dsd_fme_168863000_$(date +%Y%m%d_%H%M%S).log
```

A recorded discriminator WAV (48 kHz mono, as the surveyor produces):

```sh
dsd-fme -fs -i discriminator.wav -o null -Z -D 0 2> dsd_fme_164537500_20260713_150256.log
# DSDPlus 96 kHz files: add -s 96000
```

Then on any machine: `mapper ingest dsd_fme_168863000_20260509_163344.log` (frequency and start
time are taken from the name; override with `--freq` / `--start`).

**File naming rule:** `dsd_fme_<frequency in Hz>_<YYYYmmdd>_<HHMMSS>.log`. The existing sample
`dsd_fme_168863_20260509_163344.log` uses kHz; the parser accepts both (a value below 1 000 000 is
treated as kHz) but new captures should use Hz.

## 4. Lines the parser relies on (DSD-FME print formats)

Formats below were taken from the DSD-FME source (`dmr_csbk.c`, `dmr_flco.c`, `dmr_bs.c`) and
verified against `samples/` where marked ✔. Unmarked formats are not in the samples yet.

**Burst header (every frame)** ✔
```
16:33:44 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK
16:33:44 Sync: +DMR   slot1  [slot2] | Color Code=02 | VC1*
19:58:29 Sync: +DMR  [SLOT1]  slot2  | Color Code=XX | VC1*
16:33:44 Sync: +DMR  [slot1]  slot2  | CACH/Burst FEC ERR
Sync: no sync
```
Burst types seen: `CSBK`, `VLC`, `TLC`, `VC1`–`VC6`, `VC1*`, `IDLE`, `DATA`, `R12U`, `R34U`,
`R34C`, `R_1U`, `_UNK`, each optionally followed by `(CRC ERR)`. Brackets mark the active slot.

**Capacity Plus** ✔
```
 Capacity Plus Channel Status - FL: 3 TS: 1 RS: 0 - Rest LSN: 5 - Single Block
 Bank One F80 Private or Data Call(s) -  LSN 05: TGT 64250;
  LSN 01:  Idle;  LSN 02:  Idle;  LSN 03:  Idle;  LSN 04:  Idle;
  LSN 05: 64250;  LSN 06:  Rest;  LSN 07:  Idle;  LSN 08:  Idle;
 SLCO Capacity Plus Site: 2 - Rest LSN: 5 - RS: 00
 Capacity Plus Adjacent Sites
  Site: 1 Rest: 3;
 Preamble CSBK - Individual Data - Source: 199 - Target: 64250 - Rest LSN: 6
 SLOT 2 TGT=1 SRC=2102 Cap+ Group Call  Rest LSN: 5
 SLOT 2 TGT=2102 SRC=64250 Group Call
 SLOT 2 TGT=1 SRC=2102 Group TXI Call
 Moto Data Channel: 29; 04 00 00 3B 08 FC 00 00
 Cap+ Rest LSN Change: 5; Slot 1 Free; Slot 2 Free; Go To Rest LSN;      (with -Z)
```

**Connect Plus**
```
 SLCO Connect Plus Control Channel - Net ID: 3 Site ID: 12
 SLCO Connect Plus Traffic Channel - Net ID: 3 Site ID: 12
 Connect Plus Adjacent Sites: 11; 13; 14;
 Connect Plus Adjacent Sites: None Listed;
 Connect Plus Group Voice Channel Grant; Target: 101; Source: 2001; LCN: 4; TS: 2;
 Connect Plus Private Voice Channel Grant; Target: 2002; Source: 2001; LCN: 3; TS: 1;
 Connect Plus Data Channel Grant; Target: 2002; LCN: 5; TS: 1;
 Connect Plus Slot Termination;
```

**Tier III**
```
 C_ALOHA_SYS_PARMS: Small; Net ID: 1; Site ID: 7; SYS: 1A2B;
 C_ALOHA_SYS_PARMS: Large; Net ID: 2; Site ID: 3.4; Cat: Voice; SYS: 1A2B;
 SLC_C_SYS_PARMS: Small; Net ID: 1; Site ID: 7; Reg Req: 1;
 SLC_P_SYS_PARMS: Small; Net ID: 1; Site ID: 7;
 Announcements (C_BCAST) Adjacent Site Information (Adjacent_Site)
  Online; CC Pri: 1; AC Pri: 2; LPCN: 1234;
  Small; Net ID: 1; Site ID: 8; SYS: 1A2B;
 Announcements (C_BCAST) Announce Logical Channel/Frequency Relationship (Chan_Freq)
 APCN: 0410; RX Base: 160000000; RX Step: 12500; RX Freq: 165125000;
             TX Base: 165000000; TX Step: 12500; TX Freq: 170125000;
 Announcements (C_BCAST) General Site Parameters (Gen_Site_Params)
 Announcements (C_BCAST) Vote Now Advice (Vote_Now)
 Talkgroup Voice Channel Grant (TV_GRANT)
  LPCN: 0012; TS: 1; LPCN+TS: 0023; Target: 00000101 - Source: 00002001
  Frequency: 165.125000 MHz                                                   (only with -C channel map)
  SYS: 1A2B; Reg: 1; Backoff: 3; BParms1: 0000; BParms2: 000000;              (with -Z)
```

**Hytera XPT**
```
 Hytera XPT Site Status - Free LCN: 3 SN: 1
 LCN 1 - LSN 01: Idle; LSN 02: Idle;  LCN 2 - LSN 03: Priv; LSN 04: Null;
 Hytera XPT CSBK 0x0B - SN: 1 XPT Adjacent Site:2 Free:1; Site:3 Free:4;
 SLCO Hytera XPT - Free LCN 3 - PRI LCN 1 - PRI HASH: 2A
 SLOT 1 TGT=101 SRC=2001 Hytera XPT Group Call Free LCN 3;                     (Free LCN with -Z)
```

**Common / Tier II** ✔
```
 SLCO NULL
 SLCO CRC ERR
 Activity Update TS1: Group Voice; Hash: 7; TS2: Idle; Hash: 0;
 Activity Update TS1: Idle; Hash: 0; TS2: Ind Data; Hash: 73;
 SLOT 1 Protected LC  FLCO=0x3F FID=0x7D
 SLOT 2 Unknown LC  FLCO=0x28 FID=0x80  (CRC ERR)
 Protected Control Signalling Block(s) (CRC ERR)
 Slot 2 Data Header - Indiv - Unconfirmed Delivery - Source: 199 Target: 64250
 SRC(24): 00000199; IP: 012.000.000.199; Port: 4001;
 DST(24): 00064250; IP: 013.000.250.250; Port: 4001;
 LRRP SRC: 199; Response to TGT: 64250;
 Lat: 32.101282 Lon: 34.871515                                                 (when a position is decoded)
 TXI Op: 1 -BR Delay: Irrelevant / Send at any time;
```

Anything not matched is counted and reported by `mapper ingest --report` as an unrecognized
pattern (digits replaced by `#`), so new DSD-FME builds show up as a short list rather than silent
loss.

## 5. Quick self-check of a capture

```sh
sed 's/\x1b\[[0-9;]*m//g' dsd_fme_*.log | grep -c 'Sync: +DMR'          # bursts decoded
sed 's/\x1b\[[0-9;]*m//g' dsd_fme_*.log | grep -o 'Color Code=[0-9X]*' | sort | uniq -c
sed 's/\x1b\[[0-9;]*m//g' dsd_fme_*.log | grep -E 'Site|Net ID|SYS:|Adjacent|Rest LSN' | sort | uniq -c | sort -rn | head
```

A good 60-second decode of a busy carrier has hundreds of `Sync: +DMR` lines and one dominant
colour code. Zero syncs with `-fs` → try `-xr`; many `CACH/Burst FEC ERR` → the channel filter or
level is off (the collector's channelizer uses the surveyor's 7.5 kHz low-pass and 48 kHz output).
