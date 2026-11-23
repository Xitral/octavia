"""What the assistant's parts share: the listening modes, what waits in line to be said or run, a turn's
result, where a turn came from, and a few notes for the brain."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum

from octavia.tools.permissions import ConfirmationRequest, Offer


class ListenMode(Enum):
    OFF = "off"
    WAKE = "wake"
    CAPTURE = "capture"
    BARGE = "barge"


@dataclass
class Announcement:
    text: str
    note: str | None = None
    listen: bool = True
    cue: str | None = None  # what kind it is (AnnouncementStarted)
    offer: Offer | None = None  # the one call it offers to make, which a plain yes in the next turn approves
    full: str | None = None  # the whole of it, when ``text`` says only part (shown in the chat window)


@dataclass
class TurnResult:
    text: str = ""
    interrupted: bool = False
    barge_in: str | None = None  # "wake" | "voice" when the user cut in
    error: str | None = None


@dataclass
class BackgroundTurn:
    """A turn waiting in line with the announcements: one nobody asked for just now (a scheduled job), or one the
    user typed somewhere else (``source`` "discord": a text from their phone, ``Assistant.run_from``)."""

    text: str
    note: str | None
    speak: bool
    result: asyncio.Future[TurnResult]
    source: str = "scheduled"
    started: bool = False  # taken out of the line and running
    stop_wanted: bool = False  # a texted stop came for it (``stop_from``): it's stopped at the first chance
    cue: str | None = None  # what kind of remark of hers it is ("morning"), for the avatar, as an announcement's


@dataclass
class WorkQuestion:
    """A risky step of work running beside the conversation (a screen task typing in a terminal), waiting in line
    with the announcements to be put to the user (``Assistant.ask_for_work``). ``answer`` gets their yes or no, and
    never a no for silence (then it's held open); cancelled, the question is taken back."""

    request: ConfirmationRequest
    lead: str  # what it's for, said before the question ("For “the Redshift lab”: ")
    answer: asyncio.Future[bool]
    # When nobody answered it out loud and it was held open (``Assistant._hold_open``): a yes or no said or typed
    # later answers it. None while it's still being asked.
    held_at: float | None = None


# Turns that carry the user's own words (said, typed, or texted from their phone over the Discord bridge, or typed
# in her chat window).
_USER_SOURCES = frozenset({"voice", "text", "discord", "window"})

# Of those, the ones typed somewhere else. They're the user's own turns, but a message can be typed by whoever holds
# their phone, and they can't see the screen: their words never stand in for a "yes" (``requested``), no earlier yes
# or standing approval carries over (``already_approved``), and every question goes back where the turn came from
# (``remote_confirmers``). AUTO calls still go ahead (``unattended``).
_REMOTE_SOURCES = frozenset({"discord"})

# Turns typed somewhere other than her voice loop: queued behind what she's doing (``run_from``), answered where they
# came from (no follow-up listening; the reply read all at once), and her questions asked there (``remote_confirmers``).
# Her chat window ("window", ``core/chat.py``) is one, but it's at the PC: the user's words count there as said ones do.
_ROUTED_SOURCES = _REMOTE_SOURCES | {"window"}

# Lines said out loud that aren't the conversation's turns (``Assistant._aside``): her approval questions and the
# answers to them, a quick "Got it.". Whoever follows the conversation (her chat window) shows them; the transcript
# doesn't keep them.
ASIDE = "aside"


def _new_future() -> asyncio.Future[TurnResult]:
    return asyncio.get_running_loop().create_future()


def _resolve(future: asyncio.Future | None, value: object) -> None:
    if future is not None and not future.done():
        future.set_result(value)


# Told to the brain with every turn texted over the Discord bridge (``bridge/discord_bridge.py``): the reply is read on
# a phone, not heard. Here so the local brain (``localbrain/delegation.py``) knows it's only context.
TEXTED_NOTE = (
    "the user texted this from their phone over Discord, away from the PC: your reply goes back to them as a text "
    "message, not spoken. Text like yourself, the way a friend texts: short, same attitude as out loud; links are "
    "fine, a short list or bold only when it helps, no emoji unless they use them. Anything "
    "that needs their yes is asked in that chat for you, so just make the call (don't ask first in your reply). "
    "send_to_phone sends them files"
)

# Cut off in the middle of her steps: whatever the user says next comes first, and carrying the old request through
# (as the system prompt says to) would go on editing their slides while they talk about something else.
_ON_HOLD_NOTE = (
    "you were cut off while still working on the user's earlier request: that work is on hold. Respond to what they "
    "say now, and don't take any more steps on the earlier request until they ask you to carry on with it"
)
# The user asking which model (or what) answered them: the brain then gets told (``_answered_by``).
_ASKS_WHO_ANSWERED = re.compile(
    r"\b(?:models?|which\s+ai|what\s+ai|who\s+(?:just\s+)?(?:answered|replied|spoke))\b", re.I
)
