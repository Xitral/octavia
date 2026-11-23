r"""Where a public install keeps what's the user's: memories, settings, keys and models, outside the program folder.

The program lives in its install folder (``%LOCALAPPDATA%\Programs\OCTAVIA``), which an update rewrites and an
uninstall deletes. Everything that's the user's lives in ``%LOCALAPPDATA%\OCTAVIA`` (or ``OCTAVIA_USER_DIR``) and
outlives both, so installing again later finds it all where it was:

* ``data\`` (memories, notes, history, logins): the install's ``data`` folder is a junction to it, so every
  ``data/...`` path in the code lands there unchanged;
* ``models\`` (the downloaded speech models): through ``OCTAVIA_MODELS_DIR``, no download again;
* ``.env`` and ``config\local.yaml``: read from here (``state_root``).

The owner's own install (the personal edition, ``edition.py``) keeps everything in its checkout, as always, unless
``OCTAVIA_USER_DIR`` says otherwise.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from octavia import edition

log = logging.getLogger(__name__)

ENV = "OCTAVIA_USER_DIR"
LINKED = ("data",)


def user_dir() -> Path | None:
    """The user's folder, or None when the install keeps everything in its own folder (the owner's checkout)."""
    if os.environ.get(ENV):
        return Path(os.environ[ENV])
    if not edition.PUBLIC:
        return None
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "OCTAVIA"


def state_root(root: Path) -> Path:
    """Where ``.env`` and ``config/local.yaml`` are: the user's folder, else the install."""
    return user_dir() or root


def is_junction(path: Path) -> bool:
    try:
        return path.is_junction() or path.is_symlink()
    except OSError:
        return False


def _junction(link: Path, target: Path) -> None:
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError):
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def attach(root: Path) -> list[str]:
    """Makes the install use the user's folder (see the module docstring); returns what it did. Safe to call on
    every start: what's already in place is left alone, and nothing of the user's is ever deleted."""
    home = user_dir()
    if home is None:
        return []
    done: list[str] = []
    for sub in ("data", "models", "config"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    for name in LINKED:
        link, target = root / name, home / name
        if is_junction(link):
            continue
        if link.exists():
            if any(target.iterdir()):
                log.warning("%s and %s both hold data: the install's is used until you merge them", link, target)
                continue
            # A first start from an unzipped folder: what's there moves to the user's folder.
            for item in link.iterdir():
                shutil.move(str(item), str(target / item.name))
            link.rmdir()
            done.append(f"moved {name} to {target}")
        _junction(link, target)
        done.append(f"linked {name} to {target}")
    os.environ.setdefault("OCTAVIA_MODELS_DIR", str(home / "models"))
    return done


def detach(root: Path) -> None:
    """Removes the junctions (never what they point to): the uninstaller's first step."""
    for name in LINKED:
        link = root / name
        if is_junction(link):
            os.rmdir(link)  # a junction is removed as an empty folder: its target is untouched
