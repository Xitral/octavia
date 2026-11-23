"""Her journal: one SQLite file (``inner_life.database``, default ``data/inner/inner.db``).

A file of its own, like Futures', so the user can read, export or wipe what she thought without touching the
conversations, the saved notes or what Futures learned. It has real migrations (``MIGRATIONS`` + ``PRAGMA
user_version``); a database from a newer version is refused.

What's kept is only what the reflection wrote and the checks let through (``rules``): never the material she was
given (only its size and a hash), never what a tool returned.

Threading: one connection behind a re-entrant lock. Every method is synchronous and safe to call from any thread;
async code calls them through ``asyncio.to_thread`` (``InnerLife``), and the proactive gate calls ``candidates`` from
Futures' worker thread.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("follow_up", "question", "idea", "connection", "noticed")
BASES = ("fact", "observation", "correlation", "guess")
BRING_UP = ("next_conversation", "when_relevant", "only_if_asked")
OPEN = "open"
STATUSES = (OPEN, "shared", "dismissed", "resolved", "expired")
MAX_OFFERS = 3  # conversations a thought is offered in before it's let go, so nothing is offered forever
OFFERED_OUT = "offered in several conversations without fitting"
# What happened to her, as she remembers it: what surprised her, what she got wrong, what she enjoyed, what she learned.
EXPERIENCE_KINDS = ("surprised", "got_wrong", "enjoyed", "learned")
DAY_ENTRY, LOOKING_BACK, DREAM = "day", "looking_back", "dream"  # the kinds of journal entry
ENTRY_KINDS = (DAY_ENTRY, LOOKING_BACK, DREAM)
# A changed stance is a leaning first: it becomes her opinion only when a reflection on a later night (this long
# after) still sees it differently, and a leaning no later night took up is let go after LEAN_KEEP_S.
LEAN_MIN_S = 20 * 3600.0
LEAN_KEEP_S = 14 * 86400.0
# A note she jots in conversation waits for that night's reflection to keep it (``settle_jotted``); one no night kept
# within JOTTED_KEEP_S is let go.
JOTTED = "jotted"
JOTTED_KEEP_S = 3 * 86400.0


class StoreError(RuntimeError):
    """The journal can't be used (written by a newer version, or not a database)."""


MIGRATIONS: tuple[str, ...] = (
    # 1: the first schema
    """
    CREATE TABLE reflections (
        id INTEGER PRIMARY KEY,
        started_at REAL NOT NULL,
        finished_at REAL,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        digest_chars INTEGER NOT NULL DEFAULT 0,
        digest_hash TEXT NOT NULL DEFAULT '',
        tools TEXT NOT NULL DEFAULT '[]',
        usage TEXT NOT NULL DEFAULT '{}',
        model TEXT,
        dropped TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX reflections_by_time ON reflections(started_at);
    CREATE TABLE entries (
        id INTEGER PRIMARY KEY,
        reflection_id INTEGER,
        created_at REAL NOT NULL,
        text TEXT NOT NULL
    );
    CREATE TABLE thoughts (
        id INTEGER PRIMARY KEY,
        reflection_id INTEGER,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        kind TEXT NOT NULL,
        thought TEXT NOT NULL,
        say TEXT NOT NULL,
        topic TEXT NOT NULL,
        keywords TEXT NOT NULL DEFAULT '[]',
        basis TEXT NOT NULL,
        sources TEXT NOT NULL DEFAULT '[]',
        domain TEXT NOT NULL,
        sensitive INTEGER NOT NULL DEFAULT 0,
        importance REAL NOT NULL,
        bring_up TEXT NOT NULL,
        status TEXT NOT NULL,
        expires_at REAL,
        offered INTEGER NOT NULL DEFAULT 0,
        revisions INTEGER NOT NULL DEFAULT 0,
        closed_at REAL,
        close_reason TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX thoughts_by_status ON thoughts(status, importance);
    CREATE TABLE opinions (
        id INTEGER PRIMARY KEY,
        reflection_id INTEGER,
        created_at REAL NOT NULL,
        topic TEXT NOT NULL,
        stance TEXT NOT NULL,
        why TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        replaced_by INTEGER
    );
    CREATE INDEX opinions_by_topic ON opinions(topic, active);
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    """,
    # 2: every domain a thought touches (one put off-limits later hides it), and when it was last offered
    """
    ALTER TABLE thoughts ADD COLUMN domains TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE thoughts ADD COLUMN offered_at REAL;
    """,
    # 3: a growing self: what happened to her, how she's felt night by night, her own self-notes, opinions she's only
    # leaning toward yet, and entries that are a monthly look back or a dream
    """
    CREATE TABLE experiences (
        id INTEGER PRIMARY KEY,
        reflection_id INTEGER,
        created_at REAL NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        topic TEXT NOT NULL,
        keywords TEXT NOT NULL DEFAULT '[]',
        sources TEXT NOT NULL DEFAULT '[]',
        domains TEXT NOT NULL DEFAULT '[]',
        feeling REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX experiences_by_time ON experiences(created_at);
    CREATE TABLE feelings (
        id INTEGER PRIMARY KEY,
        reflection_id INTEGER,
        created_at REAL NOT NULL,
        word TEXT NOT NULL,
        why TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE self_notes (
        id INTEGER PRIMARY KEY,
        created_at REAL NOT NULL,
        origin TEXT NOT NULL,
        reflection_id INTEGER,
        text TEXT NOT NULL,
        about TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        replaced_by INTEGER,
        replaces INTEGER,
        closed_at REAL,
        close_reason TEXT NOT NULL DEFAULT ''
    );
    ALTER TABLE entries ADD COLUMN kind TEXT NOT NULL DEFAULT 'day';
    ALTER TABLE opinions ADD COLUMN leaning INTEGER NOT NULL DEFAULT 0;
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)
# Everything she wrote ("forget everything" deletes all of it).
WRITTEN = ("thoughts", "opinions", "entries", "experiences", "feelings", "self_notes")


def similar(a: str, b: str) -> bool:
    """The same stance in other words: most of the shorter one's words are in the other ("Synthwave beats it" and
    "Synthwave beats lo-fi for sets")."""
    words = [set(re.findall(r"[a-z0-9']+", t.lower())) - _FILLER for t in (a, b)]
    small, big = sorted(words, key=len)
    return bool(small) and len(small & big) >= max(1, round(0.6 * len(small)))


_FILLER = frozenset("a an the it is it's i i'm to of for and or but so that this at in on my me just really".split())


def _same(a: str, b: str) -> bool:
    """The same words, whatever the case and spacing."""
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Thought:
    id: int
    reflection_id: int | None
    created_at: float
    updated_at: float
    kind: str
    thought: str
    say: str
    topic: str
    keywords: tuple[str, ...]
    basis: str
    sources: tuple[str, ...]
    domain: str
    sensitive: bool
    importance: float
    bring_up: str
    status: str
    expires_at: float | None
    offered: int
    domains: tuple[str, ...]
    offered_at: float | None
    revisions: int
    closed_at: float | None
    close_reason: str

    @property
    def is_open(self) -> bool:
        return self.status == OPEN


@dataclass(frozen=True)
class Opinion:
    id: int
    reflection_id: int | None
    created_at: float
    topic: str
    stance: str
    why: str
    active: bool
    replaced_by: int | None
    leaning: bool = False  # a changed stance she isn't settled on yet (``lean``): not her opinion so far


@dataclass(frozen=True)
class Experience:
    """Something that happened to her, in her own words: ``feeling`` from -1 (it stung) to 1 (she loved it)."""

    id: int
    reflection_id: int | None
    created_at: float
    kind: str
    text: str
    topic: str
    keywords: tuple[str, ...]
    sources: tuple[str, ...]
    domains: tuple[str, ...]
    feeling: float


@dataclass(frozen=True)
class Feeling:
    """How a night's reflection left her feeling, in one of ``rules.FEELINGS``."""

    id: int
    reflection_id: int | None
    created_at: float
    word: str
    why: str


@dataclass(frozen=True)
class SelfNote:
    """One of her own notes about herself, written without asking (``origin`` reflection or conversation). One she
    rewrote points to its newer version; one taken back keeps why."""

    id: int
    created_at: float
    origin: str
    reflection_id: int | None
    text: str
    about: str
    active: bool
    replaced_by: int | None
    replaces: int | None
    closed_at: float | None
    close_reason: str


@dataclass(frozen=True)
class Reflection:
    id: int
    started_at: float
    finished_at: float | None
    trigger: str
    status: str  # running | done | skipped | failed
    reason: str
    digest_chars: int
    digest_hash: str
    tools: tuple[str, ...]
    usage: dict[str, Any]
    model: str | None
    dropped: tuple[str, ...]  # why each thought the checks refused was refused


@dataclass(frozen=True)
class Entry:
    id: int
    reflection_id: int | None
    created_at: float
    text: str
    kind: str = DAY_ENTRY


def _thought(row: sqlite3.Row) -> Thought:
    return Thought(
        id=row["id"],
        reflection_id=row["reflection_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        kind=row["kind"],
        thought=row["thought"],
        say=row["say"],
        topic=row["topic"],
        keywords=tuple(loads(row["keywords"], [])),
        basis=row["basis"],
        sources=tuple(loads(row["sources"], [])),
        domain=row["domain"],
        sensitive=bool(row["sensitive"]),
        importance=row["importance"],
        bring_up=row["bring_up"],
        status=row["status"],
        expires_at=row["expires_at"],
        offered=row["offered"],
        domains=tuple(loads(row["domains"], [])),
        offered_at=row["offered_at"],
        revisions=row["revisions"],
        closed_at=row["closed_at"],
        close_reason=row["close_reason"],
    )


def _opinion(row: sqlite3.Row) -> Opinion:
    return Opinion(
        id=row["id"],
        reflection_id=row["reflection_id"],
        created_at=row["created_at"],
        topic=row["topic"],
        stance=row["stance"],
        why=row["why"],
        active=bool(row["active"]),
        replaced_by=row["replaced_by"],
        leaning=bool(row["leaning"]),
    )


def _experience(row: sqlite3.Row) -> Experience:
    return Experience(
        id=row["id"],
        reflection_id=row["reflection_id"],
        created_at=row["created_at"],
        kind=row["kind"],
        text=row["text"],
        topic=row["topic"],
        keywords=tuple(loads(row["keywords"], [])),
        sources=tuple(loads(row["sources"], [])),
        domains=tuple(loads(row["domains"], [])),
        feeling=row["feeling"],
    )


def _self_note(row: sqlite3.Row) -> SelfNote:
    return SelfNote(
        id=row["id"],
        created_at=row["created_at"],
        origin=row["origin"],
        reflection_id=row["reflection_id"],
        text=row["text"],
        about=row["about"],
        active=bool(row["active"]),
        replaced_by=row["replaced_by"],
        replaces=row["replaces"],
        closed_at=row["closed_at"],
        close_reason=row["close_reason"],
    )


def _reflection(row: sqlite3.Row) -> Reflection:
    return Reflection(
        id=row["id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        trigger=row["trigger"],
        status=row["status"],
        reason=row["reason"],
        digest_chars=row["digest_chars"],
        digest_hash=row["digest_hash"],
        tools=tuple(loads(row["tools"], [])),
        usage=loads(row["usage"], {}) or {},
        model=row["model"],
        dropped=tuple(loads(row["dropped"], [])),
    )


class InnerStore:
    """The journal. ``path`` may be ``":memory:"`` (tests); ``clock`` is epoch seconds, like ``time.time``."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._depth = 0
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")  # `octavia inner` beside the running assistant
                self._migrate()
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"her journal can't be opened: {exc}") from None

    # ------------------------------------------------------------------ schema and access
    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        version = self.schema_version
        if version > SCHEMA_VERSION:
            raise StoreError(f"her journal is version {version}, newer than this code ({SCHEMA_VERSION})")
        for number in range(version, SCHEMA_VERSION):
            with self.transaction() as conn:  # one by one: executescript would commit halfway
                for statement in MIGRATIONS[number].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {number + 1}")
            log.info("Her journal was migrated to version %d", number + 1)

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

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def _write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ small settings
    def get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return loads(row["value"], default) if row else default

    def set(self, key: str, value: Any) -> None:
        self._write(
            "INSERT INTO kv(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, dumps(value), self.clock()),
        )

    # ------------------------------------------------------------------ reflections
    def start_reflection(self, trigger: str) -> int:
        cur = self._write(
            "INSERT INTO reflections(started_at, trigger, status) VALUES (?,?,?)", (self.clock(), trigger, "running")
        )
        return int(cur.lastrowid or 0)

    def finish_reflection(
        self,
        reflection_id: int,
        status: str,
        *,
        reason: str = "",
        digest_chars: int = 0,
        digest_hash: str = "",
        tools: Iterable[str] = (),
        usage: dict[str, Any] | None = None,
        model: str | None = None,
        dropped: Iterable[str] = (),
    ) -> None:
        self._write(
            "UPDATE reflections SET finished_at=?, status=?, reason=?, digest_chars=?, digest_hash=?, tools=?, "
            "usage=?, model=?, dropped=? WHERE id=?",
            (
                self.clock(),
                status,
                reason,
                digest_chars,
                digest_hash,
                dumps(sorted(set(tools))),
                dumps(usage or {}),
                model,
                dumps(list(dropped)),
                reflection_id,
            ),
        )

    def reflections(self, limit: int = 10) -> list[Reflection]:
        rows = self._rows("SELECT * FROM reflections ORDER BY started_at DESC, id DESC LIMIT ?", (limit,))
        return [_reflection(r) for r in rows]

    def last_reflection(self, status: str = "done") -> Reflection | None:
        row = self._one("SELECT * FROM reflections WHERE status=? ORDER BY started_at DESC, id DESC LIMIT 1", (status,))
        return _reflection(row) if row else None

    def attempts_since(self, since: float) -> int:
        """Reflections that ran (or tried to) since ``since``: skipped ones cost nothing and don't count."""
        row = self._one("SELECT COUNT(*) AS n FROM reflections WHERE started_at>=? AND status<>'skipped'", (since,))
        return int(row["n"]) if row else 0

    def abandon_running(self) -> int:
        """Reflections left 'running' by a crash or a shutdown are marked failed (at start)."""
        return self._write(
            "UPDATE reflections SET status='failed', reason='interrupted', finished_at=? WHERE status='running'",
            (self.clock(),),
        ).rowcount

    # ------------------------------------------------------------------ entries
    def add_entry(self, reflection_id: int | None, text: str, kind: str = DAY_ENTRY) -> int:
        if kind not in ENTRY_KINDS:
            raise ValueError(f"not a kind of entry: {kind!r}")
        cur = self._write(
            "INSERT INTO entries(reflection_id, created_at, text, kind) VALUES (?,?,?,?)",
            (reflection_id, self.clock(), text, kind),
        )
        return int(cur.lastrowid or 0)

    def entries(self, limit: int = 5, kind: str = DAY_ENTRY, *, since: float | None = None) -> list[Entry]:
        """Newest first: her day entries, or her monthly look backs (``LOOKING_BACK``) or dreams (``DREAM``)."""
        rows = self._rows(
            "SELECT * FROM entries WHERE kind=? AND created_at>=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (kind, since if since is not None else float("-inf"), limit),
        )
        return [Entry(r["id"], r["reflection_id"], r["created_at"], r["text"], r["kind"]) for r in rows]

    # ------------------------------------------------------------------ experiences and feelings
    def add_experience(
        self,
        reflection_id: int | None,
        *,
        kind: str,
        text: str,
        topic: str,
        keywords: Iterable[str],
        sources: Iterable[str],
        domains: Iterable[str],
        feeling: float,
    ) -> int:
        if kind not in EXPERIENCE_KINDS:
            raise ValueError(f"not a kind of experience: {kind!r}")
        cur = self._write(
            "INSERT INTO experiences(reflection_id, created_at, kind, text, topic, keywords, sources, domains, "
            "feeling) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                reflection_id,
                self.clock(),
                kind,
                text,
                topic,
                dumps(list(keywords)),
                dumps(list(sources)),
                dumps(sorted(set(domains))),
                min(max(float(feeling), -1.0), 1.0),
            ),
        )
        return int(cur.lastrowid or 0)

    def experiences(self, limit: int = 30, *, since: float | None = None) -> list[Experience]:
        """Newest first."""
        rows = self._rows(
            "SELECT * FROM experiences WHERE created_at>=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (since if since is not None else float("-inf"), limit),
        )
        return [_experience(r) for r in rows]

    def add_feeling(self, reflection_id: int | None, word: str, why: str) -> int:
        cur = self._write(
            "INSERT INTO feelings(reflection_id, created_at, word, why) VALUES (?,?,?,?)",
            (reflection_id, self.clock(), word, why),
        )
        return int(cur.lastrowid or 0)

    def feelings(self, limit: int = 7, *, since: float | None = None) -> list[Feeling]:
        """Newest first."""
        rows = self._rows(
            "SELECT * FROM feelings WHERE created_at>=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (since if since is not None else float("-inf"), limit),
        )
        return [Feeling(r["id"], r["reflection_id"], r["created_at"], r["word"], r["why"]) for r in rows]

    # ------------------------------------------------------------------ her own self-notes
    def add_self_note(
        self,
        origin: str,
        text: str,
        *,
        about: str = "",
        reflection_id: int | None = None,
        replaces: int | None = None,
    ) -> int:
        """A note of hers about herself. ``replaces`` an active note of hers: that one stays, pointing to this one,
        so ``undo_self_note`` can put it back."""
        with self.transaction():
            if replaces is not None:
                old = self.self_note(replaces)
                if old is None or not old.active:
                    replaces = None
            cur = self._write(
                "INSERT INTO self_notes(created_at, origin, reflection_id, text, about, replaces) VALUES (?,?,?,?,?,?)",
                (self.clock(), origin, reflection_id, text, about, replaces),
            )
            new_id = int(cur.lastrowid or 0)
            if replaces is not None:
                self._write(
                    "UPDATE self_notes SET active=0, replaced_by=?, closed_at=?, close_reason='rewritten' WHERE id=?",
                    (new_id, self.clock(), replaces),
                )
            return new_id

    def jot_self_note(self, text: str, *, about: str = "", replaces: int | None = None) -> int:
        """A note she jotted in conversation: not hers yet (not in her prompt) until a night keeps it."""
        cur = self._write(
            "INSERT INTO self_notes(created_at, origin, text, about, replaces, active, close_reason) "
            "VALUES (?,?,?,?,?,0,?)",
            (self.clock(), "conversation", text, about, replaces, JOTTED),
        )
        return int(cur.lastrowid or 0)

    def jotted(self, limit: int = 20) -> list[SelfNote]:
        """Notes jotted in conversation that no night kept or let go yet, oldest first."""
        rows = self._rows(
            "SELECT * FROM self_notes WHERE active=0 AND close_reason=? AND closed_at IS NULL "
            "ORDER BY created_at, id LIMIT ?",
            (JOTTED, limit),
        )
        return [_self_note(r) for r in rows]

    def settle_jotted(self, note_id: int, reflection_id: int | None = None) -> SelfNote | None:
        """A night keeps a jotted note: it's an active note of hers now, rewriting the one it named if that one is
        still current. None if it wasn't a jotted note."""
        with self.transaction():
            note = self.self_note(note_id)
            if note is None or note.active or note.close_reason != JOTTED or note.closed_at is not None:
                return None
            replaces = note.replaces
            old = self.self_note(replaces) if replaces is not None else None
            if old is None or not old.active:
                replaces = None
            self._write(
                "UPDATE self_notes SET active=1, close_reason='', reflection_id=?, replaces=? WHERE id=?",
                (reflection_id, replaces, note_id),
            )
            if replaces is not None:
                self._write(
                    "UPDATE self_notes SET active=0, replaced_by=?, closed_at=?, close_reason='rewritten' WHERE id=?",
                    (note_id, self.clock(), replaces),
                )
            return self.self_note(note_id)

    def let_go_of_jotted(self, now: float | None = None) -> int:
        """Jotted notes no night kept within ``JOTTED_KEEP_S``."""
        now = self.clock() if now is None else now
        return self._write(
            "UPDATE self_notes SET closed_at=?, close_reason='not kept by a night' "
            "WHERE active=0 AND close_reason=? AND closed_at IS NULL AND created_at<=?",
            (now, JOTTED, now - JOTTED_KEEP_S),
        ).rowcount

    def self_note(self, note_id: int) -> SelfNote | None:
        row = self._one("SELECT * FROM self_notes WHERE id=?", (note_id,))
        return _self_note(row) if row else None

    def self_notes(self, *, active: bool = True, limit: int = 50, origin: str | None = None) -> list[SelfNote]:
        """Newest first; ``active`` False for every one, rewritten and taken back ones too."""
        where = [*(["active=1"] if active else []), *(["origin=?"] if origin else [])]
        sql = "SELECT * FROM self_notes" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self._rows(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*([origin] if origin else []), limit))
        return [_self_note(r) for r in rows]

    def self_notes_since(self, since: float, origin: str | None = None) -> int:
        """How many she wrote since ``since`` (from ``origin``, or anywhere)."""
        row = self._one(
            "SELECT COUNT(*) AS n FROM self_notes WHERE created_at>=?" + (" AND origin=?" if origin else ""),
            (since, *([origin] if origin else [])),
        )
        return int(row["n"]) if row else 0

    def retract_self_note(self, note_id: int, reason: str = "taken back") -> SelfNote | None:
        """An active note of hers is taken back (it stays in her history); None if it wasn't active."""
        with self.transaction():
            cur = self._write(
                "UPDATE self_notes SET active=0, closed_at=?, close_reason=? WHERE id=? AND active=1",
                (self.clock(), reason, note_id),
            )
            return self.self_note(note_id) if cur.rowcount else None

    def undo_self_note(self, note_id: int) -> SelfNote | None:
        """Take back an active note and put back the one it rewrote, if that one is still only rewritten (not taken
        back). Returns the note put back, or the taken-back note when it rewrote nothing; None if it wasn't active."""
        with self.transaction():
            note = self.retract_self_note(note_id, "undone")
            if note is None:
                return None
            if note.replaces is not None:
                old = self.self_note(note.replaces)
                if old is not None and old.replaced_by == note.id and old.close_reason == "rewritten":
                    self._write(
                        "UPDATE self_notes SET active=1, replaced_by=NULL, closed_at=NULL, close_reason='' WHERE id=?",
                        (old.id,),
                    )
                    return self.self_note(old.id)
            return note

    # ------------------------------------------------------------------ thoughts
    def add_thought(
        self,
        reflection_id: int | None,
        *,
        kind: str,
        thought: str,
        say: str,
        topic: str,
        keywords: Iterable[str],
        basis: str,
        sources: Iterable[str],
        domain: str,
        sensitive: bool,
        importance: float,
        bring_up: str,
        expires_at: float | None,
        domains: Iterable[str] = (),
    ) -> int:
        now = self.clock()
        cur = self._write(
            "INSERT INTO thoughts(reflection_id, created_at, updated_at, kind, thought, say, topic, keywords, basis, "
            "sources, domain, sensitive, importance, bring_up, status, expires_at, domains) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reflection_id,
                now,
                now,
                kind,
                thought,
                say,
                topic,
                dumps(list(keywords)),
                basis,
                dumps(list(sources)),
                domain,
                int(sensitive),
                importance,
                bring_up,
                OPEN,
                expires_at,
                dumps(sorted(set(domains))),
            ),
        )
        return int(cur.lastrowid or 0)

    def revise_thought(self, thought_id: int, **fields: Any) -> bool:
        """A later reflection's new take on an open thought: its text and labels change. It's offered afresh only
        when what she'd say changed, so a thought restated night after night still runs out of offers."""
        allowed = {
            "kind", "thought", "say", "topic", "keywords", "basis", "sources", "domain", "domains", "sensitive",
            "importance", "bring_up", "expires_at", "reflection_id",
        }  # fmt: skip
        values = {k: v for k, v in fields.items() if k in allowed}
        for key in ("keywords", "sources", "domains"):
            if key in values:
                values[key] = dumps(list(values[key]))
        if "sensitive" in values:
            values["sensitive"] = int(values["sensitive"])
        with self.transaction():
            before = self.thought(thought_id)
            if before is None or before.status != OPEN:
                return False
            fresh = "say" in values and " ".join(str(values["say"]).lower().split()) != " ".join(
                before.say.lower().split()
            )
            assignments = ", ".join(f"{k}=?" for k in values)
            self._write(
                f"UPDATE thoughts SET {assignments}, updated_at=?, revisions=revisions+1"
                + (", offered=0, offered_at=NULL" if fresh else "")
                + " WHERE id=?",
                (*values.values(), self.clock(), thought_id),
            )
            return True

    def thought(self, thought_id: int) -> Thought | None:
        row = self._one("SELECT * FROM thoughts WHERE id=?", (thought_id,))
        return _thought(row) if row else None

    def thoughts(self, status: str | Iterable[str] | None = OPEN, limit: int = 50) -> list[Thought]:
        """Newest first; ``status`` None for every one."""
        if status is None:
            rows = self._rows("SELECT * FROM thoughts ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))
        else:
            wanted = [status] if isinstance(status, str) else list(status)
            marks = ",".join("?" * len(wanted))
            rows = self._rows(
                f"SELECT * FROM thoughts WHERE status IN ({marks}) ORDER BY updated_at DESC, id DESC LIMIT ?",
                (*wanted, limit),
            )
        return [_thought(r) for r in rows]

    def close_thought(self, thought_id: int, status: str, reason: str = "") -> Thought | None:
        """An open thought is shared, dismissed, resolved or expired; None if it wasn't open."""
        if status not in STATUSES or status == OPEN:
            raise ValueError(f"not a closing status: {status!r}")
        with self.transaction():
            cur = self._write(
                "UPDATE thoughts SET status=?, closed_at=?, close_reason=?, updated_at=? WHERE id=? AND status=?",
                (status, self.clock(), reason, self.clock(), thought_id, OPEN),
            )
            return self.thought(thought_id) if cur.rowcount else None

    def mark_offered(self, thought_id: int) -> int:
        """Offered in one more conversation; returns how many. One offered ``MAX_OFFERS`` times isn't picked again,
        and ``expire`` lets it go once that last conversation is over."""
        with self.transaction():
            self._write(
                "UPDATE thoughts SET offered=offered+1, offered_at=? WHERE id=? AND status=?",
                (self.clock(), thought_id, OPEN),
            )
            row = self._one("SELECT offered FROM thoughts WHERE id=?", (thought_id,))
            return int(row["offered"]) if row else 0

    def expire(self, now: float | None = None, *, offer_grace_s: float = 3 * 3600) -> int:
        """Let go of open thoughts kept long enough, and of those offered ``MAX_OFFERS`` times (once the last
        conversation they were offered in is surely over)."""
        now = self.clock() if now is None else now
        with self.transaction():
            kept = self._write(
                "UPDATE thoughts SET status='expired', closed_at=?, close_reason='kept long enough', updated_at=? "
                "WHERE status=? AND expires_at IS NOT NULL AND expires_at<=?",
                (now, now, OPEN, now),
            ).rowcount
            offered = self._write(
                "UPDATE thoughts SET status='expired', closed_at=?, close_reason=?, updated_at=? "
                "WHERE status=? AND offered>=? AND offered_at IS NOT NULL AND offered_at<=?",
                (now, OFFERED_OUT, now, OPEN, MAX_OFFERS, now - offer_grace_s),
            ).rowcount
        return kept + offered

    def closed_since(self, since: float, limit: int = 20) -> list[Thought]:
        rows = self._rows(
            "SELECT * FROM thoughts WHERE status<>? AND closed_at>=? ORDER BY closed_at DESC LIMIT ?",
            (OPEN, since, limit),
        )
        return [_thought(r) for r in rows]

    # ------------------------------------------------------------------ opinions
    def opinions(self, *, active: bool = True, limit: int = 30) -> list[Opinion]:
        sql = "SELECT * FROM opinions" + (" WHERE active=1" if active else "") + " ORDER BY created_at DESC, id DESC"
        return [_opinion(r) for r in self._rows(sql + " LIMIT ?", (limit,))]

    def opinion_history(self, topic: str) -> list[Opinion]:
        """What she thought about ``topic`` over time, oldest first (not what she's only leaning toward)."""
        rows = self._rows(
            "SELECT * FROM opinions WHERE lower(topic)=lower(?) AND leaning=0 ORDER BY created_at, id", (topic.strip(),)
        )
        return [_opinion(r) for r in rows]

    def leanings(self, limit: int = 10) -> list[Opinion]:
        """Changed stances she's leaning toward but not settled on, newest first."""
        rows = self._rows("SELECT * FROM opinions WHERE leaning=1 ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        return [_opinion(r) for r in rows]

    def opinion_changes_since(self, since: float) -> list[tuple[Opinion, Opinion]]:
        """(before, now) for every opinion that changed since ``since``, oldest first."""
        rows = self._rows("SELECT * FROM opinions WHERE leaning=0 AND created_at>=? ORDER BY created_at, id", (since,))
        out = []
        for row in rows:
            before = self._one("SELECT * FROM opinions WHERE replaced_by=? AND leaning=0", (row["id"],))
            if before is not None:
                out.append((_opinion(before), _opinion(row)))
        return out

    def lean(
        self,
        reflection_id: int | None,
        topic: str,
        stance: str,
        why: str,
        *,
        now: float | None = None,
        promote: bool = True,
    ) -> str:
        """A night's stance on ``topic``, taken slowly. Returns what happened:

        - "formed": she had no opinion on it, so this is one now;
        - "held": it's the stance she already has (and any leaning away from it is let go);
        - "leaning": it differs from her opinion, so it's a leaning for now (her opinion stays);
        - "changed": she was already leaning the same way (``similar``) since a night at least ``LEAN_MIN_S`` ago and
          ``promote`` (a night she reflected by herself, not one asked for mid-argument), so this stance is her
          opinion now (the old one stays in her history, replaced). A leaning a different way starts over.
        """
        now = self.clock() if now is None else now
        topic = topic.strip()
        with self.transaction():
            current = self._one(
                "SELECT * FROM opinions WHERE lower(topic)=lower(?) AND active=1 ORDER BY id DESC LIMIT 1", (topic,)
            )
            leaning = self._one(
                "SELECT * FROM opinions WHERE lower(topic)=lower(?) AND leaning=1 ORDER BY id DESC LIMIT 1", (topic,)
            )
            if current is None:
                if leaning is not None:
                    self._write("DELETE FROM opinions WHERE id=?", (leaning["id"],))
                self.set_opinion(reflection_id, topic, stance, why)
                return "formed"
            if _same(current["stance"], stance):
                if leaning is not None:
                    self._write("DELETE FROM opinions WHERE id=?", (leaning["id"],))
                return "held"
            same_way = leaning is not None and similar(leaning["stance"], stance)
            if same_way and promote and now - leaning["created_at"] >= LEAN_MIN_S:
                self._write(
                    "UPDATE opinions SET topic=?, stance=?, why=?, reflection_id=?, created_at=?, leaning=0, active=1 "
                    "WHERE id=?",
                    (current["topic"], stance, why, reflection_id, now, leaning["id"]),
                )
                self._write("UPDATE opinions SET active=0, replaced_by=? WHERE id=?", (leaning["id"], current["id"]))
                return "changed"
            if leaning is not None:
                # The same way again too soon keeps its start; a different way is a new leaning, starting now.
                self._write(
                    "UPDATE opinions SET stance=?, why=?, reflection_id=?, created_at=? WHERE id=?",
                    (stance, why, reflection_id, leaning["created_at"] if same_way else now, leaning["id"]),
                )
                return "leaning"
            self._write(
                "INSERT INTO opinions(reflection_id, created_at, topic, stance, why, active, leaning) "
                "VALUES (?,?,?,?,?,0,1)",
                (reflection_id, now, topic, stance, why),
            )
            return "leaning"

    def let_go_of_leanings(self, now: float | None = None) -> int:
        """Leanings no later night took up within ``LEAN_KEEP_S``: passing thoughts, deleted."""
        now = self.clock() if now is None else now
        return self._write("DELETE FROM opinions WHERE leaning=1 AND created_at<=?", (now - LEAN_KEEP_S,)).rowcount

    def set_opinion(self, reflection_id: int | None, topic: str, stance: str, why: str) -> tuple[int, bool]:
        """Her stance on ``topic``: a new opinion, or a changed one (the old one stays, replaced). Returns (id,
        whether it changed an earlier stance); the same stance again changes nothing."""
        topic = topic.strip()
        with self.transaction():
            row = self._one(
                "SELECT * FROM opinions WHERE lower(topic)=lower(?) AND active=1 ORDER BY id DESC LIMIT 1", (topic,)
            )
            if row is not None and _same(row["stance"], stance):
                return int(row["id"]), False
            cur = self._write(
                "INSERT INTO opinions(reflection_id, created_at, topic, stance, why) VALUES (?,?,?,?,?)",
                (reflection_id, self.clock(), topic, stance, why),
            )
            new_id = int(cur.lastrowid or 0)
            if row is not None:
                self._write("UPDATE opinions SET active=0, replaced_by=? WHERE id=?", (new_id, row["id"]))
            return new_id, row is not None

    # ------------------------------------------------------------------ forgetting
    def forget_thought(self, thought_id: int) -> bool:
        return self._write("DELETE FROM thoughts WHERE id=?", (thought_id,)).rowcount > 0

    def forget_matching(self, matches: Callable[[str], bool]) -> int:
        """Delete every thought, opinion and entry whose text ``matches`` says is about it. Returns how many."""
        removed = 0
        with self.transaction():
            for row in self._rows("SELECT id, thought, say, topic FROM thoughts"):
                if matches(" ".join((row["thought"], row["say"], row["topic"]))):
                    removed += self._write("DELETE FROM thoughts WHERE id=?", (row["id"],)).rowcount
            for row in self._rows("SELECT id, topic, stance, why FROM opinions"):
                if matches(" ".join((row["topic"], row["stance"], row["why"]))):
                    removed += self._write("DELETE FROM opinions WHERE id=?", (row["id"],)).rowcount
            for row in self._rows("SELECT id, text FROM entries"):
                if matches(row["text"]):
                    removed += self._write("DELETE FROM entries WHERE id=?", (row["id"],)).rowcount
            for row in self._rows("SELECT id, text, topic FROM experiences"):
                if matches(" ".join((row["text"], row["topic"]))):
                    removed += self._write("DELETE FROM experiences WHERE id=?", (row["id"],)).rowcount
            for row in self._rows("SELECT id, text, about FROM self_notes"):
                if matches(" ".join((row["text"], row["about"]))):
                    removed += self._write("DELETE FROM self_notes WHERE id=?", (row["id"],)).rowcount
            for row in self._rows("SELECT id, why FROM feelings"):
                if row["why"] and matches(row["why"]):
                    removed += self._write("UPDATE feelings SET why='' WHERE id=?", (row["id"],)).rowcount
        self.vacuum()
        return removed

    def reset(self) -> int:
        """Delete everything she wrote (her settings stay, and so do the marks of what she already read, so a fresh
        journal doesn't go back over the days just forgotten). Returns how many rows went."""
        with self.transaction():
            removed = sum(self._write(f"DELETE FROM {table}").rowcount for table in WRITTEN)
            self._write("DELETE FROM reflections")
        self.vacuum()
        return removed

    def vacuum(self) -> None:
        """Deleted text doesn't linger in free pages."""
        if self.path == ":memory:":
            return
        with self._lock:
            try:
                self._conn.execute("VACUUM")
            except sqlite3.OperationalError:
                log.debug("Couldn't vacuum her journal", exc_info=True)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for status in STATUSES:
            row = self._one("SELECT COUNT(*) AS n FROM thoughts WHERE status=?", (status,))
            out[f"thoughts_{status}"] = int(row["n"]) if row else 0
        for table in ("entries", "reflections", "experiences", "feelings"):
            row = self._one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        row = self._one("SELECT COUNT(*) AS n FROM opinions WHERE leaning=0")
        out["opinions"] = int(row["n"]) if row else 0
        row = self._one("SELECT COUNT(*) AS n FROM self_notes WHERE active=1")
        out["self_notes"] = int(row["n"]) if row else 0
        row = self._one("SELECT COUNT(*) AS n FROM opinions WHERE leaning=1")
        out["leanings"] = int(row["n"]) if row else 0
        return out
