"""Telling "say that again" apart from everything else: "Sorry, can you repeat that?", "I didn't catch that",
"Octavia, what did you just say?".

It's answered with no model at all: she says her last reply again, word for word (``Assistant._say_again``). Asking
the brain to repeat itself cost a turn and a few seconds, and it reworded the answer instead of repeating it. Only the
whole utterance counts (``core`` is what ``stopping.addressed`` returns), so "pause the music and repeat what you
said" or "what did you say about the weather" stay requests for the brain. "What was that?" and "one more time" aren't
ones, nor a bare "repeat": they're as often about a sound or a song.
"""

from __future__ import annotations

import re

_SAID = r"what you (?:just )?said(?: to me)?"
# Not "I didn't get it" or "I missed it": those answer her ("did you get my email?") or mean "I don't understand".
_MISSED = rf"i (?:didn'?t|did not) (?:quite )?(?:catch|hear) (?:that|you|{_SAID})|i missed (?:that|{_SAID})"
_LEAD = rf"(?:(?:sorry|wait|huh|pardon|i wasn'?t (?:paying attention|listening)|{_MISSED}) )*"
_ASK = r"(?:(?:can|could|would|will) you (?:please )?|please )?"
_AGAIN = "|".join(
    (
        rf"say (?:that|it) again|say again|say {_SAID}(?: again)?",
        rf"repeat (?:that|yourself|{_SAID}|the last (?:part|thing|bit))(?: again)?",
        r"what did you (?:just )?say",
        r"come again",
        r"pardon",
        _MISSED,
    )
)
_PATTERN = re.compile(rf"{_LEAD}{_ASK}(?:{_AGAIN})(?: (?:to|for) me)?(?: again)?(?: please)?")


def asks_to_say_again(core: str) -> bool:
    """Whether the whole utterance asks her to repeat her last reply. ``core`` is what ``stopping.addressed`` returns
    (lowercase, no punctuation, without her name or "please")."""
    return bool(core) and _PATTERN.fullmatch(core) is not None
