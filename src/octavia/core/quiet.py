"""Whether the user asked her to work without narrating ("just work, talk less"), or to talk them through it again.

Quiet work is a conversation-long mode (``Assistant``): what she says between her steps isn't spoken, and the brain is
told to keep it to the result. Nothing here grants anything, so the lists can be generous.
"""

from __future__ import annotations

import re

_QUIET = re.compile(
    "|".join(
        (
            # "just work", "just do it", "just keep going", "just get it done"
            r"\bjust\s+(?:work|do\s+(?:it|the\s+work|them|this|that)|keep\s+(?:going|working)|get\s+(?:it|this)\s+done)\b",
            # "talk less", "less talking", "stop talking", "quit talking", "no talking", "stop narrating"
            r"\b(?:talk|speak)\s+less\b",
            r"\bless\s+(?:talking|talk|chatter|narrat\w*)\b",
            r"\b(?:stop|quit|no(?:\s+more)?|enough|without)\s+(?:the\s+)?(?:talking|narrat\w*|commentary|chatter|explaining)\b",
            r"\b(?:don'?t|do\s+not|no\s+need\s+to|you\s+don'?t\s+(?:have|need)\s+to)\s+"
            r"(?:narrate|explain|talk|tell\s+me\s+(?:what|each|every)|describe|announce|say\s+what)",
            # "work quietly", "do it in silence", "be quiet while you work", "shut up and work"
            r"\b(?:work|do\s+(?:it|this|them)|keep\s+going)\s+(?:quietly|silently|in\s+silence)\b",
            r"\b(?:be\s+quiet|shut\s+up|hush)\s+(?:and|while)\b",
            r"\bquiet(?:ly)?\s+mode\b",
        )
    ),
    re.IGNORECASE,
)

_TALK = re.compile(
    "|".join(
        (
            r"\b(?:talk|walk)\s+me\s+through\b",
            r"\b(?:you\s+can|go\s+ahead\s+and|feel\s+free\s+to)\s+(?:talk|narrate|explain)\b",
            r"\btell\s+me\s+what\s+you'?re\s+doing\b",
            r"\b(?:keep\s+me\s+(?:posted|updated)|narrate|explain\s+(?:as\s+you\s+go|each\s+step|what\s+you'?re\s+doing))\b",
        )
    ),
    re.IGNORECASE,
)

_NEGATED_TALK = re.compile(r"\b(?:don'?t|do\s+not|no\s+need\s+to|stop|quit)\b[^.?!]{0,20}$", re.IGNORECASE)


def asks_for_quiet(text: str) -> bool:
    """The user wants her to work without narrating: "just work and talk less", "stop narrating"."""
    return bool(_QUIET.search(text or ""))


def asks_for_talk(text: str) -> bool:
    """The user wants to hear her steps again: "talk me through it", "you can narrate again"."""
    text = text or ""
    for match in _TALK.finditer(text):
        if not _NEGATED_TALK.search(text[: match.start()]):
            return True
    return False
