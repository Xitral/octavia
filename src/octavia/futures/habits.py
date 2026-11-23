"""Habits: what she kept from her experiments on herself, each one inspectable and reversible.

A habit is a variant of one of her experiments (``habit_rules.SELF_TEMPLATES``) that the experiment showed works
better for the user, kept as how she does it from then on: "keeping answers short when you sound stressed". Each
records where it came from (the experiment, and its result in words), and every change to it (kept, dropped,
restored) goes into the audit trail with what caused it, as her memory's facts keep their corrections. What a habit
says or changes is fixed in ``habit_rules``; this is only the record.

Dropping one is always possible, by the user's word or by hand (``octavia futures habits drop N``): she stops doing
it at once, and the experiment behind it is never run again on her own. Restoring brings it back.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from octavia.futures.habit_rules import habit_name
from octavia.futures.privacy import redact
from octavia.futures.store import FuturesStore

ACTIVE, DROPPED = "active", "dropped"
FROM_EXPERIMENT, FROM_USER = "experiment", "user"


@dataclass(frozen=True)
class Habit:
    id: int
    key: str  # the experiment template it came from
    variant: str
    status: str  # "active" or "dropped"
    experiment_id: int | None
    evidence: str  # the experiment's result, in words
    source: str  # "experiment" (she kept it from a result) or "user" (they asked for it back)
    created_at: float
    changed_at: float
    note: str  # why it was last changed

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    @property
    def name(self) -> str:
        return habit_name(self.key, self.variant)


def _to_habit(row: sqlite3.Row) -> Habit:
    return Habit(
        id=row["id"],
        key=row["key"],
        variant=row["variant"],
        status=row["status"],
        experiment_id=row["experiment_id"],
        evidence=row["evidence"],
        source=row["source"],
        created_at=row["created_at"],
        changed_at=row["changed_at"],
        note=row["note"],
    )


class Habits:
    """The habits table. Plain synchronous code: callers run it on the Futures worker (``FuturesService.call``)."""

    def __init__(self, store: FuturesStore) -> None:
        self.store = store

    def adopt(
        self, key: str, variant: str, *, experiment_id: int | None, evidence: str, cause: str | None = None
    ) -> Habit:
        """Keep ``variant`` of ``key`` as a habit. One already active for the key is returned as it is; one the user
        dropped stays dropped (only they bring it back)."""
        with self.store.transaction():
            row = self.store.one("SELECT * FROM habits WHERE key=? ORDER BY id DESC LIMIT 1", (key,))
            if row is not None and row["status"] == ACTIVE and row["variant"] == variant:
                return _to_habit(row)
            if row is not None and row["status"] == DROPPED:
                return _to_habit(row)
            now = self.store.clock()
            if row is not None:  # another variant of the same thing: this result replaces it
                self.store.changed(
                    "UPDATE habits SET status=?, changed_at=?, note=? WHERE id=?",
                    (DROPPED, now, f"replaced by experiment #{experiment_id}", row["id"]),
                )
                self.store.record_change(f"habit:{row['id']}", "replaced", cause=cause, detail={"by": experiment_id})
            habit_id = self.store.insert(
                "INSERT INTO habits(key, variant, status, experiment_id, evidence, source, created_at, changed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (key, variant, ACTIVE, experiment_id, redact(evidence, 600), FROM_EXPERIMENT, now, now),
            )
            self.store.record_change(
                f"habit:{habit_id}",
                "adopted",
                cause=cause or (f"experiment:{experiment_id}" if experiment_id is not None else None),
                detail={"key": key, "variant": variant, "experiment": experiment_id},
            )
        return self._must_get(habit_id)

    def drop(self, habit_id: int, *, reason: str = "", cause: str | None = None) -> Habit | None:
        """Stop doing it. None when there's no such habit or it was already dropped."""
        return self._set(habit_id, DROPPED, from_=ACTIVE, reason=reason or "the user dropped it", cause=cause)

    def restore(self, habit_id: int, *, reason: str = "", cause: str | None = None) -> Habit | None:
        """Bring a dropped habit back (the user asked). None when there's no such habit or it's already active. Any
        other active habit for the same thing is dropped: one variant of a thing at a time."""
        with self.store.transaction():
            current = self.get(habit_id)
            if current is None or current.active:
                return None
            for other in self.active():
                if other.key == current.key:
                    self._set(other.id, DROPPED, from_=ACTIVE, reason=f"habit #{habit_id} restored", cause=cause)
            return self._set(
                habit_id, ACTIVE, from_=DROPPED, reason=reason or "the user asked for it back", cause=cause
            )

    def _set(self, habit_id: int, status: str, *, from_: str, reason: str, cause: str | None) -> Habit | None:
        with self.store.transaction():
            current = self.get(habit_id)
            if current is None or current.status != from_:
                return None
            self.store.changed(
                "UPDATE habits SET status=?, changed_at=?, note=?, source=CASE WHEN ?='active' THEN 'user' "
                "ELSE source END WHERE id=?",
                (status, self.store.clock(), redact(reason, 200), status, habit_id),
            )
            self.store.record_change(
                f"habit:{habit_id}",
                "restored" if status == ACTIVE else "dropped",
                cause=cause,
                detail={"key": current.key, "variant": current.variant, "reason": redact(reason, 200)},
            )
        return self.get(habit_id)

    def get(self, habit_id: int) -> Habit | None:
        row = self.store.one("SELECT * FROM habits WHERE id=?", (habit_id,))
        return _to_habit(row) if row else None

    def _must_get(self, habit_id: int) -> Habit:
        found = self.get(habit_id)
        if found is None:
            raise ValueError(f"there's no habit #{habit_id}")
        return found

    def active(self) -> list[Habit]:
        return [_to_habit(r) for r in self.store.execute("SELECT * FROM habits WHERE status=? ORDER BY id", (ACTIVE,))]

    def list(self, limit: int = 20) -> list[Habit]:
        """Every habit, newest first, dropped ones too."""
        rows = self.store.execute("SELECT * FROM habits ORDER BY id DESC LIMIT ?", (limit,))
        return [_to_habit(r) for r in rows]

    def history(self, habit_id: int) -> list[dict[str, object]]:
        """Every change to a habit, newest first (the audit trail)."""
        return self.store.changes(f"habit:{habit_id}")
