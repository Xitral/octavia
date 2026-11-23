"""Chord voicings with smooth voice leading: which notes of a chord to play, in which octaves, so that each chord
moves as little as possible from the one before.

For each chord every way of stacking its tones in close position (each inversion, from each octave in range) is a
candidate, with its "drop 2" spread (the second voice from the top dropped an octave) too. The chosen one is the
cheapest by:

- movement: the voices' total distance from the previous voicing's (each voice to its nearest neighbor when the
  counts differ), which is what makes a progression sound connected instead of jumping around;
- staying near the part's register (its center), so a long progression doesn't drift up or down;
- no mud: thirds and seconds low in the register sound muddy, so close intervals below E3 cost a lot;
- a smooth top line: the highest voice is what's heard as a melody, so it shouldn't leap;
- for pads, openness: spread voicings sound fuller than close ones.

When there are more tones than voices the fifth goes first (it adds the least), then the root when another part
plays it; with fewer, the root and then the fifth are doubled.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass

from octavia.music.theory import Chord


@dataclass(frozen=True)
class Register:
    low: int  # the lowest note a voice may take
    high: int  # the highest
    center: int  # where the voicing's average should sit
    voices: int  # how many notes to play
    spread: bool = False  # prefer open voicings (pads)


CHORDS = Register(low=48, high=79, center=62, voices=4)
PAD = Register(low=50, high=86, center=67, voices=4, spread=True)


def _tone_set(chord: Chord, voices: int, root_elsewhere: bool) -> list[int]:
    """Which pitch classes to play, ``voices`` of them."""
    tones = list(chord.tones)
    intervals = [(t - chord.root) % 12 for t in tones]
    while len(tones) > voices:
        if 7 in intervals and len(tones) > 3:
            drop = intervals.index(7)  # the fifth adds the least
        elif root_elsewhere and 0 in intervals and len(tones) > 3:
            drop = intervals.index(0)  # the bass has it (a "rootless" voicing)
        else:
            drop = len(tones) - 1  # the highest extension
        tones.pop(drop)
        intervals.pop(drop)
    doubling = [chord.root, (chord.root + 7) % 12 if (chord.root + 7) % 12 in tones else chord.root]
    while len(tones) < voices:
        tones.append(doubling[(len(tones) - len(chord.tones)) % 2])
    return tones


def candidates(tones: list[int], register: Register) -> set[tuple[int, ...]]:
    """Every close-position stacking of ``tones`` within the register, plus each one's drop-2 spread."""
    found: set[tuple[int, ...]] = set()
    for order in set(itertools.permutations(tones)):
        first = order[0]
        start = register.low + ((first - register.low) % 12)
        while start <= register.high:
            notes = [start]
            for pc in order[1:]:
                step = (pc - notes[-1]) % 12 or 12  # a doubled tone goes up an octave
                notes.append(notes[-1] + step)
            if notes[-1] <= register.high:
                found.add(tuple(notes))
                if len(notes) >= 4 and notes[-2] - 12 >= register.low:
                    dropped = sorted([*notes[:-2], notes[-2] - 12, notes[-1]])
                    if len(set(dropped)) == len(dropped):  # a doubled tone can't land on its twin
                        found.add(tuple(dropped))
            start += 12
    return found


def _movement(new: tuple[int, ...], old: list[int]) -> float:
    if len(new) == len(old):
        return float(sum(abs(a - b) for a, b in zip(sorted(new), sorted(old), strict=True)))
    return float(sum(min(abs(n - o) for o in old) for n in new))


def _cost(
    voicing: tuple[int, ...], previous: list[int] | None, register: Register, chord: Chord, bass_low: bool
) -> float:
    mean = sum(voicing) / len(voicing)
    cost = 0.35 * abs(mean - register.center)
    for low, high in itertools.pairwise(voicing):
        gap = high - low
        if low < 52 and gap < 5:
            cost += 5.0
        elif low < 57 and gap < 3:
            cost += 2.0
        if gap > 12:
            cost += (gap - 12) * 0.8  # a hole in the middle of the chord
    span = voicing[-1] - voicing[0]
    if register.spread:
        cost += max(0, 14 - span) * 0.45
    elif span > 19:
        cost += (span - 19) * 0.5
    if bass_low and chord.bass != chord.root and voicing[0] % 12 != chord.bass:
        cost += 6.0  # a slash chord or inversion asked for its bass note at the bottom
    if previous:
        cost += _movement(voicing, previous)
        leap = abs(voicing[-1] - max(previous))
        if leap > 4:
            cost += (leap - 4) * 0.6
    return cost


def voice(
    chord: Chord,
    previous: list[int] | None,
    register: Register = CHORDS,
    rng: random.Random | None = None,
    root_elsewhere: bool = True,
) -> list[int]:
    """The best voicing of ``chord`` after ``previous`` (None for the first chord), lowest note first.

    ``root_elsewhere``: another part (the bass) plays the root, so it may be left out of big chords, and an
    inversion's bass note needn't be at the bottom.
    """
    tones = _tone_set(chord, register.voices if len(chord.tones) > 2 else min(3, register.voices), root_elsewhere)
    options = candidates(tones, register)
    if not options:  # a register too narrow for this chord: stack it from the bottom anyway
        options = {tuple(sorted(register.low + ((t - register.low) % 12) for t in tones))}
    jitter = rng.random if rng else (lambda: 0.0)
    return list(min(options, key=lambda v: _cost(v, previous, register, chord, not root_elsewhere) + 0.05 * jitter()))


def power_voicing(chord: Chord, previous: list[int] | None) -> list[int]:
    """Root, fifth and octave from the root nearest the last one (E2 to E3), as a rock guitar plays it."""
    root = 40 + (chord.root - 40) % 12  # E2..D#3
    if previous and abs(root + 12 - previous[0]) < abs(root - previous[0]) and root + 12 <= 52:
        root += 12
    fifth = chord.tones[1] if len(chord.tones) > 1 else (chord.root + 7) % 12
    return [root, root + ((fifth - chord.root) % 12 or 7), root + 12]
