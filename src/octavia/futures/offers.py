"""Offers of help before she's asked: "Your history essay is due at 11 tonight. Want me to pull up the rubric?"

Plain deterministic code, like the rest of Futures: an offer is built from what the world model, the app in front and
the calendar already show, with no model call, and goes through the proactive gate (``proactive.py``) like any remark
of hers. Quiet hours, focus mode and Do Not Disturb, the budgets, cooldowns and mutes all apply, plus a smaller
budget of its own (``offers_per_day``), and the same offer is never made twice for one deadline or one meeting.

An offer only ever asks. Nothing here acts: a yes goes to the brain as an ordinary turn, with a note saying what was
offered, so whatever the help needs runs through the tool executor like any request. Anything risky (a submit, a
send, a delete) is still asked there. The ``permissions.Offer`` it's announced with names only a read-only tool
(``OFFER_TOOLS``), which never asks anyway: it keeps the yes away from her tool-less quick voice and approves nothing.

The moments that make an offer:

* an assignment Canvas still shows open, due within ``offer_assignment_h`` hours;
* the same, earlier, when the user brings a writing app (``work_apps``) to the front while one is due within a day:
  the moment they're likely starting it;
* a meeting with an online link starting within ``offer_meeting_min`` minutes (``sources.CalendarWatch``).

How each one went is kept on its recommendation: "yes" → accepted, "not now" → deferred (the moment was wrong, so it
counts toward no mute), "no" → rejected, the help used without a yes → followed, no answer within ten minutes →
ignored. ``take_up`` turns that history into how relevant the next offer of its kind is, so a kind the user keeps
waving off fades below the gate's threshold by itself, and one yes brings it back. The ``offers`` report
(``octavia futures report offers``) has the numbers.

Anything else that wants to bring something up on its own (a thought from her reflections, say) hands a ``Candidate``
to ``FuturesService.suggest``: one way to speak up, through the same gate, never a second one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from octavia.core.text import normalize_text
from octavia.futures.explain import Trace
from octavia.futures.proactive import OFFER_PREFIX, RESPOND_WINDOW_S, Candidate
from octavia.futures.schema import Policy

if TYPE_CHECKING:
    from octavia.futures.sources import Upcoming
    from octavia.futures.store import FuturesStore
    from octavia.futures.world import Entity

ASSIGNMENT = OFFER_PREFIX + "assignment"
MEETING = OFFER_PREFIX + "meeting"
OFFER_KINDS = (ASSIGNMENT, MEETING)
# The (read-only) tool each kind offers to use. Announced as the offer, so a plain "yes" goes to the brain, which has
# the tools, rather than to her quick voice; a read-only call needs no approval, so it approves nothing either.
OFFER_TOOLS = {ASSIGNMENT: "canvas_assignment", MEETING: "get_calendar_events"}
ANSWER_WINDOW_S = RESPOND_WINDOW_S  # no answer to an offer within this: ignored
WORKING_AHEAD_S = 24 * 3600.0  # a writing app brought to the front offers help with work due within this
WORKING_FRESH_S = 30 * 60.0  # ...and that offer is only worth saying this soon after the app came up
RETRY_AFTER_S = 7 * 86400.0  # a kind that faded gets one more chance when none was made for this long
MEETING_GRACE_S = 5 * 60.0  # a meeting-link offer still makes sense this long after it started
TAKE_UP_WINDOW_S = 30 * 86400.0  # how far back the take-up of a kind is counted
TAKEN = frozenset({"accepted", "followed", "engaged"})
NOT_TAKEN = frozenset({"rejected", "ignored"})  # "deferred" is neither: wrong moment, not a wrong idea
# Using the help an offer was about, without saying yes first, counts as taking it up.
FOLLOW_TOOLS = {
    ASSIGNMENT: frozenset({"canvas_assignment", "canvas_course_material"}),
    MEETING: frozenset({"open_url", "get_calendar_events"}),
}

# What there is to pull up for each kind of Canvas work.
_WHAT = {"quiz": "what it covers", "discussion": "the prompt"}

# Short answers to an offer. "Not now" is looked for first: "no, not now" and "yeah, later" are both a later.
_NOT_NOW = re.compile(
    r"\b(?:not now|not right now|not yet|not at the moment|not today|later|maybe later|in a bit|in a minute|"
    r"another time|some other time|remind me later|after this)\b"
)
_FAREWELL = re.compile(r"\b(?:see you|talk to you|catch you|bye)\b")
# Phrases with a "no" in them that answer nothing ("no worries"), and a moot offer ("no, I already did it"): neither a
# yes nor a turn-down of the idea.
_NEITHER = re.compile(r"\b(?:no worries|no problem|no prob|no clue|no idea|already|i did|done it|finished it)\b")
_NEGATION = re.compile(r"\b(?:no|not|nope|nah|dont|do not|never|stop|cancel)\b")
_NO = re.compile(r"^(?:no+|nope|nah|no thanks?|no thank you|im good|i'm good|im fine|dont|do not|pass|skip it)\b")
_YES = re.compile(
    r"^(?:yes|yeah|yep|yup|ya|sure|please|ok|okay|alright|all right|go ahead|do it|go for it|absolutely|definitely|"
    r"of course|sounds good|why not|that would be great|thatd be great|that would help|pull it up|open it)\b"
)
_MAX_ANSWER_WORDS = 8  # longer than this is a new request, not an answer


def classify_answer(text: str) -> str | None:
    """A short answer to an offer: "yes", "not_now" or "no"; None for anything else (a new request, a question)."""
    words = normalize_text(text).replace("'", "")
    if not words or len(words.split()) > _MAX_ANSWER_WORDS or _NEITHER.search(words) or _FAREWELL.search(words):
        return None
    if _NOT_NOW.search(words):
        return "not_now"
    if _NO.match(words):
        return "no"
    if _YES.match(words):
        # "please don't", "yeah no", "ok no thanks": a yes-word up front with a no after it is no clear yes.
        return None if _NEGATION.search(words) else "yes"
    return None


def is_work_app(app: str, process: str, names: Iterable[str]) -> bool:
    """Whether the app in front is one the user writes in (by whole words of its display or program name, so "word"
    is Microsoft Word, not 1Password)."""
    seen = normalize_text(f"{app} {process}".replace(".", " "))
    for name in names:
        wanted = normalize_text(name)
        if wanted and re.search(rf"\b{re.escape(wanted)}\b", seen):
            return True
    return False


def take_up(store: FuturesStore, kind: str, now: float) -> tuple[float, int, int]:
    """How often offers of ``kind`` were taken up lately: (the Beta(1,1) estimate, taken, not taken), from the last 30
    days. With no history the estimate is a half. An unanswered one counts half a miss (the user may just have been
    away), a turned-down one a whole. And when none of the kind was made for a week, the estimate is a half again: a
    kind that faded gets another chance, or a "yes" could never bring it back."""
    rows = store.execute(
        "SELECT response, delivered_at FROM recommendations WHERE kind=? AND status='delivered' AND delivered_at>=?",
        (kind, now - TAKE_UP_WINDOW_S),
    )
    taken = sum(1 for r in rows if r["response"] in TAKEN)
    not_taken = sum(1 for r in rows if r["response"] in NOT_TAKEN)
    misses = sum(1.0 if r["response"] == "rejected" else 0.5 for r in rows if r["response"] in NOT_TAKEN)
    last = max((r["delivered_at"] or 0.0 for r in rows), default=0.0)
    if rows and now - last >= RETRY_AFTER_S:
        return 0.5, taken, not_taken
    return (taken + 1) / (taken + misses + 2), taken, not_taken


def relevance_scale(estimate: float) -> float:
    """What the take-up does to an offer's relevance: little at first, then more. Each offer waved off or left
    unanswered makes the next one wait until its deadline is nearer, and four in a row with nothing taken up put it
    under the gate's threshold altogether (unanswered ones count half), until a week without one gives it another
    chance and a yes brings it back."""
    return max(0.0, min(1.0, 2.5 * estimate))


def _taken_line(taken: int, not_taken: int) -> str:
    if not taken and not not_taken:
        return "no earlier offers of this kind to go on"
    return f"earlier offers like it: {taken} taken up, {not_taken} turned down or unanswered (last 30 days)"


def assignment_offer(
    entity: Entity,
    *,
    now: float,
    when: str,
    course: str = "",
    working: bool = False,
    horizon_h: float = 4.0,
    scale: float = 1.0,
    history: tuple[int, int] = (0, 0),
) -> Candidate:
    """An offer to pull up what an assignment needs. ``when`` is its due time as it's said ("at 11 PM"); ``working``:
    made because the user just brought a writing app to the front."""
    due = float(entity.due_ts or now)
    kind = str(entity.attrs.get("kind") or "assignment")
    what = _WHAT.get(kind, "the rubric")
    title = entity.name or "your assignment"
    if working:
        # The soonest work due is a guess at what they're starting, so it's said as one.
        text = f"\"{title}\" is due {when}. If that's what you're starting, want me to pull up {what}?"
        urgency, relevance = 0.7, 0.9
    else:
        hours_left = max(0.0, (due - now) / 3600)
        text = f'"{title}" is due {when}. Want me to pull up {what}?'
        urgency = 0.5 + 0.5 * max(0.0, min(1.0, 1 - hours_left / max(horizon_h, 0.25)))
        relevance = 0.8
    of_course = f" ({course})" if course else ""
    return Candidate(
        kind=ASSIGNMENT,
        key=f"{ASSIGNMENT}:{entity.ref}@{due:.0f}",
        text=text,
        policy=Policy.ASK,
        urgency=urgency,
        relevance=relevance * scale,
        domain=entity.domain or "school",
        sensitivity=entity.sensitivity or "personal",
        subject=entity.ref,
        note=(
            f'(Octavia Futures) You just offered, unprompted, to pull up {what} for the Canvas {kind} "{title}"'
            f"{of_course}, due {when}. On a yes, get it with canvas_assignment and give them what matters. On a no or "
            "a not now, just say okay: don't offer again."
        ),
        trace=Trace(
            observations=(
                {"event": entity.last_event or "", "what": f'"{title}" is due {when}', "at": now},
                *(({"event": "", "what": "a writing app just came to the front", "at": now},) if working else ()),
            ),
            assumptions=(
                "Canvas's last look (hourly) showed it not handed in yet",
                *(("the soonest work due is what you're starting",) if working else ()),
                _taken_line(*history),
            ),
            confidence="moderate",
        ).to_dict(),
        # Made because a writing app came up, it's only timely for a while; otherwise until it's due.
        expires_at=min(due, now + WORKING_FRESH_S) if working else due,
        listen=True,
    )


def meeting_offer(
    item: Upcoming, *, now: float, at: str, scale: float = 1.0, history: tuple[int, int] = (0, 0)
) -> Candidate:
    """An offer to open an online meeting's link a few minutes before it starts. ``at`` is its start as it's said
    ("3:00 PM"): a clock time stays right however long the offer waits for a moment to be said."""
    starts = f"started at {at}" if item.start <= now else f"starts at {at}"
    title = item.title or "Your meeting"
    return Candidate(
        kind=MEETING,
        key=f"{MEETING}:{item.key}",
        text=f'"{title}" {starts}. Want me to open the meeting link?',
        policy=Policy.ASK,
        urgency=0.8,
        relevance=0.8 * scale,
        domain=item.domain,
        sensitivity=item.sensitivity,
        subject=None,
        note=(
            f'(Octavia Futures) You just offered, unprompted, to open the online meeting link for "{title}", which '
            f"{starts}. On a yes, get_calendar_events shows the event with its meeting link: open that with "
            "open_url. On a no or a not now, just say okay."
        ),
        trace=Trace(
            observations=({"event": "", "what": f'"{title}" on the calendar has an online meeting link', "at": now},),
            assumptions=("the calendar is up to date (it's read every 15 minutes)", _taken_line(*history)),
            confidence="strong",
        ).to_dict(),
        expires_at=item.start + MEETING_GRACE_S,
        listen=True,
    )


@dataclass(frozen=True)
class KindTally:
    """How the offers of one kind went, for the report."""

    kind: str
    made: int = 0
    accepted: int = 0
    followed: int = 0
    deferred: int = 0
    rejected: int = 0
    ignored: int = 0
    held: tuple[tuple[str, int], ...] = ()  # (reason, count) of those never said

    @property
    def taken(self) -> int:
        return self.accepted + self.followed


def tally(store: FuturesStore, now: float, kinds: Sequence[str] = OFFER_KINDS) -> list[KindTally]:
    """Per kind of offer, over the last 30 days: made, and how each was taken; and why the rest weren't made."""
    since = now - TAKE_UP_WINDOW_S
    out = []
    for kind in kinds:
        counts: dict[str, Any] = {"made": 0}
        for row in store.execute(
            "SELECT response, COUNT(*) AS n FROM recommendations WHERE kind=? AND status='delivered' "
            "AND delivered_at>=? GROUP BY response",
            (kind, since),
        ):
            counts["made"] += row["n"]
            response = row["response"]
            if response in ("accepted", "engaged"):
                counts["accepted"] = counts.get("accepted", 0) + row["n"]
            elif response in ("followed", "deferred", "rejected", "ignored"):
                counts[response] = counts.get(response, 0) + row["n"]
        held = store.execute(
            "SELECT reason, COUNT(*) AS n FROM recommendations WHERE kind=? AND status IN ('suppressed','expired') "
            "AND created_at>=? GROUP BY reason ORDER BY n DESC, reason",
            (kind, since),
        )
        out.append(KindTally(kind=kind, held=tuple((r["reason"] or "", r["n"]) for r in held), **counts))
    return out
