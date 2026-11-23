"""How she's speaking right now: her pace and whether she whispers, for more than one sentence at a time.

A tone cue such as "[whispering]" only colours the sentence it starts (each sentence is synthesized on its own, and
the chunker keeps one cue per sentence), so "whisper it, and slower" used to go back to her normal voice after the first
sentence. ``SpeakingStyle`` holds a delivery the voice applies to every sentence it makes until it's set back to
normal, or until what it was set for is over: one reply (the user's turn) or the conversation. The
``speaking_style`` tool sets it; nothing is saved, so a restart is always her normal voice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

MIN_SPEED = 0.5
MAX_SPEED = 1.5
WHISPER_CUE = "whispering"


@dataclass(frozen=True)
class Delivery:
    speed: float = 1.0  # times her configured pace
    whisper: bool = False

    @property
    def normal(self) -> bool:
        return self.speed == 1.0 and not self.whisper

    def describe(self) -> str:
        """How she's speaking, after "I'm speaking": "normally", "in a whisper, at 80% of my usual pace"."""
        if self.normal:
            return "normally"
        parts = ["in a whisper"] if self.whisper else []
        if self.speed != 1.0:
            parts.append(f"at {round(self.speed * 100)}% of my usual pace")
        return ", ".join(parts)


NORMAL = Delivery()


def clamp_speed(speed: float) -> float:
    return round(min(MAX_SPEED, max(MIN_SPEED, float(speed))), 2)


class SpeakingStyle:
    """The delivery every sentence of hers is made with, shared by the tool and the voice.

    ``lasts`` (given to ``set``) is read when the delivery is set and again before each sentence: once it returns
    something else (another turn's id, another conversation's), the delivery is over and she's back to normal.
    """

    def __init__(self) -> None:
        self._delivery = NORMAL
        self._lasts: Callable[[], object] | None = None
        self._key: object = None

    def current(self) -> Delivery:
        if self._lasts is not None and not self._still_on():
            log.info("Speaking normally again: what speaking %s was set for is over", self._delivery.describe())
            self.reset()
        return self._delivery

    def set(self, delivery: Delivery, lasts: Callable[[], object] | None = None) -> Delivery:
        delivery = Delivery(clamp_speed(delivery.speed), delivery.whisper)
        if delivery.normal:
            self.reset()
            return NORMAL
        self._delivery, self._lasts = delivery, lasts
        self._key = lasts() if lasts is not None else None
        return delivery

    def reset(self) -> None:
        self._delivery, self._lasts, self._key = NORMAL, None, None

    def _still_on(self) -> bool:
        try:
            return self._lasts() == self._key  # type: ignore[misc]
        except Exception:
            log.debug("Couldn't tell whether the speaking style still applies; ending it", exc_info=True)
            return False


def whispered(text: str) -> str:
    """``text`` with the whisper cue in front, unless it's already there."""
    if f"[{WHISPER_CUE}]" in text.lower():
        return text
    return f"[{WHISPER_CUE}] {text}"
