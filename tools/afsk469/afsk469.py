#!/usr/bin/env python3
"""Decode the 469.1 MHz telemetry link to frames of bits.

What the signal is (worked out from an SDRTrunk baseband recording, 2026-11-02):

* analogue NBFM carrying audio FSK: two tones near 900 and 1500 Hz, 600 baud, continuous phase;
* the bit is the duration of each tone segment, not the tone: 1 symbol = 0, 2 symbols = 1;
  a segment of 4 symbols separates frames, a long steady tone (about 40 ms) leads each burst;
* a frame is 64 or 32 bits and is repeated 3 to 8 times per burst (successive copies come out
  with the tones swapped, which does not change the decoded bits).

Input: WAV files.
* stereo 16-bit = complex baseband I/Q (SDRTrunk "baseband" recording of the channel);
* mono = audio that is already FM-demodulated (any sample rate from 8 kHz up; no MP3).

Output: one line per burst, and with --csv a small file that can be shared instead of the
recording. Only numpy is needed. Nothing here touches DMR: this is a research tool, kept out of
the mapper (README.md next to this file; tests/test_afsk469.py checks it on a synthetic signal).

    python afsk469.py recording.wav [more.wav ...] [--csv frames.csv] [--verbose]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import wave
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

BAUD = 600.0


# -- small DSP helpers -------------------------------------------------------------------------
def lowpass(sig: np.ndarray, fc: float, fs: float, taps: int = 301) -> np.ndarray:
    taps = min(taps, max(31, (len(sig) // 2) * 2 - 1))
    t = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * fc / fs * t) * np.hamming(taps)
    return np.convolve(sig, h / h.sum(), mode="same")


def fm_demod(iq: np.ndarray, fs: float) -> np.ndarray:
    y = lowpass(iq, 5000, fs)
    f = np.diff(np.unwrap(np.angle(y))) * fs / (2 * np.pi)
    return f


def two_tones(audio: np.ndarray, fs: float) -> tuple[float, float]:
    """The two strongest tones between 600 and 2000 Hz, low first."""
    a = audio - audio.mean()
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a))))
    f = np.fft.rfftfreq(len(a), 1 / fs)
    m = (f > 600) & (f < 2000)
    fm, sm = f[m], spec[m]
    first = fm[np.argmax(sm)]
    sm2 = np.where(np.abs(fm - first) < 250, 0, sm)
    second = fm[np.argmax(sm2)]
    lo, hi = sorted((float(first), float(second)))
    return lo, hi


# -- reading -----------------------------------------------------------------------------------
class Source:
    def __init__(self, path: Path):
        self.path = path
        self.w = wave.open(str(path), "rb")
        if self.w.getsampwidth() != 2:
            raise SystemExit(f"{path}: only 16-bit WAV is supported")
        self.fs = float(self.w.getframerate())
        self.ch = self.w.getnchannels()
        self.n = self.w.getnframes()
        self.iq = self.ch == 2
        self.tones = (900.0, 1500.0)
        m = re.match(r"(\d{8})_(\d{6})", path.name)
        self.start = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None

    def read(self, a: int, b: int) -> np.ndarray:
        a, b = max(0, a), min(self.n, b)
        self.w.setpos(a)
        raw = np.frombuffer(self.w.readframes(b - a), dtype=np.int16).astype(np.float64) / 32768
        if self.iq:
            raw = raw.reshape(-1, 2)
            return raw[:, 0] + 1j * raw[:, 1]
        return raw.reshape(-1, self.ch)[:, 0]

    def activity(self, block_s: float = 0.01) -> tuple[np.ndarray, np.ndarray]:
        """Per 10 ms over the whole file, read in chunks: (on/off, level in dB).

        I/Q: RF power over the noise floor. Audio: an unsquelched FM receiver is loud on noise, so
        energy cannot tell the signal from noise there; instead the share of the energy in two narrow
        bands around the tones (``self.tones``) counts, and the level is that share in dB."""
        blk = int(self.fs * block_s)
        vals = []
        chunk = blk * 1000
        for a in range(0, self.n, chunk):
            x = self.read(a, a + chunk)
            k = len(x) // blk
            if k == 0:
                break
            x = x[: k * blk].reshape(k, blk)
            if self.iq:
                vals.append(10 * np.log10(np.mean(np.abs(x) ** 2, axis=1) + 1e-15))
            else:
                n = max(blk, int(self.fs * 0.02))
                xx = np.zeros((k, n)); xx[:, :blk] = x - x.mean(axis=1, keepdims=True)
                X = np.abs(np.fft.rfft(xx * np.hanning(n), axis=1)) ** 2
                f = np.fft.rfftfreq(n, 1 / self.fs)
                band = X[:, (f > 300) & (f < 3400)].sum(axis=1) + 1e-15
                tone = sum(X[:, np.abs(f - t) < 110].sum(axis=1) for t in self.tones)
                vals.append(10 * np.log10(tone / band + 1e-6))
        v = np.concatenate(vals) if vals else np.zeros(0)
        if self.iq:
            floor = np.percentile(v, 10)
            return v > floor + 10.0, v - floor
        return v > -5.0, v

def find_bursts(on: np.ndarray, level: np.ndarray, min_s: float = 0.3) -> list[tuple[float, float, float]]:
    bursts, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and (on[j] or (j + 3 < len(on) and on[j + 1: j + 4].any())):
                j += 1
            if (j - i) / 100 >= min_s:
                bursts.append((i / 100, (j - i) / 100, float(level[i:j].mean())))
            i = j
        else:
            i += 1
    return bursts


# -- decoding ----------------------------------------------------------------------------------
def segments(audio: np.ndarray, fs: float, f_lo: float, f_hi: float) -> list[tuple[int, float]]:
    """Tone segments as (tone, duration in symbols): tone 0 = low, 1 = high."""
    t = np.arange(len(audio)) / fs
    m1 = np.abs(lowpass(audio * np.exp(-2j * np.pi * f_lo * t), 450, fs))
    m2 = np.abs(lowpass(audio * np.exp(-2j * np.pi * f_hi * t), 450, fs))
    act = (m1 + m2) > 0.25 * np.percentile(m1 + m2, 90)
    soft = m2 / (np.median(m2[act]) + 1e-12) - m1 / (np.median(m1[act]) + 1e-12)
    soft[~act] = 0
    # hysteresis
    h = 0.15 * np.std(soft[act]) if act.any() else 0
    state, cur, start = 0, 0, 0
    segs: list[list[float]] = []
    for i, v in enumerate(soft):
        s = 1 if v > h else -1 if v < -h else cur
        if s != cur:
            if cur != 0:
                segs.append([1 if cur > 0 else 0, (i - start) / fs * BAUD])
            cur, start = s, i
    if cur != 0:
        segs.append([1 if cur > 0 else 0, (len(soft) - start) / fs * BAUD])
    # merge glitches shorter than 0.4 symbol into their neighbours
    out: list[list[float]] = []
    for tone, d in segs:
        if out and d < 0.4:
            out[-1][1] += d
            continue
        if out and out[-1][0] == tone:
            out[-1][1] += d
        else:
            out.append([tone, d])
    return [(int(t_), d) for t_, d in out]


def frames_from_segments(segs: list[tuple[int, float]]) -> tuple[list[str], int]:
    """Split at 4-symbol delimiters; bits = 1 for a 2-symbol segment, 0 for a 1-symbol one."""
    frames, cur, broken = [], [], 0
    for _, d in segs:
        n = int(round(d))
        if n == 4:
            if cur:
                frames.append("".join(cur))
            cur = []
        elif n in (1, 2):
            cur.append("1" if n == 2 else "0")
        else:
            if cur:
                broken += 1
            cur = []
    return frames, broken


def hexs(bits: str) -> str:
    return " ".join(f"{int(bits[i:i + 8], 2):02X}" for i in range(0, len(bits) - len(bits) % 8, 8))


def decode_file(path: Path, verbose: bool, tones: tuple[float, float]) -> list[dict]:
    src = Source(path)
    src.tones = tones
    kind = "I/Q" if src.iq else "audio"
    dur = src.n / src.fs
    print(f"# {path.name}: {kind}, {src.fs:.0f} Hz, {dur:.1f} s", file=sys.stderr)
    on, level = src.activity()
    rows = []
    for t0, ln, snr in find_bursts(on, level):
        x = src.read(int((t0 - 0.02) * src.fs), int((t0 + ln + 0.02) * src.fs))
        if src.iq:
            f0 = fm_demod(x, src.fs)
            offset = float(np.median(f0[len(f0) // 10: -len(f0) // 10 or None]))
            audio = lowpass(f0 - offset, 2500, src.fs)
        else:
            offset = float("nan")
            audio = lowpass(x - x.mean(), 2500, src.fs)
        lo, hi = two_tones(audio[int(0.05 * src.fs):], src.fs)
        segs = segments(audio, src.fs, lo, hi)
        frames, broken = frames_from_segments(segs)
        good = [f for f in frames if len(f) in (32, 64)]
        counts = Counter(good)
        when = (src.start + timedelta(seconds=t0)).isoformat(sep=" ") if src.start else ""
        best = counts.most_common(1)[0] if counts else (None, 0)
        row = {"file": path.name, "t_s": round(t0, 2), "time": when, "len_s": ln, "level_db": round(snr, 1), "level_kind": "rf_snr" if src.iq else "tone_share",
               "offset_hz": round(offset) if offset == offset else "", "tones_hz": f"{lo:.0f}/{hi:.0f}", "frames": len(frames),
               "valid": len(good), "copies": best[1], "bits": best[0] or "",
               "hex": hexs(best[0]) if best[0] else "", "parity": (best[0].count("1") % 2) if best[0] else "",
               "other_lengths": ",".join(str(len(f)) for f in frames if len(f) not in (32, 64))}
        rows.append(row)
        flag = "" if best[0] else "  (no complete frame)"
        print(f"{when or f't={t0:8.2f}s'}  {ln:4.2f}s  {snr:4.1f}dB  off {row['offset_hz'] if row['offset_hz'] == '' else format(row['offset_hz'], '+5')}Hz  tones {row['tones_hz']}  "
              f"x{best[1]} {len(best[0] or '')}b  {row['hex']}{flag}")
        if verbose and len(counts) > 1:
            for b, n in counts.most_common()[1:]:
                print(f"      also x{n}: {hexs(b)}")
    return rows


def summary(rows: list[dict]) -> None:
    msgs = [r for r in rows if r["bits"]]
    print(f"\n# bursts {len(rows)}, decoded {len(msgs)}; parity even in "
          f"{sum(1 for r in msgs if r['parity'] == 0)} of {len(msgs)}")
    addr = Counter(r["hex"].split()[2] + r["hex"].split()[3] for r in msgs if len(r["bits"]) == 64)
    print("# address field (bytes 2-3) of 64-bit frames:", ", ".join(f"{a} x{n}" for a, n in addr.most_common()))
    short = Counter(r["hex"] for r in msgs if len(r["bits"]) == 32)
    print("# 32-bit frames:", ", ".join(f"{h} x{n}" for h, n in short.most_common()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav", nargs="+", type=Path)
    ap.add_argument("--csv", type=Path, help="write the decoded bursts here (small; share this, not the WAV)")
    ap.add_argument("--tones", default="900,1500", help="approximate tone pair for finding bursts in audio files (Hz)")
    ap.add_argument("--verbose", action="store_true", help="also list the less frequent frames of each burst")
    a = ap.parse_args()
    rows = []
    for p in a.wav:
        rows += decode_file(p, a.verbose, tuple(float(v) for v in a.tones.split(",")))
    summary(rows)
    if a.csv:
        with a.csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
            w.writeheader()
            w.writerows(rows)
        print(f"# wrote {a.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
