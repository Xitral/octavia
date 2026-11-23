"""No lines about her panel being there.

After putting a quiz question on a panel she'd close with "The question's on the panel, tap your pick or just say
it." The user watched the panel change: the line told them nothing, and on 2026-11-22 they asked her to just ask
the question and stop. Her prompt says so; this makes it so. A short sentence whose only news is that something is on
the panel, or how to answer on it, is left out of what she says (and keeps); a sentence that says something about the
topic, or a real question about it, always goes out, panel words or not.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import replace

from octavia.brain.base import BrainEvent, ResponseDone, TextDelta, ToolUseStarted

log = logging.getLogger(__name__)

_MAX_WORDS = 14  # longer than this, a sentence carries more than a pointer
_WHERE = r"(?:on|up on|onto|in)\s+(?:the|your|my|that|this)\s+(?:panel|screen|quiz|board)"
# "It's on the panel", "the question's up on your screen", "everything's on the panel now".
_IS_THERE = re.compile(
    r"^(?:okay,?\s+|so,?\s+|and\s+)?(?:it|that|this|the \w+|your \w+|everything|they|both|all \w+)"
    rf"(?:'s|'re| is| are)\s+(?:all\s+|now\s+|right\s+)?(?:up\s+)?{_WHERE}\b",
    re.IGNORECASE,
)
# "Tap your pick or just say it", "click an answer or tell me", "pick one on the panel".
_HOW_TO_ANSWER = re.compile(
    # The verb said as an instruction (at the start, after a comma or "just"), not "which does MRV pick on the panel".
    r"(?:^|,\s*|\bjust\s+)(?:tap|click|press|hit|pick|choose|select)\b[^.?!]*\b(?:or (?:just )?(?:say|tell)|button|"
    rf"{_WHERE})\b|^(?:just\s+)?(?:say|tell me) (?:it|your (?:pick|answer)) or (?:tap|click)",
    re.IGNORECASE,
)
# "Take a look at the panel", "check the panel".
_LOOK = re.compile(
    r"^(?:take a look|have a look|look|check(?: out)?)\s+(?:at\s+)?(?:the|your)\s+panel\b", re.IGNORECASE
)
_SENTENCE_END = re.compile(r"[.!?…][\"”')\]]*(?:\s+|$)|\n+")


def is_panel_pointer(sentence: str) -> bool:
    """Whether ``sentence`` only says something is on her panel, or how to answer on it."""
    text = sentence.strip().strip("*_ ")
    if not text or len(text.split()) > _MAX_WORDS:
        return False
    how = bool(_HOW_TO_ANSWER.search(text))
    if text.endswith("?") and not how:
        return False  # a real question, even one about what's on the panel
    return how or bool(_IS_THERE.search(text)) or bool(_LOOK.search(text))


async def without_panel_pointers(events: AsyncIterable[BrainEvent]) -> AsyncIterator[BrainEvent]:
    """The brain's ``events`` with pointer sentences left out, whole sentences at a time (her voice speaks in
    sentences anyway). ``ResponseDone.text`` loses them too."""
    partial = ""
    dropped: list[str] = []

    def sentences(final: bool) -> list[str]:
        nonlocal partial
        out = []
        while match := _SENTENCE_END.search(partial):
            out.append(partial[: match.end()])
            partial = partial[match.end() :]
        if final and partial:
            out.append(partial)
            partial = ""
        return out

    def kept(parts: list[str]) -> str:
        text = ""
        for sentence in parts:
            if is_panel_pointer(sentence):
                log.info("Not saying a line about the panel being there: %r", sentence.strip())
                dropped.append(sentence.strip())
            else:
                text += sentence
        return text

    async for event in events:
        if isinstance(event, TextDelta):
            partial += event.text
            if text := kept(sentences(False)):
                yield TextDelta(text)
        elif isinstance(event, ToolUseStarted | ResponseDone):
            if text := kept(sentences(True)):
                yield TextDelta(text)
            if isinstance(event, ResponseDone) and dropped and event.text:
                cleaned = event.text
                for line in dropped:
                    cleaned = cleaned.replace(line, "", 1)
                event = replace(event, text=re.sub(r"[ \t]{2,}", " ", cleaned).strip())
            yield event
        else:
            yield event
