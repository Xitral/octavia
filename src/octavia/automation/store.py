"""The automation database: the rolling event log, the rules, every run, and the kill switch.

One SQLite file (``data/automation/automation.db``), one connection behind a lock: the engine calls it from worker
threads (``asyncio.to_thread``) so the event loop never waits on the disk. Events and runs older than
``RETENTION_DAYS`` are deleted, and the log never holds more than ``MAX_EVENTS``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from octavia.automation.events import BusEvent
from octavia.automation.rules import Action, Condition, Rule, Window

DB_FILE = "data/automation/automation.db"
RETENTION_DAYS = 14
MAX_EVENTS = 20_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    id TEXT PRIMARY KEY, type TEXT NOT NULL, source TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL,
    provenance TEXT NOT NULL, sensitivity TEXT NOT NULL, chain TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, event TEXT NOT NULL, spec TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0, max_per_hour INTEGER NOT NULL, created_at REAL NOT NULL,
    authorized_at REAL, fire_count INTEGER NOT NULL DEFAULT 0, last_fired REAL);
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER NOT NULL, event_id TEXT NOT NULL, event_type TEXT NOT NULL,
    ts REAL NOT NULL, depth INTEGER NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_rule ON runs(rule_id, ts);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class AutomationStore:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------------------------------------- events
    def append(self, event: BusEvent) -> None:
        """Log ``event`` (never its transient fields)."""
        self._write(
            "INSERT OR IGNORE INTO events(id, type, source, ts, payload, provenance, sensitivity, chain) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                event.id,
                event.type,
                event.source,
                event.ts,
                json.dumps(event.payload, default=str),
                event.provenance,
                event.sensitivity,
                json.dumps(list(event.chain)),
            ),
        )

    def recent(self, *, since: float = 0.0, limit: int = 500) -> list[BusEvent]:
        """Logged events since ``since``, oldest first (the newest ``limit``)."""
        rows = self._read("SELECT * FROM events WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, limit))
        return [_to_event(r) for r in reversed(rows)]

    def prune(self, now: float | None = None) -> int:
        """Delete events and runs past retention, and the oldest events past ``MAX_EVENTS``."""
        cutoff = (self.clock() if now is None else now) - RETENTION_DAYS * 86400
        with self._lock, self._conn:
            gone = self._conn.execute("DELETE FROM events WHERE ts<?", (cutoff,)).rowcount
            gone += self._conn.execute(
                "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                (MAX_EVENTS,),
            ).rowcount
            self._conn.execute("DELETE FROM runs WHERE ts<?", (cutoff,))
        return gone

    # -------------------------------------------------------------------------------------------------- rules
    def add_rule(self, rule: Rule) -> Rule:
        cur = self._write(
            "INSERT INTO rules(name, event, spec, enabled, max_per_hour, created_at) VALUES (?,?,?,?,?,?)",
            (rule.name, rule.event, _spec(rule), int(rule.enabled), rule.max_per_hour, self.clock()),
        )
        added = self.rule(int(cur.lastrowid or 0))
        assert added is not None
        return added

    def rule(self, rule_id: int) -> Rule | None:
        rows = self._read("SELECT * FROM rules WHERE id=?", (rule_id,))
        return _to_rule(rows[0]) if rows else None

    def rules(self) -> list[Rule]:
        return [_to_rule(r) for r in self._read("SELECT * FROM rules ORDER BY id")]

    def set_enabled(self, rule_id: int, enabled: bool) -> Rule | None:
        authorized = self.clock() if enabled else None
        self._write(
            "UPDATE rules SET enabled=?, authorized_at=COALESCE(?, authorized_at) WHERE id=?",
            (int(enabled), authorized, rule_id),
        )
        return self.rule(rule_id)

    def delete_rule(self, rule_id: int) -> bool:
        return self._write("DELETE FROM rules WHERE id=?", (rule_id,)).rowcount > 0

    def fired(self, rule_id: int, at: float) -> None:
        self._write("UPDATE rules SET fire_count=fire_count+1, last_fired=? WHERE id=?", (at, rule_id))

    # --------------------------------------------------------------------------------------------------- runs
    def log_run(self, rule_id: int, event: BusEvent, status: str, detail: str) -> None:
        self._write(
            "INSERT INTO runs(rule_id, event_id, event_type, ts, depth, status, detail) VALUES (?,?,?,?,?,?,?)",
            (rule_id, event.id, event.type, self.clock(), event.depth, status, detail[:1000]),
        )

    def start_run(self, rule_id: int, event: BusEvent) -> int:
        """Log a run as "running" the moment the rule fires, so the log keeps firing order (a refusal decided while
        it runs comes after it) and a run cut short by a crash still shows. ``finish_run`` fills in the outcome."""
        cur = self._write(
            "INSERT INTO runs(rule_id, event_id, event_type, ts, depth, status, detail) VALUES (?,?,?,?,?,?,?)",
            (rule_id, event.id, event.type, self.clock(), event.depth, "running", ""),
        )
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, detail: str) -> None:
        self._write("UPDATE runs SET status=?, detail=? WHERE id=?", (status, detail[:1000], run_id))

    def runs(self, rule_id: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
        where, params = ("WHERE rule_id=?", (rule_id,)) if rule_id is not None else ("", ())
        rows = self._read(f"SELECT * FROM runs {where} ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit))
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------------------------------------- kv
    def get_flag(self, key: str) -> bool:
        rows = self._read("SELECT value FROM kv WHERE key=?", (key,))
        return bool(rows) and rows[0]["value"] == "1"

    def set_flag(self, key: str, value: bool) -> None:
        self._write("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, "1" if value else "0"))


def _spec(rule: Rule) -> str:
    return json.dumps(
        {
            "conditions": [c.model_dump() for c in rule.conditions],
            "window": rule.window.model_dump(),
            "max_age_s": rule.max_age_s,
            "actions": [a.model_dump() for a in rule.actions],
        }
    )


def _to_rule(row: sqlite3.Row) -> Rule:
    spec = json.loads(row["spec"])
    return Rule(
        id=row["id"],
        name=row["name"],
        event=row["event"],
        conditions=[Condition.model_validate(c) for c in spec.get("conditions", [])],
        window=Window.model_validate(spec.get("window") or {}),
        max_age_s=spec.get("max_age_s"),
        actions=[Action.model_validate(a) for a in spec.get("actions", [])],
        enabled=bool(row["enabled"]),
        max_per_hour=row["max_per_hour"],
        created_at=row["created_at"],
        authorized_at=row["authorized_at"],
        fire_count=row["fire_count"],
        last_fired=row["last_fired"],
    )


def _to_event(row: sqlite3.Row) -> BusEvent:
    return BusEvent(
        type=row["type"],
        source=row["source"],
        payload=json.loads(row["payload"]),
        ts=row["ts"],
        provenance=row["provenance"],
        sensitivity=row["sensitivity"],
        chain=tuple(json.loads(row["chain"])),
        id=row["id"],
    )
