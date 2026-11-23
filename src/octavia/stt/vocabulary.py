"""Custom speech vocabulary: names and words speech recognition keeps getting wrong.

Parakeet (through onnx-asr) decodes greedily and takes no prompt or hotword list, so the vocabulary can't steer
recognition itself. The transcript is corrected afterwards instead: ``personality/vocabulary.yaml`` lists terms
("Saoirse", "TickTick") and, optionally, how they tend to come out ("sear sha"). A heard phrase becomes the term when
it is one of those spellings, the term with other spacing or capitals ("tick tick"), or very close to it ("octa via"
for "Octavia"). The close match is strict and only for longer terms, so ordinary words are left
alone; anything it misses belongs in ``sounds_like``. The file is re-read when it changes, so an edit applies to the
next thing said, without a restart.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from rapidfuzz import fuzz

log = logging.getLogger(__name__)

VOCABULARY_FILE = "personality/vocabulary.yaml"

_FUZZY_MIN_CHARS = 6  # shorter terms are too close to ordinary words to guess at
_FUZZY_SCORE = 90.0  # rapidfuzz ratio of the spaceless forms: "meridianatlast" vs "meridianatlas" is 96
_WORD = re.compile(r"[^\W_]+")
_JOIN = re.compile(r"[\s-]+")  # what may sit between the words of one heard phrase


def _key(text: str) -> str:
    """Lowercase letters and digits only, so spacing, hyphens and capitals don't matter."""
    return "".join(_WORD.findall(text.lower()))


@dataclass(frozen=True)
class VocabularyTerm:
    term: str
    sounds_like: tuple[str, ...] = ()


def load_vocabulary(path: Path) -> list[VocabularyTerm]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} isn't valid YAML: {exc}") from None
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a list of terms")
    terms = []
    for item in data:
        if isinstance(item, str):
            item = {"term": item}
        if not isinstance(item, dict) or not isinstance(item.get("term"), str):
            raise ValueError(f"{path}: {item!r} must be a term in quotes, or have 'term' and 'sounds_like'")
        unknown = set(item) - {"term", "sounds_like"}
        if unknown:
            raise ValueError(f"{path}: '{item['term']}' has unknown keys {sorted(unknown)} (use term, sounds_like)")
        sounds_like = item.get("sounds_like") or []
        if isinstance(sounds_like, str):
            sounds_like = [sounds_like]
        if not isinstance(sounds_like, list) or not all(isinstance(x, str) for x in sounds_like):
            raise ValueError(f"{path}: '{item['term']}.sounds_like' must be a list of phrases in quotes")
        term = " ".join(item["term"].split())
        if _key(term):
            terms.append(VocabularyTerm(term, tuple(" ".join(x.split()) for x in sounds_like if _key(x))))
    return terms


@dataclass(frozen=True)
class _Candidate:
    start: int  # word index
    end: int  # exclusive
    score: float
    term: str


class Vocabulary:
    def __init__(self, terms: Sequence[VocabularyTerm]) -> None:
        self.terms = tuple(terms)
        self._exact: dict[str, str] = {}
        max_words = 0
        for t in self.terms:
            for spelling in (t.term, *t.sounds_like):
                self._exact.setdefault(_key(spelling), t.term)
                max_words = max(max_words, len(_WORD.findall(spelling)))
        self._fuzzy = [(t.term, k) for t in self.terms if len(k := _key(t.term)) >= _FUZZY_MIN_CHARS]
        self._max_words = max_words + 1 if max_words else 0  # one more: "meridian at last"

    def correct(self, text: str) -> str:
        if not self._max_words:
            return text
        words = list(_WORD.finditer(text))
        candidates = []
        for i in range(len(words)):
            best: _Candidate | None = None
            for j in range(i + 1, min(i + self._max_words, len(words)) + 1):
                if j - i > 1 and not _JOIN.fullmatch(text[words[j - 2].end() : words[j - 1].start()]):
                    break
                match = self._match("".join(w.group().lower() for w in words[i:j]))
                if match is not None and (best is None or match[0] > best.score):
                    best = _Candidate(i, j, *match)
            if best is not None:
                candidates.append(best)
        # The best matches win where they overlap: "a tick tick" is "a" + the exact term, not a close match.
        taken: set[int] = set()
        chosen = []
        for c in sorted(candidates, key=lambda c: (-c.score, c.start)):
            if taken.isdisjoint(range(c.start, c.end)):
                taken.update(range(c.start, c.end))
                chosen.append(c)
        for c in sorted(chosen, key=lambda c: c.start, reverse=True):
            text = text[: words[c.start].start()] + c.term + text[words[c.end - 1].end() :]
        return text

    def _match(self, key: str) -> tuple[float, str] | None:
        term = self._exact.get(key)
        if term is not None:
            return 100.0, term
        best = None
        for term, term_key in self._fuzzy:
            score = fuzz.ratio(key, term_key, score_cutoff=_FUZZY_SCORE)
            if score and (best is None or score > best[0]):
                best = (score, term)
        return best


class VocabularyFile:
    """The vocabulary in a user-edited file, re-read whenever the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self._vocabulary = Vocabulary(())

    def current(self) -> Vocabulary:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp = stamp
            if stamp is None:
                self._vocabulary = Vocabulary(())
            else:
                try:
                    self._vocabulary = Vocabulary(load_vocabulary(self.path))
                except (OSError, ValueError) as exc:
                    # Half-way through an edit: keep the last good list rather than losing every correction.
                    log.warning("Speech vocabulary not reloaded: %s", exc)
        return self._vocabulary

    def correct(self, text: str) -> str:
        return self.current().correct(text)
