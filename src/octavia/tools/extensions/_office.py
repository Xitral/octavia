"""What the Word and Excel file tools share: which file, whether it's open, and saving the parts an edit changed.

An Office file is a zip of XML parts, read and written here with the standard library (as the PowerPoint tools do).
Only the parts that change are rewritten; every other part is copied as it is. The new file is written beside the
old one and swapped in only once it's complete, so a failed edit leaves the file as it was, and the edits of one
call are all made or none is.

A file open in Word or Excel isn't edited: the app keeps its own copy and writes it back when it saves, which would
undo these edits, or these would replace changes the user hasn't saved yet. Its lock file (``~$`` and the name) says
it's open, as does Windows refusing to let it be written. Only files in the user's own folders are read or written,
and nothing leaves this PC.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import sys
import tempfile
import zipfile
from typing import Self
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from pydantic import Field, model_validator

from octavia.tools.base import ToolArgs
from octavia.tools.extensions.files import FileMemory, KnownFolders, Refused, check_openable
from octavia.tools.extensions.powerpoint import _SERIALIZE

MAX_OUTLINE = 20_000  # characters of outline in one answer
MAX_EDITS = 30
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f]")  # not allowed in XML; tabs and line breaks are kept
_DEFAULT = "aadefault"  # the prefix a part's default namespace is written with, then taken off again (sorted first)


class EditError(Exception):
    """An edit that can't be made, and why (said to the model)."""


def clean_text(text: str) -> str:
    return CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def serialize(root: Element, namespaces: dict[str, str]) -> bytes:
    """A part's XML, with the prefixes it was written with and all of its namespace declarations.

    ElementTree leaves out declarations no element uses, while ``mc:Ignorable`` names prefixes in attribute text,
    so every prefix the part declared is declared again on its root; and a part written with a default namespace
    (as Excel writes its sheets) keeps it rather than getting an invented prefix.
    """
    default = namespaces.get("")
    with _SERIALIZE:
        for prefix, uri in namespaces.items():
            if prefix and not re.fullmatch(r"ns\d+", prefix):
                ElementTree.register_namespace(prefix, uri)
        if default:
            # ElementTree's own default_namespace refuses attributes without a namespace, which every part has.
            ElementTree.register_namespace(_DEFAULT, default)
        body = ElementTree.tostring(root, encoding="unicode")
    if default:
        body = re.sub(rf"<(/?){_DEFAULT}:", r"<\1", body)
        if f"{_DEFAULT}:" in body:  # an attribute in that namespace still needs the prefix
            body = body.replace(f"xmlns:{_DEFAULT}=", f'xmlns="{default}" xmlns:{_DEFAULT}=', 1)
        else:
            body = body.replace(f"xmlns:{_DEFAULT}=", "xmlns=", 1)
    end = body.index(">")
    if body[end - 1] == "/":
        end -= 1
    declared = set(re.findall(r'\sxmlns:([^=\s]+)="', body[:end]))
    missing = "".join(
        f' xmlns:{prefix}="{uri}"'
        for prefix, uri in namespaces.items()
        if prefix and prefix not in declared and not re.fullmatch(r"ns\d+", prefix)
    )
    body = body[:end] + missing + body[end:]
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + body.encode("utf-8")


def save_parts(path: str, changed: dict[str, bytes], removed: frozenset[str] = frozenset()) -> None:
    """Writes a new copy of the file beside it with ``changed`` parts replaced and ``removed`` ones left out, then
    swaps it in. Blocking."""
    folder = os.path.dirname(path)
    handle, temp = tempfile.mkstemp(prefix="~edit-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "wb") as out:
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(out, "w") as target:
                for original in source.infolist():
                    if original.filename in removed:
                        continue
                    info = copy.copy(original)
                    if info.filename in changed:
                        target.writestr(info, changed[info.filename])
                        continue
                    big = info.file_size >= zipfile.ZIP64_LIMIT
                    with source.open(original) as reading, target.open(info, "w", force_zip64=big) as writing:
                        shutil.copyfileobj(reading, writing)
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- which file, and is it open


def lock_files(name: str, *, shortened: bool) -> set[str]:
    """The names of the hidden file an open document has beside it: "~$" and its name. Word (``shortened``) drops
    the first character of a seven-character name and the first two of a longer one."""
    names = {f"~${name}"}
    stem = os.path.splitext(name)[0]
    if shortened and len(stem) == 7:
        names.add(f"~${name[1:]}")
    elif shortened and len(stem) > 7:
        names.add(f"~${name[2:]}")
    return names


def busy_reason(path: str, *, shortened: bool = False) -> str | None:
    """Why the file can't be written safely right now ("open", "read-only", "locked"), or None. Blocking."""
    folder, name = os.path.split(path)
    if any(os.path.exists(os.path.join(folder, lock)) for lock in lock_files(name, shortened=shortened)):
        return "open"
    if not os.access(path, os.W_OK):
        return "read-only"
    if sys.platform == "win32":
        try:
            with open(path, "r+b"):  # nothing is written: this only asks whether another program holds it
                pass
        except PermissionError:
            return "locked"
        except OSError:
            pass
    return None


def busy_message(name: str, reason: str, app: str) -> str:
    if reason == "read-only":
        return f"{name} is read-only, so I can't edit it. Nothing was changed."
    what = f"is open in {app}" if reason == "open" else f"is open in another program, probably {app}"
    return (
        f"{name} {what}, so I haven't touched it: {app} would write its own copy back when it saves, undoing my "
        f"edits, or my edits would replace changes not saved yet. Ask the user to save and close it in {app} (or "
        f"offer to close {app} for them with close_application, which lets them save), then try again; they can "
        "reopen it afterwards with open_file. Nothing was changed."
    )


class FileArgs(ToolArgs):
    number: int | None = Field(default=None, ge=1, description="A find_files result number.")
    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Or a path, full or as find_files shows it.",
    )

    @model_validator(mode="after")
    def _one_file(self) -> Self:
        if (self.number is None) == (self.path is None):
            raise ValueError("give either a result number from find_files or a path, not both")
        return self


def resolve(args: FileArgs, folders: KnownFolders, memory: FileMemory, extension: str, app: str) -> tuple[str, str]:
    """The file's path and name, once it's sure to be an ``extension`` file of the user's own. Raises Refused.
    Blocking."""
    if args.number is not None:
        results = memory.results()
        if not results:
            raise Refused("I have no recent find_files results (they're kept for ten minutes). Search again first.")
        if args.number > len(results):
            raise Refused(f"There's no result {args.number}; pick 1 to {len(results)}.")
        target = results[args.number - 1].path
    else:
        assert args.path is not None
        target = args.path
    opening = check_openable(target, folders)
    found = os.path.splitext(opening.name)[1].casefold()
    if opening.kind != "file" or found != extension:
        if found == extension[:-1]:
            raise Refused(f"{opening.name} is an old {found} file; it needs saving as {extension} in {app} first.")
        raise Refused(f"{opening.name} isn't {'an' if app[0] in 'AEIOU' else 'a'} {app} ({extension}) file.")
    return opening.path, opening.name
