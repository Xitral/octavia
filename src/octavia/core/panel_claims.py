"""She never says a panel is up before it is.

Her answers go out as she writes them, and her prompt asks for words first and the panel call last, so she can say
"It's now a 3D game, round three on the panel" while still writing the program for it. On 2026-11-22 she said just
that, the program took long enough that the user spoke over her, and no panel ever came up; it had happened before
(a 3D map-coloring panel "that got cancelled"). So a sentence that says a panel is up, or what's newly on it, is held
until one of her panel calls in this turn has come back done. Everything she says after it waits with it, so nothing
is said out of order. When the turn ends with no panel call done, the claim is dropped and she says plainly that the
panel didn't come up.

A sentence about a panel that was already up ("WA is red on the panel") isn't held: only one that says the panel
changed ("I've put it on the panel", "it's now a 3D game on the panel") is, or any when no panel is up at all.
Offers and plans ("want me to put it on a panel?", "I'll put it up") claim nothing.

While a panel is up, a sentence saying how things stand now ("SA is now green") is what the panel should show too:
she said "SA is now green" with the 3D map unchanged. It's held the same way, but never dropped: if no panel call
comes back done, it's said, and she adds that the panel doesn't show it yet. Only while they study at the screen:
elsewhere "your build is now ready" has nothing to do with a pinned panel.

Both are kinds of claim for ``claim_hold.ClaimHold``, the one hold for everything she says she did: stricter than its
general kind (any call of hers that changes something bears out "I've done it"), because "on the panel" says which
call it needs.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from octavia.core.claim_hold import ACTION, ClaimHold, Held, Kind, Turn
from octavia.core.claim_hold import without_dropped as without_dropped
from octavia.core.claims import Act, Claim

# The calls that put something on one of her panels.
PANEL_CALL = re.compile(
    r"(?:^|__)(?:show_visual|update_visual|explain|simulate|concept_map|practice_problem|practice_worksheet|"
    r"walk_through|show_image|show_page|bring_back_visual)$"
)
_PANEL = re.compile(
    r"\b(?:panels?|on (?:the|your|my) screen|on screen|up (?:there|for you)|pulled (?:it |that )?up|popped (?:it |that )?up)\b",
    re.IGNORECASE,
)
# ...saying it's there now, or that she just put it there.
_NEW = re.compile(
    r"\b(?:now|I've|I have|I just|I put|I made|I turned|I added|I changed|I updated|I drew|I built|I set|I threw|"
    r"I pulled|I popped|I swapped|I opened|it's up|is up|are up|went up|here's|here it is|there it is)\b",
    re.IGNORECASE,
)
# ...not an offer or a plan.
_NOT_YET = re.compile(
    r"\b(?:I'll|I will|I'm going to|I am going to|going to|gonna|let me|want me|should I|shall I|can I|could I|"
    r"would you like|do you want|if you want|I can|I could|next time)\b",
    re.IGNORECASE,
)
# How things stand now, said while a panel is up: the panel should show it too.
_STATE = re.compile(r"\b(?:is|are|'s|'re|has|have) now\b|\bnow (?:is|are|has|have|shows?)\b", re.IGNORECASE)
NOT_UP = "Actually, the panel didn't come up, so it isn't on your screen."
NOT_SHOWN = "The panel doesn't show that yet, though."


def says_state(sentence: str, panels_up: bool) -> bool:
    """Whether ``sentence``, said while a panel is up, says how things stand now (what the panel should show)."""
    return panels_up and bool(_STATE.search(sentence)) and not _NOT_YET.search(sentence)


def says_panel_up(sentence: str, panels_up: bool) -> bool:
    """Whether ``sentence`` says a panel is up (or newly shows something). With ``panels_up`` (one of hers is on
    screen already), only a sentence saying it changed counts."""
    if not _PANEL.search(sentence) or _NOT_YET.search(sentence):
        return False
    return not panels_up or bool(_NEW.search(sentence))


def _says_up(sentence: str, turn: Turn) -> Claim | None:
    return Claim("panel", sentence.strip()) if says_panel_up(sentence, turn.panels_up) else None


def _says_state(sentence: str, turn: Turn) -> Claim | None:
    return Claim("state", sentence.strip()) if says_state(sentence, turn.panels_up) else None


def _panel_bears_out(held: Held, act: Act) -> bool:
    return act.ok and bool(PANEL_CALL.search(act.name))


# Kinds of ``claim_hold.ClaimHold``: "it's on the panel" is borne out by a panel call alone, and with none it isn't up
# (dropped, said so); how things stand now, while studying with a panel up, waits the same way but is never dropped.
PANEL_UP = Kind("panel_up", _says_up, _panel_bears_out, lambda held, turn: (False, NOT_UP))
PANEL_STATE = Kind("panel_state", _says_state, _panel_bears_out, lambda held, turn: (True, NOT_SHOWN), ("panel_up",))


def kinds(states: bool = True, actions: bool = True) -> tuple[Kind, ...]:
    """The kinds of claim she's held to in a turn, most specific first: panels (how things stand too, with
    ``states``), then any action (with ``actions``: ``claims.mode`` enforce)."""
    return (PANEL_UP, *((PANEL_STATE,) if states else ()), *((ACTION,) if actions else ()))


class PanelClaimHold(ClaimHold):
    """``ClaimHold`` with the panel kinds alone: ``panel_done`` when a panel call of hers has come back done."""

    def __init__(
        self, feed: Callable[[str], None], panels_up: Callable[[], bool] = lambda: False, states: bool = True
    ) -> None:
        super().__init__(feed, (PANEL_UP, PANEL_STATE) if states else (PANEL_UP,), panels_up=panels_up)

    def panel_done(self) -> None:
        """A panel call of hers came back done: what was held is true now, and goes out."""
        self.finished(Act("update_visual", True))
