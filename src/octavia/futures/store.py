"""Octavia Futures' database: one SQLite file (``futures.database``, default ``data/futures/futures.db``).

It is a file of its own, like the self-improvement journal, so the user can inspect, export or wipe what she has
learned about them without touching conversations or long-term memories.

Unlike the other stores it has real migrations: ``MIGRATIONS`` is an ordered list of scripts and ``PRAGMA
user_version`` says how many have run, so a column added later reaches existing databases too. A database written
by a newer version is refused rather than guessed at.

Threading model: one connection behind a re-entrant lock. The domain code (``world``, ``hypotheses``...) is plain
synchronous code on top of ``execute``/``transaction``; the service runs each step of its pipeline in a worker thread
through ``run``, so the event loop (and her voice) never waits on the disk.

Every change to a belief (a hypothesis formed, a prediction resolved, a recommendation made) is also written to the
``changes`` table and logged as one JSON line on the ``octavia.futures.trace`` logger: what changed, which event
caused it, which provider reasoned it. That is how "why did she believe that?" is answered from data, not from
prose logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
trace_log = logging.getLogger("octavia.futures.trace")


class StoreError(RuntimeError):
    """The database can't be used (written by a newer version, or not a database)."""


MIGRATIONS: tuple[str, ...] = (
    # 1: the first schema
    """
    CREATE TABLE events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        ts REAL NOT NULL,
        source TEXT NOT NULL,
        dedup_key TEXT UNIQUE,
        subject TEXT,
        entities TEXT NOT NULL DEFAULT '[]',
        goal TEXT,
        action TEXT,
        outcome TEXT,
        confidence REAL,
        duration_s REAL,
        feedback TEXT,
        context TEXT NOT NULL DEFAULT '{}',
        data TEXT NOT NULL DEFAULT '{}',
        domain TEXT NOT NULL,
        sensitivity TEXT NOT NULL,
        caused_by TEXT
    );
    CREATE INDEX events_by_type ON events(type, ts);
    CREATE INDEX events_by_subject ON events(subject, ts);
    CREATE INDEX events_by_ts ON events(ts);

    CREATE TABLE entities (
        ref TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT,
        attrs TEXT NOT NULL DEFAULT '{}',
        domain TEXT NOT NULL,
        sensitivity TEXT NOT NULL,
        source TEXT NOT NULL,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        last_event TEXT
    );
    CREATE INDEX entities_by_kind ON entities(kind, status);

    CREATE TABLE entity_changes (
        id INTEGER PRIMARY KEY,
        ref TEXT NOT NULL,
        field TEXT NOT NULL,
        old TEXT,
        new TEXT,
        ts REAL NOT NULL,
        event_id TEXT
    );
    CREATE INDEX entity_changes_by_ref ON entity_changes(ref, ts);
    CREATE INDEX entity_changes_by_ts ON entity_changes(ts);

    CREATE TABLE relations (
        id INTEGER PRIMARY KEY,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        object TEXT NOT NULL,
        attrs TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        event_id TEXT,
        UNIQUE (subject, predicate, object)
    );
    CREATE INDEX relations_by_object ON relations(object, predicate);

    CREATE TABLE episodes (
        id INTEGER PRIMARY KEY,
        thread TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        last_at REAL NOT NULL,
        goal TEXT,
        event_ids TEXT NOT NULL DEFAULT '[]',
        problems TEXT NOT NULL DEFAULT '[]',
        resolutions TEXT NOT NULL DEFAULT '[]',
        outcome TEXT,
        learned TEXT NOT NULL DEFAULT '[]',
        signature TEXT NOT NULL DEFAULT '',
        summary_by TEXT NOT NULL DEFAULT 'deterministic',
        domain TEXT NOT NULL,
        sensitivity TEXT NOT NULL
    );
    CREATE INDEX episodes_by_thread ON episodes(thread, status);
    CREATE INDEX episodes_by_time ON episodes(started_at);
    CREATE VIRTUAL TABLE episodes_fts USING fts5(title, signature, content='episodes', content_rowid='id');
    CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
        INSERT INTO episodes_fts(rowid, title, signature) VALUES (new.id, new.title, new.signature);
    END;
    CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, title, signature)
            VALUES ('delete', old.id, old.title, old.signature);
    END;
    CREATE TRIGGER episodes_au AFTER UPDATE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, title, signature)
            VALUES ('delete', old.id, old.title, old.signature);
        INSERT INTO episodes_fts(rowid, title, signature) VALUES (new.id, new.title, new.signature);
    END;

    CREATE TABLE outcomes (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        subject TEXT NOT NULL,
        key TEXT UNIQUE,
        expected TEXT,
        actual TEXT,
        success INTEGER,
        value REAL,
        started_at REAL,
        resolved_at REAL NOT NULL,
        time_to_outcome_s REAL,
        rating INTEGER,
        feedback TEXT,
        cost_usd REAL,
        model TEXT,
        tools TEXT NOT NULL DEFAULT '[]',
        retries INTEGER NOT NULL DEFAULT 0,
        features TEXT NOT NULL DEFAULT '{}',
        event_id TEXT,
        domain TEXT NOT NULL
    );
    CREATE INDEX outcomes_by_kind ON outcomes(kind, resolved_at);

    CREATE TABLE hypotheses (
        id INTEGER PRIMARY KEY,
        key TEXT UNIQUE NOT NULL,
        statement TEXT NOT NULL,
        domain TEXT NOT NULL,
        metric TEXT NOT NULL,
        feature TEXT,
        value TEXT,
        claim TEXT NOT NULL,
        status TEXT NOT NULL,
        strength TEXT NOT NULL,
        support INTEGER NOT NULL DEFAULT 0,
        trials INTEGER NOT NULL DEFAULT 0,
        other_support INTEGER NOT NULL DEFAULT 0,
        other_trials INTEGER NOT NULL DEFAULT 0,
        effect REAL,
        confounders TEXT NOT NULL DEFAULT '[]',
        origin TEXT NOT NULL,
        proposed_experiment TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_evidence_at REAL
    );
    CREATE INDEX hypotheses_by_metric ON hypotheses(metric, status);

    CREATE TABLE hypothesis_evidence (
        id INTEGER PRIMARY KEY,
        hypothesis_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        direction INTEGER NOT NULL,
        outcome_id INTEGER,
        experiment_id INTEGER,
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX hypothesis_evidence_by_hypothesis ON hypothesis_evidence(hypothesis_id, ts);

    CREATE TABLE predictions (
        id INTEGER PRIMARY KEY,
        key TEXT UNIQUE NOT NULL,
        kind TEXT NOT NULL,
        subject TEXT,
        statement TEXT NOT NULL,
        probability REAL NOT NULL,
        basis TEXT NOT NULL,
        support INTEGER NOT NULL DEFAULT 0,
        trials INTEGER NOT NULL DEFAULT 0,
        features TEXT NOT NULL DEFAULT '{}',
        hypothesis_id INTEGER,
        provider TEXT NOT NULL,
        created_at REAL NOT NULL,
        resolve_by REAL,
        status TEXT NOT NULL,
        occurred INTEGER,
        correct INTEGER,
        resolved_at REAL,
        resolution_event TEXT,
        event_id TEXT
    );
    CREATE INDEX predictions_by_status ON predictions(status, kind);

    CREATE TABLE experiments (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL,
        title TEXT NOT NULL,
        hypothesis_id INTEGER,
        domain TEXT NOT NULL,
        kind TEXT NOT NULL,
        octavia_acts INTEGER NOT NULL,
        variants TEXT NOT NULL,
        metric TEXT NOT NULL,
        min_per_variant INTEGER NOT NULL,
        constraints TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        created_at REAL NOT NULL,
        approved_at REAL,
        started_at REAL,
        ends_at REAL,
        ended_at REAL,
        result TEXT,
        confidence TEXT,
        notes TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX experiments_by_status ON experiments(status, metric);

    CREATE TABLE trials (
        id INTEGER PRIMARY KEY,
        experiment_id INTEGER NOT NULL,
        unit TEXT NOT NULL,
        variant TEXT NOT NULL,
        assigned_at REAL NOT NULL,
        outcome_id INTEGER,
        success INTEGER,
        UNIQUE (experiment_id, unit)
    );

    CREATE TABLE performance (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        model TEXT,
        provider TEXT,
        success INTEGER,
        failure TEXT,
        latency_s REAL,
        retries INTEGER NOT NULL DEFAULT 0,
        corrected INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL,
        tokens_in INTEGER,
        tokens_out INTEGER,
        confidence REAL,
        tool_calls INTEGER,
        turn TEXT,
        app TEXT,
        event_id TEXT
    );
    CREATE INDEX performance_by_name ON performance(kind, name, ts);
    CREATE INDEX performance_by_turn ON performance(turn);

    CREATE TABLE triggers (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        condition TEXT NOT NULL,
        description TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT NOT NULL,
        once INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        expires_at REAL,
        fired_at REAL,
        fired_event TEXT,
        fire_count INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'user'
    );
    CREATE INDEX triggers_by_status ON triggers(status);

    CREATE TABLE recommendations (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        policy TEXT NOT NULL,
        urgency REAL NOT NULL,
        relevance REAL NOT NULL,
        domain TEXT NOT NULL,
        sensitivity TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        trace TEXT NOT NULL DEFAULT '{}',
        note TEXT,
        subject TEXT,
        created_at REAL NOT NULL,
        expires_at REAL,
        delivered_at REAL,
        response TEXT,
        response_at REAL,
        experiment_id INTEGER,
        variant TEXT
    );
    CREATE INDEX recommendations_by_key ON recommendations(key, created_at);
    CREATE INDEX recommendations_by_status ON recommendations(status, created_at);

    CREATE TABLE objectives (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        domain TEXT NOT NULL,
        weight REAL NOT NULL DEFAULT 1.0,
        evidence TEXT NOT NULL DEFAULT '',
        hypothesis_id INTEGER,
        entity TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE controls (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE kv (
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (namespace, key)
    );

    CREATE TABLE changes (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        object TEXT NOT NULL,
        change TEXT NOT NULL,
        cause TEXT,
        provider TEXT NOT NULL DEFAULT 'deterministic',
        detail TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX changes_by_object ON changes(object, ts);
    CREATE INDEX changes_by_ts ON changes(ts);
    """,
    # 2: open episodes are looked up on every conversation event and every minute
    """
    CREATE INDEX episodes_by_status ON episodes(status, kind, started_at);
    CREATE INDEX recommendations_by_subject ON recommendations(subject, kind);
    CREATE INDEX predictions_by_subject ON predictions(subject, status);
    """,
    # 3: habits she kept from her experiments on herself (``habits``); their history is in the audit trail
    """
    CREATE TABLE habits (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL,
        variant TEXT NOT NULL,
        status TEXT NOT NULL,
        experiment_id INTEGER,
        evidence TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL,
        created_at REAL NOT NULL,
        changed_at REAL NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX habits_by_key ON habits(key, status);
    """,
    # 4: what she did in a situation and how it ended (``lessons``), found again by the situation's words
    """
    CREATE TABLE lessons (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        situation TEXT NOT NULL,
        actions TEXT NOT NULL DEFAULT '[]',
        outcome TEXT NOT NULL,
        success INTEGER NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        claim TEXT NOT NULL DEFAULT '',
        domain TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE INDEX lessons_by_time ON lessons(ts);
    CREATE VIRTUAL TABLE lessons_fts USING fts5(situation, content='lessons', content_rowid='id', tokenize='porter');
    CREATE TRIGGER lessons_ai AFTER INSERT ON lessons BEGIN
        INSERT INTO lessons_fts(rowid, situation) VALUES (new.id, new.situation);
    END;
    CREATE TRIGGER lessons_ad AFTER DELETE ON lessons BEGIN
        INSERT INTO lessons_fts(lessons_fts, rowid, situation) VALUES ('delete', old.id, old.situation);
    END;
    """,
    # 5: written approaches to what keeps going wrong, and how the turns with and without each went (``strategies``)
    """
    CREATE TABLE strategies (
        id INTEGER PRIMARY KEY,
        situation TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL,
        origin TEXT NOT NULL,
        domain TEXT NOT NULL,
        created_at REAL NOT NULL,
        changed_at REAL NOT NULL,
        why TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE strategy_trials (
        id INTEGER PRIMARY KEY,
        strategy INTEGER NOT NULL,
        shown INTEGER NOT NULL,
        turn TEXT NOT NULL UNIQUE,
        ts REAL NOT NULL,
        success INTEGER
    );
    CREATE INDEX strategy_trials_by_strategy ON strategy_trials(strategy, shown);
    """,
)

SCHEMA_VERSION = len(MIGRATIONS)


def dumps(value: Any) -> str:
    """Compact JSON for a TEXT column (sets and tuples as lists, anything else as its string)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_jsonable)


def loads(raw: str | None, default: Any = None) -> Any:
    """A JSON column's value, or ``default`` for an empty or broken one."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _jsonable(value: Any) -> Any:
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return str(value)


class FuturesStore:
    """The Futures database. ``path`` may be ``":memory:"`` (tests). ``clock`` is epoch seconds, like
    ``time.time``: every timestamp the domain code writes comes from it, so tests can move time."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._depth = 0  # transaction nesting: only the outermost one begins and commits
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")  # `octavia futures` beside the running assistant
                self._migrate()
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"the Futures database can't be opened: {exc}") from None

    # ------------------------------------------------------------------ schema
    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        version = self.schema_version
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"the Futures database is version {version}, newer than this code ({SCHEMA_VERSION}); "
                "it was written by a newer Octavia"
            )
        for number in range(version, SCHEMA_VERSION):
            with self.transaction() as conn:
                for statement in _statements(MIGRATIONS[number]):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {number + 1}")
            log.info("Futures database migrated to version %d", number + 1)

    # ------------------------------------------------------------------ access
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic read-modify-write. Nested use joins the outer transaction."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def insert(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> int:
        """Run an INSERT; returns the new row id."""
        with self._lock:
            return int(self._conn.execute(sql, tuple(params)).lastrowid or 0)

    def changed(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> int:
        """Run an UPDATE or DELETE; returns how many rows it changed."""
        with self._lock:
            return self._conn.execute(sql, tuple(params)).rowcount

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """``fn(*args, **kwargs)`` in a worker thread: how async code calls the synchronous domain code."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ------------------------------------------------------------------ small state
    def kv_get(self, namespace: str, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key))
        return loads(row["value"], default) if row else default

    def kv_set(self, namespace: str, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(namespace, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (namespace, key, dumps(value), self.clock()),
        )

    def kv_delete(self, namespace: str, key: str) -> None:
        self.execute("DELETE FROM kv WHERE namespace=? AND key=?", (namespace, key))

    # ------------------------------------------------------------------ the audit trail
    def record_change(
        self,
        obj: str,
        change: str,
        *,
        cause: str | None = None,
        provider: str = "deterministic",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Note that a belief changed: ``obj`` is what ("hypothesis:3"), ``change`` how ("created", "strengthened",
        "resolved"), ``cause`` the event id (or other object) that caused it, ``provider`` what reasoned it."""
        ts = self.clock()
        detail = detail or {}
        self.execute(
            "INSERT INTO changes(ts, object, change, cause, provider, detail) VALUES (?,?,?,?,?,?)",
            (ts, obj, change, cause, provider, dumps(detail)),
        )
        trace_log.info(
            "%s",
            dumps({"ts": round(ts, 3), "object": obj, "change": change, "cause": cause, "provider": provider}),
        )

    def changes(self, obj: str | None = None, *, since: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """The audit trail, newest first: of one object, or of everything since a time."""
        where, params = [], []
        if obj:
            where.append("object=?")
            params.append(obj)
        if since is not None:
            where.append("ts>=?")
            params.append(since)
        sql = "SELECT * FROM changes" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit))
        return [
            {
                "ts": r["ts"],
                "object": r["object"],
                "change": r["change"],
                "cause": r["cause"],
                "provider": r["provider"],
                "detail": loads(r["detail"], {}),
            }
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _statements(script: str) -> list[str]:
    """Split a migration script into statements, keeping a trigger's body (which has its own ';') in one piece."""
    statements, current = [], []
    for line in script.splitlines():
        if not line.strip():
            continue
        current.append(line)
        text = "\n".join(current)
        if text.rstrip().endswith(";") and sqlite3.complete_statement(text):
            statements.append(text)
            current = []
    if current and "\n".join(current).strip():
        statements.append("\n".join(current))
    return statements
