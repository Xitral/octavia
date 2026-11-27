"""One check for everything she says she did: a claim waits for the call that bears it out (2026-11-22).

Her answers go out as she writes them, and she writes her words before her calls ("say your words first, make the
call last"), so she can say "I've turned the console effect off" or "It's now a 3D game on the panel" while the call
that would do it hasn't run yet, or never runs, or fails. ``claims.ClaimWatch`` checks a claim against her steps, but
only the steps already done when the sentence is spoken: a claim said ahead of its call had nothing to be checked
against. ``panel_claims`` fixed that for panels alone. This is the general form, for every tool:

* a sentence claiming something is done or changed now (``action_claim``: "I've sent it", "I turned it off",
  "Done.") that nothing of hers bears out yet is **held**, and everything she says after it waits with it, so nothing
  is said out of order;
* each call of hers that finishes is weighed against what's held (``Kind.bears_out``): when every held claim is borne
  out, it all goes out, through the same vetting as ever (``ClaimWatch.vet`` then sees the call that ran);
* a general claim with no call started ``WAIT_FOR_CALL_S`` after it was written goes out as written: she writes her
  words and then the call, so a reply still being written with no call is one with none coming (a story, an answer
  about earlier), and it mustn't wait in silence for the end of it;
* when the turn ends with a claim still held, its kind says what happens (``Kind.at_end``): a general claim whose
  calls after it all failed is said, and she adds plainly that it didn't go through (her next turn is told); one
  ``ClaimWatch`` knows ("I've recorded it") is said and vetted in its own words; one never tried is said as written.
  Nothing she said is lost: only a panel claim is dropped, when no panel came up at all.

What counts as a claim, what bears it out and what happens at the end are a ``Kind``: the general one here (any of her
tools that changes something bears out a claim of a change; any call bears out "I checked"), and the panel ones in
``panel_claims`` (only a panel call bears out "it's on the panel"; no panel call at all means it isn't up), which are
stricter where her words are more specific. A sentence takes the first kind it matches. No model and no waiting
beyond what the call itself takes: a claim borne out when it's said (most are) goes straight out.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from octavia.core.claims import (
    _NOT_A_CLAIM,
    _NOT_SOMEONE,
    _OVERNIGHT,
    _QUOTED,
    _READS,
    _REPORTED,
    READ_VERBS,
    Act,
    Claim,
    claims_of,
    judge,
)

log = logging.getLogger(__name__)

# Calls that are no step of their own (loading a tool's schema).
NEUTRAL = frozenset({"ToolSearch"})
_SENTENCE_END = re.compile(r"[.!?…][\"”')\]]*(?:\s+|$)|\n+")

# A change she says she made, in the first person, done: "I've put", "I just turned", "I swapped it". Any verb, so a
# new tool needs no new word here; only verbs of thinking, feeling and talking (no change to anything) are left out.
_DID = re.compile(
    r"\bI(?:'ve| have)?\s+(?:just\s+|already\s+|now\s+|also\s+|finally\s+|then\s+|went ahead and\s+)?"
    r"([a-z]+ed|put|set|made|built|wrote|written|sent|took|taken|threw|thrown|drew|drawn|gave|given|brought|shut|cut|"
    r"began|begun|hid|hidden|split|spun|stuck|swept)\b",
    re.IGNORECASE,
)
_THINKING = frozenset(
    "noticed figured wondered realized realised guessed loved liked hated missed learned learnt talked asked answered "
    "laughed worried assumed imagined heard mentioned named called used needed wanted tried failed seemed lived enjoyed "
    "agreed decided counted interpreted misread misunderstood confused mixed felt expected hoped meant suggested "
    "explained described covered compared picked preferred appreciated noted bet doubted joked teased recommended "
    "considered remembered judged estimated worked struggled paused stopped watched worded".split()
)  # fmt: skip
# A plan or an offer claims nothing yet.
_PLAN = re.compile(r"\b(?:I'll|I will|I'm going to|going to|gonna|let me|want me|let's)\b", re.IGNORECASE)
# ...and words about what's past: "I've already saved that", "You're right, I messed that up", "a moment ago".
_EARLIER = re.compile(
    r"\b(?:already|earlier|ago|this morning|last (?:time|week|night)|you're right|you were right|my bad|my mistake)\b",
    re.IGNORECASE,
)
_READ_VERBS = READ_VERBS

DIDNT_WORK = "Actually, that didn't go through."
WAIT_FOR_CALL_S = 2.5  # a general claim waits this long for a call to start (her calls came 1.35 s median, 2.4 s p90
# after a claim in 30 days of transcripts, counting the call's own writing), then goes out as written


def action_claim(sentence: str) -> Claim | None:
    """What ``sentence`` claims she did or changed now, if it does: ``claims.claims_of``'s claims ("I sent it",
    "Done."), and a change in the first person with any verb ("I flipped the counts on")."""
    bare = _QUOTED.sub("", sentence.strip())
    if not bare or _NOT_A_CLAIM.search(bare) or _PLAN.search(bare) or _OVERNIGHT.search(bare):
        return None  # a plan, a question, a denial; overnight work is held to its own check (``ClaimWatch``)
    if _EARLIER.search(bare):
        return None  # about before this turn: ``ClaimWatch`` judges it against her earlier steps
    if any(m.group(1).lower() not in _NOT_SOMEONE for m in _REPORTED.finditer(bare)):
        return None  # someone else's words
    if found := claims_of(sentence):
        return found[0]
    clip = sentence.strip()[:300]
    for match in _DID.finditer(bare):
        verb = match.group(1).lower().split()[0]
        if verb not in _THINKING and not re.match(r"\s*\w+ing\b", bare[match.end() :]):  # "I started talking"
            return Claim(verb, clip)
    return None


@dataclass
class Turn:
    """What a kind can see of the turn so far."""

    acts: list[Act] = field(default_factory=list)  # her calls that finished in this turn, in order
    started: int = 0  # her calls started in this turn
    panels_up: bool = False  # one of her panels is on screen
    earlier: Sequence[Act] = ()  # her calls in the few turns before


@dataclass
class Held:
    sentence: str
    kind: Kind
    claim: Claim | None = None
    acts_before: int = 0  # how many of her calls had come back when it was said
    started_before: int = 0  # how many had started
    at: float = 0.0  # when it was written (``ClaimHold``'s clock)
    borne: bool = False


@dataclass
class Kind:
    """A kind of claim: which sentences make it, which finished call bears it out, what happens at the turn's end if
    nothing did (``at_end``: keep the sentence or not, and a line to add, or "")."""

    name: str
    says: Callable[[str, Turn], Claim | None]
    bears_out: Callable[[Held, Act], bool]
    at_end: Callable[[Held, Turn], tuple[bool, str]]
    # Kinds whose line at the end says this one's too ("the panel didn't come up" covers "it doesn't show that yet").
    covered_by: tuple[str, ...] = ()
    # What her next turn is told when one of these isn't borne out and gets a line ({said}: her words, {heard}: it).
    note: str = ""
    # Goes out as written when no call has started ``WAIT_FOR_CALL_S`` after it.
    waits_for_call: bool = False
    # Its line is said once lately (``ClaimHold``'s ``told``): for a line about how things are ("the panel didn't come
    # up"), not one correcting what she just claimed ("that didn't go through"), which must always follow its claim.
    said_once: bool = False


def _bears_out_action(held: Held, act: Act) -> bool:
    if not act.ok or act.name in NEUTRAL:
        return False
    claim = held.claim
    if claim is not None and claim.verb in _READ_VERBS:
        return True  # "I checked": any call of hers is a look at something
    if claim is not None and judge(Claim(claim.verb, claim.sentence), [act]).backed_by:
        return True
    return not _READS.match(act.name)  # a call that changes something


def _says_action(sentence: str, turn: Turn) -> Claim | None:
    claim = action_claim(sentence)
    if claim is None:
        return None
    if turn.earlier and judge(Claim(claim.verb, claim.sentence), list(turn.earlier)).backed_by:
        return None  # about a step of hers a turn or two back ("I installed it" after the install)
    return claim


def _action_at_end(held: Held, turn: Turn) -> tuple[bool, str]:
    after = [a for a in turn.acts[held.acts_before :] if a.name not in NEUTRAL]
    if after and not any(a.ok for a in after) and not claims_of(held.sentence):
        # Every call she made after saying it failed. The sentence may carry more than the claim ("I calculated it:
        # the total is $42"), so it's said, and the line after it says the step didn't work.
        return True, DIDNT_WORK
    # Never tried, or a story: said as written. A claim ``ClaimWatch`` knows ("I've recorded it") is said too: going
    # out now, after the calls, it's vetted against them, and one they contradict is said honestly in its words.
    return True, ""


ACTION = Kind(
    "action",
    _says_action,
    _bears_out_action,
    _action_at_end,
    covered_by=("panel_up",),
    waits_for_call=True,
    note=(
        'in your last reply you said "{said}" before the call that would do it, and every call you made after it '
        "failed, so the user heard {heard} after it. Don't say it's done until a call of yours comes back done; if "
        "it matters, try again or tell them plainly what went wrong"
    ),
)


class ClaimHold:
    """Stands between her words and her voice for one turn: ``text`` as it's written, ``step`` when she starts a
    call, ``finished`` when one comes back, ``finish`` at the end. ``feed`` says text. ``kinds`` in order of
    precedence; ``panels_up`` tells whether one of her panels is on screen; ``earlier`` gives her calls of the few
    turns before."""

    def __init__(
        self,
        feed: Callable[[str], None],
        kinds: Sequence[Kind] = (ACTION,),
        *,
        panels_up: Callable[[], bool] = lambda: False,
        earlier: Callable[[], Sequence[Act]] = lambda: (),
        flush: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        told: Callable[[str], bool] = lambda line: False,
    ) -> None:
        self._feed = feed
        self._flush = flush  # what's gone out is a whole thing to say (the voice's ``flush``)
        self._clock = clock
        self._told = told  # whether she's said a line already, lately: it isn't said again (only her brain is told)
        self._kinds = tuple(kinds)
        self._panels_up = panels_up
        self._earlier = earlier
        self._turn = Turn()
        self._partial = ""
        self._held: list[tuple[str, Held | None]] = []  # from the first claim on: each sentence, with its claim
        self.dropped: list[str] = []  # claims never borne out and not said (``finish``)
        self.notes: list[str] = []  # for her next turn, about what was taken back (``Kind.note``)
        self.added: list[str] = []  # what she said instead or besides
        self.repeated: list[str] = []  # lines not said again (``told``)
        self.once: list[str] = []  # the lines added that are said only once lately (``Kind.said_once``)
        self.borne_late = 0  # claims held, then borne out by a call that came back after they were written

    @property
    def holding(self) -> bool:
        return bool(self._held)

    def text(self, text: str) -> None:
        self._waited()
        self._partial += text
        while match := _SENTENCE_END.search(self._partial):
            sentence, self._partial = self._partial[: match.end()], self._partial[match.end() :]
            self._sentence(sentence)

    def step(self) -> None:
        """She's starting a call: what she wrote before it is a whole thing to say (or hold)."""
        if self._partial.strip():
            self._sentence(self._partial)
        self._partial = ""
        self._turn.started += 1

    def finished(self, act: Act) -> None:
        """One of her calls came back: what it bears out is true now, and goes out once nothing held is waiting."""
        self._turn.acts.append(act)
        waiting = [h for _, h in self._held if h is not None and not h.borne]
        for held in waiting:
            if self._guard(held.kind.bears_out, False, held, act):
                held.borne = True
                self.borne_late += 1
        if self._held and all(h is None or h.borne for _, h in self._held):
            self._release()

    def _waited(self) -> None:
        """General claims with no call started since they were written, ``WAIT_FOR_CALL_S`` on: none is coming for
        them, so they go out as written (``ClaimWatch`` still judges them)."""
        now = self._clock()
        for _, held in self._held:
            if (
                held is not None
                and not held.borne
                and held.kind.waits_for_call
                and self._turn.started == held.started_before
                and now - held.at >= WAIT_FOR_CALL_S
            ):
                held.borne = True
        if self._held and all(h is None or h.borne for _, h in self._held):
            self._release()

    def finish(self) -> str:
        """The turn is over: each claim still held goes as its kind says. Returns the lines added ("" for none)."""
        if self._partial.strip():
            self._sentence(self._partial)
        self._partial = ""
        if not self._held:
            return ""
        said: list[str] = []
        lines: list[tuple[str, Kind]] = []
        noted: list[tuple[str, str, str]] = []
        for sentence, held in self._held:
            if held is None or held.borne:
                said.append(sentence)
                continue
            keep, line = self._guard(held.kind.at_end, (True, ""), held, self._turn)
            log.info(
                "Claim not borne out (%s): %r, %s", held.kind.name, sentence.strip(), "said" if keep else "dropped"
            )
            if keep:
                said.append(sentence)
            else:
                self.dropped.append(sentence)
            if held.kind.note and line:
                noted.append((held.kind.note, sentence.strip(), line))
            if line and all(line != said_line for said_line, _ in lines):
                lines.append((line, held.kind))
        self._held = []
        named = {kind.name for _, kind in lines}
        self.added = [line for line, kind in lines if not named.intersection(kind.covered_by)]
        # Said lately already: once is enough out loud ("the panel didn't come up" after every reply was nagging,
        # 2026-11-23); her brain is still told, so it stops saying what isn't so.
        once = {line for line, kind in lines if kind.said_once}
        self.once = [line for line in self.added if line in once]
        self.repeated = [line for line in self.once if self._guard(self._told, False, line)]
        self.added = [line for line in self.added if line not in self.repeated]
        for note, sentence, line in noted:
            heard = (
                f'"{line}"' if line in self.added else "nothing about it (you'd said that already, so it was left out)"
            )
            self.notes.append(note.format(said=sentence, heard=heard))
        out = "".join(said).strip()
        added = " ".join(self.added)
        if out or added:
            self._feed(" ".join(p for p in (out, added) if p))
        return added

    def _sentence(self, sentence: str) -> None:
        held = self._claim(sentence)
        if self._held or held is not None:
            self._held.append((sentence, held))
            return
        self._feed(sentence)

    def _claim(self, sentence: str) -> Held | None:
        turn = self._turn
        turn.panels_up = self._safe(self._panels_up, False)
        turn.earlier = self._safe(self._earlier, ())
        for kind in self._kinds:
            if (claim := self._guard(kind.says, None, sentence, turn)) is None:
                continue
            held = Held(
                sentence, kind, claim, acts_before=len(turn.acts), started_before=turn.started, at=self._clock()
            )
            if any(self._guard(kind.bears_out, False, held, act) for act in turn.acts):
                return None  # borne out already: said at once
            return held
        return None

    def _release(self) -> None:
        held, self._held = self._held, []
        self._feed("".join(s for s, _ in held))
        self._flush()  # a whole thing said: what she writes next isn't glued onto it

    @staticmethod
    def _guard(call: Callable[..., object], default: object, *args: object) -> object:
        """A kind's own judgment, never in the way of her answer."""
        try:
            return call(*args)
        except Exception:
            log.warning("Judging a claim failed", exc_info=True)
            return default

    @staticmethod
    def _safe(get: Callable[[], object], default: object) -> object:
        try:
            return get()
        except Exception:
            return default


def without_dropped(text: str, dropped: list[str], added: str) -> str:
    """Her reply as it was really said: the claims that were dropped taken out, the line said instead put in."""
    for claim in dropped:
        text = text.replace(claim.strip(), "", 1)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return f"{text} {added}".strip() if added else text
