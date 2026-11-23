"""Catching the moments her event loop is blocked, and what blocked it (docs/octavia-2.0.md, round 11, D-020).

Her voice ran dry mid-sentence in 40 of 67 measured turns on 2026-11-02, while Fish delivered her audio at about twice
real time: replaying 45 of her real replies through the same speech pipeline on a silent test speaker ran dry once.
What differs live is everything else sharing the event loop: when something on it runs for a few hundred
milliseconds without yielding, Fish's audio sits unread in its socket and the speaker's queue empties.

A task on the loop beats every ``TICK_S``; the beat that comes more than ``STALL_S`` late counts and logs a stall
(at most ``LOGGED_PER_MINUTE`` lines a minute at INFO; over ``MAX_STALL_S`` the machine slept). Meanwhile a daemon
thread, seeing the beat late, looks at the loop thread's stack (``sys._current_frames``) to say where it was stuck:
the first look and the last, so a run of short calls shows as two places. ``snapshot`` gives running totals, so a speech stream can say how much of
its playing time the loop was stuck (``SpeakingFinished.stalled_s``).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from types import FrameType

log = logging.getLogger(__name__)

TICK_S = 0.02  # how often the loop beats
STALL_S = 0.15  # a beat this late is a stall: long enough to empty the speaker's queue between Fish's chunks
MAX_STALL_S = 10.0  # later than this the machine slept: not a stall
LOGGED_PER_MINUTE = 6  # stall lines at INFO; more go to DEBUG
_FRAMES_SHOWN = 3  # of her own code, innermost first

current: LoopWatch | None = None  # the running app's, for whoever wants its totals (speech)


def where(frame: FrameType | None) -> str:
    """The innermost frame and the innermost few of her own code under it, as "file:line function"."""
    shown: list[str] = []
    innermost = ""
    while frame is not None:
        code = frame.f_code
        spot = f"{code.co_filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{frame.f_lineno} {code.co_name}"
        if not innermost:
            innermost = spot
        module = frame.f_globals.get("__name__") or ""
        if (module == "octavia" or module.startswith("octavia.")) and len(shown) < _FRAMES_SHOWN:
            shown.append(f"{module}:{frame.f_lineno} {code.co_name}")
        frame = frame.f_back
    if not shown:
        return innermost or "unknown"
    return " <- ".join(shown) + f" (in {innermost})"


class LoopWatch:
    """See the module docstring. ``start`` on the loop it watches; ``stop`` when the app closes.

    Stalls are counted on the loop itself, by the beat that comes late, so a total read on the loop (``snapshot``)
    never trails behind: one that's over but not yet beaten for counts too. The thread only says where the loop was.
    """

    def __init__(self, stall_s: float = STALL_S, clock: Callable[[], float] = time.perf_counter) -> None:
        self.stall_s = stall_s
        self._clock = clock
        self.stalls = 0
        self.stalled_s = 0.0  # all stalls' time together
        self.longest_s = 0.0
        self._beat = clock()
        self._loop_thread: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = threading.Event()
        # Where the loop thread was in the stall since the beat at [0]: (beat, first look, latest look), the thread's.
        self._seen: tuple[float, str, str] | None = None
        self._logged: deque[float] = deque()  # when recent stalls were logged at INFO

    def start(self) -> None:
        global current
        if self._task is not None:
            return
        self._loop_thread = threading.get_ident()
        self._beat = self._clock()
        self._task = asyncio.get_running_loop().create_task(self._beats(), name="loop-watch")
        threading.Thread(target=self._watch, name="loop-watch", daemon=True).start()
        current = self

    def stop(self) -> None:
        global current
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
        if current is self:
            current = None

    def snapshot(self) -> tuple[int, float]:
        """(stalls, seconds stalled) so far; read on the loop, a stall that just ended counts though not yet beaten."""
        late = self._late(self._clock())
        if late is None:
            return self.stalls, self.stalled_s
        return self.stalls + 1, self.stalled_s + late

    def _late(self, now: float) -> float | None:
        late = now - self._beat - TICK_S
        return late if self.stall_s < late <= MAX_STALL_S else None

    async def _beats(self) -> None:
        while True:
            self.beat()
            await asyncio.sleep(TICK_S)

    def beat(self) -> None:
        """On the loop: count the stall this beat ends, if it ends one (a test calls it directly)."""
        now = self._clock()
        late, since = self._late(now), self._beat
        self._beat = now
        if late is None:
            return
        self.stalls += 1
        self.stalled_s += late
        self.longest_s = max(self.longest_s, late)
        seen = self._seen
        place = "an unknown spot"
        if seen is not None and seen[0] == since:
            place = seen[1] if seen[1] == seen[2] else f"{seen[1]}, later {seen[2]}"
        while self._logged and now - self._logged[0] > 60:
            self._logged.popleft()
        level = logging.INFO if len(self._logged) < LOGGED_PER_MINUTE else logging.DEBUG
        if level == logging.INFO:
            self._logged.append(now)
        log.log(level, "Event loop stuck %.0f ms in %s", late * 1000, place)

    def _watch(self) -> None:
        while not self._stop.wait(TICK_S):
            self.look()

    def look(self) -> None:
        """From the watching thread: where the loop is, while it's stuck (a test calls it directly)."""
        beat = self._beat
        if self._late(self._clock()) is None:
            return
        frame = sys._current_frames().get(self._loop_thread) if self._loop_thread is not None else None
        spot = where(frame)
        seen = self._seen
        self._seen = (beat, seen[1], spot) if seen is not None and seen[0] == beat else (beat, spot, spot)
