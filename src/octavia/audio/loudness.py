"""Hearing a sudden bang (a door slamming, a pan dropped) in the microphone frames the wake-word detector already
gets, so the avatar can jump at it.

Only the loudness of each frame is looked at, here on the PC: nothing is kept, recorded or sent anywhere. A bang
comes out of the quiet all at once, with loud samples, and has died down again a moment later; someone starting to
talk gets loud as quickly but stays loud, so it isn't one.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

import numpy as np

from octavia.audio.types import rms_dbfs

RISE_DB = 25.0  # a frame this far above the room's background, straight out of it
QUIET_DB = 6.0  # "straight out of it": the frame before was at most this far above the background
PEAK_DBFS = -10.0  # with its loudest sample at least this loud
FALL_DB = 15.0  # and this long after it's this far below its loudest again: a bang, not a voice
FALL_S = 0.15
BACKGROUND_S = 4.0  # the background follows the room's level over about this long
REST_S = 30.0  # at most one bang every this long


class BangDetector:
    """Feed it every frame (float32 in [-1, 1], ``frame_s`` seconds each); ``process`` says when one ended a bang
    (a moment after it, once it has died down)."""

    def __init__(self, frame_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._frame_s = frame_s
        self._clock = clock
        self._background: float | None = None  # dBFS
        self._previous = -120.0  # the last frame's level
        self._loudest: float | None = None  # while a bang may be under way: its loudest frame so far
        self._left = 0  # ...and the frames left before it must have died down
        self._last_bang = -math.inf

    def reset(self) -> None:
        """Frames stopped coming for a while (a conversation): start over, keeping only the time of the last bang."""
        self._background = None
        self._previous = -120.0
        self._loudest = None

    def process(self, frame: np.ndarray) -> bool:
        level = rms_dbfs(frame)
        bang = False
        if self._background is None:
            self._background = level
        elif self._loudest is not None:
            self._left -= 1
            if self._left > 0:
                self._loudest = max(self._loudest, level)
            else:
                now = self._clock()
                bang = level <= self._loudest - FALL_DB and now - self._last_bang >= REST_S
                if bang:
                    self._last_bang = now
                self._loudest = None
        elif self._starts_bang(frame, level):
            self._loudest = level
            self._left = max(1, round(FALL_S / self._frame_s))
        if self._background is not None:
            # The background follows the room slowly, and a loud moment hardly moves it.
            step = min(1.0, self._frame_s / BACKGROUND_S)
            self._background += step * (min(level, self._background + 3.0) - self._background)
        self._previous = level
        return bang

    def _starts_bang(self, frame: np.ndarray, level: float) -> bool:
        assert self._background is not None
        if level - self._background < RISE_DB or self._previous - self._background > QUIET_DB:
            return False
        peak = float(np.max(np.abs(frame))) if frame.size else 0.0
        return 20.0 * math.log10(max(peak, 1e-6)) >= PEAK_DBFS
