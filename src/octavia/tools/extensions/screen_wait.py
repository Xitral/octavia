"""Wait for the screen to change, inside the turn: for a step of a task whose result takes a while to show (a
submission being graded on DataCamp, a page loading, an install finishing).

``start_screen_watch`` wakes her only between the user's turns, so a task that ends its turn to wait for one stalls
when the wake never comes. This tool blocks instead, comparing tiny copies of the window that was in front (as the
watch does, on this computer, sending nothing), and returns once part of it has changed and has stopped changing, or
when its time is up. She then looks with ``look_at_screen``, which is why this one is SAFE: no picture leaves the
computer here. It stops early when the user cuts the turn off.

What it compares with is how the window looked at her last click or key press (``BEFORE``, which the computer-control
tools fill right before their input), not how it looks when the wait starts: she often calls it after the change
already happened (an Athena query finishing seconds after Run), and comparing from then would wait the full time for
a second change. Without such a copy (it's used once, and only for the same window) the first look is the baseline.

With ``until_gone`` it waits for a progress label ("Running", "Submitting") to leave the window instead, asking UI
Automation for it once a second.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult, TurnInfo
from octavia.tools.extensions import _uia
from octavia.tools.extensions._uia import Match, UIAUnavailable
from octavia.tools.extensions.screen_capture import Capture, capture_screen
from octavia.tools.extensions.screen_pointing import avatar_pids
from octavia.tools.extensions.screen_watch import (
    _REGION_WORDS,
    MAX_FAILURES,
    PATCH_DIFF,
    REGIONS,
    Region,
    foreground_window,
    thumbnail,
)

log = logging.getLogger(__name__)

MAX_WAIT_S = 120.0
DEFAULT_WAIT_S = 15.0
EVERY_S = 1.0
STABLE_S = 3.0  # a smaller change counts once nothing at all has moved for this long
SMALL_CHANGE = 0.005  # the least that counts that way (0.5% of the region)
APPEAR_S = 2.0  # a progress label not showing this long after the input: it was over before the wait began
TAKE_S = 0.5  # the most a copy before her input may hold that input up
COPY_MAX_AGE_S = 60.0  # an older copy says nothing about what her last input did

# Searches for a label: the text, the processes to look past, and the time budget (0: the foreground window only).
Find = Callable[[str, Collection[int], float], Awaitable[list[Match]]]


def changed_share(before: np.ndarray, after: np.ndarray) -> float:
    """The share of patches of two tiny copies that differ."""
    return float((np.abs(before - after).max(axis=2) > PATCH_DIFF).mean())


@dataclass(frozen=True)
class Before:
    """Tiny copies of the window in front, each region's, taken right before one of her clicks or key presses."""

    window: int
    taken: float  # on the store's clock
    action: str  # what came after it, in words: "your click", "pressing Enter"
    looks: dict[str, np.ndarray]


class InputSnapshots:
    """The copy of the window from right before her last click or key press, for ``wait_for_screen_change`` to compare
    with. The computer-control tools ``take`` it; the wait ``use``s it once."""

    def __init__(
        self,
        capture: Capture | None = None,
        front: Callable[[], int] = foreground_window,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capture = capture
        self.front = front
        self.clock = clock
        self._copy: Before | None = None

    async def take(self, action: str) -> None:
        """Copy the window in front now, off the event loop and for at most ``TAKE_S``. Never raises: without a copy
        the wait just starts from its own first look."""
        self._copy = None
        if self._capture is None and sys.platform != "win32":
            return
        capture = self._capture or capture_screen
        taken = self.clock()
        try:
            window = self.front()
            pixels, *_ = await asyncio.wait_for(asyncio.to_thread(capture, "window"), TAKE_S)
            looks = {region: thumbnail(pixels, region) for region in REGIONS}
        except Exception:
            log.debug("Copying the window before her input failed", exc_info=True)
            return
        self._copy = Before(window, taken, action, looks)

    def use(self, window: int) -> Before | None:
        """The copy, once, if it's of ``window`` and recent enough."""
        copy, self._copy = self._copy, None
        if copy is None or copy.window != window or self.clock() - copy.taken > COPY_MAX_AGE_S:
            return None
        return copy

    def forget(self) -> None:
        """Drop the copy: what happened since (scrolling) would count as the change."""
        self._copy = None


BEFORE = InputSnapshots()


class WaitForScreenChangeArgs(ToolArgs):
    seconds: float = Field(
        default=DEFAULT_WAIT_S,
        ge=3,
        le=MAX_WAIT_S,
        description="The longest to wait, in seconds: more for something known to be slow.",
    )
    region: Region = Field(
        default="screen",
        description="Where in the window the change will show.",
    )
    change: float = Field(
        default=0.03,
        ge=0.005,
        le=0.9,
        description="Share of the region that must change (0.03 = 3%): lower for a small result message.",
    )
    until_gone: str = Field(
        default="",
        max_length=60,
        description="Instead, wait until this progress label is gone from the window, e.g. 'Running'.",
    )


class WaitForScreenChange(Tool):
    name = "wait_for_screen_change"
    description = (
        "Wait, inside this turn, until the window in front changes and settles: after Submit or Run (grading, a "
        "query), a page load, a download or install. It compares "
        "with the window right before your last click or key press, so a change that already happened counts at "
        "once. Then call look_at_screen and keep going. Use this, not start_screen_watch, in the middle of a task: "
        "a watch only wakes you between the user's turns, so ending your turn to wait for it stalls the task."
    )
    Args = WaitForScreenChangeArgs
    risk = RiskLevel.SAFE  # it only compares pixels on this computer; nothing is sent until she looks
    category = "system"
    timeout_s = MAX_WAIT_S + 15.0

    def __init__(
        self,
        capture: Capture | None = None,
        front: Callable[[], int] = foreground_window,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        before: InputSnapshots | None = None,
        find: Find | None = None,
    ) -> None:
        self._capture = capture
        self.front = front
        self.sleep = sleep
        self.clock = clock
        # The copies her clicks and keys take of the real screen: only with the real screen, never with a fake one.
        self.before = before if before is not None else (BEFORE if capture is None else None)
        self._find = find

    async def precheck(self, args: WaitForScreenChangeArgs, ctx: ToolContext) -> str | None:
        if self._capture is None and sys.platform != "win32":
            return "Watching the screen only works on Windows."
        return None

    async def run(self, args: WaitForScreenChangeArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        capture = self._capture or capture_screen
        where = _REGION_WORDS[args.region]
        started = _turn(ctx)
        start = self.clock()
        deadline = start + args.seconds
        window = self.front()
        copy = self.before.use(window) if self.before is not None else None
        if args.until_gone.strip():
            return await self._until_gone(args, ctx, started, window, copy)
        # From right before her input, so a change that already happened counts; else from the first look.
        baseline = copy.looks.get(args.region) if copy is not None else None
        from_input = baseline is not None
        last: np.ndarray | None = None
        still_since = start  # when anything at all last moved
        failures = 0
        share = 0.0
        while True:
            if _cut_off(ctx, started):
                return ToolResult.success(
                    "Stopped waiting: the user cut the turn off.", data={"changed": False, "cut_off": True}
                )
            if self.front() != window:
                last = None  # another window is in front: what it shows isn't the change awaited
            else:
                try:
                    pixels, *_ = await asyncio.to_thread(capture, "window")
                except Exception:
                    failures += 1
                    log.debug("Waiting for the screen to change, a look failed", exc_info=True)
                    if failures >= MAX_FAILURES:
                        return ToolResult.error("I couldn't read the screen to wait for it to change.")
                else:
                    failures = 0
                    look = thumbnail(pixels, args.region)
                    now = self.clock()
                    if baseline is None or baseline.shape != look.shape:
                        # The first look without a copy from her input, or the window was resized: compare from here.
                        from_input = from_input and baseline is None
                        baseline = look
                    moved = changed_share(last, look) if last is not None and last.shape == look.shape else None
                    if moved is None or moved > 0:
                        still_since = now
                    share = changed_share(baseline, look)
                    # Changed and settled; or a smaller change, with nothing at all moving since (a ticking timer
                    # or a spinner keeps it waiting).
                    if moved is not None and (
                        (share >= args.change and moved < args.change / 2)
                        or (share >= SMALL_CHANGE and now - still_since >= STABLE_S)
                    ):
                        waited = round(now - start)
                        data: dict[str, Any] = {"changed": True, "share": round(share, 3), "waited_s": waited}
                        if from_input and copy is not None:
                            ago = round(now - copy.taken)
                            data["since_input_s"] = ago
                            what = f"{share:.0%} of {where} had changed since {copy.action} {ago} seconds ago"
                        else:
                            what = f"After {waited} seconds, {share:.0%} of {where} had changed"
                        return ToolResult.success(
                            f"{what} and settled. Call look_at_screen to see it, then carry on with the task.",
                            data=data,
                        )
                    last = look
            if self.clock() >= deadline:
                break
            await self.sleep(min(EVERY_S, max(0.0, deadline - self.clock())))
        waited = round(self.clock() - start)
        if share >= args.change:
            what = f"{share:.0%} of {where} changed but kept changing"
        else:
            what = f"{where} didn't change much"
        if from_input:  # a change since her input would have counted
            hint = "it may still be working."
        else:
            hint = "it may have changed before you started waiting, or still be working."
        return ToolResult.success(
            f"Waited {waited} seconds: {what}. Call look_at_screen to check: {hint}",
            data={"changed": False, "share": round(share, 3), "waited_s": waited},
        )

    async def _until_gone(
        self,
        args: WaitForScreenChangeArgs,
        ctx: ToolContext,
        started: TurnInfo | None,
        window: int,
        copy: Before | None,
    ) -> ToolResult:
        """Wait for ``args.until_gone`` to leave the window in front: searched there alone, once a second."""
        label = args.until_gone.strip()
        find = self._find or _uia.find
        pids = avatar_pids(ctx)
        start = self.clock()
        deadline = start + args.seconds
        since = copy.taken if copy is not None else start  # her input, when it's known
        seen = False
        failures = 0
        while True:
            if _cut_off(ctx, started):
                return ToolResult.success(
                    "Stopped waiting: the user cut the turn off.", data={"changed": False, "cut_off": True}
                )
            if self.front() == window:  # another window in front: its labels aren't the ones awaited
                try:
                    found = await find(label, pids, 0)
                except UIAUnavailable:
                    return ToolResult.error(
                        f"I can't read the window's labels here, so I can't wait for “{label}” to go. Wait without "
                        "until_gone instead."
                    )
                except Exception:
                    failures += 1
                    log.debug("Waiting for %r to go, a search failed", label, exc_info=True)
                    if failures >= MAX_FAILURES:
                        return ToolResult.error(f"I couldn't search the window to wait for “{label}” to go.")
                else:
                    failures = 0
                    now = self.clock()
                    waited = round(now - start)
                    if any(m.foreground and not m.covered for m in found):
                        seen = True
                    elif seen:
                        return ToolResult.success(
                            f"After {waited} seconds, “{label}” is gone. Call look_at_screen to see the result, then "
                            "carry on with the task.",
                            data={"changed": True, "gone": True, "waited_s": waited},
                        )
                    elif now - since >= APPEAR_S:
                        return ToolResult.success(
                            f"“{label}” isn't showing, so it seems to have finished already. Call look_at_screen to "
                            "see the result, then carry on with the task.",
                            data={"changed": True, "gone": True, "waited_s": waited},
                        )
            if self.clock() >= deadline:
                break
            await self.sleep(min(EVERY_S, max(0.0, deadline - self.clock())))
        waited = round(self.clock() - start)
        return ToolResult.success(
            f"Waited {waited} seconds: “{label}” is still showing. Call look_at_screen to check, or wait again if it's "
            "still working.",
            data={"changed": False, "gone": False, "waited_s": waited},
        )


def _turn(ctx: ToolContext) -> TurnInfo | None:
    current = ctx.extras.get("turn")
    if not callable(current):
        return None
    try:
        info = current()
    except Exception:
        log.debug("Asking which turn is running failed", exc_info=True)
        return None
    return info if isinstance(info, TurnInfo) else None


def _cut_off(ctx: ToolContext, started: TurnInfo | None) -> bool:
    """Whether the user cut off the turn this call runs in (a new request, the stop hotkey), or it's over."""
    if started is None:
        return False
    now = _turn(ctx)
    return now is None or now.id != started.id or now.interrupted


TOOLS = [WaitForScreenChange]
