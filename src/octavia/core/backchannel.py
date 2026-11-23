"""A soft "mhm" while the user pauses mid-thought, the way a person shows they're still listening.

When the user trails off ("set a timer for, um"), the assistant keeps listening (``core/pauses.py``), and the pause
used to be silent: nothing told them she was still there, so they'd repeat themselves or give up. A backchannel is
said once per utterance, ``AFTER_MS`` into a pause whose early transcript (``octavia.stt.early``) sounds unfinished,
quietly, from the phrase cache (no network wait). A finished sentence never gets one: it gets an answer.
"""

from __future__ import annotations

import random

import numpy as np

from octavia.core.phrases import PhraseCache

PHRASES = ("Mhm.", "Mm-hm.", "Uh-huh.")
AFTER_MS = 700.0  # into the pause: past a breath, well before the full end-of-speech wait
VOLUME = 0.6  # of a normal phrase: under the user's next words, not over them


class Backchannel:
    """Picks a cached "mhm" and says when one is due. Registers its phrases with the cache."""

    def __init__(
        self, cache: PhraseCache, rng: random.Random | None = None, phrases: tuple[str, ...] = PHRASES
    ) -> None:
        self.cache = cache
        self.phrases = phrases
        self._rng = rng or random.Random()
        self._last: str | None = None
        self._said_at: int | None = None  # the endpointer's sound_frames when one was said (one per pause)
        self.said = 0  # in the capture running
        cache.add(phrases)

    def reset(self) -> None:
        """A new capture."""
        self._said_at = None
        self.said = 0

    def due(self, pause_ms: float, sound_frames: int, unfinished: bool, end_ms: float = float("inf")) -> bool:
        """Whether to say one now: far enough into a pause that sounds unfinished (``AFTER_MS``, or a little before
        ``end_ms``, the silence that ends the utterance, when that's shorter), and none said in this pause. Two at most
        per utterance: more would sound like nagging."""
        after = min(AFTER_MS, end_ms - 150.0)
        return unfinished and pause_ms >= after and self._said_at != sound_frames and self.said < 2

    def choose(self, sound_frames: int) -> tuple[str, np.ndarray] | None:
        """A phrase and its (quieter) audio; None when none is cached yet. Counts it as said."""
        clips = {p: clip for p in self.phrases if (clip := self.cache.get(p)) is not None}
        if not clips:
            return None
        names = list(clips)
        if len(names) > 1 and self._last in names:
            names.remove(self._last)
        phrase = self._rng.choice(names)
        self._last = phrase
        self._said_at = sound_frames
        self.said += 1
        return phrase, (clips[phrase] * VOLUME).astype(np.float32, copy=False)
