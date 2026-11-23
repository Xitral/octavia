"""Small JSON state files under data/ (what the integrations remember between runs), read and written one way.

Every integration used to carry its own copy: read or give up quietly, and write through a temporary file so a
crash or a full disk never leaves half a file behind. What a file holds, and what to do when it can't be written,
stays with its owner.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def read_json(path: Path, what: str | None = None) -> Any | None:
    """The JSON in ``path``, or None when there's no such file or it can't be read or parsed. ``what`` ("the
    scheduled jobs"): a file that's there but unreadable is logged as a warning naming it."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        if what:
            log.warning("Couldn't read %s in %s", what, path, exc_info=True)
        return None
    try:
        return json.loads(text)
    except ValueError:
        if what:
            log.warning("Couldn't read %s in %s: it isn't valid JSON", what, path)
        return None


def write_json(path: Path, data: Any, *, indent: int | None = None, sort_keys: bool = False) -> None:
    """Writes ``data`` to ``path`` all at once: to a temporary file next to it, then renamed over it, so the file is
    always either the old one or the new one. Makes the folder if needed. Raises OSError when it can't be written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=sort_keys), encoding="utf-8")
    temp.replace(path)
