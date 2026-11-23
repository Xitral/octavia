"""Telling a spoken "stop" apart from everything else, however it's addressed: "stop", "Octavia, stop!", "hold on",
"okay stop everything please".

It's what lets a stop be heard while she works: the conversation checks every utterance with it, and while she works
(her own steps, or a screen task beside the conversation) short utterances are heard without the wake phrase and
checked with it too (``Assistant._listen_for_stop``). Only the whole utterance counts, so "stop the music" or "wait,
what's the weather" stay requests for the brain.
"""

from __future__ import annotations

import re
from typing import Literal

from octavia.core.text import normalize_text

StopKind = Literal["stop", "all"]

# A stop that only means "not now": the screen task is paused rather than dropped, and picked up again when the user
# asks (``holds``).
_HOLD = frozenset({
    "hold on", "hang on", "hold up", "wait", "wait a second", "wait a sec", "wait a minute", "one second", "one sec",
    "pause", "pause it", "pause that", "pause this", "pause the task", "pause this task", "freeze",
})  # fmt: skip
# Halt what's running now; anything waiting in line goes on afterwards.
_STOP = _HOLD | frozenset({
    "stop", "stop it", "stop that", "stop this", "stop now", "stop working", "stop the task", "stop this task",
    "stop that task", "stop what you're doing", "stop what you are doing", "stop doing that", "stop doing this",
    "halt", "abort", "cancel", "cancel that", "cancel it", "cancel the task", "cancel this task", "quit it", "enough",
    "that's enough", "drop it", "drop that", "drop this", "drop the task", "drop this task", "drop that task",
})  # fmt: skip
# Halt what's running and drop everything waiting too.
_STOP_ALL = frozenset({
    "stop everything", "stop all", "stop all tasks", "stop all the tasks", "stop all of it", "stop all of them",
    "stop them all", "stop it all", "stop everything you're doing", "stop everything you are doing", "cancel everything",
    "cancel all", "cancel all tasks", "cancel all the tasks", "cancel all of it", "cancel them all", "abort everything",
    "clear the queue", "stop and clear the queue", "halt everything", "drop everything", "drop all of it",
})  # fmt: skip
# Typed, these take back what was asked before too (out loud they end the conversation: ``_DISMISSALS``).
_TYPED_STOP = frozenset(
    {"never mind", "nevermind", "nvm", "forget it", "forget that", "forget about it", "scratch that"}
)
# Where a texted message splits into the requests in it: "drop the task and make a fix for the stall".
_CLAUSES = re.compile(r"\s*(?:[.!?;,\n]+|\band then\b|\bthen\b|\band\b)\s*", re.IGNORECASE)
_LEADING = frozenset({"hey", "hi", "ok", "okay", "oh", "uh", "um", "no", "please", "just", "so", "and", "yo", "oi"})
_TRAILING = ("right now", "please", "now", "thanks", "thank you", "already")


def addressed(text: str, name: str = "", wake_phrase: str = "") -> str:
    """The utterance without who it's said to and the politeness around it, normalized: "Hey Octavia, stop it
    please!" -> "stop it"."""
    words = normalize_text(text).split()
    names = {w for w in normalize_text(f"{name} {wake_phrase}").split() if w}
    while words and (words[0] in _LEADING or words[0] in names):
        words.pop(0)
    changed = True
    while words and changed:
        changed = False
        if words[-1] in names:
            words.pop()
            changed = True
            continue
        joined = " ".join(words)
        for tail in _TRAILING:
            if joined.endswith(" " + tail):
                words = words[: -len(tail.split())]
                changed = True
                break
    # "stop stop stop", "hold on, hold on": said again, it's the same command.
    while len(words) >= 2 and words[-1] == words[-2]:
        words.pop()
    half = len(words) // 2
    if half and len(words) % 2 == 0 and words[:half] == words[half:]:
        words = words[:half]
    return " ".join(words)


def stop_kind(core: str) -> StopKind | None:
    """ "all" for stop everything, "stop" for stop what's running, None for anything else. ``core`` is what
    ``addressed`` returns."""
    if core in _STOP_ALL:
        return "all"
    if core in _STOP:
        return "stop"
    return None


def holds(core: str) -> bool:
    """Whether a stop asks her to hold on ("wait", "pause") rather than to drop the work. ``core`` is what
    ``addressed`` returns."""
    return core in _HOLD


def stop_command(text: str, name: str = "", wake_phrase: str = "") -> StopKind | None:
    return stop_kind(addressed(text, name, wake_phrase))


def stop_in(text: str, name: str = "", wake_phrase: str = "") -> tuple[StopKind | None, str, str]:
    """A stop that opens a typed message, which (unlike an utterance) often carries more after it: "drop the task and
    make a fix for the stall". Returns the strongest stop it opens with ("all" over "stop"; None: none), the stop as
    ``addressed`` puts it, and the rest of the message, as typed ("make a fix for the stall"; "" when the stop was all
    of it). Only the opening counts, each part of it whole: "stop the music", "find it and cancel it" and "check the
    build, then stop" are no stop. Nor is "wait" or "hold on": typed, it leads into more ("wait, add physics too")."""
    kind: StopKind | None = None
    core = ""
    text = text.strip()
    at = 0  # where the rest begins
    for part in _CLAUSES.finditer(text + "."):
        piece = text[at : part.start()]
        said = addressed(piece, name, wake_phrase)
        if said:  # not only her name ("Octavia, stop!")
            found = None if holds(said) else stop_kind(said) or ("stop" if said in _TYPED_STOP else None)
            if found is None:
                break
            if kind != "all":
                kind, core = found, said
        at = min(part.end(), len(text))
    if kind is None:
        return None, "", text
    return kind, core, text[at:].strip()
