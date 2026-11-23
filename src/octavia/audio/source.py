"""Microphone input interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import numpy as np


class AudioSource(ABC):
    """Yields 512-sample, 16 kHz, mono float32 frames forever (until closed)."""

    async def start(self) -> None:
        pass

    @abstractmethod
    def frames(self) -> AsyncIterator[np.ndarray]: ...

    async def close(self) -> None:
        pass
