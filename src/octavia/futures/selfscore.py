"""Self-scoring: after every reply, how it went for the user, and her experiments on herself that learn from that.

**The verdict.** Each reply of hers to the user gets one, from what the user does next (no model is asked):

* **thanked**: "thanks", "perfect", "that's it";
* **fine**: nothing said against it (they moved on, or nothing followed within ``REPLY_SETTLED_S``);
* **not**, and why: *corrected* ("no, I meant...", Futures' own correction check), *repeated* (the same request
  again), *complained* ("that's not it", "why are you just sitting there"), *too long* ("get to the point"),
  *asked again* ("sorry, what?", answered by saying it again) or *cut off* (the user talked over her).

The corrected, repeated and complained signals are the very ones her caught mistakes are found by
(``evolve.mistakes.signal``; the night's scan adds its own filters, such as a request's length, before it keeps a
case). Each verdict is a
``turn_verdict`` outcome, with what the reply was (question or request, the first tool it used, voice or typed) and
which experiment variants or habits it had.

**Experiments on herself.** The templates in ``habit_rules.SELF_TEMPLATES`` vary one thing about how she works (short
answers when the user sounds stressed, leading with the answer, when a to-do heads-up comes), through Futures' own
experiments: fixed templates, fair assignment, a minimum per group and a time limit, read by ``stats.compare``. The
ones that only change how she talks run on their own (``self_scoring.experiments: own``); one that changes the
user's day (the heads-up timing) is asked first, as every personal experiment is. One runs at a time, each once.

**Habits.** When one shows, with at least moderate evidence, that the change works better, she keeps it
(``habits``): it's listed with its evidence, said out loud when kept, and dropped at a word.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from octavia.core.text import normalize_text
from octavia.evolve.mistakes import signal
from octavia.futures.experiments import Experiment, Experiments
from octavia.futures.habit_rules import HEADS_UP_TIMING, SELF_TEMPLATES, TURN_TEMPLATES, adoptable, keeping_text
from octavia.futures.habits import Habits
from octavia.futures.outcomes import Outcome, Outcomes
from octavia.futures.stats import Tally
from octavia.futures.store import FuturesStore
from octavia.futures.tuning import TuningSnapshot

log = logging.getLogger(__name__)

THANKED = "thanked"
FINE = "fine"
CORRECTED = "corrected"
REPEATED = "repeated"
COMPLAINED = "complained"
TOO_LONG = "too_long"
ASKED_AGAIN = "asked_again"
CUT_OFF = "cut_off"
UNHAPPY = frozenset({CORRECTED, REPEATED, COMPLAINED, TOO_LONG, ASKED_AGAIN, CUT_OFF})
# What an experiment counts against a variant: not a bare cut-off. A longer reply is talked over more just by lasting
# longer, and "ok, stop" after the answer came first is satisfaction; a cut-off followed by a correction or a repeat
# still counts, as that.
AGAINST = UNHAPPY - {CUT_OFF}
SPOKEN = {
    CORRECTED: "corrected",
    REPEATED: "asked again in other words",
    COMPLAINED: "complained about",
    TOO_LONG: "too long",
    ASKED_AGAIN: "asked to repeat",
    CUT_OFF: "cut short",
}
OUTCOME = "turn_verdict"
DAY_S = 86400.0
LOOK_EVERY_S = 6 * 3600.0  # whether to start her next experiment on herself
HEADS_UPS_NEEDED = 4  # heads-ups said in the last month before the heads-up timing is worth testing
PROPOSAL_DAYS = 7  # an offer of one of hers left unanswered this long is withdrawn
MIN_REPLIES = 30  # replies judged in the last two weeks before she experiments on herself at all (she's in use)
MIN_FOR_PLACE = 3  # replies of one kind before its share gone wrong is worth saying

# "Thanks", "perfect", "that's it", at the start of what they said.
_THANKS = re.compile(
    r"^(?:thanks?|thank you|thx|ty|cheers|perfect|great|awesome|amazing|nice|excellent|brilliant|good job|well done|"
    r"nice job|exactly|love it|beautiful|sweet|thats (?:it|right|perfect|great|awesome|exactly it)|you(?:re| are) "
    r"(?:the best|awesome|amazing)|good girl|nailed it)\b"
)
# A thanks that isn't one: "thanks but that's wrong", "nice try".
_NOT_THANKS = re.compile(r"\b(?:but|wrong|not|didnt|doesnt|isnt|wasnt|try|anyway)\b")
# "Too long", "get to the point": only at the start of what they said ("skip the song", "brief me on the news" and
# "what's the shorter route" are new requests).
_TOO_LONG = re.compile(
    r"^(?:(?:ok|okay|alright|um|uh|so|yeah|octavia)\s+)*(?:(?:thats |way |that was |its )?too long|"
    r"get to the point|(?:make it|keep it|a bit|much) short(?:er)?|shorter please|be brief|short version|tl ?dr|"
    r"you talk too much|stop rambling|less talking|just (?:give me )?the answer|i didnt ask for all that|"
    r"enough already)\b"
)
# "Why do you think Rome fell?" asks for an opinion: not a complaint, though it starts like "why did you...".
_ASKS_WHY = re.compile(r"^why (?:do|did|would) you (?:think|believe|like|prefer|reckon|suppose)\b")


def reaction(text: str, *, request: str | None, asked: bool = False) -> str | None:
    """What the user's next words say about her last reply (to ``request``), when said right after it: one of the
    verdicts above, or None (nothing either way). A plain "no" or "no thanks" is an answer (to her question, or to
    an offer of more that didn't end in "?"), not a correction. ``asked`` is kept for callers; plain answers never
    count either way."""
    from octavia.futures.ingest import is_plain_answer, looks_like_correction  # ingest calls this: not at the top

    words = normalize_text(text).replace("'", "")
    if not words:
        return None
    if is_plain_answer(text):
        return None
    if looks_like_correction(text):
        return CORRECTED
    if _TOO_LONG.search(words):
        return TOO_LONG
    found = signal(request or "", text)
    if found == COMPLAINED and _ASKS_WHY.match(words):
        found = None
    if found is not None:
        return found
    if _THANKS.match(words) and not _NOT_THANKS.search(words) and not text.rstrip().endswith("?"):
        return THANKED
    return None


def worse(a: str | None, b: str | None) -> str | None:
    """Of two reactions to the same reply, the one that counts: anything against it beats a thanks."""
    if a in UNHAPPY or b is None:
        return a
    if b in UNHAPPY or a is None:
        return b
    return a


@dataclass
class PendingReply:
    """Her last reply, until what the user does next settles how it went."""

    turn: str
    event_id: str
    ts: float
    features: dict[str, str]
    tuning: tuple[tuple[str, str], ...] = ()
    interrupted: bool = False
    tool: str = ""  # the first tool the reply used
    how: str | None = None  # the user's reaction so far


@dataclass(frozen=True)
class Verdicts:
    """How a stretch of replies went."""

    total: int = 0
    thanked: int = 0
    unhappy: Counter[str] = field(default_factory=Counter)

    @property
    def not_ok(self) -> int:
        return sum(self.unhappy.values())

    def describe(self) -> str:
        if not self.total:
            return "no replies to judge"
        fine = self.total - self.thanked - self.not_ok
        parts = [f"{self.total} replies: {self.thanked} thanked, {fine} fine, {self.not_ok} not"]
        if self.unhappy:
            parts.append(" (" + ", ".join(f"{SPOKEN.get(k, k)} {n}" for k, n in self.unhappy.most_common()) + ")")
        return "".join(parts)


def verdicts(outcomes: Iterable[Outcome]) -> Verdicts:
    total = thanked = 0
    unhappy: Counter[str] = Counter()
    for o in outcomes:
        total += 1
        if o.actual == THANKED:
            thanked += 1
        elif o.actual in UNHAPPY:
            unhappy[str(o.actual)] += 1
    return Verdicts(total, thanked, unhappy)


class SelfScoring:
    """The engine's part for self-scoring. Plain synchronous code on the Futures worker, like the rest of it."""

    def __init__(
        self,
        store: FuturesStore,
        outcomes: Outcomes,
        experiments: Experiments,
        habits: Habits,
        settings: Any,  # config.SelfScoringConfig
        *,
        hears_tone: bool = True,
    ) -> None:
        self.store = store
        self.outcomes = outcomes
        self.experiments = experiments
        self.habits = habits
        self.settings = settings
        self.hears_tone = hears_tone

    # ------------------------------------------------------------------ the verdict
    def settle(self, pending: PendingReply, how: str | None, now: float) -> Outcome | None:
        """Record how ``pending`` went (``how`` None: fine) and count it in the experiments it took part in."""
        if not self.settings.enabled:
            return None
        success = how not in UNHAPPY
        counted = how not in AGAINST  # what the experiments count (a bare cut-off isn't against a variant)
        features = {**pending.features, "tool": pending.tool or "none"}
        for what, variant in pending.tuning:
            features[f"t_{what.replace(':', '_')}"] = variant
        outcome = self.outcomes.record(
            OUTCOME,
            f"turn:{pending.turn}",
            key=f"verdict:{pending.turn}",
            success=success,
            expected="went down fine",
            actual=how or FINE,
            started_at=pending.ts,
            resolved_at=now,
            features=features,
            event_id=pending.event_id,
            domain="assistant",
        )
        if outcome is None:
            return None
        for what, variant in pending.tuning:
            kind, _, number = what.partition(":")
            if kind != "experiment" or not number.isdigit():
                continue
            unit = f"turn:{pending.turn}"
            if self.experiments.enroll(int(number), unit, variant) == variant:
                self.experiments.record(int(number), unit, success=counted, outcome_id=outcome.id)
        return outcome

    # ------------------------------------------------------------------ which experiment next
    def next_template(self, now: float, *, heads_ups: int, nudging: bool) -> str | None:
        """Her next experiment on herself, when it's time for one: she's in use (``MIN_REPLIES``), none of hers is
        open, it never ran (nor has a habit), and it can run here. ``heads_ups``: heads-ups said in the last month;
        ``nudging``: the heads-up experiment is open."""
        if not self.settings.enabled or self.settings.experiments == "off":
            return None
        last = float(self.store.kv_get("selfscore", "looked", 0.0) or 0.0)
        if now - last < LOOK_EVERY_S:
            return None
        self.store.kv_set("selfscore", "looked", now)
        if self.outcomes.tally(OUTCOME, since=now - 14 * DAY_S).trials < MIN_REPLIES:
            return None
        for e in self.experiments.list(status="proposed", limit=50):
            if e.key in SELF_TEMPLATES and now - e.created_at >= PROPOSAL_DAYS * DAY_S:
                # Offered and never answered (or the offer was never said): withdrawn, so it can't hold up the rest.
                self.experiments.stop(e.id, reason=f"no answer in {PROPOSAL_DAYS} days")
        open_ = [e for status in ("proposed", "running") for e in self.experiments.list(status=status, limit=50)]
        if any(e.key in SELF_TEMPLATES for e in open_):
            return None
        habits = {h.key for h in self.habits.list(limit=100)}
        for key in SELF_TEMPLATES:
            if self.experiments.latest(key) is not None or key in habits:
                continue
            if key == "brief_when_stressed" and not self.hears_tone:
                continue
            if key == HEADS_UP_TIMING and (nudging or heads_ups < HEADS_UPS_NEEDED):
                continue
            return key
        return None

    # ------------------------------------------------------------------ keeping what works
    def completed(self, experiment: Experiment, result: Mapping[str, Any], cause: str | None) -> str | None:
        """An experiment of hers finished: keep its change as a habit when the result says so. Returns what to say
        about keeping it, or None."""
        if experiment.key not in SELF_TEMPLATES or not (self.settings.enabled and self.settings.keep_habits):
            return None
        variant = adoptable(experiment.key, experiment.variants, result)
        if variant is None:
            return None
        habit = self.habits.adopt(
            experiment.key,
            variant,
            experiment_id=experiment.id,
            evidence=str(result.get("summary") or ""),
            cause=cause,
        )
        if not habit.active or habit.experiment_id != experiment.id:
            return None  # the user dropped it before: it stays dropped
        log.info("Kept a habit from experiment #%d: %s", experiment.id, habit.name)
        return keeping_text(experiment.key, variant, str(result.get("summary") or ""))

    # ------------------------------------------------------------------ what applies to turns now
    @property
    def experimenting(self) -> bool:
        """Whether her experiments on herself may run (live settings: read when used)."""
        return bool(self.settings.enabled and self.settings.experiments != "off")

    def snapshot(self) -> TuningSnapshot:
        if not self.settings.enabled:
            return TuningSnapshot()
        running = tuple(
            (e.id, e.key, e.variants)
            for e in self.experiments.running()
            if e.key in TURN_TEMPLATES and e.ends_at is not None and e.ends_at > self.store.clock()
        )
        if not self.experimenting:
            running = ()
        habits = tuple((h.id, h.key, h.variant) for h in self.habits.active() if h.key in TURN_TEMPLATES)
        return TuningSnapshot(running, habits)

    def heads_up_late(self) -> bool:
        """Whether she kept the later heads-up as a habit."""
        return self.settings.enabled and any(
            h.key == HEADS_UP_TIMING and h.variant == "late" for h in self.habits.active()
        )

    # ------------------------------------------------------------------ the report
    def report(self, now: float, *, days: int = 7, local_time: Any = datetime.fromtimestamp) -> str:
        """Her own record, in words: how her replies went, where they go wrong, her experiments on herself and the
        habits she kept from them."""
        recent = self.outcomes.list(OUTCOME, since=now - days * DAY_S, limit=5000)
        before = self.outcomes.list(OUTCOME, since=now - 2 * days * DAY_S, limit=10000)
        before = [o for o in before if o.resolved_at < now - days * DAY_S]
        lines = [f"How my replies went (the last {days} days; judged from what you said or did next, no model asked):"]
        this = verdicts(recent)
        lines.append(f"- {this.describe()}.")
        if before:
            last = verdicts(before)
            lines.append(f"- The {days} days before: {last.describe()}.")
        places = _worst_places(recent)
        if places:
            lines.append("- Where they went wrong most: " + "; ".join(places) + ".")
        lines.extend(self._experiment_lines())
        habits = self.habits.list(limit=20)
        if habits:
            lines.append("Habits I kept from those experiments (any can be dropped with a word):")
            for h in habits:
                when = local_time(h.created_at).strftime("%b %d").replace(" 0", " ")
                state = "" if h.active else f" [dropped: {h.note}]" if h.note else " [dropped]"
                origin = f"from experiment #{h.experiment_id}" if h.experiment_id is not None else "your choice"
                lines.append(f"- #{h.id} {h.name}, since {when} ({origin}){state}. {h.evidence}".rstrip())
        else:
            lines.append("No habits kept yet: I keep a change only when an experiment clearly shows it works better.")
        return "\n".join(lines)

    def _experiment_lines(self) -> list[str]:
        mine = [e for e in self.experiments.list(limit=50) if e.key in SELF_TEMPLATES]
        if not mine:
            return ["No experiments on myself yet."]
        lines = ["My experiments on myself (one at a time, each once):"]
        for e in mine[:5]:
            counts = self.experiments.counts(e.id)
            progress = ", ".join(f"{v.replace('_', ' ')}: {t.trials}" for v, t in counts.items())
            line = f"- #{e.id} {e.title} [{e.status}]"
            if e.status == "running":
                line += f" ({progress} so far; {e.min_per_variant} each needed)"
            if e.result and e.result.get("summary"):
                line += f" {e.result['summary']}"
            lines.append(line)
        return lines


def _worst_places(outcomes: Iterable[Outcome]) -> list[str]:
    """The tools (or "no tool") whose replies went wrong most often, with their counts."""
    groups: dict[str, Tally] = {}
    for o in outcomes:
        place = o.features.get("tool") or "no tool"
        groups[place] = groups.get(place, Tally()) + Tally(int(o.actual in UNHAPPY), 1)
    ranked = sorted(
        ((t.successes / t.trials, t.successes, place, t) for place, t in groups.items() if t.trials >= MIN_FOR_PLACE),
        reverse=True,
    )
    return [f"{place.replace('_', ' ')} ({t.successes} of {t.trials})" for _, n, place, t in ranked[:3] if n]
