"""Screen watch: a watcher that costs nothing until the screen changes a lot, then wakes her with one small picture,
e.g. when a game shows a disaster warning banner at the top of the screen.

It compares tiny copies of the screen on this computer every second or so; nothing goes to Claude until a big
change. A change counts only where the picture was steady, changed suddenly and then stayed so (per patch of the
tiny copy): a camera sweeping around or water moving keeps changing and doesn't count, a banner appearing over the
sky does. It watches the window that was in front when it started and pauses while another is in front, so the user
switching windows isn't a change. It stops by itself after its time or its number of wakes.

Waking is a turn of her own, as a scheduled job runs (``Assistant.run_in_background``, ``ctx.extras["background"]``,
voice mode only), so it waits for a break in the conversation and anything risky is asked. It doesn't wake her
during the user's own turns: she's with them then. The picture it took waits in memory for ``see_screen_change``
for a few minutes, then it's dropped. Starting the watch is CONFIRM: the user agrees then to the pictures it sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field

from octavia.core.events import EventBus, ScreenshotTaken
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._game_vision import small_frame
from octavia.tools.extensions.screen_capture import Capture, _gdi, attachment, capture_screen, prepare_image

log = logging.getLogger(__name__)

Region = Literal["screen", "top", "middle", "bottom"]
REGIONS: dict[str, tuple[float, float]] = {
    "screen": (0, 1),
    "top": (0, 0.25),
    "middle": (0.25, 0.75),
    "bottom": (0.75, 1),
}
_REGION_WORDS = {
    "screen": "the screen",
    "top": "the top of the screen",
    "middle": "the middle of the screen",
    "bottom": "the bottom of the screen",
}
THUMB_WIDTH = 96  # the tiny copies compared: a patch is 20 pixels of a 1920-pixel-wide screen
PATCH_DIFF = 24.0  # a patch changed when a colour channel moved by this much (of 255)
COOLDOWN_S = 30.0  # between two wakes
ALERT_WIDTH = 640  # the picture she's woken with: small, but a banner's words stay readable
ALERT_TTL_S = 300.0
MAX_FAILURES = 5  # captures in a row that fail: the screen can't be read (locked PC), so the watch ends
DEFAULT_INSTRUCTION = "Something on the screen changed a lot. See what it is and tell me in a sentence."

Wake = Callable[[str, str], Any]  # (text, note) -> the background turn (a future), as Assistant.run_in_background


def thumbnail(pixels: np.ndarray, region: str) -> np.ndarray:
    """A tiny copy of ``region`` of the picture, to compare."""
    top, bottom = REGIONS[region]
    height = pixels.shape[0]
    first = int(height * top)
    part = pixels[first : max(int(height * bottom), first + 1)]
    return small_frame(part, THUMB_WIDTH).astype(np.float32)


def settled_change(looks: deque[np.ndarray] | list[np.ndarray]) -> float:
    """The share of patches that were steady, changed at once, and stayed so, over four looks in a row."""
    a, b, c, d = looks

    def moved(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return np.abs(x - y).max(axis=2) > PATCH_DIFF

    return float((~moved(a, b) & moved(b, c) & ~moved(c, d)).mean())


def foreground_window() -> int:
    if sys.platform != "win32":
        return 0
    return int(_gdi().user32.GetForegroundWindow() or 0)


@dataclass(frozen=True)
class WatchSettings:
    region: str = "screen"
    change: float = 0.1
    every_s: float = 1.0
    minutes: float = 10.0
    max_wakes: int = 3
    instruction: str = DEFAULT_INSTRUCTION

    @property
    def where(self) -> str:
        return _REGION_WORDS[self.region]


@dataclass(frozen=True)
class Alert:
    """The picture taken when the watch woke her."""

    image: dict[str, str]
    width: int
    height: int
    share: float
    where: str
    taken: float


class ScreenWatcher:
    """One watch at a time, as a task on the event loop. ``start`` replaces a running one; ``stop`` ends it."""

    def __init__(
        self,
        capture: Capture | None = None,
        front: Callable[[], int] = foreground_window,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capture = capture or capture_screen
        self.front = front
        self.sleep = sleep
        self.clock = clock
        self.settings: WatchSettings | None = None
        self.wakes = 0
        self._task: asyncio.Task[None] | None = None
        self._alert: Alert | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, settings: WatchSettings, wake: Wake, busy: Callable[[], bool], events: EventBus) -> None:
        self.stop()
        self.settings, self.wakes = settings, 0
        self._task = asyncio.get_running_loop().create_task(self._watch(settings, wake, busy, events), name="watch")

    def stop(self) -> bool:
        """End the watch; whether one was running."""
        was = self.running
        if self._task is not None:
            self._task.cancel()
            self._task = None
        return was

    async def wait(self) -> None:
        """Until the watch ends by itself (for tests)."""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def alert(self) -> Alert | None:
        if self._alert is not None and self.clock() - self._alert.taken > ALERT_TTL_S:
            self._alert = None
        return self._alert

    async def _watch(self, settings: WatchSettings, wake: Wake, busy: Callable[[], bool], events: EventBus) -> None:
        end = self.clock() + settings.minutes * 60
        window = self.front()
        looks: deque[np.ndarray] = deque(maxlen=4)
        failures = 0
        quiet_until = 0.0
        pending: Any = None
        while self.clock() < end and self.wakes < settings.max_wakes:
            if self.front() != window:
                looks.clear()  # another window is in front: what it shows isn't a change
            else:
                try:
                    pixels, *_ = await asyncio.to_thread(self.capture, "window")
                except Exception:
                    failures += 1
                    log.debug("The screen watch couldn't take a look", exc_info=True)
                    if failures >= MAX_FAILURES:
                        log.warning("The screen watch stopped: the screen can't be read")
                        return
                else:
                    failures = 0
                    look = thumbnail(pixels, settings.region)
                    if looks and looks[-1].shape != look.shape:
                        looks.clear()  # the window was resized
                    looks.append(look)
                    if len(looks) == 4 and (share := settled_change(looks)) >= settings.change:
                        waiting = pending is not None and not pending.done()
                        if self.clock() >= quiet_until and not waiting and not busy():
                            pending = await self._wake(pixels, share, settings, wake, events)
                            quiet_until = self.clock() + COOLDOWN_S
                            looks.clear()
                            looks.append(look)
            await self.sleep(settings.every_s)

    async def _wake(
        self, pixels: np.ndarray, share: float, settings: WatchSettings, wake: Wake, events: EventBus
    ) -> Any:
        small = small_frame(pixels, ALERT_WIDTH)
        data, width, height = await asyncio.to_thread(prepare_image, small)
        image = attachment(data)
        self._alert = Alert(image, width, height, share, settings.where, self.clock())
        self.wakes += 1
        events.emit(ScreenshotTaken(area="window"))
        note = (
            f"your screen watch (which the user started) woke you on its own: {share:.0%} of {settings.where} "
            "suddenly changed and stayed changed. The user didn't just say this. Call see_screen_change to see the "
            f"small picture it took, then do what the instruction asks, briefly (wake {self.wakes} of at most "
            f"{settings.max_wakes})"
        )
        log.info("The screen watch saw %.0f%% of %s change: waking", share * 100, settings.where)
        try:
            turn = wake(settings.instruction, note)
        except Exception:
            log.warning("The screen watch couldn't wake the assistant", exc_info=True)
            return None
        if isinstance(turn, asyncio.Future):
            turn.add_done_callback(_log_failure)
        return turn


def _log_failure(turn: asyncio.Future[Any]) -> None:
    if not turn.cancelled() and (exc := turn.exception()) is not None:
        log.warning("The screen watch's wake-up failed: %s", exc)


WATCHER = ScreenWatcher()


# --------------------------------------------------------------------------- tools


class StartScreenWatchArgs(ToolArgs):
    region: Region = Field(
        default="screen",
        description="'top' for a banner or warning across the top (a disaster in a game), 'bottom' for chat.",
    )
    change: float = Field(
        default=0.1,
        ge=0.03,
        le=0.9,
        description="Share of the region that must change at once and stay changed (0.1 = a tenth).",
    )
    every_s: float = Field(default=1.0, ge=0.5, le=5.0, description="Seconds between local looks.")
    minutes: float = Field(default=10.0, ge=1, le=60, description="How long to watch.")
    max_wakes: int = Field(default=3, ge=1, le=10, description="Stop after this many wakes.")
    instruction: str = Field(
        default=DEFAULT_INSTRUCTION,
        min_length=1,
        max_length=300,
        description="What to do when woken, as a request to yourself, e.g. 'Tell me what disaster is coming'.",
    )


class _WatchTool(Tool):
    category = "system"

    def __init__(self, watcher: ScreenWatcher | None = None) -> None:
        self.watcher = watcher if watcher is not None else WATCHER


class StartScreenWatch(_WatchTool):
    name = "start_screen_watch"
    description = (
        "Watch the screen locally, at no cost, and wake yourself with one small picture only when part of it "
        "suddenly changes a lot and stays changed (a warning banner in a game, a pop-up, a loading screen "
        "finishing). Watches the window in front when it starts; wakes you only between the user's turns. When "
        "woken, call see_screen_change. Starting it again replaces the watch. To see while moving in a game, use "
        "game_hold's watch. Not for waiting in the middle of a task: use wait_for_screen_change and carry on in "
        "the same turn."
    )
    Args = StartScreenWatchArgs
    risk = RiskLevel.CONFIRM

    async def describe_call(self, args: StartScreenWatchArgs, ctx: ToolContext) -> str:
        times = "once" if args.max_wakes == 1 else f"up to {args.max_wakes} times"
        return (
            f"watch {_REGION_WORDS[args.region]} for {args.minutes:g} minutes, and when it changes a lot ({times}) "
            "send a small picture of it to Claude"
        )

    async def precheck(self, args: StartScreenWatchArgs, ctx: ToolContext) -> str | None:
        if self.watcher.capture is capture_screen and sys.platform != "win32":
            return "Watching the screen only works on Windows."
        if not callable(ctx.extras.get("background")):
            return "Watching the screen only works while the voice assistant is running."
        return None

    async def run(self, args: StartScreenWatchArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        background = ctx.extras["background"]
        turn = ctx.extras.get("turn")

        def wake(text: str, note: str) -> Any:
            return background(text, note)

        def busy() -> bool:
            return callable(turn) and turn() is not None

        settings = WatchSettings(
            args.region, args.change, args.every_s, args.minutes, args.max_wakes, args.instruction.strip()
        )
        replaced = self.watcher.running
        self.watcher.start(settings, wake, busy, ctx.events)
        times = "once" if args.max_wakes == 1 else f"up to {args.max_wakes} times"
        return ToolResult.success(
            f"{'Replaced the earlier watch: now w' if replaced else 'W'}atching {settings.where} for "
            f"{args.minutes:g} minutes. I'll be woken {times} when it changes a lot, and only between the user's "
            "turns.",
            data={"region": args.region, "minutes": args.minutes, "max_wakes": args.max_wakes},
        )


class StopScreenWatch(_WatchTool):
    name = "stop_screen_watch"
    description = "Stop watching the screen (the watch start_screen_watch began)."
    risk = RiskLevel.SAFE

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        if self.watcher.stop():
            return ToolResult.success("Stopped watching the screen.")
        return ToolResult.success("I wasn't watching the screen.")


class SeeScreenChange(_WatchTool):
    name = "see_screen_change"
    description = (
        "See the small picture your screen watch took when it woke you (kept for 5 minutes). Only after "
        "start_screen_watch woke you; for a fresh look, use look_at_screen."
    )
    risk = RiskLevel.SAFE  # the picture was taken under the watch the user agreed to; this only shows it

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        alert = self.watcher.alert()
        if alert is None:
            return ToolResult.error("The screen watch has no recent picture.")
        age = max(0, round(self.watcher.clock() - alert.taken))
        return ToolResult.success(
            f"The picture the screen watch took {age} seconds ago, when {alert.share:.0%} of {alert.where} changed "
            f"({alert.width}x{alert.height} pixels). Never read out passwords, codes or other secrets in it.",
            data={"image": alert.image, "width": alert.width, "height": alert.height},
        )


TOOLS = [StartScreenWatch, StopScreenWatch, SeeScreenChange]
