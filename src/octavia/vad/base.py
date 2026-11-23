"""Voice activity detector interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class VoiceActivityDetector(ABC):
    name: str = "vad"

    def load(self) -> None:
        """Load models (blocking)."""

    @abstractmethod
    def probability(self, frame: np.ndarray) -> float:
        """Speech probability for one 512-sample 16 kHz float32 frame."""

    @abstractmethod
    def reset(self) -> None:
        """Clear recurrent state (between utterances)."""
