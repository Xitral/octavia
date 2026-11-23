"""The user's own files: ``find_files`` looks for files and folders by name, ``open_file`` opens one of them.

The search reads names, dates and sizes, never what's inside a file, and only in the user's own folders: Desktop,
Documents, Downloads, Pictures, Music and Videos wherever Windows keeps them (OneDrive or the user can move them,
even to another drive, so they're asked of Windows with SHGetKnownFolderPath), plus the top two levels of the home
folder. Where the Windows Search index covers one of those folders, the index is asked (``search_index``: well under
a second, where walking this PC's 350,000 files takes a minute); the others, and all of them when the index can't
be asked, are walked, capped (``MAX_ENTRIES``, ``BUDGET_S``) so a huge folder can't hold up the reply. Either way the
same rules apply: hidden folders, AppData, program folders (node_modules, virtual environments, caches) and links to
elsewhere are left out. The names found go to Claude, which the user accepted. The numbered results are kept in
memory for ten minutes (``FILES``), so "open number two" works.

``open_file`` opens only what opening can't run: documents, pictures, music, videos, archives and folders. An
archive is shown selected in File Explorer rather than handed to whatever app claims it. Programs, scripts,
shortcuts and installers are refused, since their default action runs them. The path is checked twice: as written,
before anything on disk is touched (so a network path is never even looked up), and again once resolved, so a link
or junction can't lead outside the user's folders.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
import re
import stat
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from octavia.core.known_documents import STORE_PATH as KNOWN_DOCUMENTS_PATH
from octavia.core.known_documents import describe, known_documents
from octavia.platform.apps import _startfile
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

MAX_ENTRIES = 250_000  # files and folders walked through in one search (about 3 s here with a warm disk cache)
BUDGET_S = 3.0  # and the time the walk may take
MAX_RESULTS = 10
PICKED_TTL_S = 4 * 3600.0  # a file picked in Explorer stays readable for follow-up questions this long
RESULTS_TTL_S = 600.0  # how long open_file can use a search's numbers
HOME_DEPTH = 2  # the home folder is listed this many levels down, no deeper
QUERY_CHARS = 100

Kind = Literal["any", "document", "image", "music", "video", "folder", "archive"]
Where = Literal["anywhere", "desktop", "documents", "downloads", "pictures", "music", "videos"]

DOCUMENTS = frozenset(
    {".pdf", ".txt", ".md", ".rtf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".csv"}
)
IMAGES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff", ".avif"})
MUSIC = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff"})
VIDEOS = frozenset({".mp4", ".m4v", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".mpg", ".mpeg", ".3gp"})
ARCHIVES = frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"})
KINDS = {"document": DOCUMENTS, "image": IMAGES, "music": MUSIC, "video": VIDEOS, "archive": ARCHIVES}
# Handed to their usual app. Nothing else is: whatever isn't listed here may run something when it's opened (a
# program, script, shortcut or installer, a macro-enabled document, a web page, an SVG with a script in it).
OPENABLE = DOCUMENTS | IMAGES | MUSIC | VIDEOS
REFUSED = "I only open documents, pictures, music, videos and folders; use open_application for programs."

# The user's folders, searched in this order after the top of the home folder: a search the caps cut short gets
# furthest in the first ones.
PLACES = ("desktop", "downloads", "documents", "pictures", "music", "videos")
LABELS = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
    "home": "Home",
}
_FOLDER_IDS = {  # KNOWNFOLDERID values (KnownFolders.h)
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "home": "5E6C858F-0E22-4760-9AFE-EA3317B67173",  # FOLDERID_Profile
}
# Never looked into, whatever their attributes: app data, and what programs and tools keep beside the user's things.
_SKIP_FOLDERS = frozenset(
    {
        "appdata",
        "node_modules",
        "bower_components",
        "__pycache__",
        "site-packages",
        "venv",
        "virtualenv",
        "cache",
        "caches",
        "$recycle.bin",
        "system volume information",
    }
)
# Left out, like File Explorer leaves them out. Windows' own files and folders (desktop.ini, $RECYCLE.BIN) are hidden
# as well as system; system alone marks a folder the user customised with a desktop.ini (its icon, a localised
# name), such as D:\Audio\Music\Sound Manager here, which Explorer shows like any other.
_HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN
_DIRECTORY = stat.FILE_ATTRIBUTE_DIRECTORY
_CHECK_EVERY = 256  # entries between looks at the clock
_WORD = re.compile(r"[^\W_]+")  # letters and digits: spaces, "_", "-" and "." all split words
_NOT_WORD = re.compile(r"[\W_]+")
_FILLER = frozenset({"my", "the", "a", "an", "file", "files", "folder", "folders"})
_DRIVE = re.compile(r"[A-Za-z]:")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_KIND_WORDS = {
    "any": "files or folders",
    "document": "documents",
    "image": "pictures",
    "music": "music files",
    "video": "videos",
    "folder": "folders",
    "archive": "archives",
}

# A known folder's name ("documents", ..., "home") -> where it is, or None when there isn't one.
KnownFolders = Callable[[str], Path | None]


# --------------------------------------------------------------------------- the user's folders


@functools.cache
def _shell() -> Any:
    """SHGetKnownFolderPath and CoTaskMemFree, typed, on first use (they only exist on Windows).

    Private ``WinDLL`` instances, so setting ``argtypes`` here can't change how other modules call them.
    """
    import ctypes
    from ctypes import wintypes as w
    from types import SimpleNamespace

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]

    get = ctypes.WinDLL("shell32").SHGetKnownFolderPath
    get.restype = ctypes.c_long
    get.argtypes = [ctypes.POINTER(GUID), w.DWORD, w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    free = ctypes.WinDLL("ole32").CoTaskMemFree
    free.restype = None
    free.argtypes = [ctypes.c_void_p]
    return SimpleNamespace(ctypes=ctypes, GUID=GUID, get=get, free=free)


def _shell_folder(folder_id: str) -> str | None:
    """Where Windows keeps a known folder; None when it has none (or it's gone: the default flags check)."""
    api = _shell()
    guid = api.GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    out = api.ctypes.c_void_p()
    result = api.get(api.ctypes.byref(guid), 0, None, api.ctypes.byref(out))
    try:
        return api.ctypes.wstring_at(out.value) if result == 0 and out.value else None
    finally:
        if out.value:  # the caller frees it, whether the call worked or not
            api.free(out)


def known_folder(name: str) -> Path | None:
    """Where one of the user's folders is ("documents", ..., or "home"): Windows says, since OneDrive or the user may
    have moved it anywhere; elsewhere, or when Windows can't say, the usual place in the home folder."""
    if sys.platform == "win32":
        try:
            found = _shell_folder(_FOLDER_IDS[name])
        except OSError:
            log.debug("Asking Windows where %s is failed", name, exc_info=True)
            found = None
        if found:
            return Path(found)
    home = Path.home()
    if name == "home":
        return home
    usual = home / LABELS[name]
    return usual if usual.is_dir() else None


def _folder(folders: KnownFolders, name: str) -> Path | None:
    try:
        return folders(name)
    except Exception:  # a resolver in a bad state costs that folder, not the search
        log.debug("Finding the %s folder failed", name, exc_info=True)
        return None


def _inside(path: str, folder: str) -> bool:
    """``path`` is ``folder`` or somewhere in it, going by the text alone (so compare two resolved paths, or two
    written ones)."""
    path, folder = os.path.normcase(path), os.path.normcase(folder)
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


@dataclass(frozen=True)
class Place:
    """One of the user's folders: how places in it are shown, where it is as Windows names it, and where it really
    is once links are followed."""

    label: str
    path: str
    real: str


def user_places(folders: KnownFolders) -> list[Place]:
    """The known folders and the home folder that exist, most specific first when one is inside another."""
    places = []
    for name in (*PLACES, "home"):
        folder = _folder(folders, name)
        if folder is not None and folder.is_dir():
            path = os.path.abspath(folder)
            places.append(Place(LABELS[name], path, os.path.realpath(path)))
    return sorted(places, key=lambda p: -len(p.real))


def shown_place(path: str, places: Sequence[Place]) -> str:
    """The folder ``path`` (resolved) is in, as the user knows it: "Documents\\School"; "" for a place itself."""
    for place in places:  # most specific first
        if _inside(path, place.real):
            parent = os.path.dirname(path)
            if not _inside(parent, place.real):
                return ""
            relative = os.path.relpath(parent, place.real)
            return place.label if relative == "." else f"{place.label}\\{relative}"
    return os.path.dirname(path)


# --------------------------------------------------------------------------- searching


class Query:
    """What to look for: the words of a name. A name matches as well as it holds them all, as whole words, or is
    exactly them."""

    def __init__(self, text: str) -> None:
        words = _WORD.findall(text.casefold())
        kept = [w for w in words if w not in _FILLER]
        self.words = tuple(dict.fromkeys(kept or words))  # "my tax file" looks for "tax"; "the file" for both
        self.exactly = {" ".join(words), " ".join(self.words)}
        self.forms = [_forms(word) for word in self.words]  # each word as it may be written

    def match(self, name: str, folder: bool) -> tuple[bool, int, bool] | None:
        """(exactly this name, how many of the words it has, all of those at the start of a word), or None."""
        text = name.casefold()
        squashed = _NOT_WORD.sub("", text)  # so "taxreturn" is found in "Tax Return" too
        found = [forms for forms in self.forms if any(form in squashed for form in forms)]
        if not found:
            return None
        tokens = _WORD.findall(text)
        whole = all(any(token.startswith(form) for token in tokens for form in forms) for forms in found)
        stem = text if folder else os.path.splitext(text)[0]
        exact = " ".join(_WORD.findall(stem)) in self.exactly or " ".join(tokens) in self.exactly
        return exact, len(found), whole


def _forms(word: str) -> tuple[str, ...]:
    """A word, and it without a plural ending ("photos" finds "photo")."""
    forms = [word]
    if len(word) > 4 and word.endswith("ies"):
        forms.append(word[:-3] + "y")
    elif len(word) > 4 and word.endswith("es"):
        forms.append(word[:-2])
    if len(word) > 3 and word.endswith("s"):
        forms.append(word[:-1])
    return tuple(forms)


@dataclass(frozen=True)
class Found:
    """A file or folder whose name matched, as find_files lists it and open_file opens it."""

    path: str
    name: str
    place: str  # the folder it's in, as the user knows it: "Documents\\School"
    folder: bool
    size: int  # bytes; 0 for a folder
    modified: float  # seconds since the epoch


@dataclass(frozen=True)
class Hit:
    found: Found
    exact: bool
    words: int  # how many of the query's words its name has
    whole: bool
    # For a match from the index, the known folder it's in: the index doesn't say whether a folder on the way is
    # hidden, so that's still to be looked at (``pick``). "" for what the walk found: it looked on its way down.
    root: str = ""


@dataclass(frozen=True)
class Root:
    """Where a search starts: a folder, how places in it are shown, and how many levels down to list (None: all)."""

    path: str
    label: str
    depth: int | None = None


@dataclass
class Walk:
    hits: list[Hit]
    looked_at: int
    complete: bool  # False when the caps stopped it


def _of_kind(kind: str, name: str, folder: bool) -> bool:
    if kind == "any":
        return True
    if kind == "folder":
        return folder
    return not folder and os.path.splitext(name)[1].casefold() in KINDS[kind]


def walk(
    roots: Sequence[Root],
    query: Query,
    kind: str = "any",
    *,
    max_entries: int = MAX_ENTRIES,
    budget_s: float = BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
    avoid: Iterable[str] = (),
) -> Walk:
    """Every name under ``roots`` that matches, breadth first through all of them at once: when the caps stop it,
    the shallow things everywhere have been seen before the deep ones anywhere. Never goes into the folders in
    ``avoid`` (searched another way). Blocking."""
    deadline = clock() + budget_s
    # each root is walked once, from itself, even when it's inside another
    starts = {os.path.normcase(path) for path in (*(root.path for root in roots), *avoid)}
    queue = deque((root.path, root.label, root.depth) for root in roots)
    hits: list[Hit] = []
    looked = 0
    while queue:
        if clock() > deadline:
            return Walk(hits, looked, complete=False)
        folder, place, depth = queue.popleft()
        # A folder's matches and subfolders count once its whole listing is read: a pyvenv.cfg anywhere in it makes
        # it a Python virtual environment, whatever it's called, and then none of it does. (Asking each folder for
        # that file instead took 40% of a search's time on this PC.)
        found: list[Hit] = []
        inner: list[tuple[str, str, int | None]] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if looked >= max_entries or (looked % _CHECK_EVERY == 0 and clock() > deadline):
                        return Walk(hits + found, looked, complete=False)
                    looked += 1
                    name = entry.name
                    if name.casefold() == "pyvenv.cfg":
                        found, inner = [], []
                        break
                    info = _listed(entry)
                    if info is None:
                        continue
                    is_folder = stat.S_ISDIR(info.st_mode)
                    if is_folder:
                        if name.casefold() in _SKIP_FOLDERS:
                            continue
                        if (depth is None or depth > 1) and os.path.normcase(entry.path) not in starts:
                            inner.append((entry.path, f"{place}\\{name}", None if depth is None else depth - 1))
                    if _of_kind(kind, name, is_folder) and (match := query.match(name, is_folder)):
                        size = 0 if is_folder else info.st_size
                        found.append(Hit(Found(entry.path, name, place, is_folder, size, info.st_mtime), *match))
        except OSError:
            continue  # gone since, or not the user's to read
        hits.extend(found)
        queue.extend(inner)
    return Walk(hits, looked, complete=True)


def _listed(entry: os.DirEntry[str]) -> os.stat_result | None:
    """An entry's details, or None when it's left out: hidden by its attributes (Windows' own files among them) or
    by a leading dot (.git, .venv, .cache), one of Office's lock files, or a link, which may lead anywhere, even back
    up the tree."""
    if entry.name.startswith((".", "~$")):
        return None
    try:
        if entry.is_symlink() or entry.is_junction():
            return None
        info = entry.stat(follow_symlinks=False)  # from the listing itself on Windows: no extra call
    except OSError:
        return None
    return None if getattr(info, "st_file_attributes", 0) & _HIDDEN else info


def rank(hits: Iterable[Hit], words: int) -> list[Hit]:
    """Best first: an exact name first, then whole words before parts of words, then the most recently changed.
    Only names with all the words, unless none has them: then those missing just one (so a misheard word still
    finds the file), most words first."""
    hits = list(hits)
    pool = [h for h in hits if h.words == words] or [h for h in hits if h.words >= words - 1]
    return sorted(pool, key=lambda h: (not h.exact, -h.words, not h.whole, -h.found.modified, h.found.name.casefold()))


def pick(ranked: Iterable[Hit], limit: int) -> list[Hit]:
    """The first ``limit`` of ``ranked``, leaving out matches from the index that are inside a hidden folder. The
    index doesn't say, so each folder on the way is looked at (once), for the few matches shown rather than the
    hundreds that aren't. Blocking."""
    hidden: dict[str, bool] = {}
    shown: list[Hit] = []
    for hit in ranked:
        if hit.root and _in_hidden_folder(hit.found.path, hit.root, hidden):
            continue
        shown.append(hit)
        if len(shown) == limit:
            break
    return shown


def _in_hidden_folder(path: str, root: str, hidden: dict[str, bool]) -> bool:
    folder = os.path.dirname(path)
    while _inside(folder, root) and os.path.normcase(folder) != os.path.normcase(root):
        key = os.path.normcase(folder)
        if key not in hidden:
            try:
                hidden[key] = bool(getattr(os.stat(folder), "st_file_attributes", 0) & _HIDDEN)
            except OSError:
                hidden[key] = False  # gone since: open_file says so if it's asked for
        if hidden[key]:
            return True
        folder = os.path.dirname(folder)
    return False


def _unique(hits: Iterable[Hit]) -> list[Hit]:
    """Each file or folder once, even if both the index and the walk found it."""
    seen: set[str] = set()
    unique = []
    for hit in hits:
        key = os.path.normcase(hit.found.path)
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    return unique


# --------------------------------------------------------------------------- the Windows Search index

# Rows asked of the index in one search, newest first. Many are left out afterwards (the index has node_modules,
# site-packages and .git in it too): on this PC the 200 newest "readme"s kept 6, the 1000 newest 430, for 0.15 s
# more. Leaving those folders out in the query itself costs more (0.6 s for each), and the index refuses several.
INDEX_TOP = 1000
INDEX_TIMEOUT_S = 3.0  # an answer that takes longer is given up on, and those folders are walked instead
INDEX_PROBE_TTL_S = 600.0  # how long "does the index cover this folder?" is remembered
INDEX_RETRY_S = 60.0  # once the index failed, it's left alone this long rather than waited on in every search
PARTIAL_MAX_WORDS = 6  # up to this many words, a search that finds nothing asks again for all but one of them
_INDEX_CONNECTION = "Provider=Search.CollatorDSO;Extended Properties='Application=Windows';"
# ItemUrl rather than ItemPathDisplay: a folder can be displayed under another name than its own (a desktop.ini),
# and on this PC Pictures, really D:\Media\New folder, is displayed as D:\Media\Pictures. FileAttributes says
# whether a match is hidden without touching the disk.
_INDEX_COLUMNS = "System.ItemUrl, System.DateModified, System.Size, System.ItemType, System.FileAttributes"

# Runs SQL statements against the index over one connection: each statement's rows, as tuples of its columns.
IndexSearch = Callable[[Sequence[str]], list[list[tuple[Any, ...]]]]


class IndexUnavailable(Exception):
    """The Windows Search index can't be asked: not Windows, no provider, the service is off, or it failed."""


def search_index(statements: Sequence[str]) -> list[list[tuple[Any, ...]]]:
    """Each statement's rows from the Windows Search index (the Search.CollatorDSO provider through ADODB), over one
    connection. Blocking: run it on a worker thread, where it gives COM an apartment of its own. Raises
    IndexUnavailable."""
    if sys.platform != "win32":
        raise IndexUnavailable("the Windows Search index is only on Windows")
    try:
        import comtypes
        import comtypes.client
    except ImportError as exc:
        raise IndexUnavailable(f"comtypes isn't installed ({exc})") from None
    comtypes.CoInitialize()
    try:
        failure = ""
        try:
            return _index_rows(comtypes.client, statements)
        except Exception as exc:  # COMError or OSError: no provider, the service is stopped, a query it refused
            failure = f"{type(exc).__name__}: {exc}"
        # Out of the except clause, the exception and the frames it held are gone, and with them the last references
        # to the COM objects: they're released now, before the apartment closes. Released after, they'd crash.
        raise IndexUnavailable(failure)
    finally:
        comtypes.CoUninitialize()


def _index_rows(client: Any, statements: Sequence[str]) -> list[list[tuple[Any, ...]]]:
    connection = client.CreateObject("ADODB.Connection", dynamic=True)
    connection.CommandTimeout = int(INDEX_TIMEOUT_S)
    connection.Open(_INDEX_CONNECTION)
    try:
        results = []
        for sql in statements:
            records = client.CreateObject("ADODB.Recordset", dynamic=True)
            records.Open(sql, connection)
            try:
                # GetRows hands over every row at once, as columns: one call, rather than one for each value.
                results.append([] if records.EOF else list(zip(*records.GetRows(), strict=True)))
            finally:
                records.Close()
        return results
    finally:
        connection.Close()


def _quoted(text: str) -> str:
    """Text for inside a SQL string: its quotes doubled."""
    return text.replace("'", "''")


def _like(text: str) -> str:
    """Text to find as it is inside a LIKE pattern: its wildcards bracketed, so "50%" isn't "50 then anything"."""
    for char in "[%_":  # "[" first: the brackets added for the others are to stay as they are
        text = text.replace(char, f"[{char}]")
    return _quoted(text)


def _scope(roots: Sequence[Root]) -> str:
    return "(" + " OR ".join(f"SCOPE='file:{_quoted(root.path)}'" for root in roots) + ")"


def _has_word(forms: Sequence[str]) -> str:
    # A form that holds another adds nothing: whatever has "photos" in its name has "photo".
    needed = [form for form in forms if not any(other != form and other in form for other in forms)]
    return "(" + " OR ".join(f"System.FileName LIKE '%{_like(form)}%'" for form in needed) + ")"


def names_sql(query: Query, roots: Sequence[Root], kind: str, *, partial: bool = False) -> str:
    """The index's newest ``INDEX_TOP`` files and folders under ``roots`` of that kind, with every word of the query
    in their names (``partial``: all but one of them)."""
    words = [_has_word(forms) for forms in query.forms]
    if partial:  # (a AND b) OR (a AND c) OR (b AND c)
        but_one = [" AND ".join(words[:i] + words[i + 1 :]) for i in range(len(words))]
        names = "(" + " OR ".join(f"({each})" for each in but_one) + ")"
    else:
        names = " AND ".join(words)
    where = [names, _scope(roots)]
    if kind == "folder":
        where.append("System.ItemType = 'Directory'")
    elif kind != "any":  # IN (...) is refused by the index
        where.append("(" + " OR ".join(f"System.FileExtension = '{ext}'" for ext in sorted(KINDS[kind])) + ")")
    return (
        f"SELECT TOP {INDEX_TOP} {_INDEX_COLUMNS} FROM SYSTEMINDEX WHERE {' AND '.join(where)} "
        "ORDER BY System.DateModified DESC"
    )


def probe_sql(root: Root) -> str:
    """Anything at all the index has in a folder: none means it doesn't cover the folder."""
    return f"SELECT TOP 1 System.ItemUrl FROM SYSTEMINDEX WHERE {_scope([root])}"


def venvs_sql(roots: Sequence[Root]) -> str:
    """The Python virtual environments under ``roots``: the folders with a pyvenv.cfg."""
    where = f"System.FileName = 'pyvenv.cfg' AND {_scope(roots)}"
    return f"SELECT TOP {INDEX_TOP} System.ItemUrl FROM SYSTEMINDEX WHERE {where}"


def url_path(url: Any) -> str | None:
    """A local file's path from its index URL ("file:D:/Media/New folder/x.png"; not percent-encoded, so a name
    holding "%20" means just that); None for anything else."""
    if not isinstance(url, str) or url[:5].casefold() != "file:":
        return None
    path = url[5:].replace("/", "\\")
    return os.path.normpath(path) if _DRIVE.match(path) and path[2:3] == "\\" else None


def index_hit(row: Sequence[Any], query: Query, kind: str, roots: Sequence[Root], venvs: set[str]) -> Hit | None:
    """A row of the index (``_INDEX_COLUMNS``) as a match, when it passes the walk's rules; ``roots`` most specific
    first, ``venvs`` the virtual environments' folders (normcased)."""
    url, modified, size, item_type, attributes = row
    path = url_path(url)
    if path is None:
        return None
    within = (r for r in roots if _inside(path, r.path) and os.path.normcase(path) != os.path.normcase(r.path))
    root = next(within, None)
    if root is None:
        return None  # not in the folders asked about
    attributes = int(attributes or 0)
    if attributes & _HIDDEN:
        return None
    folder = item_type == "Directory" or bool(attributes & _DIRECTORY)
    parts = os.path.relpath(path, root.path).split(os.sep)
    if any(part.startswith((".", "~$")) for part in parts):
        return None
    if any(part.casefold() in _SKIP_FOLDERS for part in (parts if folder else parts[:-1])):
        return None
    above = root.path
    for part in parts[:-1]:
        above = os.path.join(above, part)
        if os.path.normcase(above) in venvs:
            return None
    name = parts[-1]
    if not _of_kind(kind, name, folder):
        return None
    match = query.match(name, folder)
    if match is None:  # rare: the index matches a few letters' case differently from casefold()
        return None
    place = "\\".join((root.label, *parts[:-1]))
    when = modified.replace(tzinfo=UTC).timestamp() if isinstance(modified, datetime) else 0.0  # the index keeps UTC
    found = Found(path, name, place, folder, 0 if folder else int(size or 0), when)
    return Hit(found, *match, root=root.path)


def plain_date(when: float, now: float) -> str:
    """When something was changed, in words: "today at 3:05 PM", "yesterday", "4 days ago", "on March 3". In
    English whatever the process's locale, like the rest of what the model is told."""
    then, today = datetime.fromtimestamp(when), datetime.fromtimestamp(now)
    days = (today.date() - then.date()).days
    if days == 0:
        return f"today at {then.hour % 12 or 12}:{then.minute:02d} {'AM' if then.hour < 12 else 'PM'}"
    if days == 1:
        return "yesterday"
    if 1 < days < 7:
        return f"{days} days ago"
    day = f"{_MONTHS[then.month - 1]} {then.day}"
    return f"on {day}" if then.year == today.year else f"on {day}, {then.year}"


def plain_size(size: int) -> str:
    """A file's size in words: "812 bytes", "4.2 KB", "36 MB"."""
    if size < 1024:
        return "1 byte" if size == 1 else f"{size} bytes"
    value, unit = size / 1024, "KB"
    for bigger in ("MB", "GB", "TB"):
        if value < 1024:
            break
        value, unit = value / 1024, bigger
    number = f"{value:.1f}".removesuffix(".0") if value < 10 else f"{value:.0f}"
    return f"{number} {unit}"


class FileMemory:
    """The latest find_files results, numbered from 1, for open_file: in memory only, for ``ttl_s``. Thread-safe.

    Also the files and folders the user picked in Explorer's right-click menu (``core/explorer_requests.py``), for
    ``PICKED_TTL_S``: read_document reads those even where it reads nothing else (a .py file, another drive), since
    the user chose that very one."""

    def __init__(self, ttl_s: float = RESULTS_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.now = clock
        self._lock = threading.Lock()
        self._found: tuple[Found, ...] = ()
        self._at = 0.0
        self._picked: dict[str, float] = {}  # normalized path: when picked

    def pick(self, paths: Iterable[str]) -> None:
        with self._lock:
            now = self.now()
            self._picked = {p: at for p, at in self._picked.items() if now - at <= PICKED_TTL_S}
            for path in paths:
                self._picked[_picked_key(path)] = now

    def picked(self, path: str) -> bool:
        """Whether the user picked exactly this path in Explorer, a while ago at most."""
        with self._lock:
            at = self._picked.get(_picked_key(path))
            return at is not None and self.now() - at <= PICKED_TTL_S

    def remember(self, found: Iterable[Found]) -> None:
        """Keep a search's results; an empty list forgets the older ones, so a number never means an old result."""
        with self._lock:
            self._found, self._at = tuple(found), self.now()

    def results(self) -> list[Found]:
        with self._lock:
            if self._found and self.now() - self._at > self.ttl_s:
                self._found = ()
            return list(self._found)

    def clear(self) -> None:
        with self._lock:
            self._found = ()


def _picked_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


FILES = FileMemory()
MAX_KNOWN_LIKE = 3


# --------------------------------------------------------------------------- find_files


class FindFilesArgs(ToolArgs):
    query: str = Field(
        min_length=1,
        max_length=QUERY_CHARS,
        description="Words from the name.",
    )
    kind: Kind = Field(default="any")
    where: Where = Field(default="anywhere")


class FindFiles(Tool):
    name = "find_files"
    description = (
        "Find the user's files and folders by name (not content) in their usual folders. Returns up to 10 numbered "
        "matches for open_file and other file tools."
    )
    Args = FindFilesArgs
    risk = RiskLevel.SAFE  # reads names, dates and sizes only
    category = "files"
    timeout_s = 20.0

    def __init__(
        self,
        folders: KnownFolders | None = None,
        memory: FileMemory | None = None,
        *,
        index: IndexSearch | None = search_index,
        max_entries: int = MAX_ENTRIES,
        budget_s: float = BUDGET_S,
        index_timeout_s: float = INDEX_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        """``index`` asks the Windows Search index (``search_index``); None walks every folder instead."""
        self._folders = folders or known_folder
        self.memory = memory if memory is not None else FILES
        self._index = index
        self.max_entries = max_entries
        self.budget_s = budget_s
        self.index_timeout_s = index_timeout_s
        self._clock = clock
        self._now = now
        self._covered: dict[str, tuple[float, bool]] = {}  # folder (normcased) -> (asked when, the index covers it)
        self._index_down_until = 0.0

    async def describe_call(self, args: FindFilesArgs, ctx: ToolContext) -> str:
        return f'look for files named "{args.query}"'

    async def _ask(self, statements: Sequence[str]) -> list[list[tuple[Any, ...]]] | None:
        """The index's answers, or None when it can't give them in time; then it's left alone for a while."""
        search = self._index
        if search is None or self._clock() < self._index_down_until:
            return None
        answer: concurrent.futures.Future[list[list[tuple[Any, ...]]]] = concurrent.futures.Future()

        def ask() -> None:
            if not answer.set_running_or_notify_cancel():
                return
            try:
                answer.set_result(search(statements))
            except Exception as exc:
                answer.set_exception(exc)

        # A thread of its own rather than one of the shared pool's: an answer that never comes is given up on, and
        # mustn't keep a pool thread from the rest of the assistant.
        threading.Thread(target=ask, name="search-index", daemon=True).start()
        try:
            return await asyncio.wait_for(asyncio.wrap_future(answer), self.index_timeout_s)
        except TimeoutError:
            log.info("The Windows Search index took over %.0f s; walking the folders instead", self.index_timeout_s)
        except IndexUnavailable as exc:
            log.info("The Windows Search index can't be asked (%s); walking the folders instead", exc)
        except Exception:
            log.warning("Asking the Windows Search index failed; walking the folders instead", exc_info=True)
        self._index_down_until = self._clock() + INDEX_RETRY_S
        return None

    async def _indexed(self, known: Sequence[Root]) -> list[Root]:
        """The known folders the index covers: those it has anything at all in (remembered a while). None of them
        when there's no index to ask."""
        if self._index is None or not known:
            return []
        now = self._clock()

        def outdated(root: Root) -> bool:
            asked_at, _ = self._covered.get(os.path.normcase(root.path), (float("-inf"), False))
            return now - asked_at > INDEX_PROBE_TTL_S

        stale = [root for root in known if outdated(root)]
        if stale:
            answers = await self._ask([probe_sql(root) for root in stale])
            if answers is None:
                return []
            for root, rows in zip(stale, answers, strict=True):
                self._covered[os.path.normcase(root.path)] = (now, bool(rows))
        return [root for root in known if self._covered[os.path.normcase(root.path)][1]]

    async def _from_index(self, query: Query, roots: Sequence[Root], kind: str) -> tuple[list[Hit], bool] | None:
        """The matches the index has under ``roots``, and whether there may be more than it gave (INDEX_TOP); None
        when it can't be asked."""
        answers = await self._ask([names_sql(query, roots, kind), venvs_sql(roots)])
        if answers is None:
            return None
        rows, venv_rows = answers
        if not rows and 1 < len(query.words) <= PARTIAL_MAX_WORDS:
            again = await self._ask([names_sql(query, roots, kind, partial=True)])
            rows = again[0] if again else []
        venvs = {os.path.normcase(os.path.dirname(path)) for url, *_ in venv_rows if (path := url_path(url))}
        specific = sorted(roots, key=lambda root: -len(root.path))
        hits = [hit for row in rows if (hit := index_hit(row, query, kind, specific, venvs)) is not None]
        return hits, len(rows) >= INDEX_TOP

    def _walk(self, roots: Sequence[Root], query: Query, kind: str, avoid: Iterable[str] = ()) -> Walk:
        return walk(
            roots,
            query,
            kind,
            max_entries=self.max_entries,
            budget_s=self.budget_s,
            clock=self._clock,
            avoid=avoid,
        )

    def _roots(self, where: str) -> list[Root]:
        """Where to search: that folder, or all of them and the top of the home folder, each once. The home folder
        is searched first: two levels of it are quick, so even a search the caps cut short covers it."""
        wanted = [(name, None) for name in (PLACES if where == "anywhere" else (where,))]
        if where == "anywhere":
            wanted.append(("home", HOME_DEPTH))  # last here, so a known folder in the same place keeps its name
        roots: list[Root] = []
        seen: set[str] = set()
        for name, depth in wanted:
            folder = _folder(self._folders, name)
            if folder is None or not folder.is_dir():
                continue
            key = os.path.normcase(os.path.realpath(folder))
            if key not in seen:  # two known folders in one place: it's searched once, under the first name
                seen.add(key)
                roots.append(Root(os.path.abspath(folder), LABELS[name], depth))
        return sorted(roots, key=lambda root: root.depth is None)

    async def run(self, args: FindFilesArgs, ctx: ToolContext) -> ToolResult:
        query = Query(args.query)
        if not query.words:
            return ToolResult.error("Give some words from the name to look for.")
        roots = await asyncio.to_thread(self._roots, args.where)
        if not roots:
            where = f"your {LABELS[args.where]} folder" if args.where != "anywhere" else "your folders"
            return ToolResult.error(f"I couldn't find {where} on this PC.")
        # The index answers for the known folders it covers, while the rest (the top of the home folder, and any
        # folder the index leaves out) is walked at the same time.
        indexed = await self._indexed([root for root in roots if root.depth is None])
        others = [root for root in roots if root not in indexed]
        avoid = [root.path for root in indexed]
        walking = asyncio.to_thread(self._walk, others, query, args.kind, avoid)
        asked = None
        if indexed:
            found, asked = await asyncio.gather(walking, self._from_index(query, indexed, args.kind))
            if asked is None:  # the index gave out after all: those folders are walked too
                rest = await asyncio.to_thread(self._walk, indexed, query, args.kind, [r.path for r in others])
                found = Walk(found.hits + rest.hits, found.looked_at + rest.looked_at, found.complete and rest.complete)
                indexed = []
        else:
            found = await walking
        from_index, capped = asked if asked is not None else ([], False)
        ranked = rank(_unique([*from_index, *found.hits]), len(query.words))
        shown = await asyncio.to_thread(pick, ranked, MAX_RESULTS)
        self.memory.remember(hit.found for hit in shown)  # numbered as shown
        data = {
            "matches": len(ranked),
            "looked_at": found.looked_at,
            "complete": found.complete,
            "indexed": [root.label for root in indexed],
        }
        scope = f" in your {LABELS[args.where]}" if args.where != "anywhere" else ""
        what = _KIND_WORDS[args.kind]
        cut_short = "The search stopped early (too many files to go through in time), so there may be more."
        if not shown:
            lines = [f'No {what}{scope} have "{args.query}" in their names.']
            if not found.complete:
                lines.append(cut_short)
            lines.append(
                "Only names are searched, not what's inside files, and hidden and program folders are skipped. Try "
                "other words from the name."
            )
            return ToolResult.success(" ".join(lines) + _known_like(args.query, ctx), data=data)
        if shown[0].words < len(query.words):
            head = f'No {what}{scope} have all of "{args.query}" in their names; these have some of those words:'
        else:
            matches = "match" if len(ranked) == 1 else "matches"
            at_least = " or more" if capped else ""  # the index gave all the rows it was asked for
            head = f'Found {len(ranked)}{at_least} {matches}{scope} for "{args.query}", best first:'
        now = self._now()
        lines = [head]
        for number, hit in enumerate(shown, 1):
            item = hit.found
            about = [f"in {item.place}", f"modified {plain_date(item.modified, now)}"]
            if not item.folder:
                about.append(plain_size(item.size))
            lines.append(f"{number}. {item.name}{' (folder)' if item.folder else ''}, " + ", ".join(about))
        if len(ranked) > len(shown):
            lines.append(f"({len(ranked) - len(shown)} more not shown.)")
        if not found.complete:
            lines.append(cut_short)
        lines.append("To open one, call open_file with its number.")
        return ToolResult.success("\n".join(lines) + _known_like(args.query, ctx), data=data)


def _known_like(query: str, ctx: ToolContext) -> str:
    """What she's read lately that ``query`` could mean, wherever the search looked and however the file is spelled."""
    known = known_documents(ctx.config.resolve_path(KNOWN_DOCUMENTS_PATH)).like(query)[:MAX_KNOWN_LIKE]
    if not known:
        return ""
    return "\nYou've read these, in case it's one (read_document with its path):\n" + "\n".join(
        describe(d) for d in known
    )


# --------------------------------------------------------------------------- open_file


class Refused(Exception):
    """A file or folder open_file won't open, and why (said to the model, and so to the user)."""


@dataclass(frozen=True)
class Opening:
    """What open_file is about to open, once checked."""

    path: str  # resolved
    name: str
    place: str
    kind: Literal["file", "folder", "archive"]


def _written_path(text: str, folders: KnownFolders) -> str:
    """The path the user or model wrote, made whole: find_files shows places from a known folder
    ("Documents\\School\\essay.docx"), and any other relative path is taken from the home folder. Only text is
    handled here: nothing on disk is touched."""
    text = text.strip().strip('"').strip()
    if not text:
        raise Refused("Give the file's name or path.")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        first, *rest = path.parts or ("",)
        base = None
        for name in (*PLACES, "home"):
            if first.casefold() == LABELS[name].casefold():
                base = _folder(folders, name)
                break
        if base is not None:
            path = base.joinpath(*rest)
        else:
            home = _folder(folders, "home")
            if home is None:
                raise Refused("Give the file's full path.")
            path = home / path
    return os.path.normpath(path)


def check_openable(text: str, folders: KnownFolders) -> Opening:
    """The file or folder ``text`` names, once it's sure to be one of the user's own and safe to open; raises Refused,
    saying why, otherwise. Blocking (it looks on disk)."""
    places = user_places(folders)
    path = _written_path(text, folders)
    # As written, before anything on disk is looked at: only a local drive (a network path would be looked up on
    # the network), no alternate data stream ("x.txt:y.pdf"), and inside the user's folders.
    drive = os.path.splitdrive(path)[0]
    if not _DRIVE.fullmatch(drive) or ":" in path[len(drive) :]:
        raise Refused("I only open files on this PC's own drives, in your folders.")
    if not any(_inside(path, place.path) or _inside(path, place.real) for place in places):
        raise Refused("That's outside your own folders, so I won't open it.")
    try:
        real = os.path.realpath(path, strict=True)
    except (OSError, ValueError):
        raise Refused("I can't find that file or folder; it may have been moved or deleted. Search again.") from None
    # Again, where it really is: a link or junction inside the user's folders may lead anywhere.
    if not any(_inside(real, place.real) for place in places):
        raise Refused("That leads outside your own folders, so I won't open it.")
    name = os.path.basename(real) or real
    place = shown_place(real, places)
    if os.path.isdir(real):
        return Opening(real, name, place, "folder")
    extension = os.path.splitext(real)[1].casefold()
    if os.path.isfile(real):
        if extension in OPENABLE:
            return Opening(real, name, place, "file")
        if extension in ARCHIVES:
            return Opening(real, name, place, "archive")
    raise Refused(f"{name} isn't something I open. {REFUSED}")


def show_in_explorer(path: str) -> None:
    """File Explorer at ``path``'s folder with it selected: the archive itself isn't opened, so nothing in it runs.
    File names can't hold a quote, so the quoted path can't break out of the argument."""
    explorer = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "explorer.exe")
    os.startfile(explorer, "open", f'/select,"{path}"')  # type: ignore[attr-defined]


class OpenFileArgs(ToolArgs):
    number: int | None = Field(default=None, ge=1, description="A find_files result number.")
    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Or a file or folder path, full or as find_files shows it.",
    )

    @model_validator(mode="after")
    def _one(self) -> Self:
        if self.number is not None and self.path is not None:
            raise ValueError("give either number or path, not both")
        if self.number is None and self.path is None:
            raise ValueError("give a result number from find_files, or a path")
        return self


class OpenFile(Tool):
    name = "open_file"
    description = (
        "Open a document or media file in its usual app, or a folder in File Explorer. Never programs, scripts, "
        "shortcuts or installers (for apps use open_application)."
    )
    Args = OpenFileArgs
    risk = RiskLevel.SAFE  # opening a document in its viewer; nothing that can run is ever opened
    category = "files"
    timeout_s = 20.0

    def __init__(
        self,
        folders: KnownFolders | None = None,
        memory: FileMemory | None = None,
        *,
        startfile: Callable[[str], Any] | None = None,
        reveal: Callable[[str], Any] | None = None,
    ) -> None:
        self._folders = folders or known_folder
        self.memory = memory if memory is not None else FILES
        self._startfile = startfile or _startfile
        self._reveal = reveal or show_in_explorer
        self._windows_only = startfile is None or reveal is None

    async def describe_call(self, args: OpenFileArgs, ctx: ToolContext) -> str:
        return f"open result {args.number}" if args.number is not None else f"open {args.path}"

    async def precheck(self, args: OpenFileArgs, ctx: ToolContext) -> str | None:
        if self._windows_only and sys.platform != "win32":
            return "Opening files only works on Windows."
        return None

    async def run(self, args: OpenFileArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        if args.number is not None:
            results = self.memory.results()
            if not results:
                return ToolResult.error(
                    "I have no recent find_files results (they're kept for ten minutes). Search again first."
                )
            if args.number > len(results):
                return ToolResult.error(f"There's no result {args.number}; pick 1 to {len(results)}.")
            target = results[args.number - 1].path
        else:
            assert args.path is not None  # the arguments' validator made sure
            target = args.path
        try:
            opening = await asyncio.to_thread(check_openable, target, self._folders)
        except Refused as exc:
            return ToolResult.error(str(exc))
        try:
            await asyncio.to_thread(self._reveal if opening.kind == "archive" else self._startfile, opening.path)
        except OSError as exc:
            return ToolResult.error(f"Windows couldn't open {opening.name} ({exc.strerror or exc}).")
        where = f" ({opening.place})" if opening.place else ""
        if opening.kind == "folder":
            content = f"Opened the {opening.name} folder{where} in File Explorer."
        elif opening.kind == "archive":
            content = (
                f"Showed {opening.name}{where} in File Explorer, selected in its folder; opening it from there shows "
                "what's inside."
            )
        else:
            content = f"Opened {opening.name}{where}."
        return ToolResult.success(content, data={"kind": opening.kind})


TOOLS = [FindFiles, OpenFile]
