"""Wake-word detector interface.

The wake phrase always comes from config (``assistant.wake_phrase``). Engines
that can't handle arbitrary phrases (e.g. a trained openWakeWord model) must
say so clearly in their setup errors instead of silently ignoring the phrase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class WakeDetection:
    phrase: str
    score: float | None = None
    # Seconds of audio after the wake phrase that the detector had already consumed
    # when it fired (detection lags the phrase). The orchestrator replays that much
    # recent audio into the capture so "hey computer what time is it" isn't clipped.
    trailing_s: float = 0.0
    # Heard by speech recognition when the detector missed it (``wake/second_chance.py``), with what the user said
    # after the name in the same breath ("" for the name alone): that is their request, already transcribed.
    second_chance: bool = False
    heard: str = ""


class WakeWordError(RuntimeError):
    """Setup problem the user must fix (missing model, unsupported phrase, ...)."""


class WakeWordDetector(ABC):
    name: str = "wake"

    def __init__(self, phrase: str) -> None:
        self.phrase = phrase

    def load(self) -> None:
        """Load models (blocking; called once from a worker thread). Raise WakeWordError on setup problems."""

    @abstractmethod
    def process(self, frame: np.ndarray) -> WakeDetection | None:
        """Feed one 512-sample 16 kHz float32 frame. Must be fast (runs per frame)."""

    def reset(self) -> None:
        """Clear internal state/buffers (called when (re)entering wake listening)."""

    def close(self) -> None:
        pass
