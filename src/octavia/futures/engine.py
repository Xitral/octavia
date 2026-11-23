"""The Futures service: where the loop closes.

    event ──► world model, episode ──► "remind me when" triggers
      │
      └─► outcome (a to-do done on time or not, a tool call that worked, a reply that needed correcting)
            └─► prediction scored ──► hypotheses re-mined (counts, never vibes)
                  └─► the next prediction uses what was learned; a heads-up goes through the proactive gate

``FuturesService`` owns the database and every part of the model. It listens to the assistant's event bus (``on_event``
is O(1): it only queues), and one worker task takes the queue in order, running each step of the pipeline in a
worker thread (``store.run``) so the event loop, and her voice with it, never waits on the disk. Tools and the CLI
reach it through ``call``, which goes through the same queue, so nothing ever runs beside the pipeline.

What the pipeline decides to say goes back to the loop as ``Effects``: announcements (through the proactive gate,
with ``cue="futures"`` so the reaction to them can be measured) and short notes for the brain's next turn.

A failure in any step is logged with ``log.exception`` (the self-improvement error watcher turns that into an
improvement item with the traceback), recorded as a ``futures.failure`` event, and the pipeline carries on with
the next event. Too many failures in a row switch the pipeline off until the next start, rather than filling the
journal. The messages never carry what the event was about, only its type: the evidence goes to a model.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from octavia.core.events import Event, SessionEnded
from octavia.futures import offers
from octavia.futures.episodes import Episodes
from octavia.futures.events import EventLog, FuturesEvent
from octavia.futures.experiments import (
    TEMPLATES,
    ExperimentPolicy,
    ExperimentRefused,
    Experiments,
    task_nudge_eligible,
)
from octavia.futures.explain import Trace
from octavia.futures.habit_rules import HEADS_UP_TIMING, LATE_SHARE, SELF_TEMPLATES
from octavia.futures.habits import Habits
from octavia.futures.hypotheses import Hypotheses, HypothesisChange
from octavia.futures.ingest import FUTURES_CUE, Normalizer
from octavia.futures.lessons import Lessons
from octavia.futures.objectives import Objectives
from octavia.futures.outcomes import Outcomes
from octavia.futures.predictions import Prediction, Predictions
from octavia.futures.privacy import Controls, classify_domain, redact
from octavia.futures.proactive import OFFER_PREFIX, Candidate, GateSettings, ProactiveGate, Recommendation
from octavia.futures.prospective import Trigger, Triggers, reminder_text
from octavia.futures.reasoning import DeterministicReasoner, ReasonerRouter, ReasoningTask
from octavia.futures.schema import STRENGTH_ORDER, HypothesisStatus, Policy, Strength, ref, split_ref
from octavia.futures.selfeval import Performance
from octavia.futures.selfscore import CUT_OFF, PendingReply, SelfScoring, worse
from octavia.futures.simulate import MIN_SAMPLES, Scenario, WeekRisk, WeekWork, WorkItem, describe_week, sample_week
from octavia.futures.sources import part_of_day, time_context
from octavia.futures.stats import sample_strength
from octavia.futures.store import FuturesStore, dumps, loads
from octavia.futures.strategies import Strategies
from octavia.futures.tuning import TuningSnapshot
from octavia.futures.world import WorldModel

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.futures.sources import Upcoming

log = logging.getLogger(__name__)

TICK_S = 20.0  # how often time-based things are looked at (overdue, pending remarks): cheap queries only
HEAVY_TICK_S = 60.0  # expiry, experiments, heads-ups: at most this often
GRACE_S = 10 * 60.0  # a to-do ticked off within this after its due time still counts as on time
ENROL_MARGIN_S = 30 * 60.0  # a to-do joins the heads-up timing only while its later heads-up is this far off
REPLY_SETTLED_S = 10 * 60.0  # a reply nobody corrected within this went through
REFIRE_S = 10 * 60.0  # a repeating "remind me when" fires at most this often
MINE_EVERY = 10  # re-look for patterns in tool calls and replies after this many new outcomes of the kind
MAX_QUEUE = 5000
MORNING_WAIT_S = 20.0  # the morning rundown waits this long at most for the remarks held for it
MAX_FAILURES = 25  # this many failures in FAILURE_WINDOW_S and the pipeline stops until the next start
FAILURE_WINDOW_S = 600.0
DAY_S = 86400.0
RECENT_S = 120 * DAY_S  # patterns in her own tool calls and replies: from the last four months only
_PRIORITY = {0: "none", 1: "low", 3: "medium", 5: "high"}


@dataclass
class Hooks:
    """How the service reaches the rest of the assistant (set once the assistant exists; all optional)."""

    announce: Callable[[str, str | None, bool, str | None], None] | None = None  # (text, note, listen, cue)
    # The same, for an offer of help: (text, note, listen, cue, the read-only tool it offers). None: ``announce``.
    announce_offer: Callable[[str, str | None, bool, str | None, str], None] | None = None
    # The whole delivery, when set (``wiring.connect``): (what's due to be said, the read-only tool it offers or
    # None). Her own ideas are said in her own words, and every one passes the unprompted gate first. None: the two
    # above, as written.
    speak: Callable[[Recommendation, str | None], None] | None = None
    note: Callable[[str], None] | None = None  # context for the brain's next turn
    busy: Callable[[], bool] = lambda: False  # not a good moment to speak up (a conversation is going on)
    improvement: Callable[[int], Awaitable[Any]] | None = None  # the self-improvement manager's get(id)
    task_title: Callable[[str], Awaitable[str | None]] | None = None  # a TickTick to-do's name, by its id
    focusing: Callable[[], bool] = lambda: False  # focus mode or Do Not Disturb: her own remarks wait
    calendar: Callable[[], Sequence[Upcoming]] = tuple  # online meetings coming up (``sources.CalendarWatch``)
    # When the day's morning rundown (TickTick's) is due, at a time (epoch seconds in, out), while it's still to come;
    # None otherwise. Her own remarks wait for it, and it says them along (``FuturesService.morning_remarks``). Runs
    # on the worker thread: quick, no I/O.
    morning_rundown: Callable[[float], float | None] = lambda now: None
    calendar_starts: Callable[[], Awaitable[Sequence[float]]] | None = None  # when this week's events start
    # What her turns do differently now (``futures.tuning``): handed the new snapshot, from the worker thread, whenever
    # her experiments on herself or her habits change.
    tuning: Callable[[TuningSnapshot], None] | None = None
    # Whether she brought an item (a Futures ref) up lately on her own (``core.heads_up``): an offer about it would
    # repeat her, so it waits. Runs on the worker thread: quick, thread-safe.
    told: Callable[[str], bool] = lambda subject: False
    # Whether deadline alerts (``core.deadlines``) are running: they bring Canvas work up as it gets close (and may
    # offer the help), so the timed offers about it step aside. Runs on the worker thread: quick.
    deadlines: Callable[[], bool] = lambda: False


@dataclass(frozen=True)
class RemarkSource:
    """Another part of Octavia with things to bring up on its own (her reflections' thoughts, say), asked at every
    look (``HEAVY_TICK_S``) for its candidates, which go through the gate like her own remarks: one way to speak up.
    ``prefix`` is the start of the kinds its candidates have ("thought_"), so it hears back about its own: ``shared``
    gets a candidate's key once it was said, ``declined`` once the user turned it down ("no", "not helpful", "never
    mind"). All three run on the Futures worker thread and must be quick and thread-safe; never INFORM.
    ``offer_tool``: for a remark that offers help ("Want me to open it?"), the read-only tool a yes would use, given its
    key, so the yes goes to the brain with its tools (``Hooks.announce_offer``); None for a plain remark. Runs on the
    event loop, so it must be quick and thread-safe too."""

    prefix: str
    candidates: Callable[[float], Iterable[Candidate]]
    shared: Callable[[str], None] = lambda key: None
    declined: Callable[[str], None] = lambda key: None
    offer_tool: Callable[[str], str | None] = lambda key: None


WORK_HOURS = "work_hours"  # an outcome: how long a piece of work took, as the user said
_WEEK_GUESS_H = {"task": (0.25, 2.0), "assignment": (0.5, 4.0)}  # hours, when there's nothing better: stated as a guess


@dataclass
class Effects:
    """What one step decided, for the event loop to carry out."""

    deliver: list[Recommendation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class FuturesService:
    """Octavia Futures, running."""

    def __init__(
        self,
        config: AppConfig,
        *,
        store: FuturesStore | None = None,
        clock: Callable[[], float] = time.time,
        categories: Mapping[str, str] | None = None,
        local_time: Callable[[float], datetime] = datetime.fromtimestamp,
    ) -> None:
        self.config = config
        self.settings = settings = config.futures
        self.store = store or FuturesStore(config.resolve_path(settings.database), clock=clock)
        self.clock = self.store.clock
        self.local_time = local_time
        store = self.store
        self.controls = Controls(
            store,
            proactive=settings.proactive,
            exclude_sources=settings.exclude_sources,
            off_limits=settings.off_limits,
        )
        self.log = EventLog(store)
        self.world = WorldModel(store)
        self.episodes = Episodes(store)
        self.outcomes = Outcomes(store)
        self.hypotheses = Hypotheses(
            store,
            self.outcomes,
            allows=self.controls.allows_learning,
            suppressed=self.controls.suppressed,
            windows={"tool_call": RECENT_S, "brain_turn": RECENT_S},
        )
        self.predictions = Predictions(store, self.outcomes)
        scoring = config.self_scoring
        self.experiments = Experiments(
            store,
            self.outcomes,
            ExperimentPolicy(self.controls, autonomous_internal=scoring.enabled and scoring.experiments == "own"),
        )
        self.habits = Habits(store)
        self.selfscore = SelfScoring(
            store,
            self.outcomes,
            self.experiments,
            self.habits,
            scoring,
            hears_tone=config.emotion.enabled and config.emotion.note,
        )
        self._tuning_sent: TuningSnapshot | None = None
        self.objectives = Objectives(store)
        self.triggers = Triggers(store)
        self.performance = Performance(store)
        self.gate = ProactiveGate(
            store,
            self.controls,
            GateSettings(
                max_per_hour=settings.max_per_hour,
                max_per_day=settings.max_per_day,
                cooldown_s=settings.cooldown_h * 3600,
                quiet_hours=settings.quiet_hours,
                offers_per_day=settings.offers_per_day,
            ),
            local_time=local_time,
            morning_rundown=lambda now: self.hooks.morning_rundown(now),
            holds_for_morning=self._rundown_can_say,
        )
        self.reasoner = ReasonerRouter([DeterministicReasoner()], performance=_Queued(self))
        self.boot = int(self.clock())
        self.normalizer = Normalizer(
            context=lambda ts: time_context(ts, local_time=local_time),
            categories=dict(categories or {}),
            keep_request_words=settings.keep_request_words,
            model=config.brain.claude.model if config.brain.provider == "claude" else config.brain.provider,
            boot=self.boot,
        )
        self.hooks = Hooks()
        self.voice = False
        self._queue: asyncio.Queue[tuple[Any, ...]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._busy = False
        self._focusing = False
        self._stopped = False
        self._failures: list[float] = []
        self._dropped = False
        self._last_heavy = 0.0
        self._pending_reply: PendingReply | None = None
        self._turn_requests: dict[str, str] = {}  # a turn's words, for its lesson (``lessons``)
        self._turn_claims: dict[str, str] = {}  # a sentence of that turn's her tools contradicted
        self.lessons = Lessons(
            self.store,
            allowed=self.controls.allows_learning,
            enabled=lambda: self.selfscore.settings.enabled and self.selfscore.settings.lessons,
        )
        self.strategies = Strategies(
            self.store,
            self.lessons,
            enabled=lambda: (
                self.selfscore.settings.enabled
                and self.selfscore.settings.lessons
                and self.selfscore.settings.strategies
                and self.selfscore.experimenting
            ),
        )
        self._turn_kinds: dict[str, str] = {}
        self._since_mined: dict[str, int] = {}
        self._noted: dict[str, float] = {}  # "this happened before" notes, by problem signature
        self._pending_candidates: list[Candidate] = []  # found while learning; considered at the next tick
        self._sources: list[RemarkSource] = []  # other parts of Octavia with things to bring up (add_source)

    # ------------------------------------------------------------------ lifecycle
    async def start(self, *, voice: bool) -> None:
        """Start taking events. ``voice``: the voice assistant (she may speak up); chat mode only records."""
        self.voice = voice
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work(), name="futures")
        self._ticker = asyncio.create_task(self._tick_loop(), name="futures-tick")
        self.submit(
            FuturesEvent(
                type="assistant.started",
                source="assistant",
                data={"mode": "voice" if voice else "chat"},
                context=time_context(self.clock(), local_time=self.local_time),
            )
        )

    async def close(self) -> None:
        """Record the stop, finish what's queued (briefly) and close the database."""
        if self._ticker is not None:
            self._ticker.cancel()
        if self._queue is not None and self._worker is not None and not self._worker.done():
            self.submit(FuturesEvent(type="assistant.stopped", source="assistant"))
            self._queue.put_nowait(("stop",))
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(self._worker), 5.0)
            self._worker.cancel()
        self._stopped = True
        while self._queue is not None and not self._queue.empty():
            item = self._queue.get_nowait()
            if item[0] == "call" and not item[4].done():
                item[4].cancel()
        with contextlib.suppress(Exception):
            if self.voice:  # a chat beside the voice assistant must not close the voice one's episodes
                await self.store.run(self.episodes.close_all, self.clock())
        self.store.close()

    # ------------------------------------------------------------------ input
    def on_event(self, event: Event) -> None:
        """The event bus subscriber: just queues (it runs inline wherever the event was emitted)."""
        self._put(("bus", event))

    def submit(self, event: FuturesEvent) -> None:
        """Record an event from a source of Futures' own (TickTick snapshots, Canvas, the app in front). Safe to
        call from any thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._put(("event", event))
        else:
            loop.call_soon_threadsafe(self._put, ("event", event))

    def _put(self, item: tuple[Any, ...]) -> None:
        if self._queue is None or self._stopped:
            return
        if self._queue.qsize() >= MAX_QUEUE:
            if not self._dropped:
                log.warning("Futures is falling behind; dropping events until it catches up")
                self._dropped = True
            return
        self._dropped = False
        self._queue.put_nowait(item)

    async def call(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` (synchronous domain code) in the pipeline's turn: after everything queued before it, never
        beside it. How tools and the prompt reach the model."""
        if self._queue is None or self._worker is None or self._worker.done():
            return await self.store.run(fn, *args, **kwargs)  # not running (the CLI, tests): nothing to wait for
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(("call", fn, args, kwargs, future))
        return await future

    def defer(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        """Run ``fn`` in the pipeline's turn, later, without waiting for it (bookkeeping from the event loop)."""
        if self._queue is None or self._loop is None:
            return
        future = self._loop.create_future()
        future.add_done_callback(_log_failure)
        self._put(("call", fn, args, kwargs, future))

    async def settle(self) -> None:
        """Return once everything queued so far has been taken in."""
        await self.call(lambda: None)

    async def tick(self, now: float | None = None) -> None:
        """Look at what time passing changes (overdue, pending remarks, experiments) now, rather than at the next
        tick, and return once that's done."""
        self._put(("tick", self.clock() if now is None else now))
        await self.settle()

    # ------------------------------------------------------------------ the worker
    async def _work(self) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            kind = item[0]
            if kind == "stop":
                return
            if kind == "call":
                _, fn, args, kwargs, future = item
                try:
                    result = await self.store.run(fn, *args, **kwargs)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        future.set_result(result)
                continue
            if self._stopped:
                continue
            try:
                self._busy = self._is_busy()
                if kind == "bus":
                    await self._on_bus(item[1])
                elif kind == "event":
                    await self._run(self._record, item[1])
                elif kind == "tick":
                    await self._run(self._tick, item[1])
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failed(item)

    async def _on_bus(self, event: Event) -> None:
        if isinstance(event, SessionEnded):
            await self._run(self._session_over, event.session_id)
        for fevent in self.normalizer.normalize(event):
            if fevent.type.startswith("build.") and self.hooks.improvement is not None:
                fevent = await self._with_build_details(fevent)
            elif fevent.type == "task.completed" and not fevent.data.get("title") and self.hooks.task_title:
                fevent = await self._with_task_title(fevent)
            await self._run(self._record, fevent)

    async def _with_task_title(self, event: FuturesEvent) -> FuturesEvent:
        """A to-do ticked off by id alone that she has never seen, while a "when I tick off..." reminder waits: its
        name, asked of TickTick (one request, no Claude usage), so the reminder can match."""
        if not await self.store.run(self._title_needed, event):
            return event
        try:
            title = await self.hooks.task_title(split_ref(event.subject or "")[1])  # type: ignore[misc]
        except Exception:
            log.debug("Couldn't look up a to-do's name", exc_info=True)
            return event
        return replace(event, data={**event.data, "title": redact(title, 120)}) if title else event

    def _title_needed(self, event: FuturesEvent) -> bool:
        if not event.subject or self._known_title(event):
            return False
        return any(t.kind == "task_completed" for t in self.triggers.armed())

    async def _run(self, step: Callable[..., None], *args: Any) -> None:
        effects = Effects()
        await self.store.run(step, *args, effects)
        self._carry_out(effects)

    def _carry_out(self, effects: Effects) -> None:
        for rec in effects.deliver:
            if self.hooks.announce is not None and (self.voice or rec.policy is Policy.INFORM):
                tool = offers.OFFER_TOOLS.get(rec.kind) or self._source_offer_tool(rec)
                if self.hooks.speak is not None:
                    self.hooks.speak(rec, tool)
                elif tool is not None and self.hooks.announce_offer is not None:
                    self.hooks.announce_offer(rec.text, rec.note, rec.listen, FUTURES_CUE, tool)
                else:
                    self.hooks.announce(rec.text, rec.note, rec.listen, FUTURES_CUE)
        for note in effects.notes:
            if self.hooks.note is not None:
                self.hooks.note(note)

    def _source_offer_tool(self, rec: Recommendation) -> str | None:
        for source in self._sources:
            if source.prefix and rec.kind.startswith(source.prefix):
                try:
                    return source.offer_tool(rec.key)
                except Exception:
                    log.warning("A remark source failed to name the tool of %s", rec.kind, exc_info=True)
        return None

    def _is_busy(self) -> bool:
        if self.voice and self.hooks.announce is None:
            return True  # not connected to her voice yet (still starting): what's due waits for it
        try:
            return bool(self.hooks.busy())
        except Exception:
            return True  # a check that breaks counts as not a good moment

    def _is_focusing(self) -> bool:
        try:
            return bool(self.hooks.focusing())
        except Exception:
            return False  # a check that breaks says nothing about the user; the other checks still hold

    def _failed(self, item: tuple[Any, ...]) -> None:
        what = type(item[1]).__name__ if item[0] == "bus" else getattr(item[1], "type", item[0])
        log.exception("Futures couldn't process %s", what)
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW_S] + [now]
        with contextlib.suppress(Exception):
            self.log.append(
                FuturesEvent(type="futures.failure", source="futures", data={"stage": item[0], "what": str(what)})
            )
        if len(self._failures) >= MAX_FAILURES:
            self._stopped = True
            log.warning(
                "Futures stopped after %d failures in a few minutes; it starts again with Octavia", MAX_FAILURES
            )

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            self._put(("tick", self.clock()))

    async def _with_build_details(self, event: FuturesEvent) -> FuturesEvent:
        """A build event with what the self-improvement journal knows: tests passed and failed, cost."""
        try:
            _, key = split_ref(event.subject or "")
            item = await self.hooks.improvement(int(key))  # type: ignore[misc]
        except Exception:
            log.debug("Couldn't read the build's details", exc_info=True)
            return event
        report = getattr(item, "report", None) or {}
        data = dict(event.data)
        for check in (report.get("gate") or {}).get("checks") or []:
            if check.get("name") == "pytest":
                summary = str(check.get("summary", ""))
                for word, key_name in (("passed", "tests_passed"), ("failed", "tests_failed")):
                    number = _count_before(summary, word)
                    if number is not None:
                        data[key_name] = number
        build = report.get("build") or {}
        if build.get("cost_usd") is not None:
            data["cost_usd"] = build["cost_usd"]
        if build.get("duration_s") is not None:
            data["build_s"] = build["duration_s"]
        return replace(event, data=data)

    # ------------------------------------------------------------------ the pipeline (worker thread)
    def _record(self, event: FuturesEvent, fx: Effects) -> None:
        event = self._protect(self.log.prepare(event))
        if not self.controls.allows_event(event):
            return
        stored = self.log.append(event)
        if stored is None:
            return  # the same happening, seen again
        self.world.apply(stored)
        if self.voice or "session" not in stored.context:
            self.episodes.observe(stored)
        if not stored.data.get("baseline"):
            for trigger in self.triggers.match(self._for_matching(stored)):
                if self.controls.muted("trigger", stored.ts):
                    break  # the user muted reminders: they stay armed, and nothing is written
                if trigger.fired_at is not None and stored.ts - trigger.fired_at < REFIRE_S:
                    continue  # the same opening seen twice (her tool, then the app in front), or a quick return
                self._fire(trigger, stored, fx)
        rule = _RULES.get(stored.type)
        if rule is not None:
            rule(self, stored, fx)

    def _for_matching(self, event: FuturesEvent) -> FuturesEvent:
        """The event as "remind me when" conditions see it: a to-do she ticked off herself carries only its id, so
        its title comes from the world model (not stored: only for matching)."""
        if event.type.startswith("task.") and not event.data.get("title") and event.subject:
            entity = self.world.get(event.subject)
            if entity is not None:
                return replace(event, data={**event.data, "title": entity.name})
        return event

    def _protect(self, event: FuturesEvent) -> FuturesEvent:
        """The privacy step before anything is kept: a request or a to-do whose words put it in a protected domain
        (health, money, relationships, work) is filed there, so that domain's controls reach it; and which app was in
        front is dropped when the computer is off-limits or not recorded."""
        if event.type in _WORDED and event.domain in _REFILED_FROM:
            known = self._known_title(event)
            found = classify_domain(event.data.get("text"), event.data.get("title"), known)
            entity = self.world.get(event.subject) if found is None and event.subject else None
            if entity is not None and entity.domain not in _REFILED_FROM:
                found = entity.domain  # filed there before (by its words): an event without any doesn't undo it
            if found is not None:
                sensitivity = "sensitive" if found in ("health", "finance") else event.sensitivity
                event = replace(event, domain=found, sensitivity=sensitivity)
        if "app" in event.context and not (
            self.controls.allows_source("computer") and self.controls.allows_learning("computer")
        ):
            event = replace(
                event,
                context={k: v for k, v in event.context.items() if k != "app"},
                data={k: v for k, v in event.data.items() if k != "app"},
            )
        return event

    def _known_title(self, event: FuturesEvent) -> str:
        """What a to-do is called, when the event itself doesn't say (her tool acted on it by id): from the world
        model, or TickTick's last look."""
        if not event.type.startswith("task.") or event.data.get("title") or not event.subject:
            return ""
        entity = self.world.get(event.subject)
        if entity is not None and entity.name != split_ref(event.subject)[1]:
            return entity.name
        snapshot = self.store.kv_get("ticktick", "snapshot", {}) or {}
        seen = snapshot.get(split_ref(event.subject)[1]) if isinstance(snapshot, dict) else None
        return str(seen.get("title") or "") if isinstance(seen, dict) else ""

    def _internal(self, kind: str, subject: str, *, data: Mapping[str, Any] | None = None, cause: str | None = None):
        """An event of Futures' own (a prediction made, a trigger fired): kept in the stream, so "what happened"
        includes what she concluded and when."""
        return self.log.append(
            FuturesEvent(type=kind, source="futures", subject=subject, data=dict(data or {}), caused_by=cause)
        )

    # --- to-dos -------------------------------------------------------------------------------------------
    def _task_seen(self, event: FuturesEvent, fx: Effects) -> None:
        due = event.data.get("due_ts")
        if due is not None:
            self._replace_predictions(event, due)  # seen again after being out of sight, with another date
            self._predict_task(event, float(due))

    def _task_updated(self, event: FuturesEvent, fx: Effects) -> None:
        if "due_ts" not in event.data:
            return
        due = event.data.get("due_ts")
        self._replace_predictions(event, due)
        if due is not None:
            self._predict_task(event, float(due))

    def _replace_predictions(self, event: FuturesEvent, due: Any) -> None:
        """A to-do's due date moved: its prediction for the old date is cancelled, or, when the old date had already
        passed (the to-do was pushed back after it slipped), settled as a miss."""
        entity = self.world.get(event.subject or "")
        attrs = dict(entity.attrs) if entity else {}
        for prediction in self._open_for(event.subject, "task_on_time"):
            if due is not None and prediction.key == _task_key(event.subject, float(due)):
                continue
            old = _due_of(prediction.key)
            if old is not None and event.ts > _deadline(old, _all_day(prediction, attrs)) + GRACE_S:
                self._settle_task(prediction, False, event, attrs, lateness_h=None)
            else:
                self.predictions.cancel(prediction.key, reason="rescheduled")
                self._withdraw_heads_up(prediction.key, "rescheduled")

    def _predict_task(self, event: FuturesEvent, due: float) -> None:
        entity = self.world.get(event.subject or "")
        attrs = dict(entity.attrs) if entity else {}
        all_day = bool(event.data.get("all_day", attrs.get("all_day", False)))
        if _deadline(due, all_day) <= event.ts:
            return  # already past: nothing left to predict
        title = event.data.get("title") or (entity.name if entity else "")
        if not title or title == split_ref(event.subject or "")[1]:
            return  # known only by its id: nothing to say about it that would mean anything
        similar = {"due_part_of_day": self._due_part(due, all_day)}
        prediction = self.predictions.predict(
            "task_on_time",
            event.subject or "",
            key=_task_key(event.subject, due),
            statement=f'"{title}" will be done by {self._when(due, all_day)}',
            metric="task_on_time",
            resolve_by=_deadline(due, all_day),
            features=similar,
            event_id=event.id,
            noun="to-dos",
        )
        if prediction is not None:
            self._predicted(prediction, event)

    def _predicted(self, prediction: Prediction, event: FuturesEvent) -> None:
        """A prediction made: in the stream, and in the world model as what it's about."""
        self._internal(
            "prediction.created",
            f"prediction:{prediction.id}",
            data={"kind": prediction.kind, "probability": round(prediction.probability, 2)},
            cause=event.id,
        )
        if prediction.subject:
            self.world.relate(f"prediction:{prediction.id}", "concerns", prediction.subject, event_id=event.id)

    def _task_done(self, event: FuturesEvent, fx: Effects) -> None:
        done_at = float(event.data.get("completed_ts") or event.ts)
        entity = self.world.get(event.subject or "")
        attrs = dict(entity.attrs) if entity else {}
        for prediction in self._open_for(event.subject, "task_on_time"):
            due = _due_of(prediction.key)
            if due is None:
                continue
            all_day = _all_day(prediction, attrs)
            on_time = done_at <= _deadline(due, all_day) + GRACE_S
            self.store.kv_delete("overdue", prediction.key)
            self._settle_task(prediction, on_time, event, attrs, lateness_h=(done_at - _deadline(due, all_day)) / 3600)
        for rec in self._recommendations_for(event.subject, kind="heads_up", since=event.ts - DAY_S):
            if rec.response is None:
                followed = self.gate.respond(rec.id, "followed", now=event.ts)
                if followed is not None:
                    self._record_response(followed, "followed", event.id)

    def _task_overdue(self, event: FuturesEvent, fx: Effects) -> None:
        """TickTick showed it still open past its due time. Past the grace too, it's a miss now; otherwise it
        becomes one at the first tick after the grace unless it's ticked off first (``_missed``)."""
        entity = self.world.get(event.subject or "")
        attrs = dict(entity.attrs) if entity else {}
        for prediction in self._open_for(event.subject, "task_on_time"):
            due = _due_of(prediction.key)
            if due is None:
                continue
            if event.ts > _deadline(due, _all_day(prediction, attrs)) + GRACE_S + self._look_interval():
                self._settle_task(prediction, False, event, attrs, lateness_h=None)
            else:
                self.store.kv_set("overdue", prediction.key, {"event": event.id, "ts": event.ts})

    def _look_interval(self) -> float:
        """How long until TickTick's next look can report a to-do ticked off in the app (plus a margin)."""
        return self.config.ticktick.check_every_min * 60 + 90

    def _missed(self, now: float) -> None:
        """To-dos seen still open past their due time and not ticked off within the grace since: missed."""
        for prediction in self.predictions.open("task_on_time"):
            seen = self.store.kv_get("overdue", prediction.key)
            due = _due_of(prediction.key)
            if not seen or due is None:
                continue
            entity = self.world.get(prediction.subject or "")
            attrs = dict(entity.attrs) if entity else {}
            if now > _deadline(due, _all_day(prediction, attrs)) + GRACE_S + self._look_interval():
                event = self.log.get(str(seen.get("event"))) or FuturesEvent(
                    type="task.overdue", source="futures", ts=now, subject=prediction.subject
                )
                self._settle_task(prediction, False, replace(event, ts=now), attrs, lateness_h=None)
                self.store.kv_delete("overdue", prediction.key)

    def _task_deleted(self, event: FuturesEvent, fx: Effects) -> None:
        for prediction in self._open_for(event.subject, "task_on_time"):
            self.predictions.cancel(prediction.key, reason="deleted")
            self._withdraw_heads_up(prediction.key, "deleted")

    def _settle_task(
        self, prediction: Prediction, on_time: bool, event: FuturesEvent, attrs: Mapping[str, Any], lateness_h: Any
    ) -> None:
        resolved = self.predictions.resolve(prediction.key, occurred=on_time, event_id=event.id)
        if resolved is None:
            return
        self._withdraw_heads_up(prediction.key, "no longer due")
        self._internal(
            "prediction.resolved",
            f"prediction:{resolved.id}",
            data={"occurred": on_time, "correct": resolved.correct},
            cause=event.id,
        )
        nudged = self._was_nudged(prediction.key)
        features = self._task_features(attrs, _due_of(prediction.key) or event.ts) | {
            "nudged": "yes" if nudged else "no"
        }
        outcome = self.outcomes.record(
            "task_on_time",
            prediction.subject or "",
            key=prediction.key,
            success=on_time,
            expected="done by its due time",
            actual="on time" if on_time else ("late" if lateness_h is not None else "not done by its due time"),
            value=round(lateness_h, 2) if lateness_h is not None else None,
            started_at=prediction.created_at,
            resolved_at=event.ts,
            features=features,
            event_id=event.id,
            domain=str(self._domain_of(prediction.subject) or "productivity"),
        )
        if outcome is None:
            return
        for experiment in self.experiments.running(metric="task_on_time"):
            variant = self.experiments.variant(experiment.id, prediction.key)
            if (variant == "nudge" and not nudged) or (variant == "no_nudge" and nudged):
                continue  # what was assigned isn't what happened: left out of both groups' counts
            self.experiments.record(experiment.id, prediction.key, success=on_time, outcome_id=outcome.id)
        self._mine("task_on_time", event)

    def _domain_of(self, subject: str | None) -> str | None:
        entity = self.world.get(subject or "")
        return entity.domain if entity is not None else None

    def _was_nudged(self, prediction_key: str) -> bool:
        """Did she give a heads-up about this to-do (this due date of it)?"""
        row = self.store.one(
            "SELECT 1 FROM recommendations WHERE key=? AND status='delivered'", (f"heads_up:{prediction_key}",)
        )
        return row is not None

    def _task_features(self, attrs: Mapping[str, Any], due: float) -> dict[str, str]:
        all_day = bool(attrs.get("all_day", False))
        local = self.local_time(due)
        return {
            "due_part_of_day": self._due_part(due, all_day),
            "day_type": "weekend" if local.weekday() >= 5 else "weekday",
            "priority": _PRIORITY.get(attrs.get("priority") or 0, "none"),
            "created_by": str(attrs.get("created_by") or attrs.get("by") or "ticktick"),
        }

    def _due_part(self, due: float, all_day: bool) -> str:
        return "all_day" if all_day else part_of_day(self.local_time(due).hour)

    def _when(self, ts: float, all_day: bool = False) -> str:
        moment = self.local_time(ts)
        today = self.local_time(self.clock()).date()
        day = (
            "today"
            if moment.date() == today
            else "tomorrow"
            if moment.date() == today + timedelta(days=1)
            else moment.strftime("%A %B %d").replace(" 0", " ")
        )
        return day if all_day else f"{moment.strftime('%I:%M %p').lstrip('0')} {day}"

    def _open_for(self, subject: str | None, kind: str) -> list[Prediction]:
        if not subject:
            return []
        rows = self.store.execute(
            "SELECT key FROM predictions WHERE status='open' AND kind=? AND subject=?", (kind, subject)
        )
        found = (self.predictions.by_key(r["key"]) for r in rows)
        return [p for p in found if p is not None]

    def _recommendations_for(
        self, subject: str | None, *, kind: str, since: float | None = None, delivered: bool = True
    ) -> list[Recommendation]:
        if not subject:
            return []
        sql = "SELECT id FROM recommendations WHERE subject=? AND kind=?"
        params: list[Any] = [subject, kind]
        if delivered:
            sql += " AND status='delivered'"
        if since is not None:
            sql += " AND created_at>=?"
            params.append(since)
        found = (self.gate.get(r["id"]) for r in self.store.execute(sql, params))
        return [r for r in found if r is not None]

    # --- her own actions and replies -----------------------------------------------------------------------
    def _action(self, event: FuturesEvent, fx: Effects) -> None:
        data = event.data
        tool = str(data.get("tool") or "")
        success = event.type == "computer.action_performed"
        app = data.get("app")
        self.performance.record(
            "tool",
            tool,
            success=success,
            category=str(data.get("category") or ""),
            failure=data.get("failure"),
            latency_s=event.duration_s,
            retries=1 if data.get("retry") else 0,
            turn=event.context.get("turn"),
            app=app,
            event_id=event.id,
            ts=event.ts,
        )
        self.outcomes.record(
            "tool_call",
            event.subject or ref("skill", tool),
            key=f"tool:{event.id}",
            success=success,
            retries=1 if data.get("retry") else 0,
            resolved_at=event.ts,
            features={
                "tool": tool,
                "app": app,
                "category": data.get("category"),
                "part_of_day": event.context.get("part_of_day"),
            },
            event_id=event.id,
            domain="computer",
        )
        self._mine_every("tool_call", event)
        if success:
            self._followed_offer(tool, event)
        if not success and event.context.get("turn") and self.hooks.note is not None:
            self._this_happened_before(tool, str(data.get("failure") or ""), app, event, fx)

    def _this_happened_before(self, tool: str, failure: str, app: Any, event: FuturesEvent, fx: Effects) -> None:
        """A failure like one she got past before: tell the brain what worked then (once a day per kind)."""
        signature = " ".join(w for w in (tool, failure, str(app or "").replace(" ", "_")) if w).lower()
        if event.ts - self._noted.get(signature, 0.0) < DAY_S:
            return
        learned = self.episodes.resolution_for(signature)
        if learned:
            self._noted[signature] = event.ts
            fx.notes.append(f"(Octavia Futures) {tool} just failed in a way it has before; what worked then: {learned}")

    def _request(self, event: FuturesEvent, fx: Effects) -> None:
        self._settle_reply(corrected=False, now=event.ts, next_words=str(event.data.get("text") or ""))
        turn = event.context.get("turn")
        if turn:
            self._turn_kinds[str(turn)] = str(event.data.get("kind") or "other")
            if len(self._turn_kinds) > 200:
                self._turn_kinds.pop(next(iter(self._turn_kinds)))
            self._turn_requests[str(turn)] = str(event.data.get("text") or "")
            self.strategies.started(str(turn), str(event.data.get("text") or ""), event.ts)
            if len(self._turn_requests) > 200:
                self._turn_requests.pop(next(iter(self._turn_requests)))

    def turn_note(self, text: str, now: float) -> str | None:
        """What her record says for this turn (``wiring.with_lessons``): the past cases, and an approach being tried."""
        parts = (self.lessons.note(text, now), self.strategies.note(text, now))
        return "\n".join(part for part in parts if part) or None

    def task_ended(self, kind: str, task_id: object, title: str, instruction: str, status: str, report: str) -> None:
        """A screen or web task ended: a case of its own, and a point against the approach of the turn that began it."""
        case = self.lessons.from_task(kind, task_id, title, instruction, status, report)
        if case is not None:
            self.strategies.task_ended(case.success, case.ts)

    def _claim_corrected(self, event: FuturesEvent, fx: Effects) -> None:
        self._turn_claims[str(event.data.get("turn") or "")] = str(event.data.get("sentence") or "")
        if len(self._turn_claims) > 50:
            self._turn_claims.pop(next(iter(self._turn_claims)))

    def _correction(self, event: FuturesEvent, fx: Effects) -> None:
        turn = event.data.get("about_turn")
        if turn:
            self.performance.mark_corrected(str(turn))
        self._settle_reply(corrected=True, now=event.ts, next_words=str(event.data.get("text") or ""))

    def _reaction(self, event: FuturesEvent, fx: Effects) -> None:
        """How the user took her last reply (``selfscore.reaction``): kept on it until the reply settles."""
        pending = self._pending_reply
        about = str(event.data.get("about_turn") or "")
        if pending is None or (about and about != pending.turn):
            return
        pending.how = worse(pending.how, str(event.data.get("how") or "") or None)

    def _reply(self, event: FuturesEvent, fx: Effects) -> None:
        data = event.data
        turn = str(data.get("turn") or event.context.get("turn") or "")
        tools = self.store.one("SELECT COUNT(*) AS n FROM performance WHERE kind='tool' AND turn=?", (turn,))
        self.performance.record(
            "brain",
            str(data.get("model") or "brain"),
            success=event.outcome != "failure",
            category=self._turn_kinds.get(turn, ""),
            model=data.get("model") or None,
            latency_s=data.get("first_word_s") or event.duration_s,
            tokens_in=data.get("tokens_in"),
            tokens_out=data.get("tokens_out"),
            tool_calls=int(tools["n"]) if tools and turn else None,
            turn=turn or None,
            event_id=event.id,
            ts=event.ts,
        )
        self._settle_reply(corrected=False, now=event.ts)
        if event.outcome in ("success", "interrupted") and turn:
            tuning = tuple(
                (str(pair[0]), str(pair[1]))
                for pair in data.get("tuning") or ()
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            )
            first = self.store.one(
                "SELECT name FROM performance WHERE kind='tool' AND turn=? ORDER BY ts, id LIMIT 1", (turn,)
            )
            self._pending_reply = PendingReply(
                turn,
                event.id,
                event.ts,
                {
                    "kind": self._turn_kinds.get(turn, "other"),
                    "source": str(data.get("source") or ""),
                    "part_of_day": str(event.context.get("part_of_day") or ""),
                },
                tuning=tuning,
                interrupted=event.outcome == "interrupted",
                tool=str(first["name"]) if first else "",
            )

    def _settle_reply(self, *, corrected: bool, now: float, next_words: str = "") -> None:
        pending, self._pending_reply = self._pending_reply, None
        if pending is None:
            return
        if not pending.interrupted:  # a reply cut off was never "not corrected": the user didn't let it finish
            self.outcomes.record(
                "brain_turn",
                f"turn:{pending.turn}",
                key=f"turn:{pending.turn}",
                success=not corrected,
                expected="no correction",
                actual="corrected" if corrected else "not corrected",
                started_at=pending.ts,
                resolved_at=now,
                features=pending.features,
                event_id=pending.event_id,
                domain="assistant",
            )
            self._mine_every("brain_turn", None)
        how = pending.how  # a correction is among the reactions (a plain "no thanks" isn't, though it starts with no)
        if how is None and pending.interrupted:
            how = CUT_OFF
        self.selfscore.settle(pending, how, now)
        case = self.lessons.from_turn(
            pending,
            how,
            self._turn_requests.pop(pending.turn, ""),
            next_words,
            self._turn_claims.pop(pending.turn, ""),
            now,
        )
        self.strategies.settled(pending.turn, case is None or not case.troubled)  # no case: nothing went wrong

    def _feedback(self, event: FuturesEvent, fx: Effects) -> None:
        if event.data.get("about") != "recommendation":
            return
        answer = _ANSWERS.get(str(event.data.get("how") or ""))
        if answer is not None:
            # A short answer ("yes", "not now", "no") is about a remark that asked something, and only one asked
            # recently: after a plain heads-up, a "no" is about something else.
            latest = self.gate.latest()
            if (
                latest is None
                or (not latest.listen and answer != "deferred")  # "not now" fits a heads-up too
                or latest.response is not None  # answered already: a later "no" is to something else
                or latest.delivered_at is None
                or event.ts - latest.delivered_at > offers.ANSWER_WINDOW_S
            ):
                return
            rec = self.gate.respond(latest.id, answer, now=event.ts)
            if rec is not None:
                self._record_response(rec, answer, event.id)
            return
        response = "accepted" if event.data.get("verdict") == "positive" else "rejected"
        latest = self.gate.latest()
        if (
            latest is not None
            and latest.response is not None
            and (latest.kind.startswith(OFFER_PREFIX) or any(latest.kind.startswith(s.prefix) for s in self._sources))
        ):
            # An offer (or another part's remark) already answered: "never mind" while she reads the rubric out, or
            # "that's all" after she opened the app, isn't a turn-down of it.
            return
        rec = self.gate.respond(None, response, now=event.ts)
        if rec is not None:
            self._record_response(rec, response, event.id)

    def _record_response(self, rec: Recommendation, response: str, cause: str | None) -> None:
        if response == "rejected":
            self._tell_source(rec, "declined")
        self._internal("recommendation.feedback", f"recommendation:{rec.id}", data={"response": response}, cause=cause)
        self.outcomes.record(
            "recommendation",
            f"recommendation:{rec.id}",
            key=f"recommendation:{rec.id}",
            success=response in ("accepted", "followed", "engaged"),
            expected="taken up",
            actual=response,
            started_at=rec.delivered_at,
            features={"kind": rec.kind, "variant": rec.variant},
            domain="futures",
        )

    # --- self-improvement builds ----------------------------------------------------------------------------
    def _build_started(self, event: FuturesEvent, fx: Effects) -> None:
        _, number = split_ref(event.subject or "")
        prediction = self.predictions.predict(
            "build_passes",
            event.subject or "",
            key=f"build:{number}@{int(event.ts)}",
            statement=f'build #{number} ("{event.data.get("title", "")}") will pass its checks',
            metric="build",
            resolve_by=event.ts + 3 * 3600,
            event_id=event.id,
        )
        if prediction is not None:
            self._predicted(prediction, event)

    def _build_over(self, event: FuturesEvent, fx: Effects) -> None:
        passed = event.type == "build.test_passed"
        neutral = event.data.get("reason") == "no_change"  # it decided no change was needed: neither
        for prediction in self._open_for(event.subject, "build_passes"):
            if neutral:
                self.predictions.cancel(prediction.key, reason="no change needed")
                continue
            resolved = self.predictions.resolve(prediction.key, occurred=passed, event_id=event.id)
            if resolved is not None:
                self._internal("prediction.resolved", f"prediction:{resolved.id}", cause=event.id)
        if neutral:
            return
        self.performance.record(
            "build",
            "builder",
            success=passed,
            failure=None if passed else event.data.get("failure"),
            cost_usd=event.data.get("cost_usd"),
            latency_s=event.data.get("build_s"),
            event_id=event.id,
            ts=event.ts,
        )
        self.outcomes.record(
            "build",
            event.subject or "",
            key=f"build:{event.subject}:{event.id}",
            success=passed,
            expected="passes its checks",
            actual=event.type.removeprefix("build."),
            cost_usd=event.data.get("cost_usd"),
            resolved_at=event.ts,
            features={"part_of_day": event.context.get("part_of_day")},
            event_id=event.id,
            domain="self",
        )
        self._mine("build", event)

    # --- learning -------------------------------------------------------------------------------------------
    def _mine_every(self, metric: str, event: FuturesEvent | None) -> None:
        self._since_mined[metric] = self._since_mined.get(metric, 0) + 1
        if self._since_mined[metric] >= MINE_EVERY:
            self._mine(metric, event)

    def _mine(self, metric: str, event: FuturesEvent | None) -> None:
        self._since_mined[metric] = 0
        cause = event.id if event is not None else None
        for change in self.hypotheses.mine(metric, cause=cause):
            self._hypothesis_changed(change, cause)

    def _hypothesis_changed(self, change: HypothesisChange, cause: str | None) -> None:
        h = change.hypothesis
        if change.change == "updated":
            return
        self._internal(
            "hypothesis.changed",
            f"hypothesis:{h.id}",
            data={"change": change.change, "status": h.status, "strength": h.strength, "from": change.old_status},
            cause=cause,
        )
        if (
            h.proposed_experiment
            and h.status in (HypothesisStatus.OBSERVED.value, HypothesisStatus.SUPPORTED.value)
            and self.settings.experiments == "ask"
        ):
            self._offer_experiment(h.proposed_experiment, h.id, h.statement, cause)

    def _offer_experiment(
        self, template: str, hypothesis_id: int | None, statement: str, cause: str | None, *, text: str | None = None
    ) -> None:
        """A pattern that only an experiment can tell cause from coincidence in: propose one (it runs only if the
        user says yes). ``text``: what she says to offer it, when it isn't about a pattern (her experiments on
        herself)."""
        h = self.hypotheses.get(hypothesis_id) if hypothesis_id is not None else None
        pattern = (
            {"id": h.id, "statement": h.statement, "strength": h.strength, "claim": h.claim, "status": h.status}
            if h is not None
            else {}
        )
        earlier = self.experiments.latest(template)
        if earlier is not None and earlier.status in ("rejected", "stopped", "completed"):
            return  # the user already answered (or it already ran): only they bring it up again
        try:
            experiment = self.experiments.propose(template, hypothesis_id=hypothesis_id, cause=cause)
        except (ExperimentRefused, ValueError):
            return
        if hypothesis_id is not None:
            self.world.relate(f"experiment:{experiment.id}", "evaluates", f"hypothesis:{hypothesis_id}", event_id=cause)
        if experiment.status != "proposed" or self.store.one(
            "SELECT 1 FROM recommendations WHERE key=? AND status IN ('delivered','pending')",
            (f"experiment_offer:{experiment.id}",),
        ):
            return
        self._pending_candidates.append(
            Candidate(
                kind="experiment_offer",
                key=f"experiment_offer:{experiment.id}",
                text=text
                or (
                    f"I've noticed something I can't tell cause from coincidence in: {statement} "
                    f"If you like, we could test it for a few weeks: {experiment.title.lower()}. Just say if you're in."
                ),
                policy=Policy.ASK,
                urgency=0.4,
                relevance=0.9,
                domain="futures",
                subject=f"experiment:{experiment.id}",
                note=(
                    f"(Octavia Futures) You offered experiment #{experiment.id} ({experiment.title}). It only starts "
                    "if the user clearly agrees: then call futures_experiment with action approve."
                ),
                trace=Trace(
                    hypotheses=(pattern,) if pattern else (),
                    assumptions=(
                        "the difference could come from something else, which is what the test would show",
                        "nothing changes unless you say yes, and you can stop it any time",
                    ),
                    confidence=str(pattern.get("strength") or "insufficient") if pattern else "insufficient",
                ).to_dict(),
                listen=True,
            )
        )

    # --- "remind me when" -----------------------------------------------------------------------------------
    def _fire(self, trigger: Trigger, event: FuturesEvent, fx: Effects) -> None:
        fired = self.triggers.fire(trigger.id, event_id=event.id, now=event.ts)
        if fired is None:
            return
        self._internal("trigger.fired", f"trigger:{trigger.id}", data={"kind": trigger.kind}, cause=event.id)
        if self._build_news_spoken(trigger, event):
            # The self-improvement manager says out loud that the build is ready (or installed): a reminder of its
            # own would be the same news twice. It's used up all the same, and what the user wanted to hear about it
            # goes to the brain's next turn instead.
            self.store.record_change(f"trigger:{trigger.id}", "merged", cause=event.id, detail={"into": "build news"})
            message = fired.message.strip()
            fx.notes.append(
                f'(Octavia Futures) A "remind me when" the user set just fired ({trigger.description}). The build '
                "announcement already told them, so it wasn't said separately."
                + (f" Their reminder: {message}" if message else "")
            )
            return
        candidate = Candidate(
            kind="trigger",
            key=f"trigger:{trigger.id}:{event.id}",
            text=reminder_text(fired, _what_happened(event)),
            policy=Policy.INFORM,
            urgency=1.0,
            relevance=1.0,
            domain=event.domain or "general",
            sensitivity=event.sensitivity or "personal",
            subject=f"trigger:{trigger.id}",
            note=f'(Octavia Futures) A "remind me when" the user set just fired ({trigger.description}).',
            trace=Trace(
                observations=({"event": event.id, "what": _what_happened(event), "at": event.ts},),
                assumptions=("you still want this reminder",),
                confidence="strong",
            ).to_dict(),
            expires_at=event.ts + 12 * 3600,
            listen=True,
        )
        rec = self._consider(candidate, event.ts, fx)
        if rec.status in ("suppressed", "expired") and fired.once:
            # Never said (muted, off-limits, too late): the reminder isn't used up by that.
            self.store.changed("UPDATE triggers SET status='armed', fired_at=NULL WHERE id=?", (trigger.id,))
            self.store.record_change(f"trigger:{trigger.id}", "re-armed", cause=event.id, detail={"why": rec.reason})

    def _build_news_spoken(self, trigger: Trigger, event: FuturesEvent) -> bool:
        """Whether a "remind me when a build passes" brings news the self-improvement manager already says out loud
        (``announce: speak``). Shown only on the console, the reminder is still said; a failed build's always is."""
        settings = self.config.self_improvement
        return (
            trigger.kind == "build_succeeded"
            and event.type in ("build.test_passed", "build.installed")
            and settings.enabled
            and settings.announce == "speak"
        )

    # --- the proactive gate ---------------------------------------------------------------------------------
    async def morning_remarks(self, wait_s: float = MORNING_WAIT_S) -> list[tuple[str, str | None]]:
        """The remarks of her own that waited for the morning rundown, as (text, note), for the rundown to say along:
        they count as said from now. An offer of help isn't among them (a yes to it needs its own announcement): it's
        said after the rundown, like any other. When the worker is too busy to get to it within ``wait_s``, none
        (they're then said on their own, after the rundown), and none are let go later either."""
        if not self.voice:
            return []
        deadline = time.monotonic() + wait_s / 2
        try:
            return await asyncio.wait_for(self.call(self._release_for_morning, self.clock(), deadline), wait_s)
        except TimeoutError:
            log.info("Futures was too busy to hand its remarks to the morning rundown")
            return []

    def _rundown_can_say(self, kind: str, key: str) -> bool:
        """Whether the morning rundown can say a remark of this kind inside it: not an offer of help."""
        if kind.startswith(OFFER_PREFIX) or offers.OFFER_TOOLS.get(kind) is not None:
            return False
        for source in self._sources:
            if source.prefix and kind.startswith(source.prefix):
                try:
                    return source.offer_tool(key) is None
                except Exception:
                    return False
        return True

    def _release_for_morning(self, now: float, deadline: float) -> list[tuple[str, str | None]]:
        if time.monotonic() > deadline:
            return []  # the rundown stopped waiting for this: it's being said without them
        released = self.gate.release_for_morning(
            now=now, focusing=self._focusing, keep=lambda rec: self._rundown_can_say(rec.kind, rec.key)
        )
        fx = Effects()  # nothing to carry out: the rundown says them
        for rec in released:
            self._delivered(rec, fx)
        return [(rec.text, rec.note) for rec in released]

    def _consider(self, candidate: Candidate, now: float, fx: Effects) -> Recommendation:
        busy = self._busy if self.voice else candidate.policy is not Policy.INFORM
        rec = self.gate.consider(candidate, now=now, busy=busy, focusing=self._focusing)
        if rec.status == "delivered":
            self._delivered(rec, fx)
        return rec

    def _delivered(self, rec: Recommendation, fx: Effects) -> None:
        self._internal("recommendation.delivered", f"recommendation:{rec.id}", data={"kind": rec.kind})
        fx.deliver.append(rec)
        self._tell_source(rec, "shared")

    def _tell_source(self, rec: Recommendation, what: str) -> None:
        for source in self._sources:
            if source.prefix and rec.kind.startswith(source.prefix):
                try:
                    getattr(source, what)(rec.key)
                except Exception:
                    log.warning("A remark source failed to take back %s for %s", what, rec.kind, exc_info=True)

    # --- time passing (worker thread) -----------------------------------------------------------------------
    def _tick(self, now: float, fx: Effects) -> None:
        self._focusing = self._is_focusing()  # here, on the worker thread: reading Do Not Disturb is a system call
        if self._pending_reply is not None and now - self._pending_reply.ts >= REPLY_SETTLED_S:
            self._settle_reply(corrected=False, now=now)
        if now - self._last_heavy >= HEAVY_TICK_S:
            self._last_heavy = now
            self.episodes.close_idle(now)
            self._missed(now)
            self.predictions.expire(now)
            self.triggers.expire(now)
            self._evaluate_experiments(now)
            self._heads_up(now, fx)
            self._offer_help(now, fx)
            self._from_sources(now, fx)
            self._unanswered_offers(now)
            self._daily(now)
            self.lessons.backfill_once(now)
            self.strategies.evaluate(now)
            self._self_experiments(now)
            self._publish_tuning()
        for candidate in self._take_candidates():
            self._consider(candidate, now, fx)
        if self.voice:
            for rec in self.gate.due(now=now, busy=self._busy, focusing=self._focusing):
                self._delivered(rec, fx)
        self._rearm_unsaid()

    def _rearm_unsaid(self) -> None:
        """A "remind me when" that waited and ran out of time without being said isn't used up: arm it again."""
        rows = self.store.execute(
            "SELECT id, subject FROM recommendations WHERE kind='trigger' AND status='expired' AND reason<>'re-armed'"
        )
        for row in rows:
            _, number = split_ref(row["subject"] or "")
            if number.isdigit():
                armed = self.store.changed(
                    "UPDATE triggers SET status='armed', fired_at=NULL WHERE id=? AND status='fired' AND once=1",
                    (int(number),),
                )
                if armed:
                    self.store.record_change(f"trigger:{number}", "re-armed", detail={"why": "never said"})
            self.store.changed("UPDATE recommendations SET reason='re-armed' WHERE id=?", (row["id"],))

    def _withdraw_heads_up(self, prediction_key: str, reason: str) -> None:
        """A heads-up waiting to be said about something that no longer holds (done, moved, deleted, or put in the
        experiment's no-heads-up group) is dropped rather than said late."""
        rows = self.store.execute(
            "SELECT id FROM recommendations WHERE key=? AND status='pending'", (f"heads_up:{prediction_key}",)
        )
        for row in rows:
            self.store.changed(
                "UPDATE recommendations SET status='suppressed', reason=? WHERE id=?", (reason, row["id"])
            )
            self.store.record_change(f"recommendation:{row['id']}", "suppressed", detail={"reason": reason})

    def _take_candidates(self) -> list[Candidate]:
        taken, self._pending_candidates = self._pending_candidates, []
        return taken

    def _daily(self, now: float) -> None:
        last = self.store.kv_get("futures", "daily", 0.0)
        if now - float(last) < DAY_S:
            return
        self.store.kv_set("futures", "daily", now)
        self.hypotheses.mark_stale(now)
        cutoff = now - self.settings.retention_days * DAY_S
        removed = self.log.delete(until=cutoff)
        if removed:
            log.info("Futures: %d events past retention deleted", removed)
        self.lessons.expire(cutoff)

    def _self_experiments(self, now: float) -> None:
        """Her next experiment on herself, when it's time for one (``selfscore.next_template``): one that only
        changes how she talks starts on its own (``self_scoring.experiments: own``), one that changes the user's day
        is offered first, as every personal experiment is."""
        if self.settings.experiments == "off":
            return
        said = self.store.one(
            "SELECT COUNT(*) AS n FROM recommendations WHERE kind='heads_up' AND status='delivered' "
            "AND delivered_at>=?",
            (now - 30 * DAY_S,),
        )
        nudging = any(
            e.key == "task_nudge" for status in ("proposed", "running") for e in self.experiments.list(status=status)
        )
        key = self.selfscore.next_template(now, heads_ups=int(said["n"]) if said else 0, nudging=nudging)
        if key is None:
            return
        scoring = self.config.self_scoring  # live: read when it's used
        self.experiments.policy.autonomous_internal = scoring.enabled and scoring.experiments == "own"
        template = TEMPLATES[key]
        self._offer_experiment(
            key,
            None,
            template.question,
            "selfscore",
            text=f"I'd like to try something on myself, to work better for you. {template.description} Shall I?",
        )
        started = self.experiments.latest(key)
        if started is not None and started.status == "running":
            log.info("Started experiment #%d on herself: %s", started.id, started.title)
            self._publish_tuning()

    def publish_tuning(self) -> None:
        """Hand her turns what applies to them now (``futures.tuning``), when that changed. On the worker thread."""
        self._publish_tuning()

    def _publish_tuning(self) -> None:
        if self.hooks.tuning is None:
            return
        snapshot = self.selfscore.snapshot()
        if snapshot == self._tuning_sent:
            return
        self._tuning_sent = snapshot
        try:
            self.hooks.tuning(snapshot)
        except Exception:
            log.warning("Couldn't hand the turns their tuning", exc_info=True)

    def _evaluate_experiments(self, now: float) -> None:
        for experiment in self.experiments.running():
            if experiment.key in SELF_TEMPLATES and not self.selfscore.experimenting:
                self.experiments.stop(experiment.id, reason="her experiments on herself were switched off")
                self._publish_tuning()
                continue
            if not self.controls.allows_experiment(experiment.domain):
                stopped = self.experiments.stop(experiment.id, reason="its domain is off-limits now")
                self.release_experiment(stopped, "The experiment was stopped: the user put its domain off-limits.")
                continue
            result = self.experiments.evaluate(experiment.id, now=now)
            if result is None:
                continue
            self._internal(
                "experiment.completed",
                f"experiment:{experiment.id}",
                data={"supported": result.supported, "strength": result.strength},
            )
            if result.hypothesis_id is not None:
                hypothesis = self.hypotheses.get(result.hypothesis_id)
                if hypothesis is not None:
                    self.hypotheses.apply_experiment(
                        hypothesis.id,
                        supported=_bears_out(hypothesis, result),
                        strength=result.strength,
                        summary=result.summary,
                        experiment_id=experiment.id,
                    )
            text = f"The experiment you agreed to is done. {result.summary}"
            if experiment.key in SELF_TEMPLATES:
                done = self.experiments.get(experiment.id)
                kept = self.selfscore.completed(
                    experiment, (done.result if done else None) or {}, f"experiment:{experiment.id}"
                )
                if experiment.approved_at is None:  # one she ran on her own
                    text = f"I finished testing {experiment.title.lower()} on myself. {result.summary}"
                if kept:
                    text = kept if experiment.approved_at is None else f"The experiment you agreed to is done. {kept}"
                    self._publish_tuning()
            self._pending_candidates.append(
                Candidate(
                    kind="experiment_result",
                    key=f"experiment_result:{experiment.id}",
                    text=text,
                    policy=Policy.SUGGEST,
                    urgency=0.6,
                    relevance=0.8,
                    domain=experiment.domain,
                    subject=f"experiment:{experiment.id}",
                    note=f"(Octavia Futures) Experiment #{experiment.id} finished: {result.summary}",
                    trace=Trace(
                        observations=tuple(
                            {"event": "", "what": f"{variant}: {tally.describe()}", "at": now}
                            for variant, tally in result.by_variant.items()
                        ),
                        confidence=result.strength,
                    ).to_dict(),
                )
            )

    def _heads_up(self, now: float, fx: Effects) -> None:
        """To-dos due soon that, going by her records, tend to slip: a heads-up, through the gate. During the
        heads-up experiment each eligible to-do takes its turn instead, enrolled only at a moment its heads-up could
        actually be said, so the two groups stay alike."""
        horizon = now + self.settings.heads_up_h * 3600
        late_s = self.settings.heads_up_h * LATE_SHARE * 3600
        nudging = [e for e in self.experiments.running(key="task_nudge") if self.controls.allows_experiment(e.domain)]
        timing = [e for e in self.experiments.running(key=HEADS_UP_TIMING) if self.controls.allows_experiment(e.domain)]
        if not self.selfscore.experimenting:
            timing = []  # switched off: stopped at the next look (``_evaluate_experiments``); meanwhile, as usual
        late_habit = self.selfscore.heads_up_late()
        busy = self._busy or not self.voice
        for prediction in self.predictions.open("task_on_time"):
            due = prediction.resolve_by
            if due is None or not now < due <= horizon:
                continue
            entity = self.world.get(prediction.subject or "")
            if entity is None or entity.status not in (None, "open"):
                continue
            risky = prediction.probability < 0.5 and prediction.trials >= 5
            experiment = nudging[0] if nudging and _eligible_for_experiment(entity, now, self.world) else None
            variant = self.experiments.variant(experiment.id, prediction.key) if experiment else None
            if experiment is not None and variant is None:
                probe = self._heads_up_candidate(prediction, entity, now, due, risky, "nudge", experiment.id)
                if self.gate.check(probe, now=now, busy=busy, focusing=self._focusing)[0] == "delivered":
                    try:
                        variant = self.experiments.assign(experiment.id, prediction.key)
                    except ValueError:  # it has run its course
                        variant = None
                    if variant == "no_nudge":
                        self._withdraw_heads_up(prediction.key, f"experiment #{experiment.id}: no heads-up this time")
            if variant == "no_nudge" or (variant is None and not risky):
                continue
            if variant is None and self._heads_up_waits(
                prediction, entity, now, due, risky, timing[0] if timing else None, late_habit, late_s, busy
            ):
                continue
            self._consider(
                self._heads_up_candidate(
                    prediction, entity, now, due, risky, variant, experiment.id if experiment and variant else None
                ),
                now,
                fx,
            )

    def _heads_up_waits(
        self,
        prediction: Prediction,
        entity: Any,
        now: float,
        due: float,
        risky: bool,
        timing: Any,
        late_habit: bool,
        late_s: float,
        busy: bool,
    ) -> bool:
        """Whether this to-do's heads-up waits for the later time: it's in the "late" group of the heads-up timing
        experiment, or she kept the later heads-up as a habit. Either only for a to-do the experiment's rules allow
        (no school work, nothing high-priority). A to-do is enrolled, as in the heads-up experiment, only at a moment
        its usual heads-up could be said, and only while the later time is still well ahead (``ENROL_MARGIN_S``), so
        the two groups really differ; every enrolled one is counted, heads-up said or not. A heads-up already waiting
        to be said is withdrawn meanwhile."""
        if not _eligible_for_experiment(entity, now, self.world):
            return False
        later = due - now > late_s
        when = None
        if timing is not None:
            when = self.experiments.variant(timing.id, prediction.key)
            if when is None and due - now > late_s + ENROL_MARGIN_S:
                probe = self._heads_up_candidate(prediction, entity, now, due, risky, None, None)
                if self.gate.check(probe, now=now, busy=busy, focusing=self._focusing)[0] == "delivered":
                    try:
                        when = self.experiments.assign(timing.id, prediction.key)
                    except ValueError:  # it has run its course
                        when = None
        waits = later and (when == "late" or (when is None and late_habit))
        if waits:
            self._withdraw_heads_up(prediction.key, "the heads-up comes later this time")
        return waits

    def _heads_up_candidate(
        self,
        prediction: Prediction,
        entity: Any,
        now: float,
        due: float,
        risky: bool,
        variant: str | None,
        experiment_id: int | None,
    ) -> Candidate:
        hours_left = (due - now) / 3600
        urgency = 0.5 + 0.5 * max(0.0, min(1.0, 1 - hours_left / self.settings.heads_up_h))
        title = entity.name
        all_day = bool(entity.attrs.get("all_day"))
        when = self._when(due - (DAY_S if all_day else 0), all_day)
        text = f'Heads-up: "{title}" is due {when}.'
        if risky:
            text += f" To-dos like it have slipped before: {prediction.basis}."
        features = self._task_features(entity.attrs, float(entity.attrs.get("due_ts") or due))
        patterns = [
            h
            for h in self.hypotheses.list(metric="task_on_time", status=("observed", "supported", "weakened"))
            if h.feature != "nudged" and features.get(h.feature or "") == h.value and (h.effect or 0) < 0
        ][:2]
        assumptions = [
            "the due time in TickTick is right",
            "it isn't done yet (TickTick last showed it open)",
            "a heads-up now leaves enough time to act on it",
        ]
        if variant:
            assumptions.insert(
                0,
                f"this heads-up is part of experiment #{experiment_id}, which you agreed to: to-dos take turns "
                "getting one, so the test is fair",
            )
        return Candidate(
            kind="heads_up",
            key=f"heads_up:{prediction.key}",
            text=text,
            policy=Policy.SUGGEST,
            urgency=urgency if risky else max(urgency, 0.6),
            relevance=(1 - prediction.probability) if risky else 0.6,
            domain=entity.domain or "productivity",
            sensitivity=entity.sensitivity or "personal",
            subject=prediction.subject,
            note=(
                f'(Octavia Futures) You gave a heads-up about "{title}", due {when}.'
                + (" Part of the heads-up experiment the user agreed to." if variant else "")
            ),
            trace=Trace(
                observations=({"event": prediction.event_id or "", "what": f'"{title}" is due {when}', "at": now},),
                predictions=(
                    {
                        "id": prediction.id,
                        "statement": prediction.statement,
                        "probability": prediction.probability,
                        "basis": prediction.basis,
                    },
                ),
                hypotheses=tuple(
                    {"id": h.id, "statement": h.statement, "strength": h.strength, "claim": h.claim, "status": h.status}
                    for h in patterns
                ),
                assumptions=tuple(assumptions),
                confidence=sample_strength(prediction.trials).value,
            ).to_dict(),
            expires_at=due,
            experiment_id=experiment_id,
            variant=variant,
        )

    # --- offers of help (offers.py) --------------------------------------------------------------------------
    def suggest(self, candidate: Candidate) -> None:
        """Bring something up on her own from elsewhere in Octavia (a thought from her reflections, say): it goes
        through the gate at the next tick like everything she says unprompted, so there's one way to speak up, with
        one set of limits. Never INFORM, which is only for what the user asked to be told. Call on the event loop."""
        if Policy(candidate.policy) is Policy.INFORM:
            raise ValueError("INFORM is for what the user asked to be told; suggest a remark of her own")
        self.defer(self._pending_candidates.append, candidate)

    def add_source(self, source: RemarkSource) -> None:
        """Ask ``source`` for things to bring up at every look from now on (``RemarkSource``)."""
        if not source.prefix or source.prefix.startswith(OFFER_PREFIX) or source.prefix in ("trigger", "heads_up"):
            raise ValueError(f"a remark source needs a kind prefix of its own, not {source.prefix!r}")
        self._sources.append(source)

    def _from_sources(self, now: float, fx: Effects) -> None:
        for source in list(self._sources):
            try:
                found = list(source.candidates(now))
            except Exception:
                log.warning("A remark source (%s) failed", source.prefix, exc_info=True)
                continue
            for candidate in found:
                if not candidate.kind.startswith(source.prefix) or Policy(candidate.policy) is Policy.INFORM:
                    log.warning(
                        "Dropped a remark from %s: kind %r, policy %s", source.prefix, candidate.kind, candidate.policy
                    )
                    continue
                self._consider(candidate, now, fx)

    def _offer_help(self, now: float, fx: Effects) -> None:
        """Canvas work due soon, and an online meeting about to start: an offer of help each, through the gate. A
        held-back offer is considered again every look, so its words stay current until it can be said."""
        if not (self.voice and self.settings.offers):
            return
        if not self._deadlines():  # with deadline alerts on, those say it (and may offer the help) instead
            horizon = now + self.settings.offer_assignment_h * 3600
            for entity in self._assignments_due(now, horizon):
                if not self._told(entity.ref):
                    self._consider(self._assignment_offer(entity, now), now, fx)
        lead = self.settings.offer_meeting_min * 60
        meetings = [
            m
            for m in self._upcoming()
            if m.start - lead <= now < m.start + offers.MEETING_GRACE_S
            # Never even kept as a recommendation: a meeting in a protected or off-limits domain.
            and m.sensitivity != "sensitive"
            and self.controls.allows_learning(m.domain)
            and m.online
            and not self._told(f"calendar:{m.key}")  # a deadline alert just said it's starting
        ]
        if meetings:
            estimate, taken, not_taken = offers.take_up(self.store, offers.MEETING, now)
            for item in meetings:
                at = self.local_time(item.start).strftime("%I:%M %p").lstrip("0")
                candidate = offers.meeting_offer(
                    item, now=now, at=at, scale=offers.relevance_scale(estimate), history=(taken, not_taken)
                )
                self._consider(candidate, now, fx)

    def _deadlines(self) -> bool:
        try:
            return bool(self.hooks.deadlines())
        except Exception:
            return False

    def _told(self, subject: str) -> bool:
        try:
            return bool(self.hooks.told(subject))
        except Exception:
            return False  # a check that breaks holds nothing back

    def _assignments_due(self, now: float, before: float) -> list[Any]:
        """Canvas work still open, due after ``now`` and before ``before``, soonest first; none while the user keeps
        Canvas out (what the world model remembers of it is no longer kept up to date)."""
        if not self.controls.allows_source("canvas"):
            return []
        return [
            e
            for e in self.world.open_with_due(kinds=("assignment",), before=before)
            if (e.due_ts or 0) > now and e.status in (None, "open")
        ]

    def _upcoming(self) -> Sequence[Upcoming]:
        if not self.controls.allows_source("calendar"):
            return ()
        try:
            return tuple(self.hooks.calendar())
        except Exception:
            log.debug("Couldn't read the meetings coming up", exc_info=True)
            return ()

    def _assignment_offer(self, entity: Any, now: float, *, working: bool = False) -> Candidate:
        estimate, taken, not_taken = offers.take_up(self.store, offers.ASSIGNMENT, now)
        course = next((_name_of(r.object, self.world) for r in self.world.related(entity.ref, "belongs_to")), "")
        return offers.assignment_offer(
            entity,
            now=now,
            when=f"at {self._when(float(entity.due_ts or now))}",
            course=course,
            working=working,
            horizon_h=self.settings.offer_assignment_h,
            scale=offers.relevance_scale(estimate),
            history=(taken, not_taken),
        )

    def _app_in_front(self, event: FuturesEvent, fx: Effects) -> None:
        """A writing app came to the front while Canvas work is due within a day: likely the moment they start it,
        and the best time to offer what it needs."""
        if not (self.voice and self.settings.offers) or event.data.get("baseline"):
            return
        app, process = str(event.data.get("app") or ""), str(event.data.get("process") or "")
        if not offers.is_work_app(app, process, self.settings.work_apps):
            return
        due_soon = self._assignments_due(event.ts, event.ts + offers.WORKING_AHEAD_S)
        if due_soon and not self._told(due_soon[0].ref):
            self._focusing = self._is_focusing()  # read now, not at the last tick: this moment is what counts
            self._consider(self._assignment_offer(due_soon[0], event.ts, working=True), event.ts, fx)

    def _assignment_changed(self, event: FuturesEvent, fx: Effects) -> None:
        """Handed in, or moved: an offer still waiting about it is dropped rather than said wrong or late (a moved
        one is offered again at its new time, under its new key)."""
        if not event.subject:
            return
        reason = "handed in" if event.type == "canvas.assignment_submitted" else "its due time changed"
        rows = self.store.execute(
            "SELECT id FROM recommendations WHERE subject=? AND kind=? AND status='pending'",
            (event.subject, offers.ASSIGNMENT),
        )
        for row in rows:
            self.store.changed(
                "UPDATE recommendations SET status='suppressed', reason=? WHERE id=?", (reason, row["id"])
            )
            self.store.record_change(
                f"recommendation:{row['id']}", "suppressed", cause=event.id, detail={"reason": reason}
            )

    def _followed_offer(self, tool: str, event: FuturesEvent) -> None:
        """The help an offer was about, used soon after it without a yes ("open the rubric for that"): taken up."""
        latest = self.gate.latest()
        if latest is None or latest.response is not None or latest.delivered_at is None:
            return
        if event.ts - latest.delivered_at > offers.ANSWER_WINDOW_S:
            return
        if tool not in offers.FOLLOW_TOOLS.get(latest.kind, ()):
            return
        rec = self.gate.respond(latest.id, "followed", now=event.ts)
        if rec is not None:
            self._record_response(rec, "followed", event.id)

    def _unanswered_offers(self, now: float) -> None:
        """Offers nobody answered within ten minutes: ignored, which counts against the kind's take-up."""
        rows = self.store.execute(
            "SELECT id FROM recommendations WHERE status='delivered' AND response IS NULL AND substr(kind, 1, ?)=? "
            "AND delivered_at<=? AND delivered_at>=?",
            (len(OFFER_PREFIX), OFFER_PREFIX, now - offers.ANSWER_WINDOW_S, now - DAY_S),
        )
        for row in rows:
            rec = self.gate.respond(row["id"], "ignored", now=now)
            if rec is not None:
                self._record_response(rec, "ignored", None)

    def _session_over(self, session_id: str, fx: Effects) -> None:
        self.episodes.close_thread(f"session:{session_id}", self.clock())

    # ------------------------------------------------------------------ for the brain's prompt
    async def summary_for_prompt(self) -> str:
        """One line of state for the system prompt ("" when there's nothing): what she's watching for and how
        much she has to go on."""
        return await self.call(self._summary)

    def _summary(self) -> str:
        armed = len(self.triggers.armed())
        open_predictions = len(self.predictions.open())
        patterns = self.hypotheses.list(status=("observed", "supported"))
        proposed = self.experiments.list(status="proposed")
        running = self.experiments.running()
        parts = []
        if armed:
            parts.append(f'{armed} "remind me when" waiting')
        if open_predictions:
            parts.append(f"{open_predictions} open predictions")
        if patterns:
            parts.append(f"{len(patterns)} patterns with some evidence")
        if running:
            parts.append(f"{len(running)} experiment running")
        if proposed:
            parts.append(f"{len(proposed)} experiment offered, awaiting the user's answer")
        if not self.controls.proactive_enabled:
            parts.append("speaking up on your own is off")
        return "; ".join(parts)

    # ------------------------------------------------------------------ what the tools do (worker thread)
    def add_trigger(self, kind: str, value: str, message: str, *, once: bool, days: float) -> Trigger:
        now = self.clock()
        trigger = self.triggers.add(kind, value, message, once=once, expires_at=now + days * DAY_S if days else None)
        if kind == "email_from" and value.strip():
            self.world.relate(ref("user", "me"), "waiting_on", ref("person", value), attrs={"trigger": trigger.id})
        return trigger

    def record_answer(self, kind: str, text: str, trace: Trace) -> int:
        """Keep something she worked out on request (a comparison of options), so "why?" can be answered later."""
        now = self.clock()
        rec_id = self.store.insert(
            "INSERT INTO recommendations(key, kind, text, policy, urgency, relevance, domain, sensitivity, status, "
            "reason, trace, created_at, delivered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{kind}:{int(now)}",
                kind,
                redact(text, 600),
                Policy.SUGGEST.value,
                0.0,
                0.0,
                "futures",
                "personal",
                "answered",
                "asked",
                dumps(trace.to_dict()),
                now,
                now,
            ),
        )
        self.store.record_change(f"recommendation:{rec_id}", "answered", provider=trace.provider)
        return rec_id

    def release_experiment(self, experiment: Any, reason: str) -> None:
        """An experiment that ended without a result (the user stopped it): its hypothesis stops being "tested" and
        goes back to what its own evidence says."""
        if experiment is None or experiment.hypothesis_id is None:
            return
        h = self.hypotheses.get(experiment.hypothesis_id)
        if h is not None and h.status == HypothesisStatus.TESTING.value:
            self.hypotheses.apply_experiment(
                h.id, supported=None, strength=h.strength, summary=reason, experiment_id=experiment.id
            )

    # --- the week, sampled -----------------------------------------------------------------------------------
    def note_work_hours(self, title: str, hours: float) -> None:
        """How long a piece of work really took, as the user said: what the week's ranges are drawn from later."""
        entity = next((e for e in self.world.open_with_due() if _shares_words(title, e.name)), None)
        self.outcomes.record(
            WORK_HOURS,
            entity.ref if entity is not None else f"work:{title[:60]}",
            success=None,
            value=hours,
            actual=redact(title, 80),
            features={"kind": entity.kind if entity is not None else "task"},
            domain="productivity",
        )

    def _week_work(self, estimates: Mapping[str, float]) -> list[WeekWork]:
        """Open work due in the next week, each with its hours from the best thing behind them: the user's estimate
        now, then records of how long this kind of work took, then a stated guess."""
        now = self.clock()
        rows = self.store.execute("SELECT actual, value, features FROM outcomes WHERE kind=?", (WORK_HOURS,))
        records = [(str(r["actual"] or ""), float(r["value"]), loads(r["features"], {})) for r in rows if r["value"]]
        work = []
        for entity in self.world.open_with_due(before=now + 7 * DAY_S):
            due = entity.attrs.get("due_ts")
            if due is None or float(due) <= now:
                continue
            said = next((h for words, h in estimates.items() if _shares_words(words, entity.name)), None)
            alike = [h for title, h, _ in records if _shares_words(title, entity.name)]
            kin = [h for _, h, f in records if f.get("kind") == entity.kind]
            guess = _WEEK_GUESS_H.get(entity.kind, _WEEK_GUESS_H["task"])
            if said is not None:
                item = WeekWork(
                    entity.ref, entity.name, float(due), said * 0.7, said * 1.5, (), "the user's own estimate", True
                )
            elif len(alike) >= MIN_SAMPLES:
                basis = f"{len(alike)} records of how long work like it took"
                item = WeekWork(entity.ref, entity.name, float(due), min(alike), max(alike), tuple(alike), basis, True)
            elif len(kin) >= MIN_SAMPLES:
                basis = f"{len(kin)} records of how long {entity.kind}s took"
                item = WeekWork(entity.ref, entity.name, float(due), min(kin), max(kin), tuple(kin), basis, True)
            else:
                item = WeekWork(entity.ref, entity.name, float(due), guess[0], guess[1])
            work.append(item)
        return work

    def _week(
        self, first: str, free_per_day: float | None, estimates: Mapping[str, float], busy: Mapping[date, float]
    ) -> tuple[list[WeekRisk], list[WeekRisk], list[str], str]:
        """On the worker: (risks in the order asked, risks soonest-due-first, assumptions, what was put first)."""
        now = self.clock()
        work = self._week_work(estimates)
        settings = self.settings

        def free_hours(day: date) -> float:
            if free_per_day is not None:
                return free_per_day
            return settings.week_free_hours_weekend if day.weekday() >= 5 else settings.week_free_hours_weekday

        assumptions = (
            [f"about {free_per_day:g} free hours a day"]
            if free_per_day is not None
            else [
                f"about {settings.week_free_hours_weekday:g} free hours on a weekday and "
                f"{settings.week_free_hours_weekend:g} on a weekend day (nobody's measured this: ask)"
            ]
        )
        if busy:
            assumptions.append("an hour for each calendar event")
        chosen = [w for w in work if first and _shares_words(first, w.title)]
        usual = sample_week(work, now, free_hours, busy=busy, local_time=self.local_time)
        if not chosen:
            return usual, [], assumptions, ""
        refs = [w.ref for w in chosen]
        asked = sample_week(work, now, free_hours, busy=busy, first=refs, local_time=self.local_time)
        return asked, usual, assumptions, chosen[0].title

    async def week_risks(self) -> list[WeekRisk]:
        """How the coming week looks with nothing assumed beyond the settings (``core.deadlines``)."""
        busy = await self._busy_hours()
        risks, _, _, _ = await self.call(self._week, "", None, {}, busy)
        return risks

    async def week_outlook(
        self, first: str = "", free_per_day: float | None = None, estimates: Mapping[str, float] | None = None
    ) -> str:
        """The week sampled, in words for her brain (the ``week_outlook`` tool). CPU only, no model."""
        busy = await self._busy_hours()
        asked, usual, assumptions, chosen = await self.call(
            self._week, first, free_per_day, dict(estimates or {}), busy
        )
        return describe_week(
            asked, self.clock(), versus=usual, first=chosen, assumptions=assumptions, local_time=self.local_time
        )

    async def _busy_hours(self) -> dict[date, float]:
        """An hour for each event on the calendar this week (``Hooks.calendar_starts``); nothing when it can't be read."""
        if self.hooks.calendar_starts is None:
            return {}
        try:
            starts = await self.hooks.calendar_starts()
        except Exception:
            log.warning("Couldn't read the calendar for the week", exc_info=True)
            return {}
        busy: dict[date, float] = {}
        for at in starts:
            day = self.local_time(at).date()
            busy[day] = busy.get(day, 0.0) + 1.0
        return busy

    def latest_recommendation(self) -> Recommendation | None:
        row = self.store.one(
            "SELECT id FROM recommendations WHERE status IN ('delivered','answered') "
            "ORDER BY COALESCE(delivered_at, created_at) DESC, id DESC LIMIT 1"
        )
        return self.gate.get(row["id"]) if row else None

    def respond(self, target: int | None, response: str) -> Recommendation | None:
        now = self.clock()
        rec = self.gate.respond(target, response, now=now)
        if rec is None and target is None:
            latest = self.latest_recommendation()
            rec = self.gate.respond(latest.id, response, now=now) if latest else None
        if rec is not None:
            self._record_response(rec, response, None)
        return rec

    def scenario(self, hours: float, options: Iterable[str], focus: str) -> Scenario:
        """The situation "what should I work on?" is about, from the world model: open dated work, how likely each
        is to be finished on time, the user's own goals and the patterns she has evidence for."""
        now = self.clock()
        goals = self.objectives.active()
        goal_text = {f"goal:{g.id}": g.text for g in goals}
        overall = self.predictions.base_rate("task_on_time", noun="to-dos")
        items = []
        for entity in self.world.open_with_due(before=now + 7 * DAY_S):
            due = entity.attrs.get("due_ts")
            key = _task_key(entity.ref, float(due)) if due is not None else None
            prediction = self.predictions.by_key(key) if key else None
            on_time: float | None
            if prediction is not None and prediction.status == "open":
                on_time, on_time_basis, samples = prediction.probability, prediction.basis, prediction.trials
            elif entity.kind == "task":
                on_time, tally, on_time_basis = overall
                samples = tally.trials
            else:  # no outcomes of its own kind yet: to-dos say nothing about assignments
                on_time, on_time_basis, samples = None, f"no history for {entity.kind}s yet", 0
            items.append(
                WorkItem(
                    ref=entity.ref,
                    title=entity.name,
                    kind=entity.kind,
                    due_ts=float(due) if due is not None else None,
                    hours=None,
                    hours_basis="no history for how long this takes",
                    on_time=on_time,
                    on_time_basis=on_time_basis,
                    goals=tuple(
                        dict.fromkeys(
                            [
                                goal_text[r.object]
                                for r in self.world.related(entity.ref, "contributes_to")
                                if r.object in goal_text
                            ]
                            + [g.text for g in goals if _shares_words(g.text, entity.name)]
                        )
                    ),
                    blocked_by=tuple(
                        _name_of(r.object, self.world) for r in self.world.related(entity.ref, "blocked_by")
                    ),
                    priority=entity.attrs.get("priority"),
                    sensitivity=entity.sensitivity,
                    on_time_samples=samples,
                )
            )
        patterns = [
            {"statement": h.statement, "strength": h.strength, "claim": h.claim}
            for h in self.hypotheses.list(metric="task_on_time", status=("observed", "supported"), limit=3)
        ]
        return Scenario(
            now=now,
            hours=hours,
            items=tuple(items),
            objectives=tuple({"id": g.id, "text": g.text, "label": g.label, "weight": g.weight} for g in goals),
            options=tuple(o for o in options if o.strip()),
            focus=focus,
            patterns=tuple(patterns),
        )

    async def compare(self, hours: float, options: Iterable[str], focus: str = "") -> tuple[Scenario, Any]:
        scenario = await self.call(self.scenario, hours, list(options), focus)
        result = await self.reasoner.reason(ReasoningTask("simulate", scenario.to_dict()))
        return scenario, result


def _log_failure(future: asyncio.Future[Any]) -> None:
    if not future.cancelled() and future.exception() is not None:
        log.warning("Futures bookkeeping failed", exc_info=future.exception())


class _Queued:
    """``Performance`` for the reasoner router, which runs on the event loop: each record goes onto the pipeline's
    queue instead of touching the database from the loop thread."""

    def __init__(self, service: FuturesService) -> None:
        self._service = service

    def record(self, *args: Any, **kwargs: Any) -> None:
        self._service.defer(self._service.performance.record, *args, **kwargs)


# ---------------------------------------------------------------------------------------------- helpers
def _task_key(subject: str | None, due: float) -> str:
    return f"task_on_time:{subject}@{int(due)}"


def _due_of(key: str) -> float | None:
    _, _, due = key.rpartition("@")
    try:
        return float(due)
    except ValueError:
        return None


def _all_day(prediction: Prediction, attrs: Mapping[str, Any]) -> bool:
    """Whether the to-do was all-day when the prediction was made (its features say), else as it is now."""
    part = prediction.features.get("due_part_of_day") if prediction.features else None
    return part == "all_day" if part else bool(attrs.get("all_day", False))


def _deadline(due: float, all_day: bool) -> float:
    """When a to-do stops being on time: its due time, or the end of its day for an all-day one."""
    return due + DAY_S if all_day else due


_STOP = frozenset(
    "the a an and or for of to in on at my your with from about this that into get got make do finish work "
    "more less up down out over some".split()
)  # fmt: skip


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in _STOP}


def _shares_words(goal: str, title: str) -> bool:
    """Does a to-do sound like part of a goal (a word of substance in common)? Only for weighing options, and the
    comparison says which goal it counted it toward."""
    return bool(_words(goal) & _words(title))


def _bears_out(hypothesis: Any, result: Any) -> bool | None:
    """Does an experiment's result bear out what the hypothesis claims? The experiment says whether its first variant
    (the heads-up) did better than the second; the hypothesis may claim either direction ("to-dos I gave a heads-up
    about did worse" is the usual, confounded, one). Same direction with at least moderate evidence: yes. The other
    direction, or no real difference: no. Too little to tell: None."""
    if result.supported is None:
        return None
    clear = STRENGTH_ORDER[Strength(result.strength)] >= STRENGTH_ORDER[Strength.MODERATE]
    found = (1 if result.difference > 0 else -1 if result.difference < 0 else 0) if clear else 0
    effect = hypothesis.effect or 0.0
    claimed = (1 if effect > 0 else -1 if effect < 0 else 0) * (1 if hypothesis.value in (None, "yes") else -1)
    if found == 0 or claimed == 0:
        return False if found == 0 else None
    return found == claimed


def _eligible_for_experiment(entity: Any, now: float, world: WorldModel | None = None) -> bool:
    """The heads-up experiment's constraints (``experiments.task_nudge_eligible``), and school work kept out: a to-do
    made from a Canvas assignment lives in TickTick like any other, so one that shares its title with an assignment
    she knows of, or that sounds like school work, is left out."""
    if "priority" not in entity.attrs:
        return False  # the rules say never high-priority to-dos, and this one's priority isn't known
    if not task_nudge_eligible(
        due_ts=entity.attrs.get("due_ts"),
        all_day=bool(entity.attrs.get("all_day")),
        priority=entity.attrs.get("priority"),
        now=now,
        domain=entity.domain,
    ):
        return False
    title = " ".join(str(entity.name).lower().split())
    if _SCHOOL.search(title):
        return False
    if world is not None:
        for assignment in world.find("assignment", limit=500):
            name = " ".join(assignment.name.lower().split())
            if name and (name in title or title in name):
                return False
    return True


_SCHOOL = re.compile(
    r"\b(?:assignments?|homework|hw|exams?|quiz(?:zes)?|essays?|lab|labs|lectures?|midterms?|finals?|tests?|"
    r"study|studying|problem sets?|psets?|readings?|chapters?|ch|class|classes|course|courses|papers?|thesis|"
    r"discussion|canvas|syllabus|professor|prof|seminar|tutorial|project report)\b"
)


def _what_happened(event: FuturesEvent) -> str:
    data = event.data
    if event.type in ("computer.application_opened", "computer.context_changed"):
        return f"{data.get('app') or 'an app'} came to the front"  # said out loud now: never "None"
    if event.type == "gmail.message_received":
        return f"an email from {data.get('sender') or 'someone'} arrived"
    if event.type.startswith("build."):
        return f'build of "{data.get("title", "")}": {event.type.removeprefix("build.").replace("_", " ")}'
    if event.type == "task.completed":
        return f'"{data.get("title", "a to-do")}" was ticked off'
    if event.type == "timer.completed":
        return f"the {data.get('label') or ''} timer rang".replace("  ", " ")
    if event.type.startswith("canvas."):
        return f"{event.type.removeprefix('canvas.').replace('_', ' ')}: {data.get('title', '')}"
    return event.type.replace(".", ": ").replace("_", " ")


def _name_of(entity_ref: str, world: WorldModel) -> str:
    entity = world.get(entity_ref)
    return entity.name if entity else split_ref(entity_ref)[1]


def _count_before(text: str, word: str) -> int | None:
    found = re.search(rf"(\d+) {word}", text)
    return int(found.group(1)) if found else None


# Events whose words can say what they're about, and the everyday domains they may be refiled from.
_WORDED = frozenset(
    {
        "user.request",
        "user.correction",
        "task.created",
        "task.updated",
        "task.seen",
        "task.completed",
        "task.overdue",
        "task.deleted",
    }
)
_REFILED_FROM = frozenset({"general", "productivity", "assistant"})  # not school: coursework is school

_RULES: dict[str, Callable[[FuturesService, FuturesEvent, Effects], None]] = {
    "task.seen": FuturesService._task_seen,
    "task.created": FuturesService._task_seen,
    "task.updated": FuturesService._task_updated,
    "task.completed": FuturesService._task_done,
    "task.overdue": FuturesService._task_overdue,
    "task.deleted": FuturesService._task_deleted,
    "computer.action_performed": FuturesService._action,
    "computer.action_failed": FuturesService._action,
    "user.request": FuturesService._request,
    "assistant.claim_corrected": FuturesService._claim_corrected,
    "user.correction": FuturesService._correction,
    "user.reaction": FuturesService._reaction,
    "user.feedback": FuturesService._feedback,
    "assistant.reply": FuturesService._reply,
    "build.started": FuturesService._build_started,
    "build.test_passed": FuturesService._build_over,
    "build.test_failed": FuturesService._build_over,
    "build.failure": FuturesService._build_over,
    "computer.application_opened": FuturesService._app_in_front,
    "computer.context_changed": FuturesService._app_in_front,
    "canvas.assignment_submitted": FuturesService._assignment_changed,
    "canvas.assignment_updated": FuturesService._assignment_changed,
}
# A short answer to a remark that asked something (``offers.classify_answer``, via ``ingest``) -> its response.
_ANSWERS = {"yes": "accepted", "no": "rejected", "not_now": "deferred"}
