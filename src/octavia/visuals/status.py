"""Her status panel: one small panel (id ``status``) for the work running beside her, updated in place. Downloads and
long commands (long jobs), background tasks, web tasks and deep thinks, self-improvement builds and installs, and
objectives, each with where it stands, its progress, and how it ended (done, or failed and why).

Where the facts come from is each tracker's own record, where it already is; the board reads no files and asks
Windows nothing itself:

- long jobs: the checks the job watcher makes every few seconds anyway, passed on through ``JobManager.listeners``;
- tasks, web tasks, deep thinks and objectives: their managers' lists in memory;
- self-improvement: its ``ImprovementChanged`` events (one journal read at start), and a running build's stage from
  the manager's memory.

A tick every few seconds gathers those, and the panel is sent only what changed (and never more often than
``MIN_SEND_S``). A panel the board puts up has no timer: the board closes it.

When it shows (``visuals.status_panel``):

- ``auto``: by itself once something has been running for ``GRACE_S`` (so a quick job doesn't flash it), never while
  a screen task is working (a panel in the way stops her clicks), and never by pushing out another panel (it waits
  for room). Each finished item stays a little while (``DONE_LINGER_S``, a failure ``FAILED_LINGER_S``), then the panel
  closes once nothing is left. Closing it (the user, or her with close_visual) dismisses what was on it: those items
  don't bring it back, a new one does.
- ``ask``: only when asked ("what's running?", the ``whats_running`` tool).
- ``off``: never; ``whats_running`` still answers in words.

Asked for, it shows everything running and what ended in the last hour, for ``REQUEST_S`` at least.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octavia.visuals.host import PanelEvent, PanelHost, VisualsError
from octavia.visuals.spec import Visual

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.core.events import EventBus

log = logging.getLogger(__name__)

PANEL_ID = "status"
TICK_S = 3.0  # how often the board looks while its panel is up
IDLE_TICK_S = 5.0  # and while it isn't (memory only: nothing is read from disk)
MIN_SEND_S = 2.0  # the panel isn't redrawn more often than this
GRACE_S = 10.0  # running this long before the panel comes up by itself
DONE_LINGER_S = 60.0
FAILED_LINGER_S = 600.0
REQUEST_S = 90.0  # asked for, it stays at least this long
RECENT_S = 3600.0  # asked for, it also shows what ended this long ago at most
RETRY_S = 60.0  # after the panel couldn't be shown, the board waits this long before trying again
# After the panel closes it isn't shown again for this long (real time): the app still folds it away, and a show of
# the same id meanwhile would meet its "closed" and fail.
REOPEN_GAP_S = 1.0
MAX_ROWS = 6
BAR_CELLS = 10

# How each tracker's states read on the panel, and which ones count as trouble.
JOB_STATES = {
    "starting": "starting",
    "connecting": "connecting",
    "running": "running",
    "stalled": "stalled",
    "stuck": "stuck",
    "unreadable": "checking",
    "done": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "died": "died",
}
WAITING = frozenset({"waiting", "queued"})  # going, but not running: never a reason to come up
TROUBLE = frozenset({"stalled", "stuck", "failed", "died", "unfinished", "rolled back"})


@dataclass(frozen=True)
class Item:
    """One piece of work as the panel shows it."""

    key: str  # "job:3", "task:5", "build:12", ... (stable while it lives)
    kind: str  # "Download", "Command", "Task", "Web task", "Deep think", "Update", "Objective"
    title: str
    state: str  # a word or two: running, waiting, done, failed, ...
    live: bool  # still going (or waiting its turn)
    progress: float | None = None  # 0 to 1, when there's a measure of it
    detail: str = ""
    ended_at: float | None = None  # epoch seconds, when the tracker knows when it ended

    @property
    def trouble(self) -> bool:
        return self.state in TROUBLE

    @property
    def active(self) -> bool:
        """Running, not only waiting its turn (or for the usage budget, which can take days)."""
        return self.live and self.state not in WAITING

    @property
    def mark(self) -> str:
        if self.trouble:
            return "⚠" if self.live else "✗"
        if self.live:
            return "▸"
        return "✓" if self.state in ("done", "installed", "ready") else "–"


# ---------------------------------------------------------------------------------------------------- words


def elapsed(seconds: float) -> str:
    """Minutes at most, so the panel doesn't change every second."""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "under a minute"
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def size(n: float | None) -> str:
    if n is None:
        return "?"
    if n < 1024**2:
        return f"{max(0, round(n / 1024))} KB"
    if n < 1024**3:
        return f"{round(n / 1024**2)} MB"
    return f"{n / 1024**3:.1f} GB"


_FORMATTING = str.maketrans({"*": "∗", "`": "ʹ", "$": "＄", "~": "∼", "[": "(", "]": ")", "|": "¦", "\\": "∖"})


def plain(text: str, limit: int = 120) -> str:
    """Text from a title, an error or a page, shown as it is: on one line, short, and with nothing the panel would
    take for formatting (bold, code, math, a link)."""
    flat = " ".join(str(text).split()).translate(_FORMATTING)
    flat = re.sub(r"(?<!\w)_|_(?!\w)", "‗", flat, flags=re.ASCII)  # only an underscore at a word's edge makes italics
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def bar(fraction: float) -> str:
    fraction = min(1.0, max(0.0, fraction))
    full = round(fraction * BAR_CELLS)
    return "▰" * full + "▱" * (BAR_CELLS - full) + f" {int(fraction * 100)}%"


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------- the trackers


def job_items(checks: Iterable[Any]) -> list[Item]:
    """Long jobs, from ``_long_jobs.check`` results."""
    out = []
    for c in checks:
        state = JOB_STATES.get(c.status, c.status)
        download = c.kind == "download"
        title = Path(c.path).name if download and c.path else c.title
        progress = None
        detail = ""
        if download:
            progress = c.done / c.total if c.total else None
            amount = f"{size(c.done)} of {size(c.total)}" if c.total else f"{size(c.done)} so far"
            if c.alive:
                parts = [amount]
                if c.status == "running" and c.rate > 0:
                    parts.append(f"{c.rate / 1024**2:.1f} MB/s")
                    if c.total:
                        parts.append(f"about {elapsed(max(0, c.total - c.done) / c.rate)} left")
                detail = " · ".join(parts)
            elif c.status == "done":
                detail = size(c.done)
            else:
                detail = amount
        if c.error and not c.alive and c.status != "done":
            detail = f"{detail}. {plain(c.error, 160)}" if detail else plain(c.error, 160)
            if c.resumable:
                detail += " It can be resumed."
        elif not download and c.alive and c.output:
            last = c.output.strip().splitlines()[-1:] or [""]
            detail = plain(last[0], 90)
        out.append(
            Item(
                f"job:{c.id}", "Download" if download else "Command", plain(title, 80), state, c.alive, progress, detail
            )
        )
    return out


def task_items(tasks: Iterable[Any], kind: str, prefix: str, now: float) -> list[Item]:
    """Background tasks, web tasks or deep thinks (``_background_tasks.Task``)."""
    out = []
    for t in tasks:
        detail = ""
        if t.status == "queued":
            state, detail = "waiting", "waiting for a free slot"
        elif t.status == "running":
            state = "running"
            started = _epoch(t.started)
            parts = [f"{t.steps} step{'s' if t.steps != 1 else ''}"]
            if t.activity:
                parts.append(plain(t.activity, 60))
            if started is not None:
                parts.append(elapsed(now - started))
            detail = " · ".join(parts)
        else:
            state = t.status if t.status != "unfinished" else "unfinished"
            if t.status in ("done", "unfinished"):
                detail = plain(t.summary, 140)
            elif t.status == "failed":
                detail = plain(t.error or "", 140)
        out.append(
            Item(f"{prefix}:{t.id}", kind, plain(t.title, 80), state, t.active, None, detail, _epoch(t.finished))
        )
    return out


def objective_items(objectives: Iterable[Any]) -> list[Item]:
    out = []
    for o in objectives:
        steps = [s for s in o.steps if s.status != "replaced"]  # the plan as it stands (``Objective.live_steps``)
        total = len(steps)
        verified = sum(1 for s in steps if s.status == "verified")
        progress = verified / total if total else None
        detail = ""
        if o.status == "planning":
            state, detail = "planning", "making a plan"
        elif o.open:
            current = next((s for s in steps if s.status in ("running", "checking")), None)
            state = "waiting" if o.waiting and current is None else "working"
            if o.waiting and current is None:
                detail = plain(o.waiting, 120)
            elif current is not None:
                number = steps.index(current) + 1
                what = "checking" if current.status == "checking" else plain(current.activity, 60) or "working"
                detail = f"step {number} of {total}: {plain(current.title, 60)} · {what}"
            else:
                detail = f"{verified} of {total} steps verified"
        else:
            state = o.status
            if o.status in ("failed",) and o.error:
                detail = plain(o.error, 140)
            elif o.status == "blocked" and getattr(o, "blocked_on", ""):
                detail = "needs you: " + plain(o.blocked_on, 120)
            elif total:
                detail = f"{verified} of {total} steps verified"
        title = o.title or o.goal
        out.append(
            Item(
                f"objective:{o.id}", "Objective", plain(title, 80), state, o.open, progress, detail, _epoch(o.finished)
            )
        )
    return out


# A running build's stage, said short (``evolve.manager``'s stages are sentences for her to say).
_STAGES = {
    "getting a copy of my code ready": "getting ready",
    "the developer agent is writing the change": "writing the change",
    "the developer agent is fixing what my final checks found (its second and last try)": "fixing what checks found",
    "running my final checks (the linter and the full test suite, a few minutes)": "running the final checks",
    "waiting for another build's final checks to finish before running its own": "waiting to run its checks",
}


@dataclass
class _Build:
    title: str
    status: str
    changed: float  # epoch seconds
    message: str = ""


def build_items(builds: dict[int, _Build], manager: Any) -> list[Item]:
    """Self-improvement builds and installs: what its events said, and how a running build is going."""
    installing = getattr(manager, "installing_ids", frozenset()) if manager is not None else frozenset()
    out = []
    for number, b in builds.items():
        live, detail, ended = False, "", b.changed
        if b.status == "queued":
            state, live, detail = "queued", True, "waiting to be built"
        elif b.status == "building":
            state, live = "building", True
            progress = manager.build_progress(number) if manager is not None else None
            if progress is not None:
                stage, steps, seconds = progress
                parts = [_STAGES.get(stage, plain(stage, 60))]
                if steps:
                    parts.append(f"{steps} step{'s' if steps != 1 else ''}")
                parts.append(elapsed(seconds))
                detail = " · ".join(parts)
        elif b.status == "ready" and number in installing:
            state, live, detail = "installing", True, "testing and installing"
        elif b.status == "ready":
            state, detail = "ready", "built and checked: ready to install"
        elif b.status == "installed":
            state = "installed"
        elif b.status == "failed":
            state, detail = "failed", plain(b.message, 140)
        elif b.status == "no_change":
            state, detail = "no change", "nothing in the code needed changing"
        elif b.status == "rolled_back":
            state = "rolled back"
        elif b.status == "rejected":
            state = "dropped"
        else:  # noted: nothing running
            continue
        out.append(
            Item(f"build:{number}", "Update", plain(b.title, 80), state, live, None, detail, None if live else ended)
        )
    return out


# ---------------------------------------------------------------------------------------------------- the panel


def _order(items: list[Item]) -> list[Item]:
    """Running ones first (trouble first among them), then failures, then the rest; otherwise as gathered."""
    return sorted(items, key=lambda i: (0 if i.live else 1, 0 if i.trouble else 1))


def render(items: list[Item], *, requested: bool = False) -> Visual:
    """The panel for ``items`` (already chosen and in order); the first ``MAX_ROWS`` get a row each."""
    running = sum(1 for i in items if i.live)
    title = f"Running · {running}" if running else "Background work"
    if not items:
        return Visual.model_validate(
            {"title": title, "blocks": [{"kind": "text", "markdown": "Nothing's running right now."}]}
        )
    rows = []
    for item in items[:MAX_ROWS]:
        parts = [f"**{item.state}**"]  # one line a row where it fits: the panel stays small
        if item.progress is not None and (item.live or item.state != "done"):
            parts.append(bar(item.progress))
        if item.detail:
            parts.append(item.detail)
        rows.append({"title": f"{item.mark} {item.kind}: {item.title}", "text": " · ".join(parts)})
    block: dict[str, Any] = {"kind": "steps", "items": rows}
    more = len(items) - MAX_ROWS
    if more > 0:
        block["caption"] = f"And {more} more. Ask “what's running?” for all of it."
    elif not requested and not running:
        block["caption"] = "All finished. It closes by itself."
    return Visual.model_validate({"title": title, "blocks": [block]})


def spoken(items: list[Item]) -> str:
    """The same in words, for her answer to "what's running?"."""
    if not items:
        return "Nothing is running in the background right now, and nothing ended in the last hour."
    lines = []
    for item in items:
        line = f"{item.kind} “{item.title}”: {item.state}"
        if item.detail:
            line += f" ({item.detail})"
        if item.progress is not None and item.live:
            line += f", {int(item.progress * 100)}%"
        lines.append(line + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------- the board


class StatusBoard:
    """Keeps the status panel in step with the trackers in ``extras`` (the tool context's: "long_jobs", "tasks",
    "web_tasks", "thoughts", "objectives", "evolve", and "screen_busy"), looked up each time so the order the app
    starts them in doesn't matter. ``start`` on the loop, ``aclose`` at shutdown."""

    def __init__(
        self,
        host: PanelHost,
        config: AppConfig,
        extras: dict[str, Any],
        events: EventBus | None = None,
        *,
        clock: Callable[[], float] = time.time,
        tick_s: float = TICK_S,
        idle_tick_s: float = IDLE_TICK_S,
    ) -> None:
        self.host = host
        self.config = config
        self.extras = extras
        self.events = events
        self._clock = clock
        self._tick_s = tick_s
        self._idle_tick_s = idle_tick_s
        self._jobs: list[Any] = []  # the job watcher's latest checks
        self._jobs_seen = False  # the watcher's first round has come in
        self._jobs_hooked: Any = None  # the JobManager whose listeners have ours
        self._builds: dict[int, _Build] = {}
        self._first_live: dict[str, float] = {}  # when each item was first seen going
        self._ended: dict[str, float] = {}  # when each item was first seen ended
        self._history: set[str] = set()  # ended before the board started: only shown when asked
        self._dismissed: set[str] = set()
        self._shown: list[str] = []  # the keys on the panel now
        self._wire: str | None = None  # what the panel shows now
        self._sent_at = float("-inf")
        self._requested_until = 0.0
        self._retry_at = 0.0
        self._closing = False  # the board itself is closing the panel (not a dismissal)
        self._closed_at = float("-inf")  # time.monotonic() when the panel last closed
        self._started = False
        self._lock = asyncio.Lock()
        self._poke = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._unsubscribe: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ settings

    @property
    def mode(self) -> str:
        if not self.config.visuals.enabled:
            return "off"
        return str(getattr(self.config.visuals, "status_panel", "auto"))

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        self._unsubscribe.append(self.host.subscribe(self._on_panel))
        self.host.self_updating.add(PANEL_ID)
        if self.events is not None:
            from octavia.core.events import ImprovementChanged

            self._unsubscribe.append(self.events.subscribe(self._on_improvement, ImprovementChanged))
        await self._seed_builds()
        self._hook_jobs()
        self._take_stock(self.gather(), self._clock(), first=True)
        self._started = True
        self._task = asyncio.get_running_loop().create_task(self._run(), name="status-panel")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for stop in self._unsubscribe:
            stop()
        self._unsubscribe.clear()
        jobs = self._jobs_hooked
        if jobs is not None and self._on_jobs in jobs.listeners:
            jobs.listeners.remove(self._on_jobs)

    async def _seed_builds(self) -> None:
        manager = self.extras.get("evolve")
        journal = getattr(manager, "journal", None)
        if journal is None:
            return
        from octavia.evolve.models import Status

        try:
            items = await journal.list([Status.QUEUED, Status.BUILDING, Status.READY], limit=20)
        except Exception:
            log.warning("The status panel couldn't read the self-improvement list", exc_info=True)
            return
        for item in items:
            self._builds.setdefault(
                item.id, _Build(item.title, item.status.value, float(item.updated_at or self._clock()))
            )

    def _hook_jobs(self) -> None:
        jobs = self.extras.get("long_jobs")
        if jobs is None or jobs is self._jobs_hooked or not hasattr(jobs, "listeners"):
            return
        jobs.listeners.append(self._on_jobs)
        self._jobs_hooked = jobs

    # ------------------------------------------------------------------ what's going on

    def _on_jobs(self, checks: list[Any]) -> None:
        if not self._jobs_seen:  # the watcher's first round: jobs that had ended by then are history, not news
            self._jobs_seen = True
            self._take_stock(job_items(checks), self._clock(), first=True)
        self._jobs = list(checks)
        self._poke.set()

    def _on_improvement(self, event: Any) -> None:
        self._builds[event.improvement_id] = _Build(event.title, event.status, self._clock(), event.message)
        self._poke.set()

    def gather(self) -> list[Item]:
        """Everything the trackers have, as panel items (memory only)."""
        now = self._clock()
        items = job_items(self._jobs)
        for name, kind, prefix in (
            ("tasks", "Task", "task"),
            ("web_tasks", "Web task", "web"),
            ("thoughts", "Deep think", "think"),
        ):
            manager = self.extras.get(name)
            if manager is not None:
                items += task_items(manager.tasks, kind, prefix, now)
        runner = self.extras.get("objectives")
        if runner is not None:
            items += objective_items(runner.objectives)
        items += build_items(self._builds, self.extras.get("evolve"))
        return items

    def _take_stock(self, items: list[Item], now: float, *, first: bool = False) -> None:
        """When each item was first seen going, and first seen ended. What had ended before the board started is
        history (shown only when asked); so is a build the journal had as ready from before."""
        for item in items:
            if item.live:
                self._first_live.setdefault(item.key, now)
                self._ended.pop(item.key, None)
                self._history.discard(item.key)
            elif item.key not in self._ended:
                self._ended[item.key] = now
                if first:
                    self._history.add(item.key)

    def _recent(self, item: Item, now: float) -> bool:
        """Ended in the last hour (as far as anyone knows when), or a build that waits for the user to install it."""
        if item.state == "ready":
            return True
        if item.ended_at is not None:
            return now - item.ended_at <= RECENT_S
        ended = self._ended.get(item.key)
        return ended is not None and item.key not in self._history and now - ended <= RECENT_S

    def choose(self, items: list[Item], now: float) -> list[Item]:
        """The items the panel shows now, in order."""
        if now < self._requested_until:
            return _order([i for i in items if i.live or self._recent(i, now)])
        chosen = []
        for item in items:
            if item.key in self._dismissed or item.key in self._history:
                continue
            if item.live:
                chosen.append(item)
                continue
            ended = self._ended.get(item.key)
            linger = FAILED_LINGER_S if item.trouble else DONE_LINGER_S
            if ended is not None and now - ended < linger:
                chosen.append(item)
        if not any(i.active or not i.live for i in chosen):
            return []  # only things waiting their turn: they don't keep it up
        return _order(chosen)

    def _wants_to_open(self, chosen: list[Item], now: float) -> bool:
        """Whether the panel may come up by itself now (it isn't up)."""
        if self.mode != "auto" or now < self._retry_at or self._screen_busy():
            return False
        if len(self.host.open_panels) + 1 >= self.config.visuals.max_panels:
            return False  # it leaves a free place, so her next panel never pushes out one she's explaining with
        return any(i.active and now - self._first_live.get(i.key, now) >= GRACE_S for i in chosen)

    def _screen_busy(self) -> bool:
        busy = self.extras.get("screen_busy")
        try:
            return bool(busy()) if callable(busy) else False
        except Exception:
            return False

    # ------------------------------------------------------------------ the loop

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("The status panel couldn't refresh", exc_info=True)
            wait = self._tick_s if self._up else self._idle_tick_s
            self._poke.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._poke.wait(), wait)
            gap = MIN_SEND_S - (self._clock() - self._sent_at)
            if gap > 0:
                await asyncio.sleep(min(gap, MIN_SEND_S))

    @property
    def _up(self) -> bool:
        return PANEL_ID in self.host.open_panels

    async def refresh(self) -> None:
        """Brings the panel in step with the trackers: up, updated, closed, or left alone."""
        async with self._lock:
            self._hook_jobs()
            now = self._clock()
            items = self.gather()
            self._take_stock(items, now)
            requested = now < self._requested_until
            if self.mode == "off" or (self.mode == "ask" and not requested):
                await self._close()
                return
            if not requested and self._up and self._screen_busy():
                await self._close()  # a screen task is working: a panel in the way would stop its clicks
                return
            if not requested and self._up and len(self.host.open_panels) >= self.config.visuals.max_panels:
                await self._close()  # her panels filled up: the next one mustn't push out one she's explaining with
                return
            chosen = self.choose(items, now)
            if not chosen and not requested:
                await self._close()
                return
            if not self._up and not requested and not self._wants_to_open(chosen, now):
                return
            if not self._up:
                gap = REOPEN_GAP_S - (time.monotonic() - self._closed_at)
                if gap > 0:
                    if not requested:
                        return  # the next tick shows it
                    await asyncio.sleep(gap)
            visual = render(chosen, requested=requested)
            wire = json.dumps(visual.to_wire(), sort_keys=True, ensure_ascii=False)
            if self._up and wire == self._wire:
                return
            try:
                await self.host.show(visual, panel_id=PANEL_ID, stay_s=0, sound=False)
            except VisualsError as exc:
                log.info("The status panel couldn't be shown: %s", exc)
                self._retry_at = now + RETRY_S
                self._wire = None
                return
            self._wire, self._sent_at = wire, self._clock()
            self._shown = [i.key for i in chosen]  # all of it, rows past the last included: closing dismisses them all
            if not requested and self._screen_busy():
                await self._close()  # a screen task started while it came up

    async def _close(self) -> None:
        if not self._up:
            return
        self._closing = True
        try:
            await self.host.close(PANEL_ID)
        except VisualsError as exc:
            log.info("The status panel couldn't be closed: %s", exc)
        finally:
            self._closing = False

    def _on_panel(self, event: PanelEvent) -> None:
        if event.id != PANEL_ID:
            return
        self._wire = None
        self._closed_at = time.monotonic()
        if event.by == "user" or (event.by == "octavia" and not self._closing):
            self._dismissed.update(self._shown)  # closed on purpose: what was on it doesn't bring it back
            self._requested_until = 0.0
        elif event.by in ("crashed", "failed", "app"):  # an app that keeps dying isn't started again every tick
            self._retry_at = self._clock() + RETRY_S
        self._shown = []

    # ------------------------------------------------------------------ asked for

    async def request(self) -> tuple[str, bool]:
        """ "What's running?": the panel comes up (unless panels are off) for a while with everything running and
        what ended in the last hour. Returns (the same in words, whether the panel is up)."""
        now = self._clock()
        items = self.gather()
        self._take_stock(items, now)
        if self.mode != "off":
            self._requested_until = now + REQUEST_S
            self._retry_at = 0.0
            await self.refresh()
        chosen = _order([i for i in items if i.live or self._recent(i, now)])
        return spoken(chosen), self._up

    async def hide(self) -> bool:
        """Takes the panel down, as if the user closed it. False when it wasn't up."""
        if not self._up:
            return False
        self._dismissed.update(self._shown)
        self._requested_until = 0.0
        await self._close()
        return True
