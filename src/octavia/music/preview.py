"""A rough listen to a MIDI file through the synth Windows already has: the Microsoft GS Wavetable Synth, played
with the Windows MCI sequencer (winmm), with no window and nothing to install.

It sounds like a basic General MIDI synth, not like BandLab's instruments: it's for hearing the notes, not the
mix. One file plays at a time, starting a new one stops the last, and ``stop`` ends it. MCI calls all run on one
worker thread of their own, since MCI devices belong to the thread that opened them. MCI can't open a path longer
than 127 characters, so a file deep in a long folder plays from a copy in the temp folder.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ALIAS = "octavia_midi_preview"


class PreviewError(RuntimeError):
    pass


def available() -> bool:
    return sys.platform == "win32"


def _send(command: str) -> str:
    import ctypes

    winmm = ctypes.WinDLL("winmm")
    buffer = ctypes.create_unicode_buffer(256)
    error = winmm.mciSendStringW(command, buffer, 255, None)
    if error:
        message = ctypes.create_unicode_buffer(256)
        winmm.mciGetErrorStringW(error, message, 255)
        raise PreviewError(message.value or f"MCI error {error}")
    return buffer.value


MCI_PATH_LIMIT = 127  # the MCI sequencer can't open a longer path ("cannot find the specified file")


def _short_path(path: Path) -> str:
    """The 8.3 form of a path, when the drive keeps them; else the path."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.WinDLL("kernel32").GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value if 0 < length < 1024 else str(path)


def _playable(path: Path) -> str:
    """A path MCI can open: the file's own when it's short enough, else a copy in the temp folder."""
    short = _short_path(path)
    if len(short) <= MCI_PATH_LIMIT:
        return short
    copy = Path(tempfile.gettempdir()) / "octavia-midi-preview.mid"
    shutil.copyfile(path, copy)
    return _short_path(copy)


class MciPlayer:
    """Plays one MIDI file at a time through Windows' own synth."""

    def __init__(self) -> None:
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="midi-preview")
        self._open = False

    def _stop(self) -> bool:
        if not self._open:
            return False
        try:
            _send(f"stop {ALIAS}")
        finally:
            _send(f"close {ALIAS}")
            self._open = False
        return True

    def _play(self, path: Path) -> float:
        self._stop()
        _send(f'open "{_playable(path)}" type sequencer alias {ALIAS}')
        self._open = True
        _send(f"set {ALIAS} time format milliseconds")
        length = float(_send(f"status {ALIAS} length") or 0) / 1000
        _send(f"play {ALIAS} from 0")
        return length

    async def play(self, path: Path) -> float:
        """Start playing ``path``; returns its length in seconds."""
        return await asyncio.get_running_loop().run_in_executor(self._worker, self._play, path)

    async def stop(self) -> bool:
        """Stop what's playing; False when nothing was."""
        return await asyncio.get_running_loop().run_in_executor(self._worker, self._stop)
