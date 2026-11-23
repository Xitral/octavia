"""SQLite-backed implementations of the memory interfaces (one database file).

SQLite work is small and fast, so calls run in a worker thread through
``asyncio.to_thread`` with a lock guarding the shared connection.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from octavia.memory.base import (
    APART,
    ConversationStore,
    LongTermMemory,
    MemoryItem,
    SessionRecord,
    ToolStateStore,
    TurnRecord,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    brain TEXT,
    brain_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    pinned INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    session_id TEXT,
    turn_id INTEGER,
    replaces INTEGER,
    superseded_by INTEGER,
    superseded_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text, category, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text, category) VALUES (new.id, new.text, new.category);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text, category) VALUES ('delete', old.id, old.text, old.category);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text, category) VALUES ('delete', old.id, old.text, old.category);
    INSERT INTO memories_fts(rowid, text, category) VALUES (new.id, new.text, new.category);
END;

CREATE TABLE IF NOT EXISTS tool_state (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
"""

# Columns added to ``memories`` after databases were already made: added on open when missing (``Database``).
_MEMORY_COLUMNS = {
    "session_id": "TEXT",
    "turn_id": "INTEGER",
    "replaces": "INTEGER",
    "superseded_by": "INTEGER",
    "superseded_at": "REAL",
}

# Full-text index of every turn, so past conversations can be searched. A database made before it existed is
# indexed once when the table is first created (``Database.__init__``).
_TURNS_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(text, content='turns', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO turns_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


class Database:
    """Owns the SQLite connection. Share one instance between the stores."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(memories)")}
            for column, kind in _MEMORY_COLUMNS.items():
                if column not in have:
                    try:
                        self._conn.execute(f"ALTER TABLE memories ADD COLUMN {column} {kind}")
                    except sqlite3.OperationalError as exc:  # another process opening it added it first
                        if "duplicate column" not in str(exc):
                            raise
            indexed = self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='turns_fts'").fetchone()
            self._conn.executescript(_TURNS_FTS)
            if not indexed:
                self._conn.execute("INSERT INTO turns_fts(turns_fts) VALUES ('rebuild')")
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def run(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def run_insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            return int(self._conn.execute(sql, params).lastrowid or 0)

    async def arun(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self.run, sql, params)

    async def ainsert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return await asyncio.to_thread(self.run_insert, sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _to_item(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        id=row["id"],
        text=row["text"],
        category=row["category"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source=row["source"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        replaces=row["replaces"],
        superseded_by=row["superseded_by"],
    )


_WORD = re.compile(r"[\w']+", re.UNICODE)


def _fts_query(text: str) -> str | None:
    words = [w.strip("'") for w in _WORD.findall(text.lower())]
    words = [w for w in words if len(w) > 1]
    if not words:
        return None
    # Prefix match each word, any word may match; bm25 ranks by overlap.
    return " OR ".join(f'"{w}"*' for w in words[:12])


# Only a fact's current version: a corrected one stays in the table as history (``superseded_by`` set).
_CURRENT = "superseded_by IS NULL"
# Facts about the user, not the notes she keeps for her own work (``APART``: a playbook for an app). Those stay out of
# the prompt's pick, plain recall, listing and counting; only ``list_apart`` reads them.
_FACTS = "category NOT LIKE '" + APART.replace("'", "''") + "%'"
# What the prompt's pick of facts reads to know what the user has been talking about: their recent words.
_CONTEXT_TURNS = 40
_CONTEXT_DAYS = 7
_CONTEXT_WORDS = 12


def _same_words(a: str, b: str) -> bool:
    """The same fact written differently only in case, spacing or punctuation."""
    return [w.strip("'") for w in _WORD.findall(a.lower())] == [w.strip("'") for w in _WORD.findall(b.lower())]


class SqliteLongTermMemory(LongTermMemory):
    def __init__(self, db: Database) -> None:
        self.db = db
        # The ids ``choose_for_prompt`` put in her latest system prompt, in order; None until it has run.
        self._prompt_ids: list[int] | None = None

    async def add(
        self,
        text: str,
        category: str = "general",
        pinned: bool = False,
        source: str = "user",
        session_id: str | None = None,
    ) -> MemoryItem:
        text = " ".join(text.split())
        if not text:
            raise ValueError("memory text is empty")
        now = time.time()
        same = await self._find_same(text, category=category)
        if same is not None:
            # Saying it again refreshes it; hearing it from the user outranks having worked it out.
            await self.db.arun(
                "UPDATE memories SET updated_at=?, pinned=MAX(pinned, ?), category=?, "
                "source=CASE WHEN ?='user' THEN 'user' ELSE source END WHERE id=?",
                (now, int(pinned), category, source, same.id),
            )
            return await self._get(same.id)
        return await self._insert(text, category, pinned, source, session_id, None, now)

    async def correct(
        self, item_id: int, text: str, session_id: str | None = None, source: str = "user"
    ) -> MemoryItem | None:
        old = await self._current(item_id)
        if old is None:
            return None
        text = " ".join(text.split())
        if not text:
            raise ValueError("memory text is empty")
        if _same_words(text, old.text):
            return old
        now = time.time()
        same = await self._find_same(text, exclude=old.id, category=old.category)
        if same is not None:  # the corrected fact is already saved: the old one folds into it
            new = same
            await self.db.arun(
                "UPDATE memories SET updated_at=?, pinned=MAX(pinned, ?) WHERE id=?", (now, int(old.pinned), new.id)
            )
        else:
            new = await self._insert(text, old.category, old.pinned, source, session_id, old.id, now)
        await self.db.arun("UPDATE memories SET superseded_by=?, superseded_at=? WHERE id=?", (new.id, now, old.id))
        return await self._get(new.id)

    async def history(self, item_id: int) -> list[MemoryItem]:
        """The fact first, then every version it replaced: the one it was written over (``replaces``) and any that
        folded into it (a correction that matched it), newest first."""
        first = await self.get(item_id)
        if first is None:
            return []
        chain, seen, todo = [first], {first.id}, [first]
        while todo:
            item = todo.pop(0)
            rows = await self.db.arun(
                "SELECT * FROM memories WHERE (id=? OR superseded_by=?) AND id!=? ORDER BY created_at DESC, id DESC",
                (item.replaces if item.replaces is not None else -1, item.id, item.id),
            )
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    older = _to_item(r)
                    chain.append(older)
                    todo.append(older)
        return chain

    async def search(self, query: str, limit: int = 5, include_old: bool = False) -> list[MemoryItem]:
        current = f" AND m.{_FACTS}" if include_old else f" AND m.{_CURRENT} AND m.{_FACTS}"
        fts = _fts_query(query)
        if fts is not None:
            rows = await self.db.arun(
                "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                f"WHERE memories_fts MATCH ?{current} ORDER BY bm25(memories_fts) LIMIT ?",
                (fts, limit),
            )
            if rows:
                return [_to_item(r) for r in rows]
        like = f"%{query.strip()}%"
        rows = await self.db.arun(
            f"SELECT * FROM memories m WHERE text LIKE ?{current} ORDER BY updated_at DESC LIMIT ?", (like, limit)
        )
        return [_to_item(r) for r in rows]

    async def list(self, limit: int = 50, category: str | None = None) -> list[MemoryItem]:
        if category:
            rows = await self.db.arun(
                f"SELECT * FROM memories WHERE category=? AND {_CURRENT} ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (category, limit),
            )
        else:
            rows = await self.db.arun(
                f"SELECT * FROM memories WHERE {_CURRENT} AND {_FACTS} ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (limit,),
            )
        return [_to_item(r) for r in rows]

    async def count(self) -> int:
        rows = await self.db.arun(f"SELECT COUNT(*) AS n FROM memories WHERE {_CURRENT} AND {_FACTS}")
        return int(rows[0]["n"])

    async def delete(self, item_id: int) -> bool:
        """Delete a fact with the older versions it corrected: forgetting it mustn't leave what it said behind."""
        chain = await self.history(item_id)
        if not chain:
            return False
        ids = tuple(m.id for m in chain)
        marks = ",".join("?" * len(ids))
        await self.db.arun(f"DELETE FROM memories WHERE id IN ({marks})", ids)
        # A newer version of a deleted one stays, without that history.
        await self.db.arun(f"UPDATE memories SET replaces=NULL WHERE replaces IN ({marks})", ids)
        return True

    async def list_apart(self, prefix: str = APART, limit: int = 500) -> list[MemoryItem]:
        if not prefix.startswith(APART):
            raise ValueError(f"only notes kept apart ({APART}...) are listed here")
        escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        rows = await self.db.arun(
            f"SELECT * FROM memories WHERE category LIKE ? ESCAPE '!' AND {_CURRENT} ORDER BY updated_at DESC LIMIT ?",
            (escaped + "%", limit),
        )
        return [_to_item(r) for r in rows]

    async def trim_history(self, item_id: int, keep: int) -> int:
        """Delete all but the ``keep`` newest older versions of a fact; returns how many went."""
        chain = await self.history(item_id)
        old = chain[1 + max(0, keep) :]
        if not old:
            return 0
        ids = tuple(m.id for m in old)
        marks = ",".join("?" * len(ids))
        await self.db.arun(f"DELETE FROM memories WHERE id IN ({marks})", ids)
        await self.db.arun(f"UPDATE memories SET replaces=NULL WHERE replaces IN ({marks})", ids)
        return len(ids)

    async def get(self, item_id: int) -> MemoryItem | None:
        rows = await self.db.arun("SELECT * FROM memories WHERE id=?", (item_id,))
        return _to_item(rows[0]) if rows else None

    async def for_prompt(self, limit: int) -> list[MemoryItem]:
        """The facts in her latest system prompt that are still current (``choose_for_prompt``), so "already in the
        prompt" means the same to the turn's recall and her quick voice; before any prompt, pinned then recent."""
        if self._prompt_ids is None:
            return await self.list(limit=limit)
        if not self._prompt_ids or limit <= 0:
            return []
        marks = ",".join("?" * len(self._prompt_ids))
        rows = await self.db.arun(
            f"SELECT * FROM memories WHERE id IN ({marks}) AND {_CURRENT}", tuple(self._prompt_ids)
        )
        by_id = {r["id"]: _to_item(r) for r in rows}
        return [by_id[i] for i in self._prompt_ids if i in by_id][:limit]

    async def choose_for_prompt(self, limit: int) -> list[MemoryItem]:
        """Pinned facts, then up to half the room left for those that match what the user has been talking about
        (their words in the last week's turns), then the most recently saved."""
        chosen: list[MemoryItem] = []
        if limit > 0:
            chosen = await self._pinned(limit)
            taken = {m.id for m in chosen}
            room = limit - len(chosen)
            if room > 0:
                for m in await self._relevant((room + 1) // 2):
                    if m.id not in taken:
                        chosen.append(m)
                        taken.add(m.id)
                rows = await self.db.arun(
                    f"SELECT * FROM memories WHERE {_CURRENT} AND {_FACTS} AND pinned=0 ORDER BY updated_at DESC "
                    "LIMIT ?",
                    (limit + len(taken),),
                )
                for r in rows:
                    if len(chosen) >= limit:
                        break
                    if r["id"] not in taken:
                        chosen.append(_to_item(r))
                        taken.add(r["id"])
        self._prompt_ids = [m.id for m in chosen]
        return chosen

    async def _pinned(self, limit: int) -> list[MemoryItem]:
        rows = await self.db.arun(
            f"SELECT * FROM memories WHERE {_CURRENT} AND {_FACTS} AND pinned=1 ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [_to_item(r) for r in rows]

    async def _relevant(self, limit: int) -> list[MemoryItem]:
        from octavia.core.conversation_memory import keywords  # core imports memory, not the other way round

        rows = await self.db.arun(
            "SELECT text FROM turns WHERE role='user' AND created_at>=? ORDER BY id DESC LIMIT ?",
            (time.time() - _CONTEXT_DAYS * 86400, _CONTEXT_TURNS),
        )
        counts: Counter[str] = Counter()
        for r in rows:
            counts.update(keywords(r["text"]))
        fts = _fts_query(" ".join(w for w, _ in counts.most_common(_CONTEXT_WORDS)))
        if fts is None or limit <= 0:
            return []
        found = await self.db.arun(
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
            f"WHERE memories_fts MATCH ? AND m.{_CURRENT} AND m.{_FACTS} AND m.pinned=0 "
            "ORDER BY bm25(memories_fts) LIMIT ?",
            (fts, limit),
        )
        return [_to_item(r) for r in found]

    async def _current(self, item_id: int) -> MemoryItem | None:
        """The fact ``item_id`` is now: itself, or the correction that replaced it (an old id she still had)."""
        item = await self.get(item_id)
        seen: set[int] = set()
        while item is not None and item.superseded_by is not None and item.id not in seen:
            seen.add(item.id)
            newer = await self.get(item.superseded_by)
            if newer is None:
                break
            item = newer
        return item

    async def _find_same(self, text: str, exclude: int | None = None, category: str = "") -> MemoryItem | None:
        """A current fact that says ``text`` already (ignoring case, spacing and punctuation). A note kept apart
        (``APART``) only ever matches one of its own category, and a fact never matches such a note."""
        if category.startswith(APART):
            rows = await self.db.arun(
                f"SELECT * FROM memories WHERE lower(text) = lower(?) AND {_CURRENT} AND category=?", (text, category)
            )
            candidates = [_to_item(r) for r in rows]
        else:
            rows = await self.db.arun(
                f"SELECT * FROM memories WHERE lower(text) = lower(?) AND {_CURRENT} AND {_FACTS}", (text,)
            )
            candidates = [_to_item(r) for r in rows] + await self.search(text, limit=25)
        return next((m for m in candidates if m.id != exclude and _same_words(m.text, text)), None)

    async def _insert(
        self,
        text: str,
        category: str,
        pinned: bool,
        source: str,
        session_id: str | None,
        replaces: int | None,
        now: float,
    ) -> MemoryItem:
        turn_id = None
        if session_id is not None:  # the user's turn it was saved during: what they said that it came from
            rows = await self.db.arun(
                "SELECT MAX(id) AS id FROM turns WHERE session_id=? AND role='user'", (session_id,)
            )
            turn_id = rows[0]["id"] if rows else None
        item_id = await self.db.ainsert(
            "INSERT INTO memories(text, category, pinned, source, created_at, updated_at, session_id, turn_id, "
            "replaces) VALUES (?,?,?,?,?,?,?,?,?)",
            (text, category, int(pinned), source, now, now, session_id, turn_id, replaces),
        )
        return await self._get(item_id)

    async def _get(self, item_id: int) -> MemoryItem:
        item = await self.get(item_id)
        if item is None:
            raise LookupError(f"memory {item_id} vanished")
        return item


class SqliteConversationStore(ConversationStore):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def start_session(self, session_id: str, brain: str) -> None:
        await self.db.arun(
            "INSERT OR IGNORE INTO sessions(id, brain, started_at) VALUES (?,?,?)", (session_id, brain, time.time())
        )

    async def end_session(self, session_id: str, reason: str, summary: str | None = None) -> None:
        await self.db.arun(
            "UPDATE sessions SET ended_at=?, end_reason=?, summary=COALESCE(?, summary) WHERE id=?",
            (time.time(), reason, summary, session_id),
        )

    async def set_brain_session(self, session_id: str, brain_session_id: str) -> None:
        await self.db.arun("UPDATE sessions SET brain_session_id=? WHERE id=?", (brain_session_id, session_id))

    async def add_turn(self, session_id: str, role: str, text: str, interrupted: bool = False) -> None:
        await self.db.arun(
            "INSERT INTO turns(session_id, role, text, interrupted, created_at) VALUES (?,?,?,?,?)",
            (session_id, role, text, int(interrupted), time.time()),
        )

    async def recent_turns(self, session_id: str, limit: int = 20) -> list[TurnRecord]:
        rows = await self.db.arun(
            "SELECT * FROM (SELECT * FROM turns WHERE session_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (session_id, limit),
        )
        return _to_turns(rows)

    async def latest_turns(self, limit: int = 20) -> list[TurnRecord]:
        rows = await self.db.arun(
            "SELECT * FROM (SELECT * FROM turns WHERE session_id=(SELECT session_id FROM turns ORDER BY id DESC LIMIT 1)"
            " ORDER BY id DESC LIMIT ?) ORDER BY id",
            (limit,),
        )
        return _to_turns(rows)

    async def turns_since(self, since: float, limit: int = 100) -> list[TurnRecord]:
        rows = await self.db.arun(
            "SELECT * FROM (SELECT * FROM turns WHERE created_at>=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (since, limit),
        )
        return _to_turns(rows)

    async def turns_between(self, start: float, end: float, limit: int = 500) -> list[TurnRecord]:
        rows = await self.db.arun(
            "SELECT * FROM (SELECT * FROM turns WHERE created_at>=? AND created_at<? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (start, end, limit),
        )
        return _to_turns(rows)

    async def next_turn_at(self, at: float, *, later: bool) -> float | None:
        sql = (
            "SELECT MIN(created_at) AS at FROM turns WHERE created_at>?"
            if later
            else ("SELECT MAX(created_at) AS at FROM turns WHERE created_at<?")
        )
        rows = await self.db.arun(sql, (at,))
        found = rows[0]["at"] if rows else None
        return float(found) if found is not None else None

    async def search_turns(
        self, query: str, limit: int = 10, exclude_session: str | None = None, since: float | None = None
    ) -> list[TurnRecord]:
        fts = _fts_query(query)
        if fts is None:
            return []
        rows = await self.db.arun(
            "SELECT t.* FROM turns_fts f JOIN turns t ON t.id = f.rowid "
            "WHERE turns_fts MATCH ? AND t.session_id IS NOT ? AND t.created_at >= ? "
            "ORDER BY bm25(turns_fts) LIMIT ?",
            (fts, exclude_session, since or 0.0, limit),
        )
        return _to_turns(rows)

    async def recent_sessions(self, limit: int = 10, since: float = 0.0) -> list[SessionRecord]:
        rows = await self.db.arun(
            "SELECT s.id, s.started_at, s.ended_at, s.summary, COUNT(t.id) AS turns, MAX(t.created_at) AS last_at "
            "FROM sessions s JOIN turns t ON t.session_id = s.id WHERE s.started_at >= ? "
            "GROUP BY s.id ORDER BY s.started_at DESC, s.rowid DESC LIMIT ?",
            (since, limit),
        )
        return [
            SessionRecord(
                id=r["id"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                summary=r["summary"],
                turns=r["turns"],
                last_turn_at=r["last_at"],
            )
            for r in rows
        ]


def _to_turns(rows: list[sqlite3.Row]) -> list[TurnRecord]:
    return [
        TurnRecord(
            session_id=r["session_id"],
            role=r["role"],
            text=r["text"],
            created_at=r["created_at"],
            interrupted=bool(r["interrupted"]),
        )
        for r in rows
    ]


class SqliteToolStateStore(ToolStateStore):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, namespace: str, key: str, default: Any = None) -> Any:
        rows = await self.db.arun("SELECT value FROM tool_state WHERE namespace=? AND key=?", (namespace, key))
        return json.loads(rows[0]["value"]) if rows else default

    async def set(self, namespace: str, key: str, value: Any) -> None:
        await self.db.arun(
            "INSERT INTO tool_state(namespace, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (namespace, key, json.dumps(value), time.time()),
        )

    async def delete(self, namespace: str, key: str) -> None:
        await self.db.arun("DELETE FROM tool_state WHERE namespace=? AND key=?", (namespace, key))
