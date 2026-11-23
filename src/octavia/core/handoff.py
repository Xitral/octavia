"""Her reply carried on from words she's already said: the handoff from her opener to the brain (docs/octavia-2.0.md).

When her opener (``brain/opener.py``) has started a reply out loud, the brain is told those exact words in its turn
and writes what comes next (decision D-004). This sits between the brain's text and the speech stream the opener is
already playing on, so the two are one reply in one voice:

* The brain's first sentence is held until it's whole and checked once: one that only acknowledges ("Sure!",
  "Good question.") or says the opener again is dropped; one that begins with the opener's words has them cut. The
  rest streams straight through, so nothing else waits.
* ``report`` says how it went for the routing log: the silence between the opener's last audio and the brain's first
  (``gap_s``), what was dropped, and whether the brain began by taking something back ("Actually, ...": a sign the
  opener was wrong, counted as ``corrected``).
"""

from __future__ import annotations

import re
import time
from typing import Any

from octavia.core.speech import SpeechStream
from octavia.core.text import normalize_text

# The end of the brain's first sentence: its punctuation, then a space (or a line break).
_SENTENCE_END = re.compile(r"[.!?…][\"')\]”’]*\s|\n")
_HOLD_CHARS = 240  # past this with no sentence end, it's let through as it is
_ACK_ONLY = re.compile(
    r"^(?:(?:sure|okay|ok|alright|all right|got it|gotcha|right|yeah|yes|of course|absolutely|good question|"
    r"great question|on it|no problem|happy to help|totally|for sure|mhm|hmm+|so)[\s,!.]*)+$",
    re.IGNORECASE,
)
_TAKES_BACK = re.compile(r"^(?:actually|wait|hold on|no,|nope|sorry|correction|scratch that|i misspoke|well, no)\b",
                         re.IGNORECASE)  # fmt: skip
_WORD = re.compile(r"[a-z0-9']+")


def _content(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 3}


def judge(sentence: str, opener: str) -> tuple[str, str]:
    """What becomes of the brain's first ``sentence`` after ``opener``: ("keep" | "drop" | "trim" | "ack", what's
    said). "drop": it says the opener again and adds nothing (its content words are all the opener's); "trim": it
    began with the opener's own words, which are cut off it; "ack": it only acknowledges ("Sure!"), dropped only if
    more follows it."""
    plain = sentence.strip()
    if not plain:
        return "keep", sentence
    if _ACK_ONLY.match(plain):
        return "ack", ""
    said = normalize_text(opener)
    norm = normalize_text(plain)
    if said and norm == said:
        return "drop", ""
    if said and norm.startswith(said + " "):
        # The opener's words, then more: keep only the more (as many words as the opener had are cut).
        words = plain.split()
        rest = " ".join(words[len(opener.split()) :]).lstrip(" ,;:-—")
        return ("trim", rest) if rest else ("drop", "")
    mine = _content(plain)
    if mine and mine <= _content(opener):
        return "drop", ""
    return "keep", sentence


class Handoff:
    """The speech stream of a reply her opener began, with the brain's text going in through ``feed``. Everything
    else is the stream's own (``__getattr__``)."""

    def __init__(self, stream: SpeechStream, opener: str) -> None:
        self.stream = stream
        self.opener = opener
        self.opener_segments = stream.pushed  # the opener's own segments, all fed before the brain's first word
        self._held = ""
        self._judged = False
        self._ack: str | None = None  # an acknowledgment-only first sentence, dropped only if more follows it
        self.dropped = ""  # the brain's first sentence, or the opener's words cut off it, when it said them again
        self.corrected = False
        self._parts: list[str] = []  # what was dropped or cut, in order

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)

    def feed(self, delta: str) -> None:
        if self._judged:
            self.stream.feed(delta)
            return
        self._held += delta
        head = self._held.lstrip()
        end = _SENTENCE_END.search(head)
        if end is None and len(head) < _HOLD_CHARS:
            return
        cut = end.end() if end is not None else len(head)
        self._release(head[:cut], head[cut:])

    def _release(self, first: str, rest: str = "") -> None:
        self._held = ""
        verdict, said = judge(first, self.opener)
        if verdict == "ack":
            if self._ack is not None:
                self._parts.append(self._ack.strip())
            self._ack = first
            if rest:
                self.feed(rest)
            return
        if self._ack is not None:  # more came after the acknowledgment: it goes
            self._parts.append(self._ack.strip())
            self._ack = None
        if verdict == "drop":
            self._parts.append(first.strip())
        elif verdict == "trim":
            self._parts.append(first.strip()[: len(first.strip()) - len(said)].strip())
        self._judged = True
        self.dropped = " ".join(self._parts)
        out = (said if verdict != "drop" else "") + (" " if verdict == "trim" and rest else "") + rest
        self.corrected = bool(_TAKES_BACK.match(out.lstrip()))
        if out.strip():
            self.stream.feed(out)

    def _settle(self) -> None:
        if self._judged:
            return
        if self._held.strip():
            self._release(self._held.lstrip())
        if not self._judged and self._ack is not None:
            # The acknowledgment was all there was ("Yes."): it's her answer, so it's said.
            self._judged = True
            self.dropped = " ".join(self._parts)
            self.stream.feed(self._ack)
            self._ack = None

    def flush(self) -> None:
        self._settle()
        self.stream.flush()

    def say(self, text: str) -> None:
        self._settle()
        self.stream.say(text)

    def finish(self) -> None:
        self._settle()
        self.stream.finish()

    def trim(self, text: str) -> str:
        """The brain's whole reply as she said it: without what was dropped from its start."""
        body = text.lstrip()
        for part in self._parts:
            if part and body.startswith(part) and body[len(part) : len(part) + 1] in ("", " ", ",", ";", ":", "-"):
                body = body[len(part) :].lstrip(" ,;:-—")
        return body

    def report(self) -> dict[str, Any]:
        """How the handoff went: the silence the user heard between the opener and the brain's first words (None when
        the brain's words never played), what was dropped, and whether the brain began by taking something back."""
        stream = self.stream
        gap = None
        last = self.opener_segments - 1
        ended = stream.played_at.get(last)
        began = next((stream.started_at[i] for i in sorted(stream.started_at) if i > last), None)
        if ended is not None and began is not None:
            gap = round(max(0.0, began - ended), 3)
        report: dict[str, Any] = {"gap_s": gap}
        if began is not None:
            report["answer_ago_s"] = round(time.perf_counter() - began, 3)  # the brain's first words were heard then
        if self.dropped:
            report["dropped"] = self.dropped
        if self.corrected:
            report["corrected"] = True
        return report
