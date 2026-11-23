"""A MIDI file as a song: tracks of notes in ticks, plus the tempo map, time and key signatures and markers. Read and
written with mido.

Reading pairs each note-on with its note-off (a note-on at velocity 0 counts as an off), first in first out per
channel and pitch, and splits a track holding several channels (a type 0 file has one track with everything) into
one track per channel, so every track has one channel and one instrument, as DAWs show them. Controller, pitch
bend and aftertouch messages are kept with their track; lyrics, sysex and the like are dropped.

Writing makes a type 1 file: a first "conductor" track with the tempo, time signature, key signature and section
markers, then one track per part with its name and instrument (program change) up front. At one moment note-offs
come before note-ons, so a repeated note is never cut by its own previous note's end, and every track ends at the
song's end, so a DAW shows the whole length even when the last bar is quiet.
"""

from __future__ import annotations

import copy
from bisect import bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

import mido

PPQ = 480  # ticks per quarter note in the files we write
DRUM_CHANNEL = 9  # General MIDI drums: channel 10, counted from 1


class MidiError(ValueError):
    """A file that isn't a MIDI file we can read."""


@dataclass
class Note:
    start: int  # ticks
    end: int
    pitch: int
    velocity: int

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass
class Track:
    name: str
    channel: int
    program: int
    notes: list[Note] = field(default_factory=list)
    events: list[tuple[int, mido.Message]] = field(default_factory=list)  # controllers, pitch bend: (tick, message)

    @property
    def is_drums(self) -> bool:
        return self.channel == DRUM_CHANNEL


@dataclass
class Song:
    ppq: int = PPQ
    tracks: list[Track] = field(default_factory=list)
    tempos: list[tuple[int, float]] = field(default_factory=lambda: [(0, 120.0)])  # (tick, bpm), sorted
    time_signatures: list[tuple[int, int, int]] = field(default_factory=lambda: [(0, 4, 4)])  # (tick, num, den)
    key_signatures: list[tuple[int, str]] = field(default_factory=list)  # (tick, mido key name: "Am", "Bb")
    markers: list[tuple[int, str]] = field(default_factory=list)
    title: str = ""
    end: int = 0  # ticks: where the song ends (at least its last note's end)

    # ------------------------------------------------------------------ measuring

    @property
    def bpm(self) -> float:
        return self.tempos[0][1] if self.tempos else 120.0

    @property
    def meter(self) -> tuple[int, int]:
        _, num, den = self.time_signatures[0] if self.time_signatures else (0, 4, 4)
        return num, den

    def bar_ticks(self, at: int = 0) -> int:
        """How long a bar is at tick ``at``."""
        num, den = self._signature_at(at)[1:]
        return round(self.ppq * 4 * num / den)

    def _signature_at(self, tick: int) -> tuple[int, int, int]:
        signatures = self.time_signatures or [(0, 4, 4)]
        index = bisect_right([s[0] for s in signatures], tick) - 1
        return signatures[max(index, 0)]

    def bar_starts(self, until: int | None = None) -> list[int]:
        """The tick each bar starts at, following time signature changes, up to the song's end."""
        end = self.length if until is None else until
        starts, tick = [], 0
        while tick < end or not starts:
            starts.append(tick)
            tick += self.bar_ticks(tick)
        return starts

    @property
    def last_note_end(self) -> int:
        return max((n.end for t in self.tracks for n in t.notes), default=0)

    @property
    def length(self) -> int:
        """The song's length in ticks: to its end, or its last note's end, whichever is later."""
        return max(self.end, self.last_note_end)

    @property
    def bars(self) -> int:
        """Whole bars, counting a partly filled last bar as one."""
        return len(self.bar_starts()) if self.length else 0

    def seconds(self, tick: int | None = None) -> float:
        """The time at ``tick`` (default: the end), through the tempo map."""
        target = self.length if tick is None else tick
        tempos = self.tempos or [(0, 120.0)]
        total, previous_tick, previous_bpm = 0.0, 0, tempos[0][1]
        for at, bpm in tempos:
            if at >= target:
                break
            total += (at - previous_tick) / self.ppq * 60.0 / previous_bpm
            previous_tick, previous_bpm = at, bpm
        return total + (target - previous_tick) / self.ppq * 60.0 / previous_bpm

    def note_tracks(self) -> list[Track]:
        """The tracks that have notes: what a DAW shows as the song's parts, numbered from 1 in that order."""
        return [t for t in self.tracks if t.notes]

    def free_channel(self) -> int | None:
        """A channel no track uses (never the drum channel), for a new part."""
        used = {t.channel for t in self.tracks}
        return next((c for c in range(16) if c != DRUM_CHANNEL and c not in used), None)

    def copy(self) -> Song:
        return copy.deepcopy(self)

    # ------------------------------------------------------------------ reading

    @classmethod
    def read(cls, path: str | Path) -> Song:
        """Raises MidiError when it isn't a readable MIDI file."""
        try:
            midi = mido.MidiFile(str(path), clip=True)
        except (OSError, EOFError, ValueError, KeyError, IndexError) as exc:
            raise MidiError(f"it isn't a MIDI file I can read ({exc})") from exc
        if midi.type == 2:
            raise MidiError("it's a type 2 MIDI file (separate patterns), which DAWs don't use")
        if not midi.ticks_per_beat:
            raise MidiError("it uses SMPTE timing instead of beats")
        return cls._from_mido(midi)

    @classmethod
    def _from_mido(cls, midi: mido.MidiFile) -> Song:
        song = cls(ppq=midi.ticks_per_beat, tempos=[], time_signatures=[])
        end = 0
        for index, mido_track in enumerate(midi.tracks):
            name = ""
            tick = 0
            programs: dict[int, list[tuple[int, int]]] = defaultdict(list)
            open_notes: dict[tuple[int, int], deque[tuple[int, int]]] = defaultdict(deque)
            notes: dict[int, list[Note]] = defaultdict(list)
            events: dict[int, list[tuple[int, mido.Message]]] = defaultdict(list)
            for message in mido_track:
                tick += message.time
                kind = message.type
                if kind == "track_name" and not name:
                    name = message.name.strip()
                elif kind == "set_tempo":
                    song.tempos.append((tick, round(mido.tempo2bpm(message.tempo), 3)))
                elif kind == "time_signature":
                    song.time_signatures.append((tick, message.numerator, message.denominator))
                elif kind == "key_signature":
                    song.key_signatures.append((tick, message.key))
                elif kind in ("marker", "cue_marker"):
                    song.markers.append((tick, message.text))
                elif kind == "program_change":
                    programs[message.channel].append((tick, message.program))
                elif kind == "note_on" and message.velocity > 0:
                    open_notes[(message.channel, message.note)].append((tick, message.velocity))
                elif kind in ("note_off", "note_on"):
                    waiting = open_notes.get((message.channel, message.note))
                    if waiting:
                        start, velocity = waiting.popleft()
                        notes[message.channel].append(Note(start, max(tick, start + 1), message.note, velocity))
                elif kind in ("control_change", "pitchwheel", "aftertouch", "polytouch"):
                    events[message.channel].append((tick, message.copy(time=0)))
            end = max(end, tick)
            for (channel, pitch), waiting in open_notes.items():  # never ended: they last to the track's end
                for start, velocity in waiting:
                    notes[channel].append(Note(start, max(tick, start + 1), pitch, velocity))
            channels = sorted(set(notes) | {c for c in events if events[c]})
            if index == 0 and not channels and name:
                song.title = name  # a type 1 file's first track names the song
            for channel in channels:
                track_notes = sorted(notes.get(channel, []), key=lambda n: (n.start, n.pitch))
                first = track_notes[0].start if track_notes else 0
                changes = programs.get(channel, [])
                program = next((p for t, p in reversed(changes) if t <= first), changes[0][1] if changes else 0)
                label = name or f"Track {index + 1}"
                if len(channels) > 1:
                    label = f"{label} (channel {channel + 1})"
                song.tracks.append(Track(label, channel, program, track_notes, events.get(channel, [])))
        song.tempos = _dedupe(sorted(song.tempos, key=lambda t: t[0])) or [(0, 120.0)]
        if song.tempos[0][0] != 0:
            song.tempos.insert(0, (0, 120.0))
        song.time_signatures = _dedupe(sorted(song.time_signatures, key=lambda t: t[0])) or [(0, 4, 4)]
        if song.time_signatures[0][0] != 0:
            song.time_signatures.insert(0, (0, 4, 4))
        song.key_signatures = _dedupe(sorted(song.key_signatures, key=lambda t: t[0]))
        song.markers.sort(key=lambda m: m[0])
        last = song.last_note_end
        # A file's end-of-track can sit far past its music (or before it): trust it only when it's close.
        song.end = end if last <= end <= last + song.bar_ticks(last) * 2 else last
        return song

    # ------------------------------------------------------------------ writing

    def to_mido(self) -> mido.MidiFile:
        midi = mido.MidiFile(type=1, ticks_per_beat=self.ppq)
        end = self.length
        conductor: list[tuple[int, int, mido.MetaMessage | mido.Message]] = []
        if self.title:
            conductor.append((0, 0, mido.MetaMessage("track_name", name=self.title)))
        for tick, num, den in self.time_signatures:
            conductor.append((tick, 1, mido.MetaMessage("time_signature", numerator=num, denominator=den)))
        for tick, name in self.key_signatures:
            conductor.append((tick, 2, mido.MetaMessage("key_signature", key=name)))
        for tick, bpm in self.tempos:
            conductor.append((tick, 3, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm))))
        for tick, text in self.markers:
            conductor.append((tick, 4, mido.MetaMessage("marker", text=text)))
        midi.tracks.append(_track_from(conductor, end))
        for track in self.tracks:
            events: list[tuple[int, int, mido.MetaMessage | mido.Message]] = [
                (0, 0, mido.MetaMessage("track_name", name=track.name)),
                (0, 1, mido.Message("program_change", channel=track.channel, program=track.program)),
            ]
            for tick, message in track.events:
                events.append((tick, 3, message.copy(channel=track.channel)))
            for note in _without_overlaps(track.notes):
                velocity = min(127, max(1, note.velocity))
                events.append(
                    (note.start, 4, mido.Message("note_on", channel=track.channel, note=note.pitch, velocity=velocity))
                )
                events.append(
                    (note.end, 2, mido.Message("note_off", channel=track.channel, note=note.pitch, velocity=0))
                )
            midi.tracks.append(_track_from(events, end))
        return midi

    def write_new(self, path: Path) -> None:
        """Write the song to ``path``, which must not exist yet (raises FileExistsError): an existing file is never
        overwritten."""
        midi = self.to_mido()
        with path.open("xb") as file:
            midi.save(file=file)


def _dedupe(items: list) -> list:
    """Keep one entry per tick (the last one, as a player would end up with)."""
    by_tick: dict[int, object] = {}
    for item in items:
        by_tick[item[0]] = item
    return list(by_tick.values())


def _without_overlaps(notes: list[Note]) -> list[Note]:
    """The notes with no two of the same pitch sounding at once: a note that starts while the same pitch still
    sounds cuts the earlier one short there (two note-ons in a row would leave one note-off stuck or early)."""
    cleaned: list[Note] = []
    last: dict[int, Note] = {}
    for note in sorted(notes, key=lambda n: (n.start, n.pitch, -n.end)):
        if not 0 <= note.pitch <= 127 or note.end <= note.start:
            continue
        previous = last.get(note.pitch)
        if previous is not None and previous.end > note.start:
            if previous.start == note.start:
                continue  # the same note twice: keep the first (the longer, by the sort)
            previous.end = note.start
        copy_ = Note(note.start, note.end, note.pitch, note.velocity)
        cleaned.append(copy_)
        last[note.pitch] = copy_
    return [n for n in cleaned if n.end > n.start]


def _track_from(events: list[tuple[int, int, mido.MetaMessage | mido.Message]], end: int) -> mido.MidiTrack:
    """A mido track from (tick, order, message) events, in delta times, ending at ``end``."""
    track = mido.MidiTrack()
    now = 0
    for tick, _, message in sorted(events, key=lambda e: (e[0], e[1])):
        track.append(message.copy(time=tick - now))
        now = tick
    track.append(mido.MetaMessage("end_of_track", time=max(0, end - now)))
    return track
