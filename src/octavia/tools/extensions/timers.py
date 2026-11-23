"""Timers: "set a timer for five seconds", rung out loud or with a chime when they're done.

Scheduled jobs and TickTick reminders only go down to the minute and a job runs through the brain; a timer is just a
delay on the event loop, down to the second, and ringing it costs no Claude usage. "speak" says it as an announcement
(``ctx.extras["announce"]``: once nobody is talking, so it never cuts into a conversation); "chime" plays a ring
right away (``ctx.extras["chime"]``) and falls back to speaking when there's nothing to play it on. Timers live only
while she runs: a restart forgets them.

Each timer set, rung or cancelled is also told on the event bus (``TimerChanged``), for Octavia Futures; nothing about
the timer depends on anyone listening.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from octavia.core.events import TimerChanged
from octavia.core.text import spoken_list
from octavia.tools.base import NoArgs, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

MIN_S = 1.0
MAX_S = 24 * 3600.0
SOUND = "timer"  # the earcon a chiming timer plays
RING_NOTE = "That was a timer the user set, ringing on its own; it's done and gone."
CUE = "timer"  # what kind of announcement a ring is (the avatar holds up a ringing stopwatch)


def spoken_duration(seconds: float) -> str:
    """ "1 hour, 5 minutes and 30 seconds", as it's said."""
    hours, rest = divmod(max(1, round(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{n} {unit}" + ("" if n == 1 else "s")
        for n, unit in ((hours, "hour"), (minutes, "minute"), (secs, "second"))
        if n
    ]
    return spoken_list(parts)


@dataclass
class Timer:
    id: int
    seconds: float
    label: str
    alert: str
    ends: float  # on the event loop's clock
    handle: asyncio.TimerHandle | None = None

    def title(self) -> str:
        """How she names it: "pasta timer", or "timer for 5 minutes"."""
        return f"{self.label} timer" if self.label else f"timer for {spoken_duration(self.seconds)}"

    def ring_text(self) -> str:
        return f"Time's up! Your {self.title()} is done."


Ring = Callable[[Timer], None]


class TimerBoard:
    """The running timers. Call on the event loop thread."""

    def __init__(self) -> None:
        self._timers: dict[int, Timer] = {}
        self._next_id = 1

    def start(self, seconds: float, label: str, alert: str, ring: Ring) -> Timer:
        loop = asyncio.get_running_loop()
        timer = Timer(self._next_id, seconds, label, alert, loop.time() + seconds)
        self._next_id += 1
        timer.handle = loop.call_later(seconds, self._ring, timer, ring)
        self._timers[timer.id] = timer
        return timer

    def _ring(self, timer: Timer, ring: Ring) -> None:
        self._timers.pop(timer.id, None)
        try:
            ring(timer)
        except Exception:
            log.warning("Couldn't ring timer #%s", timer.id, exc_info=True)

    def running(self) -> list[Timer]:
        """Soonest first."""
        return sorted(self._timers.values(), key=lambda t: t.ends)

    def remaining(self, timer: Timer) -> float:
        return max(0.0, timer.ends - asyncio.get_running_loop().time())

    def cancel(self, timer: Timer) -> None:
        if timer.handle is not None:
            timer.handle.cancel()
        self._timers.pop(timer.id, None)

    def find(self, ref: str) -> list[Timer]:
        """The timers ``ref`` means: its number ("2", "#2"), its label (or part of it), or every one for "all"."""
        ref = ref.strip()
        running = self.running()
        if ref.casefold() == "all":
            return running
        number = ref.lstrip("#")
        if number.isdigit():
            return [t for t in running if t.id == int(number)]
        wanted = ref.casefold()
        exact = [t for t in running if t.label.casefold() == wanted]
        return exact or [t for t in running if wanted and wanted in t.label.casefold()]


BOARD = TimerBoard()


def _tell(ctx: ToolContext, action: str, timer: Timer) -> None:
    """Say on ``ctx.events`` that ``timer`` was set, rang or was cancelled. Never in the timer's way: a failure here
    is only logged."""
    try:
        ctx.events.emit(
            TimerChanged(
                action=action, timer_id=timer.id, seconds=float(timer.seconds), label=timer.label, alert=timer.alert
            )
        )
    except Exception:
        log.debug("Couldn't tell that timer #%s %s", timer.id, action, exc_info=True)


def ringer(ctx: ToolContext) -> Ring | None:
    """How a timer set in ``ctx`` rings; None when it can't (no voice or console to say it on)."""
    announce = ctx.extras.get("announce")
    chime = ctx.extras.get("chime")
    if not callable(announce) and not callable(chime):
        return None

    def ring(timer: Timer) -> None:
        _tell(ctx, "rang", timer)  # first, so a ring that's only a chime is seen too
        if timer.alert == "chime" and callable(chime) and chime(SOUND):
            return
        if callable(announce):
            announce(timer.ring_text(), RING_NOTE, False, cue=CUE)

    return ring


def _listing(board: TimerBoard) -> str:
    running = board.running()
    if not running:
        return "No timers are running."
    lines = [
        f"#{t.id} {t.title()}: {spoken_duration(board.remaining(t))} left" + (" (chimes)" if t.alert == "chime" else "")
        for t in running
    ]
    return "Running timers:\n" + "\n".join(lines)


class SetTimerArgs(ToolArgs):
    seconds: float = Field(default=0, ge=0)
    minutes: float = Field(default=0, ge=0)
    hours: float = Field(default=0, ge=0)
    label: str = Field(default="", max_length=60, description="e.g. 'pasta'")
    alert: Literal["speak", "chime"] = Field(default="speak", description="chime: a ring instead of speaking.")

    @property
    def total(self) -> float:
        return self.seconds + self.minutes * 60 + self.hours * 3600

    @model_validator(mode="after")
    def _in_range(self) -> Self:
        if not MIN_S <= self.total <= MAX_S:
            raise ValueError("A timer runs from 1 second to 24 hours.")
        return self


class SetTimer(Tool):
    name = "set_timer"
    description = (
        "Start a countdown timer of seconds + minutes + hours (1 s to 24 h). For a clock time or recurring, use a "
        "scheduled job or reminder."
    )
    Args = SetTimerArgs
    risk = RiskLevel.SAFE
    category = "time"

    def __init__(self, board: TimerBoard | None = None) -> None:
        self.board = board if board is not None else BOARD

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        return None if ringer(ctx) is not None else "Timers can't ring here: there's no voice or console to ring on."

    async def run(self, args: SetTimerArgs, ctx: ToolContext) -> ToolResult:
        ring = ringer(ctx)
        if ring is None:
            return ToolResult.error("Timers can't ring here: there's no voice or console to ring on.")
        label = args.label.strip()
        if label.casefold().endswith(" timer"):
            label = label[: -len(" timer")].strip()
        timer = self.board.start(args.total, label, args.alert, ring)
        _tell(ctx, "created", timer)
        how = "play a chime" if args.alert == "chime" else "say so"
        return ToolResult.success(
            f"Timer #{timer.id} set for {spoken_duration(args.total)}"
            + (f" ({label})" if label else "")
            + f". When it's done you'll {how} on your own.",
            data={"id": timer.id, "seconds": args.total, "label": label, "alert": args.alert},
        )


class ListTimers(Tool):
    name = "list_timers"
    description = "List the running timers and how long each has left."
    Args = NoArgs
    risk = RiskLevel.SAFE
    category = "time"

    def __init__(self, board: TimerBoard | None = None) -> None:
        self.board = board if board is not None else BOARD

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.success(_listing(self.board))


class CancelTimerArgs(ToolArgs):
    timer: str = Field(
        default="",
        description="Number, label or 'all'; empty if only one runs.",
    )


class CancelTimer(Tool):
    name = "cancel_timer"
    description = "Stop a running timer (or all)."
    Args = CancelTimerArgs
    risk = RiskLevel.SAFE
    category = "time"

    def __init__(self, board: TimerBoard | None = None) -> None:
        self.board = board if board is not None else BOARD

    async def run(self, args: CancelTimerArgs, ctx: ToolContext) -> ToolResult:
        running = self.board.running()
        if not running:
            return ToolResult.error("No timers are running.")
        if not args.timer:
            if len(running) > 1:
                return ToolResult.error("More than one timer is running; say which.\n" + _listing(self.board))
            found = running
        else:
            found = self.board.find(args.timer)
        if not found:
            return ToolResult.error(f"No running timer matches “{args.timer}”.\n" + _listing(self.board))
        if len(found) > 1 and args.timer.casefold() != "all":
            return ToolResult.error(f"“{args.timer}” matches more than one timer; say which.\n" + _listing(self.board))
        for timer in found:
            self.board.cancel(timer)
            _tell(ctx, "cancelled", timer)
        names = ", ".join(f"#{t.id} {t.title()}" for t in found)
        return ToolResult.success(f"Cancelled {names}.")


TOOLS = [SetTimer, ListTimers, CancelTimer]
