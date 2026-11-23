"""Did her click or typing land? A look at the screen around it before and after, for what UI Automation can't see.

A remote screen streamed into a web page (the cloud workspace beside a course's instructions), a canvas or a video is
drawn, not described: UI Automation sees one unnamed element, and the focus stays on the page whatever happens inside
it. So for those the screen itself is the check. ``Landing.before`` copies the region twice a moment apart (what
changes between those two changes on its own: a blinking caret, a clock, a video), the input goes in, and
``Landing.after`` polls the region for a while (the remote end answers over the network) for pixels that differ from
both copies. A typed character draws more than a caret: typing needs a change wider than a caret's line.

``Delivery`` keeps, per window, which way of getting input in worked last there (a plain click or a slow one; key
presses by scan code or a paste), so the next input to that window starts with it.

Everything here is a check, never proof, and never raises: a region that can't be read is "can't tell" (None), which
the tools say as such rather than as landed.

Not loaded as a tool module (the name starts with ``_``); ``computer_control`` uses it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from octavia.tools.extensions.screen_capture import Rect

log = logging.getLogger(__name__)

PIXEL_DIFF = 32  # a channel this much brighter or darker is a change, not a streamed picture's compression shimmer
CLICK_PX = 12  # a click that landed changes at least this many pixels (a caret appearing, a button pressed in)
TYPED_PX = 20  # typing that landed changes at least this many pixels...
CARET_PX = 4  # ...across more than this width: a caret blinking on or moving is a thin upright line
APART_S = 0.08  # between the two copies before the input
WAITS_S = (0.2, 0.25, 0.4, 0.65)  # polls after the input: 1.5 s in all for the remote end to draw it
GRAB_S = 0.5  # the most one copy may take
MAX_SIDE = 1600  # the region looked at, at most: around the spot, never the whole of a big screen
AROUND = (450, 300)  # half the width and height looked at around a spot when the element's extent isn't known
KEPT_WINDOWS = 64

Grab = Callable[[Rect], np.ndarray]


def region(x: int, y: int, box: Rect | None = None, around: tuple[int, int] = AROUND) -> Rect:
    """The region to watch for input at (x, y): ``box`` (the element it went into) when known, else ``around`` it,
    and never more than ``MAX_SIDE`` a side, kept around the spot."""
    if box is None:
        box = (x - around[0], y - around[1], x + around[0], y + around[1])
    left, top, right, bottom = box
    half = MAX_SIDE // 2
    left, right = max(left, x - half), min(right, x + half)
    top, bottom = max(top, y - half), min(bottom, y + half)
    return left, top, max(right, left + 1), max(bottom, top + 1)


@dataclass(frozen=True)
class Frames:
    """Two copies of a region from right before her input."""

    rect: Rect
    first: np.ndarray
    second: np.ndarray


def changed(frames: Frames, after: np.ndarray) -> np.ndarray:
    """Which pixels of ``after`` differ from both copies before: what the input did, not what changes on its own."""
    if after.shape != frames.first.shape or after.shape != frames.second.shape:
        return np.ones(after.shape[:2], bool)  # the region moved or resized: that's a change
    a = after.astype(np.int16)
    return (np.abs(a - frames.first).max(axis=2) > PIXEL_DIFF) & (np.abs(a - frames.second).max(axis=2) > PIXEL_DIFF)


def shows_input(mask: np.ndarray, typing: bool) -> bool:
    """Whether the changed pixels in ``mask`` are input landing: a click's few, or typing's, wider than a caret."""
    count = int(mask.sum())
    if not typing:
        return count >= CLICK_PX
    if count < TYPED_PX:
        return False
    columns = np.flatnonzero(mask.any(axis=0))
    return int(columns[-1] - columns[0]) + 1 > CARET_PX


class Landing:
    """The before-and-after look. ``grab`` reads a region of the screen (None: the real screen, on Windows only)."""

    def __init__(
        self, grab: Grab | None = None, sleep: Callable[[float], Awaitable[Any]] | None = None, real: bool = True
    ) -> None:
        if grab is None and real and sys.platform == "win32":
            from octavia.tools.extensions.screen_capture import capture_rect

            grab = capture_rect
        self.grab = grab
        self.sleep = sleep or asyncio.sleep

    async def _copy(self, rect: Rect) -> np.ndarray | None:
        if self.grab is None:
            return None
        try:
            pixels = await asyncio.wait_for(asyncio.to_thread(self.grab, rect), GRAB_S)
        except Exception:
            log.debug("Copying the screen around her input failed", exc_info=True)
            return None
        return np.asarray(pixels, np.int16)

    async def before(self, rect: Rect) -> Frames | None:
        """Two copies of ``rect``, ``APART_S`` apart; None when it can't be read (then nothing can be told)."""
        first = await self._copy(rect)
        if first is None:
            return None
        await self.sleep(APART_S)
        second = await self._copy(rect)
        return None if second is None else Frames(rect, first, second)

    async def after(self, frames: Frames | None, typing: bool, waits: Sequence[float] = WAITS_S) -> bool | None:
        """Whether the region shows her input within the ``waits``: True as soon as it does, False when it never
        did, None when it can't be told."""
        if frames is None:
            return None
        seen = False
        for wait in waits:
            await self.sleep(wait)
            after = await self._copy(frames.rect)
            if after is None:
                continue
            seen = True
            if shows_input(changed(frames, after), typing):
                return True
        return False if seen else None


Window = tuple[int, int]  # (hwnd, pid)


class Delivery:
    """Per window, the way of getting input in that worked there last, to try first next time. Kept for the last
    ``KEPT_WINDOWS`` windows, in memory only (a window's handle means nothing after a restart)."""

    def __init__(self, kept: int = KEPT_WINDOWS) -> None:
        self.kept = kept
        self._worked: OrderedDict[tuple[Window, str], str] = OrderedDict()

    def order(self, window: Window, kind: str, ways: Sequence[str]) -> tuple[str, ...]:
        """``ways`` (the usual order) with the one that worked last in ``window`` for ``kind`` ("click", "type")
        first."""
        first = self._worked.get((window, kind))
        if first not in ways:
            return tuple(ways)
        return (first, *(way for way in ways if way != first))

    def worked(self, window: Window, kind: str, way: str) -> None:
        key = (window, kind)
        self._worked.pop(key, None)
        self._worked[key] = way
        while len(self._worked) > self.kept:
            self._worked.popitem(last=False)


DELIVERY = Delivery()
