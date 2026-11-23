"""Whether a transcript sounds like the user paused mid-thought rather than finished.

The endpointer only hears silence, so "Set a timer for, um" followed by a breath to think closes the turn and she
answers half a request. The transcript tells the two apart: a trailing filler ("um"), a dangling conjunction ("and",
"so", "because"), an article with nothing after it, or a trailing comma or ellipsis means more is coming, so the
assistant keeps listening a little longer. A question or exclamation mark means the user finished, unless the last
word is a filler.
"""

from __future__ import annotations

import re

FILLERS = frozenset({"um", "umm", "uh", "uhh", "uhm", "erm", "er", "hmm", "hm", "mm", "mmm", "ah", "eh"})

# Words a finished sentence practically never ends on.
_DANGLING = frozenset(
    {"and", "but", "or", "so", "because", "cause", "like", "if", "the", "a", "an", "plus", "although"}
)

# ...except right after these: "I think so", "what's it like", "I'd like", "the cause", "C plus plus".
_FINISHED_AFTER = {
    "so": frozenset({"think", "hope", "guess", "suppose", "believe", "said", "say", "do", "did", "not", "is", "so"}),
    "like": frozenset({"it", "that", "this", "look", "looks", "feel", "feels", "sound", "sounds", "d", "would"}),
    "cause": frozenset({"the", "a", "that", "this", "its", "good", "main", "root"}),
    "plus": frozenset({"plus", "or"}),
}

# "and then", "but then": "then" alone ends plenty of sentences ("okay then").
_DANGLING_PAIRS = frozenset({("and", "then"), ("but", "then"), ("so", "then")})

_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")
_TRAILING_OFF = ("...", "…", ",", "-", "–", "—", ";", ":")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("’", "'"))


def only_fillers(text: str) -> bool:
    """True for a lone "um" or "uh, hmm": nothing worth keeping once the real request follows."""
    words = _words(text)
    return bool(words) and all(w in FILLERS for w in words)


def sounds_unfinished(text: str) -> bool:
    """True when ``text`` ends like a pause to think: a filler, a dangling word, or trailing punctuation."""
    stripped = text.strip()
    words = _words(stripped)
    if not words:
        return False
    last = words[-1]
    if last in FILLERS or stripped.endswith(_TRAILING_OFF):
        return True
    if stripped.endswith(("?", "!")):
        return False
    before = words[-2].split("'")[-1] if len(words) > 1 else ""
    if (before, last) in _DANGLING_PAIRS:
        return True
    return last in _DANGLING and before not in _FINISHED_AFTER.get(last, frozenset())
