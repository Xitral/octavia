"""Putting Octavia Futures together with the running assistant (``App`` calls these; nothing else knows both).

``start_futures`` opens the database and starts the service: it listens to the event bus (only the event types it
turns into Futures events), and in voice mode the watches of its own run beside it: which app is in front
(``futures.context_watch``), Canvas (``futures.canvas_watch``, only with a Canvas token) and the calendar's timed
events (``futures.calendar_watch``, only with a calendar feed, for offers and deadline alerts). ``connect`` hands it
the assistant's ways of speaking up and of noting things for the brain, and whether the user is in focus mode or Do Not Disturb; ``watch_ticktick`` lets it see every look the
TickTick reminders take at what's due, so it costs no extra requests.

If the database can't be opened (written by a newer version, or damaged), Octavia runs without Futures and says
so once: a problem here must never keep her from starting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from octavia.core.events import EventBus, Notice
from octavia.tools.permissions import Offer

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.futures.engine import FuturesService

log = logging.getLogger(__name__)


@dataclass
class FuturesComponents:
    service: FuturesService
    closers: list[Callable[[], Any]] = field(default_factory=list)  # in the order they should run
    _spawned: set[asyncio.Task[Any]] = field(default_factory=set)
    # Also shown each event the Canvas watch and the TickTick snapshots record (the heads-ups: ``core.heads_up``);
    # set once the assistant runs, on the event loop. Must be quick and never raise.
    news: Callable[[Any], None] | None = None
    canvas: Any = None  # the CanvasWatch, when it runs (deadline alerts read its planner and ask it to look again)
    calendar: Any = None  # the CalendarWatch, when it runs (deadline alerts read what's coming up)

    def record(self, event: Any) -> Any:
        """Where the Canvas watch and the TickTick snapshots hand their events: the service, and ``news``."""
        if self.news is not None:
            try:
                self.news(event)
            except Exception:
                log.warning("Couldn't pass a %s change on for a heads-up", getattr(event, "type", "?"), exc_info=True)
        return self.service.submit(event)

    def spawn(self, work: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(work)
        self._spawned.add(task)
        task.add_done_callback(self._spawned.discard)

    async def settle_spawned(self) -> None:
        """Stop what's still taking in a TickTick look, before the database closes."""
        for task in list(self._spawned):
            task.cancel()
        for task in list(self._spawned):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def start_futures(
    config: AppConfig,
    events: EventBus,
    *,
    voice: bool,
    categories: dict[str, str],
    exclude_pids: Callable[[], frozenset[int]] = frozenset,
) -> FuturesComponents | None:
    """The running Futures service, or None when it's off or can't start."""
    if not config.futures.enabled:
        return None
    try:
        from octavia.futures.engine import FuturesService
        from octavia.futures.ingest import BUS_TYPES

        service = FuturesService(config, categories=categories)
    except Exception as exc:
        log.warning("Octavia Futures couldn't start", exc_info=True)
        events.emit(Notice(source="FUTURES", message=f"Octavia Futures is off this time: {exc}", level=logging.WARNING))
        return None
    parts = FuturesComponents(service)
    unsubscribe = events.subscribe(service.on_event, *BUS_TYPES)
    await service.start(voice=voice)
    parts.closers = [unsubscribe, parts.settle_spawned, service.close]
    if voice and config.futures.context_watch and sys.platform == "win32":
        try:
            from octavia.futures.sources import ContextWatch, read_foreground_app

            controls = service.controls

            def read() -> tuple[str, str] | None:
                # Nothing is even looked at while the computer is off-limits or not recorded.
                if not (controls.allows_source("computer") and controls.allows_learning("computer")):
                    return None
                return read_foreground_app(exclude_pids())

            watch = ContextWatch(events.emit, read=read)
            watch.start()
            parts.closers.insert(0, watch.close)
        except Exception:
            log.warning("The Futures context watch didn't start", exc_info=True)
    if voice and config.futures.canvas_watch:
        try:
            from octavia.futures.sources import CanvasWatch
            from octavia.tools.extensions.canvas import canvas_token

            controls = service.controls
            canvas = CanvasWatch(
                parts.record,
                service.store,
                # Only with a token, and never while the user keeps Canvas or school out of it.
                configured=lambda: (
                    bool(canvas_token()) and controls.allows_source("canvas") and controls.allows_learning("school")
                ),
                every_s=config.futures.canvas_every_min * 60,
            )
            canvas.start()
            parts.canvas = canvas
            parts.closers.insert(0, canvas.close)
        except Exception:
            log.warning("The Futures Canvas watch didn't start", exc_info=True)
    if voice and config.futures.calendar_watch and (config.futures.offers or config.deadlines.enabled):
        try:
            from octavia.futures.sources import CalendarWatch, _calendar_configured

            controls = service.controls
            calendar = CalendarWatch(configured=lambda: controls.allows_source("calendar") and _calendar_configured())
            calendar.start()
            service.hooks.calendar = calendar.current
            parts.calendar = calendar
            parts.closers.insert(0, calendar.close)
        except Exception:
            log.warning("The Futures calendar watch didn't start", exc_info=True)
    return parts


def with_lessons(
    recall: Callable[[str], Awaitable[str | None]] | None, service: FuturesService, budget_s: float = 0.3
) -> Callable[[str], Awaitable[str | None]]:
    """The conversation's per-turn lookup (``Assistant.recall``) with the past cases like what was said
    (``lessons.note``). Side by side, within ``budget_s``: Futures being busy costs the turn its lesson, never time."""

    async def cases(text: str) -> str | None:
        try:
            return await asyncio.wait_for(service.call(service.turn_note, text, service.clock()), budget_s)
        except Exception:
            log.debug("No lessons in time for the turn", exc_info=True)
            return None

    async def found(text: str) -> str | None:
        if recall is None:
            return None
        return await recall(text)

    async def recalled(text: str) -> str | None:
        words, lesson = await asyncio.gather(found(text), cases(text), return_exceptions=True)
        if isinstance(words, BaseException):
            log.warning("Couldn't look up memories for the turn", exc_info=words)
            words = None
        return "\n".join(part for part in (words, lesson) if isinstance(part, str) and part) or None

    return recalled


async def write_strategy(service: FuturesService, ask: Callable[[str, str], Awaitable[str]], budget: Any) -> Any:
    """One night's look (``strategies``): a situation that keeps going wrong gets one tool-less call to write an approach
    to it. ``budget(job)``: why the usage budget says no, or None. Nothing is spent unless there's something to write."""
    from octavia.futures.strategies import WRITER_SYSTEM

    now = service.clock()
    cluster = await service.call(service.strategies.recurring, now)
    if cluster is None:
        return None
    if why := await budget("strategies"):
        log.info("No new strategy tonight: %s", why)
        return None
    reply = await ask(WRITER_SYSTEM, cluster.prompt(now))
    return await service.call(service.strategies.wrote, cluster, reply, service.clock())


def connect(service: FuturesService, assistant: Any) -> None:
    """Let the service speak up (through the proactive gate) and note things for the brain's next turn."""
    service.hooks.announce = lambda text, note, listen, cue: assistant.announce(text, note, listen, cue=cue)
    service.hooks.announce_offer = lambda text, note, listen, cue, tool: assistant.announce(
        text, note, listen, cue=cue, offer=Offer(tool)
    )
    service.hooks.speak = lambda rec, tool: speak(assistant, rec, tool)
    service.hooks.note = assistant.note
    service.hooks.busy = lambda: not assistant.idle
    service.hooks.focusing = focusing
    # Her experiments on herself and the habits she kept: their notes join her turns (futures.tuning).
    from octavia.core.events import BrainStarted, ToneDetected
    from octavia.futures.tuning import Tuner

    tuner = Tuner(min_confidence=service.config.emotion.min_confidence)
    assistant.tuner = tuner
    assistant.events.subscribe(tuner.on_event, ToneDetected, BrainStarted)
    service.hooks.tuning = tuner.update
    service.defer(service.publish_tuning)


URGENT = 0.9  # a remark this urgent can't wait (an exam tomorrow, a meeting starting)
OWN_TURN = "(On my own) Something to bring up."
OWN_NOTE = (
    'You decided to bring this up on your own; nobody asked just now. A draft of it: "{text}"\n{note}'
    "Say it to the user in a sentence or two of your own, with what you know of them (a question when it offers "
    "something). The draft is only the facts and the ask: don't read it out."
)


def speak(assistant: Any, rec: Any, tool: str | None) -> None:
    """One delivery of Futures (``Hooks.speak``). What the user asked to be told ("remind me when") is said as they
    set it up, an asked line; everything else is her own idea, said in her own words (``tell_in_own_words``) with
    the line as written as the fallback. Both pass the unprompted gate first."""
    from octavia.futures.ingest import FUTURES_CUE

    source = f"futures:{rec.kind}"
    urgent = rec.urgency >= URGENT
    if rec.kind == "trigger":  # the user's own "remind me when"
        assistant.announce(
            rec.text,
            rec.note,
            rec.listen,
            cue=FUTURES_CUE,
            offer=Offer(tool) if tool else None,
            kind="asked",
            source=source,
            urgent=urgent,
        )
        return
    note = OWN_NOTE.format(text=rec.text, note=f"{rec.note}\n" if rec.note else "")
    assistant.tell_in_own_words(
        OWN_TURN, note, rec.text, rec.note, cue=FUTURES_CUE, source=source, urgent=urgent, facts=rec.note or ""
    )


_dnd: Any = None


def focusing() -> bool:
    """Whether the user is in focus mode ("let me focus for 45 minutes") or has Windows' Do Not Disturb on: her own
    remarks wait then. Blocking (a system call): the service asks from its worker thread."""
    global _dnd
    from octavia.tools.extensions import focus

    if focus.KEEPER.session is not None:
        return True
    if sys.platform != "win32":
        return False
    if _dnd is None:
        _dnd = focus.WindowsDoNotDisturb()
    return (_dnd.get() or focus.DND_OFF) != focus.DND_OFF


def watch_ticktick(
    parts: FuturesComponents, fetch: Callable[[], Awaitable[Sequence[Any]]], client: Any
) -> Callable[[], Awaitable[Sequence[Any]]]:
    """``fetch`` (a look at the due to-dos), with every successful look also shown to Futures' TickTick snapshots."""
    from octavia.futures.sources import TaskSnapshots, ticktick_verify

    snapshots = TaskSnapshots(
        parts.service.store, parts.record, verify=lambda task_id: ticktick_verify(task_id, client=client)
    )

    controls = parts.service.controls
    parts.service.hooks.task_title = lambda task_id: ticktick_title(client, task_id)

    async def looked() -> Sequence[Any]:
        tasks = await fetch()
        # Nothing is kept (not even the snapshot) while the user keeps TickTick or their to-dos out of it.
        if controls.allows_source("ticktick") and controls.allows_learning("productivity"):
            parts.spawn(_observe(snapshots, tasks))
        return tasks

    return looked


async def ticktick_title(client: Any, task_id: str) -> str | None:
    """A TickTick to-do's name, by its id (``get_task_by_id``: one request, no Claude usage)."""
    from octavia.tools.extensions.ticktick import parse_tasks

    answer = await client.call("get_task_by_id", {"task_id": task_id}, retry=True)
    if not answer.ok:
        return None
    found = next((t for t in parse_tasks(answer.text) if str(t.get("id") or "") == task_id), None)
    title = " ".join(str(found.get("title") or "").split()) if found else ""
    return title or None


async def _observe(snapshots: Any, tasks: Sequence[Any]) -> None:
    try:
        await snapshots.observe(tasks)
    except Exception:
        log.exception("Futures couldn't take in a TickTick look")
