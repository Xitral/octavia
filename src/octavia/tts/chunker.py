"""Turns a stream of LLM text deltas into speakable segments.

TTS sounds best on whole sentences, but waiting for the whole reply adds
latency. The chunker releases a segment as soon as a sentence boundary is
confirmed (the punctuation is followed by whitespace). It merges very short
sentences after the first one, splits run-ons at a clause boundary, and cleans
out markdown and other things that shouldn't be read aloud.

A one-word opener ("Partly.", "Thanks.") can optionally be held and sent with
the next sentence: spoken alone it finishes long before the next segment is
generated and synthesized, leaving an awkward pause right after it.

What follows a flush (a tool call, a question asked mid-turn) is an opener
again: nothing is playing by then, so its first sentence goes out at once
rather than waiting to be merged with the next. A long opening sentence can
also be cut at a clause boundary (``first_max_chars``), so the first words
are heard before the model has finished writing it.
"""

from __future__ import annotations

import logging
import re

from octavia.tts.shown import (
    Spoken,
    beats_in,
    clean_for_screen,
    inside,
    mark_spans,
    open_mark,
    said_form,
    shown_form,
)
from octavia.tts.texting import expand_texting_abbreviations

log = logging.getLogger(__name__)

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "fig",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "a.m", "p.m",
}  # fmt: skip

_BOUNDARY = re.compile(r"[.!?…]+[\"')\]]*(?=\s)|\n+")
_CLAUSE = re.compile(r"[,;:—–](?=\s)")

_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MD_MARKS = re.compile(r"(\*\*|__|\*|`+|~~)")
_LIST_PREFIX = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")
_HEADING = re.compile(r"(?m)^\s*#{1,6}\s*")
_CONTROL = re.compile(r"<\|[^|>]*\|>")
_BRACKET = re.compile(r"\[([^\[\]]{0,40})\]")
_SENTENCE_END = re.compile(r"[.!?…]+[\"')]*(?:\s+|$)|\n+")  # for one tone cue per sentence
# A list of sources after a web search ("Sources: - [TALON internship](https://...)"): shown on screen, not read
# out. The heading only counts with a list or a link right after it, so "Sources: two of them agree" is still said.
_SOURCES = re.compile(
    r"(?i)(?:^|(?<=[\s.!?:;,)\]]))(?:sources?|references|citations|further\s+reading|links)\s*:\s*"
    r"(?:\n|[-*\u2022]\s|\[|https?://|www\.|\d+[.)]\s)"
)
# ...and one that the reply ends on, with nothing after it yet.
_SOURCES_AT_END = re.compile(r"(?i)(?:^|(?<=[\s.!?:;,)\]]))(?:sources?|references|citations|links)\s*:\s*$")
# A place on the screen given as numbers ("at about (980, 326)", "x 980, y 326", "pixel 412"): her own aim for a click,
# never something the user needs to hear. Small pairs ("the point (3, 4)") and years ("(2024, 2025)") aren't one.
_SPOT_PAIR = re.compile(r"\(\s*(\d{1,5})\s*,\s*(\d{1,5})\s*\)")
_SPOT_XY = re.compile(r"\bx\s*[=:]?\s*\d{2,5}\s*,?\s*(?:and\s+)?y\s*[=:]?\s*\d{2,5}\b", re.IGNORECASE)
_SPOT_PIXEL = re.compile(r"\bpixels?\s*\(?\d{2,5}", re.IGNORECASE)
# An interjection a reply opens with, after any cues ("[excited] Oh, you passed?"): only with its comma, so "Oh no."
# and "Yeah right." are left alone.
_OPENER = re.compile(
    r"^(\s*(?:\[[^\]]+\]\s*)*)(oh|ooh|yeah|yep|yup|okay|ok|alright|so|well|hmm|sure|nope|nah|huh|right),\s+",
    re.IGNORECASE,
)
_AFTER_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")  # where one sentence ends and the next starts, keeping its stop
# A pair with one small number ("(45, 600)") is a screen spot only next to words about the screen, not in a lesson.
_ON_SCREEN = re.compile(
    r"\b(?:box|button|field|click|clicking|tap|screen|window|cursor|icon|menu|tab|link|located|sits|top|bottom|left|right)\b",
    re.IGNORECASE,
)


def gives_screen_spot(text: str) -> bool:
    """Whether ``text`` says where something is on the screen in numbers (its spoken part: a [[shown|said]] mark's
    shown side can hold a formula)."""
    said = said_form(text)
    for m in _SPOT_PAIR.finditer(said):
        a, b = int(m.group(1)), int(m.group(2))
        if 1900 <= a <= 2100 and 1900 <= b <= 2100:
            continue  # years
        if min(a, b) >= 100 or (max(a, b) >= 100 and _ON_SCREEN.search(said)):
            return True
    return bool(_SPOT_XY.search(said) or _SPOT_PIXEL.search(said))


# A take tagged with herself ("..., which is basically my whole personality.", "Pretty much my kind of food."): the
# same filler every time a favourite comes up (2026-11-22, the user: "odd and generic"). The take is said; the tag isn't.
_ME = (
    r"(?:basically |pretty much |kind of |kinda |literally |just |totally |so )?"
    r"(?:me(?: in (?:one|a) (?:word|nutshell|\w+))?|my whole (?:personality|philosophy|thing|vibe|brand|deal)"
    r"(?: in (?:one|a) \w+)?|my (?:spirit animal|love language|kind of \w+|whole \w+)|right up my alley)"
)
_SELF_TAG_CLAUSE = re.compile(
    rf",\s*(?:and |which |that )(?:that's |that is |is |'s )?{_ME}(?=\s*[.!?…,]|\s*$)", re.IGNORECASE
)
# A whole sentence only with a hedge in it ("Pretty much my kind of food."): "That's my kind of music." is a take.
_SELF_TAG_SENTENCE = re.compile(
    rf"^\s*(?:\[[^\]]+\]\s*)?(?:that's |which is |it's )?(?=basically |pretty much |kind of |kinda |literally |totally )"
    rf"{_ME}[.!?…]*\s*$",
    re.IGNORECASE,
)


# An answer as a label and a comma ("Brisket, no contest.", "Gold coins, easy."): the user heard it as a tic. The
# label is said on its own ("Brisket."), and the next sentence (held with it: a one-word opener) carries the why.
_LABEL_COMMA = re.compile(
    r"^(\s*(?:\[[^\]]+\]\s*)?(?!(?i:okay|ok|alright|yeah|yep|yes|no|nope|oh|sure|hey|whoa|wait|i|i'd|i'm|it's|that's|"
    r"you|we)\b)(?:[A-Z][\w'-]*)(?: [\w'-]+){0,4}), (?:no contest|easy|easily|hands down|no question|"
    r"no doubt|all day)([.!])(\s*)$"
)


# Hedges tacked onto a take (2026-11-22, the user): "is basically" on every comparison ("is basically medieval drag
# racing"), and "honestly" before or after an opinion ("I love it, honestly."), which reads as a sign she's lying.
# Not where "basically" means "almost" ("That's basically right" in a lesson is "nearly right", not "right").
_BASICALLY = re.compile(
    r"\b((?:is|it's|that's|was|are|they're|he's|she's|which's|'s)\s+)basically\s+"
    r"(?!(?:right|correct|it|there|done|finished|the same|true|over|ready|all|everything|nothing|empty|full|free|"
    r"zero|gone|set|fine|wrong|same|idle|no|none|never|always|impossible|unchanged|untouched|equal|identical|even|"
    r"flat|tied|instant|free)\b)",
    re.IGNORECASE,
)
_HONESTLY_LEAD = re.compile(r"^(\s*(?:\[[^\]]+\]\s*)?)honestly,?\s+(\w)", re.IGNORECASE)
_HONESTLY_TAIL = re.compile(r",\s*honestly(?=\s*[.!?…]|\s*$)", re.IGNORECASE)


_HONESTLY_MID = re.compile(r"(,|\band\b|\bbut\b)\s+honestly,?\s+", re.IGNORECASE)  # "..., and honestly that's ..."
_BASICALLY_LEAD = re.compile(r"^(\s*(?:\[[^\]]+\]\s*)?)basically,?\s+(?=(?:the|a|an)\s)", re.IGNORECASE)


def without_hedges(sentence: str) -> str:
    """``sentence`` without a hedge on a take: "it's basically X" is "it's X", "Honestly, I love it" is "I love it"."""
    sentence = _HONESTLY_TAIL.sub("", _BASICALLY.sub(r"\1", sentence))
    sentence = _HONESTLY_MID.sub(lambda m: m.group(1) + " ", sentence)
    sentence = _BASICALLY_LEAD.sub(lambda m: m.group(1) + "\0", sentence)
    sentence = re.sub("\0(.)", lambda m: m.group(1).upper(), sentence)
    return _HONESTLY_LEAD.sub(lambda m: m.group(1) + m.group(2).upper(), sentence)


def without_self_tag(sentence: str) -> str:
    """``sentence`` without a tag that ties the take back to her ("" when the whole sentence is one), a label answer
    without its ", no contest", and no hedge on a take (``without_hedges``)."""
    if _SELF_TAG_SENTENCE.match(sentence):
        return ""
    return without_hedges(_LABEL_COMMA.sub(r"\1\2\3", _SELF_TAG_CLAUSE.sub("", sentence)))


_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff\U0000fe0f\U0000200d]", flags=re.UNICODE
)


def clean_for_speech(text: str, allowed_tags: frozenset[str] | set[str] = frozenset()) -> str:
    """Strip what shouldn't be read aloud. ``allowed_tags`` are [cues] the TTS engine performs. A [[shown|said]]
    mark (``tts/shown.py``) is read as its spoken part."""
    text = said_form(text)
    text = _CONTROL.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _BRACKET.sub(lambda m: m.group(0) if m.group(1).strip().lower() in allowed_tags else "", text)
    text = _URL.sub("the link", text)
    text = _HEADING.sub("", text)
    text = _LIST_PREFIX.sub("", text)
    text = _MD_MARKS.sub("", text)
    text = _EMOJI.sub("", text)
    text = expand_texting_abbreviations(text)
    return " ".join(text.split())


class SentenceChunker:
    def __init__(
        self,
        min_chars: int = 40,
        max_chars: int = 280,
        first_min_chars: int = 1,
        allowed_tags: frozenset[str] | set[str] = frozenset(),
        pause_tags: frozenset[str] | set[str] = frozenset(),
        first_min_words: int = 1,
        inline_tags: frozenset[str] | set[str] = frozenset(),
        first_max_chars: int | None = None,
        avoid_start: str | None = None,
    ) -> None:
        # The word her last reply opened with ("oh"): this one doesn't open with it too (``_open``).
        self.avoid_start = avoid_start
        self.start_word: str | None = None  # what this reply opened with ("" for no interjection), once it has
        self._opened = False
        self.allowed_tags = allowed_tags
        self.pause_tags = pause_tags
        self.inline_tags = inline_tags  # moment cues (a laugh, a breath) that may also go inside a sentence
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.first_min_chars = first_min_chars
        self.first_min_words = first_min_words
        self.first_max_chars = first_max_chars
        self._buf = ""
        self._pending = ""  # complete sentences held back until long enough
        self._emitted = 0  # segments since the start or the last flush (0: the next one opens)
        self._muted = False  # a list of sources began: nothing after it is spoken

    def feed(self, delta: str) -> list[str]:
        if self._muted:
            return []
        self._buf += delta
        if (sources := _SOURCES.search(self._buf)) is not None:
            self._buf = self._buf[: sources.start()]
            self._muted = True
        out: list[str] = []
        while True:
            cut = self._find_boundary(self._buf)
            if cut is None:
                break
            sentence, self._buf = self._buf[:cut], self._buf[cut:]
            if gives_screen_spot(sentence):
                log.info("Not saying a screen position: %r", sentence.strip())
                continue
            if (untagged := without_self_tag(sentence)) != sentence:
                log.info("Not saying a self-tag: %r", sentence.strip())
                if not untagged.strip():
                    continue
                sentence = untagged + (" " if sentence[-1:].isspace() else "")
            out.extend(self._accept(sentence))
        # A long opener: speak up to its last clause boundary now instead of waiting for the full stop (not a lone
        # "Well," though: that would be followed by a gap).
        if self._emitted == 0 and self.first_max_chars and len(self._buf) > self.first_max_chars:
            cut = self._find_clause(self._buf)
            if cut >= self.first_max_chars // 2:
                piece, self._buf = self._buf[:cut], self._buf[cut:]
                out.extend(self._accept(piece, force=True))
        # Split run-on text that has no sentence boundary yet (never inside a mark: one still being written is waited
        # for, up to a point).
        while len(self._buf) > self.max_chars:
            start = open_mark(self._buf)
            if start is not None and start < self.max_chars:
                break
            cut = self._find_clause(self._buf[: self.max_chars]) or self._buf.rfind(" ", 0, self.max_chars)
            if cut <= 0:
                cut = self.max_chars
            if (span := inside(mark_spans(self._buf), cut)) is not None:
                cut = span[0] if span[0] > 0 else span[1]
            piece, self._buf = self._buf[:cut], self._buf[cut:]
            out.extend(self._accept(piece, force=True))
        return out

    def flush(self) -> list[str]:
        """Release everything buffered (at tool calls and at the end of a reply)."""
        if (sources := _SOURCES_AT_END.search(self._buf)) is not None:
            self._buf = self._buf[: sources.start()]
        self._muted = False  # the list is over: what comes after the next step (or a question) is spoken
        rest = self._buf
        if gives_screen_spot(rest):
            log.info("Not saying a screen position: %r", rest.strip())
            rest = " ".join(part for part in _AFTER_SENTENCE.split(rest) if not gives_screen_spot(part))
        if (untagged := " ".join(filter(None, map(without_self_tag, _AFTER_SENTENCE.split(rest))))) != " ".join(
            _AFTER_SENTENCE.split(rest)
        ):
            log.info("Not saying a self-tag: %r", rest.strip())
            rest = untagged
        raw = self._pending + " " + rest
        text = self._limit_cues(clean_for_speech(raw, self.allowed_tags))
        if not self._word_count(text):
            text = ""  # a cue with nothing after it has nothing to colour
        self._pending = ""
        self._buf = ""
        self._emitted = 0  # a flush means a pause (a step, a question): what comes next opens again
        return [self._spoken(self._open(text), raw)] if text else []

    def _accept(self, piece: str, force: bool = False) -> list[str]:
        if self._pending and beats_in(piece) and clean_for_speech(self._pending, self.allowed_tags):
            # A beat starts its own segment, so its step plays as that sentence starts, not with the one before.
            held, self._pending = self._pending, ""
            return [*self._accept(held, force=True), *self._accept(piece, force)]
        combined = (self._pending + " " + piece) if self._pending else piece
        cleaned = clean_for_speech(combined, self.allowed_tags)
        if not cleaned:
            self._pending = combined if beats_in(combined) else ""  # a beat on a line of its own: with what follows
            return []
        if self._emitted == 0:
            short = len(cleaned) < self.first_min_chars or self._word_count(cleaned) < self.first_min_words
        else:
            short = len(cleaned) < self.min_chars
        if short and not force:
            self._pending = combined
            return []
        cleaned = self._limit_cues(cleaned)
        if not self._word_count(cleaned):
            self._pending = combined  # only cues, on a line of their own: they go with what follows
            return []
        self._pending = ""
        self._emitted += 1
        return [self._spoken(self._open(cleaned), combined)]

    def _open(self, said: str) -> str:
        """The reply's first segment, without the interjection her last reply opened with too ("Oh, ..." twice in a
        row was a tic once fillers and cues came in, 2026-11-22). Later segments pass as they are."""
        if self._opened:
            return said
        self._opened = True
        m = _OPENER.match(said)
        self.start_word = m.group(2).lower() if m else ""
        if m is None or not self.avoid_start or self.start_word != self.avoid_start or m.end() >= len(said):
            return said
        rest = said[m.end() :]
        log.info("Not opening with %r twice in a row", m.group(2))
        return m.group(1) + rest[:1].upper() + rest[1:]

    @staticmethod
    def _spoken(said: str, raw: str) -> Spoken:
        """The segment as said, carrying how her subtitles show it and its beats (``tts/shown.py``)."""
        return Spoken(said, clean_for_screen(shown_form(raw)) or said, beats_in(raw))

    @staticmethod
    def _word_count(text: str) -> int:
        """Words actually spoken: tone cues like "[sigh]" don't make a one-word opener any longer."""
        return len(_BRACKET.sub("", text).split())

    def _limit_cues(self, text: str) -> str:
        """Keep at most one tone cue at the start of each sentence (pauses aside), and at most one moment cue
        (``inline_tags``: a laugh, a breath, a stressed word) inside it.

        A cue restyles the sentence it starts, so a restyling one in the middle of a sentence, or a second at its
        start, makes the voice lurch. A moment cue sits at the word it happens on instead. Several sentences of one
        reply may each have their cues.
        """
        parts: list[str] = []
        pos = 0
        started = False  # the sentence has words before this cue
        cued = False  # the sentence already has its cue
        moment = False  # ...and its moment cue inside it
        for m in _BRACKET.finditer(text):
            between = text[pos : m.start()]
            parts.append(between)
            pos = m.end()
            ends = list(_SENTENCE_END.finditer(between))
            if ends:
                started, cued, moment = False, False, False
                between = between[ends[-1].end() :]
            started = started or bool(between.strip())
            tag = m.group(1).strip().lower()
            if tag not in self.allowed_tags or tag in self.pause_tags:
                parts.append(m.group(0))
            elif not started and not cued:
                cued = True
                parts.append(m.group(0))
            elif started and not moment and tag in self.inline_tags:
                moment = True
                parts.append(m.group(0))
        parts.append(text[pos:])
        return " ".join("".join(parts).split())

    def _find_boundary(self, text: str) -> int | None:
        spans = mark_spans(text)
        for m in _BOUNDARY.finditer(text):
            if inside(spans, m.end()) is not None:
                continue  # inside a mark: "$x = 1.$ " or a line break in TeX isn't the end of her sentence (".]] " is)
            if m.group().startswith("\n"):
                return m.end()
            if m.group() == "." and self._not_a_sentence_end(text, m.start()):
                continue
            return m.end()
        return None

    @staticmethod
    def _not_a_sentence_end(text: str, dot_index: int) -> bool:
        start = dot_index
        while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "."):
            start -= 1
        word = text[start:dot_index]
        if word.lower() in _ABBREVIATIONS:
            return True
        # "1. First item": a list number at the start of a line.
        line_start = text.rfind("\n", 0, start) + 1
        if word.isdigit() and not text[line_start:start].strip():
            return True
        # Initials like "J. R. R. Tolkien".
        return len(word) == 1 and word.isupper()

    @staticmethod
    def _find_clause(text: str) -> int:
        last = 0
        spans = mark_spans(text)
        for m in _CLAUSE.finditer(text):
            if inside(spans, m.start()) is None:
                last = m.end()
        return last
