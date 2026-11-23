"""The pitched parts: bass, chords, pad, arpeggio and melody, each following the song's harmony (a list of
``ChordSpan``: which chord sounds when).

- Bass plays the chord roots in the genre's rhythm (``presets`` step patterns), with fifths, octaves and notes
  leading into the next chord; hip-hop and trap basses play with the kick, an 808 holding until the next one.
  Each root is taken in the octave nearest the last note, so the line moves smoothly.
- Chords play voice-led voicings (``voicing``) in the genre's comping rhythm; a chord change always gets a new
  attack, even inside a held note. Rock plays power chords.
- The pad holds each chord, voiced wide and high, softly.
- The arpeggio runs through the current chord's notes (up, down, up and down, or a "pinky" pattern against the
  lowest note) at the genre's rate.
- The melody is made of phrases (usually four bars) built from a one-bar rhythmic motif, as songs are: the motif
  comes back (A A A C in a chorus, A B A C in a verse), and when it does it keeps its shape, moved to fit the new
  chord. Strong beats and long notes are chord tones; other notes may be passing or neighbor tones, reached and
  left by step. Leaps are followed by a step back the other way, the line arches up and down across each phrase,
  and the last phrase of a section comes home to a stable note (the tonic at the song's end). Each note is
  chosen at random among the good options (seeded), weighted by how well it fits.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import dataclass, field

from octavia.music import voicing
from octavia.music.feel import BeatNote
from octavia.music.grooves import kick_steps
from octavia.music.meter import STEP, Meter
from octavia.music.presets import Genre, Role
from octavia.music.theory import Chord, ChordSpan, Key, power_chord


class Harmony:
    """Which chord sounds when."""

    def __init__(self, spans: list[ChordSpan]) -> None:
        self.spans = sorted(spans, key=lambda s: s.start)
        self._starts = [s.start for s in self.spans]

    def index_at(self, t: float) -> int:
        return max(0, bisect_right(self._starts, t + 1e-6) - 1)

    def at(self, t: float) -> ChordSpan:
        return self.spans[self.index_at(t)]

    def next_after(self, t: float) -> ChordSpan:
        index = self.index_at(t)
        return self.spans[min(index + 1, len(self.spans) - 1)]

    def changes_between(self, start: float, end: float) -> list[float]:
        """Chord changes strictly inside (start, end)."""
        return [s.start for s in self.spans if start + 1e-6 < s.start < end - 1e-6]


@dataclass
class Section:
    """What a part needs to know about the section it plays in."""

    start: float  # beats
    bars: int
    role: Role
    energy: float
    meter: Meter
    first_bar: int = 0  # the section's first bar's number in the song
    ending: bool = False  # the song ends with this section's last bar

    @property
    def end(self) -> float:
        return self.start + self.bars * self.meter.bar

    @property
    def last_bar_start(self) -> float:
        return self.end - self.meter.bar


def _scale_velocity(base: float, energy: float) -> int:
    return int(max(1, min(127, base * (0.72 + 0.32 * energy))))


def _parse_steps(pattern: str, steps: int) -> list[tuple[int, int, str]]:
    """A step pattern as (step, length in steps, token) hits: "R---5-.." -> [(0, 4, "R"), (4, 2, "5")]."""
    pattern = (pattern + "." * steps)[:steps]
    hits = []
    for step, char in enumerate(pattern):
        if char in ".-":
            continue
        length = 1
        while step + length < steps and pattern[step + length] == "-":
            length += 1
        hits.append((step, length, char))
    return hits


# --------------------------------------------------------------------------- bass


def _generic_bass(meter: Meter) -> str:
    steps = meter.steps
    out = ["-"] * steps
    out[0] = "R"
    strong = sorted(round(s / STEP) for s in meter.strong)
    for at in strong[1:]:
        out[at] = "5"
    last = round(meter.pulses[-1] / STEP)
    if last > 0 and out[last] == "-":
        out[last] = "A"
    return "".join(out)


def _kick_bass(genre: Genre, meter: Meter, role: Role, bar: int, rng: random.Random, complexity: int) -> str:
    """A bass pattern that plays with the kick: each note holds until the next kick (an 808) or for at most a
    beat and a half (a bass guitar)."""
    steps = meter.steps
    kicks = kick_steps(genre, meter, role, bar) or [0]
    if 0 not in kicks:
        kicks = [0, *kicks]
    hold = steps if genre.key == "trap" else 6
    out = ["."] * steps
    for index, step in enumerate(kicks):
        token = "R"
        if complexity >= 3 and index > 0 and rng.random() < 0.22:
            token = "O" if genre.key == "trap" else rng.choice("5O")
        out[step] = token
        until = kicks[index + 1] if index + 1 < len(kicks) else steps
        for s in range(step + 1, min(until, step + hold)):
            out[s] = "-"
    return "".join(out)


def _simplify(pattern: str, complexity: int) -> str:
    if complexity <= 1:  # roots on the beats only
        return "".join(
            ("R" if c not in ".-" else c) if i % 4 == 0 else ("-" if c != "." else ".") for i, c in enumerate(pattern)
        )
    if complexity == 2:
        return pattern.replace("A", "R").replace("C", "5")
    return pattern


def _nearest(pc: int, near: int, low: int, high: int) -> int:
    """The note with pitch class ``pc`` closest to ``near``, kept within [low, high]."""
    candidates = [n for n in range(low, high + 1) if n % 12 == pc % 12]
    return min(candidates, key=lambda n: (abs(n - near), n)) if candidates else near


def bass_part(
    genre: Genre, key: Key, harmony: Harmony, section: Section, complexity: int, rng: random.Random
) -> list[BeatNote]:
    meter = section.meter
    low, high = genre.bass_center - 10, genre.bass_center + 12
    notes: list[BeatNote] = []
    previous = genre.bass_center
    staccato = genre.key in ("funk", "house")
    chromatic = genre.color >= 1  # jazzier genres lead in by half step now and then
    for bar in range(section.bars):
        bar_start = section.start + bar * meter.bar
        if section.ending and bar == section.bars - 1:
            root = _nearest(harmony.at(bar_start).chord.bass, previous, low, high)
            notes.append(BeatNote(bar_start, meter.bar * 0.95, root, _scale_velocity(100, section.energy)))
            break
        if genre.bass == "kick" and meter.is_common:
            pattern = _kick_bass(genre, meter, section.role, section.first_bar + bar, rng, complexity)
        elif meter.is_common:
            pattern = _simplify(genre.bass, complexity)
        else:
            pattern = _simplify(_generic_bass(meter), complexity)
        hits = _parse_steps(pattern, meter.steps)
        if complexity >= 4 and hits and rng.random() < 0.5:  # lead into a chord change that the pattern doesn't
            change = harmony.changes_between(bar_start, bar_start + meter.bar + 1e-3)
            target = (change[0] if change else bar_start + meter.bar) - 2 * STEP
            step = round((target - bar_start) / STEP)
            if 0 < step < meter.steps and all(h[0] != step for h in hits):
                hits = [(s, min(n, step - s) if s < step else n, t) for s, n, t in hits] + [(step, 2, "A")]
                hits.sort()
        for step, length, token in hits:
            at = bar_start + step * STEP
            span = harmony.at(at)
            chord = span.chord
            end = min(at + length * STEP, span.end, section.end)
            for cut in harmony.changes_between(at, end):  # a held note restarts on the new chord's root
                pitch = _bass_pitch(token, chord, harmony, at, previous, low, high, key, chromatic)
                notes.append(BeatNote(at, cut - at, pitch, 0))
                at, chord, token = cut, harmony.at(cut).chord, "R"
            pitch = _bass_pitch(token, chord, harmony, at, previous, low, high, key, chromatic)
            duration = (end - at) * (0.6 if staccato and length <= 2 else 0.94)
            notes.append(BeatNote(at, max(duration, 0.1), pitch, 0))
            previous = pitch
    for note in notes:
        strength = meter.strength((note.start - section.start) % meter.bar)
        note.velocity = _scale_velocity(88 + 5 * min(strength, 2), section.energy)
    return notes


def _bass_pitch(
    token: str,
    chord: Chord,
    harmony: Harmony,
    at: float,
    previous: int,
    low: int,
    high: int,
    key: Key,
    chromatic: bool = False,
) -> int:
    root = _nearest(chord.bass if token == "R" else chord.root, previous, low, high - 5)
    if token == "5":
        fifth = (chord.root + 7) % 12 if 7 in chord.intervals or len(chord.tones) < 3 else chord.tones[2]
        up = root + ((fifth - root) % 12)
        return up if up <= high else up - 12
    if token == "O":
        return root + 12 if root + 12 <= high + 5 else root
    if token == "C":
        tone = chord.tones[1] if len(chord.tones) > 1 else chord.root
        return _nearest(tone, root + 4, low, high)
    if token == "A":  # lead into the next chord's root: by step from the side the line comes from
        upcoming = harmony.next_after(at).chord
        target = _nearest(upcoming.bass, previous, low, high)
        rising = target >= previous
        side = -1 if rising else 1
        diatonic = next((target + side * d for d in (1, 2) if (target + side * d) % 12 in key.scale), target + side * 2)
        if chromatic and round(at * 4) % 2:
            return min(high, max(low, target + side))  # a chromatic approach, in the jazzier genres
        return min(high, max(low, diatonic))
    return root


# --------------------------------------------------------------------------- chords and pad


def _generic_comp(meter: Meter) -> str:
    steps = meter.steps
    out = ["-"] * steps
    for pulse in meter.pulses:
        out[round(pulse / STEP)] = "x"
    return "".join(out)


@dataclass
class Voicer:
    """Keeps one part's last voicing so the next chord can be voice-led from it."""

    register: voicing.Register
    power: bool = False
    root_elsewhere: bool = True
    previous: list[int] | None = None
    _cache: dict[int, list[int]] = field(default_factory=dict)

    def voicing_for(self, span_index: int, chord: Chord, rng: random.Random) -> list[int]:
        if span_index not in self._cache:
            if self.power:
                pitches = voicing.power_voicing(power_chord(chord), self.previous)
            else:
                pitches = voicing.voice(chord, self.previous, self.register, rng, self.root_elsewhere)
            self._cache = {span_index: pitches}
            self.previous = pitches
        return self._cache[span_index]


def chord_part(
    genre: Genre,
    harmony: Harmony,
    section: Section,
    complexity: int,
    rng: random.Random,
    voicer: Voicer,
    *,
    pad: bool = False,
) -> list[BeatNote]:
    meter = section.meter
    notes: list[BeatNote] = []
    if pad:  # each chord held for as long as it lasts, across bar lines
        for index, span in enumerate(harmony.spans):
            start, end = max(span.start, section.start), min(span.end, section.end)
            if end - start < 0.05:
                continue
            velocity = _scale_velocity(58, section.energy)
            for pitch in voicer.voicing_for(index, span.chord, rng):
                notes.append(BeatNote(start, (end - start) * 0.98, pitch, velocity))
        return notes
    if complexity <= 1:
        pattern = "x" + "-" * (meter.steps - 1)
    elif meter.is_common:
        pattern = genre.comp
    else:
        pattern = _generic_comp(meter)
    gate = 0.9 if genre.key in ("house", "reggaeton", "funk") else 0.95
    for bar in range(section.bars):
        bar_start = section.start + bar * meter.bar
        hits = _parse_steps(pattern, meter.steps)
        if section.ending and bar == section.bars - 1:
            hits = [(0, meter.steps, "x")]
        # A chord change inside a held hit (or a rest) gets its own attack.
        starts = [bar_start + s * STEP for s, _, _ in hits]
        extra = [
            c
            for c in harmony.changes_between(bar_start - 1e-3, bar_start + meter.bar)
            if all(abs(c - s) > 1e-6 for s in starts)
        ]
        events = [(bar_start + s * STEP, bar_start + (s + n) * STEP) for s, n, _ in hits]
        for change in extra:
            events = [(a, min(b, change) if a < change else b) for a, b in events]
            following = [a for a, _ in events if a > change]
            events.append((change, min(following) if following else bar_start + meter.bar))
        for at, until in sorted(events):
            index = harmony.index_at(at)
            span = harmony.spans[index]
            end = min(until, span.end, section.end)
            if end - at < 0.05:
                continue
            pitches = voicer.voicing_for(index, span.chord, rng)
            accent = 6 if meter.strength(at - bar_start) >= 2 else 0
            velocity = _scale_velocity(76 + accent, section.energy)
            for pitch in pitches:
                notes.append(BeatNote(at, (end - at) * gate, pitch, velocity))
    return notes


# --------------------------------------------------------------------------- arpeggio

ARP_SHAPES = ("up", "updown", "down", "pinky", "converge")


def _arp_sequence(pitches: list[int], shape: str) -> list[int]:
    ordered = sorted(pitches)
    if shape == "down":
        return ordered[::-1]
    if shape == "updown":
        return ordered + ordered[-2:0:-1]
    if shape == "pinky":  # the lowest note against each of the others in turn
        low, rest = ordered[0], ordered[1:] or ordered
        return [x for pair in zip([low] * len(rest), rest, strict=True) for x in pair]
    if shape == "converge":
        out, lo, hi = [], 0, len(ordered) - 1
        while lo <= hi:
            out.append(ordered[lo])
            if lo != hi:
                out.append(ordered[hi])
            lo, hi = lo + 1, hi - 1
        return out
    return ordered


def arpeggio_part(
    genre: Genre, harmony: Harmony, section: Section, complexity: int, rng: random.Random, voicer: Voicer
) -> list[BeatNote]:
    meter = section.meter
    rate = genre.arp_rate * (2 if complexity <= 1 else 1)
    shape = rng.choice(ARP_SHAPES[:3] if complexity <= 2 else ARP_SHAPES)
    notes: list[BeatNote] = []
    t = section.start
    index_in_chord, current = 0, -1
    end = section.last_bar_start + rate if section.ending else section.end
    while t < end - 1e-6:
        index = harmony.index_at(t)
        span = harmony.spans[index]
        if index != current:
            current, index_in_chord = index, 0
            base = voicer.voicing_for(index, span.chord, rng)
            tones = sorted(set(base + [p + 12 for p in base if p + 12 <= 90]))[: 4 + min(complexity, 4)]
            sequence = _arp_sequence(tones, shape)
        pitch = sequence[index_in_chord % len(sequence)]
        strength = meter.strength((t - section.start) % meter.bar)
        velocity = _scale_velocity(70 + 6 * min(strength, 2), section.energy)
        notes.append(BeatNote(t, rate * 0.82, pitch, velocity))
        index_in_chord += 1
        t += rate
    return notes


# --------------------------------------------------------------------------- melody


@dataclass
class Motif:
    """A one-bar rhythm (onset, length in beats) and, once sung, its shape: the steps between its notes along the
    scale."""

    rhythm: list[tuple[float, float]]
    steps: list[int] | None = None
    first_pitch: int | None = None


@dataclass
class MelodyMemory:
    """What the melody remembers across sections: each role's motif (a second verse sings the first's rhythm) and
    whole sections (a chorus over the same chords comes back the same, as a hook should)."""

    motifs: dict[str, Motif] = field(default_factory=dict)
    sections: dict[tuple, list[BeatNote]] = field(default_factory=dict)


def make_rhythm(
    meter: Meter, density: float, grid: float, syncopation: float, rng: random.Random
) -> list[tuple[float, float]]:
    """A one-bar rhythm: onsets chosen with weight by how strong each moment is (anticipations of strong beats
    get extra weight with syncopation), each lasting until the next."""
    count_steps = max(1, round(meter.bar / grid))
    weights = []
    for step in range(count_steps):
        position = step * grid
        strength = meter.strength(position)
        weight = (0.3, 0.55, 0.8, 1.0)[strength]
        upcoming = meter.strength(position + grid) if step + 1 < count_steps else 3
        if strength == 0 and upcoming >= 2:
            weight += syncopation * 0.6  # the sixteenth just before a beat: an anticipation
        weights.append(weight)
    target = max(2, min(count_steps, round(density * meter.bar * rng.uniform(0.8, 1.2))))
    chosen: set[int] = set()
    if rng.random() > syncopation * 0.5:
        chosen.add(0)
    pool = list(range(count_steps))
    while len(chosen) < target:
        step = rng.choices(pool, weights=weights)[0]
        chosen.add(step)
    onsets = sorted(chosen)
    rhythm = []
    for index, step in enumerate(onsets):
        until = onsets[index + 1] * grid if index + 1 < len(onsets) else meter.bar
        length = until - step * grid
        if index + 1 == len(onsets) and rng.random() < 0.5:
            length = min(length, max(grid, 1.0))  # leave a breath before the next bar
        rhythm.append((step * grid, length))
    return rhythm


def _variation(
    rhythm: list[tuple[float, float]], meter: Meter, grid: float, rng: random.Random
) -> list[tuple[float, float]]:
    """The same rhythm with one change at its end: a note dropped, or one split in two."""
    out = list(rhythm)
    if len(out) > 2 and rng.random() < 0.5:
        out.pop()
        onset, _ = out[-1]
        out[-1] = (onset, meter.bar - onset)
    else:
        onset, length = out[-1]
        if length >= 2 * grid:
            half = round(length / 2 / grid) * grid
            out[-1] = (onset, half)
            out.append((onset + half, length - half))
    return out


def _cadence(rhythm: list[tuple[float, float]], meter: Meter) -> list[tuple[float, float]]:
    """A phrase ending: the motif's first half, then a long note on the bar's middle strong beat (or its start)."""
    middle = max((s for s in meter.strong if s > 0), default=0.0)
    if middle <= 0:
        return [(0.0, meter.bar * 0.9)]
    head = [(o, min(length, middle - o)) for o, length in rhythm if o < middle]
    return (
        [*head, (middle, (meter.bar - middle) * 0.9)] if head else [(0.0, middle), (middle, (meter.bar - middle) * 0.9)]
    )


@dataclass
class _Line:
    key: Key
    meter: Meter
    harmony: Harmony
    low: int
    high: int
    center: int
    rng: random.Random
    complexity: int
    notes: list[BeatNote] = field(default_factory=list)
    _last_interval: int = 0
    _last_outside: bool = False

    def __post_init__(self) -> None:
        self.scale_notes = [n for n in range(self.low, self.high + 1) if n % 12 in self.key.scale]

    @property
    def previous(self) -> int | None:
        return self.notes[-1].pitch if self.notes else None

    def _strong(self, at: float, length: float, bar_start: float) -> bool:
        return self.meter.strength(at - bar_start) >= 2 or length >= 1.0

    def choose(self, at: float, length: float, bar_start: float, target: float, home: int | None = None) -> int:
        """The note to sing at ``at``: each scale note in range is scored and one is picked, weighted by fit."""
        chord = self.harmony.at(at).chord
        strong = self._strong(at, length, bar_start)
        previous = self.previous
        costs = []
        for pitch in self.scale_notes:
            inside = pitch % 12 in chord.tones
            cost = 0.0
            if strong and not inside:
                cost += 7.0 if self.complexity < 5 else 4.5
            if previous is not None:
                interval = abs(pitch - previous)
                if not inside and interval > 2:
                    cost += 5.0  # a note outside the chord is reached by step (passing or neighbor tone)
                if self._last_outside and interval > 2:
                    cost += 5.0  # and left by step
                cost += {0: 1.3, 1: 0.0, 2: 0.0, 3: 0.5, 4: 0.8, 5: 1.4, 7: 1.8}.get(
                    interval, 2.4 + 0.8 * max(0, interval - 7)
                )
                if interval > 12:
                    cost += 12.0
                direction = (pitch > previous) - (pitch < previous)
                if abs(self._last_interval) > 4 and direction == (self._last_interval > 0) - (self._last_interval < 0):
                    cost += 2.5  # after a leap, step back the other way
                if len(self.notes) >= 2 and pitch == previous == self.notes[-2].pitch:
                    cost += 3.0
            elif not inside:
                cost += 3.0
            cost += 0.28 * abs(pitch - target)
            if home is not None and pitch % 12 == home:
                cost -= 3.5
            costs.append(cost)
        temperature = 0.55 + 0.12 * self.complexity
        best = min(costs)
        weights = [math.exp(-(c - best) / temperature) for c in costs]
        return self.rng.choices(self.scale_notes, weights=weights)[0]

    def add(self, at: float, length: float, pitch: int) -> None:
        previous = self.previous
        self._last_interval = pitch - previous if previous is not None else 0
        self._last_outside = pitch % 12 not in self.harmony.at(at).chord.tones
        self.notes.append(BeatNote(at, length, pitch, 0))

    def fit(self, at: float, length: float, bar_start: float, pitch: int) -> int:
        """``pitch`` moved to the nearest chord tone when it falls on a strong beat outside the chord."""
        pitch = min(self.high, max(self.low, pitch))
        chord = self.harmony.at(at).chord
        if self._strong(at, length, bar_start) and pitch % 12 not in chord.tones:
            options = [n for n in self.scale_notes if n % 12 in chord.tones] or self.scale_notes
            pitch = min(options, key=lambda n: (abs(n - pitch), n))
        elif pitch % 12 not in self.key.scale:
            pitch = min(self.scale_notes, key=lambda n: abs(n - pitch))
        return pitch


def _scale_step(line: _Line, pitch: int, steps: int) -> int:
    """``pitch`` moved ``steps`` scale degrees (up or down) along the line's scale."""
    notes = line.scale_notes
    index = min(range(len(notes)), key=lambda i: abs(notes[i] - pitch))
    return notes[max(0, min(len(notes) - 1, index + steps))]


def melody_part(
    genre: Genre,
    key: Key,
    harmony: Harmony,
    section: Section,
    complexity: int,
    rng: random.Random,
    memory: MelodyMemory,
    center: int,
) -> list[BeatNote]:
    meter = section.meter
    chords_key = (
        section.role.name,
        section.bars,
        tuple(harmony.at(section.start + b * meter.bar).chord.name for b in range(section.bars)),
    )
    if section.role.name in ("chorus", "drop") and chords_key in memory.sections:
        return [BeatNote(n.start + section.start, n.duration, n.pitch, n.velocity) for n in memory.sections[chords_key]]
    density = genre.melody_density * (0.7 + 0.12 * complexity) * (0.8 + 0.3 * section.energy)
    grid = genre.melody_grid if complexity >= 3 else max(genre.melody_grid, 0.5)
    motif = memory.motifs.get(section.role.name)
    if motif is None:
        motif = Motif(make_rhythm(meter, density, grid, genre.syncopation, rng))
        memory.motifs[section.role.name] = motif
    line = _Line(key, meter, harmony, center - 9, center + 12, center, rng, complexity)
    phrase_bars = 4 if section.bars >= 4 else section.bars
    if section.role.name in ("chorus", "drop"):
        plan = ["A", "A", "A", "C"]
    elif section.role.name == "verse":
        plan = ["A", "B", "A", "C"]
    else:
        plan = ["A", "B", "V", "C"]
    if phrase_bars < 4:
        plan = ["A", "C"][-phrase_bars:] if phrase_bars <= 2 else ["A", "B", "C"]
    b_rhythm = make_rhythm(meter, density, grid, genre.syncopation, rng)
    for bar in range(section.bars):
        bar_start = section.start + bar * meter.bar
        position = bar % phrase_bars
        phrase = bar // phrase_bars
        part = plan[min(position, len(plan) - 1)]
        last_bar = bar == section.bars - 1
        if last_bar:
            part = "C"
        if section.ending and last_bar:
            rhythm = [(0.0, meter.bar * 0.9)]
        elif part == "A":
            rhythm = motif.rhythm
        elif part == "B":
            rhythm = b_rhythm
        elif part == "V":
            rhythm = _variation(motif.rhythm, meter, grid, rng)
        else:
            rhythm = _cadence(motif.rhythm, meter)
        lift = 2 if phrase % 2 else 0
        for index, (onset, length) in enumerate(rhythm):
            at = bar_start + onset
            if at >= section.end - 1e-6:
                continue
            length = min(length, section.end - at)
            x = (position + onset / meter.bar) / phrase_bars
            target = center + lift + (3 + complexity) * math.sin(math.pi * min(1.0, x * 1.1)) - 2
            final = last_bar and index == len(rhythm) - 1
            home = key.tonic_pc if final and (section.ending or section.role.name in ("chorus", "outro")) else None
            if final and home is None:
                stable = harmony.at(at).chord
                home = stable.root if rng.random() < 0.6 else stable.tones[min(2, len(stable.tones) - 1)]
            if part == "A" and motif.steps is not None and index < len(motif.steps) + 1 and not final:
                if index == 0:
                    pitch = line.fit(at, length, bar_start, motif.first_pitch or center)
                    chord = harmony.at(at).chord
                    tones = [n for n in line.scale_notes if n % 12 in chord.tones]
                    if tones:
                        pitch = min(tones, key=lambda n: abs(n - (motif.first_pitch or center)))
                else:
                    pitch = line.fit(
                        at, length, bar_start, _scale_step(line, line.previous or center, motif.steps[index - 1])
                    )
            else:
                pitch = line.choose(at, length, bar_start, target, home)
            line.add(at, length, pitch)
        if part == "A" and motif.steps is None and not last_bar:
            sung = line.notes[-len(rhythm) :]
            motif.first_pitch = sung[0].pitch
            motif.steps = [_steps_between(line, a.pitch, b.pitch) for a, b in zip(sung, sung[1:], strict=False)]
    notes = line.notes
    for index, note in enumerate(notes):  # no overlaps, and a little shape in the dynamics
        if index + 1 < len(notes):
            note.duration = max(0.05, min(note.duration, notes[index + 1].start - note.start) * 0.92)
        strength = meter.strength((note.start - section.start) % meter.bar)
        loud = 84 + 4 * min(strength, 2) + max(-4, min(6, (note.pitch - center) // 2))
        note.velocity = _scale_velocity(loud, section.energy)
    if section.role.name in ("chorus", "drop"):
        memory.sections[chords_key] = [
            BeatNote(n.start - section.start, n.duration, n.pitch, n.velocity) for n in notes
        ]
    return notes


def _steps_between(line: _Line, a: int, b: int) -> int:
    notes = line.scale_notes
    ia = min(range(len(notes)), key=lambda i: abs(notes[i] - a))
    ib = min(range(len(notes)), key=lambda i: abs(notes[i] - b))
    return ib - ia
