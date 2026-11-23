"""How the notes are played rather than which notes: swing, strummed chords and humanizing.

- Swing delays every second sixteenth (or eighth) in each pair, MPC style: 50% is straight, 66% a triplet shuffle.
  It's a smooth warp of time within each pair, so note ends move with their starts and nothing overlaps.
- Strumming rolls a chord's notes from the bottom up by a few milliseconds, as a hand on keys or strings does.
- Humanizing nudges each note's timing and loudness a little at random (seeded, so a seed always gives the same
  file), and lays a hip-hop snare back a touch. At 0 nothing moves: every note sits exactly on the grid (swung, if
  the song swings).

Timing amounts are in milliseconds and converted with the tempo, so a slow song isn't pushed around more than a
fast one.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class BeatNote:
    """A note with its timing in beats (quarter notes)."""

    start: float
    duration: float
    pitch: int
    velocity: int

    @property
    def end(self) -> float:
        return self.start + self.duration


def ms_to_beats(ms: float, bpm: float) -> float:
    return ms / 1000.0 * bpm / 60.0


def swing_time(t: float, percent: float, grid: float) -> float:
    """Where time ``t`` (beats) lands with ``percent`` swing on a ``grid`` (0.25 sixteenths, 0.5 eighths)."""
    ratio = percent / 100.0
    pair = 2 * grid
    base = math.floor(t / pair + 1e-9) * pair
    into = t - base
    if into <= grid:
        return base + into * (pair * ratio) / grid
    return base + pair * ratio + (into - grid) * (pair * (1 - ratio)) / grid


def apply_swing(notes: list[BeatNote], percent: float, grid: float) -> None:
    if abs(percent - 50) < 1e-6:
        return
    for note in notes:
        start = swing_time(note.start, percent, grid)
        end = swing_time(note.end, percent, grid)
        note.start, note.duration = start, max(end - start, 0.02)


def strum(chord_notes: list[BeatNote], seconds: float, bpm: float) -> None:
    """Roll one chord's notes (all starting together) from the lowest up, ``seconds`` apart."""
    step = ms_to_beats(seconds * 1000, bpm)
    for index, note in enumerate(sorted(chord_notes, key=lambda n: n.pitch)):
        shift = index * step
        note.start += shift
        note.duration = max(note.duration - shift, 0.05)


@dataclass(frozen=True)
class Touch:
    """How much one part is humanized at full amount."""

    timing_ms: float = 12.0
    velocity: float = 9.0
    length: float = 0.05  # +- this fraction of each note's length
    late_ms: dict[int, float] | None = None  # pitch -> laid back by this much (a lazy hip-hop snare)


def humanize(notes: list[BeatNote], amount: float, bpm: float, rng: random.Random, touch: Touch | None = None) -> None:
    """Nudge timing, velocity and length; ``amount`` 0 (exact) .. 1 (loose, like a live player)."""
    if amount <= 0:
        return
    touch = touch or Touch()
    sigma = ms_to_beats(touch.timing_ms * amount, bpm)
    limit = 2.5 * sigma
    for note in notes:
        shift = max(-limit, min(limit, rng.gauss(0.0, sigma))) if sigma else 0.0
        if touch.late_ms and note.pitch in touch.late_ms:
            shift += ms_to_beats(touch.late_ms[note.pitch] * amount, bpm)
        note.start = max(0.0, note.start + shift)
        if touch.length:
            note.duration = max(0.02, note.duration * (1 + max(-0.2, min(0.2, rng.gauss(0.0, touch.length * amount)))))
        velocity = note.velocity + rng.gauss(0.0, touch.velocity * amount)
        note.velocity = int(max(1, min(127, round(velocity))))
