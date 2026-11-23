"""Settings changed from her windows (the tray's device picker, the settings window) rather than in the config files.

They're kept in ``data/settings.json`` and laid over ``config/default.yaml`` and ``config/local.yaml`` each time the
config loads. Each one remembers the value the config files gave when it was made (``under``): once the files say
something else (the user edited ``local.yaml`` since), the file wins and the old choice is dropped, so neither way of
setting something is silently ignored. The same rule as her voice volume (``audio/voice_volume.py``).

Keys are dotted paths into the config ("audio.input_device"); only the keys in ``EDITABLE`` can be set this way.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from octavia.settings_catalog import config_keys

log = logging.getLogger(__name__)

FILE = Path("data") / "settings.json"
_MISSING = object()

# What her windows may change: the settings window's (``settings_catalog``), and the tray's devices. Anything else
# stays a config-file job.
EDITABLE = (
    frozenset(
        {
            "audio.input_device",
            "audio.output_device",
            "audio.output_volume",
            "hotkeys.mute",
            "hotkeys.chat",
            "scheduler.quiet_hours",
            "futures.quiet_hours",
            "avatar.enabled",
            "system.tray_icon",
        }
    )
    | config_keys()
)


def _get(data: dict[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def put(data: dict[str, Any], key: str, value: Any) -> None:
    *parents, last = key.split(".")
    node = data
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[last] = value


def read(root: Path) -> dict[str, dict[str, Any]]:
    """The saved choices: ``{key: {"value": ..., "under": ...}}`` (empty when there are none or the file is bad)."""
    try:
        data = json.loads((root / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in EDITABLE and isinstance(v, dict) and "value" in v}


def apply(data: dict[str, Any], root: Path, skip: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """``data`` (the config files, merged) with the saved choices laid over it. A choice made under a value the files
    no longer give is left out, and so is any in ``skip``."""
    for key, entry in read(root).items():
        if key in skip:
            continue
        current = _get(data, key)
        if (None if current is _MISSING else current) != entry.get("under"):
            log.info("Ignoring %s from %s: the config files changed it since", key, FILE)
            continue
        put(data, key, entry["value"])
    return data


def save(root: Path, values: dict[str, Any], path: str | Path | None = None) -> None:
    """Keep ``values`` (``{key: value}``), each with what the config files give for it now (``path``: the local
    config file she was started with, when it isn't the usual one). Raises ValueError for a key that can't be set
    this way, OSError when the file can't be written."""
    unknown = sorted(set(values) - EDITABLE)
    if unknown:
        raise ValueError(f"These can't be changed from her windows: {', '.join(unknown)}")
    from octavia.config import file_values

    files = file_values(root, path)
    saved = read(root)
    for key, value in values.items():
        under = _get(files, key)
        saved[key] = {"value": value, "under": None if under is _MISSING else under}
    _write(root, saved)


def _write(root: Path, saved: dict[str, Any]) -> None:
    path = root / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    from octavia.core.live_settings import replace

    replace(tmp, path)


def forget(root: Path, keys: list[str]) -> None:
    """Drop the choices for ``keys``: they follow the config files again. Raises OSError when it can't write."""
    saved = read(root)
    if not any(key in saved for key in keys):
        return
    for key in keys:
        saved.pop(key, None)
    _write(root, saved)


def value(root: Path, key: str, default: Any = None) -> Any:
    """A saved choice, whether or not it still applies (for showing what was picked)."""
    entry = read(root).get(key)
    return default if entry is None else entry["value"]
