"""Where the music goes and how it's named: ``Documents\\Octavia\\Music`` (wherever Windows keeps Documents, which
OneDrive or the user may have moved), with names that say what's in them, like
"2026-10-06 lofi-chill A-minor 80bpm.mid".

A file is only ever created, never overwritten: it's opened with "x", and a name that's taken gets a number,
"... (2).mid". Finding a file to read takes what people say: nothing or "latest" for the newest one here, a name
or part of one ("the lofi one"), or a full path to one anywhere.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from rapidfuzz import fuzz, process

from octavia.music.song import Song

MIDI_SUFFIXES = (".mid", ".midi")
_UNSAFE = re.compile(r"[^\w\-.#()+,& ]+")
_LATEST = {"", "latest", "last", "newest", "recent", "that", "it", "this", "the last one", "the latest one"}


class LibraryError(ValueError):
    """A file that can't be found or written; the message says why."""


def music_folder() -> Path:
    """Documents\\Octavia\\Music (not created here)."""
    from octavia.tools.extensions.files import known_folder  # Windows' own idea of where Documents is

    documents = known_folder("documents") or Path.home() / "Documents"
    return documents / "Octavia" / "Music"


def slug(text: str, limit: int = 40) -> str:
    """Words for a file name: "Lo-Fi Chill!" -> "lo-fi-chill"."""
    cleaned = re.sub(r"[^a-z0-9#]+", "-", text.casefold()).strip("-")
    return cleaned[:limit].strip("-")


def safe_name(text: str, limit: int = 120) -> str:
    """A file name part with nothing Windows forbids: "Bass (Synth Bass 1)" stays readable."""
    cleaned = " ".join(_UNSAFE.sub(" ", text).split()).strip(" .")
    return cleaned[:limit].strip(" .") or "song"


def save_new(song: Song, folder: Path, stem: str) -> Path:
    """Write ``song`` as a new file in ``folder`` (created if needed) named ``stem``.mid, or ``stem`` (2).mid and
    so on when that's taken. Returns the path."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = safe_name(stem)
    for attempt in range(1, 1000):
        path = folder / (f"{stem}.mid" if attempt == 1 else f"{stem} ({attempt}).mid")
        try:
            song.write_new(path)
        except FileExistsError:
            continue
        return path
    raise LibraryError(f"too many files are already named {stem}")


def _midi_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() in MIDI_SUFFIXES]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def find_midi(reference: str | None, folder: Path) -> Path:
    """The MIDI file the user means. Raises LibraryError saying what's there."""
    text = (reference or "").strip().strip('"').strip()
    files = _midi_files(folder)
    if text.casefold() in _LATEST:
        if not files:
            raise LibraryError(f"There are no MIDI files in {folder} yet.")
        return files[0]
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    candidates = [path] if path.is_absolute() else [folder / path, Path.home() / path]
    for candidate in candidates:
        for option in (candidate, candidate.with_name(candidate.name + ".mid")):
            if option.is_file():
                if option.suffix.casefold() not in MIDI_SUFFIXES:
                    raise LibraryError(f"{option.name} isn't a MIDI file (.mid).")
                return option
    if path.is_absolute() or len(path.parts) > 1:
        raise LibraryError(f"There's no MIDI file at {text}.")
    if not files:
        raise LibraryError(f"There are no MIDI files in {folder} yet.")
    wanted = text.casefold().removesuffix(".mid")
    words = [w for w in re.split(r"[\s\-_]+", wanted) if w]
    containing = [p for p in files if all(w in p.stem.casefold() for w in words)]
    if containing:
        return containing[0]  # the newest that matches
    match = process.extractOne(
        wanted, {p: p.stem.casefold() for p in files}, scorer=fuzz.partial_ratio, score_cutoff=75
    )
    if match:
        return match[2]
    newest = ", ".join(p.name for p in files[:5])
    raise LibraryError(f"I couldn't find a MIDI file called '{text}' in {folder}. The newest there: {newest}.")


def shown(path: Path) -> str:
    """The path as the user knows it: "Documents\\Octavia\\Music\\x.mid" when it's in their Documents."""
    try:
        documents = music_folder().parent.parent
        return str(Path("Documents") / path.relative_to(documents))
    except (ValueError, OSError):
        return str(path)
