"""Preset replies: answers that don't need the brain at all.

"Thanks!" doesn't need Claude to think about it. Rules in
``personality/quick_replies.yaml`` map things the user might say to replies
spoken in the assistant's voice, with no brain call (so no usage and no
thinking delay). A rule fires only when the whole utterance is one of its
phrases, give or take the assistant's name, filler like "okay" in front and
words like "so much", so "thanks, and what's the weather?" still goes to the
brain. Reply audio is cached by ``PhraseCache`` and plays instantly.

A rule can also have lines for her moods (``moods``, keyed by the words ``inner/mood.py`` gives): on a day she's
"buzzing", "thanks" gets one of those instead. Her mood is worked out from what's already recorded, never by a model,
so the reply stays instant.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import yaml

from octavia.core.phrases import PhraseCache
from octavia.core.text import normalize_text

log = logging.getLogger(__name__)

Then = Literal["end", "listen"]

# Ignored anywhere: "thank you SO MUCH", "thanks A LOT".
_INTENSIFIERS = frozenset({"so", "much", "very", "really", "a", "lot", "bunch", "ton", "truly", "just"})
# Ignored at the start or at the end: "OKAY, thanks", "thanks AGAIN". The assistant's name is added to both.
_LEADING = (
    "all right", "got it", "sounds good", "ok", "okay", "oh", "ah", "alright", "cool", "great", "awesome",
    "perfect", "nice", "sweet", "wow", "yeah", "yes", "yep", "hey", "well", "gotcha", "and",
)  # fmt: skip
_TRAILING = ("again", "anyway", "buddy", "dude", "man", "bye", "goodbye")
_MAX_WORDS = 12  # anything longer is a real request


def _key(phrase: str) -> str:
    return " ".join(w for w in normalize_text(phrase).split() if w not in _INTENSIFIERS)


@dataclass(frozen=True)
class QuickReplyRule:
    name: str
    when: tuple[str, ...]  # phrases as written in the file
    say: tuple[str, ...]
    then: Then = "end"
    moods: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (her mood word, its replies), used instead of say

    def replies(self, mood: str | None) -> tuple[str, ...]:
        """What it may say when she's in this mood (None: no particular mood)."""
        return next((say for word, say in self.moods if word == mood), self.say) if mood else self.say


@dataclass(frozen=True, eq=False)
class QuickReply:
    rule: str
    text: str
    then: Then
    audio: np.ndarray | None = None  # cached clip at the player's rate; None = synthesize (or just show) it


def _phrase_list(path: Path, rule: str, key: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise ValueError(f"{path}: '{rule}.{key}' must be a list of phrases in quotes")
    return tuple(" ".join(x.split()) for x in items if x.strip())


def load_quick_replies(path: Path) -> list[QuickReplyRule]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} isn't valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must map rule names to rules with 'when' and 'say'")
    rules = []
    for name, body in data.items():
        if not isinstance(body, dict):
            raise ValueError(f"{path}: '{name}' needs 'when' and 'say' lists")
        unknown = set(body) - {"when", "say", "then", "moods"}
        if unknown:
            raise ValueError(f"{path}: '{name}' has unknown keys {sorted(unknown)} (use when, say, then, moods)")
        when = _phrase_list(path, str(name), "when", body.get("when"))
        say = _phrase_list(path, str(name), "say", body.get("say"))
        if not when or not say:
            raise ValueError(f"{path}: '{name}' needs at least one 'when' phrase and one 'say' reply")
        then = body.get("then", "end")
        if then not in ("end", "listen"):
            raise ValueError(f"{path}: '{name}.then' must be end or listen, not {then!r}")
        moods_raw = body.get("moods") or {}
        if not isinstance(moods_raw, dict):
            raise ValueError(f"{path}: '{name}.moods' must map a mood to its replies")
        moods = []
        for mood, lines in moods_raw.items():
            replies = _phrase_list(path, str(name), f"moods.{mood}", lines)
            if not replies:
                raise ValueError(f"{path}: '{name}.moods.{mood}' needs at least one reply")
            moods.append((" ".join(str(mood).lower().split()), replies))
        rules.append(QuickReplyRule(str(name), when, say, then, tuple(moods)))
    return rules


class QuickReplies:
    def __init__(
        self,
        rules: list[QuickReplyRule],
        cache: PhraseCache | None = None,
        assistant_name: str = "",
        rng: random.Random | None = None,
        mood: Callable[[], str | None] | None = None,
    ) -> None:
        self.rules = rules
        self.cache = cache
        self.mood = mood  # her mood word right now (``inner/mood.py`` ``Mood.word``), or None
        self._rng = rng or random.Random()
        self._last: dict[str, str] = {}
        self._lookup: dict[str, QuickReplyRule] = {}
        for rule in rules:
            for phrase in rule.when:
                key = _key(phrase)
                owner = self._lookup.setdefault(key, rule) if key else None
                if owner is not None and owner is not rule:
                    log.warning(
                        "Preset reply phrase %r is in both '%s' and '%s'; using '%s'",
                        phrase,
                        owner.name,
                        rule.name,
                        owner.name,
                    )
        name = tuple(_key(assistant_name).split())
        self._leading = [tuple(f.split()) for f in _LEADING] + ([name] if name else [])
        self._trailing = [tuple(f.split()) for f in _TRAILING] + ([name] if name else [])
        if cache is not None:
            cache.add(reply for rule in rules for reply in (*rule.say, *(r for _, say in rule.moods for r in say)))

    def find(self, text: str) -> QuickReplyRule | None:
        """The rule for this utterance, if the whole thing is one of its phrases."""
        words = normalize_text(text).split()
        if not words or len(words) > _MAX_WORDS:
            return None
        start = tuple(w for w in words if w not in _INTENSIFIERS)
        pending, seen = [start], set()
        while pending:  # peel filler off either end until something matches
            current = pending.pop()
            if not current or current in seen:
                continue
            seen.add(current)
            rule = self._lookup.get(" ".join(current))
            if rule is not None:
                return rule
            pending += [current[len(f) :] for f in self._leading if current[: len(f)] == f]
            pending += [current[: -len(f)] for f in self._trailing if current[-len(f) :] == f]
        return None

    def match(self, text: str) -> QuickReply | None:
        """The reply to give, or None when this needs the brain."""
        rule = self.find(text)
        if rule is None:
            return None
        options = list(rule.replies(self._mood()) if rule.moods else rule.say)
        if self.cache is not None:
            options = [r for r in options if self.cache.get(r) is not None] or options  # instant ones first
        if len(options) > 1 and self._last.get(rule.name) in options:
            options.remove(self._last[rule.name])  # not the same answer twice in a row
        reply = self._rng.choice(options)
        self._last[rule.name] = reply
        audio = self.cache.get(reply) if self.cache is not None else None
        return QuickReply(rule=rule.name, text=reply, then=rule.then, audio=audio)

    def _mood(self) -> str | None:
        if self.mood is None:
            return None
        try:
            return self.mood()
        except Exception:
            log.debug("Couldn't get her mood for a preset reply", exc_info=True)
            return None
