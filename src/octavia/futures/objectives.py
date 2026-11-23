"""Objectives: what the user wants, kept apart from what Octavia merely noticed.

"My priority this week is the physics lab" is the user's own word, and it outranks anything she infers. What she
infers ("you seem to put the essay first every evening") is only a proposal until the user agrees: it's labelled
"I noticed; not confirmed" wherever it shows up, and her suggestions don't lean on it until then. Once the user says
yes it becomes theirs ("I noticed; you agreed"); a proposal they turn down is never proposed again. One they ask her
to forget isn't either: its words are deleted, and only a hash of them is kept so the same idea isn't raised again.

Objectives are few and short, and their words are kept through ``privacy.redact`` like any free text she keeps.
Every change to one is in the audit trail (``objective:<id>``), without its words, so forgetting one forgets them.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

from octavia.futures.privacy import redact
from octavia.futures.schema import Knowledge
from octavia.futures.store import FuturesStore

USER_SAID = "user_said"
OBSERVED = "observed"
STATUSES = ("active", "proposed", "declined", "done", "archived")
MAX_TEXT = 200
MAX_EVIDENCE = 300
# The statuses an observed objective only reaches through the user's yes (``confirm``, then ``done``/``archive``).
_CONFIRMED = frozenset({"active", "done", "archived"})
_KV = "objectives"  # store.kv namespace: the hashes of forgotten objectives


@dataclass(frozen=True)
class Objective:
    id: int
    text: str
    source: str  # "user_said" or "observed"
    status: str  # "active", "proposed", "declined", "done" or "archived"
    domain: str
    weight: float  # how much it counts next to the user's other objectives
    evidence: str  # for an observed one: what she noticed
    hypothesis_id: int | None  # the pattern it came from, if any
    entity: str | None  # what it's about, as a world-model ref ("project:physics")
    created_at: float
    updated_at: float

    @property
    def confirmed(self) -> bool:
        """The user's own: they said it, or agreed when she proposed it."""
        return self.source == USER_SAID or self.status in _CONFIRMED

    @property
    def knowledge(self) -> Knowledge:
        return Knowledge.USER_PREFERENCE if self.confirmed else Knowledge.OBSERVATION

    @property
    def label(self) -> str:
        if self.source == USER_SAID:
            return "you said"
        return "I noticed; you agreed" if self.confirmed else "I noticed; not confirmed"


def _to_objective(row: sqlite3.Row) -> Objective:
    return Objective(
        id=row["id"],
        text=row["text"],
        source=row["source"],
        status=row["status"],
        domain=row["domain"],
        weight=float(row["weight"]),
        evidence=row["evidence"],
        hypothesis_id=row["hypothesis_id"],
        entity=row["entity"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def normalize(text: str) -> str:
    """The form objectives are compared in: "Finish the  lab report." and "finish the lab report" are one."""
    text = " ".join(unicodedata.normalize("NFKC", text or "").lower().split())
    start, end = 0, len(text)
    while start < end and _edge(text[start]):
        start += 1
    while end > start and _edge(text[end - 1]):
        end -= 1
    return text[start:end]


def _edge(ch: str) -> bool:
    """Punctuation, symbols or space around the words: quotes, a full stop, an exclamation mark."""
    return ch.isspace() or unicodedata.category(ch)[0] in "PS"


def _fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:32]


def _clean(text: str) -> str:
    clean = redact(text, MAX_TEXT)
    if not normalize(clean):
        raise ValueError("an objective needs some words")
    return clean


def _weight(weight: float) -> float:
    weight = float(weight)
    if not math.isfinite(weight) or weight < 0:
        raise ValueError("an objective's weight is a number of zero or more")
    return weight


def _domain(domain: str | None) -> str:
    return " ".join(str(domain or "").split()).lower() or "general"


class Objectives:
    """The objectives table."""

    def __init__(self, store: FuturesStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ the user's own
    def add(
        self,
        text: str,
        *,
        domain: str = "general",
        weight: float = 1.0,
        entity: str | None = None,
        cause: str | None = None,
    ) -> Objective:
        """An objective the user said: active straight away. An active one with the same words is returned with its
        weight updated; one she had only proposed, or that was declined or finished, becomes the user's and active
        again, so the same objective is never two rows."""
        clean, weight = _clean(text), _weight(weight)
        with self.store.transaction():
            now = self.store.clock()
            existing = self._same(clean)
            if existing is not None and existing.status == "active":
                if existing.weight != weight or (entity is not None and entity != existing.entity):
                    self.store.changed(
                        "UPDATE objectives SET weight=?, entity=COALESCE(?, entity), updated_at=? WHERE id=?",
                        (weight, entity, now, existing.id),
                    )
                    self._record(existing.id, "weight", cause, weight=weight, old_weight=existing.weight)
                return self._must_get(existing.id)
            if existing is not None:
                self.store.changed(
                    "UPDATE objectives SET source=?, status='active', weight=?, domain=?, entity=COALESCE(?, entity), "
                    "updated_at=? WHERE id=?",
                    (USER_SAID, weight, _domain(domain), entity, now, existing.id),
                )
                self._record(existing.id, "said", cause, status="active", old_status=existing.status)
                return self._must_get(existing.id)
            objective_id = self.store.insert(
                "INSERT INTO objectives(text, source, status, domain, weight, evidence, hypothesis_id, entity, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (clean, USER_SAID, "active", _domain(domain), weight, "", None, entity, now, now),
            )
            self._record(objective_id, "added", cause, status="active", source=USER_SAID, domain=_domain(domain))
        return self._must_get(objective_id)

    # ------------------------------------------------------------------ what she noticed
    def propose(
        self,
        text: str,
        *,
        evidence: str,
        hypothesis_id: int | None = None,
        domain: str = "general",
        cause: str | None = None,
    ) -> Objective | None:
        """An objective she noticed, waiting for the user to confirm or decline it. None when any objective, in any
        status, already has these words (a declined one is never proposed again), or the user had one like it
        forgotten."""
        clean = _clean(text)
        with self.store.transaction():
            if self._same(clean) is not None or _fingerprint(clean) in self._forgotten():
                return None
            now = self.store.clock()
            objective_id = self.store.insert(
                "INSERT INTO objectives(text, source, status, domain, weight, evidence, hypothesis_id, entity, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    clean,
                    OBSERVED,
                    "proposed",
                    _domain(domain),
                    1.0,
                    redact(evidence, MAX_EVIDENCE),
                    hypothesis_id,
                    None,
                    now,
                    now,
                ),
            )
            self._record(objective_id, "proposed", cause, status="proposed", source=OBSERVED, hypothesis=hypothesis_id)
        return self._must_get(objective_id)

    # ------------------------------------------------------------------ status changes
    def confirm(self, objective_id: int, *, cause: str | None = None) -> Objective | None:
        """The user agreed with a proposal: it's an active objective now. None when it isn't a proposal."""
        return self._move(objective_id, "active", ("proposed",), "confirmed", cause)

    def decline(self, objective_id: int, *, cause: str | None = None) -> Objective | None:
        """The user said no to a proposal; it's kept (without weight) so it isn't proposed again."""
        return self._move(objective_id, "declined", ("proposed",), "declined", cause)

    def done(self, objective_id: int, *, cause: str | None = None) -> Objective | None:
        """An active objective was reached."""
        return self._move(objective_id, "done", ("active",), "done", cause)

    def archive(self, objective_id: int, *, cause: str | None = None) -> Objective | None:
        """An active (or reached) objective that no longer matters to the user."""
        return self._move(objective_id, "archived", ("active", "done"), "archived", cause)

    def set_weight(self, objective_id: int, weight: float, *, cause: str | None = None) -> Objective | None:
        """How much an objective counts next to the others (1.0 is ordinary). None when there's no such one."""
        weight = _weight(weight)
        with self.store.transaction():
            current = self.get(objective_id)
            if current is None:
                return None
            if current.weight != weight:
                self.store.changed(
                    "UPDATE objectives SET weight=?, updated_at=? WHERE id=?",
                    (weight, self.store.clock(), objective_id),
                )
                self._record(objective_id, "weight", cause, weight=weight, old_weight=current.weight)
        return self.get(objective_id)

    def _move(
        self, objective_id: int, status: str, from_: tuple[str, ...], change: str, cause: str | None
    ) -> Objective | None:
        with self.store.transaction():
            current = self.get(objective_id)
            if current is None or current.status not in from_:
                return None
            self.store.changed(
                "UPDATE objectives SET status=?, updated_at=? WHERE id=?", (status, self.store.clock(), objective_id)
            )
            self._record(objective_id, change, cause, status=status, old_status=current.status)
        return self.get(objective_id)

    # ------------------------------------------------------------------ reading
    def get(self, objective_id: int) -> Objective | None:
        row = self.store.one("SELECT * FROM objectives WHERE id=?", (objective_id,))
        return _to_objective(row) if row else None

    def _must_get(self, objective_id: int) -> Objective:
        found = self.get(objective_id)
        if found is None:  # only if someone deleted it in between
            raise ValueError(f"there's no objective #{objective_id}")
        return found

    def active(self) -> list[Objective]:
        """The user's current objectives, the weightiest first."""
        rows = self.store.execute(
            "SELECT * FROM objectives WHERE status='active' ORDER BY weight DESC, updated_at DESC, id DESC"
        )
        return [_to_objective(r) for r in rows]

    def proposed(self) -> list[Objective]:
        """What she noticed and the user hasn't answered yet, newest first."""
        rows = self.store.execute("SELECT * FROM objectives WHERE status='proposed' ORDER BY created_at DESC, id DESC")
        return [_to_objective(r) for r in rows]

    def list(self, *, status: str | None = None) -> list[Objective]:
        """Every objective (or those with one status), most recently changed first."""
        sql, params = "SELECT * FROM objectives", []
        if status is not None:
            sql += " WHERE status=?"
            params.append(status)
        return [_to_objective(r) for r in self.store.execute(sql + " ORDER BY updated_at DESC, id DESC", params)]

    # ------------------------------------------------------------------ forgetting
    def forget(self, objective_id: int, *, cause: str | None = None) -> bool:
        """Delete an objective and its words. A hash of the words is kept so she doesn't propose it again (the user
        can still add it themselves)."""
        with self.store.transaction():
            current = self.get(objective_id)
            if current is None:
                return False
            self.store.changed("DELETE FROM objectives WHERE id=?", (objective_id,))
            forgotten = self._forgotten()
            forgotten.add(_fingerprint(current.text))
            self.store.kv_set(_KV, "forgotten", sorted(forgotten))
            self._record(objective_id, "forgotten", cause, old_status=current.status)
        return True

    def delete_all(self) -> int:
        """Every objective, and the memory of forgotten ones: a fresh start."""
        with self.store.transaction():
            count = self.store.changed("DELETE FROM objectives")
            self.store.kv_delete(_KV, "forgotten")
            self.store.record_change("objectives", "deleted_all", detail={"count": count})
        return count

    # ------------------------------------------------------------------ helpers
    def _same(self, text: str) -> Objective | None:
        """The objective with these words, if any: the active one when there are several."""
        wanted = normalize(text)
        rows = self.store.execute("SELECT * FROM objectives ORDER BY updated_at DESC, id DESC")
        matches = [_to_objective(r) for r in rows if normalize(r["text"]) == wanted]
        return next((m for m in matches if m.status == "active"), matches[0] if matches else None)

    def _forgotten(self) -> set[str]:
        return set(self.store.kv_get(_KV, "forgotten", []) or [])

    def _record(self, objective_id: int, change: str, cause: str | None, **detail: Any) -> None:
        # Never the objective's words: forgetting one must forget them, and the audit trail outlives the row.
        self.store.record_change(f"objective:{objective_id}", change, cause=cause, detail=detail)
