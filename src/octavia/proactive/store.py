"""Her overnight work, item by item: what it was, why, how its check went, her guess at the user's answer and the
answer. One JSON file (``items.json``), written whole through a temporary file, the newest ``MAX_KEPT`` kept."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from octavia.core.jsonfile import read_json, write_json

log = logging.getLogger(__name__)

MAX_KEPT = 300

PLANNED = "planned"  # suggest level: waits for the user's yes before anything runs
RUNNING = "running"  # its objective is under way
READY = "ready"  # done, and its check passed: the only status that counts as done
UNVERIFIED = "unverified"  # it ran, but its check didn't pass
FAILED = "failed"  # it couldn't run (or was cancelled)
STATUSES = (PLANNED, RUNNING, READY, UNVERIFIED, FAILED)

APPROVE, REJECT, REDIRECT = "approve", "reject", "redirect"
DECISIONS = (APPROVE, REJECT, REDIRECT)


@dataclass
class Item:
    id: int
    night: str  # the morning it's for (ISO date)
    key: str  # rules.Candidate.key: the same thing on another night has the same key
    kind: str
    title: str
    goal: str
    why: str = ""
    features: dict[str, str] = field(default_factory=dict)
    due: float | None = None
    project: str | None = None
    status: str = PLANNED
    objective: int | None = None
    files: list[str] = field(default_factory=list)  # the verified results
    evidence: str = ""  # what its check found
    problem: str = ""  # why it didn't pass, or couldn't run
    branch: str | None = None  # a project item's verified code
    prediction: float | None = None  # her guess that they'd want it (approve or redirect), made when it was planned
    basis: str = ""  # what the guess rests on
    decision: str = ""  # "" until they answer
    note: str = ""  # what they said with it
    decided_at: float | None = None
    told: bool = False  # handed over in a morning handoff
    rank_why: str = ""  # the ranker's reason, when it ranked
    created: float = 0.0
    subject: str = ""  # rules.Candidate.subject: what a claim about it must name
    plan: dict[str, Any] | None = None  # its plan as the rules wrote it (None: a project's, planned in the project)

    @property
    def decided(self) -> bool:
        return bool(self.decision)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Item:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


class ItemStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.items: list[Item] = self._load()

    @property
    def path(self) -> Path:
        return self.folder / "items.json"

    def _load(self) -> list[Item]:
        raw = read_json(self.path, "her overnight work")
        out = []
        for row in raw if isinstance(raw, list) else []:
            try:
                out.append(Item.from_json(row))
            except (TypeError, ValueError):
                log.warning("Skipped an overnight item that couldn't be read: %r", str(row)[:200])
        return out

    def save(self) -> None:
        self.items = self.items[-MAX_KEPT:]
        try:
            write_json(self.path, [i.to_json() for i in self.items], indent=1)
        except OSError:
            log.warning("Couldn't save her overnight work", exc_info=True)

    def next_id(self) -> int:
        return max((i.id for i in self.items), default=0) + 1

    def add(self, item: Item) -> Item:
        item.created = item.created or time.time()
        self.items.append(item)
        self.save()
        return item

    def get(self, item_id: int) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    def for_night(self, night: str) -> list[Item]:
        return [i for i in self.items if i.night == night]

    def by_key(self, key: str) -> list[Item]:
        return [i for i in self.items if i.key == key]

    def waiting(self) -> list[Item]:
        """Handed over (or ready to be) and not answered yet: ready, planned, or ended without passing."""
        return [i for i in self.items if not i.decided and i.status in (READY, PLANNED, UNVERIFIED)]
