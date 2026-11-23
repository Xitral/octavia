"""Reading a spoken or typed answer to "Want me to ...?" as a yes, a no, or neither."""

from __future__ import annotations

import re

from octavia.core.text import normalize_text

# A yes, and a plain "carry on" to a step of work she asked about ("continue", "keep going", "send it"). "go" counts
# only last ("yeah, go", "let's go"): "go back" says where to go instead.
_YES = re.compile(
    r"\b(yes|yeah|yep|yup|sure|do it|go ahead|confirm(ed)?|affirmative|please do|ok(ay)?|absolutely|of course|correct|"
    r"sounds good|go for it|that works|works for me|mhm+|uh huh|totally|definitely|alright|all right|fine|"
    r"continue|carry on|keep going|go on|proceed|send it|submit it|go$)\b"
)

# Phrases with a "no" or "not" in them that are yeses ("yeah, why not", "no problem"): taken out before looking for a no.
_HARMLESS_NO = re.compile(r"\b(?:why not|no problem|no worries|not a problem|no prob|(?:don'?t|do not) mind)\b")

_NO = re.compile(
    r"\b(no|not|nope|nah|don'?t|do not|cancel|stop|never ?mind|negative|wait|hold on|hang on|later)\b|n't\b"
)

# Words that can surround a yes without changing it. Anything else means the reply
# says more than yes (e.g. "okay, what's the weather?"), which is not consent.
# Pressing a yes, not taking it back: "Yes, I'm right here, you don't need to ask", "yes, stop asking", a reply
# cut off at "yes... you don't". Taken out of an answer that starts with a yes before looking for a no.
_INSISTS = re.compile(
    r"\b(?:you\s+)?(?:don'?t|do\s+not)\s+(?:even\s+)?(?:need|have)\s+to\s+(?:ask|check|confirm)(?:\s+me)?\b"
    r"|\b(?:don'?t|do\s+not|stop)\s+(?:keep\s+)?ask(?:ing)?(?:\s+me)?\b|\bno\s+need\s+to\s+ask\b"
    r"|\bwhy\s+(?:are|do)\s+you\s+(?:keep\s+)?ask(?:ing)?\b|\byou\s+(?:don'?t|do\s+not)(?:\s+\w+)?\s*$"
)

# Words after a yes that steer the step somewhere else: "yes, but the other file", "yeah, also run it", "sure, wrong
# one though" (``yes_and_more``).
_STEERS = re.compile(r"\b(?:but|except|instead|also|first|before|wrong|other|another|different|else|though)\b")

_YES_FIRST = frozenset({"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "absolutely", "yea", "ya"})

_YES_FILLER = {
    "yes", "yeah", "yep", "yup", "sure", "do", "it", "go", "ahead", "confirm", "confirmed", "affirmative",
    "please", "ok", "okay", "absolutely", "of", "course", "correct", "right", "that's", "thats", "exactly",
    "definitely", "totally", "sounds", "good", "great", "fine", "thanks", "thank", "you", "just", "so", "well",
    "um", "uh", "oh", "and", "then", "now", "thing", "one", "that", "this", "yea", "ya", "mhm", "for", "works", "me",
    "huh", "alright", "all", "perfect", "cool", "awesome", "continue", "carry", "on", "keep", "going", "proceed",
    "send", "submit", "with", "let's", "lets",
}  # fmt: skip

# Words of a reply saying the yes was given already, when she missed it ("That was a yes.", "I said yes.", "The answer
# is yes."): 2026-11-04 four of those were taken as unclear, and each paused a screen task.
_SAID_A_YES = frozenset({
    "i", "said", "say", "was", "is", "a", "the", "there", "there's", "already", "answer", "my", "again", "told", "it's",
    "its", "i've", "that's",
})  # fmt: skip

# The words of a plain no to her question and nothing more: "no", "nope, skip it", "no thanks", "not now, leave it",
# "don't send it", "don't continue".
PLAIN_NO_WORDS = frozenset({
    "no", "nope", "nah", "not", "now", "yet", "don't", "do", "it", "that", "this", "one", "skip", "please", "thanks",
    "thank", "you", "uh", "um", "oh", "negative", "never", "mind", "cancel", "just", "leave", "for", "continue",
    "proceed", "send", "submit", "go", "ahead",
})  # fmt: skip


def parse_confirmation(text: str) -> bool | None:
    """True for a clear yes, False for a no, None when unclear (the caller re-asks).

    Any negation wins ("absolutely not" is a no). A yes only counts when the reply
    is essentially just a yes: "okay, what's the weather" is a new request, not consent.
    """
    norm = " ".join(_HARMLESS_NO.sub(" sure ", normalize_text(text)).split())
    if not norm:
        return None
    if norm.split()[0] in _YES_FIRST and not _NO.search(rest := _INSISTS.sub(" ", norm)):
        if rest != norm:  # a yes, then "you don't need to ask": a yes said impatiently
            return True
    if _NO.search(norm):
        return False
    if not _YES.search(norm):
        return None
    extra = [w for w in norm.split() if w not in _YES_FILLER and w not in _SAID_A_YES]
    return True if len(extra) <= 1 else None


def yes_and_more(text: str) -> bool:
    """A reply that opens with a yes and goes on without taking it back or steering elsewhere: "Yes, paste it into
    the terminal", "Yeah, do the next file". To her question about a screen task's step it's a yes to that step, the
    words after it only saying it again; read as the user stepping in, it paused the task after every answer. Anything
    with a no, a "but", "instead", "also" or a question in it isn't one."""
    if "?" in text:
        return False
    norm = " ".join(_HARMLESS_NO.sub(" sure ", normalize_text(text)).split())
    words = norm.split()
    if not words or words[0] not in _YES_FIRST:
        return False
    rest = _INSISTS.sub(" ", norm)
    return not _NO.search(rest) and not _STEERS.search(rest)


def plain_answer(text: str) -> bool | None:
    """An answer to her question about a step, said or typed, as the one reading both: True for a yes or a plain
    "carry on" ("Continue.", "Yeah, go."), False for a plain no ("nope, skip it"), None for anything more (a
    correction such as "no, the answer is B", a question, another request), which is the user's turn of its own."""
    verdict = parse_confirmation(text)
    if verdict is False and not set(normalize_text(text).split()) <= PLAIN_NO_WORDS:
        return None
    return verdict
