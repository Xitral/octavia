"""Speech-to-text interface."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None = None
    duration_s: float = 0.0  # audio length
    latency_s: float = 0.0  # processing time


class SpeechToText(ABC):
    name: str = "stt"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def load(self) -> None:
        """Load the model (blocking; called once from a worker thread)."""

    @abstractmethod
    def transcribe_sync(self, audio: np.ndarray) -> TranscriptionResult:
        """Transcribe 16 kHz mono float32 audio. Blocking."""

    @property
    def busy(self) -> bool:
        """A transcription is running (or waiting for the model): extra listening can wait its turn."""
        return self._lock.locked()

    async def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        async with self._lock:  # models are not assumed to be thread-safe
            return await asyncio.to_thread(self.transcribe_sync, audio)

    def close(self) -> None:
        pass
