"""Outcomes: how things actually turned out. Without them there is nothing to learn from.

An outcome is one measured result of something: a to-do finished on time or not (``task_on_time``), one of her
tool calls working (``tool_call``), a brain turn going through uncorrected (``brain_turn``), a build passing its
checks (``build``), a suggestion she made being taken up (``recommendation``). Each records what was expected, what
happened, whether that counts as a success, how long it took, and the conditions it happened in (``features``,
small categorical values such as ``{"due_part_of_day": "evening", "nudged": "yes"}``), which is what the
hypothesis finder compares.

An outcome with a ``key`` is recorded once: the same to-do can't be counted twice however many times its
completion is seen.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from octavia.futures.stats import Tally
from octavia.futures.store import FuturesStore, dumps, loads


@dataclass(frozen=True)
class Outcome:
    id: int
    kind: str
    subject: str
    key: str | None
    success: bool | None  # None: neither (a neutral or unknown result)
    expected: str | None
    actual: str | None
    value: float | None  # a measured quantity, when there is one (minutes late, seconds taken)
    started_at: float | None
    resolved_at: float
    time_to_outcome_s: float | None
    rating: int | None  # the user's own verdict: -1, 0 or 1
    feedback: str | None
    cost_usd: float | None
    model: str | None
    tools: tuple[str, ...]
    retries: int
    features: Mapping[str, str]
    event_id: str | None
    domain: str


def _to_outcome(row: sqlite3.Row) -> Outcome:
    return Outcome(
        id=row["id"],
        kind=row["kind"],
        subject=row["subject"],
        key=row["key"],
        success=None if row["success"] is None else bool(row["success"]),
        expected=row["expected"],
        actual=row["actual"],
        value=row["value"],
        started_at=row["started_at"],
        resolved_at=row["resolved_at"],
        time_to_outcome_s=row["time_to_outcome_s"],
        rating=row["rating"],
        feedback=row["feedback"],
        cost_usd=row["cost_usd"],
        model=row["model"],
        tools=tuple(loads(row["tools"], [])),
        retries=row["retries"],
        features=loads(row["features"], {}),
        event_id=row["event_id"],
        domain=row["domain"],
    )


class Outcomes:
    """The outcomes table."""

    def __init__(self, store: FuturesStore) -> None:
        self.store = store

    def record(
        self,
        kind: str,
        subject: str,
        *,
        success: bool | None,
        key: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
        value: float | None = None,
        started_at: float | None = None,
        resolved_at: float | None = None,
        rating: int | None = None,
        feedback: str | None = None,
        cost_usd: float | None = None,
        model: str | None = None,
        tools: Iterable[str] = (),
        retries: int = 0,
        features: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        domain: str = "general",
    ) -> Outcome | None:
        """Record an outcome. Returns it, or None when one with this ``key`` is already recorded."""
        resolved_at = resolved_at if resolved_at is not None else self.store.clock()
        taken = resolved_at - started_at if started_at is not None else None
        clean = {str(k): str(v) for k, v in (features or {}).items() if v is not None and v != ""}
        try:
            outcome_id = self.store.insert(
                "INSERT INTO outcomes(kind, subject, key, expected, actual, success, value, started_at, "
                "resolved_at, time_to_outcome_s, rating, feedback, cost_usd, model, tools, retries, features, "
                "event_id, domain) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    subject,
                    key,
                    expected,
                    actual,
                    None if success is None else int(success),
                    value,
                    started_at,
                    resolved_at,
                    taken,
                    rating,
                    feedback,
                    cost_usd,
                    model,
                    dumps(list(tools)),
                    retries,
                    dumps(clean),
                    event_id,
                    domain,
                ),
            )
        except sqlite3.IntegrityError:
            if key is not None and self.by_key(key) is not None:
                return None
            raise
        return self.get(outcome_id)

    def rate(self, outcome_id: int, rating: int, feedback: str | None = None) -> Outcome | None:
        """The user's verdict on an outcome, after the fact."""
        self.store.execute(
            "UPDATE outcomes SET rating=?, feedback=COALESCE(?, feedback) WHERE id=?", (rating, feedback, outcome_id)
        )
        return self.get(outcome_id)

    def get(self, outcome_id: int) -> Outcome | None:
        row = self.store.one("SELECT * FROM outcomes WHERE id=?", (outcome_id,))
        return _to_outcome(row) if row else None

    def by_key(self, key: str) -> Outcome | None:
        row = self.store.one("SELECT * FROM outcomes WHERE key=?", (key,))
        return _to_outcome(row) if row else None

    def list(
        self, kind: str | None = None, *, subject: str | None = None, since: float | None = None, limit: int = 100
    ) -> list[Outcome]:
        """Outcomes newest first."""
        where, params = _where(kind, subject, since)
        rows = self.store.execute(f"SELECT * FROM outcomes{where} ORDER BY resolved_at DESC LIMIT ?", (*params, limit))
        return [_to_outcome(r) for r in rows]

    def kinds(self) -> list[str]:
        return [r["kind"] for r in self.store.execute("SELECT DISTINCT kind FROM outcomes ORDER BY kind")]

    def tally(
        self,
        kind: str,
        *,
        since: float | None = None,
        where: Mapping[str, str] | None = None,
        limit: int | None = None,
    ) -> Tally:
        """Successes out of outcomes that have one (neutral ones don't count), optionally only those whose features
        match ``where`` exactly, and only the newest ``limit``."""
        successes = trials = 0
        for outcome in self._decided(kind, since, limit):
            if where and any(outcome.features.get(k) != str(v) for k, v in where.items()):
                continue
            trials += 1
            successes += int(bool(outcome.success))
        return Tally(successes, trials)

    def by_feature(self, kind: str, feature: str, *, since: float | None = None) -> dict[str, Tally]:
        """For each value of ``feature``, the tally of outcomes with that value (outcomes without it are left
        out)."""
        groups: dict[str, Tally] = {}
        for outcome in self._decided(kind, since, None):
            value = outcome.features.get(feature)
            if value is None:
                continue
            groups[value] = groups.get(value, Tally()) + Tally(int(bool(outcome.success)), 1)
        return groups

    def groups(self, kind: str, *, since: float | None = None) -> dict[str, dict[str, Tally]]:
        """``by_feature`` for every feature at once, in one pass over the outcomes: ``{feature: {value: tally}}``,
        features in name order."""
        found: dict[str, dict[str, Tally]] = {}
        for outcome in self._decided(kind, since, None):
            one = Tally(int(bool(outcome.success)), 1)
            for feature, value in outcome.features.items():
                values = found.setdefault(feature, {})
                values[value] = values.get(value, Tally()) + one
        return {feature: found[feature] for feature in sorted(found)}

    def features(self, kind: str, *, since: float | None = None) -> list[str]:
        """The feature names outcomes of this kind carry."""
        names: set[str] = set()
        for outcome in self._decided(kind, since, None):
            names.update(outcome.features)
        return sorted(names)

    def delete(self, *, subject: str | None = None, kind: str | None = None, ids: Iterable[int] | None = None) -> int:
        clauses, params = [], []
        if subject is not None:
            clauses.append("subject=?")
            params.append(subject)
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if ids is not None:
            ids = list(ids)
            if not ids:
                return 0
            clauses.append(f"id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if not clauses:
            raise ValueError("say which outcomes to delete")
        return self.store.changed("DELETE FROM outcomes WHERE " + " AND ".join(clauses), params)

    def _decided(self, kind: str, since: float | None, limit: int | None) -> list[Outcome]:
        where, params = _where(kind, None, since)
        sql = f"SELECT * FROM outcomes{where} AND success IS NOT NULL ORDER BY resolved_at DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_to_outcome(r) for r in self.store.execute(sql, params)]


def _where(kind: str | None, subject: str | None, since: float | None) -> tuple[str, list[Any]]:
    clauses, params = ["1=1"], []
    for column, op, value in (("kind", "=", kind), ("subject", "=", subject), ("resolved_at", ">=", since)):
        if value is not None:
            clauses.append(f"{column}{op}?")
            params.append(value)
    return " WHERE " + " AND ".join(clauses), params
