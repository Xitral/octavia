"""The improvement journal: what the assistant noticed, what was built, and what became of it.

It has its own SQLite file rather than living in the memory database, so the
self-improvement history can be inspected or reset without touching conversations
or what the assistant knows about the user. Same threading model as
``memory.sqlite_store``: one connection behind a lock, work run in a thread through
``asyncio.to_thread``. Each operation holds the lock (and a transaction) for its whole
read-modify-write, so the same error reported twice at once can't become two rows.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from octavia.evolve.models import OPEN_STATUSES, Improvement, Kind, Status

MAX_TITLE = 120
MAX_DETAILS = 4000
MAX_EVIDENCE = 4000
KEEP_EVIDENCE = 3  # recent examples are enough for the builder; old ones would only bloat its prompt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS improvements (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    evidence TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT UNIQUE,
    branch TEXT,
    base_commit TEXT,
    merge_commit TEXT,
    report TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY,
    improvement_id INTEGER NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    outcome TEXT,
    turns INTEGER,
    cost_usd REAL
);
"""

_UPDATABLE = frozenset({"status", "title", "details", "branch", "base_commit", "merge_commit", "report", "error"})
_OPEN = tuple(s.value for s in OPEN_STATUSES)
_CLOSED = frozenset(s.value for s in Status if s not in OPEN_STATUSES)
# Being built or built: what's built is what was asked then, so a repeat of the note changes nothing about it.
_TAKEN_UP = frozenset({Status.QUEUED.value, Status.BUILDING.value, Status.READY.value})
# Closed without being installed: what became of an earlier note with the same title (``closed_like``).
_NOT_INSTALLED = tuple(sorted(_CLOSED - {Status.INSTALLED.value}))
# The outcome of a build cut off by an intentional restart and queued again: its rebuild is what counts toward
# the daily limit, so it isn't counted twice.
REQUEUED = Status.QUEUED.value


class StatusChanged(Exception):
    """``update(..., expect=...)`` found another status: someone else (another process) got there first."""

    def __init__(self, current: Improvement) -> None:
        super().__init__(f"#{current.id} is {current.status.value} now")
        self.current = current


def normalize_title(title: str) -> str:
    """The form titles are compared in: "Can't open  Spotify." and "can't open spotify" are the same note."""
    text = " ".join(title.lower().split())
    while text and (text[-1].isspace() or unicodedata.category(text[-1]).startswith("P")):
        text = text[:-1]
    return text


# Words that don't tell two titles apart ("DataCamp Submit shouldn't need approval" and "DataCamp Submit still asks
# approval" are the same problem).
_TITLE_STOP = frozenset(
    "the and for not but with when what why how does doesn don can cant still should shouldn need needs keep keeps "
    "again won wont isn aren her she you your its it's this that from into after before too any all".split()
)
_TITLE_WORD = re.compile(r"[a-z0-9]+")


def title_words(title: str) -> frozenset[str]:
    """The words of a title that say what it's about (3+ letters, no filler)."""
    return frozenset(w for w in _TITLE_WORD.findall(title.lower()) if len(w) >= 3 and w not in _TITLE_STOP)


def same_problem(a: str, b: str) -> bool:
    """Whether two titles name the same problem in other words: 3+ telling words shared, and most of the shorter
    title's ("Local brain as entry point" and "Local brain input tools" share only two, so they're different)."""
    wa, wb = title_words(a), title_words(b)
    shared = wa & wb
    return len(shared) >= 3 and len(shared) >= 0.6 * min(len(wa), len(wb))


def _clean_title(title: str) -> str:
    title = " ".join(str(title).split())[:MAX_TITLE].strip()
    if not title:
        raise ValueError("an improvement needs a title")
    return title


def _clean_details(details: str | None) -> str:
    return (details or "").strip()[:MAX_DETAILS]


def _load_evidence(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _load_report(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _to_improvement(row: sqlite3.Row) -> Improvement:
    return Improvement(
        id=row["id"],
        kind=Kind(row["kind"]),
        title=row["title"],
        details=row["details"],
        source=row["source"],
        status=Status(row["status"]),
        occurrences=row["occurrences"],
        evidence=_load_evidence(row["evidence"]),
        fingerprint=row["fingerprint"],
        branch=row["branch"],
        base_commit=row["base_commit"],
        merge_commit=row["merge_commit"],
        report=_load_report(row["report"]),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Journal:
    """Improvement notes and build history. ``path`` may be ``":memory:"`` (tests)."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._closed = False
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @staticmethod
    def _fetch(conn: sqlite3.Connection, improvement_id: int) -> Improvement:
        row = conn.execute("SELECT * FROM improvements WHERE id=?", (improvement_id,)).fetchone()
        if row is None:
            raise KeyError(improvement_id)
        return _to_improvement(row)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ notes

    async def note(
        self,
        kind: Kind,
        title: str,
        details: str = "",
        source: str = "assistant",
        fingerprint: str | None = None,
        evidence: str | None = None,
    ) -> tuple[Improvement, bool]:
        """Record something to improve, or count another occurrence of it. Returns (item, created).

        With a ``fingerprint`` (automatically noticed errors) the fingerprint is the identity, and a closed item
        whose error comes back is reopened: whatever was done about it didn't fix it. An installed one stays
        installed (it is what "undo the last update" undoes): the error that came back becomes a new item, which
        takes the fingerprint over. Without a fingerprint, an open item of the same kind with the same title
        (ignoring case, spacing and trailing punctuation) is the same note.

        One that's queued, being built or built (ready) is returned as it is: its details, occurrences and
        evidence describe what's being built, so a repeat doesn't change them.
        """
        return await asyncio.to_thread(self._note, kind, title, details, source, fingerprint, evidence)

    def _note(
        self,
        kind: Kind | str,
        title: str,
        details: str,
        source: str,
        fingerprint: str | None,
        evidence: str | None,
    ) -> tuple[Improvement, bool]:
        kind = Kind(kind)
        title = _clean_title(title)
        details = _clean_details(details)
        fingerprint = (fingerprint or "").strip() or None
        new_evidence = [evidence[:MAX_EVIDENCE]] if evidence and evidence.strip() else []
        now = self._clock()
        with self._transaction() as conn:
            row = self._duplicate(conn, kind, title, fingerprint)
            if row is not None and fingerprint is not None and row["status"] == Status.INSTALLED.value:
                # The fix is in, but the error came back: that's a new item. The installed one only gives up the
                # fingerprint (a unique column); its status and updated_at stay, so "the last update" is unchanged.
                conn.execute(
                    "UPDATE improvements SET fingerprint=? WHERE id=?", (f"{fingerprint}:{row['id']}", row["id"])
                )
                came_back = f"It came back after #{row['id']} was installed."
                details = _clean_details(f"{came_back}\n\n{details}" if details else came_back)
                row = None
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO improvements(kind, title, details, source, status, occurrences, evidence, "
                    "fingerprint, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
                    (
                        kind.value,
                        title,
                        details,
                        source,
                        Status.NOTED.value,
                        json.dumps(new_evidence),
                        fingerprint,
                        now,
                        now,
                    ),
                )
                return self._fetch(conn, int(cursor.lastrowid or 0)), True
            status = row["status"]
            if status in _TAKEN_UP:
                return _to_improvement(row), False
            kept = (_load_evidence(row["evidence"]) + new_evidence)[-KEEP_EVIDENCE:]
            if fingerprint is not None and status in _CLOSED:
                status = Status.NOTED.value
            details = details if len(details) > len(row["details"]) else row["details"]
            conn.execute(
                "UPDATE improvements SET occurrences=occurrences+1, evidence=?, details=?, status=?, updated_at=? "
                "WHERE id=?",
                (json.dumps(kept), details, status, now, row["id"]),
            )
            return self._fetch(conn, row["id"]), False

    @staticmethod
    def _duplicate(conn: sqlite3.Connection, kind: Kind, title: str, fingerprint: str | None) -> sqlite3.Row | None:
        if fingerprint is not None:
            return conn.execute("SELECT * FROM improvements WHERE fingerprint=?", (fingerprint,)).fetchone()
        wanted = normalize_title(title)
        rows = conn.execute(
            f"SELECT * FROM improvements WHERE kind=? AND status IN ({','.join('?' * len(_OPEN))}) "
            "ORDER BY updated_at DESC, id DESC",
            (kind.value, *_OPEN),
        ).fetchall()
        return next((r for r in rows if normalize_title(r["title"]) == wanted), None)

    async def find_open(self, kind: Kind, title: str) -> Improvement | None:
        """The open item that ``note`` would count a note of this kind and title (no fingerprint) as. Read-only."""
        return await asyncio.to_thread(self._find_open, Kind(kind), title)

    def _find_open(self, kind: Kind, title: str) -> Improvement | None:
        with self._lock:
            row = self._duplicate(self._conn, kind, _clean_title(title), None)
        return _to_improvement(row) if row is not None else None

    async def installed_like(self, title: str, within_s: float, *, other_than: int | None = None) -> Improvement | None:
        """The newest item installed within ``within_s`` that names the same problem as ``title`` in any words
        (``same_problem``), of any kind: a complaint about it again means that fix didn't work."""
        since = self._clock() - within_s
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM improvements WHERE status=? AND updated_at>=? ORDER BY updated_at DESC, id DESC",
            (Status.INSTALLED.value, since),
        )
        return next(
            (_to_improvement(r) for r in rows if r["id"] != other_than and same_problem(r["title"], title)), None
        )

    async def closed_like(self, kind: Kind, title: str) -> Improvement | None:
        """The newest item of this kind with the same title (compared as ``note`` does) that was closed without
        being installed: failed, found nothing to change, dropped or refused, or undone."""
        wanted = normalize_title(_clean_title(title))
        rows = await asyncio.to_thread(
            self._query,
            f"SELECT * FROM improvements WHERE kind=? AND status IN ({','.join('?' * len(_NOT_INSTALLED))}) "
            "ORDER BY updated_at DESC, id DESC",
            (Kind(kind).value, *_NOT_INSTALLED),
        )
        return next((_to_improvement(r) for r in rows if normalize_title(r["title"]) == wanted), None)

    async def get(self, improvement_id: int) -> Improvement | None:
        rows = await asyncio.to_thread(self._query, "SELECT * FROM improvements WHERE id=?", (improvement_id,))
        return _to_improvement(rows[0]) if rows else None

    async def list(self, statuses: Iterable[Status] | None = None, limit: int = 50) -> list[Improvement]:
        """Most recently active first (``updated_at``), optionally only those in ``statuses``."""
        sql, params = "SELECT * FROM improvements", []
        if statuses is not None:
            if isinstance(statuses, str):  # a single Status (a str enum) would otherwise iterate its letters
                statuses = [statuses]
            values = [Status(s).value for s in statuses]
            if not values:
                return []
            sql += f" WHERE status IN ({','.join('?' * len(values))})"
            params.extend(values)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(0, int(limit)))
        rows = await asyncio.to_thread(self._query, sql, tuple(params))
        return [_to_improvement(r) for r in rows]

    async def update(self, improvement_id: int, *, expect: Status | None = None, **fields: Any) -> Improvement:
        """Change the given fields (and ``updated_at``). KeyError for an unknown id, ValueError for a bad field.

        ``expect``: only if the status is still that (checked and changed in one transaction, so it holds across
        processes); StatusChanged otherwise, and nothing changes."""
        return await asyncio.to_thread(self._update, improvement_id, fields, expect)

    def _update(self, improvement_id: int, fields: dict[str, Any], expect: Status | None = None) -> Improvement:
        unknown = sorted(set(fields) - _UPDATABLE)
        if unknown:
            raise ValueError(f"can't update {', '.join(unknown)} (allowed: {', '.join(sorted(_UPDATABLE))})")
        values: dict[str, Any] = {}
        for name, value in fields.items():
            if name == "status":
                value = Status(value).value
            elif name == "title":
                value = _clean_title(value)
            elif name == "details":
                value = _clean_details(value)
            elif name == "report":
                value = None if value is None else json.dumps(value)
            elif value is not None:
                value = str(value)
            values[name] = value
        with self._transaction() as conn:
            current = self._fetch(conn, improvement_id)
            if expect is not None and current.status is not Status(expect):
                raise StatusChanged(current)
            assignments = "".join(f"{name}=?, " for name in values)  # names come from _UPDATABLE, never the caller
            conn.execute(
                f"UPDATE improvements SET {assignments}updated_at=? WHERE id=?",
                (*values.values(), self._clock(), improvement_id),
            )
            return self._fetch(conn, improvement_id)

    # ------------------------------------------------------------------ builds

    async def start_build(self, improvement_id: int) -> int:
        """Record that a build started (counted by the daily limit). KeyError for an unknown improvement."""
        return await asyncio.to_thread(self._start_build, improvement_id)

    def _start_build(self, improvement_id: int) -> int:
        with self._transaction() as conn:
            self._fetch(conn, improvement_id)
            cursor = conn.execute(
                "INSERT INTO builds(improvement_id, started_at) VALUES (?, ?)", (improvement_id, self._clock())
            )
            return int(cursor.lastrowid or 0)

    async def finish_build(self, build_id: int, outcome: str, turns: int = 0, cost_usd: float | None = None) -> None:
        await asyncio.to_thread(self._finish_build, build_id, outcome, turns, cost_usd)

    def _finish_build(self, build_id: int, outcome: str, turns: int, cost_usd: float | None) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE builds SET finished_at=?, outcome=?, turns=?, cost_usd=? WHERE id=?",
                (self._clock(), outcome, turns, cost_usd, build_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(build_id)

    async def builds_since(self, since_ts: float) -> int:
        """Builds started since then (the daily limit), except those cut off by a restart and queued again."""
        rows = await asyncio.to_thread(
            self._query,
            "SELECT COUNT(*) FROM builds WHERE started_at >= ? AND (outcome IS NULL OR outcome != ?)",
            (since_ts, REQUEUED),
        )
        return int(rows[0][0])

    async def build_seconds(self, outcome: str, limit: int) -> list[float]:
        """How long the latest builds with this outcome took, newest first ("how long does a build take?")."""
        rows = await asyncio.to_thread(
            self._query,
            "SELECT finished_at - started_at FROM builds WHERE outcome = ? AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (outcome, limit),
        )
        return [float(r[0]) for r in rows]

    # ------------------------------------------------------------------ installs

    async def last_installed(self) -> Improvement | None:
        """The most recently installed improvement that is still in: what "undo the last update" undoes."""
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM improvements WHERE status=? AND merge_commit IS NOT NULL AND merge_commit != '' "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (Status.INSTALLED.value,),
        )
        return _to_improvement(rows[0]) if rows else None

    async def find_by_merge_commit(self, sha: str) -> Improvement | None:
        sha = sha.strip().lower()
        if not sha:
            return None
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM improvements WHERE lower(merge_commit)=? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (sha,),
        )
        return _to_improvement(rows[0]) if rows else None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()
