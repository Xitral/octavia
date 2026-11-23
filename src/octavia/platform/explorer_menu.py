"""Her submenu in Explorer's right-click menu: putting it in, taking it out, and whether it's there.

Right-click a file, a folder, or the empty space in a folder or on the desktop, and an entry with her name and her
raven opens a submenu (``VERBS``): ask her about the file, have it summarized or explained, study it with her, see
what's in a folder, talk to her, open her chat, see what's running, ask about what's on the clipboard. Each entry
runs ``explorer_handler`` (``pythonw -I -m``: no console, and nothing in the folder clicked in is imported), which
hands the request to her and returns at once, so Explorer never waits on her.

Everything is the user's own registry (``HKCU\\Software\\Classes``: no admin) under one key name of hers in three
places (``LOCATIONS``), so taking it out is deleting those three keys; ``scripts/remove-explorer-menu.reg`` does the
same without her. Before a key is written or deleted, whatever is there is exported to a .reg file under
``data/explorer_menu/backups`` (double-click one to put it back). The setting ``system.explorer_menu`` says whether
it should be there; she makes it so as she starts and when the setting changes (``sync``).

On Windows 11 these entries are in the full ("classic") menu, which is under "Show more options" unless the classic
menu is switched on (``classic_menu``); the new compact menu only lists apps that register a packaged extension.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from octavia.edition import windows_name

log = logging.getLogger(__name__)

KEY = windows_name("OctaviaAssistant", "OCTAVIAAssistant")  # the key's name under each ``shell``; the label: her name
CLASSES = r"Software\Classes"
OWNER_VALUE = "OctaviaMenu"  # on each of her keys: the checkout it runs, to tell an install of this one from another
HANDLER = "octavia.platform.explorer_handler"
FOLDER = Path("data") / "explorer_menu"
BACKUPS_KEPT = 30  # .reg files: the latest ten changes' worth
ICON_SIZES = (16, 20, 24, 32, 48, 64, 256)
# Windows 11's full menu by default: this per-user class with an empty server is the documented-by-everyone switch.
CLASSIC_MENU_KEY = r"Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Verb:
    name: str  # what the handler is told (``explorer_handler.VERBS``)
    label: str  # shown in the menu; {name} is hers


# Where her submenu goes (a class under HKCU\Software\Classes), what %-argument Explorer passes there, and what's in it.
LOCATIONS: dict[str, tuple[str, tuple[Verb, ...]]] = {
    "*": (
        "%1",
        (
            Verb("ask", "Ask {name} about this"),
            Verb("summarize", "Summarize"),
            Verb("explain", "Explain"),
            Verb("study", "Study this with {name}"),
        ),
    ),
    "Directory": (
        "%1",
        (
            Verb("ask", "Ask {name} about this folder"),
            Verb("summarize", "What's in here?"),
        ),
    ),
    r"Directory\Background": (  # a folder's empty space, and the desktop's: no path needed ("%V" of a drive is "C:\")
        "",
        (
            Verb("talk", "Talk to {name}"),
            Verb("chat", "Open chat"),
            Verb("running", "What's running?"),
            Verb("clipboard", "Ask about my clipboard"),
        ),
    ),
}


class MenuError(Exception):
    """The menu couldn't be put in or taken out; the message says what, in words for the user."""


def _winreg():  # type: ignore[no-untyped-def]
    import winreg

    return winreg


def key_paths(classes: str = CLASSES) -> list[str]:
    """Her three keys, under HKCU."""
    return [rf"{classes}\{where}\shell\{KEY}" for where in LOCATIONS]


def pythonw(root: Path) -> Path:
    """The windowless interpreter of the checkout's own environment."""
    return root / ".venv" / "Scripts" / "pythonw.exe"


def command_line(root: Path, verb: str, argument: str) -> str:
    """What Explorer runs for an entry. The path is quoted (spaces); ``-I`` keeps the folder Explorer starts it in
    (and PYTHON* variables) out of the import path: a ``octavia.py`` in Downloads must never run on a right-click."""
    line = f'"{pythonw(root)}" -I -m {HANDLER} {verb}'
    return f'{line} "{argument}"' if argument else line


def icon_path(root: Path) -> Path:
    return root / FOLDER / "octavia.ico"


def make_icon(root: Path) -> Path:
    """Her raven (the tray's emblem, idle) as an .ico with the sizes Explorer asks for, written once."""
    path = icon_path(root)
    if path.exists():
        return path
    import numpy as np
    from PIL import Image

    from octavia.platform.tray import emblem

    path.parent.mkdir(parents=True, exist_ok=True)
    big = Image.fromarray(np.asarray(emblem(256), dtype=np.uint8), "RGBA")
    images = {size: Image.fromarray(np.asarray(emblem(size), dtype=np.uint8), "RGBA") for size in ICON_SIZES}
    tmp = path.with_suffix(".tmp")
    # Each size drawn at that size (the raven is hand-simplified to read at 16 px), not the big one shrunk.
    big.save(tmp, format="ICO", sizes=[(s, s) for s in ICON_SIZES], append_images=list(images.values()))
    tmp.replace(path)
    return path


def backup(paths: list[str], root: Path, *, stamp: str | None = None) -> list[Path]:
    """Export each key that's there (with everything under it) to a .reg file before it's changed; returns them."""
    winreg = _winreg()
    folder = root / FOLDER / "backups"
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() // 1_000_000 % 1000:03d}"
    saved: list[Path] = []
    for i, path in enumerate(paths):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, path))
        except OSError:
            continue  # not there: nothing to keep
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{stamp}-{i}.reg"
        done = subprocess.run(
            ["reg", "export", rf"HKCU\{path}", str(target), "/y"],
            capture_output=True,
            text=True,
            creationflags=_NO_WINDOW,
            timeout=20,
        )
        if done.returncode != 0 or not target.exists():
            raise MenuError(f"Couldn't back up {path} before changing it: {(done.stderr or done.stdout).strip()}")
        saved.append(target)
    if saved:
        old = sorted(folder.glob("*.reg"))[:-BACKUPS_KEPT]
        for path in old:
            path.unlink(missing_ok=True)
    return saved


def _delete_tree(winreg, parent: int, path: str) -> None:  # type: ignore[no-untyped-def]
    """Delete a key and everything under it (winreg.DeleteKey only takes an empty one)."""
    try:
        key = winreg.OpenKey(parent, path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
    except FileNotFoundError:
        return
    with key:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_tree(winreg, key, child)
    winreg.DeleteKey(parent, path)


def install(root: Path, name: str, *, classes: str = CLASSES, icon: Path | None = None) -> list[Path]:
    """Put her submenu in (over any earlier one of hers), after backing up what was there. Returns the backups."""
    if sys.platform != "win32":
        raise MenuError("Explorer's menu is only on Windows.")
    if not pythonw(root).exists():
        raise MenuError(f"There's no environment to run her from at {pythonw(root).parent}.")
    winreg = _winreg()
    icon = icon or make_icon(root)
    saved = backup(key_paths(classes), root)
    try:
        for where, (argument, verbs) in LOCATIONS.items():
            path = rf"{classes}\{where}\shell\{KEY}"
            _delete_tree(winreg, winreg.HKEY_CURRENT_USER, path)  # nothing left over from an older set of entries
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, name)
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, str(icon))
                winreg.SetValueEx(key, "SubCommands", 0, winreg.REG_SZ, "")  # a submenu, its entries under \shell
                winreg.SetValueEx(key, OWNER_VALUE, 0, winreg.REG_SZ, str(root))
            for i, verb in enumerate(verbs, start=1):
                entry = rf"{path}\shell\{i:02d}{verb.name}"  # Explorer orders them by key name
                # Several files selected: Explorer runs the entry once per file (``explorer_handler`` gathers them),
                # and shows it for up to 15 (its default), so a big selection never starts hundreds of runs.
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, entry, 0, winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, verb.label.format(name=name))
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"{entry}\command", 0, winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command_line(root, verb.name, argument))
    except OSError as exc:
        _remove_quietly(winreg, classes)
        raise MenuError(
            f"Windows wouldn't let her write the menu entries ({exc}); nothing was left half-done."
        ) from exc
    _notify_shell()
    log.info("Put her entries in Explorer's right-click menu (backups: %d)", len(saved))
    return saved


def _remove_quietly(winreg, classes: str) -> None:  # type: ignore[no-untyped-def]
    for path in key_paths(classes):
        try:
            _delete_tree(winreg, winreg.HKEY_CURRENT_USER, path)
        except OSError:
            log.warning("Couldn't take out %s", path, exc_info=True)


def uninstall(root: Path, *, classes: str = CLASSES) -> list[Path]:
    """Take her submenu out (backing it up first). Returns the backups; none when it wasn't there."""
    if sys.platform != "win32":
        return []
    winreg = _winreg()
    saved = backup(key_paths(classes), root)
    try:
        for path in key_paths(classes):
            _delete_tree(winreg, winreg.HKEY_CURRENT_USER, path)
    except OSError as exc:
        raise MenuError(f"Windows wouldn't let her take the menu entries out ({exc}).") from exc
    if saved:
        _notify_shell()
        log.info("Took her entries out of Explorer's right-click menu")
    return saved


def status(root: Path, *, classes: str = CLASSES) -> str:
    """ "installed" (all of it, running this checkout), "other" (there, but another checkout's or an older set),
    or "absent"."""
    if sys.platform != "win32":
        return "absent"
    winreg = _winreg()
    found = 0
    current = True
    for where, (argument, verbs) in LOCATIONS.items():
        path = rf"{classes}\{where}\shell\{KEY}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                owner, _ = winreg.QueryValueEx(key, OWNER_VALUE)
                icon, _ = winreg.QueryValueEx(key, "Icon")
        except OSError:
            continue
        found += 1
        if str(owner) != str(root) or not Path(str(icon)).exists():
            current = False
            continue
        for i, verb in enumerate(verbs, start=1):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{path}\shell\{i:02d}{verb.name}\command") as key:
                    line, _ = winreg.QueryValueEx(key, "")
            except OSError:
                current = False
                break
            if line != command_line(root, verb.name, argument):
                current = False
                break
    if not found:
        return "absent"
    return "installed" if found == len(LOCATIONS) and current else "other"


def classic_menu() -> bool | None:
    """Whether Windows 11's full right-click menu is on (her entries show straight away), or None off Windows."""
    if sys.platform != "win32":
        return None
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLASSIC_MENU_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return sys.getwindowsversion().build < 22000  # Windows 10 only has the full menu
    return value == ""


def where_to_find(name: str) -> str:
    """Where the user finds her entries on this PC, in words."""
    if classic_menu() is False:
        return (
            f'Right-click a file, a folder or the desktop and choose "Show more options" (or Shift+right-click): '
            f"{name}'s entries are in that full menu."
        )
    return f"Right-click a file, a folder or the desktop: {name}'s entries are there."


def sync(root: Path, name: str, on: bool, *, classes: str = CLASSES) -> str | None:
    """Make the menu match the setting: put in (or brought up to date) when on, taken out when off. Returns what
    changed in words, or None when nothing did. A checkout without its own environment (a worktree) leaves it be."""
    if sys.platform != "win32" or not pythonw(root).exists():
        return None
    now = status(root, classes=classes)
    if on and now != "installed":
        install(root, name, classes=classes)
        return f"{name} is in Explorer's right-click menu. {where_to_find(name)}"
    if not on and now == "installed":  # another checkout's is that one's to take out
        uninstall(root, classes=classes)
        return f"{name} is out of Explorer's right-click menu."
    return None


def _notify_shell() -> None:
    """Tell Explorer the associations changed (it re-reads them anyway, but this makes it at once)."""
    try:
        import ctypes

        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED, SHCNF_IDLIST
    except Exception:
        log.debug("SHChangeNotify failed", exc_info=True)
