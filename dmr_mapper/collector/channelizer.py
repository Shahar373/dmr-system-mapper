"""FFT channelizer: one wideband block in, N narrowband discriminator-audio streams out.

Per block of ``hop`` samples at the capture rate (2.4 MS/s): one FFT of the block plus the
overlap from the previous block, then for every channel a slice of bins around the carrier
(96 kHz wide, raised-cosine edges) and a small inverse FFT. That slice *is* the mixed, filtered
and decimated channel at 96 kHz (overlap-save, so block edges are exact). Then per channel: a
7.5 kHz low-pass (the surveyor's channel filter), FM discriminator, decimation to 48 kHz and a
slow level normalisation to int16, which is what DSD-FME reads over TCP.

Cost on this machine: ~40 ms per second of IQ for four channels.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import fft as sfft
from scipy import signal as ss

CHANNEL_RATE = 96_000      # preferred channel rate; the actual one is sample_rate / D (see pick_decimation)
AUDIO_RATE = 48_000
CHANNEL_LOWPASS_HZ = 7_500
EDGE_HZ = 8_000            # raised-cosine transition at the band edge of the 96 kHz slice
PEAK_FRACTION = 0.9        # target of the 99.5th percentile after normalisation (as the surveyor)
GAIN_FLOOR_HZ = 1_000.0    # normalisation never assumes less than ±1 kHz peak deviation
GAIN_CEIL_HZ = 8_000.0     # ... nor more than ±8 kHz


@dataclass
class ChannelState:
    freq_hz: float
    bin_center: int = 0
    residual_hz: float = 0.0
    lp_zi: np.ndarray | None = None
    dec_zi: np.ndarray | None = None
    last_sample: complex = 0j
    gain_hz: float = 2_500.0          # current estimate of the peak deviation
    level_db: float = -120.0          # channel power relative to full scale, for diagnostics
    phase: float = 0.0
    samples_out: int = 0
    dec_phase: int = 0                # where the next kept sample falls in the upsampled stream
    stats: dict = field(default_factory=dict)


def pick_decimation(fs: float) -> tuple[int, int, int]:
    """(D, up, down): channel rate fs/D between 64 and 128 kHz, an integer, and as cheap as
    possible to resample to 48 kHz (smallest up+down). 2.4 MS/s → (25, 1, 2); 2 MS/s → (25, 3, 5)."""
    from fractions import Fraction

    best: tuple[int, int, int, int] | None = None
    for D in range(2, 400):
        cr = fs / D
        if not (64_000 <= cr <= 128_000) or abs(cr - round(cr)) > 1e-6:
            continue
        fr = Fraction(AUDIO_RATE, int(round(cr)))
        if fr.denominator > 50:
            continue
        cost = fr.numerator + fr.denominator
        if best is None or cost < best[0]:
            best = (cost, D, fr.numerator, fr.denominator)
    if best is None:
        raise ValueError(f"sample rate {fs} cannot be channelised to 48 kHz audio (pick e.g. 2000000 or 2400000)")
    return best[1], best[2], best[3]


class Channelizer:
    def __init__(self, sample_rate: float, center_hz: float, hop: int | None = None):
        self.fs = float(sample_rate)
        self.center_hz = float(center_hz)
        self.D, self.up, self.down = pick_decimation(self.fs)
        self.cr = self.fs / self.D                          # channel rate: 96 kHz at 2.4 MS/s, 80 kHz at 2 MS/s
        self.hop = hop or int(self.fs // 10)                # 0.1 s
        self.hop -= self.hop % self.D
        # ~10 ms of overlap (>> the raised-cosine impulse response), chosen so that N has small
        # prime factors: 240000 + 22500 = 262500 = 2^2 3 5^5 7, which the FFT likes.
        self.overlap = int(self.fs * 0.009375)
        self.overlap -= self.overlap % self.D
        self.N = self.hop + self.overlap
        self.M = self.N // self.D                           # bins (and samples) per channel block
        self._hist = np.zeros(self.overlap, dtype=np.complex64)
        self._n0 = -self.overlap          # absolute index of the first sample of the current FFT block
        # output block b covers absolute samples [n0_b + overlap/2, n0_b + overlap/2 + hop): contiguous
        self._taper = self._make_taper()
        self.channels: dict[float, ChannelState] = {}
        self._lp = ss.firwin(65, CHANNEL_LOWPASS_HZ, fs=self.cr).astype(np.float32)
        # rational resampler cr → 48 kHz: zero-stuff by ``up``, low-pass, keep every ``down``-th sample
        self._dec = (ss.firwin(31 * max(self.up, 1) | 1, min(18_000, AUDIO_RATE * 0.4), fs=self.cr * self.up) * self.up).astype(np.float32)
        self._bin_hz = self.fs / self.N

    def _make_taper(self) -> np.ndarray:
        f = np.fft.fftfreq(self.M, d=1.0 / self.cr)
        half = self.cr / 2
        t = np.ones(self.M)
        edge = (np.abs(f) > half - EDGE_HZ)
        t[edge] = 0.5 * (1 + np.cos(np.pi * (np.abs(f[edge]) - (half - EDGE_HZ)) / EDGE_HZ))
        return t.astype(np.float32)

    def set_channels(self, freqs: list[float]) -> None:
        keep = {}
        for f in freqs:
            st = self.channels.get(f) or ChannelState(freq_hz=float(f))
            off = float(f) - self.center_hz
            st.bin_center = int(round(off / self._bin_hz))
            st.residual_hz = off - st.bin_center * self._bin_hz
            if st.lp_zi is None:
                st.lp_zi = np.zeros(len(self._lp) - 1, dtype=np.complex64)
                st.dec_zi = np.zeros(len(self._dec) - 1, dtype=np.float32)
            keep[f] = st
        self.channels = keep

    def process(self, block: np.ndarray) -> dict[float, np.ndarray]:
        """``block`` has exactly ``hop`` samples. Returns int16 48 kHz audio per channel."""
        if len(block) != self.hop:
            raise ValueError(f"expected {self.hop} samples, got {len(block)}")
        x = np.concatenate([self._hist, block])
        self._hist = block[-self.overlap:].copy()
        X = sfft.fft(x, workers=1)
        out: dict[float, np.ndarray] = {}
        discard = self.overlap // self.D
        n0 = self._n0
        self._n0 += self.hop
        for f, st in self.channels.items():
            idx = (st.bin_center + np.arange(-self.M // 2, self.M // 2)) % self.N
            slice_ = X[idx]
            slice_ = np.roll(slice_, self.M // 2) * self._taper          # bins ordered as fftfreq
            # Overlap-save with a zero-phase spectral filter: the circular convolution is wrong at
            # BOTH ends of the block (the start needs the past, the end needs the future), so keep
            # the middle ``hop/D`` samples, half the overlap away from each edge.
            ch = sfft.ifft(slice_, workers=1)[discard // 2:discard // 2 + self.hop // self.D].astype(np.complex64) * (self.D / 1.0)
            # The bin shift is a mixer that restarts at every FFT block; make it continuous in time
            # or the discriminator sees a phase step at each block boundary.
            ch *= np.exp(-2j * np.pi * ((st.bin_center * n0) % self.N) / self.N).astype(np.complex64)
            if st.residual_hz:
                n = np.arange(len(ch))
                ch = ch * np.exp(-2j * np.pi * (st.residual_hz / self.cr) * n + 1j * st.phase).astype(np.complex64)
                st.phase = float((st.phase - 2 * np.pi * st.residual_hz / self.cr * len(ch)) % (2 * np.pi))
            ch, st.lp_zi = ss.lfilter(self._lp, 1.0, ch, zi=st.lp_zi)
            power = float(np.mean(np.abs(ch) ** 2))
            st.level_db = 10 * np.log10(power + 1e-20)
            # FM discriminator in Hz
            prev = np.concatenate([[st.last_sample], ch[:-1]])
            st.last_sample = complex(ch[-1])
            dev_hz = (np.angle(ch * np.conj(prev)) * (self.cr / (2 * np.pi))).astype(np.float32)
            if self.up > 1:
                stuffed = np.zeros(len(dev_hz) * self.up, dtype=np.float32)
                stuffed[::self.up] = dev_hz
            else:
                stuffed = dev_hz
            filt, st.dec_zi = ss.lfilter(self._dec, 1.0, stuffed, zi=st.dec_zi)
            audio = filt[st.dec_phase::self.down]
            st.dec_phase = (st.dec_phase - len(stuffed)) % self.down
            # slow normalisation: 99.5th percentile of |deviation| → PEAK_FRACTION of full scale
            p = float(np.percentile(np.abs(audio), 99.5)) if len(audio) else st.gain_hz
            p = min(GAIN_CEIL_HZ, max(GAIN_FLOOR_HZ, p))
            st.gain_hz = 0.9 * st.gain_hz + 0.1 * p
            pcm = np.clip(audio / st.gain_hz * PEAK_FRACTION, -1.0, 1.0)
            out[f] = (pcm * 32767).astype(np.int16)
            st.samples_out += len(pcm)
        return out
