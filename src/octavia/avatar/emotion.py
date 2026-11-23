"""What the avatar's face and body should do while a sentence is spoken.

The brain doesn't send emotions, so they are read off the sentence itself. A Fish Audio voice cue such as
``[excited]`` is the strongest signal: the model chose it on purpose and the voice will act it out, so the face has
to match. Without one, a few cheap text heuristics (greetings, apologies, "haha", questions, exclamations...) pick a
mood. Everything is a table so tuning means editing data, not logic.

Emotion and gesture are chosen independently: the emotion comes from the first rule that sets one and the gesture
from the first rule that sets one, so "No, sorry." is sad *and* shakes the head.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# What the avatar app understands (docs/avatar-protocol.md).
EMOTIONS = frozenset("neutral happy angry sad relaxed surprised excited smug laughing curious playful thinking".split())
GESTURES = frozenset(
    "wave nod shake shrug laugh fist_pump hands_on_hips surprised_jolt sigh think_chin spin impatient".split()
)

NEUTRAL_INTENSITY = 0.2
EXCLAIM_BOOST = 0.05  # per "!" (up to three) on top of whatever emotion was chosen


@dataclass(frozen=True)
class Mood:
    emotion: str
    intensity: float  # 0..1
    gesture: str | None = None


@dataclass(frozen=True)
class _Effect:
    emotion: str | None  # None: leave the emotion to the next rule
    intensity: float
    gesture: str | None = None


# Voice cues the TTS performs. None: a pacing cue, which says nothing about mood.
CUES: dict[str, _Effect | None] = {
    "excited": _Effect("excited", 0.85, "fist_pump"),
    "laughing": _Effect("laughing", 0.9, "laugh"),
    "chuckling": _Effect("laughing", 0.55, "laugh"),
    "sigh": _Effect("sad", 0.35, "sigh"),
    "surprised": _Effect("surprised", 0.8, "surprised_jolt"),
    "sarcastic": _Effect("smug", 0.6),
    "confident": _Effect("smug", 0.55, "hands_on_hips"),
    "proud": _Effect("happy", 0.75, "hands_on_hips"),
    "playful": _Effect("playful", 0.7),
    "whispering": _Effect("relaxed", 0.5),
    "emphasis": _Effect(None, 0.0, "nod"),
    "inhale": _Effect(None, 0.0),  # a breath before a longer thought: no mood, no gesture
    "pause": None,
    "short pause": None,
    "break": None,
}

_START = r"^\W*(?:(?:oh|ah|well)\W+)?"  # a rule anchored here only fires on how the sentence opens

# Checked in order. Earlier rules win the emotion (and, separately, the gesture) over later ones.
TEXT_RULES: tuple[tuple[str, _Effect], ...] = (
    (r"\b(?:a?ha(?:ha)+h?|he(?:he)+|lol|lmao|rofl)\b", _Effect("laughing", 0.7, "laugh")),
    # Dismay first: "Oh no!" is no happy exclamation (the no/nope rule below still adds its head shake).
    (r"\b(?:oh no|uh[- ]oh|oh dear|yikes)\b", _Effect("sad", 0.5)),
    # "No way" only as an exclamation opening the sentence: "there's no way to undo that" is bad news, not a surprise.
    (
        r"\b(?:wow|whoa|woah|oh my (?:god|gosh|goodness))\b|" + _START + r"no way\b",
        _Effect("surprised", 0.7, "surprised_jolt"),
    ),
    (
        r"\b(?:sorry|apologi[sz]e|my apologies|unfortunately|sadly|fail(?:ed|s|ing|ure)?|didn't work|bad news|"
        r"no way to)\b"
        # "I couldn't ..." is bad news when something didn't get done, not "I couldn't be happier" or "agree more".
        r"|\bi (?:couldn't|could not|wasn't able to|was unable to)\b(?! (?:be|agree|have|ask|wish)\b)",
        _Effect("sad", 0.5),
    ),
    (r"\b(?:hm+|let me think|let me see|let's see|let me check|one moment)\b", _Effect("thinking", 0.55, "think_chin")),
    (
        # Greetings open the sentence; farewells open or close it ("I'll take care of it", "I can see you're
        # on YouTube" and the song "See You Again" wave at nobody).
        _START + r"(?:hi|hello|hey|howdy|greetings|good (?:morning|afternoon|evening)|welcome back)\b"
        r"|" + _START + r"(?:bye|goodbye|good ?night|see ya|see you|take care)\b(?!')(?!\s+(?:again|of|to)\b)"
        r"|\b(?:bye|goodbye|good ?night|see ya|see you(?: later| soon| tomorrow| then| around)?|take care)\W*$",
        _Effect("happy", 0.6, "wave"),
    ),
    (r"\b(?:awesome|amazing|congrat(?:s|ulations)|fantastic)\b", _Effect("excited", 0.65)),
    # Showing off: a spin on the spot.
    (
        r"\b(?:nailed it|called it|too easy|like a boss|still got it|am i good or what|(?:twenty|20) ?(?:percent|%) cooler)",
        _Effect("smug", 0.65, "spin"),
    ),
    (
        r"\b(?:obviously|easy|easily|of course|naturally|nailed it|piece of cake|no sweat)\b",
        _Effect("smug", 0.5),
    ),
    (r"!.*!", _Effect("excited", 0.7)),
    (r"\b(?:thanks|thank you|you're welcome|my pleasure|glad|great|nice|lovely)\b", _Effect("happy", 0.5)),
    (r"!", _Effect("happy", 0.5)),
    (r"\?", _Effect("curious", 0.5)),
    (_START + r"(?:no|nope|nah)\b(?!\s+(?:problem|worries|way|doubt))", _Effect(None, 0.0, "shake")),
    (
        _START + r"(?:yes|yeah|yep|yup|sure|done|okay|ok|alright|all right|absolutely|certainly|definitely|"
        r"will do|on it|no problem|no worries)\b|\b(?:got it|you got it|all set|all done|sounds good)\b",
        _Effect("happy", 0.3, "nod"),
    ),
)

_RULES = tuple((re.compile(pattern, re.IGNORECASE), effect) for pattern, effect in TEXT_RULES)
_CUE = re.compile(r"\[([^\[\]]{1,40})\]")


# Voice cues that mean she's shouting, and text that looks shouted: two or more words in capitals ("LET'S GO"), or
# one long one with an exclamation mark ("AWESOME!"). The avatar grabs a megaphone for it.
_SHOUT_CUES = frozenset(
    {"shouting", "shouts", "shout", "yelling", "yells", "yell", "screaming", "screams", "loud", "loudly"}
)
_CAPS_RUN = re.compile(r"\b[A-Z][A-Z']{2,}(?:[ ,]+[A-Z][A-Z']*)+\b")
_CAPS_WORD = re.compile(r"\b[A-Z]{5,}!")


def shouts(text: str) -> bool:
    """Whether ``text``, one spoken sentence with any voice cues, is shouted: a shouting cue, or capitals."""
    for match in _CUE.finditer(text):
        if " ".join(match.group(1).lower().split()) in _SHOUT_CUES:
            return True
    spoken = _CUE.sub(" ", text)
    return _CAPS_RUN.search(spoken) is not None or _CAPS_WORD.search(spoken) is not None


# What the user said that leaves her in a mood for a while (the avatar's resting face): thanks and praise leave her
# happy, teasing leaves her smug. First match wins.
USER_MOODS: tuple[tuple[str, str, float], ...] = (
    (
        r"\b(?:thanks?|thank you|thx|ty|cheers|good (?:job|work)|great (?:job|work)|nice (?:job|work|one)|well done|"
        r"you(?:'re| are) (?:the best|awesome|amazing|a genius|so cool|so smart)|you rock|love (?:you|it)|perfect)\b",
        "happy",
        0.45,
    ),
    (
        r"\b(?:slow ?poke|you(?:'re| are) (?:slow|wrong|lame)|so slow|took you long enough|nice try|bet you can't|"
        r"i(?:'m| am) faster|dork|egghead|loser|show[- ]off)\b",
        "smug",
        0.5,
    ),
)
_USER_MOODS = tuple(
    (re.compile(pattern, re.IGNORECASE), emotion, intensity) for pattern, emotion, intensity in USER_MOODS
)


def user_mood_for(text: str) -> tuple[str, float] | None:
    """The mood ``text``, something the user said, leaves her in: (emotion, intensity), or None."""
    text = text.replace("’", "'")
    for pattern, emotion, intensity in _USER_MOODS:
        if pattern.search(text):
            return emotion, intensity
    return None


def mood_for(text: str) -> Mood:
    """The emotion (and gesture) to show while ``text``, one spoken sentence with any voice cues, plays."""
    text = text.replace("’", "'").replace("‘", "'")
    emotion: str | None = None
    intensity = 0.0
    gesture: str | None = None
    for match in _CUE.finditer(text):
        cue = CUES.get(" ".join(match.group(1).lower().split()))
        if cue is not None:
            emotion, intensity, gesture = cue.emotion, cue.intensity, cue.gesture
            break
    spoken = _CUE.sub(" ", text).strip()
    for pattern, effect in _RULES:
        if emotion is not None and gesture is not None:
            break
        if (emotion is None and effect.emotion) or (gesture is None and effect.gesture):
            if not pattern.search(spoken):
                continue
            if emotion is None and effect.emotion:
                emotion, intensity = effect.emotion, effect.intensity
            if gesture is None and effect.gesture:
                gesture = effect.gesture
    if emotion is None:
        return Mood("neutral", NEUTRAL_INTENSITY, gesture)
    intensity += EXCLAIM_BOOST * min(spoken.count("!"), 3)
    return Mood(emotion, round(min(max(intensity, 0.0), 1.0), 2), gesture)
