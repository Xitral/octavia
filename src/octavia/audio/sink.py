"""Audio output interface used by the speech pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

import numpy as np


class AudioSink(ABC):
    """A queue-backed, interruptible audio output.

    * ``enqueue`` adds float32 mono audio at ``sample_rate``.
    * ``mark(cb)`` schedules ``cb`` (on the event loop) for when playback
      reaches this point, i.e. everything enqueued before it has been heard.
    * ``clear()`` stops sound immediately and drops queued audio and marks.
    * ``overlay(audio)`` plays a short sound (a cue) right away, over whatever is queued, without delaying it or
      its marks; ``set_loop(audio)`` keeps a sound looping under everything, fading in, until ``set_loop(None)``
      fades it out. A sink that can't mix plays an overlay in line and no loop.
    """

    sample_rate: int

    async def start(self) -> None:
        pass

    @abstractmethod
    def enqueue(self, audio: np.ndarray) -> None: ...

    @abstractmethod
    def mark(self, callback: Callable[[], None]) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def pending_seconds(self) -> float: ...

    def overlay(self, audio: np.ndarray) -> None:
        self.enqueue(audio)

    def set_loop(self, audio: np.ndarray | None) -> None:
        pass

    async def close(self) -> None:
        pass
