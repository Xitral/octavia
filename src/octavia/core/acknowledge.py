"""Instant spoken acknowledgments ("On it!") while the brain works on the answer.

Between the end of the user's sentence and the first word of the real reply
there are a few seconds of speech recognition, Claude thinking and TTS. The
acknowledger fills that gap: the moment a transcript arrives it plays a short
phrase in the assistant's own voice, then the reply follows.

Phrases come from ``personality/acknowledgments.yaml``; their audio is made
once and cached by ``PhraseCache``, so playing one needs no network.
"""

from __future__ import annotations

import logging
import random
import re
from pathlib import Path

import numpy as np
import yaml

from octavia.core.phrases import PhraseCache

log = logging.getLogger(__name__)

CATEGORIES = ("question", "request", "other")
# Said when she starts a web search before a word of her answer: the search alone keeps the user waiting many seconds.
SEARCHING = "search"
# Said when a turn has gone a long while without a word (``Assistant._still_working``).
WORKING = "working"
# Said when she starts writing out a call that takes her long to write, before any word of her answer
# (``Assistant._say_starting``): the user heard "On it." and then nothing until the call was written, had run, and
# she'd answered. Measured on 167 spoken turns with tools (2026-11-09 to 11-22): a new panel took her 11.5 s to
# write (25 calls), an explainer 13.2 s, a screen task's brief 7.6 s (20 calls), an objective 11.3 s; no other call
# took 5 s. Her answer came 12.1 s after the last word in the 112 turns where a call came before any word, 4.6 s in
# the 55 where she spoke first.
DRAWING = "panel"
SETTING_UP = "task"
SLOW_TO_WRITE = {
    "show_visual": DRAWING,
    "explain": DRAWING,
    "start_screen_task": SETTING_UP,
    "start_objective": SETTING_UP,
}
_LISTS = (*CATEGORIES, SEARCHING, WORKING, DRAWING, SETTING_UP)

_QUESTION_STARTS = {
    "what", "whats", "what's", "when", "where", "wheres", "where's", "who", "whos", "who's", "whom", "whose",
    "why", "how", "hows", "how's", "which", "is", "are", "am", "was", "were", "do", "does", "did", "have", "has",
    "had", "any", "should", "shall", "may", "might",
}  # fmt: skip
_POLITE_REQUESTS = ("can you", "could you", "would you", "will you", "can u", "please")
_REQUEST_VERBS = {
    "open", "play", "close", "launch", "start", "stop", "pause", "resume", "skip", "set", "turn", "remind",
    "add", "remember", "forget", "search", "find", "look", "show", "tell", "give", "make", "create", "send",
    "check", "read", "put", "go", "switch", "mute", "unmute", "lower", "raise", "increase", "decrease",
    "focus", "list", "write", "note", "save", "run", "book", "schedule", "call", "text", "email", "move",
}  # fmt: skip
# Small talk gets a direct reply fast enough; an "On it!" first would sound odd.
_SMALL_TALK = {
    "thanks", "thank you", "thanks a lot", "thank you so much", "cool", "nice", "awesome", "great", "perfect",
    "ok", "okay", "lol", "haha", "hello", "hi", "hey", "good morning", "good night", "goodnight", "bye",
    "goodbye", "see you", "love you", "you're the best", "yes", "no", "yeah", "nope", "sure",
}  # fmt: skip
_WORD = re.compile(r"[a-z']+")
# A thank-you or a compliment ("thank you so much", "good job", "you're a lifesaver"): her brain takes it, with what
# she just did in mind. The scripted preset lines it replaced lost to her brain on both judges, natural 21-9 and 27-3,
# character 21-8 and 23-7 (2026-11-25); her quick voice tied the presets on natural and lost on character.
_THANKS_OR_PRAISE = re.compile(
    r"\b(?:thanks|thank you|thank u|ty|thx|cheers|appreciate (?:it|you|that)|much appreciated|you'?re a lifesaver|"
    r"you saved me|(?:good|great|nice) (?:job|work|one)|well done|you(?:'?re| are) (?:so |really |the )?(?:awesome|amazing|"
    r"incredible|the best|brilliant|smart|a genius|a legend|the goat)|that was (?:impressive|amazing|awesome|great|"
    r"perfect|sick)|you did (?:great|good|amazing|it)|you nailed it|nailed it|you rock|proud of you)\b"
)
_DECLINES = re.compile(r"^(?:no|nope|nah|not now|no no)\b")


def thanks_or_praise(text: str) -> bool:
    """Whether ``text`` is a short thank-you or compliment, not a "no thanks" to an offer."""
    norm = " ".join(_WORD.findall(text.lower().replace("octavia", "")))
    words = norm.split()
    if not 0 < len(words) <= 10 or _DECLINES.match(norm) or any(w in _REQUEST_VERBS for w in words):
        return False  # "no thanks" answers an offer; "thanks, now open Spotify" is a request
    return bool(_THANKS_OR_PRAISE.search(norm))


def classify(text: str) -> str | None:
    """Kind of utterance: question, request or other. None when no acknowledgment fits (small talk)."""
    norm = " ".join(_WORD.findall(text.lower()))
    if not norm or norm in _SMALL_TALK or thanks_or_praise(text):
        return None
    words = norm.split()
    if len(words) <= 1:
        return None
    first = words[0]
    rest = norm
    for prefix in _POLITE_REQUESTS:
        if norm.startswith(prefix + " "):
            rest = norm[len(prefix) + 1 :]
            break
    if rest != norm and rest.split()[0] in _REQUEST_VERBS:
        return "request"
    if first in _REQUEST_VERBS:
        return "request"
    if text.rstrip().endswith("?") or first in _QUESTION_STARTS:
        return "question"
    return "other"


def load_phrases(path: Path) -> dict[str, list[str]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} isn't valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must map categories to lists of phrases")
    phrases: dict[str, list[str]] = {}
    for category in _LISTS:
        items = data.get(category) or []
        if not isinstance(items, list):
            raise ValueError(f"{path}: '{category}' must be a list of phrases")
        phrases[category] = [" ".join(str(p).split()) for p in items if str(p).strip()]
    unknown = set(data) - set(_LISTS)
    if unknown:
        log.warning("%s: ignoring unknown categories %s (use %s)", path, sorted(unknown), list(_LISTS))
    return phrases


class Acknowledger:
    """Picks a phrase that fits what the user said. Registers its phrases with the cache."""

    def __init__(self, phrases: dict[str, list[str]], cache: PhraseCache, rng: random.Random | None = None) -> None:
        self.phrases = phrases
        self.cache = cache
        self._rng = rng or random.Random()
        self._last: str | None = None
        self._last_start: str | None = None  # the last line said as a slow call began (``starting``)
        cache.add(p for category in _LISTS for p in phrases.get(category, []))

    @property
    def ready(self) -> int:
        """How many of the phrases can be played right now."""
        return len({p for group in self.phrases.values() for p in group if self.cache.get(p) is not None})

    def _playable(self, category: str) -> dict[str, np.ndarray]:
        clips = {p: self.cache.get(p) for p in self.phrases.get(category, [])}
        return {p: clip for p, clip in clips.items() if clip is not None}

    def choose(self, text: str) -> tuple[str, np.ndarray] | None:
        """A phrase that fits what the user said, and its audio. None to stay quiet."""
        category = classify(text)
        if category is None:
            return None
        # That list isn't ready (or is empty): borrow the generic one.
        options = self._playable(category) or self._playable("other")
        if not options:
            return None
        names = list(options)
        if len(names) > 1 and self._last in names:
            names.remove(self._last)  # never the same phrase twice in a row
        phrase = self._rng.choice(names)
        self._last = phrase
        return phrase, options[phrase]

    def still_working(self) -> tuple[str, np.ndarray] | None:
        """A phrase for a long silence mid-turn ("Still on it."), and its audio; None when none is ready."""
        options = self._playable(WORKING)
        if not options:
            return None
        phrase = self._rng.choice(list(options))
        return phrase, options[phrase]

    def starting(self, tool: str) -> tuple[str, np.ndarray] | None:
        """A phrase for the start of a call that takes long to write ("Putting it on a panel." for a new panel), and
        its audio. None for any other call, or when none of that call's lines is ready: no stand-in from the other
        lists (it says what she's doing, and "Okay." a second time wouldn't)."""
        options = self._playable(SLOW_TO_WRITE.get(tool, ""))
        if not options:
            return None
        names = list(options)
        if len(names) > 1 and self._last_start in names:
            names.remove(self._last_start)  # not the same line twice in a row
        phrase = self._rng.choice(names)
        self._last_start = phrase
        return phrase, options[phrase]

    def searching(self) -> tuple[str, np.ndarray] | None:
        """A phrase for a web search starting ("Looking it up online."), and its audio. None when there's none ready:
        no stand-in from the other lists, as "Gotcha." seconds into the wait would sound odd."""
        options = self._playable(SEARCHING)
        if not options:
            return None
        phrase = self._rng.choice(list(options))
        return phrase, options[phrase]
