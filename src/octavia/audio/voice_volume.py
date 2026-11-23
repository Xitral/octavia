"""How loud her own voice plays: a gain on the speech she makes, apart from the system volume.

``audio.output_volume`` in the config sets it; the ``voice_volume`` tool changes it by voice ("talk louder", "set your
voice to 150 percent"). A change applies to the very next sound she makes, and is kept in ``data/voice_volume.json``
so it survives a restart. The file remembers the config value it was made under: when the user edits the config
afterwards, their new config value wins over the old spoken one, so neither way of setting it is silently ignored.

Above 100% the voice can't simply be multiplied: its loudest moments (the burst of a "p" or a "b") would go past full
scale and crackle. Bending those samples back under full scale one by one (a soft clipper) crackles too, just less:
it reshapes the wave right where a plosive is loudest. ``apply_gain`` turns the gain down for a moment instead, a few
milliseconds either side of a peak, so a boost is louder and every sound keeps its shape. ``VoiceGain`` does the same
over a stream of chunks (a sentence arriving from Fish), looking 12 ms ahead across the joins between them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np

from octavia.core.jsonfile import read_json, write_json

log = logging.getLogger(__name__)

SETTINGS_FILE = "data/voice_volume.json"
MAX_GAIN = 2.0  # the config's own limit; beyond it a boost only adds distortion
CEILING = 0.95  # no boosted sample goes past this
RAMP_S = 0.006  # the gain eases down this long before a peak and back up this long after it


def clamp_gain(gain: float) -> float:
    return round(min(MAX_GAIN, max(0.0, float(gain))), 3)


def apply_gain(audio: np.ndarray, gain: float, sample_rate: int = 44100, limit: bool = False) -> np.ndarray:
    """``audio`` (float32 in -1..1) at ``gain``, turned down for a moment around any peak that would pass full scale.
    ``limit``: limited even at a gain of 1 (a voice effect may have made a peak)."""
    if (gain == 1.0 and not limit) or audio.size == 0:
        return audio
    limiter = VoiceGain(sample_rate)
    out = np.concatenate([limiter.process(audio, gain, limit), limiter.flush()])
    return out.reshape(np.shape(audio))


class VoiceGain:
    """``apply_gain`` for one stream of chunks (a sentence), with the same result as the whole stream in one go: it
    holds back the last few milliseconds of each chunk until the next arrives, so a peak at the very start of a chunk
    is eased into like any other, and remembers what it just handed out, so one at the very end is eased out of.
    ``flush`` returns what's held at the end of the stream."""

    def __init__(self, sample_rate: int = 44100) -> None:
        self._ramp = max(2, int(sample_rate * RAMP_S))
        self._hold = 2 * self._ramp  # how far ``_smooth_min`` looks either way: the min window plus half the average
        self._held = np.zeros(0, dtype=np.float32)  # boosted samples not handed out yet
        self._held_need = np.zeros(0, dtype=np.float32)  # the gain each of those needs...
        self._past_need = np.zeros(0, dtype=np.float32)  # ...and the gain the last ones handed out needed

    def process(self, audio: np.ndarray, gain: float, limit: bool = False) -> np.ndarray:
        """The boosted, limited audio ready to play: this chunk, less what's held back for the next. ``limit``: limited
        even at a gain of 1, as when a voice effect is on (``audio/voice_effects.py``)."""
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if gain == 1.0 and not limit and not self._held.size and not (self._past_need < 1.0).any():
            self._past_need = np.zeros(0, dtype=np.float32)
            return x  # unboosted and nothing turned down: as it came
        out = x * np.float32(gain)
        mag = np.abs(out)
        need = np.ones(x.size, dtype=np.float32)
        over = mag > CEILING
        need[over] = CEILING / mag[over]
        out, need = np.concatenate([self._held, out]), np.concatenate([self._held_need, need])
        ready = max(0, out.size - self._hold)
        self._held, self._held_need = out[ready:], need[ready:]
        return self._limit(out[:ready], need, ready)

    def flush(self) -> np.ndarray:
        """What's still held back, limited: the end of the stream."""
        out, need = self._held, self._held_need
        self._held = self._held_need = np.zeros(0, dtype=np.float32)
        result = self._limit(out, need, out.size)
        self._past_need = np.zeros(0, dtype=np.float32)
        return result

    def _limit(self, out: np.ndarray, need: np.ndarray, ready: int) -> np.ndarray:
        """``out`` (the first ``ready`` samples of ``need``'s, boosted) at the gain they need, seen in context."""
        past = self._past_need.size
        need = np.concatenate([self._past_need, need])
        self._past_need = need[: past + ready][-self._hold :]
        if ready == 0 or float(need.min()) == 1.0:
            return out
        result = out * _smooth_min(need, self._ramp)[past : past + ready]
        np.clip(result, -1.0, 1.0, out=result)
        return result


def _smooth_min(need: np.ndarray, ramp: int) -> np.ndarray:
    """A gain at or under ``need`` everywhere, reaching each dip ``ramp`` samples early and leaving it as late, in
    straight ramps: the lowest ``need`` within ``ramp`` samples either side, then averaged over about ``ramp``."""
    r = ramp
    lowest = _window_min(np.pad(need, r, constant_values=1.0), 2 * r + 1)
    h = r // 2
    padded = np.pad(lowest, h, mode="edge").astype(np.float64)  # the edges hold their dip: never above ``need``
    sums = np.concatenate(([0.0], np.cumsum(padded)))
    return ((sums[2 * h + 1 :] - sums[: -(2 * h + 1)]) / (2 * h + 1)).astype(np.float32)


def _window_min(x: np.ndarray, w: int) -> np.ndarray:
    """The minimum of each window of ``w`` samples (``x.size - w + 1`` of them), in a few array passes."""
    n = x.size
    blocks = -(-n // w)
    padded = np.full(blocks * w, np.inf, dtype=np.float32)
    padded[:n] = x
    grid = padded.reshape(blocks, w)
    prefix = np.minimum.accumulate(grid, axis=1).reshape(-1)
    suffix = np.minimum.accumulate(grid[:, ::-1], axis=1)[:, ::-1].reshape(-1)
    count = n - w + 1
    return np.minimum(suffix[:count], prefix[w - 1 : w - 1 + count])


class VoiceVolume:
    """The voice's gain right now, shared by everything that plays her voice, and where it's kept."""

    def __init__(self, gain: float = 1.0, path: Path | None = None, configured: float = 1.0) -> None:
        self.gain = clamp_gain(gain)
        self.path = path
        self.configured = configured  # the config's value, saved with a spoken change (see the module docstring)
        self._listeners: list[Callable[[float], None]] = []

    @classmethod
    def load(cls, path: Path, configured: float) -> VoiceVolume:
        """The saved gain, unless there's none or the config changed since it was saved: then the config's."""
        data = read_json(path, "the voice volume")
        gain = configured
        if isinstance(data, dict):
            saved, base = data.get("gain"), data.get("configured")
            if _number(saved) and _number(base) and abs(float(base) - configured) < 1e-6:
                gain = float(saved)
            elif _number(saved):
                log.info("The config's voice volume changed since it was set by voice; using the config's")
        return cls(gain, path, configured)

    def subscribe(self, listener: Callable[[float], None]) -> None:
        """Call ``listener`` with the new gain on every change (the speech output and the phrase cache)."""
        self._listeners.append(listener)

    def set(self, gain: float) -> float:
        """Use ``gain`` from now on (``save`` keeps it); returns the gain used, after clamping."""
        self.gain = clamp_gain(gain)
        for listener in self._listeners:
            listener(self.gain)
        return self.gain

    def save(self) -> None:
        """Keep the gain for the next start. Raises OSError when it can't be written."""
        if self.path is not None:
            write_json(self.path, {"gain": self.gain, "configured": self.configured}, indent=2)


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
