"""Prospective memory: "remind me when I next open Blender", "tell me when an email from Sam arrives".

A reminder at a time is a timer or a TickTick task; this is the other kind, the one that waits for something to
happen. Each ``Trigger`` is a condition on the event stream plus what the user wanted to hear, kept in the Futures
database so it survives a restart. Conditions are matched against normalized events by plain word matching, never
by asking a model: "Blender" matches an app called "Blender" or a process called ``blender.exe``, and nothing else.

Only the condition's kind and value are stored; the event types and fields it looks at are rebuilt from
``CONDITION_KINDS`` when a trigger is loaded, so a trigger set before an update still works with the update's
definition of its kind. A trigger whose kind no longer exists is kept (the user can still see and cancel it) but
never matches.

Whether and when a firing is actually said is not decided here: the service hands it to the proactive gate as an
INFORM candidate, which (the user asked for it) gets through even with proactive remarks switched off.

Baseline events (``data.baseline``: the first look at a source, reporting everything already there as new) never
match: a "tell me when a new assignment shows up" must not fire for the whole existing planner at once.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from octavia.futures.events import FuturesEvent
from octavia.futures.privacy import redact
from octavia.futures.store import FuturesStore, dumps, loads

log = logging.getLogger(__name__)

MAX_VALUE = 100  # a condition's value is a name or a few words, not a paragraph
MAX_MESSAGE = 300

# kind: (the event types it listens to, the event fields its value is looked for in, how it's described without a
# value (with "{value}" where the value goes when one is needed), whether the value may be empty)
CONDITION_KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str, bool]] = {
    "app_opened": (
        ("computer.application_opened", "computer.context_changed"),
        ("data.app", "data.process"),
        "when you next open {value}",
        False,
    ),
    "email_from": (("gmail.message_received",), ("data.sender",), "when an email from {value} arrives", False),
    "email_about": (("gmail.message_received",), ("transient.subject",), "when an email about {value} arrives", False),
    "build_succeeded": (
        ("build.test_passed", "build.installed"),
        ("data.title",),
        "when a build passes its checks",
        True,
    ),
    "build_failed": (("build.failure", "build.test_failed"), ("data.title",), "when a build fails", True),
    "task_completed": (("task.completed",), ("data.title",), "when you tick off {value}", False),
    "timer_done": (("timer.completed",), ("data.label",), "when the timer rings", True),
    "assignment_posted": (
        ("canvas.assignment_created",),
        ("data.title", "data.course"),
        "when a new assignment shows up",
        True,
    ),
    "grade_posted": (("canvas.grade_posted",), ("data.course", "data.title"), "when a grade is posted", True),
}

# How the kinds whose value is optional are described when they have one.
_WITH_VALUE: dict[str, str] = {
    "build_succeeded": "when a build for {value} passes its checks",
    "build_failed": "when a build for {value} fails",
    "timer_done": "when the {value} timer rings",
    "assignment_posted": "when a new {value} assignment shows up",
    "grade_posted": "when a grade for {value} is posted",
}

STATUSES = ("armed", "fired", "cancelled", "expired")

_NOT_WORD = re.compile(r"[^\w]+")
_warned: set[str] = set()  # kinds already reported as gone, so a missing kind is logged once, not on every look


def words(text: object) -> list[str]:
    """``text`` as the words it's matched by: lowercased, accents and punctuation dropped (``"blender.exe"`` is
    ``["blender", "exe"]``)."""
    if text is None:
        return []
    folded = unicodedata.normalize("NFKD", str(text).casefold())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return [w for w in _NOT_WORD.sub(" ", plain.replace("_", " ")).split() if w]


@dataclass(frozen=True)
class Condition:
    """What a trigger waits for: events of ``event_types`` whose ``fields`` (dotted paths into the event) mention
    every word of ``value`` (any such event when ``value`` is empty)."""

    kind: str
    value: str
    event_types: tuple[str, ...]
    fields: tuple[str, ...]

    @property
    def description(self) -> str:
        """How she says it: "when you next open Blender"."""
        return describe(self.kind, self.value)


def describe(kind: str, value: str = "") -> str:
    """The plain-words description of a condition: ``describe("email_from", "Sam")`` is "when an email from Sam
    arrives"."""
    spec = CONDITION_KINDS.get(kind)
    if spec is None:
        return f"when {kind.replace('_', ' ')} happens" + (f" ({value})" if value else "")
    template = _WITH_VALUE.get(kind, spec[2]) if value else spec[2]
    return template.format(value=value) if "{value}" in template else template


def condition(kind: str, value: str = "") -> Condition:
    """The condition of ``kind`` looking for ``value``. A ValueError for an unknown kind, or for a kind that needs
    something to look for (an app, a sender) given nothing."""
    spec = CONDITION_KINDS.get(kind)
    if spec is None:
        known = ", ".join(sorted(CONDITION_KINDS))
        raise ValueError(f"unknown kind of condition {kind!r}; the kinds are {known}")
    event_types, fields, _, may_be_empty = spec
    value = " ".join(str(value or "").split())[:MAX_VALUE].strip()
    if not words(value):
        if not may_be_empty:
            raise ValueError(f"a {kind.replace('_', ' ')} condition needs something to look for")
        value = ""
    return Condition(kind=kind, value=value, event_types=event_types, fields=fields)


def matches(cond: Condition, event: FuturesEvent) -> bool:
    """Whether ``event`` is what ``cond`` waits for. Every word of the value must appear as a whole word somewhere
    in the condition's fields (a course's name in one field and the assignment's in the other still match "physics
    lab"). A baseline event never matches."""
    if event.type not in cond.event_types or event.get("data.baseline") is True:
        return False
    wanted = words(cond.value)
    if not wanted:
        return True
    seen: set[str] = set()
    for path in cond.fields:
        seen.update(words(event.get(path)))
    return all(w in seen for w in wanted)


@dataclass(frozen=True)
class Trigger:
    """One "remind me when...". ``once`` triggers fire a single time; the others stay armed and count their
    firings. ``source`` is who set it ("user", or the part of Octavia that did)."""

    id: int
    kind: str
    value: str
    condition: Condition
    description: str
    message: str
    status: str  # "armed" | "fired" | "cancelled" | "expired"
    once: bool
    created_at: float
    expires_at: float | None
    fired_at: float | None
    fired_event: str | None
    fire_count: int
    source: str

    @property
    def armed(self) -> bool:
        return self.status == "armed"


def reminder_text(trigger: Trigger, happened: str = "") -> str:
    """What she says when ``trigger`` fires: "You asked me to remind you when you next open Blender: save the
    scene first." One that fires every time first says what happened this time (``happened``: "grade posted:
    Project 2"): its message was written once, for the first time, and alone it named a paper test long graded."""
    what = trigger.description
    lead = f"{happened[:1].upper()}{happened[1:].rstrip('. ')}. " if happened and not trigger.once else ""
    if trigger.message:
        return f"{lead}You asked me to remind you {what}: {trigger.message.rstrip()}"
    return f"{lead}You asked me to tell you {what}, and it just happened."


def _load_condition(kind: str, raw: str) -> Condition:
    stored = loads(raw, {}) or {}
    value = str(stored.get("value", "")) if isinstance(stored, dict) else ""
    kind = str(stored.get("kind", kind)) if isinstance(stored, dict) else kind
    try:
        return condition(kind, value)
    except ValueError:
        # The kind was removed (or changed to need a value) since this was set: keep it visible, matching nothing.
        if kind not in _warned:
            _warned.add(kind)
            log.warning("Futures trigger condition %r no longer exists; it won't fire", kind)
        return Condition(kind=kind, value=value, event_types=(), fields=())


def _to_trigger(row: sqlite3.Row) -> Trigger:
    cond = _load_condition(row["kind"], row["condition"])
    return Trigger(
        id=row["id"],
        kind=row["kind"],
        value=row["value"],
        condition=cond,
        description=row["description"],
        message=row["message"],
        status=row["status"],
        once=bool(row["once"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        fired_at=row["fired_at"],
        fired_event=row["fired_event"],
        fire_count=row["fire_count"],
        source=row["source"],
    )


class Triggers:
    """The triggers table."""

    def __init__(self, store: FuturesStore) -> None:
        self.store = store

    def add(
        self,
        kind: str,
        value: str,
        message: str,
        *,
        once: bool = True,
        expires_at: float | None = None,
        source: str = "user",
        cause: str | None = None,
    ) -> Trigger:
        """Arm a new trigger. A ValueError when the condition isn't one she can watch for (see ``condition``)."""
        cond = condition(kind, value)
        now = self.store.clock()
        with self.store.transaction():
            trigger_id = self.store.insert(
                "INSERT INTO triggers(kind, value, condition, description, message, status, once, created_at, "
                "expires_at, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    cond.kind,
                    cond.value,
                    dumps({"kind": cond.kind, "value": cond.value}),
                    cond.description,
                    redact(message, MAX_MESSAGE),
                    "armed",
                    int(once),
                    now,
                    expires_at,
                    source,
                ),
            )
            self.store.record_change(
                f"trigger:{trigger_id}",
                "created",
                cause=cause,
                detail={"kind": cond.kind, "value": cond.value, "once": once, "expires_at": expires_at},
            )
        trigger = self.get(trigger_id)
        assert trigger is not None
        return trigger

    def armed(self) -> list[Trigger]:
        """Every armed trigger, oldest first."""
        rows = self.store.execute("SELECT * FROM triggers WHERE status='armed' ORDER BY created_at, id")
        return [_to_trigger(r) for r in rows]

    def match(self, event: FuturesEvent) -> list[Trigger]:
        """The armed triggers ``event`` sets off (not those that had expired by the time it happened)."""
        at = event.ts or self.store.clock()
        return [t for t in self.armed() if (t.expires_at is None or at < t.expires_at) and matches(t.condition, event)]

    def fire(self, trigger_id: int, *, event_id: str, now: float | None = None) -> Trigger | None:
        """Note that the trigger went off because of ``event_id``: a ``once`` trigger is done, another stays armed.
        None when it isn't armed (already fired, cancelled, expired or unknown)."""
        now = self.store.clock() if now is None else now
        with self.store.transaction():
            trigger = self.get(trigger_id)
            if trigger is None or not trigger.armed:
                return None
            status = "fired" if trigger.once else "armed"
            self.store.changed(
                "UPDATE triggers SET status=?, fired_at=?, fired_event=?, fire_count=fire_count+1 WHERE id=?",
                (status, now, event_id, trigger_id),
            )
            self.store.record_change(
                f"trigger:{trigger_id}",
                "fired",
                cause=event_id,
                detail={"status": status, "fire_count": trigger.fire_count + 1},
            )
        return self.get(trigger_id)

    def cancel(self, trigger_id: int, *, cause: str | None = None) -> Trigger | None:
        """Disarm a trigger. None when there was no armed trigger with this id."""
        return self._close(trigger_id, "cancelled", cause=cause)

    def expire(self, now: float) -> list[Trigger]:
        """Retire the armed triggers whose time ran out."""
        with self.store.transaction():
            rows = self.store.execute(
                "SELECT id FROM triggers WHERE status='armed' AND expires_at IS NOT NULL AND expires_at<=? "
                "ORDER BY created_at, id",
                (now,),
            )
            expired = [self._close(r["id"], "expired") for r in rows]
        return [t for t in expired if t is not None]

    def get(self, trigger_id: int) -> Trigger | None:
        row = self.store.one("SELECT * FROM triggers WHERE id=?", (trigger_id,))
        return _to_trigger(row) if row else None

    def list(self, *, status: str | None = None, limit: int = 50) -> list[Trigger]:
        """Triggers newest first, optionally only those with ``status``."""
        where, params = (" WHERE status=?", [status]) if status else ("", [])
        rows = self.store.execute(
            f"SELECT * FROM triggers{where} ORDER BY created_at DESC, id DESC LIMIT ?", (*params, limit)
        )
        return [_to_trigger(r) for r in rows]

    def delete_all(self) -> int:
        """Forget every trigger (the user wiped what she keeps). Returns how many there were."""
        return self.store.changed("DELETE FROM triggers")

    def _close(self, trigger_id: int, status: str, *, cause: str | None = None) -> Trigger | None:
        with self.store.transaction():
            if not self.store.changed(
                "UPDATE triggers SET status=? WHERE id=? AND status='armed'", (status, trigger_id)
            ):
                return None
            self.store.record_change(f"trigger:{trigger_id}", status, cause=cause, detail={"status": status})
        return self.get(trigger_id)
