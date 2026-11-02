# afsk469: the 469.1 MHz telemetry link

A standalone research tool, not part of the mapper (`dmr_mapper` never imports it). It decodes the
carrier on 469.1 MHz that the mapper lists as "not DMR" to frames of bits, so that longer
recordings can be studied. DSD-FME still does all DMR decoding; this signal is not DMR.

## What the signal is

Worked out on 2026-11-02 from a 65 s SDRTrunk baseband recording (18 bursts, all decoded):

| Layer | Finding |
|---|---|
| Carrier | analogue NBFM, about 6 kHz occupied |
| Modulation | audio FSK, tones 902 and 1503 Hz (5:3), 600 baud, continuous phase (1.5 or 2.5 cycles a symbol) |
| Line code | the bit is the duration of each tone segment, not the tone: 1 symbol = 0, 2 symbols = 1. Self-clocking (every tone change is a clock edge) and polarity-free: successive copies of a frame come out with the tones swapped |
| Framing | a steady leader tone (about 40 ms), then frames separated by a 4-symbol segment |
| Frames | 64 or 32 bits, repeated 3 to 8 times per burst. Even parity in every frame seen. No BCH(31,21) (POCSAG) and no linear CRC: the GF(2) gcd of the differences between frames is trivial |
| Fixed bits | in 64-bit frames bits 15, 16, 27, 29-31 are 0 and bits 44-47 are `0110` |
| Traffic | a strong central station (about -1.06 kHz carrier offset) and weaker outstations (near 0 Hz). An outstation answers with the same bytes 2-3 (an address field, apparently) as the frame before it. At least four addresses were polled in about 25-30 s, plus a 32-bit frame every 20-25 s |

Not MDC1200, Fleetsync, Tait 1200, MPT1327 FFSK or Bell 202 (all 1200 baud with other tones), so
SDRTrunk's aux decoders show nothing. The vendor and protocol name are not known.

Not known yet: the meaning of the fields and how the last bytes are computed. Two frames with the
same address differed only in byte 4 and in the last two bytes. A recording of 20-30 minutes
gives enough pairs to study that.

## Recording

- SDRTrunk: an NBFM channel on 469.1 MHz with baseband recording (`<recorder>BASEBAND</recorder>`,
  see `sdrtrunk/README.md`). The file is a stereo 16-bit I/Q WAV at the channel rate (50 kHz):
  about 12 MB a minute.
- Or FM-demodulated audio as a mono WAV, 8 kHz or more, unsquelched or squelched (SDR++, SDRangel).
  About 1 MB a minute at 8 kHz. Not MP3.

Recordings stay on the machine that made them; they are never committed (`.gitignore`: `*.wav`).

## Use

Needs Python 3.11+ and numpy.

```sh
python tools/afsk469/afsk469.py 20261102_202211_469100000_baseband.wav --csv frames.csv
```

One line per burst on stdout: time (from the SDRTrunk file name when it has one), duration, level,
carrier offset, the two tones measured, the number of identical copies, the bit count and the frame
in hex. Then a summary: parity, the address field of the 64-bit frames, the 32-bit frames.
`--verbose` also lists the less frequent frames of a burst; `--tones` changes the tone pair used to
find bursts in audio files.

The CSV (`file, t_s, time, len_s, level_db, level_kind, offset_hz, tones_hz, frames, valid, copies,
bits, hex, parity, other_lengths`) is a few KB and is what to share for analysis, not the WAV.

## Test

`tests/test_afsk469.py` builds bursts with this coding, FM-modulates them to a 50 kHz baseband
(and writes them as plain audio at 8 kHz with noise between bursts) and checks that the tool
returns the same frames, copy counts, parity and file-name time.
