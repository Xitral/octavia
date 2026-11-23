"""Saying TickTick's tasks out loud when they come due, and what's due today each morning; and noticing when the
last task due today has been ticked off (``ClearedWatch``, for the avatar's victory lap).

TickTick can't tell anyone when a task comes due (its API has no webhooks), so this looks every
``ticktick.check_every_min`` minutes. A look costs a request to TickTick and nothing else: the reminder itself is
said straight through the voice (``Assistant.announce``), without the brain, so it uses no Claude usage. A task due
before the next look gets a timer for its exact time. What was already said is kept in
``data/ticktick/reminded.json``, so a restart doesn't repeat it.

The morning rundown (her daily briefing) is the first thing she brings up each day, in one message: what's due
today, Canvas work due today too (``more_due``), what's overdue, what's left on the calendar (``calendar``), one line
on what changed since you last talked (``changes``: a deadline that moved, hunches of Futures' that changed), and the
remarks of her own that Futures held back for it (``remarks``: a nudge about tonight's lab, say), so the day doesn't
start with a nudge, then a rundown that says nothing's due. Futures asks ``morning_rundown`` to know whether to hold
them. It's said at the first look after its time when she's idle (``idle``: nobody talking with her, nothing else to
say); a day she wasn't running then (the PC was off) or was busy all along, she catches up at her first idle moment
later that day, until ``CATCH_UP_UNTIL``. The facts are gathered by code (no Claude usage); she says them in her own
words (``tell``: a turn of her own, given the facts and left to choose what matters today), and the template
(``summary_text``) is what's said when her turn can't run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octavia.core.events import Event, ToolCallFinished, ToolCallStarted
from octavia.core.jsonfile import read_json, write_json
from octavia.tools.extensions.ticktick import PREFIX

if TYPE_CHECKING:
    from octavia.config import TickTickConfig

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=10)  # a task that came due this recently (while she was busy or starting) is still said
KEEP = timedelta(days=3)  # how long a said reminder is remembered
MAX_LISTED = 5  # the morning rundown names this many tasks, then counts the rest
MAX_EVENTS = 3  # ...this many calendar events
MAX_MOVED = 2  # ...and this many deadlines that moved
CATCH_UP_UNTIL = time(22, 0)  # a rundown missed in the morning is said at her first idle moment until this time
HOLD_BEFORE = timedelta(hours=4)  # her own remarks wait for the rundown from this long before its time...
HOLD_AFTER = timedelta(hours=3)  # ...until it's said, but no longer than this after its time...
HOLD_CATCH_UP = timedelta(hours=1)  # ...or after her first look of a day she started late (a missed morning)
IDLE_POLL_S = 15.0  # while the rundown waits for her to be idle, how often she checks
SOURCE_TIMEOUT_S = 60.0  # the calendar (Google can take 45 s), Canvas and Futures, each, before it goes without them
TALKED_WITHIN = timedelta(days=7)  # "since we last talked" looks back at most this far...
NOT_TALKED = timedelta(days=1)  # ...and this far when there's no conversation on record


@dataclass(frozen=True)
class DueTask:
    """An unfinished task with a date. ``due`` is local time (naive); ``all_day`` tasks have a date, no time."""

    id: str
    title: str
    due: datetime
    all_day: bool = False
    priority: int | None = None  # 0 none, 1 low, 3 medium, 5 high; None when TickTick didn't say
    repeats: bool = False  # a repeating to-do: ticking it off moves it to its next date, same id


@dataclass(frozen=True)
class CalendarItem:
    """One event on the calendar today, in local time (naive)."""

    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    private: bool = False  # about health, money, relationships or work (``classify_domain``): not named out loud


@dataclass(frozen=True)
class Changes:
    """What changed since the user last talked with her, from Futures: work whose deadline moved (its name and new
    due time, local and naive), and how many of her hunches about them changed."""

    moved: tuple[tuple[str, datetime], ...] = ()
    hunches: int = 0


Fetch = Callable[[], Awaitable[Sequence[DueTask]]]
Calendar = Callable[[], Awaitable[Sequence[CalendarItem] | None]]  # None: no calendar connected
# What changed since a time (a timestamp): Futures' records, read on its worker
ChangesSince = Callable[[float], Awaitable[Changes]]
# When the user last talked with her (a timestamp), None when there's no conversation on record
LastTalked = Callable[[], Awaitable[float | None]]
# Futures' remarks held for the morning, released to be said inside the rundown: (text, note for the brain)
Remarks = Callable[[], Awaitable[Sequence[tuple[str, str | None]]]]
# (what to say, a note for the brain, what kind of announcement it is: "reminder" or "morning", for the avatar)
Announce = Callable[[str, str | None, str], None]
# The rundown in her own words (``Assistant.tell_in_own_words``): (her turn, the facts as its note, the template's
# text, the template's note), the template said when her turn can't run
Tell = Callable[[str, str, str, str], None]


# TickTick's tools (without the prefix) that finish tasks, through ticktick_more too.
TASK_COMPLETIONS = frozenset({"complete_task", "complete_tasks_in_project"})


def completes_task(tool: str, args: dict[str, Any]) -> bool:
    """Whether a call of ``tool`` with ``args`` finishes a TickTick task (not just asks about one)."""
    if not tool.startswith(PREFIX):
        return False
    name = tool[len(PREFIX) :]
    if name == "more":
        inner = args.get("tool")
        if not isinstance(inner, str) or args.get("describe"):
            return False
        name = inner.strip()
    return name in TASK_COMPLETIONS


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _clock_text(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def _names(tasks: Sequence[DueTask], *, times: bool = False) -> str:
    titles = [
        f"“{t.title}” at {_clock_text(t.due)}" if times and not t.all_day else f"“{t.title}”"
        for t in tasks[:MAX_LISTED]
    ]
    if len(tasks) > MAX_LISTED:
        titles.append(f"{len(tasks) - MAX_LISTED} more")
    if len(titles) == 1:
        return titles[0]
    return ", ".join(titles[:-1]) + " and " + titles[-1]


def reminder_text(task: DueTask, now: datetime) -> str:
    """What she says when ``task`` comes due: "Heads up: “Call the dentist” is due now." """
    minutes = round((task.due - now).total_seconds() / 60)
    if minutes >= 2:
        when = f"is due in {minutes} minutes"
    elif minutes <= -2:
        when = f"was due {-minutes} minutes ago"
    else:
        when = "is due now"
    return f"Heads up: “{task.title}” {when}."


def _and(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _greeting(now: datetime | None, late: bool) -> str:
    hour = now.hour if now is not None else 9
    word = "Morning!" if hour < 12 else "Afternoon!" if hour < 17 else "Evening!"
    return f"{word} Catching you up on today." if late else word


def _event_text(item: CalendarItem, now: datetime | None) -> str:
    name = "something personal" if item.private else f"“{item.title}”"
    if item.all_day:
        return f"{name} all day"
    if now is not None and item.start < now:
        return f"{name} until {_clock_text(item.end)}"
    return f"{name} at {_clock_text(item.start)}"


def _day_text(moment: datetime, now: datetime | None) -> str:
    today = (now or moment).date()
    if moment.date() == today:
        return "today"
    if moment.date() == today + timedelta(days=1):
        return "tomorrow"
    if today < moment.date() < today + timedelta(days=7):
        return moment.strftime("%A")
    return f"{moment:%B} {moment.day}"


def changes_text(changes: Changes, now: datetime | None = None) -> str:
    """One line on what changed since the user last talked with her; "" when nothing did."""
    parts = []
    for title, due in changes.moved[:MAX_MOVED]:
        when = _day_text(due, now) if due.time() == time() else f"{_day_text(due, now)} at {_clock_text(due)}"
        parts.append(f"“{title}” moved to {when}")
    if len(changes.moved) > MAX_MOVED:
        more = len(changes.moved) - MAX_MOVED
        parts.append(f"{more} more deadline{'s' if more > 1 else ''} moved")
    if changes.hunches:
        parts.append(
            "I updated one hunch about you"
            if changes.hunches == 1
            else f"I updated {changes.hunches} hunches about you"
        )
    return f"Since we last talked, {_and(parts)}." if parts else ""


def summary_text(
    today: Sequence[DueTask],
    overdue: Sequence[DueTask],
    remarks: Sequence[str] = (),
    *,
    events: Sequence[CalendarItem] | None = None,
    changes: Changes | None = None,
    now: datetime | None = None,
    late: bool = False,
    all_read: bool = True,
) -> str:
    """The morning rundown, one message: what's due today (with the time, for a task that has one) and what's
    overdue, by name; what's on the calendar today (``events``; None when she couldn't read it); one line on what
    changed since the user last talked with her; then what else she had to bring up this morning (``remarks``).
    ``late``: a rundown she's catching up on, later than its time. ``all_read``: False when a source of what's due
    (Canvas) couldn't be read, so she doesn't call the day clear."""
    parts = [_greeting(now, late)]
    if today:
        count = "one thing" if len(today) == 1 else f"{len(today)} things"
        parts.append(f"You've got {count} due today: {_names(today, times=True)}.")
    elif not overdue and all_read and events is not None and not events:
        parts.append("Nothing's due today and your calendar's clear. Wide open sky!")
    else:
        parts.append("Nothing's due today.")
    if overdue:
        count = "One task is" if len(overdue) == 1 else f"{len(overdue)} tasks are"
        parts.append(f"{count} overdue: {_names(overdue)}.")
    if events:
        shown = [_event_text(item, now) for item in events[:MAX_EVENTS]]
        if len(events) > MAX_EVENTS:
            shown.append(f"{len(events) - MAX_EVENTS} more")
        parts.append(f"On your calendar: {_and(shown)}.")
    if changes is not None and (changed := changes_text(changes, now)):
        parts.append(changed)
    parts.extend(text.strip() for text in remarks if text.strip())
    return " ".join(parts)


RUNDOWN_TURN = "(Morning rundown) Time for today's rundown."
RUNDOWN_NOTE = (
    "it's {clock}, and this is the user's morning rundown, which you bring up on your own{late}. Say it out loud in "
    "your own voice and personality, as one short spoken message (a few sentences; no lists or headings): name "
    "everything due today and everything overdue (grouping them is fine), then choose from the rest what matters "
    "today, from what you know about them and how things stand, and leave out what doesn't. These facts were just "
    "read, so don't look them up again, and never say something was done that isn't here. A calendar entry given as "
    '"something personal" stays unnamed. {facts}'
)
MAX_FACTS = 12  # the rundown's facts name this many tasks or events, then count the rest


def _listed(items: Sequence[str]) -> str:
    shown = list(items[:MAX_FACTS])
    if len(items) > MAX_FACTS:
        shown.append(f"{len(items) - MAX_FACTS} more")
    return "; ".join(shown)


def _task_fact(task: DueTask) -> str:
    return f"“{task.title}”" if task.all_day else f"“{task.title}” at {_clock_text(task.due)}"


def rundown_note(
    today: Sequence[DueTask],
    overdue: Sequence[DueTask],
    remarks: Sequence[tuple[str, str | None]] = (),
    *,
    events: Sequence[CalendarItem] | None = None,
    changes: Changes | None = None,
    now: datetime,
    late: bool = False,
    all_read: bool = True,
) -> str:
    """The facts of the morning rundown, for her to say in her own words (``RUNDOWN_TURN``'s note): the same ones
    ``summary_text`` puts in its sentences, private events unnamed alike, and the remarks held for it with their
    notes."""
    facts = [f"Due today: {_listed([_task_fact(t) for t in today]) if today else 'nothing'}."]
    if not all_read:
        facts.append("Canvas couldn't be read just now, so more may be due: don't call the day clear.")
    if overdue:
        facts.append(f"Overdue: {_listed([f'“{t.title}” (due {_day_text(t.due, now)})' for t in overdue])}.")
    if events is None:
        facts.append("The calendar couldn't be read (or none is connected): don't call it clear.")
    elif events:
        facts.append(f"Still on the calendar today: {_listed([_event_text(item, now) for item in events])}.")
    else:
        facts.append("Nothing left on the calendar today.")
    if changes is not None and (changed := changes_text(changes, now)):
        facts.append(f"What changed since you last talked, in your words to them: {changed}")
    held = [(text.strip(), note) for text, note in remarks if text.strip()]
    if held:
        lines = [f'"{text}"' + (f" (about it: {note})" if note else "") for text, note in held]
        facts.append(
            "Things of your own you held for this morning: bring them up in it too, in your own words: "
            + " ".join(f"{n}) {line}" for n, line in enumerate(lines, 1))
        )
    return RUNDOWN_NOTE.format(
        clock=_clock_text(now),
        late=" (later than usual: you're catching them up on today)" if late else "",
        facts=" ".join(facts),
    )


def _title_key(title: str) -> str:
    return " ".join(title.casefold().split())


def _same(a: DueTask, b: DueTask) -> bool:
    """The same piece of work twice (a Canvas assignment copied into TickTick): the same name, due the same day."""
    return a.due.date() == b.due.date() and _title_key(a.title) == _title_key(b.title)


class Reminders:
    """Looks at TickTick every few minutes and says what's due. Start with ``start()``, stop with ``close()``."""

    def __init__(
        self,
        settings: TickTickConfig,
        fetch: Fetch,
        announce: Announce,
        store: Path,
        *,
        clock: Callable[[], datetime] = _now,
        more_due: Fetch | None = None,
        remarks: Remarks | None = None,
        calendar: Calendar | None = None,
        changes: ChangesSince | None = None,
        last_talked: LastTalked | None = None,
        idle: Callable[[], bool] = lambda: True,
        tell: Tell | None = None,
    ) -> None:
        self.settings = settings
        self._fetch = fetch
        self._announce = announce
        self._store = store
        self._clock = clock
        self._more_due = more_due  # more work due (Canvas's), for the rundown only: a failure leaves it out
        self._remarks = remarks
        self._calendar = calendar  # today's events, for the rundown only: a failure leaves them out
        self._changes = changes
        self._last_talked = last_talked
        self._idle = idle
        self._tell = tell  # the rundown in her own words; None: the template is said
        self._waiting = False  # the rundown is due and waits for her to be idle
        self._first_look: datetime | None = None  # her first look today that TickTick answered
        self._said: dict[str, str] = {}  # reminder key -> when it was said (ISO)
        self._summary_day = ""  # the date of the last morning rundown (ISO)
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._task: asyncio.Task[None] | None = None
        self._failing = False
        self._load()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="ticktick-reminders")

    async def close(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        interval = self.settings.check_every_min * 60
        while True:
            await self.check()
            waited = 0.0
            while waited < interval:  # while the rundown waits, it's said as soon as she's idle, not at the next look
                step = min(IDLE_POLL_S if self._waiting else interval, interval - waited)
                await asyncio.sleep(step)
                waited += step
                if self._waiting and self._idle_now():
                    break

    def _idle_now(self) -> bool:
        try:
            return bool(self._idle())
        except Exception:
            log.warning("Couldn't tell whether she's idle", exc_info=True)
            return True  # announce still waits for a quiet moment

    # ------------------------------------------------------------------ one look

    async def check(self) -> None:
        """Look at TickTick once: say the morning rundown if it's time, and set timers for what's coming due."""
        try:
            tasks = list(await self._fetch())
        except Exception as exc:
            if not self._failing:  # once, not every few minutes while TickTick is down
                log.warning("Couldn't check TickTick for what's due: %s", exc)
            self._failing = True
            self._waiting = False  # not asked again every few seconds: at the next look
            return
        self._failing = False
        now = self._clock()
        if self._first_look is None or self._first_look.date() != now.date():
            self._first_look = now
        await self._morning(tasks, now)
        if self.settings.reminders:
            self._schedule(tasks, now)
        self._forget_old(now)

    def _start(self, day: date) -> datetime | None:
        """When the rundown is due on ``day``, or None when there's none."""
        at = self.settings.morning_summary
        if not at:
            return None
        hour, minute = (int(part) for part in at.split(":"))
        return datetime.combine(day, time(hour, minute))

    def _until(self, start: datetime) -> datetime:
        """Until when a rundown due at ``start`` is still said (caught up on)."""
        return max(datetime.combine(start.date(), CATCH_UP_UNTIL), start + timedelta(hours=1))

    def morning_rundown(self, now: datetime | None = None) -> datetime | None:
        """When today's rundown is due, while it's still to come at ``now`` (local, naive): from ``HOLD_BEFORE``
        before its time until it's said, but at most ``HOLD_AFTER`` past its time (or ``HOLD_CATCH_UP`` past her first
        look, on a day she started later than that); None otherwise, and while TickTick isn't answering (no rundown can
        come then). Futures holds her own remarks for it meanwhile.
        Called from Futures' worker thread: it only reads a few fields."""
        now = now or self._clock()
        start = self._start(now.date())
        if start is None or self._failing or self._summary_day == now.date().isoformat():
            return None
        if not start - HOLD_BEFORE <= now < self._until(start):
            return None
        # Not all day: she may not be idle for hours (a call, a dead microphone), and her remarks would go stale.
        first = self._first_look or now  # before her first look, she's just starting
        until = first + HOLD_CATCH_UP if first > start + HOLD_AFTER else start + HOLD_AFTER
        if now >= until:
            return None
        return start

    def said_today(self, now: datetime | None = None) -> bool:
        """Whether today's rundown was said (it lists the Canvas work due today: deadline alerts don't repeat it).
        Thread-safe: it only reads a field."""
        return self._summary_day == (now or self._clock()).date().isoformat()

    async def _morning(self, tasks: Sequence[DueTask], now: datetime) -> None:
        start = self._start(now.date())
        if start is None or self._summary_day == now.date().isoformat() or not start <= now < self._until(start):
            self._waiting = False
            return
        if not self._idle_now():  # said at her first idle moment (``_run`` checks often meanwhile)
            self._waiting = True
            return
        self._waiting = False
        late = now >= start + timedelta(minutes=self.settings.check_every_min) + GRACE
        today = [t for t in tasks if t.due.date() == now.date()]
        overdue = [t for t in tasks if t.due.date() < now.date()]
        extras, events, changes = await asyncio.gather(
            self._more_due_today(now), self._events_today(now), self._changes_since_talked(now)
        )
        for extra in extras or ():
            if not any(_same(extra, t) for t in today):
                today.append(extra)
        if changes is not None:  # a deadline that moved to today is named among what's due: not twice
            named = {_title_key(t.title) for t in today}
            changes = Changes(
                moved=tuple(m for m in changes.moved if _title_key(m[0]) not in named), hunches=changes.hunches
            )
        if self._summary_day == now.date().isoformat():
            return  # said meanwhile, by a look that overlapped this one
        remarks = await self._held_remarks()
        self._summary_day = now.date().isoformat()
        self._save()
        today.sort(key=lambda t: t.due)
        overdue.sort(key=lambda t: t.due)
        all_read = extras is not None
        text = summary_text(
            today, overdue, [r for r, _ in remarks], events=events, changes=changes, now=now, late=late,
            all_read=all_read,
        )  # fmt: skip
        notes = [note for _, note in remarks if note]
        if self._tell is not None:
            facts = rundown_note(
                today, overdue, remarks, events=events, changes=changes, now=now, late=late, all_read=all_read
            )
            try:
                self._tell(RUNDOWN_TURN, facts, text, self._note(notes))
                return
            except Exception:
                log.warning("Couldn't give her the morning rundown to say herself: saying it as written", exc_info=True)
        self._say(text, "morning", notes)

    async def _source(self, what: str, call: Callable[[], Awaitable[Any]]) -> Any:
        """One of the rundown's sources, or None when it failed or took too long: it goes without it."""
        try:
            return await asyncio.wait_for(call(), SOURCE_TIMEOUT_S)
        except Exception as exc:
            log.info("The morning rundown goes without %s: %s", what, str(exc) or type(exc).__name__)
            return None

    async def _more_due_today(self, now: datetime) -> list[DueTask] | None:
        """Canvas work due today; None when it couldn't be read."""
        if self._more_due is None:
            return []
        found = await self._source("Canvas", self._more_due)
        return None if found is None else [t for t in found if t.due.date() == now.date()]

    async def _events_today(self, now: datetime) -> list[CalendarItem] | None:
        """What's left on the calendar today (what already ended isn't), or None without a calendar to read."""
        if self._calendar is None:
            return None
        found = await self._source("the calendar", self._calendar)
        if found is None:
            return None
        return sorted(
            (item for item in found if item.start.date() <= now.date() and (item.all_day or item.end > now)),
            key=lambda item: (item.all_day, item.start, item.title),
        )

    async def _changes_since_talked(self, now: datetime) -> Changes | None:
        if self._changes is None:
            return None
        talked = await self._source("when you last talked", self._last_talked) if self._last_talked else None
        since = (now - NOT_TALKED).timestamp() if talked is None else max(talked, (now - TALKED_WITHIN).timestamp())
        changes = self._changes
        return await self._source("what changed", lambda: changes(since))

    async def _held_remarks(self) -> Sequence[tuple[str, str | None]]:
        if self._remarks is None:
            return []
        try:
            return await self._remarks()
        except Exception:
            log.warning("Couldn't take the remarks held for the morning rundown", exc_info=True)
            return []

    def _schedule(self, tasks: Sequence[DueTask], now: datetime) -> None:
        horizon = now + timedelta(minutes=self.settings.check_every_min) + timedelta(seconds=30)
        before = timedelta(minutes=self.settings.remind_before_min)
        for task in tasks:
            if task.all_day:
                continue  # a day, not a time: the morning rundown has it
            key = f"{task.id}@{task.due.isoformat()}"
            when = task.due - before
            if key in self._said or key in self._timers or not now - GRACE <= when <= horizon:
                continue
            delay = max(0.0, (when - now).total_seconds())
            self._timers[key] = asyncio.get_running_loop().call_later(delay, self._remind, key, task)

    def _remind(self, key: str, task: DueTask) -> None:
        self._timers.pop(key, None)
        if key in self._said:
            return
        now = self._clock()
        self._said[key] = now.isoformat()
        self._save()
        self._say(reminder_text(task, now), "reminder")

    @staticmethod
    def _note(more_notes: Sequence[str] = ()) -> str:
        return " ".join(
            ["That was a TickTick reminder you said on your own; the user may want to act on it.", *more_notes]
        )

    def _say(self, text: str, cue: str, more_notes: Sequence[str] = ()) -> None:
        try:
            self._announce(text, self._note(more_notes), cue)
        except Exception:
            log.warning("Couldn't say a TickTick reminder", exc_info=True)

    def _forget_old(self, now: datetime) -> None:
        cutoff = (now - KEEP).isoformat()
        old = [key for key, said in self._said.items() if said < cutoff]
        for key in old:
            del self._said[key]
        if old:
            self._save()

    # ------------------------------------------------------------------ what was said

    def _load(self) -> None:
        data = read_json(self._store)
        if isinstance(data, dict):
            said = data.get("said")
            if isinstance(said, dict):
                self._said = {str(k): str(v) for k, v in said.items()}
            if isinstance(data.get("summary_day"), str):
                self._summary_day = data["summary_day"]

    def _save(self) -> None:
        try:
            write_json(self._store, {"said": self._said, "summary_day": self._summary_day})
        except OSError:
            log.warning("Couldn't remember which TickTick reminders were said", exc_info=True)


async def canvas_due_today() -> list[DueTask]:
    """Canvas work due today and not handed in, for the morning rundown (one request to Canvas, no Claude usage);
    none without a Canvas token."""
    from octavia.tools.extensions import canvas

    if not canvas.canvas_token():
        return []
    start, end = canvas.window(None, 1)
    items = await canvas.fetch_items(start, end)
    return [
        DueTask(
            id=f"canvas:{item.key}", title=item.title, due=item.due.astimezone().replace(tzinfo=None, microsecond=0)
        )
        for item in items
        if not item.done
    ]


async def calendar_today(transport: Any = None) -> list[CalendarItem] | None:
    """Today's events from the calendar feeds (``calendar_feed``), for the morning rundown: a request to each feed, no
    Claude usage. None when no calendar is connected; fails only when no feed could be read."""
    return await calendar_on(date.today(), transport)


async def calendar_on(day: date, transport: Any = None) -> list[CalendarItem] | None:
    """``day``'s events, as ``calendar_today`` reads today's (her overnight work looks at tomorrow's too)."""
    from octavia.tools.extensions import calendar_feed as feed

    urls = feed.calendar_urls()
    if not urls:
        return None
    reader = feed.GetCalendarEvents(transport)
    results = await asyncio.gather(*(reader._fetch(url) for url in urls), return_exceptions=True)
    texts = [r for r in results if isinstance(r, str)]
    if not texts:
        failed = next((r for r in results if isinstance(r, BaseException)), None)
        raise RuntimeError(f"no calendar could be read ({type(failed).__name__})")  # never the secret address
    events = [event for text in texts for event in feed.parse_ics(text)]
    start = feed._local(datetime.combine(day, time()))
    end = feed._local(datetime.combine(day + timedelta(days=1), time()))
    from octavia.futures.privacy import classify_domain

    return [
        CalendarItem(
            title=occ.title,
            start=occ.start.astimezone().replace(tzinfo=None),
            end=occ.end.astimezone().replace(tzinfo=None),
            all_day=occ.all_day,
            private=classify_domain(occ.title, occ.location) is not None,
        )
        for occ in feed.occurrences(events, start, end)
    ]


def futures_changes(service: Any, since: float) -> Changes:
    """What changed in Futures' records since ``since`` (a timestamp), for the rundown's "since we last talked" line:
    work whose due time moved (by its first due time in that span and its current one; not work that got its first
    due time, was finished or lost it), newest first, and how many hunches formed, strengthened, weakened or were
    judged (``futures_report``'s "changes"). Only what she may bring up unprompted: not from a source or domain the
    user kept out, not something sensitive. Runs on Futures' worker thread (``FuturesService.call``)."""
    from octavia.futures.privacy import speakable
    from octavia.futures.world import DONE

    controls = service.controls
    first: dict[str, Any] = {}  # entity ref -> its due time when the span began
    latest: dict[str, tuple[float, Any]] = {}  # entity ref -> (when it last moved, the entity as it is now)
    for entity, change in service.world.deadlines_changed(since):  # oldest first
        first.setdefault(entity.ref, change.old)
        latest[entity.ref] = (change.ts, entity)
    # A repeating to-do ticked off comes back with its next date: that's done, not moved.
    finished = {change.ref for change in service.world.changes(since=since, field="status") if change.new in DONE}
    moved: list[tuple[float, str, datetime]] = []
    for ref, (ts, entity) in latest.items():
        old, new = first[ref], entity.due_ts
        if old is None or new is None or entity.status in DONE or ref in finished:
            continue
        try:
            allowed = (
                abs(float(new) - float(old)) >= 60
                and controls.allows_source(entity.source)
                and controls.allows_proactive(entity.domain)
                and speakable(entity.sensitivity)
            )
        except (TypeError, ValueError):  # an odd due time or sensitivity: left out
            allowed = False
        if allowed:
            moved.append((ts, entity.name, datetime.fromtimestamp(float(new)).replace(microsecond=0)))
    hunches: set[str] = set()
    rows = service.store.execute(
        "SELECT object, change, detail FROM changes WHERE ts>=? AND object LIKE 'hypothesis:%'", (since,)
    )
    for row in rows:
        number = row["object"].partition(":")[2]
        if not number.isdigit() or row["change"] in ("updated", "forgotten"):
            continue
        detail = json.loads(row["detail"] or "{}")
        if isinstance(detail, dict) and detail.get("status") == "stale":
            continue  # time passing, not evidence
        h = service.hypotheses.get(int(number))
        if h is not None and controls.allows_proactive(h.domain):
            hunches.add(number)
    moved.sort(key=lambda item: -item[0])
    return Changes(moved=tuple((name, due) for _, name, due in moved), hunches=len(hunches))


def local_due(value: str | None, all_day: bool = False) -> datetime | None:
    """A TickTick date ("2026-09-26T16:00:00.000+0000", or ISO with a colon in the offset) in local time, naive."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-4:].isdigit():
        text = text[:-2] + ":" + text[-2:]  # +0000 -> +00:00
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text[:10]), time())
        except ValueError:
            return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    if all_day:
        moment = datetime.combine(moment.date(), time())
    return moment.replace(microsecond=0)


class ClearedWatch:
    """Notices the user ticking off the last task due today (overdue ones count too), so the avatar can do a victory
    lap. Feed it every event (``on_event``). When a call that finishes a task starts, it looks at what's due (a request
    to TickTick, no Claude usage); when that call has gone through, it looks again: something due today before and
    nothing after means it's all done, and ``on_cleared`` is called. A look that fails just skips it."""

    def __init__(self, fetch: Fetch, on_cleared: Callable[[], None], *, clock: Callable[[], datetime] = _now) -> None:
        self._fetch = fetch
        self._on_cleared = on_cleared
        self._clock = clock
        self._before: dict[str, asyncio.Task[int | None]] = {}  # call id -> the look taken as it started
        self._tasks: set[asyncio.Task[Any]] = set()

    def on_event(self, event: Event) -> None:
        if isinstance(event, ToolCallStarted) and completes_task(event.name, dict(event.args or {})):
            self._before[event.call_id] = self._spawn(self._due_today())
        elif isinstance(event, ToolCallFinished):
            before = self._before.pop(event.call_id, None)
            if before is not None and event.ok:
                self._spawn(self._check(before))

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._before.clear()

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name="ticktick-cleared")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _due_today(self) -> int | None:
        """How many unfinished tasks are due today or overdue, or None when TickTick couldn't say."""
        try:
            tasks = await self._fetch()
        except Exception as exc:
            log.debug("Couldn't check TickTick for what's left today: %s", exc)
            return None
        today = self._clock().date()
        return sum(1 for task in tasks if task.due.date() <= today)

    async def _check(self, before: asyncio.Task[int | None]) -> None:
        if not await before:
            return  # nothing was due today (or TickTick couldn't say): nothing to celebrate
        if await self._due_today() == 0:
            try:
                self._on_cleared()
            except Exception:
                log.warning("Couldn't celebrate clearing today's tasks", exc_info=True)
