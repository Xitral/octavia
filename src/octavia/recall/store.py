"""Screen history's database: one SQLite file (``screen_history.database``, default
``data/recall/screen_history.db``), on this PC only.

Two tables, kept apart on purpose:

* ``activity``: spans of time one window was in front ("Chrome, 'Enzyme kinetics - Canvas', canvas.example.edu,
  14:02 to 14:31"): the app, its process, the window's title and the page's address (scheme, host and path only).
  It says what the user was doing when, so a later feature that learns from what the user does can read it as is.
* ``snippets``: the text that was on screen, an FTS5 full-text table. Each row is the lines that were new in one
  look at a window (what was already kept for the same page isn't kept again), tied to its ``activity`` span.

Settings the user changes by voice (paused until when, the apps and sites they excluded) are in ``settings``.

Migrations like Octavia Futures' store: ``MIGRATIONS`` in order, ``PRAGMA user_version`` says how many have run. The
database uses incremental auto-vacuum, so forgetting things gives the space back without rewriting the whole file.

Threading: one connection behind a lock, used from the service's one database thread; nothing here touches the
event loop.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from octavia.recall import privacy

log = logging.getLogger(__name__)


class StoreError(RuntimeError):
    """The database can't be used (written by a newer version, or not a database)."""


MIGRATIONS: tuple[str, ...] = (
    # 1: the first schema
    """
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY,
        started REAL NOT NULL,
        ended REAL NOT NULL,
        app TEXT NOT NULL,
        process TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        host TEXT NOT NULL DEFAULT '',
        fullscreen INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX activity_by_time ON activity(started);
    CREATE INDEX activity_by_end ON activity(ended);
    CREATE INDEX activity_by_process ON activity(process, started);
    CREATE INDEX activity_by_host ON activity(host, started);

    CREATE VIRTUAL TABLE snippets USING fts5(
        text, title, app, url,
        activity_id UNINDEXED, at UNINDEXED, source UNINDEXED,
        tokenize = 'porter unicode61 remove_diacritics 2'
    );

    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
)


@dataclass(frozen=True)
class Span:
    id: int
    started: float
    ended: float
    app: str
    process: str
    title: str
    url: str


@dataclass(frozen=True)
class Hit:
    at: float
    app: str
    title: str
    url: str
    text: str  # the matching part, with the words found in [brackets]


_TOKEN = re.compile(r"\w+", re.UNICODE)


def fts_query(text: str) -> str:
    """The user's words as an FTS5 query that can't be a syntax error: every word quoted, all of them required, the
    last one as a prefix ("enzym" finds "enzymes")."""
    words = _TOKEN.findall(text or "")
    if not words:
        return ""
    quoted = [f'"{w}"' for w in words]
    quoted[-1] += "*"
    return " ".join(quoted)


class ScreenHistoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ connection

    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            if str(self.path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            try:
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL")  # takes effect on a new database only
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA secure_delete = ON")  # what's forgotten is overwritten, not just unlinked
                self._migrate(conn)
            except sqlite3.DatabaseError as exc:
                conn.close()
                raise StoreError(f"{self.path} can't be used as screen history: {exc}") from exc
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise StoreError(f"written by a newer version (schema {version}, this one knows {len(MIGRATIONS)})")
        for number in range(version, len(MIGRATIONS)):
            conn.executescript(f"BEGIN;\n{MIGRATIONS[number]}\nPRAGMA user_version = {number + 1};\nCOMMIT;")

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

    # ------------------------------------------------------------------ settings

    def setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # ------------------------------------------------------------------ writing

    def open_span(
        self, at: float, app: str, process: str, title: str, url: str, host: str, fullscreen: bool = False
    ) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO activity(started, ended, app, process, title, url, host, fullscreen)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (at, at, app, process, title, url, host, int(fullscreen)),
            )
            return int(cur.lastrowid or 0)

    def extend_span(self, span_id: int, at: float) -> None:
        with self._lock:
            self.conn.execute("UPDATE activity SET ended = MAX(ended, ?) WHERE id = ?", (at, span_id))

    def add_snippet(self, span_id: int, at: float, text: str, title: str, app: str, url: str, source: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO snippets(text, title, app, url, activity_id, at, source) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (text, title, app, url, span_id, at, source),
            )

    # ------------------------------------------------------------------ reading

    def search(
        self,
        query: str,
        since: float | None = None,
        until: float | None = None,
        app: str | None = None,
        limit: int = 8,
    ) -> list[Hit]:
        """Snippets matching ``query`` (all its words), best first, within the time span and app or site given."""
        match = fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT at, app, title, url, snippet(snippets, 0, '[', ']', ' … ', 24) FROM snippets WHERE snippets MATCH ?"
        )
        params: list[Any] = [match]
        if since is not None:
            sql += " AND CAST(at AS REAL) >= ?"
            params.append(since)
        if until is not None:
            sql += " AND CAST(at AS REAL) < ?"
            params.append(until)
        if app:
            sql += f" AND (app {_LIKE} OR url {_LIKE} OR title {_LIKE})"
            like = f"%{_like(app)}%"
            params += [like, like, like]
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self._lock:
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                log.debug("Screen history search failed for %r", query, exc_info=True)
                return []
        return [Hit(float(r[0]), r[1], r[2], r[3], r[4]) for r in rows]

    def spans(
        self, since: float | None = None, until: float | None = None, app: str | None = None, limit: int = 30
    ) -> list[Span]:
        """Windows that were in front between ``since`` and ``until`` (overlapping), newest first."""
        sql = "SELECT id, started, ended, app, process, title, url FROM activity WHERE 1"
        params: list[Any] = []
        if since is not None:
            sql += " AND ended >= ?"
            params.append(since)
        if until is not None:
            sql += " AND started < ?"
            params.append(until)
        if app:
            like = f"%{_like(app)}%"
            sql += f" AND (app {_LIKE} OR process {_LIKE} OR host {_LIKE} OR title {_LIKE})"
            params += [like, like, like, like]
        sql += " ORDER BY started DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [Span(*row) for row in rows]

    def text_of_span(self, span_id: int, limit_chars: int = 1500) -> str:
        with self._lock:
            rows = self.conn.execute(
                "SELECT text FROM snippets WHERE activity_id = ? ORDER BY CAST(at AS REAL)", (span_id,)
            ).fetchall()
        text = "\n".join(r[0] for r in rows)
        return text if len(text) <= limit_chars else text[:limit_chars].rstrip() + "…"

    def day(self, since: float, until: float, chars_each: int = 900) -> list[tuple[Span, str]]:
        """Every window in front between ``since`` and ``until``, oldest first, with the text read of it in that time
        (at most ``chars_each`` characters of each): what the filmstrip shows."""
        with self._lock:
            spans = self.conn.execute(
                "SELECT id, started, ended, app, process, title, url FROM activity"
                " WHERE ended >= ? AND started < ? ORDER BY started LIMIT 5000",
                (since, until),
            ).fetchall()
            rows = self.conn.execute(
                "SELECT activity_id, text FROM snippets WHERE CAST(at AS REAL) >= ? AND CAST(at AS REAL) < ?"
                " ORDER BY CAST(at AS REAL)",
                (since, until),
            ).fetchall()
        texts: dict[int, str] = {}
        for span_id, text in rows:
            before = texts.get(int(span_id), "")
            if len(before) < chars_each:
                texts[int(span_id)] = f"{before}\n{text}" if before else text
        return [(Span(*row), texts.get(row[0], "")[:chars_each]) for row in spans]

    def next_window(self, at: float, *, later: bool) -> float | None:
        """When the nearest window kept after (``later``) or before ``at`` was opened; None when there is none."""
        sql = "SELECT MIN(started) FROM activity WHERE started >= ?" if later else (
            "SELECT MAX(started) FROM activity WHERE started < ?"
        )  # fmt: skip
        with self._lock:
            found = self.conn.execute(sql, (at,)).fetchone()[0]
        return None if found is None else float(found)

    def counts(self) -> dict[str, Any]:
        with self._lock:
            spans = self.conn.execute("SELECT COUNT(*), MIN(started) FROM activity").fetchone()
            snippets = self.conn.execute("SELECT COUNT(*) FROM snippets").fetchone()[0]
        return {"spans": spans[0], "since": spans[1], "snippets": snippets, "bytes": self.size_bytes()}

    def size_bytes(self) -> int:
        with self._lock:
            pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
            free = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
            size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        return int((pages - free) * size)

    # ------------------------------------------------------------------ forgetting

    def forget_since(self, since: float) -> int:
        """Delete everything seen at or after ``since``: the text read since then and the windows opened since
        then. A window that was already open before keeps what was read of it before, and ends at ``since``.
        Returns how many windows were touched."""
        with self.transaction() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM activity WHERE started >= ?", (since,))]
            cut = conn.execute("UPDATE activity SET ended = ? WHERE started < ? AND ended >= ?", (since, since, since))
            conn.execute("DELETE FROM snippets WHERE CAST(at AS REAL) >= ?", (since,))
            self._delete_spans(conn, ids)
        self._reclaim(orphans=True)
        return len(ids) + cut.rowcount

    def forget_matching(self, app_or_site: str) -> int:
        """Delete every span (and its text) of an app or a site. A site with a dot ("x.com") matches that host and
        its subdomains only; a name ("discord") matches an app whose process or name contains it, and any host with
        that name as one of its labels (discord.com, my.discord.gg). Returns how many spans went."""
        value = app_or_site.strip().lower()
        if not value:
            return 0
        if "." in value and not value.endswith(".exe"):
            value = privacy.clean_site(value)  # "https://www.discord.com/x" -> "discord.com", as hosts are stored
            sql = f"SELECT id FROM activity WHERE host = ? OR host {_LIKE}"
            params: tuple[Any, ...] = (value, f"%.{_like(value)}")
        else:
            like = f"%{_like(value.removesuffix('.exe'))}%"
            word = _like(value)
            sql = (
                f"SELECT id FROM activity WHERE lower(process) {_LIKE} OR lower(app) {_LIKE} OR host {_LIKE}"
                f" OR host {_LIKE} OR host {_LIKE} OR host = ?"
            )
            params = (like, like, f"{word}.%", f"%.{word}.%", f"%.{word}", value)
        with self.transaction() as conn:
            ids = [r[0] for r in conn.execute(sql, params)]
            self._delete_spans(conn, ids)
        self._reclaim(orphans=True)
        return len(ids)

    def forget_span(self, span_id: int) -> None:
        with self.transaction() as conn:
            self._delete_spans(conn, [span_id])
        self._reclaim(orphans=True)

    def forget_all(self) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM snippets")
            conn.execute("DELETE FROM activity")
        with self._lock:
            self.conn.execute("INSERT INTO snippets(snippets) VALUES('rebuild')")
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def prune(self, keep_days: float, max_bytes: int, now: float | None = None, keep: int | None = None) -> int:
        """Delete what's older than ``keep_days``, then the oldest days until the database is under ``max_bytes``,
        never the span ``keep`` (the window in front; its old text can go). Returns how many spans went. Routine
        upkeep: the space is handed back, and the search index is merged a step at a time rather than rewritten."""
        now = time.time() if now is None else now
        gone = self._forget_before(now - keep_days * 86400, keep=keep)
        for _ in range(400):  # a day at a time; bounded however the clock or the sizes behave
            if self.size_bytes() <= max_bytes:
                break
            with self._lock:
                oldest = self.conn.execute("SELECT MIN(started) FROM activity").fetchone()[0]
            if oldest is None:
                break
            gone += self._forget_before(float(oldest) + 86400, by_start=True, keep=keep)
        if gone:
            with self._lock:
                # A bounded step of merging, which drops deleted entries from the segments it merges: over the
                # hourly prunes the expired words leave the index without one long rewrite.
                self.conn.execute("INSERT INTO snippets(snippets, rank) VALUES('merge', 500)")
                self.conn.execute("PRAGMA incremental_vacuum")
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return gone

    def _forget_before(self, cutoff: float, by_start: bool = False, keep: int | None = None) -> int:
        """Delete the text read before ``cutoff`` and the spans that ended before it (or, ``by_start``, began
        before it: what the size cap uses, so every round deletes something)."""
        column = "started" if by_start else "ended"
        with self.transaction() as conn:
            ids = [
                r[0]
                for r in conn.execute(f"SELECT id FROM activity WHERE {column} < ? AND id IS NOT ?", (cutoff, keep))
            ]
            conn.execute("DELETE FROM snippets WHERE CAST(at AS REAL) < ?", (cutoff,))
            self._delete_spans(conn, ids)
        return len(ids)

    @staticmethod
    def _delete_spans(conn: sqlite3.Connection, ids: list[int]) -> None:
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM snippets WHERE activity_id IN ({marks})", chunk)
            conn.execute(f"DELETE FROM activity WHERE id IN ({marks})", chunk)

    def _reclaim(self, orphans: bool = False) -> None:
        """After the user forgets something: text whose window is gone goes too (``orphans``), FTS5's index is
        rewritten so the deleted words aren't left in its pages, freed pages are overwritten (secure_delete) and
        handed back, and the write-ahead log, which still holds the old pages, is emptied."""
        with self._lock:
            if orphans:
                self.conn.execute("DELETE FROM snippets WHERE activity_id NOT IN (SELECT id FROM activity)")
            self.conn.execute("INSERT INTO snippets(snippets) VALUES('optimize')")
            self.conn.execute("PRAGMA incremental_vacuum")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


_LIKE = "LIKE ? ESCAPE '\\'"  # with ``_like``: the user's words match literally, % and _ included


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
