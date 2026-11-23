"""Deadline alerts: Canvas work and calendar events brought up as they get close, a little louder each time.

Heads-ups (``core/heads_up.py``) speak when something changes: new or moved work, new email. Nothing there speaks
because time passed, so work posted a week ago and still open at 11 PM got no word, and the only reminder near a
deadline was Futures' offer to "pull up the rubric", which fades when offers go unanswered and waits out its
budgets. Here time is what moves things. Every ``CHECK_S`` the watch reads what Canvas showed at its last look (the
Futures Canvas watch's planner, every 15 minutes) and the calendar (the Futures calendar watch's events coming up),
and each item gets a few stages as its time comes:

* Canvas work still open: ``ahead`` a day before, ``soon`` a few hours before, ``final`` within the hour (urgent: it
  breaks through focus mode and goes to the phone too). An attendance check posted in class is already at ``soon``
  or ``final`` when first seen, so it starts there;
* a timed calendar event: ``starting``, shortly before it starts (urgent).

Each stage of an item is brought up once (``Ledger``, kept on disk so a restart repeats nothing); when several
stages come due together (she was off, or it was quiet) only the latest is said; a deadline that moves starts over.
What's brought up at the same moment is one turn of hers, never a list read out. Nothing is said about work Canvas
shows handed in, and before a stage is said the Canvas look is refreshed if it's more than ``FRESH_S`` old, so a
reminder never follows the hand-in by fifteen minutes.

What it doesn't repeat: what the heads-up told the user lately (``told``: a new attendance check), and the day-before
stage of work due today once the morning rundown, which lists it, was said. Futures' timed offers about Canvas work
and its meeting-link offers step aside for these (``told``, ``Hooks.deadlines``): the alert itself may offer the help.

Time passing isn't the only thing that puts work at risk: three small things can't all fit. Every ``RISK_EVERY_S``
the week is sampled (``futures.simulate.sample_week``, plain code): a stage's note says how the week looks when that
bears on it, and a deadline further off than the stages reach that's missed even if everything goes fast is brought up
once (``_warn_early``), through the same judgment, with what that rests on.

When it's said: the earlier stages wait out quiet hours, focus mode and Do Not Disturb, and the morning rundown still
to come; the urgent ones break through focus, and through quiet hours only while the user is at the PC (input within
``ACTIVE_S``): she doesn't wake anyone for homework. And whether it's said at all is the one judgment everything she
says on her own passes (``core/unprompted.py``): a wave-off holds it, something the user said they already handed in
is stale. That judgment is told what this is (an escalating reminder they asked for), and an urgent one is said even
when it can't run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from octavia.core.heads_up import course_name, when_due
from octavia.core.jsonfile import read_json, write_json
from octavia.core.text import clock

log = logging.getLogger(__name__)

CHECK_S = 60.0  # how often the watch looks
FRESH_S = 5 * 60.0  # Canvas's look is refreshed before a stage is said when it's older than this
NEW_WAIT_S = 3 * 60.0  # an item new since the watch's first look waits this long (a heads-up about it may be on its
# way), unless the stage is urgent
ACTIVE_S = 10 * 60.0  # input within this: the user is at the PC (an urgent stage breaks through quiet hours)
TOLD_S = 3 * 3600.0  # an offer about an item she brought up waits this long (``told``)
KEEP_S = 2 * 86400.0  # what was said about an item is kept this long past its time
QUIET_FOR = "quiet hours"
CUE = "deadline"
TURN = "(Deadline) Something of theirs is due soon."


@dataclass(frozen=True)
class Stage:
    """One time to bring an item up: ``before_s`` ahead of its time. ``urgent``: it can't wait (breaks through focus
    mode, goes to the phone too)."""

    name: str
    before_s: float
    urgent: bool = False


def canvas_stages(hours: Sequence[float]) -> tuple[Stage, ...]:
    """The stages of Canvas work from the hours before it's due (``deadlines.canvas_h``), furthest first: the last
    is ``final`` (urgent), the one before it ``soon``, any before that ``ahead``."""
    ordered = sorted({float(h) for h in hours if h > 0}, reverse=True)
    names = ["ahead"] * max(0, len(ordered) - 2) + ["soon", "final"][-len(ordered) :] if ordered else []
    return tuple(
        Stage(name, h * 3600.0, urgent=(i == len(ordered) - 1))
        for i, (name, h) in enumerate(zip(names, ordered, strict=True))
    )


def event_stages(minutes: float) -> tuple[Stage, ...]:
    return (Stage("starting", minutes * 60.0, urgent=True),) if minutes > 0 else ()


@dataclass(frozen=True)
class Due:
    """Something of the user's with a time. ``id`` is stable for the item (a moved deadline keeps it), ``subject``
    its Futures ref (what the heads-up's ``told`` and Futures' offers know it by). ``seen_at``: when Canvas last
    showed it (None for the calendar)."""

    id: str
    title: str
    at: float  # due, or starts
    source: str  # "canvas" or "calendar"
    what: str = ""  # "assignment", "quiz", "event"
    course: str = ""
    subject: str | None = None
    online: bool = False  # a calendar event with a meeting link
    seen_at: float | None = None

    @property
    def key(self) -> str:
        """The item at this time: a deadline that moves is a new key, and its stages start over."""
        return f"{self.id}@{self.at:.0f}"


def reached(item: Due, now: float, stages: Sequence[Stage]) -> int | None:
    """The latest stage ``item`` has reached at ``now`` (an index into ``stages``), or None: not yet, or its time has
    passed (nothing is said about what's over)."""
    left = item.at - now
    if left <= 0:
        return None
    found = None
    for i, stage in enumerate(stages):
        if left <= stage.before_s:
            found = i
    return found


def from_planner(planner: Mapping[str, Any], seen_at: float | None, now: float, ref: Callable[[str], str]) -> list[Due]:
    """The open Canvas work still to come in the Futures Canvas watch's planner snapshot (its whole window, so what's
    new in it can be told from what only came closer). ``ref`` turns a planner key into its Futures ref
    (``ref("assignment", key)``)."""
    out = []
    for key, item in planner.items():
        if not isinstance(item, Mapping) or item.get("done"):
            continue
        due = item.get("due_ts")
        if not isinstance(due, int | float) or isinstance(due, bool) or due <= now:
            continue
        out.append(
            Due(
                id=f"canvas:{key}",
                title=" ".join(str(item.get("title") or "").split()) or "(no name)",
                at=float(due),
                source="canvas",
                what=str(item.get("kind") or "assignment"),
                course=course_name(str(item.get("course") or "")),
                subject=ref(key),
                seen_at=seen_at,
            )
        )
    return out


def from_calendar(upcoming: Sequence[Any], now: float) -> list[Due]:
    """Timed events coming up, from the Futures calendar watch (``sources.Upcoming``)."""
    return [
        Due(
            id=f"calendar:{u.key.rsplit('@', 1)[0]}",
            title=" ".join(str(u.title or "").split()) or "(no name)",
            at=float(u.start),
            source="calendar",
            what="event",
            subject=f"calendar:{u.key}",
            online=bool(getattr(u, "online", False)),
        )
        for u in upcoming
        if float(u.start) > now
    ]


class Ledger:
    """Which stage of each item was brought up (by item and time), and when she last brought an item up, in one JSON
    file so a restart repeats nothing. Also when each item was first seen. Read from Futures' worker thread too
    (``told``): a dict replaced whole, never changed in place."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._said: dict[str, dict[str, Any]] = {}  # key -> {"stage": i, "at": ts, "due": ts, "how": ...}
        self._told: dict[str, float] = {}  # subject -> when she brought it up
        self._seen: dict[str, float] = {}  # key -> first seen
        self._warned: dict[str, float] = {}  # a deadline brought up early because the week doesn't fit it -> when
        data = read_json(path, "her deadline alerts") if path is not None else None
        if isinstance(data, dict):
            self._said = {k: v for k, v in (data.get("said") or {}).items() if isinstance(v, dict)}
            self._told = {k: float(v) for k, v in (data.get("told") or {}).items() if isinstance(v, int | float)}
            self._seen = {k: float(v) for k, v in (data.get("seen") or {}).items() if isinstance(v, int | float)}
            self._warned = {k: float(v) for k, v in (data.get("warned") or {}).items() if isinstance(v, int | float)}

    def stage(self, item: Due) -> int:
        """The latest stage brought up for ``item`` at its current time; -1 for none."""
        entry = self._said.get(item.key)
        return int(entry.get("stage", -1)) if entry else -1

    def first_seen(self, item: Due, now: float, *, baseline: bool = False) -> float:
        """When ``item`` was first seen; on the watch's first look (``baseline``) an item not seen before counts as
        long known (it was there before she started, not posted just now)."""
        seen = self._seen.get(item.key)
        if seen is None:
            seen = now - NEW_WAIT_S if baseline else now
            self._seen = {**self._seen, item.key: seen}
        return seen

    def mark(self, item: Due, stage: int, how: str, now: float) -> None:
        self._said = {**self._said, item.key: {"stage": stage, "at": now, "due": item.at, "how": how}}
        if how == "said" and item.subject:
            self._told = {**self._told, item.subject: now}

    def warned(self, ref: str, due: float) -> bool:
        """Whether this deadline was already brought up early (a moved deadline starts over)."""
        return f"{ref}@{int(due)}" in self._warned

    def warn(self, ref: str, due: float, now: float) -> None:
        self._warned[f"{ref}@{int(due)}"] = now

    def said_at(self, item: Due) -> float | None:
        """When an earlier stage of ``item`` was said (not just covered by something else), or None."""
        entry = self._said.get(item.key)
        return float(entry["at"]) if entry and entry.get("how") == "said" else None

    def told(self, subject: str, now: float) -> bool:
        at = self._told.get(subject)
        return at is not None and now - at < TOLD_S

    def save(self, now: float) -> None:
        self._said = {k: v for k, v in self._said.items() if now - float(v.get("due", 0)) < KEEP_S}
        self._told = {k: t for k, t in self._told.items() if now - t < KEEP_S}
        self._seen = {k: t for k, t in self._seen.items() if now - t < KEEP_S + 30 * 86400.0}
        self._warned = {k: t for k, t in self._warned.items() if now - t < KEEP_S + 30 * 86400.0}
        if self.path is None:
            return
        try:
            state = {"said": self._said, "told": self._told, "seen": self._seen, "warned": self._warned}
            write_json(self.path, state, indent=1)
        except OSError:
            log.warning("Couldn't save her deadline alerts", exc_info=True)


# ---------------------------------------------------------------------------------------------------- the words
NOTE = """\
(Deadline) You keep an eye on {user}'s deadlines and bring them up as they get close: they asked you to. Now:
{facts}
Tell {user} out loud now, in a sentence or two of your own: what's due and when, said the way people say times \
("at 11:59 tonight", "in about 40 minutes"). No course codes, no list, no lecture.{final}{earlier} If it'd help, you \
may offer to pull up what it needs (canvas_assignment) or open an online meeting's link (get_calendar_events, then \
open_url); don't do either unasked. Canvas only shows whether it's handed in as of its last look (the time is given): \
never say more than that. If they tell you it's already done, take their word."""

FINAL_NOTE = " It's the last reminder before it's due: keep it short and plain."
EARLIER_NOTE = " You already brought {what} up at {at}: don't repeat yourself, say what's changed (how close it is)."

FACTS = """\
A deadline reminder the user asked for, escalating as it gets close ({stage}): {facts}{earlier} It's said once per \
stage; what Canvas shows handed in is never brought up."""


def _a(word: str) -> str:
    return f"an {word}" if word[:1].lower() in "aeiou" else f"a {word}"


def fact_line(item: Due, stage: Stage, now: float, local_time: Callable[[float], datetime]) -> str:
    """The facts about one item at one stage, for her brain and the judge (never said as written)."""
    when = when_due(item.at, now, local_time)
    if item.source == "calendar":
        link = " It's an online meeting with a link." if item.online else ""
        return f'- "{item.title}" on their calendar starts {when}.{link}'
    course = f" for {item.course}" if item.course else ""
    seen = f" as of {clock(local_time(item.seen_at))}" if item.seen_at is not None else ""
    last = " This is the last reminder before it's due." if stage.name == "final" else ""
    return (
        f'- "{item.title}", {_a(item.what)}{course} in Canvas, due {when}; Canvas showed it not handed in{seen}.{last}'
    )


def plain(item: Due, stage: Stage, now: float, local_time: Callable[[float], datetime]) -> str:
    """What's said as written when her own turn can't run. Each stage worded differently, so the unprompted gate's
    repeat check never takes a closer stage for the one before."""
    when = when_due(item.at, now, local_time)
    if item.source == "calendar":
        return f'"{item.title}" starts {when}.'
    if stage.name == "final":
        return f'Last call: "{item.title}" closes {when}, and Canvas still shows it open.'
    if stage.name == "soon":
        return f'"{item.title}" is due {when}. Canvas still shows it open, so it\'s worth getting to.'
    return f'Coming up: "{item.title}" is due {when}.'


# ---------------------------------------------------------------------------------------------------- the watch
# (turn, note, fallback, relay to the phone, urgent, facts for the judge): her own turn (``tell_in_own_words``)
Tell = Callable[[str, str, str, bool, bool, str], None]
Items = Callable[[], Awaitable[Sequence[Due]]]


RISK_EVERY_S = 6 * 3600.0
WORTH_SAYING = 1 / 3  # missed in this share of the sampled weeks: it bears on a reminder
RISK_NOTE = """\
(Deadline, ahead of time) You sampled how the coming week could go for {user}, from what's due and how long such work \
takes. This doesn't fit even if everything goes fast:
{facts}
Tell {user} in a sentence or two of your own, plainly and without alarm, what doesn't fit and what you'd move. What the \
hours rest on is above: if they're guesses, say so and ask. Say it in words, never a percentage. If they say it's \
fine or already done, take their word."""
RISK_FACTS = "An early heads-up about a deadline that doesn't fit the week: {facts} It's said once for each deadline."
WEEK_NOTE = " How the week looks, sampled (use it only if it bears on this, in words): {facts}"


class Deadlines:
    """Looks every ``CHECK_S`` at what's due (``canvas()``, ``calendar()``), and brings each stage up once through
    ``tell``. ``refresh()`` asks Canvas again now (the Futures Canvas watch's ``check``). ``quiet()``: quiet hours;
    ``focusing()``: focus mode or Do Not Disturb; ``active()``: the user is at the PC; ``rundown_pending()``: the
    morning rundown is still to come today; ``rundown_said()``: it was said today; ``told(subject)``: the heads-up
    brought it up lately. ``start`` it once, ``close`` it at exit; ``check`` is one look."""

    def __init__(
        self,
        tell: Tell,
        ledger: Ledger,
        *,
        canvas: Items | None = None,
        calendar: Items | None = None,
        refresh: Callable[[], Awaitable[Any]] | None = None,
        canvas_h: Sequence[float] = (24.0, 3.0, 0.75),
        event_min: float = 15.0,
        quiet: Callable[[], Awaitable[bool]] | None = None,
        focusing: Callable[[], Awaitable[bool]] | None = None,
        active: Callable[[], Awaitable[bool]] | None = None,
        rundown_pending: Callable[[float], bool] | None = None,
        rundown_said: Callable[[float], bool] | None = None,
        told: Callable[[str], bool] | None = None,
        week: Callable[[], Awaitable[Sequence[Any]]] | None = None,
        user: str = "the user",
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        local_time: Callable[[float], datetime] = datetime.fromtimestamp,
    ) -> None:
        self._tell = tell
        self.ledger = ledger
        self._canvas = canvas
        self._calendar = calendar
        self._refresh = refresh
        self.canvas_stages = canvas_stages(canvas_h)
        self.event_stages = event_stages(event_min)
        self._quiet = quiet
        self._focusing = focusing
        self._active = active
        self._rundown_pending = rundown_pending
        self._rundown_said = rundown_said
        self._heads_up_told = told
        self._week = week
        self._week_at = -RISK_EVERY_S
        self._week_seen: tuple[Any, ...] = ()
        self._canvas_h = tuple(canvas_h)
        self.user = user
        self._clock = clock
        self._sleep = sleep
        self._local_time = local_time
        self._task: asyncio.Task[None] | None = None
        self._looked = False  # the first look is the baseline: what's there then isn't new

    def stages(self, item: Due) -> tuple[Stage, ...]:
        return self.event_stages if item.source == "calendar" else self.canvas_stages

    # ------------------------------------------------------------------ running
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="deadlines")

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
                log.warning("Couldn't look at what's due", exc_info=True)
            await self._sleep(CHECK_S)

    def told(self, subject: str) -> bool:
        """Whether she brought ``subject`` up lately (Futures' offers about it wait). Thread-safe."""
        return self.ledger.told(subject, self._clock())

    # ------------------------------------------------------------------ one look
    async def check(self, *, refreshed: bool = False) -> list[tuple[Due, int]]:
        """One look. Returns what was brought up, as (item, stage index)."""
        now = self._clock()
        items = [*await self._items(self._canvas, "Canvas"), *await self._items(self._calendar, "the calendar")]
        ready: list[tuple[Due, int]] = []
        baseline, self._looked = not self._looked, True
        for item in items:
            first = self.ledger.first_seen(item, now, baseline=baseline)
            stages = self.stages(item)
            i = reached(item, now, stages)
            if i is None or self.ledger.stage(item) >= i:
                continue
            if not stages[i].urgent and now - first < NEW_WAIT_S:
                continue  # just posted: the heads-up about it may be on its way
            covered = self._covered(item, stages[i], now)
            if covered:
                self.ledger.mark(item, i, covered, now)
                log.info("Deadline %s (%s) already covered: %s", item.title[:60], stages[i].name, covered)
                continue
            ready.append((item, i))
        if ready:
            ready = await self._now_or_later(ready)
        if ready and not refreshed and self._stale(ready, now) and self._refresh is not None:
            try:
                await self._refresh()
            except Exception:
                log.warning("Couldn't look at Canvas again before a deadline alert", exc_info=True)
            else:
                return await self.check(refreshed=True)  # what's handed in meanwhile is left out
        if ready:
            self._say(ready, now, await self._week_facts(now))
        else:
            await self._warn_early(now)
        self.ledger.save(now)
        return ready

    # ------------------------------------------------------------------ the week, sampled
    async def _week_risks(self, now: float) -> Sequence[Any]:
        """The week as last sampled (``week()``), at most every ``RISK_EVERY_S``; nothing when it can't be read."""
        if self._week is None:
            return ()
        if now - self._week_at >= RISK_EVERY_S:
            self._week_at = now
            try:
                self._week_seen = tuple(await self._week())
            except Exception:
                log.warning("Couldn't sample the week", exc_info=True)
                self._week_seen = ()
        return self._week_seen

    async def _week_facts(self, now: float) -> str:
        risks = [r for r in await self._week_risks(now) if r.share >= WORTH_SAYING]
        return "; ".join(f'"{r.title}" {r.words} ({r.basis})' for r in risks[:3])

    async def _warn_early(self, now: float) -> None:
        """A deadline past the stages' reach that doesn't fit even if everything goes fast, once, one a look."""
        reach = max((s for s in self._canvas_h), default=24.0) * 3600.0
        risks = [
            r
            for r in await self._week_risks(now)
            if r.even_if_fast and r.due_ts - now > reach and not self.ledger.warned(r.ref, r.due_ts)
        ]
        if not risks or (self._quiet is not None and await self._quiet()):
            return
        if self._focusing is not None and await self._focusing():
            return
        risk = risks[0]
        when = when_due(risk.due_ts, now, self._local_time)
        facts = f'"{risk.title}", due {when}, is {risk.words}, even if everything goes fast. Its hours: {risk.basis}.'
        self.ledger.warn(risk.ref, risk.due_ts, now)
        log.info("Deadline at risk: %s", risk.title[:60])
        try:
            self._tell(
                TURN,
                RISK_NOTE.format(user=self.user, facts=f"- {facts}"),
                f'"{risk.title}" doesn\'t look like it fits this week, due {when}.',
                False,
                False,
                RISK_FACTS.format(facts=facts),
            )
        except Exception:
            log.warning("Couldn't bring up a deadline at risk", exc_info=True)

    async def _items(self, read: Items | None, what: str) -> list[Due]:
        if read is None:
            return []
        try:
            return list(await read())
        except Exception:
            log.warning("Couldn't read what's due from %s", what, exc_info=True)
            return []

    def _covered(self, item: Due, stage: Stage, now: float) -> str | None:
        """Why this stage needn't be said, as something else already said it, or None."""
        if item.subject and self._heads_up_told is not None:
            try:
                if self._heads_up_told(item.subject):
                    return "the heads-up told them"
            except Exception:
                log.debug("Couldn't ask the heads-up what it told", exc_info=True)
        if stage.name == "ahead" and self._rundown_said is not None:
            due_day = self._local_time(item.at).date()
            try:
                if due_day == self._local_time(now).date() and self._rundown_said(now):
                    return "the morning rundown listed it"
            except Exception:
                log.debug("Couldn't tell whether the rundown was said", exc_info=True)
        return None

    async def _now_or_later(self, ready: list[tuple[Due, int]]) -> list[tuple[Due, int]]:
        """What of ``ready`` can be said now: the earlier stages wait out quiet hours, focus and the morning rundown;
        the urgent ones only quiet hours while the user is away from the PC."""
        quiet = await _ask(self._quiet)
        focusing = await _ask(self._focusing)
        pending = False
        if self._rundown_pending is not None:
            try:
                pending = bool(self._rundown_pending(self._clock()))
            except Exception:
                log.debug("Couldn't tell whether the rundown is still to come", exc_info=True)
        urgent = [(item, i) for item, i in ready if self.stages(item)[i].urgent]
        later = [(item, i) for item, i in ready if not self.stages(item)[i].urgent]
        if later and (quiet or focusing or pending):
            log.debug(
                "Deadline alerts wait (%s): %s",
                QUIET_FOR if quiet else "focus" if focusing else "the morning rundown",
                ", ".join(item.title[:40] for item, _ in later),
            )
            later = []
        if urgent and quiet and not await _ask(self._active):
            log.info("Urgent deadline alerts wait: quiet hours, and nobody at the PC")
            urgent = []
        return urgent + later

    @staticmethod
    def _stale(ready: Sequence[tuple[Due, int]], now: float) -> bool:
        return any(item.seen_at is not None and now - item.seen_at > FRESH_S for item, _ in ready)

    def _say(self, ready: list[tuple[Due, int]], now: float, week: str = "") -> None:
        ready.sort(key=lambda pair: pair[0].at)
        local = self._local_time
        facts, fallback, earlier = [], [], []
        urgent = final = False
        names = []
        for item, i in ready:
            stage = self.stages(item)[i]
            urgent = urgent or stage.urgent
            final = final or stage.name == "final"
            facts.append(fact_line(item, stage, now, local))
            fallback.append(plain(item, stage, now, local))
            names.append(stage.name)
            said = self.ledger.said_at(item)
            if said is not None:
                earlier.append((item.title, clock(local(said))))
        said_before = (
            EARLIER_NOTE.format(
                what=" and ".join(f'"{t}"' for t, _ in earlier), at=" and ".join(dict.fromkeys(a for _, a in earlier))
            )
            if earlier
            else ""
        )
        note = NOTE.format(
            user=self.user, facts="\n".join(facts), final=FINAL_NOTE if final else "", earlier=said_before
        ) + (WEEK_NOTE.format(facts=week) if week and not final else "")
        judge = FACTS.format(
            stage=", ".join(dict.fromkeys(names)),
            facts=" ".join(f.removeprefix("- ") for f in facts),
            earlier=f" Earlier stage said at {', '.join(a for _, a in earlier)}." if earlier else "",
        )
        log.info(
            "Deadline alert%s: %s",
            " (urgent)" if urgent else "",
            "; ".join(f"{item.title[:50]} ({self.stages(item)[i].name})" for item, i in ready),
        )
        for item, i in ready:
            self.ledger.mark(item, i, "said", now)
        try:
            self._tell(TURN, note, " ".join(fallback), urgent, urgent, judge)
        except Exception:
            log.warning("Couldn't bring up a deadline", exc_info=True)


async def _ask(check: Callable[[], Awaitable[bool]] | None) -> bool:
    if check is None:
        return False
    try:
        return bool(await check())
    except Exception:
        log.debug("A deadline-alert check failed", exc_info=True)
        return False
