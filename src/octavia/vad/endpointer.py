"""Utterance endpointing: decides when the user started and stopped talking.

Pure logic over (frame, speech_probability) pairs, independent of any VAD
model, so it can be tested with synthetic probabilities.

Behaviour:

* A pre-roll ring buffer keeps the last ``pre_roll_ms`` of audio while
  waiting, so the first syllable isn't clipped when speech is detected.
* Hysteresis: speech begins at ``threshold`` and continues until the
  probability stays below ``neg_threshold`` for ``end_silence_ms``.
* Blips shorter than ``min_speech_ms`` of voiced audio (clicks, coughs) are
  discarded and waiting resumes.
* A trailing silence tail is kept so word endings aren't chopped.
* While less than ``short_utterance_ms`` has been voiced, the end-of-speech
  silence is extended, since people pause after a short start ("um...").
* ``no_speech_timeout_s`` ends the wait when nobody speaks;
  ``max_utterance_s`` caps runaway captures.
* While the user pauses (``pause_ms``), what they've said so far can be read
  (``speech_so_far``) before the utterance is closed, and ``sound_frames``
  tells whether anything but silence came after it (the voice tone reads the
  utterance during the end-of-speech wait, see ``octavia.emotion``).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

import numpy as np


@dataclass(frozen=True)
class EndpointerSettings:
    threshold: float = 0.5
    neg_threshold: float = 0.35
    min_speech_ms: int = 120
    end_silence_ms: int = 700
    pre_roll_ms: int = 400
    max_utterance_s: float = 30.0
    no_speech_timeout_s: float = 6.0
    tail_ms: int = 200
    # Short starts ("um...") get extra patience before the utterance is closed.
    short_utterance_ms: int = 1000
    short_utterance_extra_ms: int = 400


class EndpointKind(str, Enum):
    SPEECH_STARTED = "speech_started"
    UTTERANCE = "utterance"
    NO_SPEECH = "no_speech"


@dataclass(frozen=True)
class EndpointEvent:
    kind: EndpointKind
    audio: np.ndarray | None = None  # float32 mono, for UTTERANCE
    truncated: bool = False

    @property
    def duration_s(self) -> float:
        return 0.0 if self.audio is None else len(self.audio) / 16000


class _State(Enum):
    WAITING = 0
    TRIGGERED = 1  # above threshold, not yet confirmed as real speech
    SPEAKING = 2
    DONE = 3


class Endpointer:
    def __init__(self, settings: EndpointerSettings, sample_rate: int = 16000, frame_samples: int = 512) -> None:
        self.s = settings
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.frame_ms = 1000.0 * frame_samples / sample_rate
        self._pre_roll: deque[np.ndarray] = deque(maxlen=max(1, round(settings.pre_roll_ms / self.frame_ms)))
        self.reset()

    def reset(self, no_speech_timeout_s: float | None = None, pre_roll: bool = True) -> None:
        """Start waiting for a new utterance.

        ``pre_roll=False`` starts with an empty pre-roll buffer (use right after
        a wake word so the wake phrase itself isn't included).
        """
        self._timeout_ms = 1000.0 * (
            no_speech_timeout_s if no_speech_timeout_s is not None else self.s.no_speech_timeout_s
        )
        self._state = _State.WAITING
        if not pre_roll:
            self._pre_roll.clear()
        self._frames: list[np.ndarray] = []
        self._waited_ms = 0.0
        self._voiced_ms = 0.0
        self._silence_ms = 0.0
        self._total_ms = 0.0
        self._seeded_ms = 0.0
        self._silent_frames = 0
        self._sound_frames = 0

    @property
    def speaking(self) -> bool:
        return self._state is _State.SPEAKING

    @property
    def done(self) -> bool:
        return self._state is _State.DONE

    @property
    def pause_ms(self) -> float:
        """How long the user has been quiet in the middle of (or at the end of) an utterance; 0 when they're talking
        or no utterance has started."""
        return self._silence_ms if self._state is _State.SPEAKING else 0.0

    @property
    def needed_silence_ms(self) -> float:
        """The silence that ends the utterance as it stands: ``end_silence_ms``, plus the extra for a short start."""
        needed = float(self.s.end_silence_ms)
        if self._voiced_ms < self.s.short_utterance_ms:
            needed += self.s.short_utterance_extra_ms
        return needed

    @property
    def silence_ms(self) -> float:
        """The silence at the end so far; once the utterance has ended, the silence that ended it."""
        return self._silence_ms

    @property
    def sound_frames(self) -> int:
        """Frames heard above ``neg_threshold`` since the capture began: when it hasn't moved between two looks, only
        silence came in between. Kept after the utterance ends, until ``reset``."""
        return self._sound_frames

    def speech_so_far(self) -> np.ndarray:
        """The utterance as it would end right now (trimmed the way ``_finish`` trims it), without ending it."""
        frames = self._trimmed()
        return np.concatenate(frames).astype(np.float32, copy=False) if frames else np.zeros(0, np.float32)

    def process(self, frame: np.ndarray, prob: float) -> EndpointEvent | None:
        if self._state is _State.DONE:
            return None

        if self._state is _State.WAITING:
            self._pre_roll.append(frame)
            self._waited_ms += self.frame_ms
            if prob >= self.s.threshold:
                self._state = _State.TRIGGERED
                self._frames = list(self._pre_roll)
                self._pre_roll.clear()
                self._voiced_ms = self.frame_ms
                self._silence_ms = 0.0
                self._silent_frames = 0
                self._total_ms = self.frame_ms * len(self._frames)
                self._seeded_ms = self._total_ms  # already counted in _waited_ms
                return self._maybe_confirm()
            if self._waited_ms >= self._timeout_ms:
                self._state = _State.DONE
                return EndpointEvent(EndpointKind.NO_SPEECH)
            return None

        # TRIGGERED or SPEAKING
        self._frames.append(frame)
        self._total_ms += self.frame_ms
        if prob >= self.s.neg_threshold:
            self._sound_frames += 1
            if prob >= self.s.threshold:
                self._voiced_ms += self.frame_ms
            self._silence_ms = 0.0
            self._silent_frames = 0
        else:
            self._silence_ms += self.frame_ms
            self._silent_frames += 1

        if self._state is _State.TRIGGERED:
            event = self._maybe_confirm()
            if event is not None:
                return event
            # Time spent unconfirmed still counts toward the no-speech timeout, so steady
            # murmur (a TV in the next room) can't hold the capture open forever.
            unconfirmed_ms = self._total_ms - self._seeded_ms
            if self._waited_ms + unconfirmed_ms >= self._timeout_ms:
                self._state = _State.DONE
                self._frames = []
                return EndpointEvent(EndpointKind.NO_SPEECH)
            if self._silence_ms >= self.s.end_silence_ms:
                # Too short to be real speech: go back to waiting (keep the timeout running).
                self._state = _State.WAITING
                self._waited_ms += unconfirmed_ms
                for f in self._frames[-self._pre_roll.maxlen :]:
                    self._pre_roll.append(f)
                self._frames = []
            return None

        # SPEAKING
        if self._silence_ms >= self.needed_silence_ms:
            return self._finish(truncated=False)
        if self._total_ms >= 1000.0 * self.s.max_utterance_s:
            return self._finish(truncated=True)
        return None

    def _maybe_confirm(self) -> EndpointEvent | None:
        if self._state is _State.TRIGGERED and self._voiced_ms >= self.s.min_speech_ms:
            self._state = _State.SPEAKING
            return EndpointEvent(EndpointKind.SPEECH_STARTED)
        return None

    def _trimmed(self) -> list[np.ndarray]:
        """The frames so far, keeping only ``tail_ms`` of the trailing silence."""
        tail_frames = max(1, round(self.s.tail_ms / self.frame_ms))
        drop = max(0, self._silent_frames - tail_frames)
        return self._frames[: len(self._frames) - drop] if drop else self._frames

    def _finish(self, truncated: bool) -> EndpointEvent:
        self._state = _State.DONE
        frames = self._trimmed()
        audio = np.concatenate(frames).astype(np.float32, copy=False) if frames else np.zeros(0, np.float32)
        self._frames = []
        return EndpointEvent(EndpointKind.UTTERANCE, audio=audio, truncated=truncated)
