"""Spoken long forms of texting shorthand.

Messages read aloud (notifications, emails, chats) are full of "rn" and "idk"; the voice spells those out letter
by letter, which sounds wrong. Only a fixed list of chat shorthand is expanded, so real acronyms ("WW", "POC",
"AFSC") are left alone, and only whole words: never part of a longer word, a dotted acronym ("U.S."), a hyphenated
word ("NP-hard"), a handle or a path.
"""

from __future__ import annotations

import re

TEXTING_ABBREVIATIONS: dict[str, str] = {
    "rn": "right now",
    "btw": "by the way",
    "idk": "I don't know",
    "tbh": "to be honest",
    "imo": "in my opinion",
    "lmk": "let me know",
    "ngl": "not gonna lie",
    "omw": "on my way",
    "wyd": "what are you doing",
    "nvm": "never mind",
    "smh": "shaking my head",
    "ty": "thank you",
    "np": "no problem",
    "pls": "please",
    "plz": "please",
    "u": "you",
    "ur": "your",
}

_WORD = re.compile(
    r"(?<![\w.'’/@#-])(" + "|".join(sorted(TEXTING_ABBREVIATIONS, key=len, reverse=True)) + r")(?![\w'’/@-]|\.\w)",
    re.IGNORECASE,
)


def expand_texting_abbreviations(text: str) -> str:
    """Replace whole-word chat shorthand with what it stands for, keeping a capital at the start of a sentence."""

    def expand(m: re.Match[str]) -> str:
        word = m.group(1)
        long_form = TEXTING_ABBREVIATIONS[word.lower()]
        return long_form[0].upper() + long_form[1:] if word[0].isupper() else long_form

    return _WORD.sub(expand, text)
