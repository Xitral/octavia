"""The routines she learned, and what became of offering them: ``routines.database`` (default
``data/routines/routines.db``). Only what ``mine`` derived (app and tool names, a usual time, counts) and the user's
answers; the records it was learned from stay where they were.

One connection behind a lock, shared by the event loop's threads and the Futures worker (which asks for offers).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from octavia.routines.mine import Routine


class StoreError(RuntimeError):
    """The database can't be used (written by a newer version, or not a database)."""


MIGRATIONS: tuple[str, ...] = (
    # 1: the first schema
    """
    CREATE TABLE routines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,  -- the number the user forgets one by: never reused
        key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,          -- learned | watching | dismissed | retired
        app TEXT NOT NULL DEFAULT '',
        process TEXT NOT NULL DEFAULT '',
        after_app TEXT NOT NULL DEFAULT '',
        after_process TEXT NOT NULL DEFAULT '',
        tool TEXT NOT NULL DEFAULT '',
        day_type TEXT NOT NULL DEFAULT 'any',
        minute INTEGER NOT NULL DEFAULT 0,
        days INTEGER NOT NULL DEFAULT 0,
        out_of INTEGER NOT NULL DEFAULT 0,
        support REAL NOT NULL DEFAULT 0,
        first_learned REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_offered REAL,
        offer_pending INTEGER NOT NULL DEFAULT 0,
        offers INTEGER NOT NULL DEFAULT 0,
        taken INTEGER NOT NULL DEFAULT 0,
        misses INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
)

_ROUTINE_FIELDS = tuple(f.name for f in fields(Routine))
KEPT = ("dismissed", "retired")  # the user's (or the record's) verdict: re-mining never changes these back
_IN_KEPT = f"IN ({', '.join(repr(k) for k in KEPT)})"


class RoutineStore:
    def __init__(self, path: str | Path, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            if str(self.path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA secure_delete = ON")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > len(MIGRATIONS):
                    raise StoreError(f"written by a newer version (schema {version}, this one knows {len(MIGRATIONS)})")
                for number in range(version, len(MIGRATIONS)):
                    conn.executescript(f"BEGIN;\n{MIGRATIONS[number]}\nPRAGMA user_version = {number + 1};\nCOMMIT;")
            except sqlite3.DatabaseError as exc:
                conn.close()
                raise StoreError(f"{self.path} can't be used for routines: {exc}") from exc
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        assert self._conn is not None
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ------------------------------------------------------------------ meta
    def meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # ------------------------------------------------------------------ routines
    def replace_mined(self, mined: list[Routine]) -> tuple[int, int]:
        """What a mining found replaces what the last one found. A routine the user turned down, or that was never
        taken up (``KEPT``), keeps that verdict whatever the records say now; one the records no longer show goes.
        Returns (learned, watching) after it."""
        now = self.clock()
        with self.transaction() as conn:
            kept = {r["key"] for r in conn.execute(f"SELECT key FROM routines WHERE status {_IN_KEPT}")}
            found = {r.key for r in mined}
            for row in conn.execute(f"SELECT key FROM routines WHERE status NOT {_IN_KEPT}").fetchall():
                if row["key"] not in found:
                    conn.execute("DELETE FROM routines WHERE key=?", (row["key"],))
            for routine in mined:
                values = asdict(routine)
                if routine.key in kept:
                    values.pop("status")
                    sets = ", ".join(f"{c}=?" for c in values if c != "key")
                    conn.execute(
                        f"UPDATE routines SET {sets}, updated_at=? WHERE key=?",
                        (*(v for c, v in values.items() if c != "key"), now, routine.key),
                    )
                    continue
                cols = list(values)
                conn.execute(
                    f"INSERT INTO routines({', '.join(cols)}, first_learned, updated_at) "
                    f"VALUES({', '.join('?' * len(cols))}, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "key")
                    + ", updated_at=excluded.updated_at",
                    (*values.values(), now, now),
                )
            counts = dict(
                conn.execute("SELECT status, COUNT(*) FROM routines GROUP BY status").fetchall()  # type: ignore[arg-type]
            )
        return int(counts.get("learned", 0)), int(counts.get("watching", 0))

    def all(self, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM routines"
        params: tuple[Any, ...] = ()
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params = statuses
        sql += " ORDER BY CASE status WHEN 'learned' THEN 0 WHEN 'watching' THEN 1 ELSE 2 END, support DESC, key"
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def get(self, key: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM routines WHERE key=?", (key,)).fetchone()

    def by_id(self, number: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM routines WHERE id=?", (number,)).fetchone()

    @staticmethod
    def as_routine(row: sqlite3.Row) -> Routine:
        return Routine(**{name: row[name] for name in _ROUTINE_FIELDS})

    def offered(self, key: str, at: float) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE routines SET last_offered=?, offer_pending=1, offers=offers+1 WHERE key=?", (at, key)
            )

    def offers_since(self, since: float) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) FROM routines WHERE last_offered>=?", (since,)).fetchone()
        return int(row[0])

    def pending_outcomes(self, before: float) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM routines WHERE offer_pending=1 AND last_offered<=?", (before,)
            ).fetchall()

    def outcome(self, key: str, taken: bool, max_misses: int) -> str | None:
        """How an offer went (it was used within ``rules.OUTCOME_S``, or not). ``max_misses`` in a row not taken
        retire it. Returns the new status when it changed."""
        with self.transaction() as conn:
            row = conn.execute("SELECT status, misses FROM routines WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            if taken:
                conn.execute("UPDATE routines SET offer_pending=0, taken=taken+1, misses=0 WHERE key=?", (key,))
                return None
            misses = int(row["misses"]) + 1
            retire = misses >= max_misses and row["status"] == "learned"
            conn.execute(
                "UPDATE routines SET offer_pending=0, misses=?, status=?, reason=? WHERE key=?",
                (
                    misses,
                    "retired" if retire else row["status"],
                    f"not taken up {misses} times in a row" if retire else "",
                    key,
                ),
            )
            return "retired" if retire else None

    def turned_down_targets(self) -> set[str]:
        """The apps (processes) and lookups (tools) of every routine the user said no to: nothing else offers them."""
        with self._lock:
            rows = self.conn.execute("SELECT process, tool FROM routines WHERE status='dismissed'").fetchall()
        return {r["tool"] or r["process"] for r in rows if r["tool"] or r["process"]}

    def dismiss(self, key: str, reason: str) -> bool:
        """Never offered again (kept, so re-mining doesn't bring it back)."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE routines SET status='dismissed', offer_pending=0, reason=? WHERE key=?", (reason, key)
            )
        return cur.rowcount > 0

    def forget_all(self) -> int:
        with self._lock:
            count = self.conn.execute("SELECT COUNT(*) FROM routines").fetchone()[0]
            self.conn.execute("DELETE FROM routines")
            self.conn.execute("DELETE FROM meta WHERE key<>'offers'")
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return int(count)
