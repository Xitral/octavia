"""What her experiments on herself may change, in fixed words, and when a result is good enough to keep.

Her experiments on herself (``selfscore``) vary one thing about how she works and count how the user takes it. This
file is everything they're allowed to say or change: the note a turn gets for each variant, which tones count as the
user sounding stressed, how much later a "late" heads-up comes, what she tells the user when she keeps something,
and the rule that decides whether a result becomes a habit. Nothing here is written by a model, and no build can
change it (``evolve.policy`` FORBIDDEN): a self-build can't make a habit say something new or keep one on weaker
evidence.
"""

from __future__ import annotations

from collections.abc import Mapping

from octavia.futures.schema import STRENGTH_ORDER, Strength

# Her experiments on herself, in the order she runs them (one at a time).
BRIEF_WHEN_STRESSED = "brief_when_stressed"
ANSWER_FIRST = "answer_first"
HEADS_UP_TIMING = "heads_up_timing"
SELF_TEMPLATES = (BRIEF_WHEN_STRESSED, ANSWER_FIRST, HEADS_UP_TIMING)
TURN_TEMPLATES = frozenset({BRIEF_WHEN_STRESSED, ANSWER_FIRST})  # varied turn by turn, through the turn's notes

# Tones (``emotion.tone``) that count as the user sounding stressed.
STRESSED_TONES = frozenset({"stressed", "frustrated", "anxious", "annoyed"})
TONE_FRESH_S = 10.0  # a tone heard this recently is the one of the utterance being answered

# The note a turn gets for a variant. A variant with no note is how she usually does it. Every note starts with
# NOTE_PREFIX, which the local brain's router knows as context only (``localbrain.delegation``): a note about how
# to word the answer never moves a simple command off the local model, so both groups are answered alike.
NOTE_PREFIX = "for this answer: "
NOTES: Mapping[tuple[str, str], str] = {
    (BRIEF_WHEN_STRESSED, "brief"): NOTE_PREFIX
    + (
        "they sound stressed right now, so keep it to one or two short sentences with the answer itself first, and "
        "offer more only if they ask (a task's steps and checks still happen as usual)"
    ),
    (ANSWER_FIRST, "answer_first"): NOTE_PREFIX
    + (
        "start with the answer or the result itself in your first few words, with no preamble or restating the "
        "request (never say something is done before it is: a task's steps and checks still happen as usual)"
    ),
}

# A heads-up in the "late" group (or once that's a habit) waits until this share of the usual lead time is left:
# with the usual 3 hours, about 1 hour before the to-do is due.
LATE_SHARE = 1 / 3

# What a kept habit is called when it's listed, and what she says when she keeps it.
HABIT_NAMES: Mapping[tuple[str, str], str] = {
    (BRIEF_WHEN_STRESSED, "brief"): "keeping answers short when you sound stressed",
    (ANSWER_FIRST, "answer_first"): "leading with the answer",
    (HEADS_UP_TIMING, "late"): "giving to-do heads-ups about an hour before they're due instead of three",
}


def habit_name(key: str, variant: str) -> str:
    return HABIT_NAMES.get((key, variant), f"{key.replace('_', ' ')} ({variant.replace('_', ' ')})")


def keeping_text(key: str, variant: str, summary: str) -> str:
    """What she says when an experiment's result becomes a habit."""
    return (
        f"I tested {habit_name(key, variant)} on myself. {summary} So I'm keeping it. "
        "If you'd rather I didn't, say \"drop that habit\"."
    )


def adoptable(key: str, variants: tuple[str, ...], result: Mapping[str, object]) -> str | None:
    """The variant a completed experiment's result says to keep, or None. Only one of her own experiments, only the
    change being tried (the first variant: the second is how she already works), and only when the result supports
    it with at least moderate evidence; an unclear result, or one where the usual way did as well, changes nothing."""
    if key not in SELF_TEMPLATES or len(variants) < 2:
        return None
    if result.get("supported") is not True:
        return None
    try:
        strength = Strength(str(result.get("strength") or ""))
    except ValueError:
        return None
    if STRENGTH_ORDER[strength] < STRENGTH_ORDER[Strength.MODERATE]:
        return None
    return variants[0] if (key, variants[0]) in HABIT_NAMES else None
