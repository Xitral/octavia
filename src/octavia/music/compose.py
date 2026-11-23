"""From a description of a song to a multi-track MIDI song.

``plan`` decides everything before a note is written: the genre (its preset), key, tempo, meter, the sections (the
ones asked for, or a usual form for the length: a loop, verse and chorus, or intro, verses, choruses, a bridge and
an outro), the chord progression of each kind of section (the genre's, or one the user gave), which parts play and
on which General MIDI sounds. ``render`` then writes each part section by section, applies the feel (swing,
strumming, humanizing) and ``to_song`` lays the parts out as tracks: drums on channel 10, each other part on its
own channel with its instrument, the tempo, time and key signatures and a marker at each section.

All the randomness comes from the seed, through a separate generator for each part and section (seeded with
strings, which Python hashes the same way every run), so the same request with the same seed gives the same file,
and changing one part's settings doesn't change the others.
"""

from __future__ import annotations

import functools
import random
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from octavia.music import feel, grooves, parts, presets, voicing
from octavia.music.feel import BeatNote, Touch
from octavia.music.meter import Meter
from octavia.music.presets import GENRES, PARTS, Genre
from octavia.music.song import DRUM_CHANNEL, PPQ, Note, Song, Track
from octavia.music.theory import (
    Chord,
    ChordSpan,
    Key,
    enrich,
    estimate_key,
    is_roman,
    make_key,
    parse_key,
    realize,
    split_beats,
    tokens,
)

MAX_BARS = 256
MAX_SECTIONS = 32


class CompositionError(ValueError):
    """A request that can't be composed; the message says what to change."""


# --------------------------------------------------------------------------- reading the request


_FILLER_WORDS = ("beat", "beats", "music", "song", "track", "instrumental", "type", "style", "vibe", "vibes", "loop")


def find_genre(style: str | None) -> Genre:
    """The preset for a style as people say it ("lofi hip hop", "boom bap beat", "dnb"). Raises CompositionError."""
    if not style or not style.strip():
        return GENRES["pop"]
    text = " ".join(w for w in style.casefold().replace("_", " ").split() if w not in _FILLER_WORDS) or style.casefold()
    aliases = {
        alias: genre for genre in GENRES.values() for alias in (genre.key, genre.name.casefold(), *genre.aliases)
    }
    if text in aliases:
        return aliases[text]
    for alias in sorted(aliases, key=len, reverse=True):  # "dark trap" is trap; "lofi hip hop" is lo-fi, not hip-hop
        if len(alias) > 2 and f" {alias} " in f" {text} ":
            return aliases[alias]
    match = process.extractOne(text, list(aliases), scorer=fuzz.WRatio, score_cutoff=86)
    if match:
        return aliases[match[0]]
    raise CompositionError(f"I don't have a preset for '{style}'. I know: {known_genres()}.")


def known_genres() -> str:
    return ", ".join(g.name for g in GENRES.values())


def resolve_instrument(value: str | int, part: str) -> int:
    """A General MIDI program (0-127) from a number or a name ("rhodes", "Electric Piano 1", "808").
    Raises CompositionError."""
    if part == "drums":
        return resolve_kit(value)
    if isinstance(value, int) or str(value).strip().isdigit():
        number = int(value)
        if not 0 <= number <= 127:
            raise CompositionError(f"Instrument numbers are General MIDI programs, 0 to 127 (got {number}).")
        return number
    text = " ".join(str(value).casefold().replace("-", " ").split())
    if text in presets.INSTRUMENT_ALIASES:
        return presets.INSTRUMENT_ALIASES[text]
    names = {name.casefold(): number for number, name in enumerate(presets.GM_PROGRAMS)}
    if text in names:
        return names[text]
    containing = [number for name, number in names.items() if text in name]
    if containing:
        return containing[0]
    choices = list(presets.INSTRUMENT_ALIASES) + list(names)
    match = process.extractOne(text, choices, scorer=fuzz.ratio, score_cutoff=80)  # a misspelling, not a guess
    if match:
        return presets.INSTRUMENT_ALIASES.get(match[0], names.get(match[0], 0))
    close = [m[0] for m in process.extract(text, choices, scorer=fuzz.WRatio, limit=3)]
    raise CompositionError(
        f"I don't know a General MIDI instrument called '{value}' (for the {part}). Close ones: {', '.join(close)}; "
        "or give its program number, 0 to 127."
    )


def resolve_kit(value: str | int) -> int:
    if isinstance(value, int) or str(value).strip().isdigit():
        number = int(value)
        if not 0 <= number <= 127:
            raise CompositionError(f"Drum kit numbers go from 0 to 127 (got {number}).")
        return number
    text = str(value).casefold().replace("kit", "").replace("drums", "").strip()
    if text in presets.DRUM_KITS:
        return presets.DRUM_KITS[text]
    kits = ", ".join(sorted({k for k in presets.DRUM_KITS if "-" not in k}))
    raise CompositionError(f"I don't know a drum kit called '{value}'. Kits: {kits}.")


@dataclass(frozen=True)
class SectionSpec:
    name: str
    bars: int


def default_form(bars: int) -> list[SectionSpec]:
    """The usual shape of a song this long: a loop up to 8 bars, verse and chorus up to 16, then an intro, verses
    and choruses in eight-bar blocks (with a bridge in longer songs) and an outro."""
    if bars <= 8:
        return [SectionSpec("Loop", bars)]
    if bars <= 16:
        return [SectionSpec("Verse", bars - bars // 2), SectionSpec("Chorus", bars // 2)]
    if bars < 24:
        return [SectionSpec("Intro", 4), SectionSpec("Verse", 8), SectionSpec("Chorus", bars - 12)]
    body_bars = bars - 8
    blocks = max(1, body_bars // 8)
    shapes = {
        1: ["Chorus"], 2: ["Verse", "Chorus"], 3: ["Verse", "Chorus", "Chorus"], 4: ["Verse", "Chorus", "Verse", "Chorus"],
        5: ["Verse", "Chorus", "Verse", "Chorus", "Chorus"], 6: ["Verse", "Chorus", "Verse", "Chorus", "Bridge", "Chorus"],
    }  # fmt: skip
    if blocks in shapes:
        names = shapes[blocks]
    else:
        names = (
            ["Verse", "Chorus"] * ((blocks - 3) // 2)
            + (["Verse"] if (blocks - 3) % 2 else [])
            + ["Bridge", "Chorus", "Chorus"]
        )
    sizes = [8] * len(names)
    sizes[-1] += body_bars - 8 * len(names)
    merged: list[SectionSpec] = []
    for name, size in zip(names, sizes, strict=True):
        if merged and merged[-1].name == name:
            merged[-1] = SectionSpec(name, merged[-1].bars + size)
        else:
            merged.append(SectionSpec(name, size))
    return [SectionSpec("Intro", 4), *merged, SectionSpec("Outro", 4)]


def _section_labels(specs: list[SectionSpec]) -> list[str]:
    """Marker names: "Verse 1", "Verse 2" when a name comes back; as given otherwise."""
    names = [s.name.strip().title() if s.name.islower() else s.name.strip() for s in specs]
    counts = {n: names.count(n) for n in names}
    seen: dict[str, int] = {}
    labels = []
    for name in names:
        if counts[name] > 1 and not name[-1:].isdigit():
            seen[name] = seen.get(name, 0) + 1
            labels.append(f"{name} {seen[name]}")
        else:
            labels.append(name)
    return labels


# --------------------------------------------------------------------------- the plan


@dataclass
class Plan:
    genre: Genre
    key: Key
    meter: Meter
    bpm: int
    sections: list[parts.Section]
    labels: list[str]
    harmony: parts.Harmony
    parts: list[str]
    programs: dict[str, int]
    complexity: int
    humanize: float
    swing: int
    seed: int
    mood_words: list[str] = field(default_factory=list)
    melody_shift: int = 0
    progressions: dict[str, list[str]] = field(default_factory=dict)  # section label -> its chord names
    title: str = ""

    @property
    def bars(self) -> int:
        return sum(s.bars for s in self.sections)

    @property
    def beats(self) -> float:
        return self.bars * self.meter.bar


@functools.cache
def _chord(figure: str, key: Key) -> Chord:
    return realize(figure, key)


def _color_level(genre: Genre, complexity: int) -> int:
    if complexity <= 1:
        return 0
    level = genre.color + (1 if complexity >= 5 else 0) - (1 if complexity == 2 else 0)
    return max(0, min(2, level))


def progression_chords(text: str, key: Key) -> list[tuple[Chord, float | None]]:
    """A progression the user gave, read and checked. Raises TheoryError (a ValueError)."""
    written = tokens(text)
    if not written:
        raise CompositionError("The progression is empty; write it like 'i VI III VII' or 'Am F C G'.")
    if len(written) > 64:
        raise CompositionError("That progression is too long; give at most 64 chords.")
    out = []
    for token in written:
        figure, beats = split_beats(token)
        out.append((_chord(figure, key), beats))
    return out


def key_of_progression(text: str) -> Key | None:
    """The key chord symbols ("Am F C G") are in, found from their notes (the first chord counts most, as songs
    usually start at home); None for roman numerals, which need a key to mean anything."""
    figures = [split_beats(t)[0] for t in tokens(text)]
    if not figures or any(is_roman(f) for f in figures):
        return None
    chords = [_chord(f, make_key("C", "major")) for f in figures]
    weights = [0.0] * 12
    for chord in chords:
        for tone in chord.tones:
            weights[tone] += 1.0
        weights[chord.root] += 1.0
    weights[chords[0].root] += 1.5
    weights[chords[-1].root] += 0.5
    found, _ = estimate_key(weights)
    return found


def plan(
    *,
    style: str | None = None,
    key: str | None = None,
    tempo: int | None = None,
    time_signature: str = "4/4",
    bars: int | None = None,
    mood: str | None = None,
    sections: list[SectionSpec] | None = None,
    parts_wanted: list[str] | None = None,
    instruments: dict[str, str | int] | None = None,
    progression: str | None = None,
    complexity: int = 3,
    humanize: float = 0.5,
    swing: int | None = None,
    seed: int | None = None,
    title: str = "",
) -> Plan:
    """Decide the whole song. Raises CompositionError, TheoryError or MeterError (all ValueErrors) with a message
    saying what to change."""
    genre = find_genre(style)
    mood_value, mood_words = presets.mood_of(mood)
    seed = seed if seed is not None else random.SystemRandom().randint(1, 99_999)
    rng = random.Random(f"{seed}:plan")
    try:
        meter = Meter.parse(time_signature or "4/4")
    except ValueError as exc:
        raise CompositionError(str(exc)) from exc
    if not 1 <= complexity <= 5:
        raise CompositionError("Complexity goes from 1 (simple) to 5 (busy).")
    if not 0 <= humanize <= 1:
        raise CompositionError("Humanize goes from 0 (exactly on the grid) to 1 (loose).")
    if swing is not None and not 50 <= swing <= 75:
        raise CompositionError("Swing goes from 50 (straight) to 75 (heavy shuffle); 66 is triplet swing.")

    # Key: as asked, else the mood's or genre's mode on a common tonic.
    if key:
        song_key = parse_key(key)
    elif progression and (found := key_of_progression(progression)) is not None:
        song_key = found
    else:
        mode = mood_value.mode or genre.mode
        song_key = make_key(rng.choice(presets.COMMON_TONICS[mode]), mode)

    # Tempo: as asked, else the genre's usual one, nudged by the mood within the genre's range.
    if tempo is None:
        slow, usual, fast = genre.tempo
        lean = mood_value.tempo
        tempo = round(usual + (fast - usual) * lean if lean >= 0 else usual + (usual - slow) * lean)
    if not 40 <= tempo <= 220:
        raise CompositionError("Tempo goes from 40 to 220 BPM.")

    # Sections.
    if sections:
        if len(sections) > MAX_SECTIONS:
            raise CompositionError(f"That's too many sections; give at most {MAX_SECTIONS}.")
        total = sum(s.bars for s in sections)
        if bars is not None and bars != total:
            raise CompositionError(
                f"The sections add up to {total} bars but {bars} bars were asked for; give one or make them match."
            )
        if total > MAX_BARS:
            raise CompositionError(f"The sections add up to {total} bars; the most I write is {MAX_BARS}.")
        specs = list(sections)
    else:
        total = bars if bars is not None else 16
        if not 1 <= total <= MAX_BARS:
            raise CompositionError(f"A song is 1 to {MAX_BARS} bars long.")
        specs = default_form(total)

    # Parts and their sounds.
    wanted = list(dict.fromkeys(p.casefold().strip() for p in (parts_wanted or genre.parts)))
    unknown = [p for p in wanted if p not in PARTS]
    if unknown:
        raise CompositionError(f"I can't write a '{unknown[0]}' part; parts are {', '.join(PARTS)}.")
    if not wanted:
        raise CompositionError(f"Ask for at least one part: {', '.join(PARTS)}.")
    programs = {p: genre.instruments.get(p, 0) for p in PARTS}
    programs["drums"] = genre.kit
    for part, value in (instruments or {}).items():
        name = part.casefold().strip()
        if name not in PARTS:
            raise CompositionError(f"Instruments are given per part ({', '.join(PARTS)}); '{part}' isn't one.")
        programs[name] = resolve_instrument(value, name)

    # Sections in beats, with their roles.
    out_sections: list[parts.Section] = []
    at, bar_number = 0.0, 0
    labels = _section_labels(specs)
    ending = len(specs) >= 2 and presets.role_of(specs[-1].name).name == "outro"
    for index, spec in enumerate(specs):
        if not 1 <= spec.bars <= 128:
            raise CompositionError(f"A section is 1 to 128 bars long ('{spec.name}' has {spec.bars}).")
        role = presets.role_of(spec.name)
        energy = max(0.3, min(1.2, role.energy * mood_value.energy))
        last = index == len(specs) - 1
        out_sections.append(parts.Section(at, spec.bars, role, energy, meter, bar_number, ending and last))
        at += spec.bars * meter.bar
        bar_number += spec.bars

    harmony, progressions = _harmony(genre, song_key, meter, out_sections, labels, progression, complexity, seed)
    return Plan(
        genre=genre,
        key=song_key,
        meter=meter,
        bpm=int(tempo),
        sections=out_sections,
        labels=labels,
        harmony=harmony,
        parts=wanted,
        programs=programs,
        complexity=complexity,
        humanize=humanize,
        swing=swing if swing is not None else genre.swing,
        seed=seed,
        mood_words=mood_words,
        melody_shift=mood_value.melody_shift,
        progressions=progressions,
        title=title,
    )


def _harmony(
    genre: Genre,
    key: Key,
    meter: Meter,
    sections: list[parts.Section],
    labels: list[str],
    progression: str | None,
    complexity: int,
    seed: int,
) -> tuple[parts.Harmony, dict[str, list[str]]]:
    """The chords under the whole song: each section cycles through its progression, a chord per slot (the
    genre's chord length, scaled to the meter's bar), and an ending comes home to the tonic."""
    level = _color_level(genre, complexity)
    slot = genre.chord_beats / 4 * meter.bar
    given = progression_chords(progression, key) if progression else None
    chosen: dict[str, tuple[str, ...]] = {}
    spans: list[ChordSpan] = []
    named: dict[str, list[str]] = {}
    for section, label in zip(sections, labels, strict=True):
        if given is not None:
            chords = [(enrich(c, key, level) if level and len(c.tones) == 3 else c, beats) for c, beats in given]
        else:
            role = section.role.progression
            table = genre.progressions[key.family]
            options = table.get(role) or table["verse"]
            if role not in chosen:
                pick = random.Random(f"{seed}:progression:{role}")
                chosen[role] = pick.choice(options)
            chords = [(enrich(_chord(f, key), key, level), b) for f, b in map(split_beats, chosen[role])]
        t, index = section.start, 0
        first_cycle: list[str] = []
        while t < section.end - 1e-6:
            chord, beats = chords[index % len(chords)]
            length = beats if beats is not None else slot
            end = min(t + length, section.end)
            spans.append(ChordSpan(t, end, chord))
            if index < len(chords):
                first_cycle.append(chord.name)
            t, index = end, index + 1
        named[label] = first_cycle
        if section.ending:  # the last bar is the home chord
            home = enrich(_chord("i" if key.family == "minor" else "I", key), key, level)
            cut = section.last_bar_start
            spans = [s for s in spans if s.start < cut - 1e-6]
            if spans and spans[-1].end > cut:
                last = spans.pop()
                spans.append(ChordSpan(last.start, cut, last.chord))
            spans.append(ChordSpan(cut, section.end, home))
    return parts.Harmony(spans), named


# --------------------------------------------------------------------------- writing it


_TOUCH = {
    "drums": Touch(timing_ms=7, velocity=10, length=0.0),
    "bass": Touch(timing_ms=8, velocity=7),
    "chords": Touch(timing_ms=10, velocity=7),
    "pad": Touch(timing_ms=6, velocity=4),
    "arpeggio": Touch(timing_ms=5, velocity=8),
    "melody": Touch(timing_ms=12, velocity=9),
}
_LAID_BACK = {"boombap", "lofi", "rnb"}  # genres whose snare sits a little behind the beat


def render_part(plan: Plan, part: str, memory: parts.MelodyMemory | None = None) -> list[BeatNote]:
    """One part's notes over the whole song, with its feel applied."""
    genre, harmony = plan.genre, plan.harmony
    voicer = parts.Voicer(voicing.PAD if part == "pad" else voicing.CHORDS)
    if part == "chords" and genre.power_chords:
        voicer.power = True
    if part == "chords" and "bass" not in plan.parts:
        voicer.root_elsewhere = False
    if part == "arpeggio":
        voicer.register = voicing.Register(low=55, high=79, center=66, voices=4)
    memory = memory or parts.MelodyMemory()
    alone = len(plan.parts) == 1
    notes: list[BeatNote] = []
    for index, section in enumerate(plan.sections):
        rests = section.role.rests
        if part in rests and not alone and any(p not in rests for p in plan.parts):
            continue
        rng = random.Random(f"{plan.seed}:{part}:{index}")
        if part == "drums":
            notes += grooves.drum_section(
                genre,
                plan.meter,
                section.start,
                section.bars,
                section.role,
                section.energy,
                plan.complexity,
                rng,
                crash_start=index > 0 and section.role.drums != "lite",
                fill_end=True,
                ending=section.ending,
                first_bar=section.first_bar,
            )
        elif part == "bass":
            notes += parts.bass_part(genre, plan.key, harmony, section, plan.complexity, rng)
        elif part in ("chords", "pad"):
            notes += parts.chord_part(genre, harmony, section, plan.complexity, rng, voicer, pad=part == "pad")
        elif part == "arpeggio":
            notes += parts.arpeggio_part(genre, harmony, section, plan.complexity, rng, voicer)
        elif part == "melody":
            center = max(55, min(84, genre.melody_center + section.role.melody_shift + plan.melody_shift))
            notes += parts.melody_part(genre, plan.key, harmony, section, plan.complexity, rng, memory, center)
    apply_feel(notes, part, plan.genre, plan.bpm, plan.swing, plan.humanize, random.Random(f"{plan.seed}:feel:{part}"))
    return notes


def apply_feel(
    notes: list[BeatNote], part: str, genre: Genre, bpm: float, swing: int, humanize: float, rng: random.Random
) -> None:
    feel.apply_swing(notes, swing, genre.swing_grid)
    if part == "chords" and genre.strum and humanize > 0:
        by_start: dict[float, list[BeatNote]] = {}
        for note in notes:
            by_start.setdefault(round(note.start, 4), []).append(note)
        for group in by_start.values():
            if len(group) > 1:
                feel.strum(group, genre.strum * min(1.0, humanize * 2), bpm)
    touch = _TOUCH.get(part, Touch())
    if part == "drums" and genre.key in _LAID_BACK:
        touch = Touch(touch.timing_ms, touch.velocity, touch.length, {38: 12.0, 39: 10.0, 37: 8.0})
    feel.humanize(notes, humanize, bpm, rng, touch)


def to_ticks(notes: list[BeatNote], ppq: int, end: int, offset: int = 0) -> list[Note]:
    out = []
    for note in notes:
        start = offset + round(note.start * ppq)
        stop = min(end, offset + round(note.end * ppq))
        if start < end and stop > start:
            out.append(Note(start, stop, note.pitch, note.velocity))
    return out


def track_name(part: str, program: int) -> str:
    return f"{part.title()} ({presets.program_name(program, drums=part == 'drums')})"


def channels_for(parts_list: list[str]) -> dict[str, int]:
    free = iter(c for c in range(16) if c != DRUM_CHANNEL)
    return {p: (DRUM_CHANNEL if p == "drums" else next(free)) for p in parts_list}


def to_song(plan: Plan, rendered: dict[str, list[BeatNote]]) -> Song:
    end = round(plan.beats * PPQ)
    song = Song(
        ppq=PPQ,
        tempos=[(0, float(plan.bpm))],
        time_signatures=[(0, plan.meter.num, plan.meter.den)],
        key_signatures=[(0, plan.key.mido_name)],
        markers=[(round(s.start * PPQ), label) for s, label in zip(plan.sections, plan.labels, strict=True)],
        title=plan.title or f"{plan.genre.name} in {plan.key.name}",
        end=end,
    )
    order = [p for p in PARTS if p in rendered]
    channels = channels_for(order)
    for part in order:
        program = plan.programs[part]
        song.tracks.append(
            Track(track_name(part, program), channels[part], program, to_ticks(rendered[part], PPQ, end))
        )
    return song


@dataclass
class Composition:
    plan: Plan
    song: Song


def compose(**request) -> Composition:
    """``plan`` (same arguments), every part rendered, laid out as a song."""
    the_plan = plan(**request)
    memory = parts.MelodyMemory()
    rendered = {part: render_part(the_plan, part, memory) for part in the_plan.parts}
    return Composition(the_plan, to_song(the_plan, rendered))


def describe(composition: Composition) -> str:
    """A few sentences about what was written, for her to tell the user."""
    p, song = composition.plan, composition.song
    seconds = song.seconds()
    length = f"{int(seconds // 60)}:{int(seconds % 60):02d}"
    sections = ", ".join(f"{label} {s.bars}" for s, label in zip(p.sections, p.labels, strict=True))
    tracks = "; ".join(f"{t.name}" for t in song.tracks)
    seen: dict[tuple[str, ...], str] = {}
    chords = []
    for label, names in p.progressions.items():
        line = tuple(names[:8])
        if line not in seen:
            seen[line] = label
            chords.append(f"{label}: {' '.join(line)}")
    meter = "" if p.meter.text == "4/4" else f", {p.meter.text}"
    mood = f", {' '.join(p.mood_words)}" if p.mood_words else ""
    swing = f", swing {p.swing}%" if p.swing != 50 else ""
    return (
        f"{p.bars} bars of {p.genre.name}{mood} in {p.key.name} at {p.bpm} BPM{meter} ({length}){swing}. "
        f"Sections: {sections}. Tracks: {tracks}. Chords: {' | '.join(chords[:4])}. "
        f"Seed {p.seed}, complexity {p.complexity}, humanize {p.humanize:g}."
    )
