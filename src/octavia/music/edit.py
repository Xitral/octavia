"""Changes to a MIDI song the user already has: transpose, change tempo, extend, add a part that fits, change an
instrument, quantize, humanize, split into one file per track.

Each change works on a copy (``Song.copy``) and the tool saves the result as a new file, so the original is never
touched. A part is added over the harmony found in the file (``analysis``), in the key found there, with the
sections its markers name (our own files have them) or as one long section with fills every eight bars.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from octavia.music import analysis, compose, parts, presets
from octavia.music.feel import BeatNote, Touch, humanize
from octavia.music.meter import Meter
from octavia.music.song import DRUM_CHANNEL, Note, Song, Track
from octavia.music.theory import key_signature_key

GRIDS = {"1/4": 1.0, "1/8": 0.5, "1/16": 0.25, "1/32": 0.125, "1/4t": 2 / 3, "1/8t": 1 / 3, "1/16t": 1 / 6}


class EditError(ValueError):
    """An edit that can't be made; the message says why."""


def transpose(song: Song, semitones: int) -> Song:
    """Every pitched note moved by ``semitones`` (drums stay), folded back an octave when it would leave MIDI's
    range; the key signature moves too."""
    out = song.copy()
    for track in out.tracks:
        if track.is_drums:
            continue
        for note in track.notes:
            pitch = note.pitch + semitones
            while pitch > 127:
                pitch -= 12
            while pitch < 0:
                pitch += 12
            note.pitch = pitch
    moved = []
    for tick, name in out.key_signatures:
        key = key_signature_key(name)
        moved.append((tick, key.transposed(semitones).mido_name if key else name))
    out.key_signatures = moved
    return out


def set_tempo(song: Song, bpm: float) -> Song:
    """The song at ``bpm``; a song that changes tempo keeps its changes, in proportion."""
    out = song.copy()
    factor = bpm / out.bpm
    out.tempos = [(tick, round(value * factor, 3)) for tick, value in out.tempos]
    return out


def _loop_bars(total: int) -> int:
    """How many bars to repeat: the longest of 8, 4, 2 and 1 that the song's length is a multiple of."""
    return next((n for n in (8, 4, 2, 1) if n <= total and total % n == 0), 1)


def extend(song: Song, bars: int) -> tuple[Song, str]:
    """``bars`` more bars, repeating the song's last phrase (the bars before its outro, when a marker says where
    that is, so the ending stays at the end). Returns the song and what was repeated."""
    out = song.copy()
    starts = out.bar_starts()
    insert_bar = len(starts)  # bars before the insertion point
    for tick, text in out.markers:
        if tick in starts and starts.index(tick) > 0 and presets.role_of(text).name == "outro":
            insert_bar = starts.index(tick)
    loop = _loop_bars(insert_bar)
    first = insert_bar - loop
    bar_length = out.bar_ticks(starts[first])
    region = starts[first]
    point = region + loop * bar_length
    shift = bars * bar_length
    copies: dict[int, tuple[list[Note], list]] = {i: ([], []) for i in range(len(out.tracks))}
    done = 0
    while done < bars:  # whole repeats of the phrase, then the part of one that's left
        take = min(loop, bars - done)
        until = region + take * bar_length
        offset = point + done * bar_length - region
        for index, track in enumerate(out.tracks):
            notes, events = copies[index]
            notes += [
                Note(n.start + offset, min(n.end, until) + offset, n.pitch, n.velocity)
                for n in track.notes
                if region <= n.start < until
            ]
            events += [(t + offset, m) for t, m in track.events if region <= t < until]
        done += take

    def later(tick: int) -> int:
        return tick + shift if tick >= point and tick > 0 else tick

    for index, track in enumerate(out.tracks):  # what comes after the insertion point moves later
        for note in track.notes:
            if note.start >= point:
                note.start, note.end = note.start + shift, note.end + shift
            elif note.end > point:
                note.end = point
        notes, events = copies[index]
        track.notes = sorted([*track.notes, *notes], key=lambda n: (n.start, n.pitch))
        track.events = sorted([(later(t), m) for t, m in track.events] + events, key=lambda e: e[0])
    out.markers = [(later(t), m) for t, m in out.markers]
    out.tempos = [(later(t), b) for t, b in out.tempos]
    out.time_signatures = [(later(t), n, d) for t, n, d in out.time_signatures]
    out.key_signatures = [(later(t), k) for t, k in out.key_signatures]
    out.end = max(song.length, point) + shift
    return out, f"bars {first + 1}-{insert_bar}" if loop > 1 else f"bar {insert_bar}"


def quantize(song: Song, grid: str, strength: float = 1.0) -> Song:
    """Note starts pulled toward the nearest ``grid`` line ("1/16", "1/8t"), by ``strength`` (1 = all the way);
    pitched notes' ends too, so lengths come out on the grid (never shorter than one step)."""
    if grid not in GRIDS:
        raise EditError(f"Quantize to one of {', '.join(GRIDS)}.")
    out = song.copy()
    step = GRIDS[grid] * out.ppq
    for track in out.tracks:
        for note in track.notes:
            start = note.start + (round(note.start / step) * step - note.start) * strength
            if track.is_drums:
                end = start + note.duration
            else:
                target_end = max(round(note.end / step) * step, round(start / step) * step + step)
                end = note.end + (target_end - note.end) * strength
            note.start, note.end = round(start), max(round(start) + 1, round(end))
    return out


def humanize_song(song: Song, amount: float, seed: int) -> Song:
    """Timing and velocity nudged a little, like a player (``amount`` 0..1); drums less in time."""
    out = song.copy()
    for index, track in enumerate(out.tracks):
        beats = [BeatNote(n.start / out.ppq, n.duration / out.ppq, n.pitch, n.velocity) for n in track.notes]
        touch = Touch(timing_ms=7, velocity=10, length=0.0) if track.is_drums else Touch()
        humanize(beats, amount, out.bpm, random.Random(f"{seed}:humanize:{index}"), touch)
        track.notes = [
            Note(
                round(b.start * out.ppq), max(round(b.start * out.ppq) + 1, round(b.end * out.ppq)), b.pitch, b.velocity
            )
            for b in beats
        ]
    return out


def find_track(song: Song, wanted: str) -> Track:
    """A track by its number (as describe_midi lists them, from 1), its name, or what it plays ("bass", "drums")."""
    tracks = song.note_tracks()
    text = wanted.strip().casefold().removeprefix("track").strip()
    if text.isdigit():
        number = int(text)
        if not 1 <= number <= len(tracks):
            raise EditError(f"There's no track {number}; this file has {len(tracks)}.")
        return tracks[number - 1]
    for track in tracks:
        if text == track.name.casefold():
            return track
    if text in ("drums", "drum", "percussion", "beat"):
        found = [t for t in tracks if t.is_drums]
        if found:
            return found[0]
    matches = [
        t for t in tracks if text in t.name.casefold() or text in presets.program_name(t.program, t.is_drums).casefold()
    ]
    if matches:
        return matches[0]
    names = "; ".join(f"{i}. {t.name}" for i, t in enumerate(tracks, 1))
    raise EditError(f"I couldn't find a track called '{wanted}'. Its tracks: {names}.")


def set_instrument(song: Song, wanted: str, instrument: str) -> tuple[Song, str]:
    out = song.copy()
    track = find_track(out, wanted)
    part = "drums" if track.is_drums else "melody"
    program = compose.resolve_instrument(instrument, part)
    old = presets.program_name(track.program, track.is_drums)
    track.program = program
    new = presets.program_name(program, track.is_drums)
    if old.casefold() in track.name.casefold():
        track.name = re.sub(re.escape(old), new, track.name, flags=re.IGNORECASE)
    return out, f"{track.name}: {old} became {new}"


def guess_genre(bpm: float) -> presets.Genre:
    """A genre that suits a tempo, for a part added without one."""
    if bpm < 76:
        return presets.GENRES["lofi"]
    if bpm < 100:
        return presets.GENRES["boombap"]
    if bpm < 118:
        return presets.GENRES["pop"]
    if bpm < 132:
        return presets.GENRES["house"]
    if bpm < 162:
        return presets.GENRES["trap"]
    return presets.GENRES["dnb"]


def _roles_present(song: Song) -> list[str]:
    roles = []
    for track in song.note_tracks():
        if track.is_drums:
            roles.append("drums")
        elif 32 <= track.program <= 39 or max(n.pitch for n in track.notes) < 55:
            roles.append("bass")
        else:
            roles.append("chords")
    return roles


@dataclass
class Added:
    song: Song
    track: Track
    genre: presets.Genre
    key_name: str


def add_part(
    song: Song,
    part: str,
    *,
    style: str | None = None,
    instrument: str | None = None,
    complexity: int = 3,
    humanize_amount: float = 0.5,
    seed: int = 1,
) -> Added:
    """A new part that fits the song: in its key, over its chords, in its meter and tempo."""
    if part not in presets.PARTS:
        raise EditError(f"I can add one of: {', '.join(presets.PARTS)}.")
    if part == "drums" and any(t.is_drums for t in song.note_tracks()):
        raise EditError("It already has drums; change them with a new file, or add another part.")
    channel = DRUM_CHANNEL if part == "drums" else song.free_channel()
    if channel is None:
        raise EditError("Every MIDI channel is already used, so there's no room for another part.")
    if not song.note_tracks():
        raise EditError("It has no notes to fit a part to.")
    out = song.copy()
    found = analysis.analyze(out)
    spans = analysis.harmony(out, found)
    num, den = out.meter
    try:
        meter = Meter(num, den) if den in (2, 4, 8, 16) else Meter(4, 4)
    except ValueError:
        meter = Meter(4, 4)
    genre = compose.find_genre(style) if style else guess_genre(out.bpm)
    bars = out.bars
    sections: list[parts.Section] = []
    labels: list[str] = []
    starts = out.bar_starts()
    marks = [(starts.index(t) if t in starts else None, text) for t, text in out.markers]
    marks = [(b, text) for b, text in marks if b is not None]
    if not marks or marks[0][0] != 0:
        marks.insert(0, (0, "Loop"))
    for index, (bar, text) in enumerate(marks):
        until = marks[index + 1][0] if index + 1 < len(marks) else bars
        if until <= bar:
            continue
        role = presets.role_of(text) if text != "Loop" else presets.ROLES["loop"]
        sections.append(parts.Section(bar * meter.bar, until - bar, role, role.energy, meter, bar, False))
        labels.append(text)
    programs = {p: genre.instruments.get(p, 0) for p in presets.PARTS}
    programs["drums"] = genre.kit
    if instrument:
        programs[part] = compose.resolve_instrument(instrument, part)
    plan = compose.Plan(
        genre=genre,
        key=found.key,
        meter=meter,
        bpm=round(out.bpm),
        sections=sections,
        labels=labels,
        harmony=parts.Harmony(spans),
        parts=[part, *(_roles_present(out))],
        programs=programs,
        complexity=complexity,
        humanize=humanize_amount,
        swing=genre.swing,
        seed=seed,
    )
    notes = compose.render_part(plan, part)
    scale = out.ppq
    end = out.length
    track = Track(
        compose.track_name(part, programs[part]), channel, programs[part], compose.to_ticks(notes, scale, end)
    )
    out.tracks.append(track)
    return Added(out, track, genre, found.key.name)


def split(song: Song) -> list[tuple[str, Song]]:
    """One song per track with notes, each keeping the tempo, meter, key signature and markers."""
    out = []
    for track in song.note_tracks():
        single = song.copy()
        single.tracks = [t for t in single.tracks if t.name == track.name and t.channel == track.channel][:1]
        single.end = song.length
        out.append((track.name, single))
    return out
