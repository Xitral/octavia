"""Notes of hers that disagree: two live notes about the same thing with different details (docs/octavia-2.0.md, 3.2).

Saving a note folds it into one with the very same words (``_find_same``), and ``correct_memory`` replaces a note the
user corrects. Neither catches a second note saved beside the first: on 2026-11-02, of 108 live notes, #48 "Matthew
lives at 3408 Carina Drive" and #50 "... Corinna Drive (spelled Corinna, not Carina)" were both in her prompt, the
wrong one included, on every turn since they were saved.

* ``conflicts`` finds pairs of live notes in the same category that begin the same way (the same subject: "The
  user's friend Matthew lives") and whose content words mostly match but differ: the same thing told two ways. Two
  exams or two people are different subjects, so they aren't a pair.
* A pair where the newer note names the older one's word as wrong ("not Carina", "instead of Fridays", "rather than
  Neovim") is settled at once (``settle``): the older one is folded into the newer through the store's own correction,
  so it keeps its history and comes back with ``memory history``. Never a pinned note, and never one the user told her
  folded into one she only inferred or saw.
* Any other pair is left for her to settle where it matters: her prompt mentions it beside her notes
  (``prompt_note``), and she checks with the user if one comes up and they disagree. Nothing is decided by word
  counts alone.

Nothing here asks a model. ``octavia memory check`` prints the pairs (the measure: conflicting live pairs).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from octavia.memory.base import MemoryItem

log = logging.getLogger(__name__)

# How much of the two notes' content words must be shared for them to be about the same thing.
SAME_SUBJECT = 0.6
_WORD = re.compile(r"[a-z0-9']+")
# The words a note begins with that say what it's about ("The user's friend Matthew lives"): a pair must share them.
SUBJECT_WORDS = 5
# The newer note names a word of the older one as wrong: "not Carina", "instead of Fridays", "rather than Neovim".
_NAMES_WRONG = re.compile(r"\b(?:not|instead of|rather than|no longer)\s+([a-z0-9']+)", re.IGNORECASE)


@dataclass(frozen=True)
class Conflict:
    older: MemoryItem
    newer: MemoryItem
    overlap: float

    @property
    def correction(self) -> bool:
        """The newer note names a word of the older one as wrong ("not Carina", where the older says Carina and the
        newer otherwise doesn't): it's settled without asking. Never over a pinned note, nor over one the user told
        her by one she only inferred or saw."""
        if self.older.pinned or (self.older.source == "user" and self.newer.source != "user"):
            return False
        older = _all_words(self.older.text)
        newer_rest = _all_words(_NAMES_WRONG.sub(" ", self.newer.text))
        return any(
            (w := m.group(1).lower()) in older and w not in newer_rest for m in _NAMES_WRONG.finditer(self.newer.text)
        )

    def line(self) -> str:
        return f'#{self.older.id} "{self.older.text[:120]}" and #{self.newer.id} "{self.newer.text[:120]}"'


def _all_words(text: str) -> set[str]:
    return {w.strip("'") for w in _WORD.findall(text.lower())}


def _content(text: str) -> set[str]:
    return {w for w in _all_words(text) if len(w) > 3}


def _subject(text: str) -> tuple[str, ...]:
    return tuple(w.strip("'") for w in _WORD.findall(text.lower())[:SUBJECT_WORDS])


def conflicts(items: Sequence[MemoryItem]) -> list[Conflict]:
    """Pairs of live notes in one category about the same subject that say it differently, older first."""
    live = [m for m in items if m.superseded_by is None]
    words = {m.id: _content(m.text) for m in live}
    subjects = {m.id: _subject(m.text) for m in live}
    found: list[Conflict] = []
    for i, a in enumerate(live):
        words_a = words[a.id]
        for b in live[i + 1 :]:
            if a.category != b.category or subjects[a.id] != subjects[b.id]:
                continue
            words_b = words[b.id]
            if not words_a or not words_b or words_a == words_b:
                continue  # the same words: ``_find_same`` keeps those as one already
            overlap = len(words_a & words_b) / len(words_a | words_b)
            if overlap >= SAME_SUBJECT:
                older, newer = (a, b) if (a.created_at, a.id) <= (b.created_at, b.id) else (b, a)
                found.append(Conflict(older, newer, round(overlap, 2)))
    return found


async def settle(store: Any, items: Sequence[MemoryItem] | None = None) -> tuple[list[Conflict], list[Conflict]]:
    """Fold each older note into the newer one that corrects it (``store.correct``: history kept); returns (settled,
    left for her to settle). ``items``: the live notes, read from ``store`` when None."""
    if items is None:
        items = await store.list(limit=10_000)
    settled: list[Conflict] = []
    left: list[Conflict] = []
    folded: set[int] = set()
    for conflict in conflicts(items):
        if conflict.older.id in folded or conflict.newer.id in folded:
            continue  # one fold at a time: the next look sees what's left
        if not conflict.correction:
            left.append(conflict)
            continue
        try:
            await store.correct(conflict.older.id, conflict.newer.text, source=conflict.newer.source)
        except Exception:
            log.warning("Couldn't fold note #%s into #%s", conflict.older.id, conflict.newer.id, exc_info=True)
            left.append(conflict)
            continue
        folded.add(conflict.older.id)
        settled.append(conflict)
        log.info("Note #%s folded into #%s, which corrects it", conflict.older.id, conflict.newer.id)
    return settled, left


def prompt_note(left: Sequence[Conflict], shown: set[int]) -> str:
    """The line her prompt gets about notes that disagree among those it shows; "" when none do."""
    pairs = [c for c in left if c.older.id in shown and c.newer.id in shown]
    if not pairs:
        return ""
    listed = "; ".join(c.line() for c in pairs[:5])
    return (
        f"These notes may be the same thing told two ways: {listed}. If one comes up and they disagree, check with the "
        "user and correct_memory the one that's wrong; if both are right, they're simply different things."
    )
