"""Her panel shelf: the panels she showed that have closed, and the ones the user pinned, so they can be brought back.

A panel is gone once its time runs out; "bring back that diagram" had nothing to bring. ``PanelHost`` puts each panel
here as it closes (not the ones a feature drives, which mean nothing without it: a worksheet, an explainer, her status
panel) and every pinned panel as it's pinned. Pinned ones come back by themselves when she starts, where the user left
them. Closed ones are kept ``days`` days, at most ``LIMIT`` of them, newest first. It's all one file on this PC,
``data/visuals/shelf.json``; what's on a panel is hers, so it's data and never instructions wherever it's read.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from rapidfuzz import fuzz, utils

from octavia.core.jsonfile import read_json, write_json
from octavia.visuals.spec import PageVisual, Visual, summary

log = logging.getLogger(__name__)

FILE = "data/visuals/shelf.json"
LIMIT = 40  # closed panels kept; the pinned ones don't count
MAX_PINNED = 6  # more than this would cover the screen, and each is a window for good
SAME_NAME = 60  # rapidfuzz WRatio: "the krebs one" finds "Krebs cycle"
FIELDS = ("id", "title", "about", "at", "pinned", "bounds", "visual", "page")


@dataclass
class Kept:
    """One panel on the shelf."""

    id: str
    title: str
    about: str  # what's on it, in a few words (``spec.summary``)
    at: float  # when it was last put here
    visual: dict[str, Any] = field(default_factory=dict)  # the spec, as ``build`` takes it back
    page: bool = False  # it has a web page on it (``PageVisual``)
    pinned: bool = False
    bounds: dict[str, int] | None = None  # where the user left a pinned panel

    def build(self) -> Visual:
        """The panel again, checked as a new one would be. Raises ``ValidationError`` when it no longer fits."""
        return (PageVisual if self.page else Visual).model_validate(self.visual)


def _kept(raw: Any) -> Kept | None:
    """One saved entry, or None for anything that isn't one (a file from an older version, or edited by hand)."""
    if not isinstance(raw, dict):
        return None
    pid, title, visual = raw.get("id"), raw.get("title"), raw.get("visual")
    at = raw.get("at")
    if not (isinstance(pid, str) and isinstance(title, str) and isinstance(visual, dict)):
        return None
    if not isinstance(at, int | float):
        return None
    bounds = raw.get("bounds")
    if not (isinstance(bounds, dict) and all(isinstance(bounds.get(k), int) for k in ("x", "y", "width", "height"))):
        bounds = None
    return Kept(pid, title, str(raw.get("about") or ""), float(at), visual, raw.get("page") is True,
                raw.get("pinned") is True, bounds)  # fmt: skip


class PanelShelf:
    """The shelf's entries, saved on each change. Call everything on the event loop."""

    def __init__(
        self, path: Path, *, days: Callable[[], float] = lambda: 14.0, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = path
        self._days = days
        self._clock = clock
        raw = read_json(path, [])
        entries = [_kept(item) for item in raw] if isinstance(raw, list) else []
        self._entries: list[Kept] = [e for e in entries if e is not None]  # newest first
        self._trim()

    def _save(self) -> None:
        try:
            write_json(self.path, [{name: getattr(e, name) for name in FIELDS} for e in self._entries])
        except OSError as exc:
            log.warning("Couldn't save her panel shelf: %s", exc)

    def _trim(self) -> None:
        oldest = self._clock() - self._days() * 86400
        kept, closed = [], 0
        for entry in self._entries:
            if entry.pinned:
                kept.append(entry)
            elif entry.at >= oldest and closed < LIMIT:
                kept.append(entry)
                closed += 1
        self._entries = kept

    def get(self, panel_id: str) -> Kept | None:
        return next((e for e in self._entries if e.id == panel_id), None)

    @property
    def entries(self) -> list[Kept]:
        """Everything on it, newest first."""
        self._trim()
        return list(self._entries)

    @property
    def pinned(self) -> list[Kept]:
        return [e for e in self._entries if e.pinned]

    def put(self, panel_id: str, visual: Visual, *, pinned: bool | None = None) -> Kept:
        """Puts a panel on the shelf (or brings its entry up to date). ``pinned`` None leaves that as it was."""
        before = self.get(panel_id)
        entry = Kept(
            id=panel_id,
            title=visual.title,
            about=summary(visual),
            at=self._clock(),
            visual=visual.model_dump(by_alias=True),
            page=isinstance(visual, PageVisual),
            pinned=(before.pinned if before else False) if pinned is None else pinned,
            bounds=before.bounds if before else None,
        )
        self._entries = [entry] + [e for e in self._entries if e.id != panel_id]
        self._trim()
        self._save()
        return entry

    def unpin(self, panel_id: str) -> None:
        entry = self.get(panel_id)
        if entry is not None and entry.pinned:
            entry.pinned = False
            entry.bounds = None
            entry.at = self._clock()
            self._trim()
            self._save()

    def moved(self, panel_id: str, bounds: dict[str, int]) -> None:
        entry = self.get(panel_id)
        if entry is not None and entry.pinned:
            entry.bounds = bounds
            self._save()

    def forget(self, panel_id: str) -> bool:
        """Takes a panel off the shelf for good. Whether it was there."""
        kept = [e for e in self._entries if e.id != panel_id]
        found = len(kept) != len(self._entries)
        self._entries = kept
        if found:
            self._save()
        return found

    def clear_closed(self) -> None:
        """Forgets every closed panel; the pinned ones are the user's and stay."""
        kept = self.pinned
        if len(kept) != len(self._entries):
            self._entries = kept
            self._save()

    def find(self, name: str | None) -> Kept | None:
        """The panel ``name`` means: its id, the best match on its title or what's on it, or the newest with no name."""
        entries = self.entries
        if not name or not name.strip():
            return entries[0] if entries else None
        exact = self.get(name.strip())
        if exact is not None:
            return exact
        best, score = None, 0.0
        for entry in entries:  # newest first: on a tie the newer one stays
            found = max(
                fuzz.WRatio(name, entry.title, processor=utils.default_process),
                fuzz.partial_ratio(name, entry.about, processor=utils.default_process) * 0.8,
            )
            if found > score:
                best, score = entry, found
        return best if score >= SAME_NAME else None


def buildable(entry: Kept) -> Visual | None:
    """The entry's panel, or None when it no longer fits (saved before a change to what panels can hold)."""
    try:
        return entry.build()
    except (ValidationError, ValueError, TypeError) as exc:
        log.info("Panel %s on the shelf can't be built any more: %s", entry.id, exc)
        return None
