"""Drum parts: a genre's groove played bar after bar, with fills where sections end, crashes where they start,
trap hi-hat rolls and small variations, all seeded.

A groove is the genre's step patterns (``presets``); choruses and drops add their extra drums. Quieter sections
("lite": intros, bridges, outros) keep the hats and a kick on the downbeat. A section's last bar ends in a fill in
the genre's style, longer at higher complexity, and long sections get a short fill every eight bars so they don't
loop stiffly. Ghost notes (``o``) only play at complexity 3 and up. Meters other than 4/4 get a groove built from
their beats (``meter_groove``).

Everything is on drum channel 10 with General MIDI drum notes (kick 36, snare 38, closed hat 42, ...).
"""

from __future__ import annotations

import random

from octavia.music.feel import BeatNote
from octavia.music.meter import STEP, Meter
from octavia.music.presets import DRUM_NOTES, DRUM_VELOCITY, Genre, Role

HITS = 0.2  # how long a drum note lasts (beats): drums are one-shots, so it hardly matters
_HATS = ("hat", "ohat", "ride", "shaker", "tamb", "pedal")
_FILL_LENGTH = {1: (0,), 2: (1,), 3: (1, 2), 4: (2,), 5: (2, 4)}


def meter_groove(meter: Meter) -> dict[str, str]:
    """A plain groove for any meter: kick on the downbeat, snare on the backbeats, hats on the eighths."""
    steps = meter.steps
    pulses = [round(p / STEP) for p in meter.pulses]
    strong = sorted(round(s / STEP) for s in meter.strong)
    kick, snare, hat = ["."] * steps, ["."] * steps, ["."] * steps
    kick[0] = "X"
    if len(pulses) == 3:  # waltz: boom, chick, chick
        snare[pulses[1]] = snare[pulses[2]] = "v"
    elif len(pulses) % 2 == 0:
        for index in range(1, len(pulses), 2):
            snare[pulses[index]] = "x"
        for at in strong[1:]:
            kick[at] = "x"
    else:
        snare[pulses[-1]] = "x"
        if len(strong) > 1:
            snare[strong[1]] = "x"
    for step in range(0, steps, 2):
        hat[step] = "x" if step in pulses else "v"
    return {"kick": "".join(kick), "snare": "".join(snare), "hat": "".join(hat)}


def groove_for(genre: Genre, meter: Meter, role: Role) -> dict[str, str]:
    groove = dict(genre.groove) if meter.is_common else meter_groove(meter)
    if role.name in ("chorus", "drop") and meter.is_common:
        for drum, pattern in genre.chorus_groove.items():
            if pattern:
                groove[drum] = pattern
            else:
                groove.pop(drum, None)
    if role.drums == "lite":
        kept = {d: p for d, p in groove.items() if d in _HATS or d == "rim"}
        kept["kick"] = "X" + "." * (meter.steps - 1)
        groove = kept
    return groove


def _bar_pattern(pattern: str, bar: int, steps: int) -> str:
    """The steps of ``pattern`` for bar number ``bar``: a two-bar pattern alternates, a short one is padded."""
    if len(pattern) >= steps * 2 and len(pattern) % steps == 0:
        count = len(pattern) // steps
        return pattern[(bar % count) * steps : (bar % count + 1) * steps]
    return (pattern + "." * steps)[:steps]


def _velocity(char: str, energy: float) -> int:
    return int(min(127, DRUM_VELOCITY[char] * (0.8 + 0.22 * energy)))


def _roll(start: float, length: float, pitch: int, count: int, low: int, high: int) -> list[BeatNote]:
    """``count`` hits evenly through ``length`` beats, getting louder."""
    step = length / count
    return [
        BeatNote(start + i * step, min(HITS, step), pitch, int(low + (high - low) * i / max(1, count - 1)))
        for i in range(count)
    ]


def _fill(style: str, start: float, length: float, rng: random.Random, complexity: int) -> list[BeatNote]:
    """The fill's own notes, from ``start`` for ``length`` beats."""
    d = DRUM_NOTES
    notes: list[BeatNote] = []
    if style == "toms":
        count = int(length * 4)
        toms = [
            d["snare"],
            d["tom_high"],
            d["tom_high"],
            d["tom_mid"],
            d["tom_mid"],
            d["tom_low"],
            d["tom_floor"],
            d["tom_floor"],
        ]
        for i in range(count):
            pitch = toms[min(len(toms) - 1, i * len(toms) // count)]
            notes.append(BeatNote(start + i * STEP, HITS, pitch, int(86 + 30 * i / max(1, count - 1))))
        notes.append(BeatNote(start + length - STEP, HITS, d["kick"], 100))
    elif style == "snare_roll":
        if length >= 2:
            half = length / 2
            notes += _roll(start, half, d["snare"], int(half * 2), 60, 85)
            notes += _roll(start + half, half, d["snare"], int(half * 4), 85, 118)
        else:
            notes += _roll(start, length, d["snare"], int(length * 4), 70, 116)
        notes.append(BeatNote(start + length - STEP, HITS, d["clap"], 110))
    elif style == "trap_roll":
        if length >= 2:
            notes += _roll(start, 1.0, d["hat"], rng.choice((6, 8)), 55, 100)
            start, length = start + 1.0, length - 1.0
        notes += _roll(start, length, d["snare"], int(length * rng.choice((6, 8))), 55, 115)
    elif style == "breakbeat":
        count = int(length * 4)
        for i in range(count):
            accent = i % 2 == 0
            notes.append(BeatNote(start + i * STEP, HITS, d["snare"], 112 if accent else 72))
            if i % 4 == 0 and complexity >= 3:
                notes.append(BeatNote(start + i * STEP, HITS, d["kick"], 100))
        notes += _roll(start + length - 0.25, 0.25, d["snare"], 2, 100, 120)
    elif style == "dembow":
        notes += _roll(start + max(0.0, length - 1), min(1.0, length), d["snare"], 4, 80, 118)
        if length >= 2:
            notes.append(BeatNote(start, HITS, d["timbale"], 96))
            notes.append(BeatNote(start + 0.75, HITS, d["timbale"], 104))
    else:  # "stop": everything drops out, then a pickup into the next bar
        notes.append(BeatNote(start + length - 0.5, HITS, d["snare"], 92))
        if complexity >= 4:
            notes.append(BeatNote(start + length - 0.25, HITS, d["kick"], 96))
    return notes


def _clears(style: str) -> set[str]:
    """Which groove drums stop during a fill."""
    if style == "stop":
        return {"*"}
    if style == "toms":
        return {"kick", "snare", "clap", "rim", "hat", "ohat"}
    return {"kick", "snare", "clap", "rim"}


def _hat_rolls(bar_start: float, rng: random.Random, complexity: int, energy: float) -> tuple[set[int], list[BeatNote]]:
    """Trap hi-hat rolls in one 4/4 bar: which beats roll (their plain hats go) and the roll's notes."""
    rolled: set[int] = set()
    notes: list[BeatNote] = []
    chance = 0.2 + 0.12 * complexity
    for beat in (1, 3, 2) if complexity >= 4 else (3, 1):
        if rng.random() < chance and len(rolled) < (2 if complexity >= 4 else 1):
            rolled.add(beat)
            kind = rng.choice(("32nds", "triplets", "half"))
            at = bar_start + beat
            if kind == "32nds":
                notes += _roll(at, 1.0, DRUM_NOTES["hat"], 8, 58, 96)
            elif kind == "triplets":
                notes += _roll(at, 1.0, DRUM_NOTES["hat"], 6, 62, 98)
            else:  # a plain eighth, then a 32nd burst
                notes.append(BeatNote(at, HITS, DRUM_NOTES["hat"], _velocity("x", energy)))
                notes += _roll(at + 0.5, 0.5, DRUM_NOTES["hat"], 4, 60, 100)
    return rolled, notes


def drum_section(
    genre: Genre,
    meter: Meter,
    start: float,
    bars: int,
    role: Role,
    energy: float,
    complexity: int,
    rng: random.Random,
    *,
    crash_start: bool = False,
    fill_end: bool = True,
    ending: bool = False,
    first_bar: int = 0,
) -> list[BeatNote]:
    """The drums for one section starting at beat ``start``. ``first_bar`` keeps two-bar patterns in step across
    sections; ``ending``: the section's last bar is the song's last, so it's a single final hit."""
    groove = groove_for(genre, meter, role)
    bar_length = meter.bar
    steps = meter.steps
    notes: list[BeatNote] = []
    fill_style = genre.fill if meter.is_common else "toms"
    for bar in range(bars):
        bar_start = start + bar * bar_length
        last = bar == bars - 1
        if last and ending:
            notes.append(BeatNote(bar_start, HITS, DRUM_NOTES["kick"], _velocity("X", energy)))
            notes.append(BeatNote(bar_start, HITS, DRUM_NOTES["crash"], _velocity("X", energy)))
            continue
        fill_beats = 0.0
        if last and fill_end:
            fill_beats = float(rng.choice(_FILL_LENGTH[complexity]))
            if role.drums == "build":
                fill_beats = bar_length if complexity >= 2 else 2.0
        elif complexity >= 3 and bars > 8 and bar % 8 == 7:
            fill_beats = 1.0  # a short fill to mark each eight bars of a long section
        fill_beats = min(fill_beats, bar_length)
        fill_from = bar_start + bar_length - fill_beats
        cleared = _clears(fill_style) if fill_beats else set()
        rolled: set[int] = set()
        if genre.hat_rolls and meter.is_common and complexity >= 2 and role.drums != "lite":
            rolled, roll_notes = _hat_rolls(bar_start, rng, complexity, energy)
            notes += [n for n in roll_notes if n.start < fill_from or "hat" not in cleared and "*" not in cleared]
        for drum, pattern in groove.items():
            steps_here = _bar_pattern(pattern, first_bar + bar, steps)
            for step, char in enumerate(steps_here):
                if char == "." or (char == "o" and complexity < 3) or char not in DRUM_VELOCITY:
                    continue
                at = bar_start + step * STEP
                if drum == "hat" and int(step * STEP) in rolled:
                    continue
                if drum == "hat" and _bar_pattern(groove.get("ohat", ""), first_bar + bar, steps)[step] not in ".":
                    continue  # an open hat replaces the closed one
                if fill_beats and at >= fill_from - 1e-9 and ("*" in cleared or drum in cleared):
                    continue
                if complexity <= 1 and drum in _HATS and char == "v":
                    continue
                notes.append(BeatNote(at, HITS, DRUM_NOTES[drum], _velocity(char, energy)))
        if complexity >= 4 and not fill_beats and rng.random() < 0.18:  # a pushed extra kick now and then
            free = [
                s
                for s, c in enumerate(_bar_pattern(groove.get("kick", ""), first_bar + bar, steps))
                if c == "." and s % 2
            ]
            if free:
                notes.append(
                    BeatNote(bar_start + rng.choice(free) * STEP, HITS, DRUM_NOTES["kick"], _velocity("v", energy))
                )
        if bar == 0 and crash_start:
            notes.append(BeatNote(bar_start, HITS, DRUM_NOTES["crash"], _velocity("X", energy)))
            if not any(n.start == bar_start and n.pitch == DRUM_NOTES["kick"] for n in notes):
                notes.append(BeatNote(bar_start, HITS, DRUM_NOTES["kick"], _velocity("x", energy)))
        if fill_beats:
            notes += _fill(fill_style, fill_from, fill_beats, rng, complexity)
    return notes


def kick_steps(genre: Genre, meter: Meter, role: Role, bar: int) -> list[int]:
    """Where the kick plays in a bar (sixteenth steps), for a bass that follows it."""
    pattern = groove_for(genre, meter, role).get("kick", "X")
    return [s for s, c in enumerate(_bar_pattern(pattern, bar, meter.steps)) if c not in ".o"]
