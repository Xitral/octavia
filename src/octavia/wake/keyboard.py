"""Development wake "word": press Enter in the terminal.

Lets you test the whole pipeline (VAD, STT, brain, TTS, barge-in) before a
wake-word model is set up. Not usable when running headless.
"""

from __future__ import annotations

import sys
import threading

import numpy as np

from octavia.wake.base import WakeDetection, WakeWordDetector, WakeWordError


class KeyboardWake(WakeWordDetector):
    name = "keyboard"

    def __init__(self, phrase: str) -> None:
        super().__init__(phrase)
        self._pressed = threading.Event()
        self._thread: threading.Thread | None = None

    def load(self) -> None:
        if sys.stdin is None or not sys.stdin.isatty():
            raise WakeWordError("The keyboard wake engine needs an interactive terminal (press Enter to talk).")
        self._thread = threading.Thread(target=self._read_keys, name="keyboard-wake", daemon=True)
        self._thread.start()

    def _read_keys(self) -> None:
        while True:
            line = sys.stdin.readline()
            if not line:  # stdin closed
                return
            self._pressed.set()

    def process(self, frame: np.ndarray) -> WakeDetection | None:
        if self._pressed.is_set():
            self._pressed.clear()
            return WakeDetection(phrase="(Enter key)")
        return None

    def reset(self) -> None:
        self._pressed.clear()
