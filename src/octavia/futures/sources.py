"""Where Octavia Futures sees the world outside the conversation: the to-dos TickTick lists, the work Canvas has due
and graded, which app is in front, and the meetings coming up on the calendar.

None of these can tell her when something changes (TickTick and Canvas have no webhooks, Windows no cheap "the app in
front changed" signal without a hook), so each is a look now and then, and a change is what differs from the last
look. Each look is diffed against a snapshot kept in the Futures database (``store.kv``), so a restart neither
misses a change nor sees everything as new, and every event carries a ``dedup_key``, so a happening seen twice (two
looks racing, a snapshot lost) is still one event.

* ``TaskSnapshots`` asks TickTick for nothing: it is handed the looks the reminders (``Reminders``, ``ClearedWatch``)
  already take at the undone dated to-dos. A to-do that leaves the list may have been ticked off, deleted, or just
  moved out of the window; only asking TickTick about it (``verify``) tells which, so without that it says nothing.
* ``CanvasWatch`` looks at the planner once an hour and at grades every few hours, only once the user has set
  ``CANVAS_API_TOKEN``. The first look is a baseline: everything on it is marked so, and "remind me when a new
  assignment shows up" never fires for what was already there. A grade is kept as a band (high, mid, low), never
  the score, and is sensitive: it's never brought up unprompted.
* ``ContextWatch`` notes the app in front every couple of seconds, by name only, never a window's title: which app
  the user works in says enough, what's in it is theirs. An app counts once it has stayed in front a few seconds,
  so flicking through Alt-Tab isn't a string of app changes.
* ``CalendarWatch`` reads the calendar feeds every 15 minutes, only once the user has set ``CALENDAR_ICS_URLS``, for
  online meetings coming up. It records nothing: it only keeps the next two hours of them in memory, for an offer to
  open one's link (``offers.py``).

Everything here is plain program logic: no model is asked what happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octavia.core.events import AppContextChanged
from octavia.futures.events import FuturesEvent
from octavia.futures.privacy import redact
from octavia.futures.schema import Sensitivity, ref
from octavia.futures.store import FuturesStore, loads

if TYPE_CHECKING:
    from octavia.tools.extensions._ticktick_reminders import DueTask
    from octavia.tools.extensions.canvas import CanvasItem

log = logging.getLogger(__name__)

Record = Callable[[FuturesEvent], None]  # where the events go (the service's intake); may return an awaitable
LocalTime = Callable[[float], datetime]

DAY_S = 86400.0
VERIFY_TRIES = 3  # a to-do that left the list is asked about this many times at most, then let go
CANVAS_PAST_S = 14 * DAY_S  # the planner window: work due in the last two weeks...
CANVAS_AHEAD_S = 30 * DAY_S  # ...to a month ahead
GRADES_SINCE_S = 14 * DAY_S  # grades released in the last two weeks
COMPLETED, ABANDONED = 2, -1  # TickTick task statuses (0 is open)

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# ---------------------------------------------------------------------------------------------------- when


def part_of_day(hour: int) -> str:
    """ "morning" (5-11), "afternoon" (12-16), "evening" (17-21) or "night"."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def time_context(ts: float, *, local_time: LocalTime = datetime.fromtimestamp) -> dict[str, Any]:
    """The time conditions an event happened in, in local time: its hour, part of the day, weekday and whether that's
    a weekday or the weekend."""
    moment = local_time(ts)
    weekday = moment.weekday()
    return {
        "hour": moment.hour,
        "part_of_day": part_of_day(moment.hour),
        "weekday": _WEEKDAYS[weekday],
        "day_type": "weekend" if weekday >= 5 else "weekday",
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _status(value: Any) -> int | None:
    """A TickTick status, written as a number or a string of one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


async def _deliver(record: Record, event: FuturesEvent) -> None:
    """Hand ``event`` on. A failure there is logged, never raised: a look is never lost to one bad event."""
    try:
        result: Any = record(event)
        if inspect.isawaitable(result):
            await result
    except Exception:
        log.warning("Couldn't record a %s event", event.type, exc_info=True)


# ---------------------------------------------------------------------------------------------------- TickTick

Verify = Callable[[str], Awaitable[Mapping[str, Any] | None]]


class TaskSnapshots:
    """Diffs each look TickTick gives at the undone dated to-dos (Reminders' and ClearedWatch's own looks, so no extra
    requests) into events: a to-do seen for the first time, its due time moved, its due time passed with it still
    open, and (asked of ``verify``) ticked off or deleted once it leaves the list.

    ``verify(task_id)`` says what became of a to-do: ``{"status": 2, "completed_ts": ...}`` once it's done,
    ``{"status": -1}`` or ``{"deleted": True}`` when it's gone for good, anything else (None) when it's still open,
    only no longer in the window the looks cover (``ticktick_verify`` asks TickTick itself). Without it, a to-do
    leaving the list says nothing: done and rescheduled look the same from here.
    """

    NAMESPACE = "ticktick"
    KEY = "snapshot"

    def __init__(
        self,
        store: FuturesStore,
        record: Record,
        *,
        verify: Verify | None = None,
        clock: Callable[[], float] = time.time,
        local_time: LocalTime = datetime.fromtimestamp,
    ) -> None:
        self.store = store
        self._record = record
        self._verify = verify
        self._clock = clock
        self._local_time = local_time
        self._lock = asyncio.Lock()  # the reminders and ClearedWatch may hand over looks at the same time

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The last look as kept: ``{task id: {"title", "due_ts", "all_day"}}`` (plus ``"overdue"`` once that was
        said, and ``"gone"``: how many times ``verify`` failed for one that left the list)."""
        raw = self.store.kv_get(self.NAMESPACE, self.KEY, {})
        if not isinstance(raw, dict):
            return {}
        return {str(k): dict(v) for k, v in raw.items() if isinstance(v, Mapping)}

    async def observe(self, tasks: Sequence[DueTask]) -> None:
        """One look at what's undone and dated (``DueTask``s, due in local time): record what changed since the
        last. Never raises: it runs inside the reminders' own look, which must go on whatever happens here."""
        try:
            async with self._lock:
                await self._observe(tasks)
        except Exception:
            log.warning("Couldn't note what changed in TickTick", exc_info=True)

    async def _observe(self, tasks: Sequence[DueTask]) -> None:
        now = self._clock()
        before = await self.store.run(self.snapshot)
        # Never looked before: every to-do is "seen" for the first time, and marked a baseline (no news).
        first = await self.store.run(self.store.kv_get, self.NAMESPACE, self.KEY) is None
        after: dict[str, dict[str, Any]] = {}
        for task in tasks:
            ident = str(task.id or "")
            if not ident or ident in after:
                continue
            after[ident] = await self._listed(ident, task, before.get(ident), now, first=first)
        for ident, old in before.items():
            if ident not in after and (kept := await self._gone(ident, old, now)) is not None:
                after[ident] = kept
        await self.store.run(self.store.kv_set, self.NAMESPACE, self.KEY, after)

    async def _listed(
        self, ident: str, task: DueTask, old: Mapping[str, Any] | None, now: float, *, first: bool = False
    ) -> dict[str, Any]:
        due_ts = task.due.timestamp()
        title = redact(task.title, 120)
        entry: dict[str, Any] = {"title": title, "due_ts": due_ts, "all_day": bool(task.all_day)}
        if getattr(task, "repeats", False):
            entry["repeats"] = True
        old_due = _number(old.get("due_ts")) if old is not None else None
        data: dict[str, Any] = {"title": title, "due_ts": due_ts, "all_day": bool(task.all_day), "by": "ticktick"}
        if getattr(task, "priority", None) is not None:
            data["priority"] = task.priority
        if getattr(task, "repeats", False):
            data["repeats"] = True
        if old_due is None:
            await self._emit("task.seen", ident, now, f"tt-seen:{ident}@{due_ts:.0f}", {**data, "baseline": first})
        elif round(old_due) != round(due_ts) and due_ts > old_due and getattr(task, "repeats", False):
            # A repeating to-do moves to its next date when it's ticked off: that's the old one done (as of this
            # look, the closest she can tell), and a new one due.
            done = {"title": title, "completed_ts": now, "due_ts": old_due, "repeats": True}
            await self._emit("task.completed", ident, now, f"tt-done:{ident}@{old_due:.0f}", done, confidence=0.8)
            await self._emit("task.seen", ident, now, f"tt-seen:{ident}@{due_ts:.0f}", data)
        elif round(old_due) != round(due_ts):
            await self._emit(
                "task.updated", ident, now, f"tt-due:{ident}@{due_ts:.0f}", {**data, "old_due_ts": old_due}
            )
        elif old is not None and old.get("overdue"):
            entry["overdue"] = True  # said already, for this due time
        # An all-day to-do is due at some point in its day, so it's only overdue once the day is over.
        deadline = (task.due + timedelta(days=1)).timestamp() if task.all_day else due_ts
        if deadline < now and not entry.get("overdue"):
            await self._emit(
                "task.overdue", ident, now, f"tt-overdue:{ident}@{due_ts:.0f}", {"title": title, "due_ts": due_ts}
            )
            entry["overdue"] = True
        return entry

    async def _gone(self, ident: str, old: Mapping[str, Any], now: float) -> dict[str, Any] | None:
        """A to-do that left the list: what became of it, when ``verify`` can say. Returns what to keep of it (only
        while ``verify`` keeps failing), or None to let it go."""
        if self._verify is None:
            return None
        title = str(old.get("title") or "")
        due_ts = _number(old.get("due_ts")) or 0.0
        try:
            found = await self._verify(ident)
        except Exception as exc:
            tries = int(_number(old.get("gone")) or 0) + 1
            log.debug("Couldn't check what became of a TickTick task (try %d of %d): %s", tries, VERIFY_TRIES, exc)
            return {**old, "gone": tries} if tries < VERIFY_TRIES else None
        if not isinstance(found, Mapping):
            return None  # TickTick said something else: nothing to go on
        status = _status(found.get("status"))
        if status == COMPLETED:
            completed_ts = _number(found.get("completed_ts"))
            data = {"title": title, "completed_ts": completed_ts if completed_ts is not None else now, "due_ts": due_ts}
            await self._emit("task.completed", ident, now, f"tt-done:{ident}@{due_ts:.0f}", data, confidence=1.0)
        elif status == ABANDONED or found.get("deleted") is True:
            await self._emit("task.deleted", ident, now, f"tt-deleted:{ident}@{due_ts:.0f}", {"title": title})
        else:  # still open: it moved out of the window the looks cover, most likely to a later date
            new_due = _number(found.get("due_ts"))
            if new_due is not None and new_due > due_ts and old.get("repeats"):
                # A repeating to-do (weekly, monthly) ticked off jumps to a date past the window: the old one's done.
                done = {"title": title, "completed_ts": now, "due_ts": due_ts, "repeats": True}
                await self._emit("task.completed", ident, now, f"tt-done:{ident}@{due_ts:.0f}", done, confidence=0.8)
                data = {"title": title, "due_ts": new_due, "all_day": bool(found.get("all_day")), "by": "ticktick"}
                await self._emit("task.seen", ident, now, f"tt-seen:{ident}@{new_due:.0f}", data)
            elif new_due is not None and round(new_due) != round(due_ts):
                data = {"title": title, "due_ts": new_due, "all_day": bool(found.get("all_day")), "old_due_ts": due_ts}
                await self._emit("task.updated", ident, now, f"tt-due:{ident}@{new_due:.0f}", data)
            elif found.get("due_ts", ...) is None and "due_ts" in found:
                await self._emit("task.updated", ident, now, f"tt-due:{ident}@none", {"title": title, "due_ts": None})
        return None

    async def _emit(
        self,
        kind: str,
        ident: str,
        now: float,
        dedup_key: str,
        data: dict[str, Any],
        *,
        confidence: float | None = None,
    ) -> None:
        event = FuturesEvent(
            type=kind,
            source="ticktick",
            ts=now,
            dedup_key=dedup_key,
            subject=ref("task", ident),
            confidence=confidence,
            context=time_context(now, local_time=self._local_time),
            data=data,
        )
        await _deliver(self._record, event)


_MISSING = ("not found", "not exist", "no such task")  # how TickTick may say a task id is gone


async def ticktick_verify(task_id: str, *, client: Any = None) -> dict[str, Any] | None:
    """What became of a TickTick to-do, for ``TaskSnapshots``: ``{"status", "completed_ts"}`` from TickTick's own
    record of it (``get_task_by_id``, one request), ``{"deleted": True}`` once it's gone, None when TickTick sent
    something else. Raises when TickTick can't say, so the next look asks again."""
    from octavia.tools.extensions import ticktick
    from octavia.tools.extensions._ticktick_reminders import local_due

    client = client or ticktick.shared_client()
    answer = await client.call("get_task_by_id", {"task_id": task_id}, retry=True)
    if not answer.ok:
        if any(words in answer.text.lower() for words in _MISSING):
            return {"deleted": True}
        raise ticktick.TickTickError(answer.text[:200])
    task = next((t for t in ticktick.parse_tasks(answer.text) if str(t.get("id") or "") == task_id), None)
    if task is None:
        return None
    if task.get("deleted") in (1, True):
        return {"deleted": True}
    completed = local_due(task.get("completedTime") or task.get("completed_time"))
    all_day = bool(task.get("isAllDay") or task.get("is_all_day"))
    due = local_due(task.get("dueDate") or task.get("due_date"), all_day)
    return {
        "status": task.get("status"),
        "completed_ts": completed.timestamp() if completed is not None else None,
        "due_ts": due.timestamp() if due is not None else None,
        "all_day": all_day,
    }


# ---------------------------------------------------------------------------------------------------- Canvas

FetchItems = Callable[[datetime, datetime], Awaitable[Sequence["CanvasItem"]]]
FetchGrades = Callable[[datetime], Awaitable[Any]]  # canvas_grades.Grades, or just its GradedWork


async def _canvas_items(start: datetime, end: datetime) -> Sequence[CanvasItem]:
    from octavia.tools.extensions import canvas

    return await canvas.fetch_items(start, end)


async def _canvas_grades(since: datetime) -> Any:
    from octavia.tools.extensions import canvas_grades

    return await canvas_grades.fetch_grades(since)


def _canvas_configured() -> bool:
    from octavia.tools.extensions import canvas

    return bool(canvas.canvas_token())


def grade_band(score: float | None, points: float | None) -> str | None:
    """ "high" (90% or more), "mid" (70% or more) or "low"; None without a score out of some points."""
    if score is None or not points or points <= 0:
        return None
    share = score / points
    if share >= 0.9:
        return "high"
    return "mid" if share >= 0.7 else "low"


class CanvasWatch:
    """Polls Canvas for due work and grades (only when the user set CANVAS_API_TOKEN), diffing snapshots into events.

    ``fetch_items(start, end)`` gives the planner's ``CanvasItem``s, ``fetch_grades(since)`` the work graded since
    then (``Grades`` or a list of ``GradedWork``) and ``configured()`` whether there's a token: Canvas's own
    functions by default. ``start`` it once, ``close`` it at exit; ``check`` is one look.
    """

    NAMESPACE = "canvas"

    def __init__(
        self,
        record: Record,
        store: FuturesStore,
        *,
        fetch_items: FetchItems | None = None,
        fetch_grades: FetchGrades | None = None,
        configured: Callable[[], bool] | None = None,
        every_s: float = 3600.0,
        grades_every_s: float = 6 * 3600.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        local_time: LocalTime = datetime.fromtimestamp,
    ) -> None:
        self._record = record
        self.store = store
        self._fetch_items = fetch_items or _canvas_items
        self._fetch_grades = fetch_grades or _canvas_grades
        self._configured = configured or _canvas_configured
        self.every_s = every_s
        self.grades_every_s = grades_every_s
        self._clock = clock
        self._sleep = sleep
        self._local_time = local_time
        self._task: asyncio.Task[None] | None = None
        self._failing: set[str] = set()  # which looks are failing ("planner", "grades"), so it's said once
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="futures-canvas")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.warning("Couldn't look at Canvas for what changed", exc_info=True)
            await self._sleep(self.every_s)

    async def check(self) -> None:
        """One look: the planner every time, grades when they're due a look. Nothing without a token."""
        try:
            configured = bool(self._configured())
        except Exception:
            configured = False
        if not configured:
            return
        async with self._lock:
            now = self._clock()
            await self._planner(now)
            checked = _number(await self.store.run(self.store.kv_get, self.NAMESPACE, "grades_checked"))
            if checked is None or now - checked >= self.grades_every_s:
                await self._grades(now, first=checked is None)

    def _failed(self, part: str, exc: Exception) -> None:
        if part not in self._failing:  # once, not every hour while Canvas is down
            log.warning("Couldn't check Canvas %s: %s", "grades" if part == "grades" else "for due work", exc)
        self._failing.add(part)

    async def _planner(self, now: float) -> None:
        start = datetime.fromtimestamp(now - CANVAS_PAST_S, tz=UTC)
        end = datetime.fromtimestamp(now + CANVAS_AHEAD_S, tz=UTC)
        try:
            items = list(await self._fetch_items(start, end))
        except Exception as exc:
            self._failed("planner", exc)
            return
        self._failing.discard("planner")
        raw = await self.store.run(self.store.kv_get, self.NAMESPACE, "planner")
        first = not isinstance(raw, dict)  # never looked before: this look is the baseline
        before: dict[str, Any] = raw if isinstance(raw, dict) else {}
        after: dict[str, dict[str, Any]] = {}
        for item in items:
            due_ts = item.due.timestamp()
            after[item.key] = {
                "title": redact(item.title, 200),
                "course": item.course,
                "due_ts": due_ts,
                "kind": item.kind,
                "done": bool(item.done),
            }
            old = before.get(item.key)
            if first or not isinstance(old, Mapping):
                data = {**self._about(item), "due_ts": due_ts, "kind": item.kind, "baseline": first}
                await self._emit("canvas.assignment_created", item, now, f"cv-new:{item.key}", data)
                if item.done:  # already handed in when first seen
                    data = {**self._about(item), "baseline": first}
                    await self._emit("canvas.assignment_submitted", item, now, f"cv-sub:{item.key}", data)
                continue
            old_due = _number(old.get("due_ts"))
            if old_due is None or round(old_due) != round(due_ts) or old.get("title") != redact(item.title, 200):
                data = {
                    **self._about(item),
                    "due_ts": due_ts,
                    "kind": item.kind,
                    "old_due_ts": old_due,
                    "baseline": False,
                }
                await self._emit("canvas.assignment_updated", item, now, f"cv-upd:{item.key}@{due_ts:.0f}", data)
            if item.done and not old.get("done"):
                await self._emit("canvas.assignment_submitted", item, now, f"cv-sub:{item.key}", self._about(item))
        await self.store.run(self.store.kv_set, self.NAMESPACE, "planner", after)  # what left the window is forgotten

    async def _grades(self, now: float, *, first: bool) -> None:
        try:
            found = await self._fetch_grades(datetime.fromtimestamp(now - GRADES_SINCE_S, tz=UTC))
        except Exception as exc:
            self._failed("grades", exc)
            return
        self._failing.discard("grades")
        for work in getattr(found, "recent", found) or ():
            graded = work.graded_at.astimezone(UTC)
            digest = hashlib.sha1(f"{work.course}|{work.title}|{graded.isoformat()}".encode()).hexdigest()[:16]
            band = None if work.excused else grade_band(work.score, work.points)
            event = FuturesEvent(
                type="canvas.grade_posted",
                source="canvas",
                ts=now,
                dedup_key=f"cv-grade:{digest}",
                subject=ref("assignment", f"grade-{digest}"),
                entities=self._courses(work.course),
                context=time_context(now, local_time=self._local_time),
                data={
                    "title": redact(work.title, 200),
                    "course": work.course,
                    "band": band,
                    "late": bool(work.late),
                    "graded_ts": graded.timestamp(),
                    "baseline": first,
                },
                sensitivity=Sensitivity.SENSITIVE.value,
            )
            await _deliver(self._record, event)
        await self.store.run(self.store.kv_set, self.NAMESPACE, "grades_checked", now)

    async def snapshot(self) -> tuple[dict[str, Any], float | None]:
        """What the planner showed at the last look that worked, by key, and when that was (None: never looked)."""

        def read() -> tuple[dict[str, Any], float | None]:
            row = self.store.one(
                "SELECT value, updated_at FROM kv WHERE namespace=? AND key=?", (self.NAMESPACE, "planner")
            )
            if row is None:
                return {}, None
            value = loads(row["value"], {})
            return (value if isinstance(value, dict) else {}), _number(row["updated_at"])

        return await self.store.run(read)

    @staticmethod
    def _about(item: CanvasItem) -> dict[str, Any]:
        return {"title": redact(item.title, 200), "course": item.course}

    @staticmethod
    def _courses(course: str) -> tuple[str, ...]:
        return (ref("course", course),) if course.strip() else ()

    async def _emit(self, kind: str, item: CanvasItem, now: float, dedup_key: str, data: dict[str, Any]) -> None:
        event = FuturesEvent(
            type=kind,
            source="canvas",
            ts=now,
            dedup_key=dedup_key,
            subject=ref("assignment", item.key),
            entities=self._courses(item.course),
            context=time_context(now, local_time=self._local_time),
            data=data,
        )
        await _deliver(self._record, event)


# ---------------------------------------------------------------------------------------------------- the app in front

ReadApp = Callable[[], tuple[str, str] | None]


class ContextWatch:
    """Which app is in front, every couple of seconds: names only, never window titles.

    ``read()`` gives the app in front as ``(name, program)``, or None when there's none to speak of (the taskbar, the
    desktop, the lock screen), which keeps the last one: going to the taskbar and back isn't a change. An app becomes
    ``current`` once it has stayed in front ``settle_s`` seconds; each time that happens ``look`` returns an
    ``AppContextChanged`` (with how long the one before was in front), and the loop ``emit``s it.
    """

    def __init__(
        self,
        emit: Callable[[AppContextChanged], None],
        *,
        read: ReadApp | None = None,
        every_s: float = 2.0,
        settle_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._emit = emit
        self._read = read or read_foreground_app
        self.every_s = every_s
        self.settle_s = settle_s
        self._clock = clock
        self._sleep = sleep
        self.current: str | None = None  # the app in front, once one has settled
        self.process: str | None = None  # its program
        self._since = 0.0  # when the current app came to the front (the clock's time)
        self._candidate: tuple[str, str] | None = None  # another app in front, not there long enough yet
        self._candidate_since = 0.0
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="futures-context")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                change = self.look()
                if change is not None:
                    self._emit(change)
            except Exception:
                log.debug("Couldn't see which app is in front", exc_info=True)
            await self._sleep(self.every_s)

    def look(self) -> AppContextChanged | None:
        """One look: an ``AppContextChanged`` when another app has now stayed in front long enough, else None."""
        found = self._read()
        if found is None:
            return None
        now = self._clock()
        app, process = found
        if self.current is not None and app.casefold() == self.current.casefold():
            self._candidate = None  # back to the current one before the other settled
            return None
        if self._candidate is None or self._candidate[0].casefold() != app.casefold():
            self._candidate, self._candidate_since = (app, process), now
        if now - self._candidate_since < self.settle_s:
            return None
        previous = self.current or ""
        previous_s = max(0.0, self._candidate_since - self._since) if self.current is not None else 0.0
        self.current, self.process = self._candidate
        self._since = self._candidate_since
        self._candidate = None
        return AppContextChanged(app=self.current, process=self.process, previous=previous, previous_s=previous_s)


_LOCK_SCREEN = frozenset({"lockapp", "lockscreen"})  # window classes and programs of the lock screen
_APPS_KEPT = 64
_apps: dict[int, tuple[str, str]] = {}  # pid -> (name, program): a process keeps its pid while it runs


def read_foreground_app(exclude_pids: frozenset[int] = frozenset()) -> tuple[str, str] | None:
    """The app in front on this PC as ``(name, program)``: ("Visual Studio Code", "Code.exe"). None off Windows,
    for the taskbar, the desktop and the lock screen, for this process and ``exclude_pids``, and when Windows or the
    process can't say. Never the window's title."""
    if sys.platform != "win32":
        return None
    import psutil

    from octavia.avatar.foreground import SHELL_CLASSES
    from octavia.platform import windows as win

    try:
        hwnd = win.foreground_window()
        if not hwnd:
            return None
        kind = win.window_class(hwnd)
        if kind in SHELL_CLASSES or kind.lower() in _LOCK_SCREEN:
            return None
        pid = win.window_pid(hwnd)
        if not pid or pid == os.getpid() or pid in exclude_pids:
            return None
        found = _apps.get(pid)
        if found is None:
            found = _identify(pid)
            _apps[pid] = found
            while len(_apps) > _APPS_KEPT:
                del _apps[next(iter(_apps))]
    except (OSError, psutil.Error):
        return None
    if Path(found[1]).stem.lower() in _LOCK_SCREEN:
        return None
    return found


def _identify(pid: int) -> tuple[str, str]:
    """A process's speakable name (its program's own description, or the program's name) and program. A Store app
    stays "Application Frame Host": the helper that finds the app inside it isn't public."""
    import psutil

    from octavia.platform import windows as win

    proc = psutil.Process(pid)
    program = proc.name()
    try:
        exe = proc.exe() or ""
    except (psutil.Error, OSError):
        exe = ""
    description = win.exe_description(exe) if exe else None
    name = " ".join((description or "").split())[:60]
    return name or Path(program).stem.replace("_", " ").title() or program, program


# ------------------------------------------------------------------------------------------------- calendar


@dataclass(frozen=True)
class Upcoming:
    """A timed event coming up, as the calendar showed it. ``key`` is stable for this one occurrence (its event and
    start). ``online``: it has a meeting link (only those get the offer to open it; all of them a deadline alert,
    ``core.deadlines``). Kept in memory only: only an offer made (or held back) about it is kept, as a
    recommendation."""

    key: str
    title: str
    start: float
    domain: str = "general"
    sensitivity: str = "personal"
    online: bool = True


async def _calendar_occurrences(start: datetime, end: datetime) -> Sequence[Any]:
    from octavia.tools.extensions import calendar_feed

    reader = calendar_feed.GetCalendarEvents()
    events: list[Any] = []
    for url in calendar_feed.calendar_urls():
        events.extend(calendar_feed.parse_ics(await reader._fetch(url)))
    return calendar_feed.occurrences(events, start, end)


def _calendar_configured() -> bool:
    from octavia.tools.extensions import calendar_feed

    return bool(calendar_feed.calendar_urls())


class CalendarWatch:
    """Reads the user's calendar feeds (``CALENDAR_ICS_URLS``) every 15 minutes for timed events starting in the next
    two hours, so she can offer to open an online meeting's link just before it starts (``offers.meeting_offer``) and
    say when any of them is about to start (``core.deadlines``).

    One HTTP request per feed per look and no model; nothing is recorded as an event, only ``upcoming`` (in memory)
    changes; an offer it leads to is kept like any remark of hers (``recommendations``). A meeting whose title is in a
    protected domain (``privacy.classify_domain``: a doctor's appointment) is skipped altogether. ``configured()``: whether to look at all (a feed
    is set, and the user hasn't excluded the calendar). ``start`` it once, ``close`` it at exit; ``check`` is one look.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[datetime, datetime], Awaitable[Sequence[Any]]] | None = None,
        configured: Callable[[], bool] | None = None,
        every_s: float = 15 * 60.0,
        ahead_s: float = 2 * 3600.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._fetch = fetch or _calendar_occurrences
        self._configured = configured or _calendar_configured
        self.every_s = every_s
        self.ahead_s = ahead_s
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._failing = False
        self.upcoming: tuple[Upcoming, ...] = ()  # replaced whole, so another thread always reads a complete one

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="futures-calendar")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.warning("Couldn't look at the calendar for meetings coming up", exc_info=True)
            await self._sleep(self.every_s)

    def current(self) -> tuple[Upcoming, ...]:
        """What's coming up, or nothing while the calendar isn't to be looked at (the user excluded it meanwhile)."""
        try:
            return self.upcoming if self._configured() else ()
        except Exception:
            return ()

    async def check(self) -> None:
        try:
            configured = bool(self._configured())
        except Exception:
            configured = False
        if not configured:
            self.upcoming = ()
            return
        now = self._clock()
        try:
            found = await self._fetch(
                datetime.fromtimestamp(now, tz=UTC), datetime.fromtimestamp(now + self.ahead_s, tz=UTC)
            )
        except Exception as exc:
            if not self._failing:  # once, not every 15 minutes while it's down
                log.warning("Couldn't read the calendar for meetings coming up: %s", type(exc).__name__)
            self._failing = True
            return  # what was seen last stays: a meeting found before still starts when it said
        self._failing = False
        from octavia.futures.privacy import classify_domain

        upcoming = []
        for occ in found:
            if occ.all_day or occ.start.timestamp() < now:
                continue
            domain = classify_domain(occ.title)
            if domain:
                continue  # a doctor's appointment, a paycheck: never brought up unprompted, so not even kept
            title = redact(occ.title, 120)
            start = occ.start.timestamp()
            link = getattr(occ, "link", "") or ""
            digest = hashlib.sha1(f"{occ.title}|{link}".encode()).hexdigest()[:12]
            upcoming.append(Upcoming(key=f"{digest}@{start:.0f}", title=title, start=start, online=bool(link)))
        self.upcoming = tuple(upcoming)
