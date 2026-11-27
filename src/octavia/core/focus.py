"""What she keeps to herself while the user is in the middle of something with her.

On 2026-11-23 at 2:45 PM the user was studying Bayes' rule with her and thinking over her question ("Which piece of
Bayes' rule is the 0.9 we already know?") when she announced the night shift's report on her own: 107 turns gone
through, 23 kept you waiting. The self-improvement news had been held while they talked (``ManagerHooks.busy``),
but only for three minutes of quiet, and thinking over a question is quiet too.

So every line she'd bring up on her own (status news, her own ideas: ``Line.kind`` STATUS or INITIATIVE, not urgent)
waits while the user is focused (``AnnouncingMixin.focused``): a study session running, a question of hers still
waiting for its answer, or whatever else the app says is focus (``focus_probes``). It goes through once that's over,
checked every ``CHECK_S``, or right away in their own turn when they ask for it ("any news?", "what did you do last
night?", "what were you going to say?"): then the brain is given it to tell them (``held_note``), and it isn't said
again on its own. What they asked for (a timer, a reminder), anything urgent and replies are never held.
"""

from __future__ import annotations

import re

CHECK_S = 15.0  # how often lines held for focus look for it being over
# Her question still waits for an answer this long at most: after that, the silence isn't them thinking it over.
QUESTION_S = 5 * 60.0

_ASKS = re.compile(
    r"\b(?:any|the|your|some)\s+(?:news|updates?|reports?)\b"
    r"|\bnight\s*shift\b|\bovernight\b|\blast night\b"
    r"|\bwhat(?:'s| is| was)? new\b"
    r"|\bwhat did you (?:do|find|fix|work on|get done|notice)\b"
    r"|\bwhat were you (?:saying|going to say|about to say|gonna say|trying to say)\b"
    r"|\byou were (?:saying|going to say|about to say|gonna say)\b"
    r"|\banything (?:new|to tell|for me|to report)\b"
    r"|\b(?:ok(?:ay)?|go ahead|fine)[, ]+(?:tell me|say it)\b"
    r"|\btell me (?:now|what you (?:were|wanted))\b",
    re.IGNORECASE,
)

# An offer of hers ("Want me to install them?") isn't something to think over: news may come after it as before.
_OFFER = re.compile(
    r"^\W*(?:so |okay |ok |and |or )?(?:do you |did you )?(?:want|wanna|would you like|should i|shall i|can i|may i|"
    r"okay if i|ready (?:for|to)|how about|what about|anything else|any other|sound good|does that sound)\b",
    re.IGNORECASE,
)

NOTE = (
    "while the user was {why} you held back what you'd have brought up on your own; they're asking for it now, so "
    "tell them, briefly and in your own words: {lines}"
)


def asks_for_held(text: str) -> bool:
    """Whether the user's words ask for what she held back ("any news?", "what were you going to say?")."""
    return bool(_ASKS.search(text or ""))


def held_note(why: str, lines: list[str]) -> str:
    """The brain's note for a turn that asks for what she held back."""
    return NOTE.format(why=why or "busy with you", lines=" | ".join(f'"{line}"' for line in lines))


def thinking_over(questions: list[str]) -> bool:
    """Whether the last thing she put to the user (``questions_in``'s newest) is a question to think over ("Which
    piece of Bayes' rule is the 0.9?"), not an offer to take or leave ("Want me to read it?")."""
    return bool(questions) and not _OFFER.search(questions[-1])
