"""Whether the user told her to be quiet altogether ("shut up", "be quiet", "leave me alone"), not just to drop a subject.

On 2026-11-19 the user told her to shut up at 6:27 PM, and four minutes later she announced the night's report on her
own anyway. Said as a whole utterance, these hold what she'd bring up unasked for a while (``AnnouncingMixin._hush``):
until the user talks to her again, or a quiet period runs out. A subject waved off ("shut up about that") is a
wave-off instead (``core/unprompted.py``), so only the whole utterance counts. ``text`` is what ``addressed`` returns.
"""

from __future__ import annotations

import re

_HUSH = re.compile(
    r"(?:(?:i said|i told you to|i asked you to|can you|could you|will you|would you|just|please|now|okay|ok)\s+)*"
    r"(?:shut up|shut it|be quiet|keep quiet|quiet|quiet down|silence|hush|shush|shh+|go away|leave me alone|"
    r"stop talking|no more talking|enough talking|zip it)"
    r"(?:\s+(?:already|for now|for a while|for a bit|okay|ok|right now|now|please))*"
)


def asks_for_hush(text: str) -> bool:
    """The user wants her quiet for now: "shut up", "I said shut up", "be quiet for a while", "leave me alone"."""
    return bool(_HUSH.fullmatch((text or "").strip()))


# Said while she's quiet, these aren't the user talking to her again: another "shut up" in other words ("seriously,
# shut up", "shut the hell up"), or a bare stop or dismissal ("enough", "stop", "never mind").
_KEEPS = re.compile(
    r"\b(?:shut\b.{0,12}\bup|be quiet|keep quiet|quiet|silence|hush|shush|shh+|go away|leave me alone|"
    r"stop talking|zip it)\b"
)
_BARE = frozenset(
    {
        "stop",
        "enough",
        "never mind",
        "nevermind",
        "nothing",
        "no",
        "nope",
        "ok",
        "okay",
        "cancel",
        "forget it",
        "that's all",
        "that's it",
        "that is all",
        "no thanks",
        "please",
    }
)


def keeps_hush(text: str) -> bool:
    """While she's quiet: whether ``text`` (``addressed``'s) leaves it so, instead of ending it as the user talking
    to her again would: a hush in other words, or a bare stop or dismissal."""
    words = " ".join((text or "").lower().replace(",", " ").replace(".", " ").replace("!", " ").split())
    return not words or words in _BARE or bool(_KEEPS.search(words))
