"""The normalized event stream: what happened, as one kind of record, whatever produced it.

A ``FuturesEvent`` is made by ordinary program logic (``ingest`` turns the assistant's own bus events into these;
``sources`` watches TickTick, Canvas and the app in front), never by asking a model what happened. The ``EventLog``
keeps them:

* A happening seen twice is one event: a producer that can see the same thing again (a poll, a restart that replays
  the journal) gives it a ``dedup_key``, and the second ``append`` returns None.
* ``transient`` fields are seen by whatever handles the event right now (a "remind me when" condition may look at an
  email's subject) and never written down.
* The type must be registered (``schema.EVENT_TYPES``); its domain and sensitivity are the defaults.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from octavia.futures.schema import event_type
from octavia.futures.store import FuturesStore, dumps, loads

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class FuturesEvent:
    """One thing that happened.

    ``subject`` is the main entity it's about (``task:65a1...``), ``entities`` any others. ``outcome`` is a short
    word when it has one ("success", "failure", "declined"). ``confidence`` is how sure the producer is that it
    happened as described (1.0 for something seen directly; less for an inference such as "completed, judging by
    it leaving the due list"). ``context`` holds the conditions it happened in (hour, weekday, the app in front),
    which is what patterns are looked for in; ``data`` the details.
    """

    type: str
    source: str
    ts: float = 0.0  # 0: now, by the store's clock
    id: str = ""  # "": a new one is made
    dedup_key: str | None = None
    subject: str | None = None
    entities: tuple[str, ...] = ()
    goal: str | None = None
    action: str | None = None
    outcome: str | None = None
    confidence: float | None = None
    duration_s: float | None = None
    feedback: str | None = None
    context: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    data: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    domain: str | None = None  # None: the type's
    sensitivity: str | None = None  # None: the type's
    caused_by: str | None = None  # the event that led to this one
    transient: Mapping[str, Any] = field(default_factory=lambda: _EMPTY, compare=False)  # never stored

    def get(self, path: str, default: Any = None) -> Any:
        """A field by dotted path, for matching: ``"data.app"``, ``"context.hour"``, ``"transient.subject"``,
        ``"subject"``."""
        head, _, rest = path.partition(".")
        value: Any = getattr(self, head, None) if head in _FIELDS else None
        for part in rest.split(".") if rest else ():
            value = value.get(part) if isinstance(value, Mapping) else None
        return default if value is None else value


_FIELDS = frozenset(FuturesEvent.__dataclass_fields__)


def _to_event(row: sqlite3.Row) -> FuturesEvent:
    return FuturesEvent(
        id=row["id"],
        type=row["type"],
        ts=row["ts"],
        source=row["source"],
        dedup_key=row["dedup_key"],
        subject=row["subject"],
        entities=tuple(loads(row["entities"], [])),
        goal=row["goal"],
        action=row["action"],
        outcome=row["outcome"],
        confidence=row["confidence"],
        duration_s=row["duration_s"],
        feedback=row["feedback"],
        context=MappingProxyType(loads(row["context"], {})),
        data=MappingProxyType(loads(row["data"], {})),
        domain=row["domain"],
        sensitivity=row["sensitivity"],
        caused_by=row["caused_by"],
    )


class EventLog:
    """The events table."""

    def __init__(self, store: FuturesStore) -> None:
        self.store = store

    def prepare(self, event: FuturesEvent) -> FuturesEvent:
        """The event as it will be stored: its type checked, and its id, time, domain and sensitivity filled in.
        Raises ValueError for an unknown type."""
        kind = event_type(event.type)
        return replace(
            event,
            id=event.id or uuid.uuid4().hex,
            ts=event.ts or self.store.clock(),
            domain=event.domain or kind.domain.value,
            sensitivity=event.sensitivity or kind.sensitivity.value,
        )

    def append(self, event: FuturesEvent) -> FuturesEvent | None:
        """Store ``event`` (``prepare``d if it wasn't). Returns it as stored, transient fields still attached for
        the caller, or None when its ``dedup_key`` was seen before: the same happening, seen again."""
        event = self.prepare(event)
        try:
            self.store.execute(
                "INSERT INTO events(id, type, ts, source, dedup_key, subject, entities, goal, action, outcome, "
                "confidence, duration_s, feedback, context, data, domain, sensitivity, caused_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.id,
                    event.type,
                    event.ts,
                    event.source,
                    event.dedup_key,
                    event.subject,
                    dumps(list(event.entities)),
                    event.goal,
                    event.action,
                    event.outcome,
                    event.confidence,
                    event.duration_s,
                    event.feedback,
                    dumps(dict(event.context)),
                    dumps(dict(event.data)),
                    event.domain,
                    event.sensitivity,
                    event.caused_by,
                ),
            )
        except sqlite3.IntegrityError:
            if event.dedup_key and self.seen(event.dedup_key):
                return None
            raise
        return event

    def seen(self, dedup_key: str) -> bool:
        return self.store.one("SELECT 1 FROM events WHERE dedup_key=?", (dedup_key,)) is not None

    def get(self, event_id: str) -> FuturesEvent | None:
        row = self.store.one("SELECT * FROM events WHERE id=?", (event_id,))
        return _to_event(row) if row else None

    def many(self, ids: Iterable[str]) -> list[FuturesEvent]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.store.execute(f"SELECT * FROM events WHERE id IN ({marks}) ORDER BY ts", ids)
        return [_to_event(r) for r in rows]

    def recent(
        self,
        *,
        types: Iterable[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        subject: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> list[FuturesEvent]:
        """Events newest first, filtered."""
        where, params = self._where(types=types, since=since, until=until, subject=subject, source=source)
        rows = self.store.execute(f"SELECT * FROM events{where} ORDER BY ts DESC LIMIT ?", (*params, limit))
        return [_to_event(r) for r in rows]

    def count(self, **filters: Any) -> int:
        where, params = self._where(**filters)
        row = self.store.one(f"SELECT COUNT(*) AS n FROM events{where}", params)
        return int(row["n"]) if row else 0

    def delete(
        self,
        *,
        ids: Iterable[str] | None = None,
        types: Iterable[str] | None = None,
        source: str | None = None,
        subject: str | None = None,
        until: float | None = None,
        domain: str | None = None,
    ) -> int:
        """Delete events (the user asked her to forget them, or they're past retention). Returns how many."""
        where, params = self._where(types=types, until=until, subject=subject, source=source, domain=domain)
        if ids is not None:
            ids = list(ids)
            if not ids:
                return 0
            clause = f"id IN ({','.join('?' * len(ids))})"
            where = f"{where} AND {clause}" if where else f" WHERE {clause}"
            params = [*params, *ids]
        if not where:
            raise ValueError("refusing to delete every event without saying so (use reset)")
        return self.store.changed(f"DELETE FROM events{where}", params)

    @staticmethod
    def _where(
        *,
        types: Iterable[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        subject: str | None = None,
        source: str | None = None,
        domain: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if types is not None:
            types = list(types)
            clauses.append(f"type IN ({','.join('?' * len(types))})" if types else "0")
            params.extend(types)
        for column, op, value in (
            ("ts", ">=", since),
            ("ts", "<", until),
            ("subject", "=", subject),
            ("source", "=", source),
            ("domain", "=", domain),
        ):
            if value is not None:
                clauses.append(f"{column}{op}?")
                params.append(value)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params
