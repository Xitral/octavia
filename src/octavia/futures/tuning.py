"""What she does differently on a turn: a variant of one of her experiments on herself, or a habit she kept from one.

The assistant asks ``Tuner.for_turn`` while it writes a turn's notes, on the event loop, so nothing here touches the
database: it reads a snapshot the Futures worker hands over (``update``) whenever the running experiments or the
habits change. A turn that takes part in an experiment gets its variant at random (a coin per experiment, so neither
group is picked by what she expects of the turn), and the note that variant has (``habit_rules.NOTES``); the
(what, variant) pairs ride on the turn's ``BrainStarted`` to Futures, which counts how the reply went for each group
once the user's reaction is in (``selfscore``).

The same words asked again within ``SAME_TURN_S`` get the same decision: an early start in the user's pause and the
turn itself must render the same notes.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from octavia.core.events import BrainStarted, Event, ToneDetected
from octavia.core.text import normalize_text
from octavia.futures.habit_rules import (
    ANSWER_FIRST,
    BRIEF_WHEN_STRESSED,
    NOTES,
    STRESSED_TONES,
    TONE_FRESH_S,
    TURN_TEMPLATES,
)

USER_SOURCES = frozenset({"voice", "text", "discord", "window"})
SAME_TURN_S = 15.0  # an early start and its turn are seconds apart; a repeat later than this is a new unit


@dataclass(frozen=True)
class TuningSnapshot:
    """What applies to turns right now: running experiments on her turns (id, template, variants) and active habits
    from them (id, template, variant)."""

    experiments: tuple[tuple[int, str, tuple[str, ...]], ...] = ()
    habits: tuple[tuple[int, str, str], ...] = ()


@dataclass(frozen=True)
class TurnTuning:
    notes: tuple[str, ...] = ()
    tuning: tuple[tuple[str, str], ...] = ()  # ("experiment:3", "brief"), ("habit:1", "answer_first")


NONE = TurnTuning()


class Tuner:
    def __init__(
        self,
        *,
        min_confidence: float = 0.55,
        clock: Callable[[], float] = time.monotonic,
        coin: Callable[[], float] = random.random,
    ) -> None:
        self.min_confidence = min_confidence
        self.clock = clock
        self.coin = coin
        self.snapshot = TuningSnapshot()
        self._tone: tuple[float, str, float] | None = None  # (when, tone, confidence) of the utterance being answered
        self._decided: dict[tuple[str, str], tuple[float, TurnTuning]] = {}

    def update(self, snapshot: TuningSnapshot) -> None:
        """A new snapshot, from any thread (it replaces the old one whole)."""
        self.snapshot = snapshot

    def on_event(self, event: Event) -> None:
        """The event bus subscriber (ToneDetected, BrainStarted): how the user sounded in what's being answered."""
        if isinstance(event, ToneDetected):
            self._tone = (self.clock(), event.tone, event.confidence)
        elif isinstance(event, BrainStarted):
            self._tone = None  # that tone belonged to the turn that just started

    def stressed(self) -> bool:
        tone = self._tone
        return (
            tone is not None
            and self.clock() - tone[0] <= TONE_FRESH_S
            and tone[1] in STRESSED_TONES
            and tone[2] >= self.min_confidence
        )

    def for_turn(self, text: str, source: str) -> TurnTuning:
        """The notes and variants of this turn: habits first, then the running experiments it takes part in."""
        if source not in USER_SOURCES:
            return NONE
        snapshot = self.snapshot
        if not snapshot.experiments and not snapshot.habits:
            return NONE
        now = self.clock()
        key = (normalize_text(text), source)
        self._decided = {k: v for k, v in self._decided.items() if now - v[0] <= SAME_TURN_S}
        if key in self._decided:
            return self._decided[key][1]
        stressed = source == "voice" and self.stressed()
        notes: list[str] = []
        tuning: list[tuple[str, str]] = []
        kept = set()
        for habit_id, template, variant in snapshot.habits:
            if template in kept or not _applies(template, stressed):
                continue
            kept.add(template)
            tuning.append((f"habit:{habit_id}", variant))
            if (note := NOTES.get((template, variant))) is not None:
                notes.append(note)
        for experiment_id, template, variants in snapshot.experiments:
            if template in kept or not variants or not _applies(template, stressed):
                continue
            variant = variants[min(int(self.coin() * len(variants)), len(variants) - 1)]
            tuning.append((f"experiment:{experiment_id}", variant))
            if (note := NOTES.get((template, variant))) is not None:
                notes.append(note)
        decided = TurnTuning(tuple(notes), tuple(tuning)) if tuning else NONE
        self._decided[key] = (now, decided)
        return decided


def _applies(template: str, stressed: bool) -> bool:
    """Whether a turn takes part in this kind of experiment or habit."""
    if template not in TURN_TEMPLATES:
        return False
    if template == BRIEF_WHEN_STRESSED:
        return stressed
    return template == ANSWER_FIRST
