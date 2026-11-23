"""Small text helpers shared by the conversation logic and the integrations: how things are said aloud."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, time

_NOT_WORD = re.compile(r"[^\w' ]+")


def normalize_text(text: str) -> str:
    """Lowercase, straight apostrophes, no punctuation, single spaces: "Okay, thanks!" -> "okay thanks"."""
    text = text.lower().replace("’", "'")
    return " ".join(_NOT_WORD.sub(" ", text).split())


def spoken_list(parts: Sequence[str], last: str = "and") -> str:
    """'a', 'a and b', 'a, b and c' (no comma before the last one, as it's said); "" for none."""
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} {last} {parts[-1]}"


def clock(value: time | datetime) -> str:
    """ "8:05 AM", as it's said."""
    return f"{value:%I:%M %p}".lstrip("0")


def day(value: date) -> str:
    """ "Saturday, September 26"."""
    return f"{value:%A, %B} {value.day}"


# A sentence ends at . ! or ? (and any closing quote or bracket after it), before a space and a capital or digit.
_SENTENCE_END = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)\]]))\s+(?=[\"“(\[]?[A-Z0-9])")
# Words with a period that don't end a sentence: "Dr. Smith", "e.g. Canvas".
_ABBREVIATION = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|vs|etc|approx|e\.g|i\.e)\.$", re.IGNORECASE)
_CLAUSE_BREAKS = ("; ", ": ", " — ", " – ", ", ")


def plain(text: str) -> str:
    """One line without Markdown marks (#, *, backticks): what's left to say aloud."""
    return " ".join(re.sub(r"[#*`]+", " ", text).split())


def sentences(text: str) -> list[str]:
    """``text`` split into its sentences (a period, question or exclamation mark, then a capital or a digit)."""
    parts: list[str] = []
    for part in _SENTENCE_END.split(plain(text)):
        if parts and _ABBREVIATION.search(parts[-1]):
            parts[-1] = f"{parts[-1]} {part}"
        elif part:
            parts.append(part)
    return parts


def spoken_summary(text: str, limit: int) -> str:
    """``text`` short enough to say, never stopping mid-sentence: whole sentences while they fit in ``limit``
    characters (at least the first). A first sentence that's too long on its own ends at its last clause break
    (a comma, semicolon, colon or dash) that fits, else at a word, with a period: it's said, so no "…"."""
    whole = plain(text)
    if len(whole) <= limit:
        return whole
    parts = sentences(whole)
    said = parts[0]
    for part in parts[1:]:
        if len(said) + 1 + len(part) > limit:
            break
        said = f"{said} {part}"
    if len(said) <= limit:
        return said
    head = said[: limit - 1]
    cut = max((head.rfind(mark) for mark in _CLAUSE_BREAKS), default=-1)
    if cut < limit // 3:
        cut = head.rfind(" ")
    head = head[:cut] if cut > 0 else head
    return head.rstrip(" ,;:—–-.!?") + "."
