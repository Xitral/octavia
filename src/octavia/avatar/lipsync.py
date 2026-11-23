"""Lip sync from the speech audio as it is played.

``VisemeAnalyzer`` is fed the exact float32 blocks the speaker plays and turns every 20 ms of them into a
``MouthFrame``: how far the mouth is open (loudness) and which of the five VRM mouth shapes (aa, ih, ou, ee, oh)
it takes. The TTS gives us no phoneme timing, so the vowel is read off the audio itself: a small linear-prediction
(LPC) model of the vocal tract finds the first two formants, and their position is compared with where each
vowel's formants sit for a female voice. Consonants and breath have no clear formants and only get a small
generic opening, which reads as natural speech on an avatar far better than guessing a vowel.

It runs on a worker thread fed by the audio callback, so everything is numpy on a ~11 kHz copy of a 30 ms
window: roughly 0.1-0.3 ms per frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

VISEMES = ("aa", "ih", "ou", "ee", "oh")
# (F1, F2) in Hz for each viseme, a female voice, in VISEMES order: a, i, u, e, o.
VOWEL_FORMANTS = ((850.0, 1450.0), (320.0, 2700.0), (380.0, 1100.0), (550.0, 2250.0), (520.0, 900.0))
# What the mouth does on sound without a vowel in it (s, f, h, breath): a little open, lips relaxed.
GENERIC_OPENING = (0.2, 0.1, 0.0, 0.05, 0.0)

FLOOR_DB = -45.0  # RMS (dBFS) at which the mouth starts to open
CEIL_DB = -12.0  # RMS at which it is fully open
TARGET_RATE = 10_000  # decimate to the first rate at or above this (44.1 kHz -> 11.025 kHz)
ANALYSIS_MS = 30.0  # LPC window; 20 ms holds too few pitch periods of a low voice for stable formants
PRE_EMPHASIS = 0.9
LAG_WINDOW_HZ = 60.0  # smooths the spectrum so LPC fits the envelope, not the harmonics of a high voice
F1_RANGE = (230.0, 1150.0)
F2_RANGE = (700.0, 3300.0)
MAX_BANDWIDTH = 500.0  # wider poles are spectral tilt or noise, not formants
PITCH_RANGE = (70.0, 500.0)
VOICED_MIN, VOICED_FULL = 0.35, 0.65  # periodicity (normalised autocorrelation) from "no vowel" to "clear vowel"
VOWEL_SPREAD = 0.2  # log-frequency distance over which a vowel's weight falls off
VOWEL_REACH = 0.45  # formants this far (log units) from every vowel are treated as unclear
ATTACK_MS = 15.0
RELEASE_MS = 50.0


@dataclass(frozen=True)
class MouthFrame:
    level: float  # 0..1, loudness: how far the jaw opens
    weights: tuple[float, float, float, float, float]  # aa, ih, ou, ee, oh; each 0..1, sum at most 1

    def message(self) -> dict[str, Any]:
        """The protocol's ``mouth`` message, rounded to 3 decimals without letting the weights sum past 1."""
        v = [round(min(max(float(w), 0.0), 1.0), 3) for w in self.weights]
        excess = round(sum(v) - 1.0, 3)
        if excess > 0:
            i = v.index(max(v))
            v[i] = round(v[i] - excess, 3)
        return {"type": "mouth", "level": round(min(max(float(self.level), 0.0), 1.0), 3), "v": v}


CLOSED = MouthFrame(0.0, (0.0, 0.0, 0.0, 0.0, 0.0))


class VisemeAnalyzer:
    """Turns played audio into mouth frames, one per ``frame_ms`` of audio.

    ``feed`` accepts blocks of any size (the audio callback's), buffers the remainder, and returns the frames the
    block completed. The result depends only on the samples, never on how they were split into blocks.
    """

    def __init__(self, sample_rate: int, frame_ms: float = 20.0) -> None:
        if sample_rate <= 0 or frame_ms <= 0:
            raise ValueError("sample_rate and frame_ms must be positive")
        self.sample_rate = int(sample_rate)
        self.frame_ms = float(frame_ms)
        self._hop = max(1, round(self.sample_rate * self.frame_ms / 1000))
        self._q = max(1, self.sample_rate // TARGET_RATE)
        self._rate = self.sample_rate / self._q
        self._fir = _lowpass(self._q)
        self._n = max(32, round(self._rate * max(ANALYSIS_MS, self.frame_ms) / 1000))
        self._span = self._n * self._q + self._fir.size - 1  # input samples behind one decimated window
        self._context = max(self._span, self._hop)
        self._order = min(16, max(8, round(self._rate / 1000) + 1))
        self._window = np.hamming(self._n)
        lags = np.arange(self._order + 1)
        self._lag_window = np.exp(-0.5 * (2 * np.pi * LAG_WINDOW_HZ * lags / self._rate) ** 2)
        self._lag_window[0] = 1.0001  # white-noise correction keeps Levinson-Durbin well conditioned
        self._pitch_lags = (
            max(2, int(self._rate / PITCH_RANGE[1])),
            min(self._n // 2, int(math.ceil(self._rate / PITCH_RANGE[0]))),
        )
        self._nfft = 1 << (2 * self._n - 1).bit_length()
        self._targets = np.log(np.asarray(VOWEL_FORMANTS))
        self._generic = np.asarray(GENERIC_OPENING)
        self._attack = 1.0 - math.exp(-self.frame_ms / ATTACK_MS)
        self._release = 1.0 - math.exp(-self.frame_ms / RELEASE_MS)
        self.reset()

    def reset(self) -> None:
        """Forget buffered audio and close the mouth (call between utterances or after a barge-in)."""
        self._buf = np.zeros(self._context, dtype=np.float32)
        self._level = 0.0
        self._weights = np.zeros(len(VISEMES))

    def feed(self, block: np.ndarray) -> list[MouthFrame]:
        x = np.asarray(block, dtype=np.float32)
        if x.ndim > 1:
            x = x.reshape(x.shape[0], -1).mean(axis=1)
        if x.size == 0:
            return []
        buf = np.concatenate((self._buf, x))
        start = self._context
        fresh = buf[start:]
        if not np.isfinite(fresh).all():
            fresh[~np.isfinite(fresh)] = 0.0
        np.clip(fresh, -8.0, 8.0, out=fresh)
        frames: list[MouthFrame] = []
        while buf.size - start >= self._hop:
            end = start + self._hop
            frames.append(self._frame(buf[end - self._context : end], buf[start:end]))
            start = end
        self._buf = buf[start - self._context :].copy()
        return frames

    # -- one frame ---------------------------------------------------------------------------------------------
    def _frame(self, context: np.ndarray, hop: np.ndarray) -> MouthFrame:
        power = float(np.dot(hop, hop)) / hop.size
        db = 10.0 * math.log10(power + 1e-20)
        level = min(max((db - FLOOR_DB) / (CEIL_DB - FLOOR_DB), 0.0), 1.0)
        shape = self._shape(context) * level if level > 0.0 else np.zeros(len(VISEMES))
        self._level = self._smooth(np.asarray([self._level]), np.asarray([level]))[0]
        self._weights = self._smooth(self._weights, shape)
        total = float(self._weights.sum())
        if total > 1.0:
            self._weights /= total * (1.0 + 1e-9)
        w = self._weights
        return MouthFrame(float(self._level), (float(w[0]), float(w[1]), float(w[2]), float(w[3]), float(w[4])))

    def _smooth(self, current: np.ndarray, target: np.ndarray) -> np.ndarray:
        """Fast attack so syllables open crisply, slower release so the mouth doesn't flutter between them."""
        rate = np.where(target > current, self._attack, self._release)
        out = current + rate * (target - current)
        out[(target == 0.0) & (out < 1e-3)] = 0.0
        return out

    def _shape(self, context: np.ndarray) -> np.ndarray:
        """Mouth shape (weights summing to at most 1) for the audio window ending now."""
        x = self._decimate(context)
        x = x - x.mean()
        clarity = self._voicing(x)
        if clarity <= 0.0:
            return self._generic.copy()
        formants = self._formants(x)
        if formants is None:
            return self._generic.copy()
        d2 = (((np.log(formants) - self._targets) / VOWEL_SPREAD) ** 2).sum(axis=1)
        nearest = float(d2.min())
        vowel = np.exp(-0.5 * (d2 - nearest))
        vowel /= vowel.sum()
        clarity *= math.exp(-0.5 * nearest * (VOWEL_SPREAD / VOWEL_REACH) ** 2)
        return clarity * vowel + (1.0 - clarity) * self._generic

    def _decimate(self, context: np.ndarray) -> np.ndarray:
        if self._q == 1:
            return context[-self._n :].astype(np.float64)
        windows = np.lib.stride_tricks.sliding_window_view(context[-self._span :], self._fir.size)
        return windows[self._q - 1 :: self._q] @ self._fir

    def _voicing(self, x: np.ndarray) -> float:
        """0..1: how periodic the window is. Vowels are; fricatives and breath aren't."""
        lo, hi = self._pitch_lags
        if hi <= lo:
            return 0.0
        spec = np.fft.rfft(x, self._nfft)
        r = np.fft.irfft(spec.real**2 + spec.imag**2, self._nfft)[: hi + 1]
        energy = np.cumsum(x * x)
        total = energy[-1]
        if total <= 0.0:
            return 0.0
        k = np.arange(lo, hi + 1)
        head = energy[self._n - 1 - k]  # energy of x[: n - k]
        tail = total - energy[k - 1]  # energy of x[k:]
        nccf = r[lo : hi + 1] / np.sqrt(head * tail + 1e-20)
        peak = float(nccf.max())
        return min(max((peak - VOICED_MIN) / (VOICED_FULL - VOICED_MIN), 0.0), 1.0)

    def _formants(self, x: np.ndarray) -> np.ndarray | None:
        """(F1, F2) in Hz from the LPC polynomial's roots, or None when the spectrum shows no clear pair."""
        y = np.empty_like(x)
        y[0] = x[0]
        y[1:] = x[1:] - PRE_EMPHASIS * x[:-1]
        y *= self._window
        p = self._order
        r = np.array([np.dot(y[: self._n - k], y[k:]) for k in range(p + 1)]) * self._lag_window
        a = _levinson(r, p)
        if a is None or not np.isfinite(a).all():
            return None
        roots = np.roots(a)
        roots = roots[roots.imag > 0]
        freqs = np.angle(roots) * self._rate / (2 * np.pi)
        bws = -np.log(np.abs(roots) + 1e-12) * self._rate / np.pi
        keep = bws < MAX_BANDWIDTH
        freqs = np.sort(freqs[keep])
        f1s = freqs[(freqs >= F1_RANGE[0]) & (freqs <= F1_RANGE[1])]
        if f1s.size == 0:
            return None
        f1 = float(f1s[0])
        f2s = freqs[(freqs > f1 + 150.0) & (freqs >= F2_RANGE[0]) & (freqs <= F2_RANGE[1])]
        if f2s.size == 0:
            return None
        return np.asarray([f1, float(f2s[0])])


def _levinson(r: np.ndarray, order: int) -> np.ndarray | None:
    """LPC coefficients [1, a1..ap] from autocorrelation (Levinson-Durbin), or None for a degenerate window."""
    if r[0] <= 0.0:
        return None
    a = np.zeros(order + 1)
    a[0] = 1.0
    err = r[0]
    for i in range(1, order + 1):
        k = -(r[i] + np.dot(a[1:i], r[i - 1 : 0 : -1])) / err
        a[1:i] = a[1:i] + k * a[i - 1 : 0 : -1]
        a[i] = k
        err *= 1.0 - k * k
        if err <= 0.0:
            return None
    return a


def _lowpass(q: int) -> np.ndarray:
    """Windowed-sinc anti-alias filter for decimating by ``q`` (cutoff a little under the new Nyquist)."""
    if q == 1:
        return np.ones(1)
    taps = 8 * q + 1
    n = np.arange(taps) - (taps - 1) / 2
    cutoff = 0.45 / q  # cycles per input sample
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
    return h / h.sum()
