"""Octavia measuring herself: how her tool calls, brain turns, builds and reasoners actually do.

Every attempt is one row in the ``performance`` table: what it was (``kind`` "tool", "brain", "build" or
"reasoner", and its ``name``), what it was for (``category``: a tool's category, a reasoning task's kind), whether
it worked, and what it cost in time, retries, money and tokens. A correction from the user is marked on every row
of the turn it was about (``mark_corrected``), since that is the verdict that matters most and it only arrives
afterwards.

This is the data foundation for choosing between providers and ways of doing things later, not the choice itself:
nothing here says "use X for Y". ``estimate`` ranks candidates by what the counts say (a Beta(1, 1) estimate, so an
untried one sits in the middle rather than at the top or the bottom), and whoever routes decides what to do with
that. ``repeated_failures`` is how she notices she keeps failing at the same thing in the same place, which is
worth a self-improvement note rather than another try.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from octavia.futures.stats import Tally
from octavia.futures.store import FuturesStore

KINDS = frozenset({"tool", "brain", "build", "reasoner"})
GROUPABLE = ("kind", "name", "category", "model", "provider", "app")  # what ``stats`` can group by
UNKNOWN_FAILURE = "unknown"  # a failure recorded without saying what kind

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class PerfStat:
    """How one group of attempts went. ``kind``, ``name`` and ``category`` are the group's own values when it was
    grouped by them, the filter's value when filtered by one, and "" otherwise (a mix); ``group`` holds the value of
    every column it was grouped by."""

    kind: str
    name: str
    category: str
    tally: Tally  # successes out of the attempts that had a verdict
    median_latency_s: float | None
    retries: int
    corrected: int  # attempts in a turn the user corrected
    cost_usd: float | None  # the total, when any attempt said what it cost
    last_ts: float
    records: int = 0  # every attempt, those without a verdict too
    group: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)


@dataclass(frozen=True)
class FailureGroup:
    """The same thing failing the same way in the same place, ``count`` times."""

    name: str
    failure: str
    app: str | None
    count: int
    last_ts: float


class Performance:
    """The performance table."""

    def __init__(self, store: FuturesStore) -> None:
        self.store = store

    def record(
        self,
        kind: str,
        name: str,
        *,
        success: bool | None,
        category: str = "",
        model: str | None = None,
        provider: str | None = None,
        failure: str | None = None,
        latency_s: float | None = None,
        retries: int = 0,
        cost_usd: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        confidence: float | None = None,
        tool_calls: int | None = None,
        turn: str | None = None,
        app: str | None = None,
        event_id: str | None = None,
        ts: float | None = None,
    ) -> int:
        """Record one attempt; returns its row id. ``success`` None is an attempt without a verdict (it doesn't
        count for or against). A ValueError for an unknown ``kind`` or an empty ``name``: a typo'd kind would
        quietly start a series nobody reads."""
        if kind not in KINDS:
            raise ValueError(f"unknown performance kind {kind!r} (one of {', '.join(sorted(KINDS))})")
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("a performance record needs a name")
        return self.store.insert(
            "INSERT INTO performance(ts, kind, name, category, model, provider, success, failure, latency_s, retries, "
            "corrected, cost_usd, tokens_in, tokens_out, confidence, tool_calls, turn, app, event_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ts if ts is not None else self.store.clock(),
                kind,
                name,
                category or "",
                model,
                provider,
                None if success is None else int(bool(success)),
                failure,
                latency_s,
                max(0, int(retries)),
                0,
                cost_usd,
                tokens_in,
                tokens_out,
                confidence,
                tool_calls,
                turn,
                app,
                event_id,
            ),
        )

    def mark_corrected(self, turn: str) -> int:
        """The user corrected turn ``turn``: every attempt in it is marked. Returns how many were newly marked."""
        if not turn:
            return 0
        return self.store.changed("UPDATE performance SET corrected=1 WHERE turn=? AND corrected=0", (turn,))

    def stats(
        self,
        *,
        kind: str | None = None,
        name: str | None = None,
        category: str | None = None,
        since: float | None = None,
        by: tuple[str, ...] = ("kind", "name"),
    ) -> list[PerfStat]:
        """How attempts went, grouped by the columns in ``by`` (any of ``GROUPABLE``), most attempts with a verdict
        first."""
        by = tuple(dict.fromkeys(by))
        unknown = [column for column in by if column not in GROUPABLE]
        if unknown:
            raise ValueError(f"can't group performance by {', '.join(unknown)} (only {', '.join(GROUPABLE)})")
        where, params = _where(kind=kind, name=name, category=category, since=since)
        rows = self.store.execute(
            "SELECT kind, name, category, model, provider, app, success, latency_s, retries, corrected, cost_usd, ts "
            f"FROM performance{where} ORDER BY ts",
            params,
        )
        groups: dict[tuple[Any, ...], _Group] = {}
        for row in rows:
            key = tuple(row[column] for column in by)
            groups.setdefault(key, _Group()).add(row)
        filters = {"kind": kind, "name": name, "category": category}
        found = []
        for key, acc in groups.items():
            values = dict(zip(by, key, strict=True))
            label = {**filters, **values}
            found.append(
                PerfStat(
                    kind=label["kind"] or "",
                    name=label["name"] or "",
                    category=label["category"] or "",
                    tally=Tally(acc.successes, acc.trials),
                    median_latency_s=statistics.median(acc.latencies) if acc.latencies else None,
                    retries=acc.retries,
                    corrected=acc.corrected,
                    cost_usd=acc.cost,
                    last_ts=acc.last_ts,
                    records=acc.records,
                    group=MappingProxyType(values),
                )
            )
        found.sort(key=lambda s: (-s.tally.trials, -s.records, -s.last_ts, s.name))
        return found

    def repeated_failures(self, *, since: float, min_count: int = 3, kind: str | None = None) -> list[FailureGroup]:
        """The same thing failing the same way (and in the same app) at least ``min_count`` times since ``since``,
        the most recent first."""
        clauses, params = ["success=0", "ts>=?"], [since]
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        rows = self.store.execute(
            "SELECT name, COALESCE(failure, '') AS failure, COALESCE(app, '') AS app, COUNT(*) AS n, MAX(ts) AS last "
            f"FROM performance WHERE {' AND '.join(clauses)} "
            "GROUP BY name, COALESCE(failure, ''), COALESCE(app, '') HAVING COUNT(*) >= ? "
            "ORDER BY last DESC, n DESC, name",
            (*params, max(1, int(min_count))),
        )
        return [
            FailureGroup(
                name=r["name"],
                failure=r["failure"] or UNKNOWN_FAILURE,
                app=r["app"] or None,
                count=int(r["n"]),
                last_ts=float(r["last"]),
            )
            for r in rows
        ]

    def estimate(
        self, category: str, names: Iterable[str], *, kind: str | None = None, since: float | None = None
    ) -> list[tuple[str, Tally]]:
        """For each candidate, how its attempts at ``category`` went, the best first by ``Tally.estimate()``: an
        untried candidate counts as a toss-up, not as the best or the worst. Ties keep the order given."""
        names = list(dict.fromkeys(names))
        if not names:
            return []
        where, params = _where(kind=kind, category=category, since=since)
        marks = ",".join("?" * len(names))
        rows = self.store.execute(
            "SELECT name, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS wins, COUNT(success) AS trials "
            f"FROM performance{where} AND name IN ({marks}) GROUP BY name",
            (*params, *names),
        )
        tallies = {r["name"]: Tally(int(r["wins"] or 0), int(r["trials"] or 0)) for r in rows}
        ranked = [(n, tallies.get(n, Tally())) for n in names]
        ranked.sort(key=lambda pair: -pair[1].estimate())
        return ranked

    def delete_all(self) -> int:
        return self.store.changed("DELETE FROM performance")


class _Group:
    """Running totals for one ``stats`` group."""

    def __init__(self) -> None:
        self.successes = self.trials = self.records = self.retries = self.corrected = 0
        self.latencies: list[float] = []
        self.cost: float | None = None
        self.last_ts = 0.0

    def add(self, row: Any) -> None:
        self.records += 1
        if row["success"] is not None:
            self.trials += 1
            self.successes += int(bool(row["success"]))
        if row["latency_s"] is not None:
            self.latencies.append(float(row["latency_s"]))
        self.retries += int(row["retries"] or 0)
        self.corrected += int(bool(row["corrected"]))
        if row["cost_usd"] is not None:
            self.cost = (self.cost or 0.0) + float(row["cost_usd"])
        self.last_ts = max(self.last_ts, float(row["ts"]))


def _where(
    *, kind: str | None = None, name: str | None = None, category: str | None = None, since: float | None = None
) -> tuple[str, list[Any]]:
    clauses, params = ["1=1"], []
    for column, op, value in (
        ("kind", "=", kind),
        ("name", "=", name),
        ("category", "=", category),
        ("ts", ">=", since),
    ):
        if value is not None:
            clauses.append(f"{column}{op}?")
            params.append(value)
    return " WHERE " + " AND ".join(clauses), params
