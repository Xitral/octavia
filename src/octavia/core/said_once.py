"""She says a thing once in a turn, whatever the model writes.

When a call only records something she answers first and calls after (``personality/system.md``), so the model is
asked for more words once the call is back. It often writes its answer again, whatever the prompt says, and the user
heard it twice, the second time sometimes with a stale opening ("Yes, I can quiz you..." in the middle of a quiz) that
sounded like she had lost the thread. So what she writes after a step, when she had already answered before it, waits
until it's clear whether it says anything new. Nothing about it depends on the subject: it only weighs how much of
the new text's wording she used just before that step.

Text after "Let me look." is never held (nothing has been answered yet), and text after a failed call always goes
out: she must be able to say that it didn't work, even in the same words as her answer. A failed call she tried again
at once and that worked the second time is no failure to tell: what she writes after the retry is held like any other
(she asked "How many colors can NT still use?" twice around a retried panel update).

After an answer and nothing but changes to her own panels that worked (update_visual, show_visual...), nothing more
is said at all: those calls only confirm what's now on the panel. 2026-11-21 she asked "which do you try first?",
changed the panel, and then went on with a second paragraph (the answer again in new words, an aside about an earlier
turn) that the wording test let through, so it sounded like she asked and then wandered off.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import replace

from octavia.brain.base import BrainEvent, ResponseDone, TextDelta, ToolUseStarted

log = logging.getLogger(__name__)

_ANSWERED_WORDS = (
    10  # what she said before the step had this many words that carry meaning: an answer, not "Let me look."
)
_RETOLD = 0.55  # this share of the new text's meaningful words she had already used: she's saying it again
_CLEARLY_NEW = 0.35  # after two whole sentences, under this share: it's news, and the rest needn't wait
_HOLD_WORDS = 60  # a retelling is shorter than this: decide by here, so a long report isn't kept waiting
_MIN_WORDS = 5  # too few meaningful words to call it a retelling ("Filling in the security group now."): it goes out

# Calls that only change her own panels and confirm it: once she has answered, nothing after them needs saying.
# Not explain (she narrates it after) or show_image (she checks the picture and says what it is).
QUIET_AFTER = frozenset({"update_visual", "show_visual", "close_visual", "pin_visual", "bring_back_visual"})
_NEUTRAL = frozenset({"ToolSearch"})  # loading a tool's schema is no step of its own

_WORD = re.compile(r"[a-z0-9][a-z0-9.']*[a-z0-9]|[a-z0-9]")
_SENTENCE_END = re.compile(r"[.!?…][\"”')\]]*(?:\s|$)")
_COMMON = frozenset(
    "a about after again all also am an and any are as at be because been but by can could did do does don't for from "
    "get go going got had has have he her here him his how i i'd i'll i'm i've if in into is it it's its just know let "
    "like me my no not now of on one or our out over she so some than that that's the their them then there these "
    "they this those to too up us very was we well were what when where which who why will with would yes yeah you "
    "you'd you'll you're you've your".split()
)


def meaning_words(text: str) -> set[str]:
    """The words of ``text`` that carry its meaning, by their first five letters ("records" and "recorded")."""
    return {word[:5] for word in _WORD.findall(text.lower()) if word not in _COMMON}


def share_used(text: str, earlier: str) -> float:
    """How much of the wording of ``text`` is already in ``earlier``, from 0 to 1."""
    words = meaning_words(text)
    return len(words & meaning_words(earlier)) / len(words) if words else 0.0


def retold(text: str, earlier: str) -> bool:
    """Whether ``text``, whole, only says again what ``earlier`` said."""
    return len(meaning_words(text)) >= _MIN_WORDS and share_used(text, earlier) >= _RETOLD


def _settled(held: str, earlier: str) -> bool:
    """Whether ``held``, still being written, can be decided already: clearly news, or so long it mustn't wait."""
    if len(held.split()) >= _HOLD_WORDS or "{{" in held:  # {{step}} beats a panel plays on: never a retelling
        return True
    return len(_SENTENCE_END.findall(held)) >= 2 and share_used(held, earlier) < _CLEARLY_NEW


async def without_retelling(
    events: AsyncIterable[BrainEvent], failures: Callable[[], int]
) -> AsyncIterator[BrainEvent]:
    """The brain's ``events``, with what it wrote after a step dropped when that only repeats what it wrote just before
    the step. ``ResponseDone.text`` loses it too, so what she keeps, shows and says again is what she really said.
    ``failures``: how many of this turn's tools have failed so far."""
    passed: list[str] = []  # all that has gone out
    block: list[str] = []  # what has gone out since her last step
    answer = ""  # what she wrote just before her last step ("": she has taken no step since writing)
    failed_before = 0  # ``failures`` when she took that step
    held: list[str] = []  # written since her last step, not decided yet
    dropping = False  # the text being written was judged a retelling: the rest of it goes too
    dropped = False
    # Whether every step since her answer only changed a panel (None: no step yet but loading a tool).
    panel_only: bool | None = None

    def holding() -> bool:
        return len(meaning_words(answer)) >= _ANSWERED_WORDS and failures() == failed_before

    def go_out(text: str) -> TextDelta:
        passed.append(text)
        block.append(text)
        return TextDelta(text)

    def settle() -> TextDelta | None:
        """What's held is complete, or can be decided: it goes out, or it's dropped."""
        nonlocal dropped, answer
        text, held[:] = "".join(held), []
        if not text:
            return None
        if "{{" not in text and holding() and panel_only:
            dropped = True
            log.info("Nothing more after a panel change: %r", text.strip())
            return None
        if "{{" not in text and holding() and retold(text, answer):
            dropped = True
            log.info("Not saying again after a step: %r", text.strip())
            return None
        answer = ""  # it's news: the rest of it needn't wait
        return go_out(text)

    async for event in events:
        if isinstance(event, TextDelta):
            if dropping:
                continue
            if held or holding():
                held.append(event.text)
                if (holding() and panel_only and "{{" not in "".join(held)) or _settled("".join(held), answer):
                    out = settle()
                    dropping = out is None
                    if out is not None:
                        yield out
                continue
            yield go_out(event.text)
        elif isinstance(event, ToolUseStarted):
            if (out := settle()) is not None:
                yield out
            name = event.name.rsplit("__", 1)[-1]
            if block:
                answer, block[:] = "".join(block), []
                panel_only = None
            if name not in _NEUTRAL:
                panel_only = (panel_only is not False) and name in QUIET_AFTER and not event.builtin
            failed_before = failures()  # a call tried again: only whether the retry fails too counts
            dropping = False
            yield event
        elif isinstance(event, ResponseDone):
            if event.interrupted:
                dropped = dropped or bool(held)  # it never reached her voice
                held.clear()
            elif (out := settle()) is not None:
                yield out
            yield replace(event, text="".join(passed).strip()) if dropped else event
        else:
            yield event
