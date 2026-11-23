"""When a screen task she was given ends done, she takes it up in a turn of her own instead of a line said for her,
so a job of several steps goes on to its next step without the user saying "continue" (the 2026-11-05 audit: 27
nudges in 972 turns, all during screen work; on Assignment 4, five in 45 minutes, each after a step she'd finished
and named the next of). Whether there is a next step is hers to judge from what the user asked; a cap on how many
steps go on by themselves keeps a misjudged job from running on unwatched."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

MAX_STEPS = 4  # steps that go on by themselves within WINDOW_S; past that, the end is only announced
WINDOW_S = 30 * 60.0

TURN = "(On my own) A screen task of mine just ended."
GO_ON = (
    "If it was one step of a larger job the user gave you and the next step is clear from what they asked, go "
    'straight on with it now, as you would if they had said "continue": say in a sentence what got done and what '
    "you're starting, then start it. If the job is done, or the next step needs the user (a choice only they can "
    "make, a sign-in, a submit), say the result in a sentence or two and stop."
)


class GoingOn:
    """``ScreenTaskManager.go_on``: hands a finished task's end to her (``tell``: ``Assistant.tell_in_own_words``)
    and says it took it, or declines past the cap so the end is announced as before."""

    def __init__(self, tell: Callable[..., Any], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._tell = tell
        self._clock = clock
        self._went: deque[float] = deque()

    def __call__(self, task: Any, line: str, note: str, full: str) -> bool:
        now = self._clock()
        while self._went and now - self._went[0] > WINDOW_S:
            self._went.popleft()
        if len(self._went) >= MAX_STEPS:
            return False
        self._went.append(now)
        self._tell(
            TURN,
            f"{note}\n{GO_ON}",
            line,
            note,
            kind="asked",  # the user gave her the job: its end is never held back
            source="screen task",
            facts=note,
        )
        return True
