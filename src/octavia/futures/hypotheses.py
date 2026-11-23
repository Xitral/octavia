"""Hypotheses: the patterns Octavia finds in how things turn out, and what becomes of them as evidence comes in.

The pattern finder (``Hypotheses.mine``) is plain counting. For one kind of outcome (``outcomes``: to-dos finished
on time, her actions on the computer working...) it takes each condition the outcomes were recorded with ("due in
the evening", "in Blender") and compares the success rate of the outcomes with it against all the others
(``stats.compare``). A difference big enough to matter, with at least some evidence behind it, becomes a
hypothesis: "To-dos due in the evening were finished by their due time 2 of 7 times, compared with 9 of 11
otherwise."

What such a pattern is gets said plainly and never more:

* It's a correlation in her records, never a cause. The statement is the counts, and ``confounders`` name what else
  might explain them (what kind of to-do it was, how busy that day was, a small sample). Only an experiment where
  she varied one thing on purpose (``apply_experiment``) makes a hypothesis's claim ``causal``.
* Its ``strength`` is the category ``stats.compare`` earns from the counts, not a percentage. A weak one stays
  ``speculative``: something to watch, not to lean on.
* One look isn't enough to call a pattern ``supported``. It starts ``observed`` however strong it looks and is
  promoted only when later outcomes keep it strong.

The lifecycle, each time the counts behind a hypothesis change:

    speculative -> observed         moderate evidence, with a difference big enough to matter (``min_effect``)
    observed -> supported           strong evidence on a later look
    observed/supported -> weakened  the evidence dropped a level, or the difference shrank below ``min_effect``
    weakened -> observed/supported  it recovered
    any -> rejected                 plenty of data and no real difference, or the difference turned the other way
                                    with moderate evidence
    rejected -> observed            a clear pattern again in new outcomes (never after an experiment rejected it)
    stale -> as it was              new evidence after a long quiet (``mark_stale``), judged from where it was
    testing                         an experiment is running on it: only the counts are updated
    claim "causal"                  an experiment's verdict: ordinary counts update the numbers, not the verdict

A feature with just two values ("nudged": yes or no) gives one hypothesis, not two: each value's comparison with the
rest is the other's mirror image, and two patterns saying one thing would count twice. Forgetting one forgets both.

A change of belief (formed, strengthened, weakened, rejected, gone stale) gets an evidence row and an entry in the
audit trail. Counts that move without changing what she believes only update the row, so the trail stays readable:
``updated_at`` is when the belief last changed, ``last_evidence_at`` when new outcomes last counted toward it.

The user's own ideas ("I think I get less done on Mondays") are kept too (``add_user``), in their words, until the
records have something to say about them. From then on the counts speak, whichever way they point: her records can't
tell which way the user meant it, and a statement that disagreed with its own counts would be worse than none.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from octavia.futures.outcomes import Outcomes
from octavia.futures.privacy import redact
from octavia.futures.schema import STRENGTH_ORDER, HypothesisStatus, Knowledge, Strength
from octavia.futures.stats import Comparison, Tally, compare, sample_strength
from octavia.futures.store import FuturesStore, dumps, loads

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricInfo:
    """What an outcome kind measures, in words: ``noun`` that ``success``, and what else could explain a difference
    in it (``confounders``)."""

    noun: str
    success: str
    domain: str
    confounders: tuple[str, ...]
    predicate: str = ""  # ``success`` as the rest of a sentence, when it needs a verb ("were finished by...")

    @property
    def succeeded(self) -> str:
        """The success phrase after a plural subject: "were finished by their due time", "worked"."""
        return self.predicate or self.success


METRICS: dict[str, MetricInfo] = {
    "task_on_time": MetricInfo(
        "to-dos",
        "finished by their due time",
        "productivity",
        ("what kind of to-do it was", "how big it was", "how busy that day was"),
        predicate="were finished by their due time",
    ),
    "tool_call": MetricInfo(
        "my actions on the computer",
        "worked",
        "computer",
        ("which app it was", "what was on the screen", "how the request was worded"),
    ),
    "brain_turn": MetricInfo(
        "my replies",
        "went through without a correction",
        "assistant",
        ("what was asked", "how long the conversation was"),
    ),
    "build": MetricInfo(
        "my self-improvement builds",
        "passed their checks",
        "self",
        ("how big the change was", "which files it touched"),
    ),
    "recommendation": MetricInfo(
        "my suggestions",
        "were taken up",
        "futures",
        ("what else was going on", "how I worded them"),
    ),
}

# How a group of outcomes reads after its noun ("to-dos due in the evening"). A feature not listed reads
# "where {feature} was {value}".
FEATURES: dict[str, str | dict[str, str]] = {
    "due_part_of_day": "due in the {value}",
    "part_of_day": "in the {value}",
    "weekday": "on {value}s",
    "day_type": "on {value}s",
    "priority": "with {value} priority",
    "nudged": {"yes": "I gave a heads-up about", "no": "I didn't give a heads-up about"},
    "app": "in {value}",
    "tool": "using {value}",
    "created_by": "created by {value}",
    "category": "of the {value} kind",
}

# Values that read better as words: TickTick's priority numbers, who made a to-do.
_VALUE_WORDS: dict[str, dict[str, str]] = {
    "priority": {"0": "no", "1": "low", "3": "medium", "5": "high"},
    "created_by": {"assistant": "me", "user": "you", "ticktick": "you in TickTick"},
}
# Forms that also stand alone in brackets ("7 of 9 similar ones (with a heads-up)"), where a clause wouldn't.
_BRACKETED: dict[str, dict[str, str]] = {"nudged": {"yes": "with a heads-up", "no": "without a heads-up"}}

SMALL_SAMPLE = "a small sample"

CORRELATION, CAUSAL, USER = "correlation", "causal", "user"

_SPECULATIVE = HypothesisStatus.SPECULATIVE.value
_OBSERVED = HypothesisStatus.OBSERVED.value
_TESTING = HypothesisStatus.TESTING.value
_SUPPORTED = HypothesisStatus.SUPPORTED.value
_WEAKENED = HypothesisStatus.WEAKENED.value
_REJECTED = HypothesisStatus.REJECTED.value
_STALE = HypothesisStatus.STALE.value

_CORRELATED = frozenset({_OBSERVED, _SUPPORTED, _WEAKENED})
# Higher is more believed: a move up this ladder is "strengthened".
_LADDER = {_SPECULATIVE: 0, _WEAKENED: 1, _OBSERVED: 2, _SUPPORTED: 3}
_MODERATE = STRENGTH_ORDER[Strength.MODERATE]
_STRONG = STRENGTH_ORDER[Strength.STRONG]
_WEAK = STRENGTH_ORDER[Strength.WEAK]

_NOTES = {
    "created": "noticed",
    "strengthened": "stronger",
    "weakened": "weaker",
    "updated": "the records now say which way it goes",
}


def _rank(strength: str | Strength) -> int:
    return STRENGTH_ORDER[Strength(strength)]


def describe_feature(feature: str, value: str, *, bracketed: bool = False) -> str:
    """How outcomes with ``feature`` = ``value`` read after their noun: "due in the evening", "in Blender".
    ``bracketed`` gives a form that also stands alone ("with a heads-up" rather than "I gave a heads-up about")."""
    value = str(value)
    if bracketed and value in _BRACKETED.get(feature, {}):
        return _BRACKETED[feature][value]
    phrase = FEATURES.get(feature)
    if isinstance(phrase, dict):
        if value in phrase:
            return phrase[value]
        phrase = None
    words = _VALUE_WORDS.get(feature, {}).get(value, value.capitalize() if feature == "weekday" else value)
    if phrase is None:
        return f"where {feature.replace('_', ' ')} was {words}"
    return phrase.format(value=words)


def hypothesis_key(metric: str, feature: str, value: str) -> str:
    """The one hypothesis there is about outcomes of ``metric`` with ``feature`` = ``value``."""
    return f"{metric}|{feature}={value}"


def pattern_statement(metric: str, feature: str, value: str, group: Tally, rest: Tally) -> str:
    """The pattern as its counts and nothing more, e.g. "To-dos due in the evening were finished by their due time
    2 of 7 times, compared with 9 of 11 otherwise"."""
    info = METRICS[metric]
    noun = info.noun[:1].upper() + info.noun[1:]
    return (
        f"{noun} {describe_feature(feature, value)} {info.succeeded} {group.describe()} times, "
        f"compared with {rest.describe()} otherwise."
    )


# What can explain a difference in one feature in particular, beyond the metric's usual suspects.
FEATURE_CONFOUNDERS = {
    # Outside an experiment she only gives a heads-up about to-dos her records say tend to slip: the heads-up group
    # was worse off to begin with, so comparing it with the rest says little about the heads-up itself.
    "nudged": "I only gave heads-ups about to-dos my records said were likely to slip, so the groups weren't alike",
}


def _confounders(metric: str, strength: str, group: Tally, rest: Tally, feature: str | None = None) -> tuple[str, ...]:
    info = METRICS.get(metric)
    found = info.confounders if info else ()
    if feature in FEATURE_CONFOUNDERS:
        found = (FEATURE_CONFOUNDERS[feature], *found)
    # A moderate difference between 7 and 11 outcomes is still a handful of to-dos: say so whenever either side is.
    smallest = min(group.trials, rest.trials)
    if _rank(strength) <= _WEAK or STRENGTH_ORDER[sample_strength(smallest)] <= _WEAK:
        found = (*found, SMALL_SAMPLE)
    return found


@dataclass(frozen=True)
class Hypothesis:
    """A possible pattern and the evidence for it. ``support`` of ``trials`` is the group's count and
    ``other_support`` of ``other_trials`` everyone else's; ``effect`` is the difference of their rates."""

    id: int
    key: str
    statement: str
    domain: str
    metric: str
    feature: str | None
    value: str | None
    claim: str  # "correlation"; "causal" (an experiment said so); "user" (the user's idea, not yet borne out)
    status: str  # a HypothesisStatus value
    strength: str  # a Strength value
    support: int
    trials: int
    other_support: int
    other_trials: int
    effect: float | None
    confounders: tuple[str, ...]
    origin: str  # "miner" or "user"
    proposed_experiment: str | None
    created_at: float
    updated_at: float  # when the belief last changed
    last_evidence_at: float | None  # when new outcomes last counted toward it

    @property
    def knowledge(self) -> Knowledge:
        """What kind of claim this is. Only an experiment makes a causal claim, and a rejected hypothesis claims
        nothing, whatever it once was."""
        if self.claim == CAUSAL and self.status != _REJECTED:
            return Knowledge.CAUSAL_CLAIM
        if self.claim == CORRELATION and self.status in _CORRELATED:
            return Knowledge.CORRELATION
        return Knowledge.HYPOTHESIS

    @property
    def counts(self) -> str:
        """ "2 of 7, compared with 9 of 11 otherwise"; empty while there are no counts (a user's untested idea)."""
        if not self.trials and not self.other_trials:
            return ""
        return f"{self.support} of {self.trials}, compared with {self.other_support} of {self.other_trials} otherwise"

    @property
    def group(self) -> Tally:
        return Tally(self.support, self.trials)

    @property
    def rest(self) -> Tally:
        return Tally(self.other_support, self.other_trials)


@dataclass(frozen=True)
class HypothesisChange:
    """What one look did to a hypothesis: "created", "strengthened", "weakened", "status" or "updated"."""

    hypothesis: Hypothesis
    change: str
    old_status: str | None
    old_strength: str | None


def _to_hypothesis(row: sqlite3.Row) -> Hypothesis:
    return Hypothesis(
        id=row["id"],
        key=row["key"],
        statement=row["statement"],
        domain=row["domain"],
        metric=row["metric"],
        feature=row["feature"],
        value=row["value"],
        claim=row["claim"],
        status=row["status"],
        strength=row["strength"],
        support=row["support"],
        trials=row["trials"],
        other_support=row["other_support"],
        other_trials=row["other_trials"],
        effect=row["effect"],
        confounders=tuple(loads(row["confounders"], [])),
        origin=row["origin"],
        proposed_experiment=row["proposed_experiment"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_evidence_at=row["last_evidence_at"],
    )


def _proposed_experiment(metric: str, feature: str | None) -> str | None:
    return "task_nudge" if (metric, feature) == ("task_on_time", "nudged") else None


def _statuses(status: str | Iterable[str] | None) -> list[str] | None:
    if status is None:
        return None
    values = [status] if isinstance(status, str) else list(status)
    return [str(getattr(s, "value", s)) for s in values]


def _normal(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


class Hypotheses:
    """The hypotheses table and the pattern finder. ``allows(domain)`` says whether she may learn about a domain
    (``privacy.Controls.allows_learning``); ``suppressed(key)`` whether the user told her to forget a pattern
    (``Controls.suppressed``), so it isn't simply formed again. ``windows`` limits a metric to its recent outcomes
    (seconds back from now): tool calls pile up by the hundred, and what she did a year ago says little about how
    she does now."""

    def __init__(
        self,
        store: FuturesStore,
        outcomes: Outcomes,
        *,
        allows: Callable[[str], bool] = lambda d: True,
        suppressed: Callable[[str], bool] = lambda k: False,
        min_trials: int = 4,
        min_effect: float = 0.2,
        windows: Mapping[str, float] | None = None,
    ) -> None:
        self.store = store
        self.outcomes = outcomes
        self._allows = allows
        self._suppressed = suppressed
        self.min_trials = min_trials
        self.min_effect = min_effect
        self.windows = dict(windows or {})

    # ------------------------------------------------------------------ the pattern finder
    def mine(self, metric: str, *, cause: str | None = None) -> list[HypothesisChange]:
        """Look for patterns in the outcomes of ``metric`` and update the hypotheses already formed about them.
        Returns what changed in what she believes (counts that only moved aren't a change)."""
        info = METRICS.get(metric)
        if info is None or not self._allows(info.domain):
            return []
        changes: list[HypothesisChange] = []
        with self.store.transaction():
            now = self.store.clock()
            window = self.windows.get(metric)
            since = now - window if window else None
            for feature, groups in self.outcomes.groups(metric, since=since).items():
                total = sum(groups.values(), Tally())
                for value in self._values(metric, feature, groups, total):
                    group = groups[value]
                    rest = Tally(total.successes - group.successes, total.trials - group.trials)
                    comparison = compare(group, rest)
                    existing = self.by_key(hypothesis_key(metric, feature, value))
                    if existing is None:
                        change = self._create(metric, feature, value, group, rest, comparison, now, cause)
                    else:
                        change = self._update(existing, group, rest, comparison, now, cause)
                    if change is not None:
                        changes.append(change)
        return changes

    def mine_all(self, *, cause: str | None = None) -> list[HypothesisChange]:
        """``mine`` every metric she knows."""
        return [change for metric in METRICS for change in self.mine(metric, cause=cause)]

    def _values(self, metric: str, feature: str, groups: dict[str, Tally], total: Tally) -> list[str]:
        keys = {value: hypothesis_key(metric, feature, value) for value in groups}
        ready = [
            value
            for value in sorted(groups)
            if groups[value].trials >= self.min_trials
            and total.trials - groups[value].trials >= 1
            and not self._suppressed(keys[value])
        ]
        if len(groups) != 2:
            return ready
        # Two values: each one's comparison is the other's mirror image, so one hypothesis says it all.
        if any(self._suppressed(key) for key in keys.values()):
            return []
        existing = {value for value, key in keys.items() if self.by_key(key) is not None}
        if existing:
            return [value for value in ready if value in existing]
        if not ready:
            return []
        # "Yes" reads more naturally than its negation; otherwise the smaller group is the exception worth naming.
        return [min(ready, key=lambda value: (value != "yes", groups[value].trials, value))]

    def _create(
        self,
        metric: str,
        feature: str,
        value: str,
        group: Tally,
        rest: Tally,
        comparison: Comparison,
        now: float,
        cause: str | None,
    ) -> HypothesisChange | None:
        if abs(comparison.difference) < self.min_effect or comparison.strength is Strength.INSUFFICIENT:
            return None
        # However strong it looks, one look is an observation: "supported" waits for later outcomes to agree.
        status = _SPECULATIVE if comparison.strength is Strength.WEAK else _OBSERVED
        strength = comparison.strength.value
        hypothesis_id = self.store.insert(
            "INSERT INTO hypotheses(key, statement, domain, metric, feature, value, claim, status, strength, "
            "support, trials, other_support, other_trials, effect, confounders, origin, proposed_experiment, "
            "created_at, updated_at, last_evidence_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                hypothesis_key(metric, feature, value),
                pattern_statement(metric, feature, value, group, rest),
                METRICS[metric].domain,
                metric,
                feature,
                value,
                CORRELATION,
                status,
                strength,
                group.successes,
                group.trials,
                rest.successes,
                rest.trials,
                comparison.difference,
                dumps(_confounders(metric, strength, group, rest, feature)),
                "miner",
                _proposed_experiment(metric, feature),
                now,
                now,
                now,
            ),
        )
        hypothesis = self._get(hypothesis_id)
        self._note(hypothesis, "created", 1, cause, now)
        return HypothesisChange(hypothesis, "created", None, None)

    def _update(
        self, h: Hypothesis, group: Tally, rest: Tally, comparison: Comparison, now: float, cause: str | None
    ) -> HypothesisChange | None:
        if (group.successes, group.trials, rest.successes, rest.trials) == (
            h.support,
            h.trials,
            h.other_support,
            h.other_trials,
        ):
            return None  # nothing new to judge
        grew = group.trials > h.trials or rest.trials > h.other_trials
        status, claim = self._judge(h, comparison, grew)
        # While an experiment runs, and after one gave its verdict, ordinary counts only update the numbers.
        frozen = h.status == _TESTING or h.claim == CAUSAL
        strength = h.strength if frozen else comparison.strength.value
        statement = h.statement
        if claim == CORRELATION and h.feature is not None and h.value is not None and h.metric in METRICS:
            statement = pattern_statement(h.metric, h.feature, h.value, group, rest)
        confounders = h.confounders if frozen else _confounders(h.metric, strength, group, rest, h.feature)
        change, direction = _classify(h, status, strength, claim)
        fields: dict[str, Any] = {
            "support": group.successes,
            "trials": group.trials,
            "other_support": rest.successes,
            "other_trials": rest.trials,
            "effect": comparison.difference,
            "status": status,
            "strength": strength,
            "claim": claim,
            "statement": statement,
            "confounders": dumps(confounders),
        }
        if grew:
            fields["last_evidence_at"] = now
        if change is not None:
            fields["updated_at"] = now
        self._write(h.id, fields)
        updated = self._get(h.id)
        if change is None:
            return None
        self._note(updated, change, direction, cause, now)
        return HypothesisChange(updated, change, h.status, h.strength)

    def _judge(self, h: Hypothesis, c: Comparison, grew: bool) -> tuple[str, str]:
        """The (status, claim) ``h`` moves to on a new look at its counts: the lifecycle in the module docstring."""
        status, claim = h.status, h.claim
        if status == _TESTING:
            return status, claim
        if status == _STALE:
            if not grew:
                return status, claim
            status = self._before_stale(h.id)
        if claim == CAUSAL:
            return status, claim
        if status == _REJECTED and (not grew or self._rejected_by_experiment(h.id)):
            return status, claim
        level = _rank(c.strength)
        clear = abs(c.difference) >= self.min_effect
        adopting = claim == USER
        if adopting:
            if c.against:
                return _REJECTED, claim
            if not (clear and level >= _WEAK):
                return status, claim
            claim = CORRELATION
        # The user's idea had no direction to flip from: its effect so far is just what the counts happened to be.
        flipped = not adopting and h.effect is not None and h.effect * c.difference < 0 and level >= _MODERATE
        if c.against or flipped:
            return _REJECTED, claim
        if status in (_REJECTED, _SPECULATIVE):
            return (_OBSERVED if clear and level >= _MODERATE else status), claim
        if status in (_OBSERVED, _SUPPORTED):
            if not clear or level < _rank(h.strength):
                return _WEAKENED, claim
            return (_SUPPORTED if level >= _STRONG else status), claim
        if status == _WEAKENED:
            # Recovered: clear again with moderate evidence, and something actually got better since it weakened.
            improved = level > _rank(h.strength) or (h.effect is not None and abs(h.effect) < self.min_effect)
            if clear and level >= _MODERATE and improved:
                return (_SUPPORTED if level >= _STRONG else _OBSERVED), claim
        return status, claim

    def _before_stale(self, hypothesis_id: int) -> str:
        """The status a stale hypothesis had before it went quiet, from the audit trail."""
        rows = self.store.execute(
            "SELECT detail FROM changes WHERE object=? ORDER BY ts DESC, id DESC LIMIT 20",
            (f"hypothesis:{hypothesis_id}",),
        )
        for row in rows:
            status = loads(row["detail"], {}).get("status")
            if status in _LADDER:
                return status
        return _SPECULATIVE

    def _rejected_by_experiment(self, hypothesis_id: int) -> bool:
        row = self.store.one(
            "SELECT direction FROM hypothesis_evidence WHERE hypothesis_id=? AND experiment_id IS NOT NULL "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (hypothesis_id,),
        )
        return row is not None and row["direction"] < 0

    # ------------------------------------------------------------------ the user's ideas and experiments
    def add_user(
        self,
        statement: str,
        *,
        metric: str,
        feature: str | None = None,
        value: str | None = None,
        domain: str | None = None,
        cause: str | None = None,
    ) -> Hypothesis:
        """Keep the user's own idea about a pattern, in their words. With a ``feature`` and ``value`` it's the same
        hypothesis the pattern finder would form, so an existing one is returned as it is; the records then decide
        it. A ValueError for an empty statement, half a feature, or a domain the user put off-limits."""
        text = redact(statement, 200)
        if not text:
            raise ValueError("a hypothesis needs a statement")
        if (feature is None) != (value is None):
            raise ValueError("give both a feature and its value, or neither")
        info = METRICS.get(metric)
        domain = domain or (info.domain if info else "general")
        if not self._allows(domain):
            raise ValueError(f"{domain} is off-limits, so I'm not keeping ideas about it")
        if feature is not None and value is not None:
            key = hypothesis_key(metric, feature, str(value))
        else:
            key = f"user|{metric}|{_normal(text)}"
        with self.store.transaction():
            existing = self.by_key(key)
            if existing is not None:
                return existing
            now = self.store.clock()
            hypothesis_id = self.store.insert(
                "INSERT INTO hypotheses(key, statement, domain, metric, feature, value, claim, status, strength, "
                "confounders, origin, proposed_experiment, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    text,
                    domain,
                    metric,
                    feature,
                    None if value is None else str(value),
                    USER,
                    _SPECULATIVE,
                    Strength.INSUFFICIENT.value,
                    dumps(info.confounders if info else ()),
                    "user",
                    _proposed_experiment(metric, feature),
                    now,
                    now,
                ),
            )
            hypothesis = self._get(hypothesis_id)
            self._note(hypothesis, "created", 0, cause, now, note=f"the user suggested it: {text}")
        return hypothesis

    def mark_testing(self, hypothesis_id: int, *, cause: str | None = None) -> Hypothesis | None:
        """An experiment started on it: until its verdict, new outcomes only update the counts."""
        with self.store.transaction():
            h = self.get(hypothesis_id)
            if h is None or h.status == _TESTING:
                return h
            now = self.store.clock()
            self._write(h.id, {"status": _TESTING, "updated_at": now})
            updated = self._get(h.id)
            self._note(updated, "status", 0, cause, now, note="an experiment started on it")
        return updated

    def apply_experiment(
        self,
        hypothesis_id: int,
        *,
        supported: bool | None,
        strength: str,
        summary: str,
        experiment_id: int,
        cause: str | None = None,
    ) -> Hypothesis | None:
        """An experiment's verdict. Supported with at least moderate evidence is the one way a claim becomes
        causal; against it, the hypothesis is rejected for good; anything less leaves it a correlation."""
        level = _rank(strength)
        words = " ".join((summary or "").split())
        with self.store.transaction():
            h = self.get(hypothesis_id)
            if h is None:
                return None
            fields: dict[str, Any]
            if supported is True and level >= _MODERATE:
                # The experiment's own words: the counts it varied on purpose, which is what earns "causal".
                fields = {"status": _SUPPORTED, "claim": CAUSAL, "strength": Strength(strength).value}
                if words:
                    fields["statement"] = words
                change, direction = "strengthened", 1
            elif supported is False:
                fields = {"status": _REJECTED}
                if h.claim == CAUSAL:
                    fields["claim"] = CORRELATION  # an earlier experiment's "cause" isn't claimed any more
                change, direction = "status", -1
            else:
                fields = {"status": _SPECULATIVE if _rank(h.strength) <= _WEAK else _OBSERVED}
                change, direction = "status", 0
            now = self.store.clock()
            fields["updated_at"] = fields["last_evidence_at"] = now
            self._write(h.id, fields)
            updated = self._get(h.id)
            note = f"experiment {experiment_id}: {words}" if words else f"experiment {experiment_id}"
            self._note(updated, change, direction, cause, now, note=note, experiment_id=experiment_id)
        return updated

    def mark_stale(self, now: float, *, after_days: float = 30.0) -> list[Hypothesis]:
        """Hypotheses with no new evidence for ``after_days``: what was true a month ago may not be now."""
        cutoff = now - after_days * 86400
        stale: list[Hypothesis] = []
        with self.store.transaction():
            rows = self.store.execute(
                "SELECT * FROM hypotheses WHERE status NOT IN (?,?,?) AND COALESCE(last_evidence_at, updated_at) < ?",
                (_REJECTED, _STALE, _TESTING, cutoff),
            )
            for row in rows:
                h = _to_hypothesis(row)
                self._write(h.id, {"status": _STALE, "updated_at": now})
                updated = self._get(h.id)
                self._note(updated, "status", 0, None, now, note=f"no new evidence in {after_days:g} days")
                stale.append(updated)
        return stale

    # ------------------------------------------------------------------ reading
    def get(self, hypothesis_id: int) -> Hypothesis | None:
        row = self.store.one("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,))
        return _to_hypothesis(row) if row else None

    def by_key(self, key: str) -> Hypothesis | None:
        row = self.store.one("SELECT * FROM hypotheses WHERE key=?", (key,))
        return _to_hypothesis(row) if row else None

    def list(
        self,
        *,
        status: str | Iterable[str] | None = None,
        metric: str | None = None,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[Hypothesis]:
        """Hypotheses whose belief changed most recently first."""
        clauses, params = ["1=1"], []
        statuses = _statuses(status)
        if statuses is not None:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})" if statuses else "0")
            params.extend(statuses)
        for column, wanted in (("metric", metric), ("domain", domain)):
            if wanted is not None:
                clauses.append(f"{column}=?")
                params.append(wanted)
        rows = self.store.execute(
            f"SELECT * FROM hypotheses WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        return [_to_hypothesis(r) for r in rows]

    def recently_changed(self, since: float) -> list[Hypothesis]:
        """Hypotheses formed or changed in what they claim since ``since``, newest first."""
        rows = self.store.execute(
            "SELECT * FROM hypotheses WHERE updated_at>=? ORDER BY updated_at DESC, id DESC", (since,)
        )
        return [_to_hypothesis(r) for r in rows]

    def evidence(self, hypothesis_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        """What moved a hypothesis, newest first: ``{ts, direction, note, outcome_id, experiment_id}``."""
        rows = self.store.execute(
            "SELECT * FROM hypothesis_evidence WHERE hypothesis_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (hypothesis_id, limit),
        )
        return [
            {
                "ts": r["ts"],
                "direction": r["direction"],
                "note": r["note"],
                "outcome_id": r["outcome_id"],
                "experiment_id": r["experiment_id"],
            }
            for r in rows
        ]

    def forget(self, hypothesis_id: int) -> Hypothesis | None:
        """Delete a hypothesis and its evidence; returns what it was. (Keeping the miner from forming it again is
        ``Controls.suppress_hypothesis``.)"""
        with self.store.transaction():
            h = self.get(hypothesis_id)
            if h is None:
                return None
            self.store.execute("DELETE FROM hypothesis_evidence WHERE hypothesis_id=?", (h.id,))
            self.store.execute("DELETE FROM hypotheses WHERE id=?", (h.id,))
            self.store.record_change(f"hypothesis:{h.id}", "forgotten", detail={"key": h.key})
        return h

    # ------------------------------------------------------------------ writing
    def _get(self, hypothesis_id: int) -> Hypothesis:
        found = self.get(hypothesis_id)
        if found is None:
            raise LookupError(f"hypothesis {hypothesis_id} vanished mid-transaction")
        return found

    def _write(self, hypothesis_id: int, fields: dict[str, Any]) -> None:
        columns = ", ".join(f"{name}=?" for name in fields)
        self.store.execute(f"UPDATE hypotheses SET {columns} WHERE id=?", (*fields.values(), hypothesis_id))

    def _note(
        self,
        h: Hypothesis,
        change: str,
        direction: int,
        cause: str | None,
        now: float,
        *,
        note: str | None = None,
        experiment_id: int | None = None,
    ) -> None:
        """An evidence row and an audit-trail entry for a change of belief."""
        if note is None:
            label = f"now {h.status}" if change == "status" else _NOTES.get(change, change)
            note = f"{label}: {h.counts} ({h.strength} evidence)" if h.counts else f"{label} ({h.strength} evidence)"
        self.store.execute(
            "INSERT INTO hypothesis_evidence(hypothesis_id, ts, direction, outcome_id, experiment_id, note) "
            "VALUES (?,?,?,?,?,?)",
            (h.id, now, direction, None, experiment_id, note),
        )
        self.store.record_change(
            f"hypothesis:{h.id}",
            change,
            cause=cause,
            detail={"status": h.status, "strength": h.strength, "counts": h.counts},
        )
        log.debug("hypothesis %d %s: %s (%s, %s)", h.id, change, h.key, h.status, h.strength)


def _classify(h: Hypothesis, status: str, strength: str, claim: str) -> tuple[str | None, int]:
    """The kind of change a look made, and which way the evidence went (+1 for, -1 against, 0 neither); None when
    what she believes didn't change."""
    if status != h.status:
        if status == _REJECTED:
            return "status", -1
        if status == _WEAKENED:
            return "weakened", -1
        if h.status in _LADDER and status in _LADDER and _LADDER[status] > _LADDER[h.status]:
            return "strengthened", 1
        return "status", 0
    # A rejected or stale hypothesis's evidence moving isn't it strengthening: it claims nothing until it's judged again.
    if strength != h.strength and status in _LADDER:
        return ("strengthened", 1) if _rank(strength) > _rank(h.strength) else ("weakened", -1)
    if claim != h.claim:
        return "updated", 0
    return None, 0
