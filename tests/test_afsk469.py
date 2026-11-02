"""tools/afsk469 on a synthetic signal built with the coding found on 469.1 MHz (2026-11-02).

No recording is checked in (no IQ or WAV is kept): the test generates the signal. Audio FSK at
902/1503 Hz, 600 baud, continuous phase; each tone segment is one symbol for a 0 and two for a 1;
a 4-symbol segment separates frames; a steady leader tone starts the burst. FM-modulated onto a
complex baseband at 50 kHz, as SDRTrunk records a channel, and also written as plain audio.
"""

from __future__ import annotations

import importlib.util
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

TOOL = Path(__file__).resolve().parents[1] / "tools" / "afsk469" / "afsk469.py"
FS = 50_000
BAUD = 600
TONES = (902.0, 1503.0)


def _tool():
    spec = importlib.util.spec_from_file_location("afsk469", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _segments(frame: str, copies: int) -> list[int]:
    """Durations in symbols of the successive tone segments of one burst."""
    seg = [24]                                    # leader
    for _ in range(copies):
        seg.append(4)                             # delimiter
        seg += [2 if b == "1" else 1 for b in frame]
    seg.append(4)
    return seg


def _audio(segments: list[int], fs: int) -> np.ndarray:
    freq = np.concatenate([np.full(int(round(n * fs / BAUD)), TONES[i % 2]) for i, n in enumerate(segments)])
    return np.sin(2 * np.pi * np.cumsum(freq) / fs)


def _write(path: Path, data: np.ndarray, fs: int, stereo: bool) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2 if stereo else 1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes((np.clip(data, -1, 1) * 30000).astype(np.int16).tobytes())


def _burst_iq(frame: str, copies: int) -> np.ndarray:
    au = _audio(_segments(frame, copies), FS)
    return 0.5 * np.exp(1j * 2 * np.pi * np.cumsum(2000.0 * au - 600.0) / FS)   # 2 kHz deviation, 600 Hz off


def _recording(frames: list[tuple[str, int]], rng) -> np.ndarray:
    parts = []
    for frame, copies in frames:
        parts.append(np.zeros(FS // 2, complex))
        parts.append(_burst_iq(frame, copies))
    parts.append(np.zeros(FS // 2, complex))
    x = np.concatenate(parts)
    return x + 0.01 * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))


FRAMES = [
    ("1001011001101010010110100110000010010110100101100000111010011000", 5),   # 96 6A 5A 60 96 96 0E 98
    ("00011101111011000000000001111100", 7),                                   # 1D EC 00 7C
    ("0100000111000010011101001010000011100011100101100101110110010001", 3),   # 41 C2 74 A0 E3 96 5D 91
]


def test_decodes_iq_bursts(tmp_path):
    rng = np.random.default_rng(1)
    x = _recording(FRAMES, rng)
    p = tmp_path / "20261102_202211_469100000_baseband.wav"
    _write(p, np.stack([x.real, x.imag], axis=1).ravel(), FS, stereo=True)
    rows = _tool().decode_file(p, verbose=False, tones=(900.0, 1500.0))
    assert [r["bits"] for r in rows] == [f for f, _ in FRAMES]
    assert [r["copies"] for r in rows] == [n for _, n in FRAMES]
    assert rows[0]["hex"] == "96 6A 5A 60 96 96 0E 98"
    assert rows[0]["time"].startswith("2026-11-02 20:22:11")       # from the SDRTrunk file name
    assert abs(rows[0]["offset_hz"] + 600) < 50
    assert all(r["parity"] == 0 for r in rows)


def test_decodes_demodulated_audio(tmp_path):
    """Plain FM audio, unsquelched (noise between bursts), at a low sample rate."""
    rng = np.random.default_rng(2)
    fs = 8000
    parts = []
    for frame, copies in FRAMES:
        parts.append(rng.uniform(-0.9, 0.9, fs // 2))           # FM noise between bursts is loud
        parts.append(0.6 * _audio(_segments(frame, copies), fs))
    parts.append(rng.uniform(-0.9, 0.9, fs // 2))
    p = tmp_path / "audio.wav"
    _write(p, np.concatenate(parts), fs, stereo=False)
    rows = _tool().decode_file(p, verbose=False, tones=(900.0, 1500.0))
    assert [r["bits"] for r in rows] == [f for f, _ in FRAMES]
    assert rows[0]["time"] == ""
