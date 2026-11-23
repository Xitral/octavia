"""Experiments: the only way Octavia earns the right to say one thing makes a difference for the user.

A pattern in her records ("to-dos she gave a heads-up about were finished on time more often") is a correlation: the
heads-up may simply have gone to the to-dos that were going to be finished anyway. To know, she has to vary that one
thing on purpose, fairly, and count. That changes what she does in the user's life, so the rules are strict:

* Nothing human-facing starts without the user's explicit yes. A proposal waits as "proposed" until the user approves
  it, and the approval checks the rules again: the user may have said "don't experiment with that" in between.
* In the protected domains (``schema.PROTECTED_DOMAINS``: school, health, money, work, other people...) she never
  changes what she does to see what happens. The most she does there is keep count of something the user chooses to
  try on themselves, with their yes.
* The user's own switches (``privacy.Controls``) come first: an off-limits domain, "no experiments with my to-dos",
  or no experiments at all.
* Experiments come from fixed templates (``TEMPLATES``), each with its question, its variants, its constraints and a
  paragraph the user is told before saying yes; never from a model making one up.
* Every experiment is bounded: a minimum number of results per variant before it's read, and a maximum number of days.

Units (a to-do, a day) are assigned deterministically and in balance: to the variant with the fewest so far, ties
broken by a hash of the experiment and the unit, so a unit always gets the same variant and a restart changes nothing.
Taking turns rather than choosing is what makes the comparison fair: she can't give the heads-up to the to-dos she
expects to be finished anyway.

The result is read with ``stats.compare`` like any other pattern, but because she varied one thing on purpose, a clear
difference is evidence of cause, for this user, with these kinds of things (``Hypotheses.apply_experiment`` makes it
a causal claim). An unclear result is said to be unclear.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from octavia.futures.outcomes import Outcomes
from octavia.futures.privacy import ALL, Controls, redact
from octavia.futures.schema import PROTECTED_DOMAINS, STRENGTH_ORDER, Strength
from octavia.futures.stats import Comparison, Tally, compare
from octavia.futures.store import FuturesStore, dumps, loads

log = logging.getLogger(__name__)

DAY = 86400.0
PERSONAL = "personal"  # it changes something in the user's day
INTERNAL = "internal"  # it only changes how she works
KINDS = frozenset({PERSONAL, INTERNAL})
STATUSES = ("proposed", "running", "completed", "stopped", "rejected")
OPEN_STATUSES = ("proposed", "running")
TICKTICK_HIGH = 5  # TickTick's priorities are 0 (none), 1, 3 and 5 (high)

_PROTECTED = frozenset(d.value for d in PROTECTED_DOMAINS)
# How a domain is said in a sentence: "you asked me not to experiment with your to-dos".
_SPOKEN = {
    "assistant": "the way I talk with you",
    "general": "that",
    "computer": "your computer",
    "productivity": "your to-dos",
    "school": "school",
    "communications": "your email and messages",
    "time": "your timers",
    "goals": "your goals",
    "routines": "your routines",
    "self": "my self-improvement",
    "futures": "my own bookkeeping",
    "relationships": "your relationships",
    "health": "your health",
    "finance": "money",
    "employment": "your work",
    "people": "other people",
}


@dataclass(frozen=True)
class Template:
    """A kind of experiment she knows how to run: what it asks, what varies, and the limits it keeps to."""

    key: str
    title: str
    question: str
    domain: str
    kind: str  # "personal" or "internal"
    octavia_acts: bool  # she varies what she does (True), or only keeps count of what the user tries (False)
    variants: tuple[str, ...]  # the first is the change being tried, the second how things go without it
    metric: str  # the outcomes kind it's measured by
    feature: str  # the outcome feature that carries the variant, so the pattern finder sees it too
    min_per_variant: int
    max_days: int
    constraints: tuple[str, ...]
    description: str  # told to the user before they say yes

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"an experiment is personal or internal, not {self.kind!r}")
        if len(self.variants) < 2 or len(set(self.variants)) != len(self.variants):
            raise ValueError("an experiment compares at least two different variants")
        if self.min_per_variant < 1 or self.max_days < 1:
            raise ValueError("an experiment needs a minimum sample and a time limit")


TEMPLATES: dict[str, Template] = {
    "task_nudge": Template(
        key="task_nudge",
        title="A heads-up before a to-do is due",
        question="Does a heads-up a few hours before a to-do is due help you finish it on time?",
        domain="productivity",
        kind=PERSONAL,
        octavia_acts=True,
        variants=("nudge", "no_nudge"),
        metric="task_on_time",
        feature="nudged",
        min_per_variant=6,
        max_days=28,
        constraints=(
            "only to-dos with a due time",
            "never school work, high-priority to-dos or anything already overdue",
            "TickTick's own reminders stay as they are: only my extra heads-up varies",
        ),
        description=(
            "For up to four weeks, I'd give you a short heads-up a few hours before some of your to-dos with a due "
            "time, and not before others, taking turns so it's a fair test. Then I'd count which ones you finish on "
            "time. School work, high-priority to-dos and anything already overdue stay out of it, and TickTick's own "
            "reminders don't change: only my extra heads-up varies. You can stop it whenever you like, and I'll tell "
            "you what I found."
        ),
    ),
    # Her experiments on herself (``selfscore``, ``habit_rules``): one thing about how she talks or when she speaks
    # up, varied at random and judged by how the user takes it. The variants' wording is fixed in habit_rules.
    "brief_when_stressed": Template(
        key="brief_when_stressed",
        title="Short answers when you sound stressed",
        question="When you sound stressed, do short answers go down better than my usual ones?",
        domain="assistant",
        kind=INTERNAL,
        octavia_acts=True,
        variants=("brief", "usual"),
        metric="turn_verdict",
        feature="x_brief_when_stressed",
        min_per_variant=15,
        max_days=42,
        constraints=(
            "only replies to something you said while your voice sounded stressed",
            "a task's steps and checks happen as usual: only how much I say varies",
        ),
        description=(
            "When your voice sounds stressed, I'd keep some answers to a sentence or two and give others as usual, "
            "taking turns at random, and count which ones you correct, cut short or ask again. If short ones clearly "
            "go down better, I'd keep doing that. You can stop it whenever you like."
        ),
    ),
    "answer_first": Template(
        key="answer_first",
        title="Leading with the answer",
        question="Do my replies go down better when the first words are the answer itself?",
        domain="assistant",
        kind=INTERNAL,
        octavia_acts=True,
        variants=("answer_first", "usual"),
        metric="turn_verdict",
        feature="x_answer_first",
        min_per_variant=60,
        max_days=28,
        constraints=("only the start of my replies changes, never what I do",),
        description=(
            "For some replies I'd start with the answer itself, with no lead-in, and for others talk as usual, taking "
            "turns at random, and count which ones you correct, cut short or ask again. If leading with the answer "
            "clearly goes down better, I'd keep doing it. You can stop it whenever you like."
        ),
    ),
    "heads_up_timing": Template(
        key="heads_up_timing",
        title="When a to-do heads-up comes",
        question="Does a heads-up about an hour before a to-do is due help you finish on time as well as one three "
        "hours before?",
        domain="productivity",
        kind=PERSONAL,
        octavia_acts=True,
        variants=("late", "early"),
        metric="task_on_time",
        feature="heads_up_timing",
        min_per_variant=6,
        max_days=42,
        constraints=(
            "only to-dos I'd give a heads-up about anyway",
            "never school work, high-priority to-dos or anything already overdue",
            "only when my heads-up comes varies; TickTick's own reminders stay as they are",
        ),
        description=(
            "For the to-dos I'd give you a heads-up about anyway, I'd give some the heads-up about an hour before "
            "they're due and others about three hours before, as now, taking turns, and count which ones you finish "
            "on time. School work, high-priority to-dos and anything overdue stay out of it. If the later heads-up "
            "clearly works better, I'd switch to it. You can stop it whenever you like."
        ),
    ),
}

SELF_TEMPLATES = frozenset({"brief_when_stressed", "answer_first", "heads_up_timing"})  # her experiments on herself


@dataclass(frozen=True)
class _Wording:
    """How a template's result is said: "With a heads-up, 7 of 8 to-dos were finished on time; without, 4 of 8."."""

    with_: str
    without: str
    noun: str
    success: str
    change: str
    helps: str
    worse: str
    scope: str


_WORDING = {
    "task_nudge": _Wording(
        with_="With a heads-up",
        without="without",
        noun="to-dos",
        success="were finished on time",
        change="the heads-up",
        helps="helps you",
        worse="you did better without it",
        scope="for you, with these kinds of to-dos",
    ),
    "brief_when_stressed": _Wording(
        with_="Kept short",
        without="as usual",
        noun="replies",
        success="went down fine",
        change="keeping it short",
        helps="goes down better when you sound stressed",
        worse="my usual answers did better",
        scope="for you, when you sound stressed",
    ),
    "answer_first": _Wording(
        with_="Leading with the answer",
        without="as usual",
        noun="replies",
        success="went down fine",
        change="leading with the answer",
        helps="goes down better",
        worse="my usual replies did better",
        scope="for you, with these kinds of requests",
    ),
    "heads_up_timing": _Wording(
        with_="With the heads-up an hour before",
        without="three hours before",
        noun="to-dos",
        success="were finished on time",
        change="the later heads-up",
        helps="works better for you",
        worse="the earlier heads-up worked better",
        scope="for you, with these kinds of to-dos",
    ),
}


def _wording(key: str, variants: tuple[str, ...]) -> _Wording:
    found = _WORDING.get(key)
    if found is not None:
        return found
    tried, other = (v.replace("_", " ") for v in variants[:2])
    return _Wording(
        with_=f"With {tried}",
        without=f"with {other}",
        noun="tries",
        success="worked",
        change=f'"{tried}"',
        helps="helps",
        worse="things went better without it",
        scope="in these conditions",
    )


def task_nudge_eligible(
    *, due_ts: float | None, all_day: bool, priority: int | None, now: float, domain: str = "productivity"
) -> bool:
    """Whether a to-do may take part in ``task_nudge``: its constraints as code. It needs a due time (not just a
    day), must not be school work (or any protected domain) or high priority, and must not be overdue already."""
    if due_ts is None or all_day or due_ts <= now:
        return False
    if _norm(domain) in _PROTECTED:
        return False
    return priority is None or priority < TICKTICK_HIGH


# ---------------------------------------------------------------------------------------------- the policy
@dataclass(frozen=True)
class Verdict:
    allowed: bool
    needs_approval: bool
    reason: str  # plain words, for the user: why not, or why it needs their yes


class ExperimentRefused(ValueError):
    """The policy won't allow this experiment; the message is the reason, in plain words for the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExperimentPolicy:
    """The rules every experiment passes, checked in order; the first that applies decides. A refused verdict still
    says ``needs_approval``, so code that only looks at that field errs on the side of asking."""

    def __init__(self, controls: Controls, *, autonomous_internal: bool = False) -> None:
        self.controls = controls
        self.autonomous_internal = autonomous_internal

    def check(self, *, domain: str, kind: str, octavia_acts: bool) -> Verdict:
        """Whether an experiment in ``domain`` of this ``kind`` may run, and whether it needs the user's yes."""
        domain = _norm(domain)
        spoken = _SPOKEN.get(domain, domain)
        if not self.controls.allows_experiment(domain):
            return Verdict(False, True, self._why_not(domain, spoken))
        if domain in _PROTECTED:
            if octavia_acts:
                return Verdict(False, True, f"I never change what I do about {spoken} to see what happens")
            return Verdict(True, True, f"with {spoken} I'd only keep count of what you try yourself, with your yes")
        if kind == PERSONAL:
            return Verdict(True, True, "it changes something in your day, so it needs your yes first")
        if kind == INTERNAL:
            if self.autonomous_internal:
                return Verdict(True, False, "it only changes how I work, so I can run it on my own")
            return Verdict(True, True, "it only changes how I work, but I still check with you first")
        return Verdict(False, True, f"I don't know what kind of experiment {kind!r} is")

    def _why_not(self, domain: str, spoken: str) -> str:
        if domain in self.controls.off_limits:
            return f"you've put {spoken} off-limits"
        if ALL in self.controls.no_experiments:
            return "you asked me not to run experiments at all"
        return f"you asked me not to experiment with {spoken}"


# ---------------------------------------------------------------------------------------------- records
@dataclass(frozen=True)
class Experiment:
    id: int
    key: str
    title: str
    hypothesis_id: int | None
    domain: str
    kind: str
    octavia_acts: bool
    variants: tuple[str, ...]
    metric: str
    min_per_variant: int
    constraints: tuple[str, ...]
    status: str  # "proposed", "running", "completed", "stopped" or "rejected"
    created_at: float
    approved_at: float | None
    started_at: float | None
    ends_at: float | None
    ended_at: float | None
    result: Mapping[str, Any] | None
    confidence: str | None  # a Strength value, once completed
    notes: str

    @property
    def template(self) -> Template | None:
        return TEMPLATES.get(self.key)


@dataclass(frozen=True)
class ExperimentResult:
    experiment_id: int
    hypothesis_id: int | None
    by_variant: dict[str, Tally]
    difference: float  # the first variant's rate minus the second's
    strength: str  # a Strength value
    supported: bool | None  # None: inconclusive
    summary: str


def _to_experiment(row: sqlite3.Row) -> Experiment:
    return Experiment(
        id=row["id"],
        key=row["key"],
        title=row["title"],
        hypothesis_id=row["hypothesis_id"],
        domain=row["domain"],
        kind=row["kind"],
        octavia_acts=bool(row["octavia_acts"]),
        variants=tuple(loads(row["variants"], [])),
        metric=row["metric"],
        min_per_variant=row["min_per_variant"],
        constraints=tuple(loads(row["constraints"], [])),
        status=row["status"],
        created_at=row["created_at"],
        approved_at=row["approved_at"],
        started_at=row["started_at"],
        ends_at=row["ends_at"],
        ended_at=row["ended_at"],
        result=loads(row["result"], None),
        confidence=row["confidence"],
        notes=row["notes"],
    )


# ---------------------------------------------------------------------------------------------- experiments
class Experiments:
    """The experiments and their trials. Every status change goes through the policy or the user, and is recorded
    in the audit trail (``experiment:<id>``)."""

    def __init__(self, store: FuturesStore, outcomes: Outcomes, policy: ExperimentPolicy) -> None:
        self.store = store
        self.outcomes = outcomes
        self.policy = policy

    # ------------------------------------------------------------------ lifecycle
    def propose(self, template: str, *, hypothesis_id: int | None = None, cause: str | None = None) -> Experiment:
        """Propose the experiment ``template``: "proposed" until the user says yes, or straight "running" when the
        policy needs no yes for it. An open one with the same key is returned as it is. Raises ValueError for an
        unknown template and ``ExperimentRefused`` when the policy won't allow it."""
        found = TEMPLATES.get(template)
        if found is None:
            known = ", ".join(sorted(TEMPLATES)) or "none"
            raise ValueError(f"unknown experiment {template!r}; the ones I know are {known}")
        verdict = self.policy.check(domain=found.domain, kind=found.kind, octavia_acts=found.octavia_acts)
        if not verdict.allowed:
            raise ExperimentRefused(verdict.reason)
        with self.store.transaction():
            row = self.store.one(
                "SELECT * FROM experiments WHERE key=? AND status IN ('proposed','running') ORDER BY id DESC LIMIT 1",
                (found.key,),
            )
            if row is not None:
                return _to_experiment(row)
            now = self.store.clock()
            running = not verdict.needs_approval
            experiment_id = self.store.insert(
                "INSERT INTO experiments(key, title, hypothesis_id, domain, kind, octavia_acts, variants, metric, "
                "min_per_variant, constraints, status, created_at, started_at, ends_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    found.key,
                    found.title,
                    hypothesis_id,
                    _norm(found.domain),
                    found.kind,
                    int(found.octavia_acts),
                    dumps(list(found.variants)),
                    found.metric,
                    found.min_per_variant,
                    dumps(list(found.constraints)),
                    "running" if running else "proposed",
                    now,
                    now if running else None,
                    now + found.max_days * DAY if running else None,
                ),
            )
            self.store.record_change(
                f"experiment:{experiment_id}",
                "started" if running else "proposed",
                cause=cause,
                detail={"key": found.key, "hypothesis": hypothesis_id, "reason": verdict.reason},
            )
        if running:
            log.info("Experiment #%d (%s) started without approval: %s", experiment_id, found.key, verdict.reason)
        return self._must_get(experiment_id)

    def approve(self, experiment_id: int, *, cause: str | None = None) -> Experiment:
        """The user said yes: a proposed experiment starts now and runs for its template's days at most. The policy
        is checked again (the user may have changed a switch since); ``ExperimentRefused`` leaves it proposed. Raises
        ValueError when it isn't a proposal waiting for a yes."""
        with self.store.transaction():
            current = self.get(experiment_id)
            if current is None:
                raise ValueError(f"there's no experiment #{experiment_id}")
            if current.status != "proposed":
                raise ValueError(f"experiment #{experiment_id} is {current.status}, not waiting for a yes")
            template = current.template
            if template is None:
                raise ValueError(f"I no longer know how to run the experiment {current.key!r}")
            # The stored record and the template as it is today must both pass: neither can sneak past the rules.
            checks = {(current.domain, current.kind, current.octavia_acts)}
            checks.add((template.domain, template.kind, template.octavia_acts))
            for domain, kind, acts in sorted(checks):
                verdict = self.policy.check(domain=domain, kind=kind, octavia_acts=acts)
                if not verdict.allowed:
                    raise ExperimentRefused(verdict.reason)
            now = self.store.clock()
            self.store.changed(
                "UPDATE experiments SET status='running', approved_at=?, started_at=?, ends_at=? WHERE id=?",
                (now, now, now + template.max_days * DAY, experiment_id),
            )
            self.store.record_change(
                f"experiment:{experiment_id}", "approved", cause=cause, detail={"key": current.key, "status": "running"}
            )
        log.info("Experiment #%d (%s) approved and running", experiment_id, current.key)
        return self._must_get(experiment_id)

    def reject(self, experiment_id: int, *, cause: str | None = None) -> Experiment | None:
        """The user said no to a proposal. None when it isn't a proposal (a running one is stopped instead)."""
        return self._end(experiment_id, "rejected", ("proposed",), notes="", cause=cause)

    def stop(self, experiment_id: int, *, reason: str = "", cause: str | None = None) -> Experiment | None:
        """End a running experiment (or withdraw a proposal) before it's done; what was counted so far is kept in
        its result, but it isn't read as evidence. None when it had already ended."""
        return self._end(experiment_id, "stopped", OPEN_STATUSES, notes=redact(reason, 200), cause=cause)

    def _end(
        self, experiment_id: int, status: str, from_: tuple[str, ...], *, notes: str, cause: str | None
    ) -> Experiment | None:
        with self.store.transaction():
            current = self.get(experiment_id)
            if current is None or current.status not in from_:
                return None
            result = None
            if current.status == "running":
                result = dumps({"by_variant": _counts(self.counts(experiment_id)), "supported": None})
            self.store.changed(
                "UPDATE experiments SET status=?, ended_at=?, result=COALESCE(?, result), notes=? WHERE id=?",
                (status, self.store.clock(), result, notes, experiment_id),
            )
            self.store.record_change(
                f"experiment:{experiment_id}",
                status,
                cause=cause,
                detail={"key": current.key, "was": current.status, "reason": notes},
            )
        return self.get(experiment_id)

    # ------------------------------------------------------------------ reading
    def get(self, experiment_id: int) -> Experiment | None:
        row = self.store.one("SELECT * FROM experiments WHERE id=?", (experiment_id,))
        return _to_experiment(row) if row else None

    def _must_get(self, experiment_id: int) -> Experiment:
        found = self.get(experiment_id)
        if found is None:  # only if someone deleted it in between
            raise ValueError(f"there's no experiment #{experiment_id}")
        return found

    def running(self, *, metric: str | None = None, key: str | None = None) -> list[Experiment]:
        """The running experiments, oldest first, optionally only those measured by ``metric`` or of one ``key``."""
        sql, params = "SELECT * FROM experiments WHERE status='running'", []
        if metric is not None:
            sql += " AND metric=?"
            params.append(metric)
        if key is not None:
            sql += " AND key=?"
            params.append(key)
        return [_to_experiment(r) for r in self.store.execute(sql + " ORDER BY started_at, id", params)]

    def list(self, *, status: str | None = None, limit: int = 20) -> list[Experiment]:
        """Experiments newest first."""
        sql, params = "SELECT * FROM experiments", []
        if status is not None:
            sql += " WHERE status=?"
            params.append(status)
        rows = self.store.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*params, limit))
        return [_to_experiment(r) for r in rows]

    def latest(self, key: str) -> Experiment | None:
        """The newest experiment of a template, whatever became of it: whether it was already turned down or done."""
        row = self.store.one("SELECT * FROM experiments WHERE key=? ORDER BY created_at DESC, id DESC LIMIT 1", (key,))
        return _to_experiment(row) if row else None

    # ------------------------------------------------------------------ trials
    def assign(self, experiment_id: int, unit: str) -> str:
        """The variant ``unit`` gets in a running experiment: the one it already has, or the variant with the fewest
        units so far (ties broken by a hash, so it's reproducible). New units aren't taken once the experiment's
        time is up. ValueError when it isn't running."""
        unit = str(unit).strip()
        if not unit:
            raise ValueError("an experiment unit needs a name")
        with self.store.transaction():
            current = self.get(experiment_id)
            if current is None or current.status != "running":
                state = "missing" if current is None else current.status
                raise ValueError(f"experiment #{experiment_id} isn't running ({state})")
            row = self.store.one("SELECT variant FROM trials WHERE experiment_id=? AND unit=?", (experiment_id, unit))
            if row is not None:
                return str(row["variant"])
            now = self.store.clock()
            if current.ends_at is not None and now >= current.ends_at:
                raise ValueError(f"experiment #{experiment_id} has run its course; it takes no new units")
            assigned = {v: 0 for v in current.variants}
            for r in self.store.execute(
                "SELECT variant, COUNT(*) AS n FROM trials WHERE experiment_id=? GROUP BY variant", (experiment_id,)
            ):
                if r["variant"] in assigned:
                    assigned[r["variant"]] = int(r["n"])
            fewest = min(assigned.values())
            tied = [v for v in current.variants if assigned[v] == fewest]
            digest = int(hashlib.sha256(f"{experiment_id}:{unit}".encode()).hexdigest(), 16)
            chosen = tied[digest % len(tied)]
            self.store.insert(
                "INSERT INTO trials(experiment_id, unit, variant, assigned_at) VALUES (?,?,?,?)",
                (experiment_id, unit, chosen, now),
            )
        return chosen

    def enroll(self, experiment_id: int, unit: str, variant: str) -> str | None:
        """Put ``unit`` in ``variant`` of a running experiment, when the variant was already chosen at random where
        it applied (a turn's notes are decided on the event loop, and this database is only written on the worker).
        Returns the variant the unit has (an earlier one stands), or None when the experiment isn't running, is out
        of time, or has no such variant."""
        unit = str(unit).strip()
        if not unit:
            return None
        with self.store.transaction():
            current = self.get(experiment_id)
            if current is None or current.status != "running" or variant not in current.variants:
                return None
            row = self.store.one("SELECT variant FROM trials WHERE experiment_id=? AND unit=?", (experiment_id, unit))
            if row is not None:
                return str(row["variant"])
            now = self.store.clock()
            if current.ends_at is not None and now >= current.ends_at:
                return None
            self.store.insert(
                "INSERT INTO trials(experiment_id, unit, variant, assigned_at) VALUES (?,?,?,?)",
                (experiment_id, unit, variant, now),
            )
        return variant

    def variant(self, experiment_id: int, unit: str) -> str | None:
        """The variant ``unit`` was given, or None when it isn't in the experiment."""
        row = self.store.one(
            "SELECT variant FROM trials WHERE experiment_id=? AND unit=?", (experiment_id, str(unit).strip())
        )
        return str(row["variant"]) if row else None

    def record(self, experiment_id: int, unit: str, *, success: bool, outcome_id: int | None = None) -> bool:
        """Count how it went for an assigned unit of a running experiment. Only its first outcome counts; returns
        whether this one did."""
        with self.store.transaction():
            current = self.get(experiment_id)
            if current is None or current.status != "running":
                return False
            changed = self.store.changed(
                "UPDATE trials SET success=?, outcome_id=? WHERE experiment_id=? AND unit=? AND success IS NULL",
                (int(bool(success)), outcome_id, experiment_id, str(unit).strip()),
            )
        return changed == 1

    def counts(self, experiment_id: int) -> dict[str, Tally]:
        """Recorded results per variant so far (units still waiting for their outcome don't count)."""
        current = self.get(experiment_id)
        if current is None:
            return {}
        tallies = {v: Tally() for v in current.variants}
        rows = self.store.execute(
            "SELECT variant, SUM(success) AS s, COUNT(*) AS n FROM trials "
            "WHERE experiment_id=? AND success IS NOT NULL GROUP BY variant",
            (experiment_id,),
        )
        for r in rows:
            if r["variant"] in tallies:
                tallies[r["variant"]] = Tally(int(r["s"] or 0), int(r["n"]))
        return tallies

    # ------------------------------------------------------------------ reading the result
    def evaluate(
        self, experiment_id: int, *, now: float | None = None, cause: str | None = None
    ) -> ExperimentResult | None:
        """Read a running experiment once it's ready: every variant has its minimum of results, or its time is up
        (then with what there is). It's completed with its result; None while it isn't ready or isn't running."""
        now = now if now is not None else self.store.clock()
        with self.store.transaction():
            current = self.get(experiment_id)
            if current is None or current.status != "running":
                return None
            tallies = self.counts(experiment_id)
            enough = all(t.trials >= current.min_per_variant for t in tallies.values())
            over = current.ends_at is not None and now >= current.ends_at
            if not (enough or over):
                return None
            first, second = current.variants[0], current.variants[1]
            c = compare(tallies[first], tallies[second])
            supported = _supported(c)
            summary = _summary(current, tallies, c, supported, enough=enough)
            result = {
                "by_variant": _counts(tallies),
                "difference": round(c.difference, 4),
                "supported": supported,
                "strength": c.strength.value,
                "ended_by": "enough" if enough else "time",
                "summary": summary,
            }
            self.store.changed(
                "UPDATE experiments SET status='completed', ended_at=?, result=?, confidence=? WHERE id=?",
                (now, dumps(result), c.strength.value, experiment_id),
            )
            self.store.record_change(
                f"experiment:{experiment_id}",
                "completed",
                cause=cause,
                detail={
                    "supported": supported,
                    "strength": c.strength.value,
                    "difference": round(c.difference, 4),
                    "counts": {v: t.describe() for v, t in tallies.items()},
                },
            )
        log.info("Experiment #%d (%s) completed: %s", experiment_id, current.key, summary)
        return ExperimentResult(
            experiment_id=experiment_id,
            hypothesis_id=current.hypothesis_id,
            by_variant=tallies,
            difference=c.difference,
            strength=c.strength.value,
            supported=supported,
            summary=summary,
        )

    def result(self, experiment_id: int) -> ExperimentResult | None:
        """A completed experiment's result, as ``evaluate`` gave it."""
        current = self.get(experiment_id)
        if current is None or current.status != "completed" or not current.result:
            return None
        raw = current.result
        by_variant = {
            str(v): Tally(int(c.get("successes", 0)), int(c.get("trials", 0)))
            for v, c in (raw.get("by_variant") or {}).items()
        }
        return ExperimentResult(
            experiment_id=current.id,
            hypothesis_id=current.hypothesis_id,
            by_variant=by_variant,
            difference=float(raw.get("difference") or 0.0),
            strength=str(raw.get("strength") or current.confidence or Strength.INSUFFICIENT.value),
            supported=raw.get("supported"),
            summary=str(raw.get("summary") or ""),
        )


# ---------------------------------------------------------------------------------------------- helpers
def _norm(domain: str | Enum) -> str:
    value = domain.value if isinstance(domain, Enum) else domain
    return " ".join(str(value).split()).lower()


def _counts(tallies: Mapping[str, Tally]) -> dict[str, dict[str, int]]:
    return {v: {"successes": t.successes, "trials": t.trials} for v, t in tallies.items()}


def _supported(c: Comparison) -> bool | None:
    """Supported with at least moderate evidence the first variant does better; refuted when the data says there's no
    real difference, or with at least moderate evidence it does no better; otherwise inconclusive."""
    clear = STRENGTH_ORDER[c.strength] >= STRENGTH_ORDER[Strength.MODERATE]
    if c.difference > 0 and clear:
        return True
    if c.against or (c.difference <= 0 and clear):
        return False
    return None


def _summary(
    experiment: Experiment, tallies: Mapping[str, Tally], c: Comparison, supported: bool | None, *, enough: bool
) -> str:
    """The result in plain words: the counts, then what they're evidence of (and of what kind)."""
    w = _wording(experiment.key, experiment.variants)
    a, b = tallies[experiment.variants[0]], tallies[experiment.variants[1]]
    first = f"{a.describe()} {w.noun} {w.success}" if a.trials else f"no {w.noun} came up"
    if not b.trials:
        second = "none came up"
    else:
        second = b.describe() if a.trials else f"{b.describe()} {w.success}"
    counts = f"{w.with_}, {first}; {w.without}, {second}."
    if supported is True:
        verdict = (
            f"That's {c.strength.value} evidence {w.change} {w.helps} "
            f"(this was an experiment, so it's evidence of cause — {w.scope})."
        )
    elif supported is False and c.against:
        verdict = f"That's evidence {w.change} makes no real difference ({w.scope})."
    elif supported is False:
        verdict = (
            f"That's {c.strength.value} evidence {w.change} doesn't help: {w.worse} "
            f"(this was an experiment, so it's evidence of cause — {w.scope})."
        )
    elif not enough:
        verdict = f"That's not enough to tell whether {w.change} {w.helps}: there were too few {w.noun} to compare."
    else:
        verdict = f"That's not enough to tell whether {w.change} {w.helps}: the difference is too small to be sure of."
    return f"{counts} {verdict}"
