"""How the user's voice sounded, in numbers: loudness, pitch and pace, each against their own usual.

Plain numpy on the utterance the endpointer captured (16 kHz mono float32), a few milliseconds even for a long one:

* **Loudness** is the mean power of the frames with voice in them, in dB below full scale. Quiet frames (more than
  ``_ACTIVE_RANGE_DB`` under the loudest part, or under ``_SILENCE_DB``) are left out, so pauses don't count.
* **Pitch** is YIN (de Cheveigne and Kawahara, 2002) on the voiced frames: the difference function from an FFT
  autocorrelation, its cumulative-mean normalization, the first dip under ``_YIN_THRESHOLD`` between 60 and 400 Hz.
  It's kept in semitones (above 55 Hz) so "higher than usual" means the same for a low voice and a high one: the
  median, and the spread between the 10th and 90th percentiles (a flat voice or an animated one).
* **Speaking rate** is the transcript's words per second of voice (``rate``); the words come later, with the
  transcript, so it's computed then.

``Baseline`` keeps a running mean and spread of each, per user (there's one), so a cue ("louder than usual") is
always relative to how they usually sound, never to an absolute level. It learns slowly (a moving average), starts
saying anything only after ``MIN_SAMPLES`` utterances, and is a few numbers in data/emotion/baseline.json: no audio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

SAMPLE_RATE = 16000
_FRAME = 1024  # 64 ms analysis window (two periods of the lowest pitch fit, with room)
_HOP = 256  # 16 ms between frames
_SILENCE_DB = -55.0  # quieter than this is never voice
_ACTIVE_RANGE_DB = 25.0  # frames this far under the loud part of the utterance are pauses, not voice
_F0_MIN, _F0_MAX = 60.0, 400.0  # the pitch range of speaking voices, low men to high women and children
_YIN_THRESHOLD = 0.2  # a dip of the normalized difference under this is a period (YIN's absolute threshold)
_MIN_VOICED = 4  # pitch frames needed before a pitch is reported (about 130 ms of voicing)
_SEMITONE_REF_HZ = 55.0
_MIN_RATE_SPEECH_S = 0.6  # too little voice to say anything about pace
_MIN_RATE_WORDS = 2

FEATURES = ("loudness_db", "pitch_st", "pitch_range_st", "rate_wps")


@dataclass(frozen=True)
class Prosody:
    """One utterance's measurements; None where it couldn't be measured (silence, no voicing)."""

    loudness_db: float | None = None
    pitch_st: float | None = None  # median pitch, semitones above 55 Hz (A1)
    pitch_range_st: float | None = None  # 10th to 90th percentile of the pitch, in semitones
    speech_s: float = 0.0  # how long the voice was active

    @property
    def pitch_hz(self) -> float | None:
        return None if self.pitch_st is None else _SEMITONE_REF_HZ * 2 ** (self.pitch_st / 12)

    def values(self, words: int = 0) -> dict[str, float | None]:
        """The features by name (``FEATURES``), with the speaking rate from ``words``."""
        return {
            "loudness_db": self.loudness_db,
            "pitch_st": self.pitch_st,
            "pitch_range_st": self.pitch_range_st,
            "rate_wps": rate(words, self.speech_s),
        }


def rate(words: int, speech_s: float) -> float | None:
    """Words per second of voice, or None when there's too little of either to tell."""
    if words < _MIN_RATE_WORDS or speech_s < _MIN_RATE_SPEECH_S:
        return None
    return words / speech_s


def _frames(audio: np.ndarray) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(audio, _FRAME)[::_HOP]


def _level_db(frames: np.ndarray) -> np.ndarray:
    return 10.0 * np.log10(np.mean(np.square(frames, dtype=np.float64), axis=1) + 1e-12)


def measure(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Prosody:
    """Loudness and pitch of ``audio``, and how much of it was voice."""
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"expected {SAMPLE_RATE} Hz audio, got {sample_rate}")
    if x.size < _FRAME:
        return Prosody()
    frames = _frames(x)
    db = _level_db(frames)
    loud = float(np.percentile(db, 95))
    active = (db > loud - _ACTIVE_RANGE_DB) & (db > _SILENCE_DB)
    if not active.any():
        return Prosody()
    speech_s = float(active.sum()) * _HOP / SAMPLE_RATE
    loudness = float(10.0 * np.log10(np.mean(10.0 ** (db[active] / 10.0))))
    f0 = pitch_track(frames[active][::2])  # every other frame (32 ms apart) is plenty for a median and a spread
    voiced = f0[np.isfinite(f0)]
    if voiced.size < _MIN_VOICED:
        return Prosody(loudness_db=loudness, speech_s=speech_s)
    st = 12.0 * np.log2(voiced / _SEMITONE_REF_HZ)
    low, mid, high = np.percentile(st, [10, 50, 90])
    return Prosody(loudness_db=loudness, pitch_st=float(mid), pitch_range_st=float(high - low), speech_s=speech_s)


def pitch_track(frames: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """YIN pitch of each frame (rows of ``_FRAME`` samples) in Hz, NaN where it isn't voiced."""
    if frames.size == 0:
        return np.zeros(0)
    x = frames.astype(np.float64) - frames.mean(axis=1, keepdims=True)
    n = x.shape[1]
    window = n // 2  # the integration window; the lags reach into the other half
    min_lag = int(sample_rate / _F0_MAX)
    max_lag = min(int(math.ceil(sample_rate / _F0_MIN)), n - window)
    # r[t] = sum over j < window of x[j] * x[j + t], for every lag at once (cross-correlation by FFT).
    size = 1 << (2 * n - 1).bit_length()
    head = np.zeros_like(x)
    head[:, :window] = x[:, :window]
    r = np.fft.irfft(np.conj(np.fft.rfft(head, size)) * np.fft.rfft(x, size), size)[:, : max_lag + 1]
    # d[t] = sum (x[j] - x[j + t])^2 = e[0] + e[t] - 2 r[t], with e[t] the energy of the window starting at t.
    c = np.concatenate([np.zeros((x.shape[0], 1)), np.cumsum(np.square(x), axis=1)], axis=1)
    lags = np.arange(max_lag + 1)
    e = c[:, lags + window] - c[:, lags]
    d = np.maximum(e[:, :1] + e - 2.0 * r, 0.0)
    d[:, 0] = 0.0
    # The cumulative-mean normalized difference: 1 at lag 0, dipping toward 0 at the period.
    running = np.cumsum(d[:, 1:], axis=1)
    cmnd = np.ones_like(d)
    cmnd[:, 1:] = d[:, 1:] * lags[1:] / np.maximum(running, 1e-12)
    # The first dip under the threshold, then the bottom of that dip (within half a period past it, so the walk
    # never runs on into the next period's dip).
    span = cmnd[:, min_lag : max_lag + 1]
    below = span < _YIN_THRESHOLD
    voiced = below.any(axis=1) & (e[:, 0] >= 1e-8)
    first = np.argmax(below, axis=1) + min_lag
    allowed = (lags[None, :] >= first[:, None]) & (lags[None, :] <= np.minimum(first * 3 // 2, max_lag)[:, None])
    t = np.argmin(np.where(allowed, cmnd, np.inf), axis=1)
    # Parabolic interpolation between the neighbours of the bottom.
    inner = (t > 0) & (t < max_lag)
    ti = np.clip(t, 1, max_lag - 1)
    rows = np.arange(x.shape[0])
    a, b, c3 = cmnd[rows, ti - 1], cmnd[rows, ti], cmnd[rows, ti + 1]
    denom = a - 2.0 * b + c3
    with np.errstate(divide="ignore", invalid="ignore"):
        shift = np.where(inner & (denom > 1e-12), 0.5 * (a - c3) / denom, 0.0)
    period = t + np.clip(shift, -1.0, 1.0)
    f0 = np.where(voiced & (period > 0), sample_rate / np.maximum(period, 1e-9), np.nan)
    return f0


# ---------------------------------------------------------------------- the user's usual

MIN_SAMPLES = 5  # utterances measured before anything is called "than usual"
_SLOWEST = 0.05  # the moving average's weight once it has settled (about the last 20 utterances)
# The least spread each feature is given, so a few very even utterances don't make every small change a cue.
_MIN_SPREAD = {"loudness_db": 2.0, "pitch_st": 1.0, "pitch_range_st": 1.5, "rate_wps": 0.3}


@dataclass
class _Running:
    n: int = 0
    mean: float = 0.0
    var: float = 0.0

    def add(self, value: float) -> None:
        self.n += 1
        weight = max(1.0 / self.n, _SLOWEST)  # a true mean at first, then a moving average
        diff = value - self.mean
        self.mean += weight * diff
        self.var = (1.0 - weight) * (self.var + weight * diff * diff)


class Baseline:
    """The user's usual loudness, pitch, pitch spread and pace: a running mean and spread of each."""

    def __init__(self) -> None:
        self._stats: dict[str, _Running] = {name: _Running() for name in FEATURES}

    def count(self, name: str) -> int:
        return self._stats[name].n

    def mean(self, name: str) -> float | None:
        stat = self._stats[name]
        return stat.mean if stat.n else None

    def deviation(self, name: str, value: float | None) -> tuple[float, float] | None:
        """How far ``value`` is from the usual: (difference, difference in spreads), or None until the usual is
        known (``MIN_SAMPLES``) or when there's no value."""
        stat = self._stats[name]
        if value is None or stat.n < MIN_SAMPLES:
            return None
        diff = value - stat.mean
        spread = max(math.sqrt(stat.var), _MIN_SPREAD[name])
        return diff, diff / spread

    def update(self, values: dict[str, float | None]) -> None:
        for name, value in values.items():
            if value is not None and name in self._stats and math.isfinite(value):
                self._stats[name].add(float(value))

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "features": {
                name: {"n": s.n, "mean": round(s.mean, 4), "var": round(s.var, 4)} for name, s in self._stats.items()
            },
        }

    @classmethod
    def from_json(cls, data: Any) -> Baseline:
        """A baseline from ``to_json``'s output; anything unreadable in it starts that feature over."""
        baseline = cls()
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, dict):
            return baseline
        for name, stat in features.items():
            if name not in baseline._stats or not isinstance(stat, dict):
                continue
            try:
                n, mean, var = int(stat["n"]), float(stat["mean"]), float(stat["var"])
            except (KeyError, TypeError, ValueError):
                continue
            if n >= 0 and math.isfinite(mean) and math.isfinite(var) and var >= 0:
                baseline._stats[name] = _Running(n, mean, var)
        return baseline
