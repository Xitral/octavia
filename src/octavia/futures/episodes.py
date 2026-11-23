"""Episodes: the event stream compressed into stretches of activity that mean something, never one per mouse move.

Three kinds, each made by plain rules:

* A **conversation** is everything that happened in one conversation session: the requests, her replies, the
  actions she took on the computer, the to-dos and timers she set. At most one is open at a time; a request in a new
  session closes the last one, and so does a quiet spell (``idle_gap_s``).
* A **build** is one self-improvement build, from starting to installing (or failing, or being rolled back).
* A **focus** stretch is a long spell in one app ("Used Blender for 40 min"), noticed when another app came to the
  front; short visits are left out.

What went wrong in an episode is a *problem* ("computer_click failed (timed_out) in Blender"), what got past it a
*resolution*, and the lesson from a resolved problem a *learned* line. Each problem also adds a few words to the
episode's *signature*, so the next time the same thing fails, ``resolution_for`` finds what worked before and
``similar`` finds episodes like it. Summaries are deterministic (``summary_by``); a model may write nicer ones later.

While an episode is open, its running counts (requests, actions, which tools failed) live in the store's ``kv``
table under ``episodes``; the closed episode keeps only what they add up to (its ``outcome``).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from octavia.futures.events import FuturesEvent
from octavia.futures.privacy import redact
from octavia.futures.schema import Sensitivity, split_ref
from octavia.futures.store import FuturesStore, dumps, loads

log = logging.getLogger(__name__)

CONVERSATION_TYPES = frozenset(
    {
        "user.request",
        "assistant.reply",
        "user.correction",
        "user.feedback",
        "computer.action_performed",
        "computer.action_failed",
        "computer.application_opened",
        "computer.application_closed",
        "task.created",
        "task.updated",
        "task.completed",
        "task.deleted",
        "timer.created",
        "memory.saved",
    }
)
BUILD_ENDS = frozenset({"build.installed", "build.failure", "build.rolled_back"})
MAX_TITLE = 80
BUILD_IDLE_S = 6 * 3600.0  # a build can be quiet for a long while between its steps; this long, it's gone

_STATE = "episodes"  # kv namespace of an open episode's running counts
_KINDS = {  # kind -> (domain, sensitivity it starts at)
    "conversation": ("assistant", Sensitivity.PERSONAL.value),
    "build": ("self", Sensitivity.PUBLIC.value),
    "focus": ("computer", Sensitivity.PERSONAL.value),
}
_SENSITIVITY_ORDER = {Sensitivity.PUBLIC.value: 0, Sensitivity.PERSONAL.value: 1, Sensitivity.SENSITIVE.value: 2}
_WORD = re.compile(r"[\w']+", re.UNICODE)
_CHECKS_FAILED = "the checks failed"
_CHECKS_LEARNED = "When a build's checks failed, another try has passed them."


@dataclass(frozen=True)
class Episode:
    id: int
    thread: str  # "session:<id>", "build:<id>", "focus:<app>"
    kind: str  # "conversation", "build" or "focus"
    title: str
    status: str  # "open" or "closed"
    started_at: float
    ended_at: float | None
    last_at: float
    goal: str | None
    event_ids: tuple[str, ...]
    problems: tuple[str, ...]
    resolutions: tuple[str, ...]
    outcome: str | None
    learned: tuple[str, ...]
    signature: str  # space-separated words naming its problems: "computer_click timed_out blender"
    summary_by: str
    domain: str
    sensitivity: str


def _to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        thread=row["thread"],
        kind=row["kind"],
        title=row["title"],
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        last_at=row["last_at"],
        goal=row["goal"],
        event_ids=tuple(loads(row["event_ids"], [])),
        problems=tuple(loads(row["problems"], [])),
        resolutions=tuple(loads(row["resolutions"], [])),
        outcome=row["outcome"],
        learned=tuple(loads(row["learned"], [])),
        signature=row["signature"],
        summary_by=row["summary_by"],
        domain=row["domain"],
        sensitivity=row["sensitivity"],
    )


@dataclass
class _Draft:
    """An open episode being changed: the row's lists as lists, and its running counts."""

    id: int
    kind: str
    title: str
    status: str
    last_at: float
    ended_at: float | None
    goal: str | None
    event_ids: list[str]
    problems: list[str]
    resolutions: list[str]
    learned: list[str]
    signature: list[str]
    outcome: str | None
    sensitivity: str
    state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, row: sqlite3.Row, state: dict[str, Any]) -> _Draft:
        return cls(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            status=row["status"],
            last_at=row["last_at"],
            ended_at=row["ended_at"],
            goal=row["goal"],
            event_ids=list(loads(row["event_ids"], [])),
            problems=list(loads(row["problems"], [])),
            resolutions=list(loads(row["resolutions"], [])),
            learned=list(loads(row["learned"], [])),
            signature=row["signature"].split(),
            outcome=row["outcome"],
            sensitivity=row["sensitivity"],
            state=state,
        )

    def count(self, name: str) -> int:
        return int(self.state.get(name, 0))

    def bump(self, name: str) -> None:
        self.state[name] = self.count(name) + 1

    def problem(self, line: str, words: Iterable[str]) -> None:
        if line not in self.problems:
            self.problems.append(line)
        for word in words:
            if word and word not in self.signature:
                self.signature.append(word)

    def resolve(self, line: str, learned: str | None = None) -> None:
        if line not in self.resolutions:
            self.resolutions.append(line)
        if learned and learned not in self.learned:
            self.learned.append(learned)


def _token(value: Any) -> str:
    """One signature word: lowercase, the spaces inside a name joined with ``_``."""
    return "_".join(str(value or "").lower().split())


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _fts_query(text: str) -> str | None:
    words = [w.strip("'") for w in _WORD.findall(text.lower())]
    words = [w for w in words if len(w) > 1]
    if not words:
        return None
    # Any word may match, each as a prefix; bm25 ranks by overlap (like memory/sqlite_store.py).
    return " OR ".join(f'"{w}"*' for w in words[:12])


def _covers(line: str, words: Iterable[str]) -> bool:
    """Whether a learned line is about all of ``words`` (a signature word's ``_`` may be a space in the line)."""
    low = line.lower()
    return all(word in low or word.replace("_", " ") in low for word in words)


class Episodes:
    """The episodes table (and its full-text index, ``episodes_fts``)."""

    def __init__(
        self,
        store: FuturesStore,
        *,
        idle_gap_s: float = 900.0,
        focus_min_s: float = 600.0,
        max_events: int = 200,
        build_idle_s: float = BUILD_IDLE_S,
    ) -> None:
        self.store = store
        self.idle_gap_s = idle_gap_s
        self.focus_min_s = focus_min_s
        self.max_events = max_events
        self.build_idle_s = max(build_idle_s, idle_gap_s)

    # ------------------------------------------------------------------ taking in events
    def observe(self, event: FuturesEvent) -> Episode | None:
        """Add ``event`` to the open episode of its thread, starting one when there's none. Returns the episode,
        or None for an event that isn't part of one."""
        ts = event.ts or self.store.clock()
        with self.store.transaction():
            if event.type.startswith("build."):
                return self._build(event, ts)
            if event.type == "computer.context_changed":
                return self._focus(event, ts)
            session = (event.context or {}).get("session")
            if event.type in CONVERSATION_TYPES and session not in (None, ""):
                return self._conversation(event, ts, f"session:{session}")
        return None

    def _conversation(self, event: FuturesEvent, ts: float, thread: str) -> Episode:
        # One conversation at a time: an event from another session means the last one is over.
        for row in self.store.execute(
            "SELECT * FROM episodes WHERE kind='conversation' AND status='open' AND thread<>?", (thread,)
        ):
            self._close(self._load(row), self._end_time(row["kind"], row["last_at"], ts))
        draft = self._current(thread, ts) or self._start(thread, "conversation", "Conversation", ts)
        if not self._add(draft, event, ts):
            return self._saved(draft)
        data = event.data or {}
        if event.type == "user.request":
            draft.bump("requests")
            text = " ".join(str(data.get("text") or "").split())
            if text and not draft.state.get("titled"):
                draft.title = redact(f"Asked: {text}", MAX_TITLE)
                draft.state["titled"] = True
            if draft.state.pop("correction_pending", False):
                draft.resolve("the user asked again")
        elif event.type == "user.correction":
            draft.bump("corrections")
            draft.problem("the user corrected her", ("correction",))
            draft.state["correction_pending"] = True
        elif event.type == "assistant.reply":
            if event.outcome == "failure":
                draft.problem("a reply failed", ("reply", "failure"))
        elif event.type == "computer.action_failed":
            draft.bump("actions")
            draft.bump("failed")
            tool = str(data.get("tool") or _key(event.subject) or "a tool")
            failure = str(data.get("failure") or "error")
            app = str(data.get("app") or "")
            draft.problem(
                f"{tool} failed ({failure})" + (f" in {app}" if app else ""), map(_token, (tool, failure, app))
            )
            failed = dict(draft.state.get("failed_tools", {}))
            failed[tool] = [failure, app]
            draft.state["failed_tools"] = failed
        elif event.type == "computer.action_performed":
            draft.bump("actions")
            tool = str(data.get("tool") or _key(event.subject) or "a tool")
            failed = dict(draft.state.get("failed_tools", {}))
            if tool in failed:
                failure, app = failed.pop(tool)
                draft.state["failed_tools"] = failed
                where = f" in {app}" if app else ""
                draft.resolve(
                    f"{tool} worked on another try",
                    f"When {tool} fails ({failure}){where}, trying it again (differently) has worked.",
                )
        return self._saved(draft)

    def _build(self, event: FuturesEvent, ts: float) -> Episode | None:
        kind, key = split_ref(event.subject or "")
        if kind != "improvement" or not key.strip():
            return None
        thread = f"build:{key.strip()}"
        data = event.data or {}
        title = " ".join(str(data.get("title") or "").split())
        draft = self._current(thread, ts)
        if draft is None and event.type == "build.rolled_back":
            # Undoing an installed improvement belongs to the build that installed it, closed as it is.
            row = self.store.one(
                "SELECT * FROM episodes WHERE thread=? ORDER BY started_at DESC, id DESC LIMIT 1", (thread,)
            )
            if row is not None:
                draft = self._load(row)
                if not self._add(draft, event, ts):
                    return self._saved(draft)
                draft.problem("it was rolled back", ("build", "rolled_back"))
                draft.outcome = _status_word(event)
                draft.ended_at = ts
                return self._saved(draft)
        if draft is None:
            draft = self._start(thread, "build", redact(f"Built: {title}" if title else "A build", MAX_TITLE), ts)
            draft.state["titled"] = bool(title)
        if not self._add(draft, event, ts):
            return self._saved(draft)
        if title and not draft.state.get("titled"):
            draft.title = redact(f"Built: {title}", MAX_TITLE)
            draft.state["titled"] = True
        if event.type == "build.test_failed":
            draft.problem(_CHECKS_FAILED, ("build", "checks_failed"))
            draft.state["checks_failed"] = True
        elif event.type == "build.test_passed" and draft.state.pop("checks_failed", False):
            draft.resolve("passed its checks on another try", _CHECKS_LEARNED)
        elif event.type == "build.rolled_back":
            draft.problem("it was rolled back", ("build", "rolled_back"))
        if event.type in BUILD_ENDS:
            draft.outcome = _status_word(event)
            return self._close(draft, ts)
        return self._saved(draft)

    def _focus(self, event: FuturesEvent, ts: float) -> Episode | None:
        data = event.data or {}
        previous = " ".join(str(data.get("previous") or "").split())
        spent = data.get("previous_s")
        if not previous or not isinstance(spent, int | float) or spent < self.focus_min_s:
            return None
        thread = f"focus:{previous.lower()}"
        for row in self.store.execute("SELECT * FROM episodes WHERE thread=? AND ended_at=?", (thread, ts)):
            if event.id and event.id in loads(row["event_ids"], []):
                return _to_episode(row)  # seen already
        domain, sensitivity = _KINDS["focus"]
        episode_id = self.store.insert(
            "INSERT INTO episodes(thread, kind, title, status, started_at, ended_at, last_at, goal, event_ids, "
            "domain, sensitivity) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                thread,
                "focus",
                redact(f"Used {previous} for {round(spent / 60)} min", MAX_TITLE),
                "closed",
                ts - spent,
                ts,
                ts,
                event.goal,
                dumps([event.id] if event.id else []),
                domain,
                _more_sensitive(sensitivity, event.sensitivity),
            ),
        )
        return self.get(episode_id)

    # ------------------------------------------------------------------ the open episode
    def _current(self, thread: str, ts: float) -> _Draft | None:
        """The thread's open episode, unless it went quiet before ``ts`` (then it's closed, and there's none)."""
        row = self.store.one(
            "SELECT * FROM episodes WHERE thread=? AND status='open' ORDER BY started_at DESC, id DESC LIMIT 1",
            (thread,),
        )
        if row is None:
            return None
        draft = self._load(row)
        if ts - draft.last_at >= self._gap(draft.kind):
            self._close(draft, draft.last_at)
            return None
        return draft

    def _start(self, thread: str, kind: str, title: str, ts: float) -> _Draft:
        domain, sensitivity = _KINDS[kind]
        episode_id = self.store.insert(
            "INSERT INTO episodes(thread, kind, title, status, started_at, last_at, domain, sensitivity) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (thread, kind, title, "open", ts, ts, domain, sensitivity),
        )
        row = self.store.one("SELECT * FROM episodes WHERE id=?", (episode_id,))
        assert row is not None
        return _Draft.load(row, {})

    def _load(self, row: sqlite3.Row) -> _Draft:
        return _Draft.load(row, dict(self.store.kv_get(_STATE, str(row["id"]), {}) or {}))

    def _add(self, draft: _Draft, event: FuturesEvent, ts: float) -> bool:
        """Count ``event`` into ``draft``; False when it's already in there."""
        if event.id and event.id in draft.event_ids:
            return False
        draft.bump("events")
        if event.id and len(draft.event_ids) < self.max_events:
            draft.event_ids.append(event.id)
        draft.last_at = max(draft.last_at, ts)
        draft.sensitivity = _more_sensitive(draft.sensitivity, event.sensitivity)
        if draft.goal is None and event.goal:
            draft.goal = event.goal
        return True

    def _saved(self, draft: _Draft) -> Episode:
        self.store.execute(
            "UPDATE episodes SET title=?, status=?, ended_at=?, last_at=?, goal=?, event_ids=?, problems=?, "
            "resolutions=?, outcome=?, learned=?, signature=?, sensitivity=? WHERE id=?",
            (
                draft.title,
                draft.status,
                draft.ended_at,
                draft.last_at,
                draft.goal,
                dumps(draft.event_ids),
                dumps(draft.problems),
                dumps(draft.resolutions),
                draft.outcome,
                dumps(draft.learned),
                " ".join(draft.signature),
                draft.sensitivity,
                draft.id,
            ),
        )
        if draft.status == "open":
            self.store.kv_set(_STATE, str(draft.id), draft.state)
        else:
            self.store.kv_delete(_STATE, str(draft.id))
        episode = self.get(draft.id)
        assert episode is not None
        return episode

    def _close(self, draft: _Draft, ended_at: float) -> Episode:
        draft.status = "closed"
        draft.ended_at = max(ended_at, draft.last_at)
        if draft.kind == "conversation":
            draft.outcome = _conversation_outcome(draft)
        log.debug("episode %d (%s) closed: %s", draft.id, draft.kind, draft.outcome)
        return self._saved(draft)

    def _gap(self, kind: str) -> float:
        return self.build_idle_s if kind == "build" else self.idle_gap_s

    def _end_time(self, kind: str, last_at: float, now: float) -> float:
        """When an episode closed at ``now`` ended: now, unless it had already gone quiet (then its last event)."""
        return now if now - last_at < self._gap(kind) else last_at

    # ------------------------------------------------------------------ closing
    def close_idle(self, now: float) -> list[Episode]:
        """Close the open episodes that have gone quiet (nothing for ``idle_gap_s``; a build gets longer). Each
        ends at its last event."""
        closed = []
        with self.store.transaction():
            for row in self.store.execute("SELECT * FROM episodes WHERE status='open' ORDER BY started_at, id"):
                if now - row["last_at"] >= self._gap(row["kind"]):
                    closed.append(self._close(self._load(row), row["last_at"]))
        return closed

    def close_thread(self, thread: str, now: float) -> Episode | None:
        """Close the thread's open episode (the conversation was dismissed, say). None when none is open."""
        with self.store.transaction():
            row = self.store.one(
                "SELECT * FROM episodes WHERE thread=? AND status='open' ORDER BY started_at DESC, id DESC LIMIT 1",
                (thread,),
            )
            if row is None:
                return None
            return self._close(self._load(row), self._end_time(row["kind"], row["last_at"], now))

    def close_all(self, now: float) -> list[Episode]:
        """Close every open episode (Octavia is stopping)."""
        with self.store.transaction():
            rows = self.store.execute("SELECT * FROM episodes WHERE status='open' ORDER BY started_at, id")
            return [self._close(self._load(r), self._end_time(r["kind"], r["last_at"], now)) for r in rows]

    # ------------------------------------------------------------------ reading
    def get(self, episode_id: int) -> Episode | None:
        row = self.store.one("SELECT * FROM episodes WHERE id=?", (episode_id,))
        return _to_episode(row) if row else None

    def recent(self, *, kind: str | None = None, limit: int = 10) -> list[Episode]:
        """Episodes newest started first."""
        where, params = ("WHERE kind=?", [kind]) if kind is not None else ("", [])
        rows = self.store.execute(
            f"SELECT * FROM episodes {where} ORDER BY started_at DESC, id DESC LIMIT ?", (*params, limit)
        )
        return [_to_episode(r) for r in rows]

    def open(self) -> list[Episode]:
        """The episodes still going, oldest started first."""
        rows = self.store.execute("SELECT * FROM episodes WHERE status='open' ORDER BY started_at, id")
        return [_to_episode(r) for r in rows]

    def similar(self, text: str, *, limit: int = 3, closed_only: bool = True) -> list[Episode]:
        """Episodes whose title or signature share words with ``text`` (any word, as a prefix), best match first."""
        query = _fts_query(text)
        if query is None:
            return []
        closed = " AND e.status='closed'" if closed_only else ""
        try:
            rows = self.store.execute(
                "SELECT e.* FROM episodes_fts f JOIN episodes e ON e.id = f.rowid "
                f"WHERE episodes_fts MATCH ?{closed} ORDER BY bm25(episodes_fts), e.id DESC LIMIT ?",
                (query, limit),
            )
        except sqlite3.OperationalError as exc:  # a query FTS can't parse finds nothing rather than failing
            log.debug("episode search %r failed: %s", query, exc)
            return []
        return [_to_episode(r) for r in rows]

    def resolution_for(self, signature: str) -> str | None:
        """What worked the last time: the newest learned line, from a closed episode whose signature has every word
        of ``signature``, that is about all those words."""
        words = list(dict.fromkeys(signature.lower().split()))
        if not words:
            return None
        rows = self.store.execute(
            "SELECT * FROM episodes WHERE status='closed' AND learned<>'[]' AND signature LIKE ? "
            "ORDER BY ended_at DESC, id DESC LIMIT 50",
            (f"%{words[0]}%",),
        )
        for row in rows:
            if not set(words) <= set(row["signature"].split()):
                continue
            for line in reversed(loads(row["learned"], [])):
                if _covers(line, words):
                    return line
        return None

    # ------------------------------------------------------------------ forgetting
    def forget(self, episode_id: int) -> bool:
        with self.store.transaction():
            self.store.kv_delete(_STATE, str(episode_id))
            return self.store.changed("DELETE FROM episodes WHERE id=?", (episode_id,)) > 0

    def delete_all(self) -> int:
        with self.store.transaction():
            self.store.changed("DELETE FROM kv WHERE namespace=?", (_STATE,))
            return self.store.changed("DELETE FROM episodes")


def _key(subject: str | None) -> str:
    return split_ref(subject)[1] if subject else ""


def _status_word(event: FuturesEvent) -> str:
    """How a build ended, in one word: its status when the event says, else the event's own ("installed")."""
    status = (event.data or {}).get("status")
    return status if isinstance(status, str) and status else event.type.partition(".")[2]


def _more_sensitive(current: str, other: str | None) -> str:
    if other in _SENSITIVITY_ORDER and _SENSITIVITY_ORDER[other] > _SENSITIVITY_ORDER.get(current, 1):
        return str(other)
    return current


def _conversation_outcome(draft: _Draft) -> str | None:
    """ "3 requests, 5 actions (1 failed), corrected once", leaving out what didn't happen."""
    parts = []
    if requests := draft.count("requests"):
        parts.append(_plural(requests, "request"))
    if actions := draft.count("actions"):
        failed = draft.count("failed")
        parts.append(_plural(actions, "action") + (f" ({failed} failed)" if failed else ""))
    if corrections := draft.count("corrections"):
        parts.append(f"corrected {_times(corrections)}")
    return ", ".join(parts) or None
