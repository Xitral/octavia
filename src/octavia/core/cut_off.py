"""Hearing "shut up" when speech recognition didn't: the stop said over her, however it came out of the transcript.

On 2026-11-23, studying Bayes' rule, the user told her to shut up three times while she talked, and speech
recognition wrote it down as "Good pop.", "Start off." and "Or up". Each went to the brain as a request: she
apologized, updated the panel and carried on with the lesson. Only "shut up" written exactly as such held her
(``core/hush.py``), so a stop that's misheard was never one.

What's said over her is the one place a misheard stop is likely, and it shows in more than the words:

* **how it sounds**: the words as sounds (``core/by_ear.py``: spelling to phonemes, the t of "shut up" flapped as
  it's said), against each way of saying stop (``_STOPS``), with swaps that cost what the sounds differ by. "Or up"
  is the tail of "shut up"; "good pop" has its beats and its closing "p". No list of mishearings: any transcript
  that sounds enough like a stop counts.
* **when it's said** (``Moment``): over her (a barge-in), the sooner after she began the likelier, again after another
  short cut-in, in a frustrated voice, or cutting off news of hers nobody asked for. The less of that there is, the
  closer it has to sound; not said over her, only a stop as such (or spelt oddly: "shuttup") counts.
* **what it isn't**: more than three words, an answer or go-ahead ("got it", "go on", a number), a question ("wait,
  what?"), a correction or answer by how it opens ("not that one", "the top one"), or a request that sounds close
  ("start over", "set up") stays the user talking to her.

``judge`` gives the verdict; the assistant then treats it as a "shut up" said plainly: her voice already stopped at
the barge-in, the turn never reaches the brain, and she stays quiet until the user talks to her again
(``AnnouncingMixin._hush``).
"""

from __future__ import annotations

from dataclasses import dataclass

from octavia.core.by_ear import alike, beats, is_vowel, sounds
from octavia.core.text import normalize_text

# The ways of telling her to stop talking, as said. Each is compared as a whole, and its tail on its own too (a misheard
# stop often loses its start: "or up" for "shut up").
_STOPS = (
    "shut up", "shut it", "shut up shut up", "stop", "stop it", "stop talking", "stop stop", "be quiet", "quiet",
    "enough", "hush", "shush", "zip it", "quit it", "stop that", "shut the hell up", "shut your mouth",
)  # fmt: skip
# Short things said over her that are the user talking to her, never a stop, however they sound: answers, go-aheads,
# acknowledgments, and requests that sound close to a stop ("start over", "look it up", "speed up").
_NOT_STOPS = frozenset({
    "yes", "yeah", "yep", "yup", "ya", "no", "nope", "nah", "okay", "ok", "k", "sure", "right", "true", "false",
    "correct", "wrong", "what", "huh", "hm", "hmm", "uh huh", "mhm", "mm hmm", "oh", "ah", "wait", "why", "how",
    "really", "cool", "nice", "great", "good", "good job", "good point", "good one", "nice one", "got it", "gotcha",
    "i got it", "i see", "makes sense", "that makes sense", "go on", "go ahead", "keep going", "continue", "carry on",
    "next", "again", "start over", "start again", "speed up", "slow down", "look it up", "look up", "pick it up",
    "sounds good", "thank you", "thanks", "of course", "exactly", "i know", "i don't know", "not sure", "maybe",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "zero", "half", "a half",
    "what's up", "sup", "hello", "hi", "hey", "bye", "please", "sorry", "my bad", "back up", "scroll up",
    "turn it up", "louder", "softer", "shut down", "shut it down", "start it", "stop the music", "set up", "sit up",
    "speak up", "pick up", "wake up",
})  # fmt: skip
# A question has one of these somewhere in it ("wait, what?", "but why"); no stop does.
_ASKING = frozenset({"what", "what's", "why", "how", "when", "where", "who", "which", "whose", "huh"})
# What a question, a correction or an answer opens with ("is it b", "not that one", "the top one"): no stop does.
_OPENS_OTHER = frozenset({
    "is", "are", "can", "could", "would", "will", "do", "does", "did", "not", "no", "the", "a", "an", "my", "your",
    "this", "these", "those", "it's",
})  # fmt: skip
_MAX_WORDS = 3  # a stop is short; anything longer is the user saying something
# "<something to do> it": a go-ahead ("read it", "send that"), unless the doing is stopping ("shut it", "zip it").
_OBJECTS = frozenset({"it", "that", "this", "them", "one"})
_STOP_VERBS = frozenset({"shut", "zip", "quit", "stop", "cut", "can", "drop", "knock"})
_MAX_AUDIO_S = 2.5  # of speech in it: "shut up" said once or twice, not a sentence

# How close it has to sound (``likeness``, 0..1) with nothing but its sound to go on: a stop as such, or spelt oddly.
_ALONE = 0.97
# Said over her: how much less alike each sign lets it be. With all of them (right as she began an announcement nobody
# asked for, in a frustrated voice, again) a short cut-in only has to sound a little like a stop; with none, nearly
# exactly. Tuned on 2026-11-23's "Good pop." (0.58), "Start off." (0.66) and "Or up" (0.82) against ordinary cut-ins
# ("good job" 0.46, "good luck" 0.54, "got it" 0.81): ``tests/test_cut_off.py`` holds both lists.
_OVER_HER = 0.93
_EARLY = 0.06  # within ``_EARLY_S`` of her starting to talk
_EARLY_S = 6.0
_AGAIN = 0.12  # another short cut-in over her came within ``AGAIN_S``
AGAIN_S = 40.0
_FRUSTRATED = 0.12  # their voice sounded frustrated, angry or stressed, confidently
_FRUSTRATED_TONES = frozenset({"frustrated", "angry", "stressed", "annoyed"})
_FRUSTRATED_MIN = 0.5
_UNASKED = 0.20  # what she was saying was hers, not asked for (an announcement, a remark of her own)
_FLOOR = 0.5  # however much else points at a stop, it must sound at least this much like one
_RHYTHM = 0.06  # as many beats (vowel sounds) as a stop, ending on its last sound
_TAIL_LESS = 0.08  # matched against only the tail of a stop: that much less sure


@dataclass(frozen=True)
class Moment:
    """When something was said, as far as telling a stop goes."""

    over_her: bool = False  # it cut in while she was talking (a barge-in)
    into_s: float | None = None  # how long she'd been talking when it cut in
    again: bool = False  # another short cut-in over her came shortly before (``AGAIN_S``)
    tone: str = ""  # the voice tone's reading ("frustrated"), "" when there's none
    tone_confidence: float = 0.0
    audio_s: float | None = None  # how much speech the utterance had
    unasked: bool = False  # what it cut into was hers, not asked for (``Line.kind`` STATUS or INITIATIVE)


@dataclass(frozen=True)
class Verdict:
    stop: bool
    likeness: float  # how much it sounds like a stop, 0..1
    needed: float  # how much it had to, given the moment
    nearest: str = ""  # the way of saying stop it sounds most like
    why: str = ""

    def __str__(self) -> str:
        return f"{'stop' if self.stop else 'no stop'}: sounds {self.likeness:.2f} like {self.nearest!r} (needed {self.needed:.2f}; {self.why})"


def likeness(text: str) -> tuple[float, str]:
    """How much ``text`` sounds like a way of saying stop (0..1, ``by_ear``), and which one. One written as such
    is 1. A stop that lost its start ("or up" for "shut up") is matched against the tail of each, a little less sure."""
    words = normalize_text(text)
    if words in _STOPS:
        return 1.0, words
    heard = sounds(words)
    best, nearest = 0.0, ""
    for stop in _STOPS:
        said = sounds(stop)
        score = alike(heard, said)
        if 3 <= len(heard) < len(said):
            score = max(score, alike(heard, said[-len(heard) :]) - _TAIL_LESS)
        if beats(heard) == beats(said) and heard[-1] == said[-1] and not is_vowel(said[-1]):
            score += _RHYTHM
        if score > best:
            best, nearest = score, stop
    return round(min(1.0, best), 3), nearest


def judge(text: str, moment: Moment) -> Verdict:
    """Whether ``text``, said at ``moment``, was the user telling her to stop talking. ``text`` is what
    ``addressed`` returns (her name and the politeness around it gone)."""
    words = normalize_text(text)
    count = len(words.split())
    if not words:
        return Verdict(False, 0.0, 1.0, why="nothing said")
    if words in _NOT_STOPS:
        return Verdict(False, 0.0, 1.0, why="an answer or request, not a stop")
    if words.split()[0] in _OPENS_OTHER or _ASKING.intersection(words.split()):
        return Verdict(False, 0.0, 1.0, why="a question, correction or answer, not a stop")
    if count > _MAX_WORDS:
        return Verdict(False, 0.0, 1.0, why=f"{count} words: too long for a stop")
    if moment.audio_s is not None and moment.audio_s > _MAX_AUDIO_S:
        return Verdict(False, 0.0, 1.0, why=f"{moment.audio_s:.1f} s of speech: too long for a stop")
    if not moment.over_her:
        # Not over her, by ear is too loose ("top" for "stop", "ship it" for "zip it"): only a stop, or one spelt
        # oddly ("shuttup", "shut op"), counts.
        nearest = spelt_like(words)
        return Verdict(bool(nearest), 1.0 if nearest else 0.0, 1.0, nearest, "not said over her: spelling only")
    words_said = words.split()
    if len(words_said) >= 2 and words_said[-1] in _OBJECTS and words_said[0] not in _STOP_VERBS:
        return Verdict(False, 0.0, 1.0, why="something to do to it ('read it', 'send that'), not a stop")
    score, nearest = likeness(words)
    needed = _ALONE
    signs = []
    if moment.over_her:
        needed = _OVER_HER
        signs.append("said over her")
        if moment.into_s is not None and moment.into_s <= _EARLY_S:
            needed -= _EARLY
            signs.append(f"{moment.into_s:.1f} s after she began")
        if moment.again:
            needed -= _AGAIN
            signs.append("a cut-in again")
        if moment.tone in _FRUSTRATED_TONES and moment.tone_confidence >= _FRUSTRATED_MIN:
            needed -= _FRUSTRATED
            signs.append(f"sounding {moment.tone}")
        if moment.unasked:
            needed -= _UNASKED
            signs.append("cutting off something nobody asked for")
    needed = round(max(_FLOOR, needed), 3)
    why = ", ".join(signs) or "not said over her"
    return Verdict(score >= needed, score, needed, nearest, why)


def _letters(text: str) -> str:
    out: list[str] = []
    for ch in text.replace(" ", "").replace("'", ""):
        if not out or ch != out[-1]:
            out.append(ch)
    return "".join(out)


def spelt_like(text: str) -> str:
    """The stop ``text`` is spelt like (its letters, doubles as one, the same or one vowel off, five letters or
    more: "shuttup", "shut op", "shot up"), or "" for none."""
    heard = _letters(normalize_text(text))
    for stop in _STOPS:
        said = _letters(stop)
        if heard == said:
            return stop
        if len(said) >= 5 and len(heard) == len(said):
            off = [(a, b) for a, b in zip(heard, said, strict=True) if a != b]
            if len(off) == 1 and off[0][0] in "aeiou" and off[0][1] in "aeiou":
                return stop
    return ""


def short_cut_in(text: str) -> bool:
    """Whether ``text`` said over her was short enough to count toward a stop said again (``Moment.again``)."""
    count = len(normalize_text(text).split())
    return 0 < count <= _MAX_WORDS
