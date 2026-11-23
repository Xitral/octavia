"""Watches the pointer for circles, and keeps what the latest one went round for her next turn.

The pointer is polled from a thread of its own, never hooked: a low-level mouse hook runs in the path of every mouse
move the user makes, so anything holding Python up would make their whole mouse lag. Polling costs them nothing, and
slows right down while the pointer rests.

The picture is taken the moment the loop closes, before anything else can change: what they circled is what was there
when they circled it, even if it has scrolled on by the time they say what they want. It's kept in memory only and
dropped when the circle lapses; nothing goes anywhere unless they talk to her while it's live.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

import numpy as np
from PIL import Image, ImageDraw

from octavia.brain.base import TurnPicture
from octavia.gesture.circle import CircleDetector, Loop
from octavia.tools.extensions._pictures import PICTURES
from octavia.tools.extensions.screen_capture import (
    IMAGE_MIME,
    SCREEN,
    Rect,
    Screenshot,
    ScreenUnavailable,
    attachment,
    capture_rect,
    clip_rect,
    monitor_rects,
    per_monitor_dpi,
    prepare_image,
)

if TYPE_CHECKING:
    from octavia.config import CirclingConfig

log = logging.getLogger(__name__)

POLL_S = 1 / 60
RESTING_POLL_S = 1 / 12  # the pointer hasn't moved for a while: it still wakes in time for the start of a loop
REST_AFTER_S = 1.0
PICTURE_PAD_RATIO = 0.6  # how much round what they circled the picture shows, so she can tell what it is
PICTURE_PAD_MIN_PX = 140
PATH_RGB = (255, 0, 200)  # a colour no screen is likely to have, so she can't take it for part of what's there

CIRCLED_NOTE = (
    "the user just circled part of their screen with the mouse, and that's the picture attached: the area they circled "
    "and what's around it, as it was when they did it, with their circle drawn on it as a thin magenta line that isn't "
    "really on the screen. When their words point at something on screen without naming it, however they put it, they "
    "mean what's inside that circle. If what they say is about something else, leave it alone and don't mention it. "
    "Positions in this picture work as positions in a screenshot for clicking, pointing and marks. Never read out "
    "passwords, keys, tokens, one-time codes, card numbers or other secrets you can see: just say one is there"
)


class Pointer(NamedTuple):
    x: int
    y: int
    free: bool  # no button down, and the pointer showing and free to move (a game hides it or holds it in place)


@dataclass(frozen=True, eq=False)
class Circled:
    """A circle they made: where, and the picture of it as it was."""

    rect: Rect  # what the pointer went round, in physical desktop pixels
    area: Rect  # what the picture shows
    pixels: np.ndarray  # full resolution, without their circle on it: for zooming in later
    width: int  # the picture she's shown
    height: int
    picture: TurnPicture
    taken: float  # on the screen memory's clock


class CircleMemory:
    """The latest circle, until she has been shown it or it lapses. Thread-safe. ``linger_s`` is asked each time, so
    the setting applies at once."""

    def __init__(
        self,
        linger_s: Callable[[], float],
        *,
        screen: Any = SCREEN,
        pictures: Any = PICTURES,
    ) -> None:
        self._linger_s = linger_s
        self._screen = screen
        self._pictures = pictures
        self._lock = threading.Lock()
        self._latest: Circled | None = None
        self._timer: threading.Timer | None = None

    def now(self) -> float:
        return self._screen.now()

    def keep(self, circled: Circled) -> None:
        with self._lock:
            self._latest = circled
            if self._timer is not None:
                self._timer.cancel()
            # On time even if nothing asks again: pixels of their screen don't linger past what the setting says.
            self._timer = threading.Timer(max(0.0, self._linger_s()) + 0.1, self.live)
            self._timer.daemon = True
            self._timer.start()

    def live(self) -> Circled | None:
        """The circle she'd take "this" to mean now, or None."""
        with self._lock:
            latest = self._latest
            if latest is not None and self._screen.now() - latest.taken > self._linger_s():
                self._latest = latest = None
            return latest

    def used(self, circled: Circled) -> None:
        """A turn of theirs is going to the brain with its picture. From here on it's in the conversation, so it isn't
        offered again, and positions in the picture work as a screenshot's."""
        with self._lock:
            if self._latest is circled:
                self._drop()
        self._screen.remember_shot(
            Screenshot(circled.pixels, circled.area, circled.width, circled.height, "screen", circled.taken)
        )
        self._pictures.remember(circled.picture.bytes, time.strftime("Circled %Y-%m-%d %H%M%S"))

    def give_back(self, circled: Circled) -> None:
        """That turn failed before she saw it: the circle is live again for what's left of its time, unless they've
        made a newer one."""
        with self._lock:
            if self._latest is not None:
                return
        self.keep(circled)

    def forget(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        self._latest = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


def picture_area(rect: Rect, monitors: list[Rect]) -> Rect:
    """What the picture of a circle round ``rect`` shows: it and what's around it, on its own monitor."""
    left, top, right, bottom = rect
    pad_x = max(round((right - left) * PICTURE_PAD_RATIO), PICTURE_PAD_MIN_PX)
    pad_y = max(round((bottom - top) * PICTURE_PAD_RATIO), PICTURE_PAD_MIN_PX)
    area = (left - pad_x, top - pad_y, right + pad_x, bottom + pad_y)

    def overlap(monitor: Rect) -> int:
        width, height = min(right, monitor[2]) - max(left, monitor[0]), min(bottom, monitor[3]) - max(top, monitor[1])
        return max(width, 0) * max(height, 0)

    best = max(monitors, key=overlap, default=None)
    if best is None or overlap(best) == 0:
        return area
    return clip_rect(area, best) or area


def picture_of(loop: Loop, area: Rect, pixels: np.ndarray, taken: float) -> Circled:
    """The picture she's shown of ``loop``: the pixels of ``area`` with their circle drawn on."""
    image = Image.fromarray(pixels)
    # The line stays about 2 px thick in what she sees, whatever the picture is scaled by.
    scale = max(1, max(pixels.shape[:2]) // 1200 + 1)
    line = [(x - area[0], y - area[1]) for x, y in loop.path]
    ImageDraw.Draw(image).line(line, fill=PATH_RGB, width=2 * scale)
    data, width, height = prepare_image(np.asarray(image))
    picture = TurnPicture(attachment(data)["data"], IMAGE_MIME, CIRCLED_NOTE)
    return Circled(loop.rect, area, pixels, width, height, picture, taken)


def _win32_pointer() -> Callable[[], Pointer | None]:
    """Reads the pointer on Windows. Blocking but quick. None when it can't be read (the PC is locked)."""
    import ctypes
    from ctypes import POINTER, Structure, wintypes

    class CURSORINFO(Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hCursor", ctypes.c_void_p),
            ("ptScreenPos", wintypes.POINT),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetCursorPos.argtypes = [POINTER(wintypes.POINT)]
    user32.GetCursorInfo.argtypes = [POINTER(CURSORINFO)]
    user32.GetClipCursor.argtypes = [POINTER(wintypes.RECT)]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    buttons = (0x01, 0x02, 0x04, 0x05, 0x06)  # left, right, middle and the two side buttons
    point, info, clip = wintypes.POINT(), CURSORINFO(), wintypes.RECT()

    def showing() -> bool:
        # Windows zeroes cbSize on the way out, and refuses the next call unless it's set again: without this, every
        # read after the first said the pointer was hidden, and no circle was ever seen (2026-11-01).
        info.cbSize = ctypes.sizeof(info)
        return bool(user32.GetCursorInfo(ctypes.byref(info)) and info.flags & 1)

    def read() -> Pointer | None:
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        free = showing() and not any(user32.GetAsyncKeyState(b) & 0x8000 for b in buttons)
        if free and user32.GetClipCursor(ctypes.byref(clip)):
            metric = user32.GetSystemMetrics
            free = clip.left <= metric(76) and clip.top <= metric(77) and clip.right >= metric(76) + metric(78)
            free = free and clip.bottom >= metric(77) + metric(79)
        return Pointer(point.x, point.y, bool(free))

    return read


class CircleWatch:
    """The thread that turns pointer movement into ``Circled`` in a ``CircleMemory``."""

    def __init__(
        self,
        config: CirclingConfig,
        memory: CircleMemory,
        *,
        paused: Callable[[], bool] = lambda: False,
        listening: Callable[[], bool] = lambda: False,
        read: Callable[[], Pointer | None] | None = None,
        capture: Callable[[Rect], np.ndarray] = capture_rect,
        monitors: Callable[[], list[Rect]] = monitor_rects,
        ring: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.memory = memory
        self.paused = paused  # her own screen task has the mouse
        self.listening = listening  # the user is talking to her right now
        self._read = read
        self._capture = capture
        self._monitors = monitors
        self._ring = ring
        self._clock = clock
        self._detector = CircleDetector()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None or (self._read is None and sys.platform != "win32"):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="circle-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            with per_monitor_dpi():  # the pointer in physical pixels, as everything else here is
                read = self._read or _win32_pointer()
                last: tuple[int, int] | None = None
                moved = 0.0
                while not self._stop.wait(POLL_S if self._clock() - moved < REST_AFTER_S else RESTING_POLL_S):
                    now = self._clock()
                    if not self.config.enabled or self._is_paused():
                        self._detector.reset()
                        continue
                    where = read()
                    if where is None or (where.x, where.y) == last:
                        continue
                    last, moved = (where.x, where.y), now
                    self.step(now, where)
        except Exception:
            log.exception("Watching for circles stopped")

    def step(self, now: float, where: Pointer) -> Circled | None:
        """One new position of the pointer. Returns the circle it closed, if it did."""
        if not where.free:
            self._detector.reset()
            return None
        loop = self._detector.feed(now, where.x, where.y, eager=self._is_listening())
        if loop is None:
            return None
        return self._landed(loop)

    def _is_paused(self) -> bool:
        try:
            return self.paused()
        except Exception:
            log.debug("Couldn't tell whether her work has the mouse", exc_info=True)
            return True

    def _is_listening(self) -> bool:
        try:
            return self.listening()
        except Exception:
            log.debug("Couldn't tell whether she's listening", exc_info=True)
            return False

    def _landed(self, loop: Loop) -> Circled | None:
        started = time.perf_counter()
        try:
            area = picture_area(loop.rect, self._monitors())
            pixels = self._capture(area)
            if pixels.shape[:2] != (area[3] - area[1], area[2] - area[0]):
                raise ScreenUnavailable("part of it is off the desktop")  # positions in the picture would be wrong
            circled = picture_of(loop, area, pixels, self.memory.now())
        except Exception as exc:  # a locked PC, a secure desktop: nothing she could be shown, so nothing is promised
            log.info("A circle couldn't be pictured (%s)", exc)
            return None
        self.memory.keep(circled)
        log.info(
            "Circled %s (%s, took %.2f s, pictured in %.0f ms)",
            loop.rect,
            "new" if loop.fresh else "again",
            loop.ended - loop.began,
            (time.perf_counter() - started) * 1000,
        )
        if loop.fresh and self._ring is not None and self.config.ring:
            self._ring.show(loop.rect)
        return circled
