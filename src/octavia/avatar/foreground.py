"""The window in front, for the avatar: she glances at the window the user switches to, and a big new window popping
up by itself (nobody has touched the keyboard or mouse for a few seconds) makes her jump.

``AvatarBridge`` calls ``ForegroundWatch.look`` a couple of times a second while she's on screen: a few Win32 calls
(no hooks), nothing leaves the PC and no Claude usage. Only on Windows; elsewhere ``read_front`` finds nothing.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

Rect = tuple[int, int, int, int]  # left, top, right, bottom: physical desktop pixels

POPUP_IDLE_S = 3.0  # a new window in front with no key pressed or mouse moved for this long came up by itself
POPUP_SHARE = 0.35  # ...and it's big: at least this much of its monitor
_SEEN_MAX = 512  # windows remembered as having been in front (a handle can be reused, so it's only a hint)
# The shell's own windows: the taskbar and the desktop. Clicking on them isn't switching to a window.
SHELL_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"})


@dataclass(frozen=True)
class Front:
    """The window in front."""

    hwnd: int
    pid: int  # the process it belongs to
    frame: Rect  # its visible frame
    monitor: Rect  # the monitor it's on (most of it)
    idle_s: float  # since the user last pressed a key, clicked or moved the mouse

    @property
    def centre(self) -> tuple[int, int]:
        left, top, right, bottom = self.frame
        return (left + right) // 2, (top + bottom) // 2

    @property
    def share(self) -> float:
        """How much of its monitor it covers, 0 to 1."""
        left, top, right, bottom = self.frame
        ml, mt, mr, mb = self.monitor
        width = max(0, min(right, mr) - max(left, ml))
        height = max(0, min(bottom, mb) - max(top, mt))
        area = (mr - ml) * (mb - mt)
        return width * height / area if area > 0 else 0.0


@dataclass(frozen=True)
class Switch:
    """Another window came to the front."""

    front: Front
    new: bool  # it hasn't been in front before (as far as the watch has seen)

    @property
    def popped_up(self) -> bool:
        """A big new window came to the front with nobody touching anything: it popped up by itself."""
        return self.new and self.front.idle_s >= POPUP_IDLE_S and self.front.share >= POPUP_SHARE


class ForegroundWatch:
    """Notices the window in front changing, one ``look`` at a time. ``read`` gives the window in front (or None:
    none, minimised, the taskbar or desktop), ``read_front`` on this PC."""

    def __init__(self, read: Callable[[], Front | None]) -> None:
        self._read = read
        self._last: int | None = None  # the window in front at the last look, None until the first
        self.front: Front | None = None  # what the last look found in front (None: nothing, or the taskbar)
        self._seen: dict[int, None] = {}  # windows that have been in front, oldest first

    def look(self) -> Switch | None:
        """One look: a Switch when another window is in front than at the last look. The first look (after
        ``reset``) only learns what's there. A look that finds nothing in front keeps the last window, so going to
        the taskbar and back isn't a switch."""
        front = self.front = self._read()
        if front is None or front.hwnd == self._last:
            return None
        first = self._last is None
        self._last = front.hwnd
        new = front.hwnd not in self._seen
        self._seen.pop(front.hwnd, None)
        self._seen[front.hwnd] = None  # the newest
        while len(self._seen) > _SEEN_MAX:
            del self._seen[next(iter(self._seen))]
        return None if first else Switch(front, new)

    def reset(self) -> None:
        """She's gone: the next look starts afresh, as what's in front then isn't a switch she saw."""
        self._last = None
        self.front = None


def read_front() -> Front | None:
    """The window in front on this PC, or None: none, minimised, the taskbar or desktop, or not Windows."""
    if sys.platform != "win32":
        return None
    from octavia.platform import windows as win
    from octavia.tools.extensions.screen_capture import foreground_frame

    found = foreground_frame()
    if found is None:
        return None
    hwnd, frame, monitor = found
    if win.window_class(hwnd) in SHELL_CLASSES:
        return None
    return Front(hwnd=hwnd, pid=win.window_pid(hwnd), frame=frame, monitor=monitor, idle_s=win.idle_seconds())
