"""From one utterance's audio to how the user sounds: the model's guess and their voice against their usual, as one
short label with a confidence ("stressed", 0.72, faster and louder than usual), and the note the brain gets for it.

How the two are combined (``combine``):

* When the model hears an emotion more than it hears calm (neutral, other, unknown together), that's the tone,
  named the way it usually shows up when talking to an assistant: angry is "frustrated", disgusted "annoyed",
  fearful "anxious", happy "cheerful" ("excited" when they're also louder or faster), sad "down", surprised
  "surprised". Cues in the voice that fit it (an energetic emotion with a louder, faster, higher voice; a sad one with
  a quieter, slower, flatter one) make it surer, cues against it less sure.
* When the model hears calm, at least two "up" cues together (louder, faster, higher, more animated than usual) make
  "stressed", and at least two "down" cues "tired"; a model that's very sure of calm needs a third cue.
* Otherwise it's "neutral", and neutral never becomes a note: nothing is said about a voice that sounds as usual.

The brain gets the tone only when it's sure enough (``emotion.min_confidence``), phrased as the guess it is
(``tone_note``). ``VoiceTone`` runs it: one worker thread for the model, the baseline kept on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from octavia.core.jsonfile import read_json, write_json
from octavia.emotion.prosody import SAMPLE_RATE, Baseline, Prosody, measure

if TYPE_CHECKING:
    from octavia.config import EmotionConfig

log = logging.getLogger(__name__)

NEUTRAL = "neutral"
# The model's emotions, named the way they usually show up when someone talks to their assistant.
TONE_OF = {
    "angry": "frustrated",
    "disgusted": "annoyed",
    "fearful": "anxious",
    "happy": "cheerful",
    "sad": "down",
    "surprised": "surprised",
}
_CALM = ("neutral", "other", "unknown")
_TENSE = ("angry", "fearful", "disgusted")
_ENERGETIC = frozenset({"angry", "disgusted", "fearful", "happy", "surprised"})  # the rest (sad) is low-energy

# feature: (the least difference that counts, the cue when above the usual, the cue when below). A cue also needs
# the value to be at least one usual spread away (``Baseline.deviation``).
CUES = {
    "loudness_db": (3.0, "louder than usual", "quieter than usual"),
    "pitch_st": (1.5, "higher-pitched than usual", "lower-pitched than usual"),
    "pitch_range_st": (2.0, "more animated than usual", "flatter than usual"),
    "rate_wps": (0.35, "faster than usual", "slower than usual"),
}
UP = frozenset(up for _, up, _ in CUES.values())
DOWN = frozenset(down for _, _, down in CUES.values())
_SPREADS = 1.0
_CUE_WEIGHT = 0.08  # how much one fitting (or contradicting) cue moves the model's confidence
_MAX_CONFIDENCE = 0.95  # never certain: it's a guess from a voice
_SURE_CALM = 0.9
# Less voice than this in an utterance and the model isn't asked: on silence or a click it still answers, and wrongly
# (it calls two seconds of silence "surprised" with 93%).
_MIN_VOICE_S = 0.4


@dataclass(frozen=True)
class Tone:
    tone: str
    confidence: float
    cues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"tone": self.tone, "confidence": self.confidence, "cues": list(self.cues)}


@dataclass(frozen=True)
class Reading:
    """What the worker measured on one utterance, before the transcript (and so the speaking rate) is known."""

    probs: dict[str, float] | None  # the model's probability per label; None when the utterance was too short
    prosody: Prosody | None  # None with ``emotion.prosody`` off
    audio_s: float = 0.0
    took_s: float = 0.0  # how long the worker took on it


class Classifier(Protocol):
    """What ``VoiceTone`` needs of a model (``EmotionModel``, or a test's fake)."""

    def load(self) -> None: ...

    def classify(self, audio: np.ndarray) -> dict[str, float] | None: ...


def cues_from(values: dict[str, float | None], baseline: Baseline) -> tuple[str, ...]:
    """The ways the measured ``values`` differ from the user's usual, in ``CUES`` order."""
    cues = []
    for name, (least, up, down) in CUES.items():
        deviation = baseline.deviation(name, values.get(name))
        if deviation is None:
            continue
        diff, spreads = deviation
        if spreads >= _SPREADS and diff >= least:
            cues.append(up)
        elif spreads <= -_SPREADS and diff <= -least:
            cues.append(down)
    return tuple(cues)


def _clamp(confidence: float) -> float:
    return round(min(_MAX_CONFIDENCE, max(0.0, confidence)), 2)


def combine(probs: dict[str, float] | None, cues: tuple[str, ...] = ()) -> Tone:
    """The model's probabilities and the voice's cues as one tone (see the module docstring)."""
    if not probs:
        return Tone(NEUTRAL, 0.0, cues)
    up = sum(cue in UP for cue in cues)
    down = sum(cue in DOWN for cue in cues)
    calm = sum(probs.get(label, 0.0) for label in _CALM)
    emotions = {label: p for label, p in probs.items() if label in TONE_OF}
    top = max(emotions, key=emotions.__getitem__) if emotions else None
    if top is not None and emotions[top] > calm:
        fits, against = (up, down) if top in _ENERGETIC else (down, up)
        tone = "excited" if top == "happy" and up >= 2 else TONE_OF[top]
        return Tone(tone, _clamp(emotions[top] + _CUE_WEIGHT * (fits - against)), cues)
    sure_calm = 0.15 if calm >= _SURE_CALM else 0.0
    if up >= 2 and up > down:
        tense = sum(probs.get(label, 0.0) for label in _TENSE)
        return Tone("stressed", _clamp(0.35 + 0.12 * up + tense - sure_calm), cues)
    if down >= 2 and down > up:
        return Tone("tired", _clamp(0.3 + 0.1 * down + probs.get("sad", 0.0) - sure_calm), cues)
    return Tone(NEUTRAL, _clamp(calm), cues)


def _spoken_cues(cues: tuple[str, ...]) -> str:
    """("faster than usual", "louder than usual") as "faster and louder than usual"."""
    words = [cue.removesuffix(" than usual") for cue in cues]
    joined = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return f"{joined} than usual"


def tone_note(tone: Tone, min_confidence: float) -> str | None:
    """The brain's note for ``tone``, or None when there's nothing worth saying (neutral, or not sure enough)."""
    if tone.tone == NEUTRAL or tone.confidence < min_confidence:
        return None
    how = f" ({_spoken_cues(tone.cues)})" if tone.cues else ""
    return (
        f"from their voice (not their words) they sound {tone.tone}{how}, a guess: let it shape your tone, and don't "
        "mention it unless it matters"
    )


@dataclass
class ToneJob:
    """A reading on its way for one utterance (``Assistant``): the worker's future, and what the turn adds to it."""

    future: Future[Reading]
    sound_frames: int = 0  # the endpointer's count when its audio was taken: unchanged = only silence came after
    heard_at: float | None = None  # when the transcript was ready (``time.perf_counter``): the wait counts from here
    words: int = 0  # the transcript's words, for the speaking rate
    started: float = field(default_factory=time.perf_counter)
    # Once its turn has used it (``Assistant._heard_tone``): the note it gave, so it's concluded (and learnt from)
    # only once, though a turn started early in the pause asks for it first.
    concluded: tuple[str | None] | None = None


class VoiceTone:
    """The feature: the model on one worker thread, the user's baseline, and their file.

    ``start`` hands an utterance to the worker and returns at once; ``conclude`` turns its reading (with the
    transcript's word count) into a ``Tone`` and lets the baseline learn from it. Both run on the event loop, so the
    baseline is only ever touched there. The audio is only read, never kept or written anywhere."""

    def __init__(self, config: EmotionConfig, model: Classifier, baseline_file: Path | None = None) -> None:
        self.config = config
        self.model = model
        self.baseline_file = baseline_file
        data = read_json(baseline_file, "the voice baseline") if baseline_file is not None else None
        self.baseline = Baseline.from_json(data) if data is not None else Baseline()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-tone")
        self._waiting: Future[Reading] | None = None
        self._learned = False  # something new to save at close
        self.ready = False

    def load(self) -> None:
        """Load the model (blocking: from a worker thread). Until it's done, ``start`` reads nothing."""
        self.model.load()
        self.ready = True

    def start(self, audio: np.ndarray) -> Future[Reading] | None:
        """Read ``audio`` (16 kHz mono) on the worker; None when the model isn't loaded. A reading still waiting for
        the worker is dropped: only the newest audio matters."""
        if not self.ready:
            return None
        if self._waiting is not None:
            self._waiting.cancel()  # only stops one that hasn't started
        future = self._pool.submit(self._read, np.asarray(audio, dtype=np.float32))
        self._waiting = future
        return future

    def _read(self, audio: np.ndarray) -> Reading:
        """On the worker. Never raises: a reading that failed is an empty one (and the turn just has no tone)."""
        started = time.perf_counter()
        try:
            measured = measure(audio)  # a few ms; also says whether there's enough voice for the model
            probs = self.model.classify(audio) if measured.speech_s >= _MIN_VOICE_S else None
            prosody = measured if self.config.prosody else None
        except Exception:
            log.warning("Couldn't read the voice tone", exc_info=True)
            probs = prosody = None
        return Reading(probs, prosody, audio_s=audio.size / SAMPLE_RATE, took_s=time.perf_counter() - started)

    def _values(self, reading: Reading, words: int) -> dict[str, float | None]:
        return reading.prosody.values(words) if reading.prosody is not None else {}

    def conclude(self, reading: Reading, words: int) -> Tone:
        """The tone of ``reading`` against the user's usual, which then learns from it."""
        values = self._values(reading, words)
        tone = combine(reading.probs, cues_from(values, self.baseline))
        self.learn(reading, words)
        return tone

    def learn(self, reading: Reading, words: int) -> None:
        """Let the baseline learn from ``reading`` without judging it (a turn that didn't wait for it)."""
        values = self._values(reading, words)
        if not any(v is not None for v in values.values()):
            return
        self.baseline.update(values)
        self._learned = True
        self._save()

    def learn_later(self, future: Future[Reading], words: int) -> None:
        """``learn`` from a reading once it's done, back on the event loop (it came too late for its turn)."""
        loop = asyncio.get_running_loop()

        def done(f: Future[Reading]) -> None:
            if f.cancelled() or f.exception() is not None:
                return
            try:
                loop.call_soon_threadsafe(self.learn, f.result(), words)
            except RuntimeError:  # the loop has closed (shutting down)
                pass

        future.add_done_callback(done)

    def _save(self) -> None:
        """Write the baseline on the worker thread, so the event loop never waits on the disk."""
        if self.baseline_file is None:
            return
        try:
            self._pool.submit(self._write, self.baseline.to_json())
        except RuntimeError:  # closed
            pass

    def _write(self, data: dict[str, Any]) -> None:
        if self.baseline_file is None:
            return
        try:
            write_json(self.baseline_file, data, indent=1)
        except OSError:
            log.warning("Couldn't save the voice baseline to %s", self.baseline_file, exc_info=True)

    def close(self) -> None:
        """Stop the worker (a reading in progress finishes) and save the baseline one last time."""
        self._pool.shutdown(wait=True, cancel_futures=True)
        if self._learned:
            self._write(self.baseline.to_json())
