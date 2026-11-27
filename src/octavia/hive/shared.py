"""The shared lessons every install's prompt gets.

Two sources, one list: the ones a release ships (``shared_lessons.yaml``, filled by ``octavia hive accept``), and the
feed's items as this install last saw them (``data/hive/items.json``, ``channel.sync_items``), which arrive and leave
without an update. A lesson is left out when the feed has revoked its id, or when the user dropped it here
(``data/hive/dropped.json``: ``octavia ether drop``): what the collective learned is offered, never forced.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LESSONS_FILE = Path(__file__).with_name("shared_lessons.yaml")
ITEMS = Path("data") / "hive" / "items.json"
DROPPED = Path("data") / "hive" / "dropped.json"
MAX_SHIPPED = 20
MAX_FROM_FEED = 20
MAX_LESSONS = MAX_SHIPPED + MAX_FROM_FEED
MAX_CHARS = 300  # a lesson is a sentence or two (``channel.MAX_ITEM_CHARS``); a longer row is left out
# A lesson being measured before it's shared (``hive/measure.py``): in the prompt for this process only.
TRIAL: list[dict[str, str]] = []


def _root() -> Path | None:
    try:
        from octavia.config import find_project_root

        return find_project_root()
    except Exception:
        return None


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def dropped(root: Path | None) -> set[str]:
    """The shared items the user said no to on this install."""
    raw = _json(root / DROPPED) if root is not None else None
    return {str(i) for i in raw} if isinstance(raw, list) else set()


def set_dropped(root: Path, item_id: str, drop: bool) -> bool:
    """Drops a shared item here, or takes it back; whether anything changed."""
    now = dropped(root)
    if (item_id in now) == drop:
        return False
    now = now | {item_id} if drop else now - {item_id}
    path = root / DROPPED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(now)), encoding="utf-8")
    return True


def lessons(path: Path = LESSONS_FILE, root: Path | None = None) -> list[dict[str, str]]:
    """Every shared lesson in force here: the shipped ones, then the feed's, without the revoked and the dropped
    (``root``: the install, found when not given). The newest ``MAX_SHIPPED`` shipped and ``MAX_FROM_FEED`` from the
    feed, so neither crowds the other out, and none longer than ``MAX_CHARS``. Whatever can't be read is left out:
    this runs as every prompt is built and must never be why one isn't."""
    try:
        return _lessons(path, root if root is not None else _root())
    except Exception:  # a hand-edited file, a half-written one: her prompt goes without, never without her
        log.warning("The shared lessons couldn't be read", exc_info=True)
        return []


def _lessons(path: Path, root: Path | None) -> list[dict[str, str]]:
    import yaml

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        loaded = None
    rows = loaded.get("lessons") if isinstance(loaded, dict) else None
    synced = _json(root / ITEMS) if root is not None else None
    synced = synced if isinstance(synced, dict) else {}
    revoked = synced.get("revoked")
    out_of_force = {str(i) for i in revoked if isinstance(i, str)} if isinstance(revoked, list) else set()
    out_of_force |= dropped(root)
    seen: set[str] = set()

    def read(source: Any, id_keys: tuple[str, ...]) -> list[dict[str, str]]:
        out = []
        for row in source if isinstance(source, list) else ():
            if not isinstance(row, dict) or row.get("kind", "lesson") != "lesson":
                continue
            text = " ".join(str(row.get("text") or "").split())
            item_id = next((str(row[k]) for k in id_keys if row.get(k)), "")
            if not text or len(text) > MAX_CHARS or (item_id and (item_id in out_of_force or item_id in seen)):
                continue
            seen.update({item_id} - {""})
            out.append({"text": text, "about": str(row.get("about") or "").strip()[:80], "id": item_id})
        return out

    shipped = read(rows, ("id", "from"))[-MAX_SHIPPED:]
    from_feed = read(synced.get("items"), ("id",))[-MAX_FROM_FEED:]
    return [*shipped, *from_feed, *read(list(TRIAL), ("id",))]


def lessons_block(path: Path = LESSONS_FILE, root: Path | None = None) -> str:
    """The prompt's block of shared lessons; "" when there are none (the prompt is then exactly as before)."""
    rows = lessons(path, root)
    if not rows:
        return ""
    lines = [f"- {r['text']}" + (f" ({r['about']})" if r["about"] else "") for r in rows]
    return (
        "Lessons learned across every Octavia (reviewed, general, about apps and how to work, never about a user):\n"
        + "\n".join(lines)
    )
