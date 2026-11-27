"""What a turn knows when the user is studying at the screen.

She can draw anything on a panel (``show_visual``: code, a table in Markdown, a scene of cells she can highlight), and
her prompt says to. What she lacked on 2026-11-01 was knowing it applied right now: after her context started afresh
she asked "which cells can be computed first?" of a table the user had never seen, by voice, to be worked out in their
head. So each turn while they're studying at the screen says so. It's context, not a script: what goes on the panel,
and what to ask, stay hers.

Every step she explains and every question she asks goes on a panel, whatever it's about: the note used to ask for one
only when the question had code, a table, a grid or steps, and on 2026-11-21 she taught map coloring by voice ("SA is
green or blue, which one?", "how many colors does NT have left?") with nothing on screen, though the user had said
many times they need to see it. When a turn of theirs still ends with a question or an explanation and no panel call
of hers (``panel_owed``), her next turn is told to put it up first.

Studying doesn't mean they're at the screen, though: on 2026-11-02 the user said they were in the shower, away from
it, and the note told her they were at it, so she offered problems on a panel they couldn't see. So what they say
about being away (``AwayFromScreen``) holds until they say they're back, or type, which only someone there can do.
On 2026-11-21 "I'm going into the shower" wasn't heard as away (only "in the shower" was), so the note said "at the
screen" again and she put Bayes' formula on a panel: the ways of saying it cover going to shower, and heading out.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

NOTE = (
    "the user is studying {course} with you, at the screen: they can see your panels, learn from what they see, and "
    "can't hold much in their head. Every reply that explains a step or asks them something puts it on a panel in "
    "that same reply, whatever the topic, without being asked: change the one that's up with update_visual, the part "
    "you mean highlighted, or show_visual when none fits. Say your words first, question included, and make the panel"
    " call last, so they hear you at once; talk about the topic, not the panel, since it isn't up yet, and never tell"
    " them it's on the panel or how to answer on it (they watched it change). Ask one small thing about what's on it:"
    " one cell, one line, not the whole problem"
)
# Her next turn's note when the last one explained or asked with nothing put on a panel.
OWED_NOTE = (
    "your last reply explained or asked something with nothing on a panel, and the user needs to see it: in this "
    "reply, put it up (update_visual on the panel that's up, or show_visual) with the part you mean highlighted"
)
# Her calls that put something on a panel: any of them counts.
PANEL_CALL = re.compile(
    r"(?:^|__)(?:show_visual|update_visual|explain|simulate|concept_map|practice_problem|practice_worksheet|"
    r"walk_through|show_image|show_page|bring_back_visual)$"
)
_EXPLAINED_WORDS = 30  # a reply this long with no question still explained something
AWAY_NOTE = (
    "the user is studying {course} with you, but said they're away from the screen and haven't said they're back: "
    "they can't see your panels, so don't offer or show one, and keep to what they asked for while away"
)

_SCREEN = r"(?:the |my |your )?(?:screen|computer|pc|desk|monitor|laptop|panels?)"
_AWAY = re.compile(
    rf"\b(?:away from|not at|not in front of|not near|nowhere near|can'?t see|cannot see|can not see) {_SCREEN}\b"
    r"|\b(?:in|into|to|take|taking|have|having|grab|grabbing|getting in(?:to)?) (?:the |a |my )?(?:quick )?"
    r"(?:shower|bath)\b"
    r"|\b(?:going to|gonna|about to|need to|have to|got to|gotta|go) (?:go )?(?:shower|bathe)\b"
    r"|\bi'?m (?:not )?(?:stepping|stepped|walking|walked|going) away\b"
    r"|\b(?:i'?m|i am) (?:heading|going|stepping|popping|running) out\b(?! of)"
    r"|\b(?:brb|be right back)\b",
    re.IGNORECASE,
)
_BACK = re.compile(
    rf"\b(?:i'?m|i am) (?:now |finally )?(?:back|at {_SCREEN}|in front of {_SCREEN})\b"
    r"|\bback (?:at|to|in front of) " + _SCREEN + r"\b"
    r"|\b(?:out of|back from) (?:the |my )?(?:shower|bath)\b",
    re.IGNORECASE,
)
_TYPED = frozenset({"text", "window"})  # typed at the PC: whoever typed it is at the screen
_FORGOTTEN_S = 3 * 3600.0  # a "back" never said: by then they've long come back, or stopped studying


def says_away(text: str) -> bool:
    return bool(_AWAY.search(text))


def says_back(text: str) -> bool:
    return not says_away(text) and bool(_BACK.search(text))


class AwayFromScreen:
    """Whether the user said they're away from the screen and hasn't said they're back since."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._since: float | None = None

    def hear(self, text: str, source: str, keep: bool = True) -> bool:
        """Whether they're away, with the turn of theirs saying ``text`` from ``source``. ``keep`` False only tells:
        what the words say isn't kept (an early start, on words that may not be the turn)."""
        since = self._since
        if since is not None and self._clock() - since > _FORGOTTEN_S:
            since = None
        if says_away(text):
            since = self._clock()
        elif source in _TYPED or says_back(text):
            since = None
        if keep:
            self._since = since
        return since is not None


def panel_owed(reply: str, calls: list[str]) -> bool:
    """Whether a reply of hers while they study at the screen owed them a panel it didn't put up: it asked them
    something or explained at length, and none of ``calls`` (her tool calls in the turn) is a panel's."""
    text = reply.strip()
    if not text or any(PANEL_CALL.search(name) for name in calls):
        return False
    return "?" in text or len(text.split()) >= _EXPLAINED_WORDS


def studying_note(course: str, away: bool = False) -> str:
    return (AWAY_NOTE if away else NOTE).format(course=course)
