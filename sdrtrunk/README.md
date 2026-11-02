# SDRTrunk playlists

SDRTrunk is a manual companion to the mapper: it follows trunking and plays audio, which the
mapper does not. The mapper tells what to put in the playlist (control channels, LCNs, sites);
SDRTrunk's logs in turn confirm LCNs and show calls. The playlist holds the local band plan, so:

- `playlist.example.xml` (committed): a template with placeholder frequencies, one channel of each kind.
- `playlist.local.xml` (git-ignored): the real playlist. Copy the template, fill it, and add it in
  SDRTrunk's playlist manager (or copy its `<channel>` blocks into the playlist in use).
- SDRTrunk's event logs and recordings are never committed (`event_logs/` and `*.wav` are ignored):
  they hold radio ids and LRRP positions, which the mapper deliberately does not keep.

## What matters in the XML (checked against SDRTrunk's source, v0.6.1 and master)

- **The DMR timeslot map's `lsn` attribute holds the LCN.** `TimeslotFrequency` says so: "lsn
  (logical slot number) is a legacy name for this field. The field currently holds the LCN".
  Grants are looked up by LCN, so there is one entry per repeater, not one per timeslot.
- **Tier III / Capacity Max:** one channel per site on its control channel, each with the full map;
  the LCN plan is network-wide. A grant to an LCN that is not in the map is logged as
  `CHANNEL GRANT - NO FREQUENCY - CHECK PLAYLIST CHANNEL CONFIG LSN CHANNEL MAP`: a repeater still to
  find. Without any map every grant looks like that (the 2026-10-30/31 logs of the 469 MHz network)
  and no traffic channel is followed.
- **Capacity Plus:** per-site numbering (LCN 1 = LSN 1/2, LCN 2 = LSN 3/4). The source is
  `sourceConfigTunerMultipleFrequency` with every repeater of the site, so SDRTrunk can hunt the rest
  channel when it moves. Only the rest repeater sends idle beacons, so the `REST:n` of the idle
  `SITE STATUS` heard on a frequency tells which LSNs that frequency carries.
- **Conventional DMR:** no map.
- **`ignore_crc="false"`:** SDRTrunk learns keyed CRC masks by itself (`DMRCrcMaskManager`): the
  Capacity Max CSBKs that fail DSD-FME's CRC pass in SDRTrunk. Messages whose mask it has not learned
  yet show `[CRC-ERROR]` with consistent fields; random colour codes and LCNs mark real noise.
- **Not DMR:** `decodeConfigNBFM` with `<record_configuration><recorder>BASEBAND</recorder>` writes a
  stereo 16-bit I/Q WAV at the channel rate (50 kHz), about 12 MB a minute.
- **XML:** lists are not wrapped (`<frequency>`, `<timeslot>`, `<recorder>` repeat directly).
  Comments are allowed but SDRTrunk drops them on save, and must not contain two hyphens in a row.

## Tuners

One SDR covers a window of its sample rate; SDRTrunk gives each channel a tuner that covers it.
On the 469 MHz network: a window around 469.4 MHz holds sites 1 and 2 of the Tier III network,
site 2 of the Capacity Plus network and 469.1; a second one around 457.5 MHz (2.4 MS/s is
enough) holds site 3, Capacity Plus sites 1 and 3 and the CC 10 repeaters. VHF needs its own.
DESIGN.md §12 has the field results.
