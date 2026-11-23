"""The coordinator of self-improvement: notes, the build queue, installs and undos.

Everything the brain's tools, the error watcher and the ``octavia evolve`` command do goes through here, so
the rules live in one place: nothing is built unless it's enabled and git is set up, builds run side by side
(up to ``parallel_builds`` at once) within a daily limit, only a build that passed the gate is ever merged, the
user's uncommitted work is never mixed into a merge, and every install is one merge commit that can be undone.

A build runs in the background (``_work``, one worker per build slot): a git worktree on its own branch, the
builder agent, a commit of whatever it changed, then the gate. The builder sessions overlap, but the gate's full
test runs take turns (``_gate_lock``): two at once would slow both and make timing-sensitive tests fail good
builds. The builder gets one more round when only the final checks failed. The outcome is recorded in the journal
and announced; the worktree is always removed, and the branch is kept only for a build that is ready to install.
Builds that finished side by side need no merging of their own: installing merges every ready build onto the
newest code and tests them together, leaving out any that clash.

Installing needs the user's say-so (a CONFIRM tool), given when installing or when asking for the build
("install it when it's ready"), unless ``auto_install`` allows it. Several ready builds go in together: merged,
and tested when needed, in a worktree beside the main checkout, which only ever moves forward to a result that
passed (a fast-forward, so the user's uncommitted work stays as it is). Under the supervisor the assistant then
restarts into the new code, once every build in progress (if any) is done: a restart never cuts one off, and one
that hasn't begun is queued again for the next start. Installs and undos while a restart is pending join it, and
the health marker watches every update the next start runs for the first time: a crash reverts them all.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import statistics
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

import psutil

from octavia.config import AppConfig
from octavia.core import usage as claude_usage
from octavia.core.events import EventBus, ImprovementChanged, Notice
from octavia.evolve.gate import LINKS, NO_CHANGES, PROTECTED, RESERVED, TWIN, UNPROVEN, UNSTARTED, Gate, is_refusal
from octavia.evolve.git import Author, Git, GitError, MergeConflictError
from octavia.evolve.journal import Journal, StatusChanged, normalize_title
from octavia.evolve.models import (
    BUDGET_REFUSAL,
    OPEN_STATUSES,
    PLAN_PREFIX,
    BuilderLike,
    BuildResult,
    BuildTask,
    CheckResult,
    EvolveError,
    EvolvePaths,
    GateReport,
    HealthMarker,
    Improvement,
    Kind,
    RestartMarker,
    Status,
    WatchedUpdate,
    clear_marker,
    is_side_worktree_name,
    read_marker,
    self_improvement_author,
    side_worktree_live,
    side_worktree_name,
    write_marker,
)
from octavia.tools.permissions import Offer

log = logging.getLogger(__name__)

BRANCH_PREFIX = "octavia"
DAY_S = 86_400.0
NOTICE_SOURCE = "EVOLVE"
MAX_REQUEST_TITLE = 80
_SLUG_CHARS = 40
HELD_NEWS_CHECK_S = 20.0  # how often held news checks whether the user is still busy (``ManagerHooks.busy``)
_PROGRESS_NOTICE_S = 1.0  # progress lines are logged in full, but shown at most this often
_SPOKEN_REASON = 200
_SPOKEN_TITLE = 50  # a longer title is a mouthful: it's called "the change you asked for" instead
_LISTED_READY = 3
# "How's the build going?": the builder's latest steps, the files it has written and the builds it's timed by.
_LATEST_STEPS = 3
_SHOWN_FILES = 4
_TIMED_BUILDS = 5
_WRITING = ("Editing ", "Writing ")  # ``describe_tool_use``'s lines for a changed file
STAGE_PREPARING = "getting a copy of my code ready"
STAGE_BUILDING = "the developer agent is writing the change"
STAGE_RETRYING = "the developer agent is fixing what my final checks found (its second and last try)"
STAGE_CHECKING = "running my final checks (the linter and the full test suite, a few minutes)"
STAGE_WAITING_CHECKS = "waiting for another build's final checks to finish before running its own"
# Shutting down must fit in the app's 10 s per closer: an install gets a moment to finish its merge (cancelled
# mid-check, it reverts), the running builds time to record their outcome and remove their worktrees: first on
# their own once their builders have stopped (so the record isn't cut short), then after being cancelled.
_CLOSE_CRITICAL_S = 2.0
_CLOSE_WIND_DOWN_S = 3.0
_CLOSE_WORKER_S = 3.0
_CLOSE_REST_S = 1.0
# Untracked or changed files here are the assistant's own data (ignored by git in the project anyway).
_OWN_DATA = ("data/",)

TURNED_OFF = "Self-improvement is turned off. Turn it on with self_improvement.enabled in the config."
NOT_SET_UP = 'My code isn\'t set up for building yet: run "uv run octavia evolve setup" once first.'
NO_GIT = "I can't build anything because git isn't installed."
DIRTY = "You have unsaved changes in files it changes too ({files}), so I can't do it safely. Commit them first."
INTERRUPTED = "interrupted by a restart"
STOPPED = "interrupted: the assistant stopped during the build"
DROPPED = "dropped while being built"
# A redone build whose clashes with newer code the builder resolved (``_develop``): never installed by itself.
RESOLVED_CLASHES = "it was redone on newer code, and the builder resolved where the two clashed in"
# A redone build whose earlier change went onto the newer code cleanly (``_develop``): no builder ran.
REAPPLIED = "redone on the newer code: the earlier build applied cleanly, so no code was written again"
CRASHED = "crashed on startup"
RESTART_YOURSELF = "Installed. Restart me to start using it."
AUTO_INSTALL_HEADS_UP = "I installed an update on my own; restarting, back in a few seconds."
NOTHING_READY = "Nothing is ready to install."
INSTALLING_NOW = "I'm in the middle of installing or undoing an update; ask me again once that's done."
NO_WORKTREE = "Git couldn't set up a copy of my code to install into, so nothing was installed."
# Flags a build the user asked to have installed once it's ready may carry and still go in by itself: not about
# safety, and nothing they'd want to hear about before it goes in.
_PREAPPROVED_FLAGS = (re.compile(r"no tests added or changed"),)
_NEW_SAFE_TOOL = re.compile(r"new tool (\S+) runs without asking")  # the gate's flags
_LOWERED_TOOL = re.compile(r"lowers the risk of tool (\S+)")
# How a failed check is said out loud ("it failed the lint check and my tests").
WEEK_S = 7 * 24 * 3600.0
# Why a proposal (the night shift's) wasn't built: building on its own isn't on (``auto_build``).
NOT_BUILDING = "auto_build isn't all: noted for the user to build"
_SPOKEN_CHECKS = {
    "ruff check": "the lint check",
    "ruff format": "the formatting check",
    "pytest": "my tests",
    "eval": "my speed and quality check on real requests",
}

_T = TypeVar("_T")
_NOW = object()  # undo_last: whatever is the last update when it is called


class _RevertFailed(Exception):
    """The revert itself failed (a conflict with later changes), not the work around it."""


@dataclass
class ManagerHooks:
    """How the manager reaches the running app; set after construction, and each one is optional."""

    # (what to say, a note for the brain's next turn, whether to listen for a reply afterwards)
    announce: Callable[..., None] | None = None  # (text, note, listen[, offer])
    restart: Callable[[RestartMarker], Awaitable[None]] | None = None  # exit with RESTART_EXIT_CODE when quiet
    brain_session: Callable[[], str | None] | None = None  # the conversation to resume after a restart
    # A note for the brain's next turn without saying anything: what an announcement shown only on the console
    # (``announce: console``, or speaking failed) still owes the brain.
    note: Callable[[str], None] | None = None
    # While it says so (her night shift: the user is asleep), results are shown on the console, not said.
    quiet: Callable[[], bool] | None = None
    # While it says so (the user is in the middle of something with her: they talked lately, a screen task runs), news
    # nobody is waiting on (a build finished, the night's report) waits until it's over (``announce(news=True)``).
    busy: Callable[[], bool] | None = None


class Noted(NamedTuple):
    """What ``note`` did: the improvement as it is now, and whether it's being built by itself (or why it can't
    be built right now, when ``auto_build`` wanted to)."""

    item: Improvement
    building: bool = False
    blocked: str | None = None
    came_back: Improvement | None = None  # an update installed this week for the same problem: it didn't fix it


@dataclass
class _Pending:
    """What this process installed and undid that it doesn't run itself: the next start runs it all.

    The health marker watches ``installed`` (a crash of that start undoes them all), and the restart marker says
    all of it once the assistant is back.
    """

    previous_head: str  # before the first of them
    installed: list[Improvement] = field(default_factory=list)  # oldest first, each with its merge commit
    undone: list[Improvement] = field(default_factory=list)
    asked_now: bool = False  # an install or undo the user is waiting on: restart after the reply, not when idle
    asked_for: set[int] = field(default_factory=set)  # installed because the user asked (now or when built)


@dataclass
class _Verdict:
    """How one build ended, before it's written to the journal."""

    status: Status
    error: str | None = None
    result: BuildResult | None = None
    report: GateReport | None = None
    turns: int = 0
    cost_usd: float | None = None
    dropped: bool = False  # the user rejected it while it was being built
    spoken: str | None = None  # what to say instead of ``error`` (which may be raw exception text)
    stopped: bool = False  # cut off because the assistant stopped (not for a restart): said at the next start


@dataclass
class _Progress:
    """A running build's progress: every line goes to the build's log and a few to the console, and it keeps
    what the user may ask about ("how's it going?"): the stage, the builder's plan and latest steps."""

    log_file: Path
    notice: Callable[[str], None]
    last_notice: float = float("-inf")
    started: float = field(default_factory=time.monotonic)
    stage: str = STAGE_PREPARING
    steps: int = 0  # the builder's tool calls so far (blocked ones aren't steps)
    plan: str = ""  # the builder's own to-do list, summarised ("2 of 5 steps done; now: Writing the tests")
    latest: deque[str] = field(default_factory=lambda: deque(maxlen=_LATEST_STEPS))
    files: list[str] = field(default_factory=list)  # what it has edited or written, in order

    def write(self, line: str) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError as exc:
            log.debug("Couldn't write the build log %s: %s", self.log_file, exc)

    def __call__(self, message: str) -> None:
        """A line from the builder (``describe_tool_use``, or a blocked call)."""
        self.write(message)
        self._saw(message)
        now = time.monotonic()
        if now - self.last_notice >= _PROGRESS_NOTICE_S:
            self.last_notice = now
            self.notice(message)

    def at(self, stage: str, line: str) -> None:
        """The build moved on to ``stage`` (said in answer to "how's it going?"); ``line`` is for the log."""
        self.stage = stage
        self.write(line)
        self.notice(line)

    def _saw(self, message: str) -> None:
        if message.startswith("Blocked "):
            return
        self.steps += 1
        if message.startswith(PLAN_PREFIX):
            self.plan = message[len(PLAN_PREFIX) :]
            return
        self.latest.append(message)
        for verb in _WRITING:
            if message.startswith(verb):
                path = message[len(verb) :].strip()
                if path and path not in self.files:
                    self.files.append(path)

    def status(self, typical_s: float | None) -> str:
        """How it's going, for the brain to put in its own words."""
        elapsed = time.monotonic() - self.started
        parts = [f"Building for {_duration(elapsed)}"]
        if typical_s is not None:
            typical = _duration(typical_s)
            parts[0] += f" (my recent builds took {typical if typical.startswith('under') else f'about {typical}'})"
        now = f"Now: {self.stage}"
        if self.stage in (STAGE_BUILDING, STAGE_RETRYING) and self.steps:
            now += f", {self.steps} step{'' if self.steps == 1 else 's'} in"
        parts.append(now)
        if self.plan:
            parts.append(f"Its plan: {self.plan}")
        if self.latest:
            parts.append(f"Latest: {'; '.join(self.latest)}")
        if self.files:
            parts.append(f"Files changed so far: {_listing(self.files, _SHOWN_FILES)}")
        return ". ".join(parts) + "."


class ImprovementManager:
    """Notes, builds, installs and undoes improvements. Create one per process; call ``start`` on the loop."""

    def __init__(
        self,
        config: AppConfig,
        journal: Journal,
        git: Git,
        builder: BuilderLike,
        gate: Gate,
        events: EventBus,
        *,
        supervised: bool,
        paths: EvolvePaths | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``supervised``: running under the supervisor, so an install can restart into the new code."""
        self._held_news: list[tuple[str, str | None, bool, Offer | None]] = []  # ``announce(news=True)`` while busy
        self._held_task: asyncio.Task[None] | None = None
        self.config = config
        self.journal = journal
        self.git = git
        self.builder = builder
        self.gate = gate
        self.events = events
        self.supervised = supervised
        self.paths = paths or EvolvePaths(config.root)
        self.hooks = ManagerHooks()
        self.author: Author = self_improvement_author(config.assistant.name)
        self._clock = clock
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._waiting: list[int] = []  # what the queue holds and no worker has taken up yet, in order
        self._workers: set[asyncio.Task[None]] = set()  # one per build slot (``parallel_builds``), all on the queue
        self._tasks: set[asyncio.Task[Any]] = set()  # background notes; cancelled on close
        self._critical: set[asyncio.Task[Any]] = set()  # installs and undos; allowed to finish on close
        self._repo_lock = asyncio.Lock()  # one change at a time to the main checkout and the shared git metadata
        self._request_lock = asyncio.Lock()
        # One full test run at a time: builder sessions overlap, but two test suites at once would double the load
        # and make timing-sensitive tests fail good builds.
        self._gate_lock = asyncio.Lock()
        # Her own builds (the night shift's, fixes she started by herself) one at a time, whatever parallel_builds
        # says: four builders and a full test run at once on 2026-10-27 ended with Windows refusing to start Python.
        # Builds the user asks for still take the free slots beside them.
        self._own_builds = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # The improvements being built, in the order they started, each with how it's going.
        self._active: dict[int, _Progress] = {}
        self._installing: set[int] = set()  # ready builds being merged and tested (it can take minutes)
        self._taken: set[int] = set()  # the ones workers are busy with (building them, or installing them after)
        self._checking: dict[int, asyncio.Future[GateReport]] = {}  # running builds' final checks
        # Builds still getting their copy of the code (it waits for the repository, which an install may hold for
        # minutes): a drop stops them there.
        self._preparing: dict[int, asyncio.Future[str]] = {}
        self._running: set[int] = set()  # builds started and not yet done recording their end
        self._no_build = asyncio.Event()  # set while no build is running (or recording its end)
        self._no_build.set()
        self._dropped: set[int] = set()  # rejected while being built
        self._repo_ready = False
        self._closed = False
        self._runs_queued = True  # this process builds what's queued (``start``): the voice assistant does
        # What this process installed or undid that it doesn't run itself (the next start does).
        self._pending: _Pending | None = None
        # Set once an install or undo has asked the app to restart: no new build starts (the ones running are
        # finished first: the restart waits for them all), and errors are ignored (this is the old code). More
        # installs and undos join the same restart.
        self._restarting = False
        self._restart_waiter: asyncio.Task[None] | None = None  # restarts once the running builds are done

    @property
    def _settings(self) -> Any:
        return self.config.self_improvement

    @property
    def restart_pending(self) -> bool:
        """An install or undo has asked the app to restart (into the new code), and it hasn't happened yet."""
        return self._restarting

    @property
    def parallel_builds(self) -> int:
        """How many builds run at once here (``self_improvement.parallel_builds``)."""
        return max(1, int(self._settings.parallel_builds))

    # ------------------------------------------------------------------ lifecycle

    async def start(self, *, resume_queued: bool = True) -> None:
        """Pick up where the last run stopped, then start the build workers.

        A build that was running when the assistant stopped can't continue (its builder session is gone): it
        is marked failed, its worktree removed, and the user told. Queued builds are built now, unless
        ``resume_queued`` is false (``octavia chat`` leaves them to the voice assistant, which may be running).
        """
        self._loop = asyncio.get_running_loop()
        self._runs_queued = resume_queued
        try:
            await self._recover(resume_queued=resume_queued)
        except Exception:
            log.warning("Couldn't recover the self-improvement state", exc_info=True)
        self._ensure_worker()

    async def close(self) -> None:
        """Stop building: every running build is cancelled, marked failed (queued again when this is a restart
        into an update, so the next start builds it) and its worktree removed."""
        if self._closed:
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        critical = [t for t in self._critical if not t.done()]
        if critical:  # stopping between a merge and its checks would leave unchecked code installed
            await asyncio.wait(critical, timeout=_CLOSE_CRITICAL_S)
        workers = [w for w in self._workers if not w.done()]
        if workers:
            if self._active:
                with contextlib.suppress(Exception):
                    await self.builder.cancel()  # all of them
            # A stopped builder returns: let the builds record that and clean up before the workers are cancelled
            # (mid-record, one would stay "building"). One still in (or waiting for) its checks is cut off here.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._no_build.wait(), _CLOSE_WIND_DOWN_S)
            for worker in workers:
                worker.cancel()
            await asyncio.wait(workers, timeout=_CLOSE_WORKER_S)
        while not self._queue.empty():  # still queued in the journal, so the next start builds them
            self._queue.get_nowait()
            self._queue.task_done()
        self._waiting.clear()
        remaining = [t for t in (*self._tasks, *self._critical) if not t.done()]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.wait(remaining, timeout=_CLOSE_REST_S)

    async def wait_idle(self) -> None:
        """Return once nothing is being noted, queued, built or installed (``octavia evolve build`` waits so)."""
        while not self._closed:
            if not self._queue.empty():
                self._ensure_worker()
            await self._queue.join()
            pending = [t for t in (*self._tasks, *self._critical) if not t.done()]
            if not pending:
                return
            await asyncio.wait(pending)

    # ------------------------------------------------------------------ noting

    async def note(self, kind: Kind | str, title: str, details: str = "", source: str = "assistant") -> Noted:
        """Record something to improve (or count it again). With ``auto_build: all`` it is built right away,
        unless the last one with this title was dropped or built without being installed.

        The assistant notes things in conversation, so the user is told a build it starts this way is under way
        (``note_improvement`` says so): unlike a fix for an error, how it ends is announced, good or bad. And
        when it can't be built now (the daily limit), the assistant mustn't offer to build it.
        """
        # The same problem again within a week of an update for it: that fix didn't work. The installed one stays
        # installed (it's what "undo" undoes); the new note says what it tried, so the next build doesn't repeat it.
        earlier = await self.journal.installed_like(title, WEEK_S)
        if earlier is not None:
            details = _came_back(earlier, details)
        item, created = await self.journal.note(Kind(kind), title, details, source)
        if earlier is not None and earlier.id == item.id:
            earlier = None
        seen = "noted" if created else f"seen {item.occurrences} times"
        self._changed(item, seen)
        self._notice(f"Noted {item.label()}" + ("" if created else f" ({seen})"), logging.DEBUG)
        if self._settings.auto_build == "all" and _auto_buildable(item):
            earlier = await self._decided_before(item)
            if earlier:  # the user can still ask for it
                self._notice(f"Not building {item.label()} on my own: {earlier}", logging.DEBUG)
            else:
                built, refusal = await self._auto_build(item, promised=True)
                if built is not None:
                    return Noted(built, building=True, came_back=earlier)
                return Noted(item, blocked=refusal, came_back=earlier)
        return Noted(item, came_back=earlier)

    async def propose(
        self, kind: Kind | str, title: str, details: str, source: str, *, build: bool = True, cooldown_s: float = WEEK_S
    ) -> tuple[Improvement, str | None]:
        """Note something found by the assistant itself (her night shift) and, with ``build``, build it: (the
        improvement, None when it's being built, or why not). Not within ``cooldown_s`` of the last one of that title
        being dropped, built without an install, or installed (a fix gets time to show), nor past the daily limit.
        Nobody was told, so only good news is said (and not at night: ``hooks.quiet``)."""
        item, _ = await self.journal.note(Kind(kind), title, details, source)
        self._changed(item, "noted")
        if not build:
            return item, NOT_BUILDING
        if not _auto_buildable(item):
            return item, f"#{item.id} isn't waiting to be built ({item.status.value.replace('_', ' ')})"
        recent = [
            i
            for i in [
                await self.journal.closed_like(item.kind, item.title),
                *[
                    i
                    for i in await self.journal.list([Status.INSTALLED], limit=200)
                    if normalize_title(i.title) == normalize_title(item.title)
                ],
            ]
            if i is not None and i.id != item.id and time.time() - i.updated_at < cooldown_s
        ]
        if recent:
            last = max(recent, key=lambda i: i.updated_at)
            return item, f"#{last.id} with that name ended {last.status.value.replace('_', ' ')} lately"
        built, refusal = await self._auto_build(item)
        return (built or item), refusal

    def _quiet(self) -> bool:
        try:
            return self.hooks.quiet is not None and bool(self.hooks.quiet())
        except Exception:
            return False

    def _busy(self) -> bool:
        try:
            return self.hooks.busy is not None and bool(self.hooks.busy())
        except Exception:
            return False

    def _tell_held_later(self) -> None:
        """Say the held news once the user isn't busy any more (checked every ``HELD_NEWS_CHECK_S``)."""
        if self._held_task is not None and not self._held_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            held, self._held_news = self._held_news, []
            for text, note, listen, offer in held:
                self.announce(text, note, listen, offer)
            return

        async def tell() -> None:
            while self._held_news:
                await asyncio.sleep(HELD_NEWS_CHECK_S)
                if self._busy():
                    continue
                held, self._held_news = self._held_news, []
                for text, note, listen, offer in held:
                    self.announce(text, note, listen, offer)

        self._held_task = loop.create_task(tell(), name="held-news")

    async def _decided_before(self, item: Improvement) -> str | None:
        """Why a note mustn't be built by itself because of what became of the last one with its title: the
        user dropped that one, or it was built and didn't make it (failed, refused, nothing to change, undone).
        A new note gets a new row (only open ones are counted again), so its own report can't say so."""
        earlier = await self.journal.closed_like(item.kind, item.title)
        if earlier is None:
            return None
        report = earlier.report or {}
        if report.get("dropped"):
            return f"you dropped #{earlier.id}"
        if report.get("build"):
            return f"an earlier build of it (#{earlier.id}) ended {earlier.status.value.replace('_', ' ')}"
        return None

    def on_error(self, fingerprint: str, title: str, evidence: str) -> None:
        """An exception in the assistant's own code (from the error watcher). Returns at once; recorded later.

        With ``auto_build`` "errors" or "all", an error seen a second time is built: once may be a fluke.
        Ignored once a restart into an update is on its way: this process runs the old code, so its errors say
        nothing about the update (and may be the very ones it fixes).
        """
        if self._closed or self._restarting:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # called from another thread
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self.on_error, fingerprint, title, evidence)
            return
        self._spawn(self._record_error(fingerprint, title, evidence))

    async def _record_error(self, fingerprint: str, title: str, evidence: str) -> None:
        try:
            item, created = await self.journal.note(
                Kind.ERROR, title, source="watcher", fingerprint=fingerprint, evidence=evidence
            )
            self._changed(item, "noted" if created else f"seen {item.occurrences} times")
            self._notice(f"Noticed an error: {item.label()} (seen {item.occurrences}x)", logging.DEBUG)
            if self._settings.auto_build in ("errors", "all") and item.occurrences >= 2 and _auto_buildable(item):
                await self._auto_build(item)
        except Exception:
            log.warning("Couldn't record an error for self-improvement", exc_info=True)

    async def _auto_build(self, item: Improvement, *, promised: bool = False) -> tuple[Improvement | None, str | None]:
        """Queue a build the assistant starts by itself: (the queued improvement, or None and why it can't). Not
        while her Claude usage budget says no (``core/usage.py``): the user can still ask for it. By the last
        reading, with no fresh one: this can run inside her turn (``note_improvement``), and ``_build`` checks again
        on a fresh reading before it starts."""
        why = claude_usage.check(_budget_job(item))
        if why is not None:
            self._notice(f"Not building {item.label()} on my own: {why}", logging.INFO)
            return None, f"{BUDGET_REFUSAL}{why}."
        try:
            return await self.request_build(item.id, auto=True, promised=promised), None
        except EvolveError as exc:
            self._notice(f"Not building {item.label()} on my own: {exc}", logging.INFO)
            return None, str(exc)

    # ------------------------------------------------------------------ building

    async def request_build(
        self,
        improvement_id: int | None = None,
        request: str = "",
        *,
        title: str = "",
        auto: bool = False,
        promised: bool = False,
        install_when_ready: bool = False,
        silent: bool = False,
    ) -> Improvement:
        """Queue a build of an improvement, or of a new one described by ``request``. Raises EvolveError.

        ``title``: a short name for a new one (otherwise it's named after the start of ``request``).
        ``auto``: started by the assistant itself (``auto_build``), so a failure isn't announced, unless
        ``promised``: the user is told it's being built, so they hear how it ends either way.
        ``install_when_ready``: the user already said yes to installing it once it's built (and passes).
        ``silent``: asked for by an objective, which tells the user how it went itself: nothing is announced about it
        (ready, failed, cut off), and her usage budget doesn't hold it back (the user asked for the objective).
        ``builds_ahead`` then says how many builds run before it.
        """
        if self._closed:
            raise EvolveError("I'm shutting down, so I can't start a build now.")
        if not self._settings.enabled:
            raise EvolveError(TURNED_OFF)
        request = (request or "").strip()
        if improvement_id is None and not request:
            raise EvolveError("Tell me what to build or fix.")
        await self._require_repository()
        async with self._request_lock:
            created = False
            if improvement_id is None:
                name = _title_from(title) if (title or "").strip() else _title_from(request)
                # A noted item of that name is what this asks to build; one being built or already built isn't
                # changed (what it builds is what was asked then), and this request isn't lost in it unsaid.
                same = await self.journal.find_open(Kind.MISSING, name)
                if same is not None and same.status is not Status.NOTED:
                    raise EvolveError(_taken_text(same))
                item, created = await self.journal.note(Kind.MISSING, name, request, "user")
            else:
                item = await self._require(improvement_id)
                if item.status is Status.QUEUED and (item.report or {}).get("auto"):
                    # One she queued by herself, now asked for: it's the user's, so her usage budget won't skip it.
                    item = await self.journal.update(item.id, report={**(item.report or {}), "auto": False})
            _check_buildable(item)
            await self._check_daily_limit(item, created)
            item = await self._set(
                item,
                Status.QUEUED,
                error=None,
                report={
                    "request": request,
                    "auto": auto,
                    **({"promised": True} if auto and promised else {}),
                    **({"install": True} if install_when_ready else {}),
                    **({"silent": True} if silent else {}),
                },
                message="queued",
                notice=f"Queued {item.label()} for building",
            )
            self._enqueue(item.id)
        return item

    def builds_ahead(self, improvement_id: int) -> int:
        """How many builds must finish before this one starts: with every build slot (``parallel_builds``) taken,
        it starts as one of them frees up, so it's the queued ones before it beyond the free slots, plus one.

        0 when it's being built or starts now (a slot is free for it), or isn't queued here. Exact right after
        ``request_build``. With one slot it's the build in progress and those queued earlier. A build that has
        finished but is still being installed on its own (its merge re-checked) holds its slot until that's done.
        While a restart is pending nothing starts here at all (``restart_pending``): the next start builds it.
        """
        if improvement_id in self._active or improvement_id in self._taken or improvement_id not in self._waiting:
            return 0
        # A worker that took one up a moment ago has it in both, until the build starts.
        before = [i for i in self._waiting[: self._waiting.index(improvement_id)] if i not in self._taken]
        return self._after(len(before))

    def builds_ahead_of_new(self) -> int:
        """How many builds must finish before one queued right now starts (0: it starts at once)."""
        return self._after(len([i for i in self._waiting if i not in self._taken]))

    def _after(self, queued_before: int) -> int:
        free = self.parallel_builds - len(self._taken.union(self._active))
        return 0 if queued_before < free else queued_before - max(free, 0) + 1

    async def _building_now(self) -> Improvement | None:
        """The earliest-started build a restart would wait for (``_builds_now``)."""
        builds = await self._builds_now()
        return builds[0] if builds else None

    async def _builds_now(self) -> list[Improvement]:
        """The builds a restart would wait for, in the order they started: those whose builder has begun (one still
        getting its copy of the code ready is queued again instead)."""
        begun = [i for i, progress in list(self._active.items()) if progress.stage != STAGE_PREPARING]
        return await self._items(begun)

    async def active_build(self) -> Improvement | None:
        """The improvement this process has been building the longest right now, if any (``active_builds``)."""
        builds = await self.active_builds()
        return builds[0] if builds else None

    async def active_builds(self) -> list[Improvement]:
        """Every improvement this process is building right now, in the order they started."""
        return await self._items(list(self._active))

    async def _items(self, improvement_ids: list[int]) -> list[Improvement]:
        items = [await self.journal.get(i) for i in improvement_ids]
        return [item for item in items if item is not None]

    async def build_blocker(self, improvement_id: int | None) -> str | None:
        """Why a build can't be queued right now (None: it can), for the build tool to say before the user is
        asked to confirm it: a "yes" that is sure to be refused wastes a question. ``request_build`` checks again.

        A new request (``improvement_id`` None) that would be refused isn't noted either: the brain notes it.
        """
        try:
            if self._closed:
                raise EvolveError("I'm shutting down, so I can't start a build now.")
            if not self._settings.enabled:
                raise EvolveError(TURNED_OFF)
            await self._require_repository()
            if improvement_id is not None:
                _check_buildable(await self._require(improvement_id))
        except EvolveError as exc:
            return str(exc)
        reason = await self._daily_limit_reason()
        return f"{_capital(reason)}." if reason else None

    def _enqueue(self, improvement_id: int) -> None:
        self._waiting.append(improvement_id)
        self._queue.put_nowait(improvement_id)
        self._ensure_worker()

    def _unwait(self, improvement_id: int) -> None:
        with contextlib.suppress(ValueError):
            self._waiting.remove(improvement_id)

    async def _require_repository(self) -> None:
        if self._repo_ready:
            return
        git = self.git

        def probe() -> str | None:
            if git.is_repo() and git.has_commits():
                return None
            return NOT_SET_UP if git.available() else NO_GIT

        problem = await asyncio.to_thread(probe)
        if problem:
            raise EvolveError(problem)
        self._repo_ready = True

    async def _check_daily_limit(self, item: Improvement, created: bool) -> None:
        reason = await self._daily_limit_reason()
        if reason:
            raise EvolveError(f"I noted it as #{item.id}, but {reason}." if created else f"{_capital(reason)}.")

    async def _daily_limit_reason(self) -> str | None:
        limit = self._settings.max_builds_per_day
        started = await self.journal.builds_since(self._clock() - DAY_S)
        waiting = len(await self.journal.list([Status.QUEUED], limit=10_000))
        if started + waiting < limit:
            return None
        if limit <= 0:
            return "building is switched off (max_builds_per_day is 0)"
        # nothing builds it later by itself: say so
        return f"I've reached my limit of {limit} build{'' if limit == 1 else 's'} a day; ask me again later"

    def _ensure_worker(self) -> None:
        """Keep one worker per build slot alive, all taking builds from the same queue in turn."""
        if self._closed:
            return
        self._workers = {w for w in self._workers if not w.done()}
        loop = asyncio.get_running_loop()
        while len(self._workers) < self.parallel_builds:
            self._workers.add(loop.create_task(self._work(), name=f"evolve-builds-{len(self._workers) + 1}"))

    async def _work(self) -> None:
        while True:
            improvement_id = await self._queue.get()
            self._taken.add(improvement_id)
            try:
                await self._build(improvement_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Building improvement #%s failed", improvement_id)
            finally:
                self._taken.discard(improvement_id)
                self._queue.task_done()

    async def _build(self, improvement_id: int) -> None:
        peek = await self.journal.get(improvement_id)
        if peek is not None and (peek.report or {}).get("auto"):
            async with self._own_builds:  # one of hers at a time
                await self._build_one(improvement_id)
        else:
            await self._build_one(improvement_id)

    async def _build_one(self, improvement_id: int) -> None:
        peek = await self.journal.get(improvement_id)
        if peek is not None and (peek.report or {}).get("auto"):
            await claude_usage.refresh()  # a fresh usage reading for the check below, outside the lock
        # Under the lock that requests and rejects take: the daily limit counts queued items and started builds
        # (so an item must never be neither), and a reject must see either "queued" or a build it can stop.
        async with self._request_lock:
            self._unwait(improvement_id)
            if self._restarting:
                return  # it stays queued in the journal: the next start builds it
            item = await self.journal.get(improvement_id)
            if item is None or item.status is not Status.QUEUED:
                return  # dropped while it waited
            # One she queued by herself is checked against her usage budget again as it starts: several are queued
            # at once (the night shift), and the ones before it may have used the day's share. Not built: back to
            # noted, so it can be asked for, or proposed again another night.
            queued = item.report or {}
            why = claude_usage.check(_budget_job(item)) if queued.get("auto") and not queued.get("redo") else None
            if why is not None:
                await self._set(
                    item,
                    Status.NOTED,
                    report={"budget": why},
                    message="not built: usage budget",
                    notice=f"Not building {item.label()} on my own now: {why}",
                )
                if _promised(queued):  # the user was told it's being built
                    self.announce(
                        f"I didn't build {spoken_name(item)} after all: {why}. You can still ask me to.",
                        f"you told the user you didn't build #{item.id} on your own, to save their Claude usage",
                        listen=False,
                    )
                return
            progress = _Progress(
                self.paths.builds / f"{item.id}.log", lambda line: self._notice(f"#{item.id}: {line}", logging.DEBUG)
            )
            item = await self._set(item, Status.BUILDING, message="building", notice=f"Building {item.label()}...")
            build_id = await self.journal.start_build(item.id)
            self._active[item.id] = progress
            self._running.add(item.id)
            self._no_build.clear()
        queued = item.report or {}
        request, auto, promised = str(queued.get("request") or ""), bool(queued.get("auto")), _promised(queued)
        # ``promised``: started by the assistant itself (``auto``), but the user was told so: they hear how it ends.
        # "install": the user asked to have it installed once it's ready. Both outlast a requeue after a restart.
        keep = {flag: True for flag in ("promised", "install", "silent", "redone") if queued.get(flag)}
        # A ready build that no longer merged (``_conflicted``): its change, to apply again on the newer code. Kept
        # only while it's queued; afterwards "redone" says it had its one redo.
        redo = queued.get("redo") if isinstance(queued.get("redo"), dict) else None
        if redo is not None:
            keep |= {"redo": redo, "redone": True}
        branch = branch_name(item)
        worktree = self._worktree(item.id)
        self._claim(item.id)
        progress.write(f"=== Building #{item.id}: {item.title}" + (f"\nRequest: {request}" if request else ""))
        verdict = _Verdict(Status.FAILED, error="the build didn't finish")
        base: str | None = None
        # One she started by herself counts against her daily Claude usage share; one the user asked for doesn't.
        charged = claude_usage.charged(_budget_job(item)) if auto else contextlib.nullcontext()
        try:
            base = await self._prepare_unless_dropped(item.id, worktree, branch)
            if base is None:
                verdict = _Verdict(Status.REJECTED, DROPPED)  # dropped while it waited for the repository
            elif self._restarting:
                # An install took the repository first (it waits for no build that hasn't really begun): this one
                # starts over after the restart, at no cost to the daily limit.
                verdict = _Verdict(Status.QUEUED)
            else:
                async with charged:
                    verdict = await self._develop(item, request, worktree, base, progress, redo)
        except asyncio.CancelledError:
            verdict = self._stopped()
            raise
        except Exception as exc:
            log.warning("Building %s failed", item.label(), exc_info=True)
            verdict = _Verdict(  # the raw text is for the journal; it's no sentence to say out loud
                Status.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                spoken=f"something went wrong on my side; the details are in octavia evolve show {item.id}",
            )
        finally:
            try:
                async with self._request_lock:  # a reject either reaches this build or finds it finished
                    self._active.pop(item.id, None)
                    # A reject from another process (``octavia evolve reject``) only reaches the journal.
                    if item.id in self._dropped or await self._rejected_meanwhile(item.id):
                        self._dropped.discard(item.id)
                        verdict.status, verdict.error, verdict.dropped = Status.REJECTED, DROPPED, True
                    progress.write(f"=== {verdict.status.value}" + (f": {verdict.error}" if verdict.error else ""))
                    item = await self._record(item, verdict, build_id, base, branch, request, auto, keep)
                await self._clean_up(worktree, None if verdict.status is Status.READY else branch)
                self._release(item.id)
            finally:
                self._running.discard(item.id)
                if not self._running:  # the others may still be going
                    self._no_build.set()
        await self._conclude(item, verdict, quiet=auto and not promised)

    def _stopped(self) -> _Verdict:
        """The verdict on a build that ``close`` cut off. An intentional restart queues it again (the next start
        builds it); any other shutdown fails it (and the next start says so, if it was asked for)."""
        if self._restarting:
            return _Verdict(Status.QUEUED)
        return _Verdict(Status.FAILED, error=STOPPED, stopped=True)

    async def _no_longer_wanted(self, improvement_id: int) -> bool:
        """Whether the user dropped the build running here: by asking this process, or from a terminal (then it
        counts as dropped here too, so the build's end and its verdict agree)."""
        if improvement_id in self._dropped:
            return True
        if await self._rejected_meanwhile(improvement_id):
            self._dropped.add(improvement_id)
            return True
        return False

    async def _rejected_meanwhile(self, improvement_id: int) -> bool:
        """Whether another process (``octavia evolve reject``) dropped it while it was being built here."""
        try:
            current = await self.journal.get(improvement_id)
        except Exception:
            log.debug("Couldn't re-read #%s", improvement_id, exc_info=True)
            return False
        return current is not None and current.status is Status.REJECTED

    async def _prepare_unless_dropped(self, improvement_id: int, worktree: Path, branch: str) -> str | None:
        """The build's copy of the code (its base commit), or None when it was dropped while it waited for the
        repository (an install can hold it for minutes): then nothing was made, and it never starts. A drop cancels
        only the wait (``reject``), never the copying itself, so nothing is left half made."""
        waiting = asyncio.ensure_future(self._repo_lock.acquire())
        self._preparing[improvement_id] = waiting
        try:
            await waiting
        except asyncio.CancelledError:
            if waiting.done() and not waiting.cancelled():
                self._repo_lock.release()  # the lock came just as this build was stopped: never kept
            current = asyncio.current_task()
            if (
                waiting.cancelled()
                and improvement_id in self._dropped
                and (current is None or not current.cancelling())
            ):
                return None
            raise
        finally:
            self._preparing.pop(improvement_id, None)
        try:
            if improvement_id in self._dropped:
                return None
            base = await asyncio.to_thread(self.git.head)
            dirty = await asyncio.to_thread(self._dirty_files)
            await asyncio.to_thread(self.git.add_worktree, worktree, branch, base)
        finally:
            self._repo_lock.release()
        if dirty:
            self._notice(
                f"The project has uncommitted changes ({_listing(dirty)}); this build starts from the last commit "
                "and won't include them.",
                logging.WARNING,
            )
        return base

    async def _develop(
        self,
        item: Improvement,
        request: str,
        worktree: Path,
        base: str,
        progress: _Progress,
        redo: dict[str, Any] | None = None,
    ) -> _Verdict:
        """Run the builder, commit its change and judge it; one more round if only the final checks failed.

        ``redo``: an earlier build of it that no longer merged. Its change is applied on the newer code first; when it
        applies cleanly, the checks judge it with no builder at all, and otherwise the builder only resolves the
        clashes."""
        feedback: str | None = None
        clashes = await self._reapply(worktree, redo, progress) if redo is not None else None
        previous: GateReport | None = None  # the first round's report, when there is a second round
        result: BuildResult | None = None
        turns, cost = 0, None
        for attempt in (1, 2):
            # Each round costs a builder session (minutes, Claude usage): not for a build that's no longer wanted.
            if await self._no_longer_wanted(item.id):
                return _Verdict(Status.REJECTED, DROPPED, result, previous, turns, cost)
            if self._closed:  # shut down meanwhile: ``close`` stopped the builders running then, not one started now
                stopped = self._stopped()
                stopped.result, stopped.turns, stopped.cost_usd = result, turns, cost
                return stopped
            # (A pending restart waits for this build, second round included: it isn't cut short here.)
            progress.at(
                STAGE_BUILDING if attempt == 1 else STAGE_RETRYING,
                "The builder starts" if attempt == 1 else "The final checks failed; giving the builder one more round",
            )
            if attempt == 1 and clashes == []:
                # The earlier build went on cleanly: what it said it does still holds, and the checks decide.
                result = BuildResult(ok=True, summary=str(redo.get("summary") or "") if redo else "", details=REAPPLIED)
            else:
                task = BuildTask(item, request, feedback, conflicts=clashes if attempt == 1 else None)
                result = await self.builder.build(task, worktree, progress)
            turns += result.turns
            if result.cost_usd is not None:
                cost = (cost or 0.0) + result.cost_usd
            if not result.ok and self._closed:  # the builder was stopped because the assistant is closing
                stopped = self._stopped()
                stopped.result, stopped.turns, stopped.cost_usd = result, turns, cost
                return stopped
            status, error, report = await self._judge(item, attempt, result, previous, worktree, base, progress)
            if status is None and report is not None:
                feedback, previous = _feedback(report), report
                self._notice(f"{item.label()} failed its checks; trying once more", logging.INFO)
                continue
            verdict = _Verdict(status or Status.FAILED, error, result, report, turns, cost)
            if verdict.status is Status.READY and report is not None and clashes:
                # The builder chose what stays where its change met newer code (the user's own edits, maybe): that
                # is for the user to look at, so it never installs by itself.
                flags = [*report.flags, f"{RESOLVED_CLASHES}: {_listing(clashes, 6)}"]
                verdict.report = report = dataclasses.replace(report, flags=flags)
            if verdict.status is Status.FAILED and report is not None and error == _reasons(report):
                # The gate's reasons may hold git output or parser messages: fine for the journal, not to say.
                verdict.spoken = _spoken_gate_failure(report, item.id)
            return verdict
        raise AssertionError("unreachable")  # pragma: no cover - the second round always returns

    async def _reapply(self, worktree: Path, redo: dict[str, Any], progress: _Progress) -> list[str] | None:
        """An earlier build's change (``redo["patch"]``) applied in ``worktree``, three ways where the code moved on:
        [] when it went on cleanly, else the files it clashes in (left with conflict markers for the builder), None
        when it couldn't be applied at all (the builder then builds it anew)."""
        patch = str(redo.get("patch") or "")
        if not patch:
            return None
        try:
            clashes = await asyncio.to_thread(self.git.apply_3way, worktree, patch)
        except (GitError, OSError) as exc:
            progress.write(f"The earlier build doesn't apply to the newer code ({exc}); building it anew")
            return None
        progress.write(
            f"The earlier build clashes with the newer code in {_listing(clashes, 6)}; the builder resolves that"
            if clashes
            else "The earlier build applies cleanly to the newer code; checking it again"
        )
        return clashes

    async def _judge(
        self,
        item: Improvement,
        attempt: int,
        result: BuildResult,
        previous: GateReport | None,
        worktree: Path,
        base: str,
        progress: _Progress,
    ) -> tuple[Status | None, str | None, GateReport | None]:
        """(status, error, report) for one round; status None means "try once more" (report: what failed)."""
        if not result.ok:
            return Status.FAILED, result.error or "the builder stopped before finishing", previous
        if result.outcome != "changed":
            if previous is not None:  # it couldn't fix the checks: the first round's failure stands
                return Status.FAILED, _reasons(previous), previous
            if result.outcome == "gave_up":  # it tried and couldn't: a failure, not a finding that nothing's needed
                return Status.FAILED, result.no_change_reason or result.summary or "I couldn't make it work", None
            return Status.NO_CHANGE, result.no_change_reason or result.summary or "there was nothing to change", None
        subject = (
            f"{item.title} (improvement #{item.id})" if attempt == 1 else f"Fix the checks of improvement #{item.id}"
        )
        message = f"{subject}\n\n{result.summary}" if result.summary else subject
        if self.gate.settings.run_checks:
            try:  # the build's own files formatted and safe lint fixed first: neither fails it or costs a round
                tidied = await self.gate.tidy(worktree, base)
            except GitError as exc:
                log.warning("Couldn't tidy %s: %s", item.label(), exc)
            else:
                if tidied:
                    progress.write(f"Tidied the changed files ({tidied})")
        # The commit lands in the repository's shared objects and refs, which another build finishing at the same
        # moment (or an install) may be writing too: one at a time, like every other change to them.
        async with self._repo_lock:
            committed = await asyncio.to_thread(self.git.commit_all, worktree, message, self.author)
        if committed is None:
            if previous is not None:
                return Status.FAILED, _reasons(previous), previous
            return Status.NO_CHANGE, "the builder didn't change anything", None
        if await self._no_longer_wanted(item.id):  # the checks take minutes: not for a build that's no longer wanted
            return Status.REJECTED, DROPPED, previous
        # A task of its own, so dropping the build stops the checks (``reject``), or its wait for them, without
        # stopping the worker.
        checking = self._checking[item.id] = asyncio.ensure_future(self._gated_check(worktree, base, progress))
        try:
            report = await checking
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if (current is None or not current.cancelling()) and item.id in self._dropped:
                return Status.REJECTED, DROPPED, previous  # only the checks were stopped: the user dropped it
            raise  # the worker itself is being stopped (``close``)
        finally:
            self._checking.pop(item.id, None)
        if report.passed:
            return Status.READY, None, report
        refusals = [r for r in report.reasons if is_refusal(r)]
        if refusals:
            return Status.REJECTED, "; ".join(refusals), report
        if report.checks_unstarted:  # never judged: no second round to fix what may not be broken
            return Status.FAILED, _reasons(report), report
        if attempt == 1 and report.checks_failed and self._settings.retry_failed_checks:
            return None, None, report
        return Status.FAILED, _reasons(report), report

    async def _gated_check(self, worktree: Path, base: str, progress: _Progress) -> GateReport:
        """The gate's verdict, once no other build's checks are running: the builders work side by side, but two
        full test suites at once would slow each other down and fail timing-sensitive tests of good builds."""
        if self._gate_lock.locked():
            progress.at(STAGE_WAITING_CHECKS, "Waiting for another build's final checks to finish")
        async with self._gate_lock:
            progress.at(STAGE_CHECKING, "Running the final checks")
            return await self.gate.check(worktree, base)

    async def _record(
        self,
        item: Improvement,
        verdict: _Verdict,
        build_id: int,
        base: str | None,
        branch: str,
        request: str,
        auto: bool,
        keep: dict[str, Any],
    ) -> Improvement:
        """Write the outcome to the journal. Never raises: the worktree must be cleaned up whatever happens.

        A build queued again (cut off by a restart) keeps only what the next build needs, and its build record
        (outcome "queued") doesn't count toward the daily limit: the rebuild does.
        """
        report: dict[str, Any] = {"request": request, "auto": auto, **keep}
        promised = bool(keep.get("promised"))
        requeued = verdict.status is Status.QUEUED
        if not requeued:
            report.pop("redo", None)  # done with: only a build that starts over needs it again
        if verdict.report is not None and not requeued:
            report["gate"] = verdict.report.to_dict()
        if verdict.result is not None and not requeued:
            report["build"] = verdict.result.to_dict()
        if verdict.dropped:  # the user's decision: it isn't built again by itself (``_auto_buildable``)
            report["dropped"] = True
        elif (
            verdict.stopped
            and verdict.status is Status.FAILED
            and (promised or not auto)
            and not keep.get("silent")
            and self.hooks.announce is not None
        ):
            # It was promised aloud ("I'll say when it's ready"): the next start says what became of it. (Not one
            # asked for in a terminal: Ctrl+C there is the user stopping it.)
            report["stopped"] = True
        ready = verdict.status is Status.READY
        try:
            await self.journal.finish_build(build_id, verdict.status.value, verdict.turns, verdict.cost_usd)
            return await self._set(
                item,
                verdict.status,
                error=verdict.error,
                branch=branch if ready else None,
                base_commit=base,
                report=report,
                message=_describe_report(verdict.report) if ready else (verdict.error or ""),
                notice=_outcome_notice(item, verdict),
                level=logging.INFO if ready or verdict.status in (Status.NO_CHANGE, Status.QUEUED) else logging.WARNING,
            )
        except Exception:
            log.warning("Couldn't record the outcome of %s", item.label(), exc_info=True)
            return item

    async def _clean_up(self, worktree: Path, branch: str | None) -> None:
        """Remove the worktree, and the branch unless it holds a build that's ready (``branch`` None)."""
        try:
            async with self._repo_lock:
                await asyncio.to_thread(self.git.remove_worktree, worktree)
                if branch is not None:
                    await asyncio.to_thread(self.git.delete_branch, branch)
        except (GitError, OSError) as exc:
            log.warning("Couldn't clean up after building (%s): %s", worktree, exc)

    async def _conclude(self, item: Improvement, verdict: _Verdict, *, quiet: bool) -> None:
        """Tell the user how it went (and install it if allowed to without asking). ``quiet``: nobody was told
        it's being built (the assistant started it by itself), so only good news is said."""
        if self._closed:
            return  # shutting down or restarting: it's recorded, and the next start lists what's ready
        if (item.report or {}).get("silent"):
            return  # an objective's: it tells the user how it went, and installing waits for their word
        result, report = verdict.result, verdict.report
        summary = result.summary.strip() if result else ""
        if verdict.status is Status.READY and report is not None:
            # A drop can land after the build recorded "ready" (while its worktree was being removed): no good
            # news, or installing, for something the user just dropped.
            if not await self._still_ready(item.id):
                return
            asked_for = bool((item.report or {}).get("install"))
            may = self._may_install_itself(report, asked_for)
            failed: str | None = None  # why an install it tried on its own didn't happen (it's still ready)
            if may:
                try:
                    restarting = await self._install_itself(item, asked_for=asked_for)
                except EvolveError as exc:
                    self._notice(f"Didn't install {item.label()} on my own: {exc}", logging.WARNING)
                    current = await self.journal.get(item.id)
                    if current is None or current.status is not Status.READY:
                        # Failed (a conflict, or its checks failed after merging): said at once, even for one she
                        # started by herself, since an install that fails unheard leaves the user thinking it's in
                        # (#172 failed that way, and they only found out by asking). Dropped meanwhile: they know.
                        if current is not None and current.status is Status.FAILED:
                            self.announce(
                                f"I built {spoken_name(item)}, but installing it didn't work out. {exc}", None
                            )
                        return
                    failed = str(exc)
                except Exception:
                    log.warning("Couldn't install %s on my own", item.label(), exc_info=True)
                    if not await self._still_ready(item.id):
                        return
                    failed = "Something went wrong on my side."
                else:
                    if not restarting:  # it's in the code, but not running yet
                        done = "I installed it like you asked." if asked_for else "I installed an update on my own."
                        self.announce(_join(done, summary, "Restart me to start using it."), None)
                    return
            ready = len(await self.ready_ids())
            building = await self._still_building(item.id)
            # Asked to install it once ready: say why it didn't happen (flagged for a look). An install it tried on its
            # own that failed is said whoever asked for it.
            held = asked_for and not may
            text = _ready_text(item, summary, report, ready, held=held, failed=failed, building=building)
            self.announce(text, _ready_note(item, report, ready, failed=failed, building=building), news=True)
            await self._mark_announced(item.id)
            return
        if quiet:
            return  # nobody is waiting for it, so a failure is only logged (and listed)
        reason = _spoken_reason(verdict.spoken or verdict.error or "")
        if verdict.status is Status.FAILED:
            self.announce(f"I couldn't build {spoken_name(item)}: {reason}.", None)
        elif verdict.status is Status.NO_CHANGE:
            self.announce(f"I looked into {spoken_name(item)}, but {_lower_first(reason)}.", None)
        elif verdict.status is Status.REJECTED and not verdict.dropped:
            refusal = _spoken_refusal(verdict.error or "")
            self.announce(f"I built {spoken_name(item)}, but it {refusal}, so I threw it away.", None)

    async def _install_itself(self, item: Improvement, *, asked_for: bool) -> bool:
        """Install a finished build without being asked right now: ``auto_install``, or one the user asked to have
        installed once it's ready (``asked_for``). Whether the assistant restarts into it. Raises EvolveError."""
        _, restarting = await self._protected(self._install([item.id], automatic=True, asked_for=asked_for))
        return restarting

    async def _still_building(self, finished_id: int) -> list[Improvement]:
        """The builds still to come once ``finished_id`` is done: queued ones, and those really being built (here
        or in a terminal; a "building" row nobody runs is left over from a crash), oldest first."""
        items = await self.journal.list([Status.QUEUED, Status.BUILDING], limit=10_000)
        return sorted(
            (
                i
                for i in items
                if i.id != finished_id
                and (i.status is Status.QUEUED or self._building_here(i.id) or self._building_elsewhere(i.id))
            ),
            key=lambda i: (i.status is not Status.BUILDING, i.id),
        )

    def _building_here(self, improvement_id: int) -> bool:
        """Whether a worker here has it: being built, or taken up a moment ago."""
        return improvement_id in self._active or improvement_id in self._taken

    async def _mark_announced(self, improvement_id: int) -> None:
        """Remember (in the journal, so it outlasts a restart) that the user was told this build is ready: the
        brain then doesn't bring it up again by itself. Not a status change, so nobody hears of it as news."""
        try:
            current = await self.journal.get(improvement_id)
            if current is None or current.status is not Status.READY:
                return
            report = {**(current.report or {}), "announced": True}
            await self.journal.update(improvement_id, expect=Status.READY, report=report)
        except StatusChanged:
            pass  # installed or dropped meanwhile: nothing left to remember
        except Exception:
            log.debug("Couldn't record that #%s was announced", improvement_id, exc_info=True)

    async def _still_ready(self, improvement_id: int) -> bool:
        current = await self.journal.get(improvement_id)
        return current is not None and current.status is Status.READY

    def _may_install_itself(self, report: GateReport, asked_for: bool) -> bool:
        """``auto_install: safe_changes`` takes only what nothing was flagged in. A build the user asked to have
        installed once ready also goes in without tests of its own, but whatever else deserves a look (a new tool
        that would run without asking included: nobody heard about that when they said yes) waits for them."""
        if not report.passed:
            return False
        if (
            self._settings.auto_install == "safe_changes"
            and not report.sensitive
            and not any(t.risk == "safe" for t in report.new_tools)
        ):
            return True
        return asked_for and _preapproved(report)

    @property
    def changing_code(self) -> bool:
        """An install or undo is under way (testing can take minutes): a restart now would cut it off, and what
        it's about to install belongs in that restart. The app waits for this before restarting."""
        return bool(self._installing) or any(not task.done() for task in self._critical)

    def build_progress(self, improvement_id: int) -> tuple[str, int, float] | None:
        """How a build running in this process is going, for her status panel: (stage, the builder's steps so far,
        seconds since it started). None when it isn't being built here."""
        progress = self._active.get(improvement_id)
        if progress is None:
            return None
        return progress.stage, progress.steps, time.monotonic() - progress.started

    @property
    def installing_ids(self) -> frozenset[int]:
        """Ready builds being merged and tested right now (an install takes minutes)."""
        return frozenset(self._installing)

    # ------------------------------------------------------------------ installing

    async def install(
        self, improvement_ids: int | Iterable[int] = (), *, all_ready: bool = False, wait: bool = True
    ) -> str:
        """Install ready builds (``all_ready``: every one there is) with one restart; returns what to say. Raises
        EvolveError when none of them could be installed (one that can't is left out, and the reply says why).

        They're merged beside the main checkout first, and several at once (or one built on older code) are
        tested together there, which takes minutes: the main checkout only ever gets code that passed, and only
        as a fast-forward. With ``wait`` off (the voice tool) this returns as soon as that testing starts, and the
        outcome is announced. Under the supervisor the assistant then restarts into the new code: after the reply,
        and after the builds in progress if any.
        """
        ids = [improvement_ids] if isinstance(improvement_ids, int) else list(improvement_ids)
        if wait:
            said, _ = await self._protected(self._install(ids, all_ready=all_ready, automatic=False))
            return said
        testing: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        task = self._critical_task(self._install(ids, all_ready=all_ready, automatic=False, testing=testing))
        later = False  # the outcome is announced rather than returned

        def announce_outcome(done: asyncio.Task[tuple[str, bool]]) -> None:
            if later:
                self._announce_install(done, said_already=testing.done())

        task.add_done_callback(announce_outcome)
        try:
            await asyncio.wait({task, testing}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            later = True  # the turn was cut off: the user still hears how it went
            raise
        if task.done():
            said, _ = task.result()
            return said
        later = True
        return testing.result()

    def _announce_install(self, task: asyncio.Task[tuple[str, bool]], *, said_already: bool) -> None:
        """How an install the caller stopped waiting for ended. Success was said before the restart was asked
        for (``said_already``), so the restart doesn't cut it off; a failure asks for no restart."""
        if task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, EvolveError):
            self.announce(str(exc), None)
        elif exc is not None:
            log.error("Installing failed", exc_info=exc)
            self.announce("Something went wrong while installing, so nothing was installed.", None)
        elif not said_already:
            self.announce(task.result()[0], None, listen=False)

    async def _install(
        self,
        improvement_ids: list[int],
        *,
        all_ready: bool = False,
        automatic: bool,
        asked_for: bool = False,
        testing: asyncio.Future[str] | None = None,
    ) -> tuple[str, bool]:
        """(what to say, whether the assistant is restarting into it). ``automatic``: nobody is waiting on it
        right now (``auto_install``, or ``asked_for``: the user asked to have it installed once it's ready).
        ``testing`` (only when the caller may stop waiting) is resolved with what to say meanwhile once a test run
        starts; what this says in the end is then announced here, before the restart is asked for."""
        async with self._repo_lock:
            items, left_out = await self._installable(improvement_ids, all_ready)
            if not items:
                raise EvolveError(_nothing_installed(left_out))
            branches = {i.id: i.branch or "" for i in items}
            worktree = self.paths.worktrees / side_worktree_name("install")
            side_branch = f"{BRANCH_PREFIX}/{worktree.name}"
            # Until they're recorded as installed: a drop or an undo now would be overtaken by this install.
            self._installing.update(branches)
            try:
                try:
                    previous = await asyncio.to_thread(self.git.head)
                    await asyncio.to_thread(self.git.add_worktree, worktree, side_branch, previous)
                except GitError as exc:
                    log.warning("Couldn't set up a worktree to install into: %s", exc)
                    raise EvolveError(NO_WORKTREE) from exc
                check = self.gate.settings.run_checks and (len(items) > 1 or items[0].base_commit != previous)
                installed, clashed = await self._merge_beside(
                    worktree,
                    side_branch,
                    previous,
                    items,
                    left_out,
                    testing if check else None,
                    check=check,
                    asked=not automatic or asked_for,
                    automatic=automatic,
                )
                pending = self._pending = self._pending or _Pending(previous_head=previous)
                for item in installed:
                    pending.installed.append(item)
                    if not automatic or asked_for:
                        pending.asked_for.add(item.id)
                pending.asked_now = pending.asked_now or not automatic
                # Whoever starts the new code next (this restart, or the user after a terminal install), the
                # supervisor watches that start: a crash reverts every update it runs for the first time.
                await self._write_health(pending)
                for item in installed:
                    await self._drop_branch(branches[item.id])
                left_out.extend(await self._leave_out(clashed, asked=not automatic or asked_for))
            finally:
                self._installing.difference_update(branches)
                try:
                    await asyncio.to_thread(self.git.remove_worktree, worktree)
                    await asyncio.to_thread(self.git.delete_branch, side_branch)
                except (GitError, OSError) as exc:  # the next start removes a leftover
                    log.warning("Couldn't clean up after an install: %s", exc)
            answered = testing is not None and testing.done()  # the caller has moved on: say how it went
            if not self._can_restart():
                said = _join(_installed_text(installed, restarting=False), *left_out)
                if answered:
                    self.announce(said, None)
                return said, False
            if automatic:  # nobody is waiting on it: say so before going quiet (the app restarts once it's said)
                heads_up = AUTO_INSTALL_HEADS_UP
                if asked_for:
                    heads_up = (
                        f"{_capital(spoken_name(installed[0]))} is ready, and I installed it like you asked. "
                        "Restarting, back in a few seconds."
                    )
                self.announce(heads_up, None, listen=False)
            building = await self._builds_now()
            waiting = f" once I've finished building {_names(building)}" if building else ""
            said = _join(_installed_text(installed, restarting=True, waiting=waiting), *left_out)
            if answered:
                self.announce(said, None, listen=False)
            # Still under the lock: an install or undo waiting for it joins the same restart.
            if not await self._schedule_restart():
                return _join(_installed_text(installed, restarting=False), *left_out), False
        return said, True

    async def _installable(self, improvement_ids: list[int], all_ready: bool) -> tuple[list[Improvement], list[str]]:
        """The ones that can be merged now, and why each other one can't (said out loud)."""
        if all_ready:
            improvement_ids = await self.ready_ids()
            if not improvement_ids:
                raise EvolveError(NOTHING_READY)
        if not improvement_ids:
            raise EvolveError("Tell me which update to install.")
        dirty = await asyncio.to_thread(self._dirty_files)
        items: list[Improvement] = []
        left_out: list[str] = []
        for improvement_id in dict.fromkeys(improvement_ids):  # once each, in the order asked
            try:
                items.append(await self._ready_to_merge(improvement_id, dirty))
            except EvolveError as exc:
                left_out.append(str(exc))
        return items, left_out

    async def ready_ids(self) -> list[int]:
        """Every improvement that's built and ready to install, oldest first."""
        return sorted(i.id for i in await self.journal.list([Status.READY], limit=10_000))

    async def _ready_to_merge(self, improvement_id: int, dirty: list[str]) -> Improvement:
        item = await self._require(improvement_id)
        _check_installable(item)
        branch = item.branch or ""
        if not branch or not await asyncio.to_thread(self.git.branch_exists, branch):
            await self._set(item, Status.FAILED, error="its branch is gone", message="branch missing")
            raise EvolveError(f"I can't find the build of #{item.id} any more. Ask me to build it again.")
        if await asyncio.to_thread(self.git.is_ancestor, branch, "HEAD"):
            # Merged already (by hand, or by an install cut off by a shutdown): merging again would be a no-op
            # whose "merge commit" is just HEAD, and undoing that would revert something else.
            await self._set(item, Status.INSTALLED, branch=None, error="it was merged outside the assistant")
            await self._drop_branch(branch)
            raise EvolveError(f"#{item.id} is already in my code.")
        if dirty:
            # Uncommitted work is left alone, unless the update changes the same files: then it waits for a commit.
            clash = _overlap(dirty, await asyncio.to_thread(self.git.branch_files, branch))
            if clash:
                raise EvolveError(f"#{item.id}: {DIRTY.format(files=_listing(clash))}")
        return item

    async def _merge_beside(
        self,
        worktree: Path,
        side_branch: str,
        previous: str,
        items: list[Improvement],
        left_out: list[str],
        testing: asyncio.Future[str] | None,
        *,
        check: bool,
        asked: bool = False,
        automatic: bool = False,
    ) -> tuple[list[Improvement], list[Improvement]]:
        """Merge them one after another in ``worktree`` (a fresh one at ``previous``, on ``side_branch``), run the
        checks there (``check``), and only if all is well move the main checkout forward to the result and record
        them as installed. (installed, clashed: ones that conflict only with an update merged before them).

        When several fail together and the failing tests (or lint) are in files only some of them changed, those
        are left out (still ready) and the rest tried once more: one update's own tests shouldn't hold up the
        others. Anything that fails leaves the main checkout untouched, and every build that didn't get in stays
        ready unless it can't ever go in as it is (one that conflicts is redone once: ``_conflicted``; ``asked``: the
        user asked for this install, so a redone one installs once it's ready). Checks that passed only after the
        gate's second look at failing tests (``Gate.recheck``) never install by themselves (``automatic``): those
        stay ready for the user.
        """
        touched: dict[int, set[str]] = {}
        if check and len(items) > 1:  # to tell whose tests failed, should they fail together
            for item in items:
                try:
                    files = await asyncio.to_thread(self.git.branch_files, item.branch or "")
                except GitError:
                    files = []
                touched[item.id] = {_norm_path(f) for f in files}
        merged, clashed = await self._merge_into(worktree, items, left_out, asked=asked)
        if check:
            if testing is not None and not testing.done():
                testing.set_result(_testing_text(len(merged), restarts=self._can_restart()))
            checks, excused = await self._test(worktree, [item for item, _ in merged], previous)
            if any(not c.ok for c in checks):
                tested = [item for item, _ in merged]
                culprits = _culprits(tested, checks, touched)
                if culprits is None:
                    failed = [c.name for c in checks if not c.ok]
                    raise EvolveError(await self._failed_together(tested, failed, left_out, clashed, len(items) > 1))
                rest = [item for item in tested if item not in culprits]
                self._notice(
                    f"The failing checks are in files {_labels(culprits)} changed; trying {_labels(rest)} without "
                    f"{'it' if len(culprits) == 1 else 'them'}",
                    logging.INFO,
                )
                await asyncio.to_thread(self.git.add_worktree, worktree, side_branch, previous)  # start over
                merged, more = await self._merge_into(worktree, rest, left_out, asked=asked)
                clashed += more
                again, excused = await self._test(worktree, [item for item, _ in merged], previous)
                if any(not c.ok for c in again):
                    failed = [c.name for c in checks if not c.ok]
                    raise EvolveError(await self._failed_together(tested, failed, left_out, clashed, True))
                left_out.append(_culprit_text(culprits))
            if excused:
                tested = [item for item, _ in merged]
                one = len(tested) == 1
                if automatic:  # a test failed and was let through: that's the user's call, not hers
                    raise EvolveError(
                        _join(
                            f"{_capital(_names(tested))} passed my tests on top of my newest code only after a second "
                            f"look at a failing test that isn't {'its' if one else 'theirs'}, so I didn't install "
                            f'{"it" if one else "them"} on my own. Say "install it" when you want '
                            f"{'it' if one else 'them'}.",
                            *left_out,
                        )
                    )
                left_out.append(
                    "One of my tests failed beside the others but not because of this, so I installed it anyway."
                )
        async with self._request_lock:  # a drop meanwhile (here or from a terminal) must win: nothing goes in
            changed = [item for item, _ in merged if not await self._still_ready(item.id)]
            if changed:
                raise EvolveError(
                    f"{_capital(_names(changed))} changed while I was testing (dropped, maybe), so I didn't "
                    "install anything. Ask me again if you still want the rest."
                )
            try:
                await asyncio.to_thread(self.git.fast_forward, merged[-1][1])
            except GitError as exc:
                log.warning("Couldn't move main forward to the tested merge: %s", exc)
                raise EvolveError(
                    "My code changed while I was getting the update ready, so nothing was installed. Ask me again."
                ) from exc
            # Recorded right away (a drop from a terminal checks the status as it writes: it can't win now).
            installed = []
            for item, merge in merged:
                installed.append(
                    await self._set(
                        item,
                        Status.INSTALLED,
                        merge_commit=merge,
                        branch=None,
                        error=None,
                        message=f"merged as {merge[:12]}",
                        notice=f"Installed {item.label()}",
                    )
                )
        return installed, clashed

    async def _merge_into(
        self, worktree: Path, items: list[Improvement], left_out: list[str], *, asked: bool = False
    ) -> tuple[list[tuple[Improvement, str]], list[Improvement]]:
        """Merge them one after another in ``worktree``: (merged with their merge commits, clashed: ones that
        conflict only with an update merged before them). EvolveError when none could be merged."""
        merged: list[tuple[Improvement, str]] = []
        clashed: list[Improvement] = []
        for item in items:
            try:
                merge = await asyncio.to_thread(
                    self.git.merge_no_ff, item.branch or "", _install_message(item), self.author, worktree
                )
            except MergeConflictError:
                if merged:  # maybe only with the others: it's out only if they go in
                    clashed.append(item)
                elif await self._still_ready(item.id):  # with the current code itself: it's redone (or rebuilt)
                    redoing = await self._conflicted(item, asked=asked)
                    left_out.append(
                        _conflict_text(item, alone=len(items) == 1, redoing=redoing, later=not self._runs_queued)
                    )
                else:
                    left_out.append(_dropped_text(item))
                continue
            except GitError as exc:
                log.warning("Merging %s failed: %s", item.label(), exc)
                dropped = not await self._still_ready(item.id)  # e.g. dropped from a terminal, branch and all
                left_out.append(
                    _dropped_text(item) if dropped else f"Git couldn't merge {spoken_name(item)}, so I left it out."
                )
                continue
            merged.append((item, merge))
        if not merged:
            raise EvolveError(_nothing_installed(left_out))
        return merged, clashed

    async def _test(
        self, worktree: Path, items: list[Improvement], previous: str
    ) -> tuple[list[CheckResult], list[str]]:
        """The checks on ``worktree`` (``previous``: the code before the merges); what failed is shown in the console
        (tests by name).

        Lint and formatting cover the files the merged updates change: files on main they don't touch (left
        unformatted by another change) failed two installs in a week (2026-10-25, 10-26). Failing tests get the
        gate's second look (``Gate.recheck``); what it lets through comes back as flags. One test run at a time,
        with the builds' final checks."""
        self._notice(f"Testing {_labels(items)} on top of my newest code before installing", logging.INFO)
        try:
            changed = await asyncio.to_thread(self.git.changed_files, worktree, previous)
        except GitError as exc:
            log.warning("Couldn't list what the merge changes: %s", exc)
            changed = None
        formatted = None if changed is None else [p for s, p in changed if s[:1] != "D" and p.endswith((".py", ".pyw"))]
        if self._gate_lock.locked():
            self._notice("Waiting for a build's final checks to finish before testing", logging.INFO)
        flags: list[str] = []
        async with self._gate_lock:
            checks = await self.gate.run_checks(worktree, worktree / "src", format_paths=formatted)
            for i, check in enumerate(checks):
                if check.name == "pytest" and not check.ok and check.started and changed is not None:
                    own = [p for _, p in changed]
                    checks[i], flag = await self.gate.recheck(worktree, worktree / "src", check, previous, own)
                    if flag:
                        flags.append(flag)
                        self._notice(f"Testing {_labels(items)}: {flag}", logging.WARNING)
        failed = [c for c in checks if not c.ok]
        if failed:
            where = _failing_places(checks)
            detail = "; ".join(f"{c.name}: {c.summary}" for c in failed)
            self._notice(
                f"Tested with my newest code, {_labels(items)} failed ({detail})"
                + (f" in {_listing(where, 6)}" if where else ""),
                logging.WARNING,
            )
        return checks, flags

    async def _leave_out(self, clashed: list[Improvement], *, asked: bool = False) -> list[str]:
        """The batch went in, and these conflict with it: they're redone on top of it (or need a new build), unless
        dropped meanwhile, which stands."""
        said = []
        async with self._request_lock:
            for item in clashed:
                if await self._still_ready(item.id):
                    redoing = await self._conflicted(item, asked=asked)
                    said.append(_conflict_text(item, alone=False, redoing=redoing, later=not self._runs_queued))
        return said

    async def _failed_together(
        self,
        tested: list[Improvement],
        failed: list[str],
        left_out: list[str],
        clashed: list[Improvement],
        batch: bool,
    ) -> str:
        """What to say when the checks failed. One build on its own that fails on top of the current code needs a
        new build; out of a batch, it may be only the combination (or a flaky test), so they all stay ready."""
        checks = _spoken_list([_SPOKEN_CHECKS.get(name, f"the {name} check") for name in failed])
        if batch:
            if len(tested) > 1:
                lead = f"Together they failed {checks}, so I didn't install any of them."
            else:
                lead = f"{_capital(spoken_name(tested[0]))} failed {checks} with my newest code, so I didn't install anything."
            still = [*tested, *clashed]
            if len(still) == 1:
                ready = "It's still ready, so you can try it again on its own."
            else:
                ready = f"{_capital(_names(still))} are still ready: install them one at a time and I'll test each."
            return _join(lead, ready, *left_out)
        item = tested[0]
        if not await self._still_ready(item.id):  # dropped meanwhile: that stands
            return _join(_dropped_text(item), *left_out)
        await self._set(
            item,
            Status.FAILED,
            error=f"failed the checks after merging with newer changes: {', '.join(failed)}",
            branch=None,
            message="checks failed after merging",
            notice=f"{item.label()} failed its checks on top of the newer code, so it wasn't installed",
            level=logging.WARNING,
        )
        await self._drop_branch(item.branch or "")
        return _join(
            "With my newest code its tests failed, so I didn't install it. Ask me to build it again.", *left_out
        )

    async def _conflicted(self, item: Improvement, *, asked: bool = False) -> bool:
        """A ready build that no longer merges with the newer code. The first time, it's queued again with its change
        (``_develop`` applies that on the newer code: the checks judge it as is when it goes on cleanly, else the
        builder only resolves the clashes), and installed once ready if the user asked for this install
        (``asked``); two installs failed on a conflict in a week (2026-10-25), each after a whole build. A second
        time, or when its change can't be kept, it failed: only a new build can do it. Whether it's being redone."""
        report = item.report or {}
        patch = None
        if not report.get("redone") and item.base_commit and item.branch and not self._closed:
            try:
                patch = await asyncio.to_thread(self.git.patch, item.base_commit, item.branch)
            except GitError as exc:
                log.warning("Couldn't keep the change of %s to redo it: %s", item.label(), exc)
        if patch:
            keep = {flag: True for flag in ("promised", "install", "silent") if report.get(flag)}
            if asked:  # they asked to install it and hear that it's redone: it goes in, and they hear how it ends
                keep |= {"install": True, "promised": True}
            summary = str((report.get("build") or {}).get("summary") or "")
            await self._set(
                item,
                Status.QUEUED,
                error=None,
                branch=None,
                report={
                    "request": str(report.get("request") or ""),
                    "auto": bool(report.get("auto")),
                    **keep,
                    "redo": {"patch": patch, "summary": summary},
                },
                message="merge conflict: redoing it on the newer code",
                notice=f"{item.label()} conflicts with newer changes; redoing it on top of them",
                level=logging.WARNING,
            )
            await self._drop_branch(item.branch or "")
            if self._runs_queued:  # else (``octavia evolve install``) it would be cut off: the next voice start does it
                self._enqueue(item.id)
            return True
        await self._set(
            item,
            Status.FAILED,
            error="conflicts with newer changes; build it again",
            branch=None,
            message="merge conflict",
            notice=f"{item.label()} conflicts with newer changes; not installed",
            level=logging.WARNING,
        )
        await self._drop_branch(item.branch or "")
        return False

    async def needs_testing(self, improvement_ids: Iterable[int] = (), *, all_ready: bool = False) -> bool:
        """Whether installing these runs the checks first (several at once, or my code changed since it was
        built): that takes a few minutes, worth mentioning before the user says yes."""
        if not self.gate.settings.run_checks:
            return False
        ids = await self.ready_ids() if all_ready else list(dict.fromkeys(improvement_ids))
        if len(ids) > 1:
            return True
        item = await self.journal.get(ids[0]) if ids else None
        if item is None:
            return False
        try:
            head = await asyncio.to_thread(self.git.head)
        except GitError:
            return False
        return item.base_commit != head

    async def install_blocker(self, improvement_ids: Iterable[int] = (), *, all_ready: bool = False) -> str | None:
        """Why none of these can be installed (None: at least one looks ready), for the tool to say before the
        user is asked to confirm. ``install`` checks everything again, authoritatively."""
        ids = await self.ready_ids() if all_ready else list(dict.fromkeys(improvement_ids))
        if not ids:
            return NOTHING_READY if all_ready else "Tell me which update to install."
        reasons = []
        for improvement_id in ids:
            item = await self.journal.get(improvement_id)
            if item is None:
                reasons.append(f"There's no improvement #{improvement_id}.")
                continue
            if item.id in self._installing:
                reasons.append(f"#{item.id} is being installed right now.")
                continue
            try:
                _check_installable(item)
            except EvolveError as exc:
                reasons.append(str(exc))
                continue
            return None
        return _nothing_installed(reasons)

    async def undo_blocker(self) -> str | None:
        """Why there's nothing to undo right now (None: there is), before the user is asked to confirm."""
        if self.changing_code:
            return INSTALLING_NOW
        item = await self.journal.last_installed()
        return None if item is not None and item.merge_commit else "There's no update to undo."

    # ------------------------------------------------------------------ the restart into what's pending

    async def _write_health(self, pending: _Pending) -> None:
        """Watch every update the next start runs for the first time: this process's, and those another one
        (``octavia evolve install``, ``octavia chat``) or an earlier start installed that aren't confirmed yet."""
        health: HealthMarker | None = read_marker(self.paths.health_marker, HealthMarker)
        undone = {i.merge_commit for i in pending.undone}
        kept: list[WatchedUpdate] = []
        if health is not None:
            for update in health.updates():
                if update.merge_commit in undone:
                    continue
                try:
                    present = await asyncio.to_thread(self.git.is_ancestor, update.merge_commit, "HEAD")
                except GitError:
                    present = False
                if present:
                    kept.append(update)
        listed = {u.merge_commit for u in kept}
        mine = [WatchedUpdate(i.id, i.title, i.merge_commit or "") for i in pending.installed]
        watched = kept + [u for u in mine if u.merge_commit not in listed]
        if not watched:
            if health is not None:
                clear_marker(self.paths.health_marker)
            return
        newest = watched[-1]
        write_marker(
            self.paths.health_marker,
            HealthMarker(
                improvement_id=newest.improvement_id,
                title=newest.title,
                merge_commit=newest.merge_commit,
                previous_head=health.previous_head if kept and health is not None else pending.previous_head,
                # The conversation to resume if the supervisor has to undo them (after the restart marker was read).
                brain_session_id=self._brain_session(),
                earlier=[_watched(u.improvement_id, u.title, u.merge_commit) for u in watched[:-1]],
            ),
        )

    def _restart_marker(self, pending: _Pending) -> RestartMarker:
        subject = (pending.installed or pending.undone)[-1]
        if not pending.installed:
            reason = "undo"
        else:
            reason = "install" if pending.asked_now else "auto_install"  # "auto_install": restart once it's idle
        return RestartMarker(
            reason=reason,
            announce=_back_text(pending),
            brain_session_id=self._brain_session(),
            improvement_id=subject.id,
        )

    async def _schedule_restart(self) -> bool:
        """Ask the app to restart into what's pending: right away, or (while builds run here) once they're all
        done, since the restart would cut them off. Asked again, the app just takes the newer marker."""
        self._restarting = True
        if self._restart_waiter is not None and not self._restart_waiter.done():
            return True  # it reads what's pending when it goes
        if self._taken:
            builds = "builds in progress are" if len(self._taken) > 1 else "build in progress is"
            self._notice(f"Restarting once the {builds} done", logging.INFO)
            waiter = self._restart_waiter = asyncio.get_running_loop().create_task(self._restart_after_build())
            self._tasks.add(waiter)
            waiter.add_done_callback(self._tasks.discard)
            return True
        return await self._call_restart()

    async def _restart_after_build(self) -> None:
        # Every running build finishes (and says how it went); what's still queued is passed over (it stays queued
        # for the next start), and nothing new starts.
        await self._queue.join()
        if self._closed or self._pending is None:
            return
        if not await self._call_restart():
            self.announce("I installed the update, but couldn't restart into it. Restart me to start using it.", None)

    async def _call_restart(self) -> bool:
        pending = self._pending
        if pending is None or self.hooks.restart is None:
            return False
        try:
            await self.hooks.restart(self._restart_marker(pending))
        except Exception:
            self._restarting = False
            log.warning("Couldn't restart into the update", exc_info=True)
            return False
        return True

    # ------------------------------------------------------------------ undoing

    async def undo_last(self, expected: int | None | object = _NOW) -> str:
        """Revert the most recently installed improvement; returns what to say. Raises EvolveError.

        ``expected``: the improvement the user was asked about (the tool fixes it before asking): an install or
        undo that finishes meanwhile mustn't make this undo another one. By default, the last one right now.
        """
        if self.changing_code:
            raise EvolveError(INSTALLING_NOW)
        if expected is _NOW:
            last = await self.journal.last_installed()
            expected = last.id if last is not None else None
        return await self._protected(self._undo_last(expected if isinstance(expected, int) else None))

    async def _undo_last(self, expected: int | None) -> str:
        async with self._repo_lock:
            item = await self.journal.last_installed()
            if item is None or not item.merge_commit:
                raise EvolveError("There's no update to undo.")
            if item.id != expected:
                raise EvolveError(
                    "Something changed since you asked (another update went in or out), so I didn't undo anything. "
                    "Ask me again if you still want to."
                )
            merge = item.merge_commit
            try:
                present = await asyncio.to_thread(self.git.is_ancestor, merge, "HEAD")
            except GitError:
                present = False
            if not present:
                await self._set(item, Status.ROLLED_BACK, error="it was no longer in the code", message="gone")
                raise EvolveError(f"\"{item.title}\" isn't in my code any more, so there's nothing to undo.")
            # The supervisor may have undone it already (it crashed on startup) with no start since to record
            # that: reverting it again would fail, and say it's still installed.
            rollback = await self.apply_pending_rollback()
            if rollback is not None and item.id in (rollback.improvement_ids or [rollback.improvement_id]):
                raise EvolveError(f'"{item.title}" crashed on startup and was already undone.')
            dirty = await asyncio.to_thread(self._dirty_files)
            if dirty:
                clash = _overlap(dirty, await asyncio.to_thread(self._merge_files, merge))
                if clash:
                    raise EvolveError(DIRTY.format(files=_listing(clash)))
            previous = await asyncio.to_thread(self.git.head)
            worktree = self.paths.worktrees / side_worktree_name("undo")
            message = f"Undo improvement #{item.id}: {item.title}"

            def revert(side: Path) -> str:
                try:
                    return self.git.revert_merge(merge, self.author, message, cwd=side)
                except GitError as exc:  # the revert itself: not the copy around it, nor moving main forward
                    raise _RevertFailed(str(exc)) from exc

            try:
                # Beside the main checkout, then a fast-forward: the user's uncommitted work stays out of it.
                await asyncio.to_thread(self.git.on_the_side, worktree, f"{BRANCH_PREFIX}/{worktree.name}", revert)
            except GitError as exc:
                log.warning("Couldn't undo %s beside the main checkout: %s", item.label(), exc)
                raise EvolveError(
                    "My code changed while I was undoing it (or git couldn't make a copy of it), so nothing was "
                    "undone. Ask me again."
                ) from exc
            except _RevertFailed as exc:
                log.warning("Reverting %s failed: %s", item.label(), exc)
                if await asyncio.to_thread(self._already_reverted, merge):
                    await self._set(
                        item,
                        Status.ROLLED_BACK,
                        error="already undone",
                        message="already undone",
                        notice=f"{item.label()} had already been undone",
                    )
                    self._forget_health(merge)
                    raise EvolveError(f'"{item.title}" was already undone, so there\'s nothing to undo.') from exc
                raise EvolveError(
                    "I couldn't undo it cleanly because later changes build on it. It's still installed."
                ) from exc
            item = await self._set(item, Status.ROLLED_BACK, message="undone", notice=f"Undid {item.label()}")
            pending = self._pending = self._pending or _Pending(previous_head=previous)
            pending.installed = [i for i in pending.installed if i.id != item.id]  # not run yet: needs no watching
            pending.undone.append(item)
            pending.asked_now = True
            self._forget_health(merge)
            await self._write_health(pending)
            if self._can_restart():
                building = await self._builds_now()
                # Still under the lock: an install or undo waiting for it joins the same restart.
                if await self._schedule_restart():
                    if building:
                        return f"Undone. I'll restart once I've finished building {_names(building)}."
                    return "Undoing it now. I'll be back in a few seconds."
        return f'Undone. Restart me to go back to the version without "{item.title}".'

    def _merge_files(self, merge: str) -> list[str]:
        out = self.git.run("diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", f"{merge}^1", merge)
        return [path for path in out.split("\0") if path]

    def _forget_health(self, merge: str) -> None:
        """An update that's gone needs no watching: a crash of the next start isn't its doing."""
        health: HealthMarker | None = read_marker(self.paths.health_marker, HealthMarker)
        if health is None:
            return
        updates = health.updates()
        remaining = [u for u in updates if u.merge_commit != merge]
        if len(remaining) == len(updates):
            return
        if not remaining:
            clear_marker(self.paths.health_marker)
            return
        newest = remaining[-1]
        health.improvement_id, health.title, health.merge_commit = (
            newest.improvement_id,
            newest.title,
            newest.merge_commit,
        )
        health.earlier = [_watched(u.improvement_id, u.title, u.merge_commit) for u in remaining[:-1]]
        write_marker(self.paths.health_marker, health)

    def _already_reverted(self, merge: str) -> bool:
        """Whether a commit since ``merge`` reverts it (the supervisor's undo of a crashed update, or one by hand),
        and that revert wasn't itself reverted since. Git's revert keeps "This reverts commit <sha>" in the body."""
        try:
            for revert in self._reverts_of(merge):
                if not self._reverts_of(revert):
                    return True
        except GitError as exc:
            log.debug("Couldn't look for a revert of %s: %s", merge[:12], exc)
        return False

    def _reverts_of(self, commit: str) -> list[str]:
        grep = f"--grep=This reverts commit {commit}"
        return self.git.run("log", "--format=%H", "--fixed-strings", grep, f"{commit}..HEAD").split()

    async def reject(self, improvement_id: int) -> str:
        """Drop an improvement (deleting its build); returns what to say. One being built is stopped first.

        One being built by another process (the voice assistant, or ``octavia evolve build`` in a terminal) can't
        be stopped from here: it's marked dropped, and that process throws the result away when it finishes.
        """
        said = f"Okay, dropped #{improvement_id}."
        async with self._request_lock:
            item = await self._require(improvement_id)
            if item.id in self._installing:  # it would go in anyway: the install is past asking
                raise EvolveError(
                    f"I'm installing #{item.id} right now, so I can't drop it halfway. Ask me again once that's done."
                )
            # Checked first: one dropped from a terminal (already "rejected" in the journal) is still built here
            # until this process stops it.
            building = item.id in self._active and item.status in (Status.BUILDING, Status.REJECTED)
            if building:
                self._dropped.add(item.id)  # the worker records it as rejected once the build has stopped
            elif item.status is Status.REJECTED:
                return said
            elif item.status is Status.INSTALLED:
                raise EvolveError("That one is already installed. To take it back, say undo the last update.")
            else:
                if item.status is Status.BUILDING and self._building_elsewhere(item.id):
                    said = (
                        f"Okay, dropped #{item.id}. Another copy of me is building it right now; "
                        "it throws the result away when it's done."
                    )
                try:
                    await self._set(
                        item,
                        Status.REJECTED,
                        expect=item.status,  # another process (an install from a terminal) may have got there first
                        branch=None,
                        report={**(item.report or {}), "dropped": True},  # the user's call: never built by itself
                        message="dropped",
                        notice=f"Dropped {item.label()}",
                    )
                except StatusChanged as exc:
                    if exc.current.status is Status.INSTALLED:
                        raise EvolveError(
                            "That one was installed just now. To take it back, say undo the last update."
                        ) from None
                    raise EvolveError(f"#{item.id} just changed; ask me again.") from None
                self._unwait(item.id)
        if building:
            # Only this one: builds running beside it go on.
            self._notice(f"Stopping the build of {item.label()}", logging.INFO)
            preparing = self._preparing.get(item.id)
            if preparing is not None and not preparing.done():
                preparing.cancel()  # still waiting for the repository: it stops there, and never starts
            checking = self._checking.get(item.id)
            if checking is not None and not checking.done():
                checking.cancel()  # the final checks take minutes (the build notices why they stopped)
            await self.builder.cancel(self._worktree(item.id))
        elif item.branch:
            self._spawn(self._drop_branch_later(item.branch))  # an install may hold the repository for minutes
        return said

    async def _drop_branch_later(self, branch: str) -> None:
        async with self._repo_lock:
            await self._drop_branch(branch)

    async def handle_restart_marker(self, marker: RestartMarker) -> None:
        """On startup after an intentional restart: record what the supervisor did while the assistant was down.

        Idempotent, and only for the install the marker is about: an item installed again since isn't touched.
        """
        if marker.reason != "crash_rollback":
            return
        ids = marker.improvement_ids or ([marker.improvement_id] if marker.improvement_id is not None else [])
        for improvement_id in ids:
            item = await self.journal.get(improvement_id)
            if item is None or item.status is not Status.INSTALLED or item.updated_at > marker.created_at:
                continue
            await self._set(
                item,
                Status.ROLLED_BACK,
                error=CRASHED,
                message=f"{CRASHED}; undone",
                notice=f"{item.label()} {CRASHED} and was undone",
                level=logging.WARNING,
            )

    async def apply_pending_rollback(self) -> RestartMarker | None:
        """Record an undo the supervisor did (an update crashed on startup) that no start has read yet: the
        terminal commands run without one. The marker stays, so the next start still says what happened."""
        marker = read_marker(self.paths.restart_marker, RestartMarker)
        if marker is None or marker.reason != "crash_rollback":
            return None
        await self.handle_restart_marker(marker)
        return marker

    def mark_healthy(self, merge_commit: str | None = None) -> None:
        """The freshly installed update has run fine for a while: the supervisor stops watching it.

        With ``merge_commit`` (the update this process started with), only a marker for that update is
        cleared: one written since, by a later install, still has its own first start ahead of it.
        """
        if merge_commit is not None:
            health = read_marker(self.paths.health_marker, HealthMarker)
            if health is None or health.merge_commit != merge_commit:
                return
        clear_marker(self.paths.health_marker)

    # ------------------------------------------------------------------ reading

    async def get(self, improvement_id: int) -> Improvement | None:
        return await self.journal.get(improvement_id)

    async def last_installed(self) -> Improvement | None:
        return await self.journal.last_installed()

    async def summary_for_prompt(self) -> str:
        """One line for the system prompt: what's open, and the ids of builds ready to install."""
        items = await self.journal.list(OPEN_STATUSES, limit=500)
        if not items:
            return ""
        counts = Counter(i.status for i in items)
        parts = []
        if counts[Status.NOTED]:
            parts.append(f"{counts[Status.NOTED]} noted")
        # A "building" row nobody runs is left over from a crash (the prompt is written before recovery fails
        # it): it isn't being built, and the brain mustn't promise it is.
        running = sum(
            1
            for i in items
            if i.status is Status.BUILDING and (self._building_here(i.id) or self._building_elsewhere(i.id))
        )
        building = counts[Status.QUEUED] + running
        if building:
            here = [i for i in self._active if any(item.id == i for item in items)]
            parts.append(f"{building} being built" + (f" ({_ids(here)} side by side)" if len(here) > 1 else ""))
        installing = [i for i in items if i.status is Status.READY and i.id in self._installing]
        if installing:
            parts.append(f"being tested and installed right now: {', '.join(f'#{i.id}' for i in installing)}")
        ready = [i for i in items if i.status is Status.READY and i.id not in self._installing]
        if ready:
            listed = ", ".join(i.label() for i in ready[:_LISTED_READY])
            parts.append(f"ready to install: {listed}" + (" and more" if len(ready) > _LISTED_READY else ""))
            heard = sorted((i for i in ready if (i.report or {}).get("announced")), key=lambda i: i.id)
            if heard:  # said once already: bringing it up again by itself is a repeat the user didn't ask for
                told = ", ".join(f"#{i.id}" for i in heard[:_LISTED_READY]) + (
                    " and more" if len(heard) > _LISTED_READY else ""
                )
                parts.append(
                    f"the user was already told {told} {'is' if len(heard) == 1 else 'are'} ready "
                    "(don't bring it up again unless they ask)"
                )
        return f"Self-improvement status: {'; '.join(parts)}." if parts else ""

    async def describe(self, statuses: Iterable[Status] | None = None, limit: int = 10) -> str:
        """A compact list for the brain: one improvement per line, newest activity first. A build in progress
        says how it's going (for "how's the update going?"), a queued one when it starts. Several builds running
        side by side are named together first ("Building #12 and #14 side by side")."""
        items = await self.journal.list(statuses, limit=limit)
        if not items:
            return "Nothing on the list."
        typical = None
        if any(i.status is Status.BUILDING for i in items):
            timed = await self.journal.build_seconds(Status.READY.value, _TIMED_BUILDS)
            typical = statistics.median(timed) if timed else None
        lines = []
        here = list(self._active)
        if len(here) > 1:
            queued = " Queued ones start as they finish." if any(i.status is Status.QUEUED for i in items) else ""
            lines.append(f"Building {_ids(here)} side by side (up to {self.parallel_builds} at once).{queued}")
        for item in items:
            text = _describe_item(item)
            if item.status is Status.BUILDING:
                text += f" - {self._build_status(item, typical)}"
            elif item.status is Status.QUEUED:
                text += f" - {self._queue_status(item)}"
            elif item.status is Status.READY and item.id in self._installing:
                then = "then I restart into it" if self._can_restart() else "then restart me to use it"
                text += f" - Being tested and installed right now ({then})."
            lines.append(text)
        return "\n".join(lines)

    def _build_status(self, item: Improvement, typical_s: float | None) -> str:
        progress = self._active.get(item.id)
        if progress is not None:
            return progress.status(typical_s)
        if item.id in self._taken:  # taken up a moment ago
            return f"Now: {STAGE_PREPARING}."
        if self._building_elsewhere(item.id):
            # The voice assistant, or `octavia evolve build` in a terminal: only its log tells how it's going.
            latest = _latest_log_line(self.paths.builds / f"{item.id}.log")
            return "Another copy of me is building it" + (f"; its latest step, at {latest}" if latest else "") + "."
        return "Not actually running: it was cut off, and my next start marks it failed."

    def _queue_status(self, item: Improvement) -> str:
        if self._restarting:
            return "Starts once I've restarted."
        if item.id not in self._waiting:
            return "Waiting to be built."
        ahead = self.builds_ahead(item.id)
        if ahead == 0:
            return "Starting now."
        if self.parallel_builds == 1:
            return f"Waiting for the {ahead} build{'' if ahead == 1 else 's'} ahead of it."
        return f"{_capital(starts_when(ahead, self.parallel_builds))}."

    # ------------------------------------------------------------------ helpers

    async def _require(self, improvement_id: int) -> Improvement:
        item = await self.journal.get(improvement_id)
        if item is None:
            raise EvolveError(f"There's no improvement #{improvement_id}.")
        return item

    def _dirty_files(self) -> list[str]:
        return [p for p in self.git.dirty_files() if not p.replace("\\", "/").lower().startswith(_OWN_DATA)]

    async def _drop_branch(self, branch: str) -> None:
        try:
            await asyncio.to_thread(self.git.delete_branch, branch)
        except GitError as exc:
            log.warning("Couldn't delete branch %s: %s", branch, exc)

    async def _set(
        self,
        item: Improvement,
        status: Status,
        *,
        message: str = "",
        notice: str | None = None,
        level: int = logging.INFO,
        **fields: Any,
    ) -> Improvement:
        updated = await self.journal.update(item.id, status=status, **fields)
        self._changed(updated, message)
        if notice:
            self._notice(notice, level)
        return updated

    def _changed(self, item: Improvement, message: str = "") -> None:
        self.events.emit(
            ImprovementChanged(improvement_id=item.id, title=item.title, status=item.status.value, message=message)
        )

    def _notice(self, message: str, level: int = logging.INFO) -> None:
        self.events.emit(Notice(source=NOTICE_SOURCE, message=message, level=level))

    def announce(
        self, text: str, note: str | None = None, listen: bool = True, offer: Offer | None = None, news: bool = False
    ) -> None:
        """Say it (``announce: speak``, when the app is listening), otherwise just show it.

        ``note`` goes to the brain's next turn (also when it's only shown); ``listen``: open a follow-up window
        for a reply afterwards; ``offer``: the one call it offers to make, which a plain yes then approves.
        ``news``: nobody is waiting on it (a build that finished, the night's report), so it waits while the user is in
        the middle of something with her (``hooks.busy``): on 2026-11-04 "Good news: I finished ..." came in the middle
        of the user's assignment work.
        """
        if news and self._busy():
            self._held_news.append((text, note, listen, offer))
            log.info("Holding self-improvement news while the user is busy: %r", text[:80])
            self._tell_held_later()
            return
        listen = listen and not self._restarting
        if self._settings.announce == "speak" and self.hooks.announce is not None and not self._quiet():
            try:
                if offer is not None:
                    self.hooks.announce(text, note, listen, offer)
                else:
                    self.hooks.announce(text, note, listen)
                return
            except Exception:
                log.warning("Couldn't announce a self-improvement result", exc_info=True)
        self._notice(text, logging.INFO)
        if note and self.hooks.note is not None:
            try:
                self.hooks.note(note)
            except Exception:
                log.warning("Couldn't pass a self-improvement note to the brain", exc_info=True)

    def _can_restart(self) -> bool:
        return self.supervised and self.hooks.restart is not None

    def _brain_session(self) -> str | None:
        if self.hooks.brain_session is None:
            return None
        try:
            return self.hooks.brain_session()
        except Exception:
            return None

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _protected(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Run an install or undo to the end even if the caller stops waiting (a voice turn can be cut off):
        stopping halfway would leave the code merged but not recorded, or recorded but not restarted into."""
        return await asyncio.shield(self._critical_task(coro))

    def _critical_task(self, coro: Coroutine[Any, Any, _T]) -> asyncio.Task[_T]:
        task = asyncio.get_running_loop().create_task(coro)
        self._critical.add(task)
        task.add_done_callback(self._settled)
        return task

    def _settled(self, task: asyncio.Task[Any]) -> None:
        self._critical.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved here, so a caller that stopped waiting doesn't leave it unreported

    # ------------------------------------------------------------------ recovery

    async def _recover(self, *, resume_queued: bool = True) -> None:
        """Builds still "building" that no live process runs were cut off by a crash (an intentional restart
        queues them again instead): fail them, clean up, and tell the user. The voice start (``resume_queued``)
        also tells of requested builds a shutdown stopped, then queues the waiting ones."""
        busy: set[int] = set()
        for item in await self.journal.list([Status.BUILDING], limit=10_000):
            if item.id in self._active or self._building_elsewhere(item.id):
                busy.add(item.id)  # e.g. `octavia evolve build` running in a terminal
                continue
            item = await self._set(
                item,
                Status.FAILED,
                error=INTERRUPTED,
                message=INTERRUPTED,
                notice=f"The build of {item.label()} was {INTERRUPTED}; ask me to build it again",
            )
            await self._clean_up(self._worktree(item.id), branch_name(item))
            self._release(item.id)
            report = item.report or {}
            if (not report.get("auto") or _promised(report)) and not report.get("silent"):  # expected news of it
                self._announce_cut_off(item)
        for folder in _subdirectories(self.paths.worktrees):
            if folder.name.isdigit():
                if int(folder.name) not in busy:
                    await self._clean_up(folder, None)
            elif not side_worktree_live(folder.name):  # an install, undo or rollback whose process is gone
                await self._clean_up(folder, f"{BRANCH_PREFIX}/{folder.name}")
        await self._drop_leftover_side_branches()
        if not resume_queued:
            return  # `octavia chat` leaves what's owed to the voice assistant, which may be running
        # A requested build that a shutdown (not a restart) stopped was failed on the way out, and nobody told.
        for item in await self.journal.list([Status.FAILED], limit=10_000):
            report = item.report or {}
            if report.get("stopped"):
                await self._set(item, Status.FAILED, report={**report, "stopped": False})  # said once
                self._announce_cut_off(item)
        queued = await self.journal.list([Status.QUEUED], limit=10_000)
        for item in sorted(queued, key=lambda i: (i.updated_at, i.id)):
            self._enqueue(item.id)

    async def _drop_leftover_side_branches(self) -> None:
        """A side worktree's branch whose folder went but whose deletion didn't (its process was killed): tidying
        up, so nothing here may stop the rest of the recovery."""
        try:
            refs = await asyncio.to_thread(
                self.git.run, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}/"
            )
            for ref in refs.split():
                name = ref.split("/", 1)[-1]
                gone = not (self.paths.worktrees / name).exists()
                if is_side_worktree_name(name) and gone and not side_worktree_live(name):
                    await self._drop_branch(ref)
        except Exception:
            log.debug("Couldn't look for leftover side branches", exc_info=True)

    def _announce_cut_off(self, item: Improvement) -> None:
        self.announce(
            f"My build of {spoken_name(item)} was cut off when I stopped. Want me to start it again?",
            f"You just told the user the build of improvement #{item.id} was interrupted. If they want "
            f"it, call build_improvement with improvement_id {item.id}.",
            offer=Offer("build_improvement", {"improvement_id": item.id}),
        )

    def _worktree(self, improvement_id: int) -> Path:
        """Where its build works (and what tells it apart from the builds beside it: ``builder.cancel``)."""
        return self.paths.worktrees / str(improvement_id)

    def _owner_file(self, improvement_id: int) -> Path:
        return self.paths.builds / f"{improvement_id}.owner"

    def _claim(self, improvement_id: int) -> None:
        """Record which process runs this build, so another one starting up doesn't take it for abandoned."""
        try:
            me = psutil.Process()
            path = self._owner_file(improvement_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"pid": me.pid, "created": me.create_time()}), encoding="utf-8")
        except (OSError, psutil.Error) as exc:
            log.debug("Couldn't record the owner of build #%s: %s", improvement_id, exc)

    def _release(self, improvement_id: int) -> None:
        with contextlib.suppress(OSError):
            self._owner_file(improvement_id).unlink(missing_ok=True)

    def _building_elsewhere(self, improvement_id: int) -> bool:
        try:
            owner = json.loads(self._owner_file(improvement_id).read_text(encoding="utf-8"))
            pid = int(owner["pid"])
            if pid == os.getpid():
                return False
            # The creation time tells a live owner from an unrelated process that reuses its pid.
            return abs(psutil.Process(pid).create_time() - float(owner["created"])) < 1.0
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            return False


# ------------------------------------------------------------------ helpers


def branch_name(item: Improvement) -> str:
    """``octavia/<id>-<slug of the title>``: readable in ``git branch`` and unique per improvement."""
    slug = re.sub(r"[^a-z0-9]+", "-", item.title.lower()).strip("-")[:_SLUG_CHARS].strip("-")
    return f"{BRANCH_PREFIX}/{item.id}-{slug or 'change'}"


def _title_from(request: str) -> str:
    text = " ".join(request.split())
    if len(text) <= MAX_REQUEST_TITLE:
        return text
    cut = text[:MAX_REQUEST_TITLE].rsplit(" ", 1)[0]
    if len(cut) < MAX_REQUEST_TITLE // 2:
        cut = text[:MAX_REQUEST_TITLE]
    return cut.rstrip(" ,.;:") + "..."


def _auto_buildable(item: Improvement) -> bool:
    """Whether the assistant may build this by itself.

    Not when an earlier build of it ended without an install (nothing to change, failed, refused): a reopened
    error would otherwise be rebuilt, with the same result, every time it recurs. Nor when the user dropped it.
    The user can still ask. (An installed fix whose error comes back isn't reopened: that's a new item.)
    """
    if item.status is not Status.NOTED:
        return False
    report = item.report or {}
    return not report.get("dropped") and not report.get("build")


def _budget_job(item: Improvement) -> str:
    """How her usage budget names a build she starts by herself (in the skip log and the morning report)."""
    return f"building {spoken_name(item)}"


def _check_buildable(item: Improvement) -> None:
    if item.status is Status.BUILDING:
        raise EvolveError(f"I'm already building #{item.id}.")
    if item.status is Status.QUEUED:
        raise EvolveError(f"#{item.id} is already waiting to be built.")
    if item.status is Status.READY:
        raise EvolveError(f"#{item.id} is already built; say install it when you want it.")
    if item.status is Status.INSTALLED:
        raise EvolveError(f"#{item.id} is already installed.")


def _taken_text(item: Improvement) -> str:
    """Why a new request isn't added to an open item of the same name that's already being built or built."""
    if item.status is Status.READY:
        return (
            f"{item.label()} is already built and waiting to be installed, so I didn't add this request to it. "
            "Install or drop it first, then ask me again."
        )
    return f"I'm already building {item.label()}, so I didn't add this request to it. Ask me again once it's ready."


def _check_installable(item: Improvement) -> None:
    if item.status is Status.INSTALLED:
        raise EvolveError(f"#{item.id} is already installed.")
    if item.status in (Status.QUEUED, Status.BUILDING):
        raise EvolveError(f"#{item.id} isn't built yet; I'll tell you when it's ready.")
    if item.status is not Status.READY:
        raise EvolveError(f"#{item.id} isn't ready to install (it's {item.status.value.replace('_', ' ')}).")
    gate = (item.report or {}).get("gate")
    if not isinstance(gate, dict) or not gate.get("passed"):
        raise EvolveError("That build didn't pass its checks, so I won't install it.")


def _feedback(report: GateReport) -> str:
    """What the builder needs to fix in its second round: the reasons and the end of each failed check's output."""
    parts = [f"- {r}" for r in report.reasons]
    for check in report.checks:
        if not check.ok:
            parts.append(f"\n{check.name}: {check.summary}\n{check.output_tail}".rstrip())
    return "\n".join(parts)


def _reasons(report: GateReport) -> str:
    return "; ".join(report.reasons) or "the final checks failed"


def _spoken_gate_failure(report: GateReport, improvement_id: int) -> str:
    """Why the gate failed a build, to say out loud: its reasons hold git output, paths and parser messages."""
    reasons = report.reasons
    if any(r.startswith("couldn't read the change from git") for r in reasons):
        return f"my checks couldn't read the change; the details are in octavia evolve show {improvement_id}"
    if any(r.startswith("couldn't parse ") for r in reasons):
        return "it wrote code that doesn't parse"
    if any(r.startswith("files in the worktree aren't committed") for r in reasons):
        return "it left some files uncommitted"
    if NO_CHANGES in reasons:
        return "it didn't end up changing anything"
    if any(r.startswith(UNSTARTED) for r in reasons):
        return (
            "my checks couldn't start (Windows wouldn't run them), so it was never tested; the details are in "
            f"octavia evolve show {improvement_id}"
        )
    failed = [_SPOKEN_CHECKS.get(c.name, f"the {c.name} check") for c in report.checks if not c.ok]
    if failed:
        return "it failed " + (failed[0] if len(failed) == 1 else f"{', '.join(failed[:-1])} and {failed[-1]}")
    return "it didn't pass my final checks"


def _passed_tests(report: GateReport) -> int | None:
    for check in report.checks:
        if check.name == "pytest" and check.ok and not report.flags_excused:  # not "all" when a failure was let through
            match = re.search(r"(\d+) passed", check.summary)
            if match:
                return int(match.group(1))
    return None


def _describe_report(report: GateReport | None) -> str:
    """E.g. "3 files changed, +40/-2 lines; 322 passed in 25.1s"."""
    if report is None:
        return ""
    files = len({c.path for c in report.changes})
    text = f"{files} file{'' if files == 1 else 's'} changed, +{report.lines_added}/-{report.lines_removed} lines"
    tests = next((c.summary for c in report.checks if c.name == "pytest"), "")
    return f"{text}; {tests}" if tests else text


def _outcome_notice(item: Improvement, verdict: _Verdict) -> str:
    if verdict.status is Status.READY:
        return f"{item.label()} is built and ready to install ({_describe_report(verdict.report)})"
    if verdict.status is Status.QUEUED:
        return f"The build of {item.label()} stopped for the restart; it starts again afterwards"
    return f"Build of {item.label()}: {verdict.status.value.replace('_', ' ')}" + (
        f" ({verdict.error})" if verdict.error else ""
    )


def spoken_name(item: Improvement) -> str:
    """How to call the improvement out loud: automatically noticed errors have technical titles, and a long
    title (a request cut short) is a mouthful."""
    if item.kind is Kind.ERROR:
        return "a fix for an error in my code"
    if len(item.title) > _SPOKEN_TITLE:
        return "an improvement I came up with" if (item.report or {}).get("auto") else "the change you asked for"
    return f'"{item.title}"'


def spoken_label(item: Improvement) -> str:
    """How a confirmation question names the improvement: its title, or (when that isn't said out loud: an error,
    a long title) its number and what it is, so the user can still tell which one is meant."""
    name = spoken_name(item)
    return name if name.startswith('"') else f"#{item.id}, {name}"


def _spoken_refusal(error: str) -> str:
    """Why the gate threw a build away, in words: what it did (the gate's refusal reasons, joined)."""
    causes = []
    if PROTECTED in error:
        causes.append("changed files it isn't allowed to touch")
    if LINKS in error:
        causes.append("added a link or submodule, which isn't allowed")
    if RESERVED in error:
        causes.append("used a tool name that's reserved for my self-improvement tools")
    if TWIN in error:
        causes.append("added a second copy of a file that already exists, which isn't allowed")
    return " and ".join(causes) or "broke one of my rules"


def _ready_text(
    item: Improvement,
    summary: str,
    report: GateReport,
    ready: int = 1,
    *,
    held: bool = False,
    failed: str | None = None,
    building: Sequence[Improvement] = (),
) -> str:
    """The "it's ready" announcement, said once per build. ``ready``: how many builds are ready now, this one
    included. The user asked to have it installed once ready, but ``held``: something in it deserves a look first,
    or ``failed``: installing it didn't work (why). ``building``: the builds still to come, which it mentions."""
    tests = _passed_tests(report)
    checks = f"It passed all {tests} tests." if tests else "It passed the checks."
    if UNPROVEN in report.flags:  # said whatever else is said: "fixed" must not be heard as proven
        checks += " But its new tests pass without the change too, so nothing proves it works yet."
    if failed:
        look = _join("I couldn't install it on my own.", failed)
    elif held:
        added = [m.group(1) for f in report.flags if (m := _NEW_SAFE_TOOL.fullmatch(f))]
        lowered = [m.group(1) for f in report.flags if (m := _LOWERED_TOOL.fullmatch(f))]
        if added:
            what = f"a tool, {added[0]}," if len(added) == 1 else f"tools ({', '.join(added)})"
            look = f"It adds {what} that would run without asking you first, so I didn't install it on my own."
        elif lowered:
            look = f"It lets {_spoken_list(lowered)} run without asking you first, so I didn't install it on my own."
        else:
            look = "Some of it deserves a look first, so I didn't install it on my own."
    else:
        look = "Some of it deserves a look before you install it." if report.flags else ""
    ask = 'Say "install it" when you want it.'
    if ready > 1:
        ask = f'Say "install it", or "install them all" for all {ready} ready updates, with one restart.'
    return _join(f"Good news: I finished {spoken_name(item)}.", summary, checks, look, _still_text(building), ask)


def _still_text(building: Sequence[Improvement]) -> str:
    """Whether a build is still going, for the "it's ready" announcement (nothing when none is)."""
    if not building:
        return ""
    if len(building) == 1:
        return f"I'm still building {spoken_name(building[0])}."
    return f"I'm still working on {len(building)} more builds."


def _ready_note(
    item: Improvement,
    report: GateReport,
    ready: int = 1,
    failed: str | None = None,
    building: Sequence[Improvement] = (),
) -> str:
    note = (
        f'You just told the user improvement #{item.id} "{item.title}" is built and ready. '
        f"If they want it, call install_improvement with improvement_ids [{item.id}]"
        + (f", or with all_ready for all {ready} ready ones." if ready > 1 else ".")
        + " Don't tell them again that it's ready unless they ask."
    )
    if building:
        note += f" Still being built: {', '.join(i.label() for i in building)}."
    if failed:
        note += f" Installing it on its own didn't work: {failed}"
    if report.flags:
        note += f" Worth mentioning if they ask: {'; '.join(report.flags)}."
    return note


def _conflict_text(item: Improvement, *, alone: bool, redoing: bool = False, later: bool = False) -> str:
    """``redoing``: it's queued to be redone on the newer code, ``later``: by the voice assistant's next start."""
    if redoing:
        when = "I'll redo it on top of them the next time I start" if later else "I'm redoing it on top of them"
        if alone:
            return (
                f"It conflicts with newer changes to my code, so I couldn't install it yet. {when}, and I'll tell you "
                "how that goes."
            )
        return f"{_capital(spoken_name(item))} conflicts with newer changes to my code, so {when}."
    if alone:
        return "It conflicts with newer changes to my code, so I couldn't install it. Ask me to build it again."
    return (
        f"{_capital(spoken_name(item))} conflicts with newer changes to my code, so I left it out. "
        "Ask me to build it again."
    )


def _back_text(pending: _Pending) -> str:
    """What the assistant says once it's back: everything the restart brought in (and took out)."""
    installed, undone = pending.installed, pending.undone
    if not installed:
        return f"Okay, I undid {_names(undone)}."
    also = f"I also undid {_names(undone)}." if undone else ""
    if len(installed) > 1:
        on_own = "" if any(i.id in pending.asked_for for i in installed) else " on my own"
        return _join(f"I'm back! I installed {len(installed)} updates{on_own}: {_names(installed)}.", also)
    item = installed[0]
    summary = str(((item.report or {}).get("build") or {}).get("summary") or "").strip()
    if item.id not in pending.asked_for:
        return _join("I'm back! I installed an update on my own.", summary or f"It's {spoken_name(item)}.", also)
    if summary:
        return _join("I'm back! The update is installed.", summary, also)
    return _join(f"I'm back! {_capital(spoken_name(item))} is installed.", also)


def _names(items: list[Improvement]) -> str:
    return _spoken_list([spoken_name(i) for i in items])


def _ids(improvement_ids: list[int]) -> str:
    """'#12', '#12 and #14'."""
    return _spoken_list([f"#{i}" for i in improvement_ids])


def starts_when(ahead: int, parallel: int) -> str:
    """When a queued build starts (``builds_ahead`` of it must finish first, with ``parallel`` build slots), to
    follow "it": "starts after the build ahead of it", "starts when one of the builds running now finishes"."""
    if parallel <= 1:
        return "starts after the build ahead of it" if ahead == 1 else f"starts after the {ahead} builds ahead of it"
    if ahead == 1:
        return "starts when one of the builds running now finishes"
    return f"starts once {ahead} of the builds ahead of it have finished"


def _spoken_list(parts: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _installed_text(items: list[Improvement], *, restarting: bool, waiting: str = "") -> str:
    """What installing says right away (``waiting``: " once I've finished building ..." when a build runs)."""
    one = len(items) == 1
    if not restarting:
        return RESTART_YOURSELF if one else f"Installed {len(items)} updates. Restart me to start using them."
    if waiting:
        return (
            f"Installed{'' if one else f' {len(items)} updates'}. I'll restart into {'it' if one else 'them'}{waiting}."
        )
    return (
        "Installing it now. I'll be back in a few seconds."
        if one
        else (f"Installing {len(items)} updates now. I'll be back in a few seconds.")
    )


def _testing_text(count: int, *, restarts: bool = True) -> str:
    what = "it" if count == 1 else "them"
    then = f"Then I'll restart into {what}" if restarts else f"Then you can restart me to use {what}"
    return f"Testing {what} with my newest code first; that takes a few minutes. {then}, or tell you if something doesn't pass."


def _labels(items: list[Improvement]) -> str:
    return ", ".join(item.label() for item in items)


def _norm_path(path: str) -> str:
    return path.strip().replace("\\", "/").lower()


# Where a check failed: pytest's "FAILED tests/x.py::test" / "ERROR tests/x.py", ruff's "--> src\\x.py:3:1" and
# "Would reformat: src/x.py".
_FAILING_PLACE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:::\S*)?(?: - .*)?$|-->\s*(\S+?):\d+|^Would reformat: (\S+)$")


def _failing_places(checks: list[CheckResult]) -> list[str]:
    """The files the failed checks point at (from the tail of their output), in order."""
    places: list[str] = []
    for check in checks:
        if check.ok:
            continue
        for line in check.output_tail.splitlines():
            match = _FAILING_PLACE.search(line.strip())
            if match:
                path = _norm_path(next(g for g in match.groups() if g))
                if path not in places:
                    places.append(path)
    return places


def _culprits(
    tested: list[Improvement], checks: list[CheckResult], touched: dict[int, set[str]]
) -> list[Improvement] | None:
    """The updates whose own files every failure points at, when that's only some of them (None otherwise):
    leaving those out, the rest may well pass."""
    places = _failing_places(checks)
    if not places or len(tested) < 2:
        return None
    culprits: list[Improvement] = []
    for place in places:
        owners = [item for item in tested if place in touched.get(item.id, set())]
        if not owners:
            return None  # a failure in a file none of them changed: it's how they combine, or the code itself
        culprits += [item for item in owners if item not in culprits]
    return culprits if len(culprits) < len(tested) else None


def _culprit_text(culprits: list[Improvement]) -> str:
    one = len(culprits) == 1
    return (
        f"{_capital(_names(culprits))} failed {'its' if one else 'their'} own tests on top of the others, so I left "
        f"{'it' if one else 'them'} out. {'It is' if one else 'They are'} still ready: try "
        f"{'it' if one else 'them'} on {'its' if one else 'their'} own."
    )


def _dropped_text(item: Improvement) -> str:
    return f"{_capital(spoken_name(item))} was dropped meanwhile, so I left it out."


def _nothing_installed(reasons: list[str]) -> str:
    if not reasons:
        return NOTHING_READY
    return reasons[0] if len(reasons) == 1 else _join("I couldn't install any of them.", *reasons)


def _install_message(item: Improvement) -> str:
    return f"Install improvement #{item.id}: {item.title}"


def _watched(improvement_id: int, title: str, merge_commit: str) -> dict[str, Any]:
    return {"improvement_id": improvement_id, "title": title, "merge_commit": merge_commit}


def _overlap(dirty: list[str], files: list[str]) -> list[str]:
    """The uncommitted files that ``files`` (what a merge or undo changes) touches too."""
    changed = {f.replace("\\", "/").lower() for f in files}
    return sorted(d for d in dirty if d.replace("\\", "/").lower() in changed)


def _preapproved(report: GateReport) -> bool:
    """Whether a build the user asked to have installed once ready may go in by itself: nothing in it beyond
    what they asked for deserves a look (see ``_PREAPPROVED_FLAGS``), and no core file changed."""
    if any(c.klass == "sensitive" for c in report.changes):
        return False
    return all(any(pattern.fullmatch(flag) for pattern in _PREAPPROVED_FLAGS) for flag in report.flags)


def _describe_item(item: Improvement) -> str:
    text = f"#{item.id} [{item.status.value}] {item.kind.value}: {item.title}"
    if item.occurrences > 1:
        text += f" (seen {item.occurrences} times)"
    if item.status is Status.READY:
        summary = str(((item.report or {}).get("build") or {}).get("summary") or "").strip()
        if summary:
            text += f" - {_short(summary, 200)}"
    elif item.error and item.status is not Status.INSTALLED:
        text += f" - {_short(item.error, 160)}"
    return text


def _promised(report: dict[str, Any]) -> bool:
    """Whether the user was told a build the assistant started by itself is under way (``note``)."""
    return bool(report.get("promised"))


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d (\d\d:\d\d):\d\d (.+)$")
_LOG_TAIL_BYTES = 8192


def _latest_log_line(log_file: Path) -> str:
    """The latest step of the current build in a build's log, e.g. "14:05: Editing src/x.py" ("" when it has
    none yet). The log keeps every build of the improvement: an earlier one's steps aren't this one's."""
    try:
        with log_file.open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - _LOG_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        match = _LOG_LINE.match(line)
        if not match:
            continue
        step = match.group(2)
        if step.startswith("=== Building"):
            return ""
        if not step.startswith(("===", "Blocked ")):
            return f"{match.group(1)}: {_short(step, 160)}"
    return ""


def _spoken_reason(error: str) -> str:
    return _short(error, _SPOKEN_REASON).rstrip(" .") or "something went wrong"


def _lower_first(text: str) -> str:
    """A sentence continued after "but": 'The service is down' -> 'the service is down'; 'API keys' stays."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _listing(paths: list[str], shown: int = 3) -> str:
    return ", ".join(paths[:shown]) + (f" and {len(paths) - shown} more" if len(paths) > shown else "")


def _subdirectories(path: Path) -> list[Path]:
    try:
        return [p for p in path.iterdir() if p.is_dir() or os.path.isjunction(p)]
    except OSError:
        return []


def _came_back(earlier: Improvement, details: str) -> str:
    """``details`` for a note of a problem that update ``earlier`` was meant to fix, with what that update did."""
    summary = str(((earlier.report or {}).get("build") or {}).get("summary") or "").strip()
    tried = f" It changed: {summary[:600]}" if summary else ""
    head = (
        f"This looks like it came back after #{earlier.id} ({earlier.title}) was installed, so that fix may not have "
        f"worked.{tried} Find out whether and why it didn't, reproduce the problem in a test that fails without the "
        "fix, and don't redo the same change."
    )
    return f"{head}\n\n{details}" if details else head
