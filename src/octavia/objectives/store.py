"""The objectives, kept in one JSON file so they survive a restart (written whole, through a temporary file)."""

from __future__ import annotations

import logging
from pathlib import Path

from octavia.core.jsonfile import read_json, write_json
from octavia.objectives.models import Objective

log = logging.getLogger(__name__)

INDEX_FILE = "objectives.json"
MAX_KEPT = 30  # ended objectives remembered; open ones are always kept


class ObjectiveStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.objectives: list[Objective] = self._load()

    @property
    def path(self) -> Path:
        return self.folder / INDEX_FILE

    def _load(self) -> list[Objective]:
        raw = read_json(self.path, "the objectives")
        out = []
        for item in raw if isinstance(raw, list) else []:
            try:
                out.append(Objective.from_json(item))
            except (TypeError, ValueError):
                log.warning("Skipped an objective that couldn't be read: %r", str(item)[:200])
        return out

    def save(self) -> None:
        ended = [o for o in self.objectives if not o.open]
        dropped = {id(o) for o in ended[:-MAX_KEPT]}
        self.objectives = [o for o in self.objectives if id(o) not in dropped]
        try:
            write_json(self.path, [o.to_json() for o in self.objectives], indent=1)
        except OSError:
            log.warning("Couldn't save the objectives", exc_info=True)

    def next_id(self) -> int:
        return max((o.id for o in self.objectives), default=0) + 1

    def find(self, ref: str) -> Objective | None:
        """By number ("2", "#2") or by words in its title or goal (the latest that matches)."""
        ref = ref.strip()
        if ref.lstrip("#").isdigit():
            number = int(ref.lstrip("#"))
            return next((o for o in self.objectives if o.id == number), None)
        wanted = ref.casefold()
        matches = [
            o for o in self.objectives if wanted and (wanted in o.title.casefold() or wanted in o.goal.casefold())
        ]
        return max(matches, key=lambda o: o.id) if matches else None
