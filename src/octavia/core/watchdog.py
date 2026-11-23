"""Notices a turn that has stopped getting anywhere, so it can be called off.

A turn is a chain of waits: the brain thinking, a tool running, a question for the user, her voice speaking the
reply. Each has its own limit somewhere (a tool's ``timeout_s``, the confirmation timeout, the brain's stall timer,
the speaker's drain backstop), but a wait that falls between them, or a limit that doesn't hold (a tool that ignores
being cancelled, a TTS stream that never ends), left her playing the thinking sound forever. The watchdog is the
backstop over all of them: when nothing in the turn has moved for ``stall_s`` it says so, and the assistant stops
the turn and tells the user what it was stuck on.

What counts as moving: anything the brain sends (``poke``), a tool starting or finishing, a question being asked or
answered, a sentence of the reply starting to play. While one of her tools runs, the wait is the tool's own limit
plus ``stall_s`` (the tool is given every chance to time out by itself first); while a question waits for the user,
it's the question's own limit (``question_limit``, at most ``QUESTION_BACKSTOP_S``) plus ``stall_s``. The brain also
calls ``poke`` through its ``heartbeat`` for whatever it hears that never becomes an event (thinking, a tool's
arguments streaming in).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from octavia.core.events import (
    Event,
    EventBus,
    PermissionRequested,
    PermissionResolved,
    SpeechSegmentStarted,
    ToolCallFinished,
    ToolCallStarted,
)

log = logging.getLogger(__name__)

QUESTION_BACKSTOP_S = 1800.0  # a question that's been waiting this long is stuck too


class TurnWatchdog:
    """One turn's watchdog: ``start`` it with the turn, await ``expired`` beside it, ``stop`` it in the turn's
    ``finally``. ``tool_limit`` gives a tool's own time limit by name (None: not one of hers, e.g. a tool the brain
    runs itself)."""

    def __init__(
        self,
        events: EventBus,
        stall_s: float,
        tool_limit: Callable[[str], float | None] = lambda _: None,
        question_limit: Callable[[], float] = lambda: QUESTION_BACKSTOP_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        self.stall_s = stall_s
        self.tool_limit = tool_limit
        self.question_limit = question_limit
        self.clock = clock
        self._last = 0.0  # when the turn last moved
        self._tools: dict[str, tuple[str, float]] = {}  # call id -> (tool name, when its own limit runs out)
        self._questions: list[tuple[str, float]] = []  # (tool name, when its limit runs out) for each question waiting
        self._running = False
        self._unsubscribe: Callable[[], None] | None = None
        self._moved = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self.stall_s > 0

    def start(self) -> None:
        self._last = self.clock()
        self._tools.clear()
        self._questions.clear()
        self._running = True
        self._unsubscribe = self.events.subscribe(
            self._on_event,
            ToolCallStarted,
            ToolCallFinished,
            PermissionRequested,
            PermissionResolved,
            SpeechSegmentStarted,
        )

    def stop(self) -> None:
        self._running = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def poke(self) -> None:
        """The turn moved (the brain sent something)."""
        self._last = self.clock()
        self._moved.set()

    def _on_event(self, event: Event) -> None:
        if not self._running:
            return
        now = self.clock()
        if isinstance(event, ToolCallStarted):
            limit = self.tool_limit(event.name)
            self._tools[event.call_id] = (event.name, now + (limit or 0.0))
        elif isinstance(event, ToolCallFinished):
            self._tools.pop(event.call_id, None)
        elif isinstance(event, PermissionRequested):
            self._questions.append((event.tool, now + min(self.question_limit(), QUESTION_BACKSTOP_S)))
        elif isinstance(event, PermissionResolved):
            for i, (tool, _) in enumerate(self._questions):
                if tool == event.tool:
                    del self._questions[i]
                    break
        self.poke()

    def deadline(self) -> float:
        """When the turn counts as stuck if nothing moves before then."""
        deadline = self._last + self.stall_s
        for _, until in self._tools.values():
            deadline = max(deadline, until + self.stall_s)
        for _, until in self._questions:
            deadline = max(deadline, until + self.stall_s)
        return deadline

    def stuck_on(self) -> str | None:
        """The tool the turn was waiting on when it stalled (the latest one still running), or None: the brain, or
        her voice."""
        if self._questions:
            return self._questions[-1][0]
        if self._tools:
            return next(reversed(self._tools.values()))[0]
        return None

    async def expired(self) -> float:
        """Return (with how long nothing had moved) once the turn has gone ``stall_s`` without moving; never, while
        the watchdog is off."""
        if not self.enabled:
            await asyncio.Event().wait()
        while True:
            self._moved.clear()
            left = self.deadline() - self.clock()
            if left <= 0:
                if not self._running:
                    await asyncio.Event().wait()  # stopped: never fires
                return self.clock() - self._last
            try:
                await asyncio.wait_for(self._moved.wait(), left)
            except TimeoutError:
                pass
