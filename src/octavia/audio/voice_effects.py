"""Effects on her voice: a radio, a ship's console, a robot, a room, a tape a touch slow. Off unless picked.

``tts.voice_effect`` picks one (her settings window, the tray's Voice effect menu, or asking her: "use your radio
voice", "turn off the voice effect", "back to normal"; ``core/engines.py``). The effect works on the audio her voice
makes, after it's made and before her volume (``audio/voice_volume.py``), so it's the same on Fish and on her local
voice, and Discord calls and echo cancellation hear it too (both take what the speaker plays).

Each preset is a ``Chain`` of small stages over the stream of chunks a sentence arrives in, plain numpy, a few
milliseconds of work per second of voice. A stage carries what it needs from one chunk to the next (a filter's
spill, a delay line, a carrier's phase), so a reply in chunks sounds exactly as it would in one go, and nothing is
held back: the first word plays as soon as it would without an effect. ``SpeechStream`` keeps one chain for a
whole reply, so a room's echo runs on from one sentence into the next instead of stopping at each, and ``flush``
lets the last of it ring out, faded, after her last word.

Every preset is level-matched to her plain voice (its last gain was measured on her own cached phrases), and with
an effect on, her volume's limiter always runs, so a peak a filter adds can't clip.

Pitch alone (her pitch with her timing kept) isn't here: it needs a heavier algorithm than this, and the cheap ones
warble. "Deeper" lowers pitch and pace together, like a tape played a touch slow.
"""

from __future__ import annotations

import math

import numpy as np

OFF = "off"
# How each pick is shown (her settings window, the tray) and said ("I'm using my radio voice now").
NAMES = {
    OFF: "Off",
    "radio": "Radio",
    "console": "Console",
    "robot": "Robot",
    "room": "Room",
    "deeper": "Deeper",
}
SAID = {
    OFF: "my voice is back to plain, no effect",
    "radio": "I'm using my radio voice now",
    "console": "I'm using my console voice now",
    "robot": "I'm using my robot voice now",
    "room": "I'm speaking with a bit of room around me now",
    "deeper": "I'm using my deeper voice now",
}
EFFECTS = tuple(NAMES)


def _empty() -> np.ndarray:
    return np.zeros(0, dtype=np.float32)


class Effect:
    """One stage over a stream of float32 mono chunks: ``process`` each chunk, ``flush`` what's left at the end."""

    def process(self, x: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def flush(self) -> np.ndarray:
        return _empty()


class Convolve(Effect):
    """``x`` convolved with ``ir`` by FFT overlap-add: what spills past a chunk is added to the next one's start."""

    def __init__(self, ir: np.ndarray) -> None:
        self.ir = np.asarray(ir, dtype=np.float64)
        self._spill = np.zeros(self.ir.size - 1)
        self._spectra: dict[int, np.ndarray] = {}  # the response's spectrum at each FFT size used

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        if n == 0:
            return _empty()
        full = n + self.ir.size - 1
        size = 1 << (full - 1).bit_length()
        spectrum = self._spectra.get(size)
        if spectrum is None:
            spectrum = self._spectra[size] = np.fft.rfft(self.ir, size)
        y = np.fft.irfft(np.fft.rfft(x.astype(np.float64), size) * spectrum, size)[:full]
        k = min(self._spill.size, full)
        y[:k] += self._spill[:k]
        rest = self._spill[k:]  # spill longer than this whole chunk: it carries on past it
        out, spill = y[:n], y[n:].copy()
        if rest.size:
            spill[: rest.size] += rest
        self._spill = spill
        return out.astype(np.float32)

    def flush(self) -> np.ndarray:
        out, self._spill = self._spill, np.zeros(self.ir.size - 1)
        return out.astype(np.float32)


def _lowpass(cutoff: float, rate: int, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / rate * n) * np.blackman(taps)
    return h / h.sum()


def band(rate: int, low: float | None, high: float | None, taps: int = 511) -> np.ndarray:
    """A linear-phase band-pass (a high- or low-pass with one edge None): a windowed sinc. 511 taps at 44.1 kHz is
    a 6 ms filter, its edges about 500 Hz wide: soft, as a radio's are."""
    impulse = np.zeros(taps)
    impulse[(taps - 1) // 2] = 1.0
    high = high if high and high < rate / 2 else None
    h = _lowpass(high, rate, taps) if high else impulse
    if low:
        h = h - _lowpass(low, rate, taps)
    return h


class Drive(Effect):
    """Soft saturation: nearly clean at speech levels, rounding only the loudest moments (a small speaker pushed)."""

    def __init__(self, amount: float) -> None:
        self.k = amount

    def process(self, x: np.ndarray) -> np.ndarray:
        return (np.tanh(self.k * x) / math.tanh(self.k)).astype(np.float32)


class Tremolo(Effect):
    """``depth`` of her voice multiplied by a ``hz`` sine: at a few tens of hertz it's a ring modulator's buzz."""

    def __init__(self, rate: int, hz: float, depth: float) -> None:
        self.step = 2 * math.pi * hz / rate
        self.depth = depth
        self.phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        t = self.phase + self.step * np.arange(x.size)
        self.phase = float((self.phase + self.step * x.size) % (2 * math.pi))
        return (x * (1 - self.depth + self.depth * np.sin(t))).astype(np.float32)


class Chorus(Effect):
    """``x`` plus ``mix`` of itself ``delay_ms`` late, the delay swaying ``sway_ms`` at ``hz``: a doubled voice."""

    def __init__(self, rate: int, delay_ms: float, sway_ms: float, hz: float, mix: float) -> None:
        self.base = delay_ms * rate / 1000
        self.sway = sway_ms * rate / 1000
        self.step = 2 * math.pi * hz / rate
        self.mix = mix
        self.phase = 0.0
        self.keep = int(math.ceil(self.base + self.sway)) + 2  # samples of the past a delayed read can reach
        self._past = np.zeros(self.keep, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        if n == 0:
            return _empty()
        buf = np.concatenate([self._past, x])
        t = self.phase + self.step * np.arange(n)
        self.phase = float((self.phase + self.step * n) % (2 * math.pi))
        pos = np.arange(n) + self.keep - (self.base + self.sway * np.sin(t))
        i = np.floor(pos).astype(np.int64)
        frac = (pos - i).astype(np.float32)
        delayed = buf[i] * (1 - frac) + buf[i + 1] * frac
        self._past = buf[-self.keep :]
        return (x + self.mix * delayed).astype(np.float32)


def room(rate: int, seconds: float, predelay_ms: float, dark: int, seed: int = 7) -> np.ndarray:
    """A room's impulse response, wet only, unit energy: noise dying away (60 dB down at ``seconds``), darkened by
    averaging ``dark`` samples, after ``predelay_ms`` of quiet. Seeded, so it's the same room every time."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    noise = rng.standard_normal(n)
    if dark > 1:
        noise = np.convolve(noise, np.ones(dark) / dark, mode="same")
    t = np.arange(n) / rate
    tail = noise * np.exp(-6.9 * t / seconds)
    fade = int(0.004 * rate)
    tail[:fade] *= np.linspace(0, 1, fade)
    ir = np.concatenate([np.zeros(int(predelay_ms * rate / 1000)), tail])
    return ir / np.sqrt(np.sum(ir**2))


def comb(rate: int, delay_ms: float, gains: tuple[float, ...]) -> np.ndarray:
    """Echoes ``delay_ms`` apart at ``gains`` (the first is the voice itself): a metallic ring at 1000/delay_ms Hz."""
    d = max(1, int(delay_ms * rate / 1000))
    ir = np.zeros(d * (len(gains) - 1) + 1)
    ir[::d] = gains
    return ir


class Tape(Effect):
    """Played at ``speed`` (0.96: 4% lower and 4% slower), read by linear interpolation carried across chunks."""

    def __init__(self, speed: float) -> None:
        self.speed = speed
        self.pos = 0.0  # where the next output sample is read, from the start of ``_past``
        self._past = _empty()

    def process(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self._past, np.asarray(x, dtype=np.float32)])
        last = buf.size - 2  # the furthest a read can start: it needs the sample after it
        if last < self.pos:
            self._past = buf
            return _empty()
        count = int(math.floor((last - self.pos) / self.speed)) + 1
        p = self.pos + self.speed * np.arange(count)
        i = np.floor(p).astype(np.int64)
        frac = (p - i).astype(np.float32)
        out = buf[i] * (1 - frac) + buf[i + 1] * frac
        nxt = self.pos + self.speed * count
        drop = int(math.floor(nxt))
        self._past = buf[drop:]
        self.pos = nxt - drop
        return out.astype(np.float32)

    def flush(self) -> np.ndarray:
        out = self.process(np.zeros(2, dtype=np.float32))
        self._past, self.pos = _empty(), 0.0
        return out


class Mix(Effect):
    """``dry`` of the voice plus ``wet`` of it through ``inner``, which must hand back as many samples as it's given
    (a ``Convolve``)."""

    def __init__(self, inner: Effect, dry: float, wet: float) -> None:
        self.inner, self.dry, self.wet = inner, dry, wet

    def process(self, x: np.ndarray) -> np.ndarray:
        return (self.dry * x + self.wet * self.inner.process(x)).astype(np.float32)

    def flush(self) -> np.ndarray:
        return (self.wet * self.inner.flush()).astype(np.float32)


class Gain(Effect):
    def __init__(self, gain: float) -> None:
        self.gain = gain

    def process(self, x: np.ndarray) -> np.ndarray:
        return (x * np.float32(self.gain)).astype(np.float32)


class Chain(Effect):
    """Stages in order. ``flush`` passes each stage's leftovers through the stages after it, then keeps at most
    ``tail_s`` of it, faded out, so the end rings out briefly instead of stopping dead or dragging on."""

    def __init__(self, name: str, rate: int, *stages: Effect, tail_s: float = 0.0) -> None:
        self.name = name
        self.rate = rate
        self.stages = stages
        self.tail = int(tail_s * rate)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        for stage in self.stages:
            x = stage.process(x)
        return x

    def flush(self) -> np.ndarray:
        out = _empty()
        for stage in self.stages:
            out = np.concatenate([stage.process(out), stage.flush()]) if out.size else stage.flush()
        out = out[: self.tail]
        if out.size:
            out = out * np.linspace(1.0, 0.0, out.size, dtype=np.float32) ** 2
        return out.astype(np.float32)


def make(name: str | None, rate: int) -> Chain | None:
    """A fresh chain for ``name`` at ``rate``, or None for "off" (or a name that isn't one: she speaks plainly)."""
    if name == "radio":  # an old receiver: a narrow band, a small speaker pushed
        return Chain(name, rate, Convolve(band(rate, 380, 3300)), Drive(2.2), Gain(0.85), tail_s=0.01)
    if name == "console":  # a ship's computer: a little air off the bottom, a swaying double, a small bright room
        return Chain(
            name,
            rate,
            Convolve(band(rate, 140, 9000, taps=255)),
            Chorus(rate, 11, 1.2, 0.35, 0.28),
            Mix(Convolve(room(rate, 0.32, 8, 3)), 1.0, 0.16),
            Gain(1.21),
            tail_s=0.15,
        )
    if name == "robot":  # a faint buzz and a metallic ring under her voice, not over it
        return Chain(
            name, rate, Tremolo(rate, 58, 0.32), Convolve(comb(rate, 4.5, (1.0, 0.42, 0.18, 0.08))), Gain(1.18),
            tail_s=0.02,
        )  # fmt: skip
    if name == "room":  # a soft small hall
        return Chain(name, rate, Mix(Convolve(room(rate, 0.7, 14, 6)), 0.95, 0.2), tail_s=0.3)
    if name == "deeper":
        return Chain(name, rate, Tape(0.96))
    return None


def apply(audio: np.ndarray, name: str | None, rate: int) -> np.ndarray:
    """``audio`` (float32, -1..1) through ``name`` in one go, its tail included (a cached phrase); as it is when off."""
    chain = make(name, rate)
    if chain is None or audio.size == 0:
        return audio
    return np.concatenate([chain.process(audio), chain.flush()])


def picked(name: object) -> str:
    """``name`` if it's an effect, else "off"."""
    return name if isinstance(name, str) and name in NAMES else OFF
