"""What's in a MIDI file: its tracks and instruments, tempo, meter, length, key and the chords bar by bar.

The key comes from music21's Krumhansl-Schmuckler key finder over how long each pitch class sounds (drums left
out), with the file's own key signature preferred when it fits nearly as well. Chords are found bar by bar by
matching what sounds in the bar (each note weighted by how long and how loud it is, and the lowest notes counted as
the bass) against chord shapes; a bar whose halves hold two clearly different chords is shown as two. The same
chords become the harmony a new part follows (``harmony``).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass

from octavia.music.presets import program_name
from octavia.music.song import Song, Track
from octavia.music.theory import (
    Chord,
    ChordSpan,
    Detected,
    Key,
    detect_chord,
    estimate_key,
    key_signature_key,
    pc_name,
    roman_for,
)


@dataclass(frozen=True)
class BarChord:
    start: int  # ticks
    end: int
    detected: Detected | None
    bass: int | None

    def name(self, key: Key) -> str:
        if self.detected is None:
            return "-"
        chord = self.detected.chord(key)
        if self.bass is not None and self.bass != chord.root and self.bass in chord.tones:
            return f"{chord.name}/{pc_name(self.bass, key.flats)}"
        return chord.name


@dataclass
class Analysis:
    key: Key
    confidence: float
    signature_key: Key | None  # what the file's key signature says, if it has one
    chords: list[list[BarChord]]  # per bar: one chord, or two for a bar that changes halfway


def pitch_weights(song: Song, start: int = 0, end: int | None = None) -> list[float]:
    """How much each pitch class sounds between two ticks: duration times loudness, drums left out."""
    weights = [0.0] * 12
    stop = song.length if end is None else end
    for track in song.tracks:
        if track.is_drums:
            continue
        for note in track.notes:
            overlap = min(note.end, stop) - max(note.start, start)
            if overlap > 0:
                weights[note.pitch % 12] += overlap * (0.5 + note.velocity / 254)
    return weights


def _bass_of(notes: list, start: int, end: int, step: int) -> int | None:
    """The pitch class heard lowest most of the time between two ticks (sampled every sixteenth)."""
    lows: Counter[int] = Counter()
    for tick in range(start, end, step):
        here = [n.pitch for n in notes if n.start <= tick < n.end]
        if here:
            lows[min(here) % 12] += 1
    return lows.most_common(1)[0][0] if lows else None


def _chord_in(notes: list, start: int, end: int, key: Key, step: int) -> BarChord:
    notes = [n for n in notes if n.start < end and n.end > start]
    bass = _bass_of(notes, start, end, step)
    weights = [0.0] * 12
    for note in notes:
        weights[note.pitch % 12] += (min(note.end, end) - max(note.start, start)) * (0.5 + note.velocity / 254)
    if bass is not None:
        weights[bass] += sum(weights) * 0.15
    return BarChord(start, end, detect_chord(weights, bass, key), bass)


def analyze(song: Song) -> Analysis:
    hint = None
    if song.key_signatures:
        hint = key_signature_key(song.key_signatures[0][1])
    starts = song.bar_starts()
    ends = [*starts[1:], song.length]
    buckets: list[list] = [[] for _ in starts]  # each bar's notes (a long note is in every bar it sounds in)
    for track in song.tracks:
        if track.is_drums:
            continue
        for note in track.notes:
            first = max(0, bisect_right(starts, note.start) - 1)
            for index in range(first, len(starts)):
                if starts[index] >= note.end:
                    break
                buckets[index].append(note)
    step = max(1, song.ppq // 4)
    sounding = [i for i, notes in enumerate(buckets) if notes]
    home = set()  # the bass notes it starts and ends on: usually the tonic
    for index in sounding[:1] + sounding[-1:]:
        bass = _bass_of(buckets[index], starts[index], ends[index], step)
        if bass is not None:
            home.add(bass)
    key, confidence = estimate_key(pitch_weights(song), hint, home)
    num, _ = song.meter
    chords: list[list[BarChord]] = []
    for start, end, notes in zip(starts, ends, buckets, strict=True):
        whole = _chord_in(notes, start, end, key, step)
        middle = start + (end - start) // 2
        if whole.detected is not None and num % 2 == 0:
            first, second = _chord_in(notes, start, middle, key, step), _chord_in(notes, middle, end, key, step)
            if (
                first.detected
                and second.detected
                and (first.detected.root, first.detected.quality) != (second.detected.root, second.detected.quality)
                and min(first.detected.score, second.detected.score) > whole.detected.score + 0.12
            ):
                chords.append([first, second])
                continue
        chords.append([whole])
    return Analysis(key, confidence, hint, chords)


def harmony(song: Song, found: Analysis) -> list[ChordSpan]:
    """The chords as spans in beats, for a new part to follow; a bar with no chord keeps the last one (the tonic
    at the start)."""
    spans: list[ChordSpan] = []
    tonic_quality = "m" if found.key.family == "minor" else ""
    current = Detected(found.key.tonic_pc, tonic_quality, 0.0).chord(found.key)
    for bar in found.chords:
        for part in bar:
            if part.detected is not None:
                current = part.detected.chord(found.key)
                if part.bass is not None and part.bass in current.tones:
                    current = Chord(current.root, current.tones, part.bass, current.name)
            spans.append(ChordSpan(part.start / song.ppq, part.end / song.ppq, current))
    return spans


def _range(track: Track) -> str:
    names = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
    low = min(n.pitch for n in track.notes)
    high = max(n.pitch for n in track.notes)
    return f"{names[low % 12]}{low // 12 - 1} to {names[high % 12]}{high // 12 - 1}"


def _sureness(confidence: float) -> str:
    if confidence >= 0.85:
        return "clearly"
    if confidence >= 0.7:
        return "probably"
    return "maybe"


def describe(song: Song, name: str, max_bars: int = 64) -> str:
    """A summary she can read out or work from."""
    seconds = song.seconds()
    num, den = song.meter
    tracks = song.note_tracks()
    if not tracks:
        return f"“{name}” has no notes in it ({len(song.tracks)} empty tracks)."
    lines = []
    tempo = f"{song.bpm:g} BPM"
    if len(song.tempos) > 1:
        tempos = [b for _, b in song.tempos]
        tempo += f" (it changes tempo {len(song.tempos) - 1} times, {min(tempos):g} to {max(tempos):g})"
    meter = f"{num}/{den}"
    if len(song.time_signatures) > 1:
        meter += f" (changing {len(song.time_signatures) - 1} times)"
    lines.append(
        f"“{name}”: {song.bars} bars of {meter} at {tempo}, {int(seconds // 60)}:{int(seconds % 60):02d} long."
    )
    found = analyze(song)
    key_text = f"Key: {_sureness(found.confidence)} {found.key.name}"
    if found.signature_key and found.signature_key.name != found.key.name:
        key_text += f" (its key signature says {found.signature_key.name})"
    lines.append(key_text + ".")
    described = []
    for number, track in enumerate(tracks, 1):
        sound = program_name(track.program, drums=track.is_drums)
        where = "drums, channel 10" if track.is_drums else f"channel {track.channel + 1}, {_range(track)}"
        details = [where, f"{len(track.notes)} notes"]
        if sound.casefold() not in track.name.casefold():
            details.insert(0, sound)
        described.append(f"{number}. {track.name} ({'; '.join(details)})")
    lines.append(f"{len(tracks)} tracks: " + "; ".join(described) + ".")
    if song.markers:
        bars = song.bar_starts()
        marks = []
        for tick, text in song.markers[:24]:
            bar = max(i for i, s in enumerate(bars) if s <= tick) + 1 if bars else 1
            marks.append(f"{text} (bar {bar})")
        lines.append("Sections: " + ", ".join(marks) + ".")
    groups = []
    shown = found.chords[:max_bars]
    for first in range(0, len(shown), 4):
        bars = shown[first : first + 4]
        names = " ".join("-".join(part.name(found.key) for part in bar) for bar in bars)
        romans = " ".join(
            "-".join(roman_for(p.detected.root, p.detected.intervals, found.key) if p.detected else "-" for p in bar)
            for bar in bars
        )
        last = first + len(bars)
        groups.append(f"bars {first + 1}-{last}: {names} ({romans})")
    more = f" ...and {len(found.chords) - max_bars} more bars" if len(found.chords) > max_bars else ""
    if any(t for t in tracks if not t.is_drums):
        lines.append("Chords: " + "; ".join(groups) + more + ".")
    return " ".join(lines)
