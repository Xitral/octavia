"""Music tools: MIDI songs for the user's DAW. They make music in BandLab, a browser DAW with no API, so she writes
.mid files they drag into it (any other DAW takes them too).

- ``compose_midi`` writes a new multi-track song from a description: genre, key, tempo, meter, length or sections,
  mood, which parts (drums, bass, chords, melody, arpeggio, pad), their General MIDI sounds, a chord progression,
  complexity, humanizing, swing and a seed (the same seed gives the same song).
- ``edit_midi`` changes a file the user has: transposes it, changes its tempo, extends it, adds a part that fits
  its key and chords, changes an instrument, quantizes, humanizes or splits it into one file per track. The result
  is always a new file; the original is left as it was.
- ``describe_midi`` says what's in a file: tracks and instruments, tempo, meter, length, key and chords per bar.
- ``preview_midi`` plays one through the General MIDI synth built into Windows, for a rough listen.

They only create new files in Documents\\Octavia\\Music (never overwriting one) and read MIDI files, so they're
safe without asking. The music itself is made by ``octavia.music`` (mido for the files, music21 for the theory).
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from octavia.music import analysis, compose, edit, library, preview
from octavia.music.song import MidiError, Song
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

PartName = Literal["drums", "bass", "chords", "melody", "arpeggio", "pad"]
BANDLAB = "Tell the user to drag it into BandLab (each part comes in as its own track)."


class _MusicTool(Tool):
    """Where the files go; tests point it at a temporary folder."""

    category = "media"

    def __init__(self, folder: Path | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self._folder = folder
        self._clock = clock or datetime.now

    @property
    def folder(self) -> Path:
        return self._folder if self._folder is not None else library.music_folder()


def _saved(path: Path) -> str:
    return f"Saved “{path.name}” in {library.shown(path.parent)}"


# --------------------------------------------------------------------------- compose_midi


class SectionArg(ToolArgs):
    name: str = Field(
        min_length=1,
        max_length=40,
        description="intro, verse, pre-chorus, chorus/hook, drop, bridge, breakdown or outro.",
    )
    bars: int = Field(ge=1, le=128)


class ComposeMidiArgs(ToolArgs):
    style: str = Field(
        default="pop",
        max_length=60,
        description=("Genre, e.g. boom-bap, trap, lo-fi, house, dnb, drill."),
    )
    key: str | None = Field(
        default=None,
        max_length=30,
        description="e.g. 'A minor', 'F# major', 'D dorian'.",
    )
    tempo_bpm: int | None = Field(default=None, ge=40, le=220)
    time_signature: str = Field(default="4/4", max_length=5)
    bars: int | None = Field(default=None, ge=1, le=256, description="Default 16, or the sections' total.")
    mood: str | None = Field(default=None, max_length=80, description="e.g. 'dark', 'chill'.")
    sections: list[SectionArg] | None = Field(
        default=None,
        max_length=32,
        description="The form in order, e.g. intro 4, verse 8, chorus 8.",
    )
    parts: list[PartName] | None = Field(
        default=None,
        min_length=1,
        max_length=6,
        description="Default: the genre's.",
    )
    instruments: dict[PartName, str | int] | None = Field(
        default=None,
        description=("GM name or 0-127 per part, e.g. {'chords': 'rhodes'}."),
    )
    progression: str | None = Field(
        default=None,
        max_length=300,
        description="'i VI III VII' or 'Am F C G'.",
    )
    complexity: int = Field(default=3, ge=1, le=5, description="1 simple .. 5 busy.")
    humanize: float = Field(default=0.5, ge=0, le=1, description="0 on the grid .. 1 loose.")
    swing: int | None = Field(default=None, ge=50, le=75, description="50 straight .. 66 triplet.")
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1, description="Same seed + settings = same song.")
    title: str | None = Field(default=None, max_length=60, description="File name.")


class ComposeMidi(_MusicTool):
    name = "compose_midi"
    description = (
        "Compose an original multi-track MIDI song (beat, loop, progression, melody, or a full song) as a new "
        ".mid in Documents\\Octavia\\Music for BandLab or any DAW. To change it later use edit_midi."
    )
    Args = ComposeMidiArgs
    risk = RiskLevel.SAFE  # only creates a new file in the music folder
    timeout_s = 90.0  # music21's first import takes a few seconds

    async def describe_call(self, args: ComposeMidiArgs, ctx: ToolContext) -> str:
        return f"compose a {args.style} MIDI song"

    async def run(self, args: ComposeMidiArgs, ctx: ToolContext) -> ToolResult:
        try:
            made = await asyncio.to_thread(self._compose, args)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return ToolResult.error(f"I couldn't save the MIDI file ({exc.strerror or exc}).")
        composition, path = made
        p = composition.plan
        return ToolResult.success(
            f"{_saved(path)}: {compose.describe(composition)} {BANDLAB}",
            data={
                "path": str(path),
                "seed": p.seed,
                "bars": p.bars,
                "bpm": p.bpm,
                "key": p.key.name,
                "genre": p.genre.name,
                "tracks": [t.name for t in composition.song.tracks],
            },
        )

    def _compose(self, args: ComposeMidiArgs) -> tuple[compose.Composition, Path]:
        sections = [compose.SectionSpec(s.name, s.bars) for s in args.sections] if args.sections else None
        composition = compose.compose(
            style=args.style,
            key=args.key,
            tempo=args.tempo_bpm,
            time_signature=args.time_signature,
            bars=args.bars,
            mood=args.mood,
            sections=sections,
            parts_wanted=list(args.parts) if args.parts else None,
            instruments=dict(args.instruments) if args.instruments else None,
            progression=args.progression,
            complexity=args.complexity,
            humanize=args.humanize,
            swing=args.swing,
            seed=args.seed,
            title=args.title or "",
        )
        p = composition.plan
        words = library.slug(args.title) if args.title else "-".join(filter(None, [p.genre.key, *p.mood_words[:1]]))
        stem = f"{self._clock():%Y-%m-%d} {words} {p.key.label} {p.bpm}bpm"
        return composition, library.save_new(composition.song, self.folder, stem)


# --------------------------------------------------------------------------- edit_midi


class InstrumentChange(ToolArgs):
    track: str = Field(
        min_length=1,
        max_length=60,
        description="Number (from describe_midi), name, or role ('bass').",
    )
    instrument: str = Field(min_length=1, max_length=60, description="GM name or 0-127 (drums: a kit).")


class EditMidiArgs(ToolArgs):
    file: str | None = Field(
        default=None,
        max_length=500,
        description="Name (or part) in the music folder, or a path. Default: the newest.",
    )
    transpose: int | None = Field(default=None, ge=-24, le=24, description="Semitones; drums stay.")
    tempo_bpm: int | None = Field(default=None, ge=40, le=220)
    extend_bars: int | None = Field(default=None, ge=1, le=128, description="Bars to add by repeating the last phrase.")
    add_part: PartName | None = Field(default=None, description="Fits the file's key and chords.")
    style: str | None = Field(default=None, max_length=60, description="Genre for the added part.")
    instrument: str | None = Field(default=None, max_length=60, description="GM sound for the added part.")
    complexity: int = Field(default=3, ge=1, le=5, description="For the added part.")
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1, description="For the added part and humanizing.")
    change_instrument: InstrumentChange | None = None
    quantize: Literal["1/4", "1/8", "1/16", "1/32", "1/4t", "1/8t", "1/16t"] | None = Field(
        default=None, description="Grid (t = triplets)."
    )
    quantize_strength: float = Field(default=1.0, ge=0, le=1, description="1 = fully on the grid.")
    humanize: float | None = Field(default=None, ge=0, le=1, description="0.3 subtle .. 1 loose.")
    split_tracks: bool = Field(default=False, description="Also save each track as its own file.")

    @model_validator(mode="after")
    def _something_to_do(self) -> EditMidiArgs:
        changes = (
            self.tempo_bpm,
            self.extend_bars,
            self.add_part,
            self.change_instrument,
            self.quantize,
            self.humanize,
        )
        if not self.transpose and all(c is None for c in changes) and not self.split_tracks:
            raise ValueError(
                "say what to change: transpose, tempo_bpm, extend_bars, add_part, change_instrument, quantize, humanize or split_tracks"
            )
        return self


class EditMidi(_MusicTool):
    name = "edit_midi"
    description = (
        "Change a MIDI file for BandLab or any DAW (several changes per call), saved as a new file; the original "
        "is never changed. Use describe_midi first to see its tracks."
    )
    Args = EditMidiArgs
    risk = RiskLevel.SAFE  # reads a file, only creates new ones in the music folder
    timeout_s = 90.0

    async def describe_call(self, args: EditMidiArgs, ctx: ToolContext) -> str:
        return f"edit the MIDI file {args.file or 'just made'}"

    async def run(self, args: EditMidiArgs, ctx: ToolContext) -> ToolResult:
        try:
            return await asyncio.to_thread(self._edit, args)
        except MidiError as exc:
            return ToolResult.error(f"I couldn't read that file: {exc}.")
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return ToolResult.error(f"I couldn't read or save the file ({exc.strerror or exc}).")

    def _edit(self, args: EditMidiArgs) -> ToolResult:
        source = library.find_midi(args.file, self.folder)
        song = Song.read(source)
        if not song.note_tracks():
            return ToolResult.error(f"“{source.name}” has no notes in it to change.")
        done: list[str] = []
        labels: list[str] = []
        seed = args.seed if args.seed is not None else 1 + zlib.crc32(source.name.encode()) % 99_999
        if args.transpose:
            song = edit.transpose(song, args.transpose)
            direction = "up" if args.transpose > 0 else "down"
            done.append(
                f"transposed {direction} {abs(args.transpose)} semitone{'s' if abs(args.transpose) != 1 else ''}"
            )
            labels.append(f"{args.transpose:+d}")
        if args.tempo_bpm:
            before = song.bpm
            song = edit.set_tempo(song, args.tempo_bpm)
            done.append(f"tempo changed from {before:g} to {args.tempo_bpm} BPM")
            labels.append(f"{args.tempo_bpm}bpm")
        if args.extend_bars:
            song, repeated = edit.extend(song, args.extend_bars)
            done.append(f"extended by {args.extend_bars} bars (repeating {repeated}) to {song.bars}")
            labels.append(f"+{args.extend_bars} bars")
        if args.add_part:
            added = edit.add_part(
                song,
                args.add_part,
                style=args.style,
                instrument=args.instrument,
                complexity=args.complexity,
                humanize_amount=0.5,
                seed=seed,
            )
            song = added.song
            done.append(f"added {added.track.name}, {added.genre.name} style, in {added.key_name}")
            labels.append(f"+{args.add_part}")
        if args.change_instrument:
            song, what = edit.set_instrument(song, args.change_instrument.track, args.change_instrument.instrument)
            done.append(f"instrument changed ({what})")
            labels.append(library.slug(args.change_instrument.instrument, 20))
        if args.quantize:
            song = edit.quantize(song, args.quantize, args.quantize_strength)
            strength = "" if args.quantize_strength >= 1 else f" at {args.quantize_strength:.0%}"
            done.append(f"quantized to {args.quantize}{strength}")
            labels.append("quantized")
        if args.humanize:
            song = edit.humanize_song(song, args.humanize, seed)
            done.append(f"humanized ({args.humanize:g})")
            labels.append("humanized")
        stem = f"{source.stem} ({', '.join(labels)})" if labels else source.stem
        paths: list[Path] = []
        if labels:
            paths.append(library.save_new(song, self.folder, stem))
        if args.split_tracks:
            for name, single in edit.split(song):
                paths.append(library.save_new(single, self.folder, f"{stem} - {name}"))
            done.append(f"split into {len(edit.split(song))} single-track files")
        listing = "; ".join(f"“{p.name}”" for p in paths[:12]) + (
            f" and {len(paths) - 12} more" if len(paths) > 12 else ""
        )
        return ToolResult.success(
            f"From “{source.name}”: {', '.join(done)}. Saved {listing} in {library.shown(self.folder)}; "
            f"the original is unchanged. {BANDLAB}",
            data={"source": str(source), "paths": [str(p) for p in paths], "bars": song.bars, "bpm": song.bpm},
        )


# --------------------------------------------------------------------------- describe_midi


class DescribeMidiArgs(ToolArgs):
    file: str | None = Field(
        default=None,
        max_length=500,
        description="Name (or part) in the music folder, or a path. Default: the newest.",
    )


class DescribeMidi(_MusicTool):
    name = "describe_midi"
    description = "What's in a MIDI file: tracks, instruments, tempo, length, key, sections, chords per bar."
    Args = DescribeMidiArgs
    risk = RiskLevel.SAFE  # reads only
    timeout_s = 60.0

    async def run(self, args: DescribeMidiArgs, ctx: ToolContext) -> ToolResult:
        try:
            path = library.find_midi(args.file, self.folder)
            text = await asyncio.to_thread(lambda: analysis.describe(Song.read(path), path.name))
        except MidiError as exc:
            return ToolResult.error(f"I couldn't read that file: {exc}.")
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return ToolResult.error(f"I couldn't read the file ({exc.strerror or exc}).")
        return ToolResult.success(text, data={"path": str(path)})


# --------------------------------------------------------------------------- preview_midi


class PreviewMidiArgs(ToolArgs):
    file: str | None = Field(default=None, max_length=500, description="Default: the newest.")
    stop: bool = Field(default=False, description="Stop the playing preview.")


class PreviewMidi(_MusicTool):
    name = "preview_midi"
    description = (
        "Play a MIDI file aloud through Windows' basic MIDI synth (not BandLab's sounds). Only when the user asks "
        "to hear it."
    )
    Args = PreviewMidiArgs
    risk = RiskLevel.SAFE  # plays a sound; nothing is changed
    timeout_s = 20.0

    def __init__(
        self, folder: Path | None = None, player: preview.MciPlayer | None = None, windows: bool | None = None
    ) -> None:
        super().__init__(folder)
        self._player = player
        self._windows = preview.available() if windows is None else windows

    async def precheck(self, args: PreviewMidiArgs, ctx: ToolContext) -> str | None:
        if not self._windows:
            return "Previewing needs Windows' built-in MIDI synth; there's no free synth here. Open the file in BandLab to hear it."
        return None

    async def run(self, args: PreviewMidiArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        if self._player is None:
            self._player = preview.MciPlayer()
        try:
            if args.stop:
                stopped = await self._player.stop()
                return ToolResult.success("Stopped the preview." if stopped else "Nothing was playing.")
            path = library.find_midi(args.file, self.folder)
            seconds = await self._player.play(path)
        except (ValueError, preview.PreviewError, OSError) as exc:
            return ToolResult.error(f"I couldn't play it ({exc}).")
        length = f" ({int(seconds // 60)}:{int(seconds % 60):02d})" if seconds else ""
        return ToolResult.success(
            f"Playing “{path.name}”{length} through Windows' basic MIDI synth; BandLab's sounds will be much better. "
            "Say stop to end it (preview_midi with stop=true).",
            data={"path": str(path)},
        )


TOOLS = [ComposeMidi, EditMidi, DescribeMidi, PreviewMidi]
