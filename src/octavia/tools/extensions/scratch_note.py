"""Scratch notes: text she wants to show the user (a study diagram, a list, a draft) in a Notepad window of its own.

Typing into Notepad went wrong: Notepad reopens the user's own tabs when it starts, so the text landed in one of
their files (and its formatting mode mangled it as it was typed). So the note is never typed: it's written to a new
file of its own in a scratch folder, and Notepad opens that file in a new tab. Nothing that exists is ever written
to, which is why it's safe without asking.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

MAX_CHARS = 20_000
TITLE_CHARS = 40
FOLDER_NAME = "Octavia notes"
_UNSAFE = re.compile(r"[^\w\- ]+")  # file names keep letters, digits, spaces and hyphens only


def scratch_folder() -> Path:
    """Where the notes go: the user's temp folder, so they don't pile up among the user's documents."""
    return Path(tempfile.gettempdir()) / FOLDER_NAME


def file_title(title: str) -> str:
    """A title as a safe file name: "Krebs cycle: diagram" is "Krebs cycle diagram"; "note" when nothing is left."""
    cleaned = " ".join(_UNSAFE.sub(" ", title).split())[:TITLE_CHARS].strip()
    return cleaned or "note"


def write_new_note(folder: Path, title: str, text: str, now: datetime) -> Path:
    """Write ``text`` to a new file in ``folder`` and return its path. Opened with "x", so an existing file is never
    touched: a name that's taken gets a number."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{file_title(title)} {now:%Y-%m-%d %H%M%S}"
    body = text.replace("\r\n", "\n")
    for attempt in range(1, 100):
        path = folder / (f"{stem}.txt" if attempt == 1 else f"{stem} ({attempt}).txt")
        try:
            with path.open("x", encoding="utf-8", newline="\r\n") as file:
                file.write(body)
        except FileExistsError:
            continue
        return path
    raise FileExistsError(f"too many notes named {stem}")


def open_in_notepad(path: Path) -> None:
    """Notepad with ``path`` in a tab of its own. Our own file names hold no quotes, so the quoted path is one
    argument."""
    os.startfile("notepad.exe", "open", f'"{path}"')  # type: ignore[attr-defined]


class ShowNoteArgs(ToolArgs):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)  # a diagram's leading spaces are its shape

    text: str = Field(min_length=1, max_length=MAX_CHARS)
    title: str = Field(default="note", max_length=80, description="For the file name, e.g. 'Krebs cycle'.")


class ShowNote(Tool):
    name = "show_note"
    description = (
        "Show text (diagram, list, draft) in a new Notepad tab as its own new file. Use instead of opening "
        "Notepad and typing with type_text, which would type into the user's reopened files."
    )
    Args = ShowNoteArgs
    risk = RiskLevel.SAFE  # writes only a new file in its own scratch folder, never an existing one
    category = "files"
    timeout_s = 20.0

    def __init__(
        self,
        folder: Path | None = None,
        opener: Callable[[Path], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._folder = folder
        self._opener = opener or open_in_notepad
        self._clock = clock or datetime.now
        self._windows_only = opener is None

    async def describe_call(self, args: ShowNoteArgs, ctx: ToolContext) -> str:
        return f"show the note “{file_title(args.title)}” in Notepad"

    async def precheck(self, args: ShowNoteArgs, ctx: ToolContext) -> str | None:
        if self._windows_only and sys.platform != "win32":
            return "Showing notes in Notepad only works on Windows."
        return None

    async def run(self, args: ShowNoteArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        folder = self._folder if self._folder is not None else scratch_folder()
        try:
            path = await asyncio.to_thread(write_new_note, folder, args.title, args.text, self._clock())
        except OSError as exc:
            return ToolResult.error(f"I couldn't write the note ({exc.strerror or exc}).")
        try:
            await asyncio.to_thread(self._opener, path)
        except OSError as exc:
            return ToolResult.error(f"I wrote the note to {path}, but Notepad didn't open ({exc.strerror or exc}).")
        return ToolResult.success(
            f"Opened the note in a new Notepad tab ({path.name}). It's already there: don't type it again.",
            data={"path": str(path), "chars": len(args.text)},
        )


TOOLS = [ShowNote]
