"""A conservative "what happens if I do A instead of B": a few ways the next hours could go, side by side.

The user asks something like "should I do the lab report or the essay tonight?" and gets branches: the plan each
one means ("2 h: Physics lab report (due tomorrow 9:00 AM)"), what it would get done, what would wait and how that
tends to go for her, what it depends on, and every assumption it makes. Nothing is presented as what will happen:
durations and on-time chances come from her own records (``predictions.base_rate`` and the like, worked out by the
caller) and each one says what it's based on, or that there's nothing to go on. How sure a branch is
(``uncertainty``) is the weakest basis it leans on, a ``Strength`` earned from counts, never a percentage.

Branches are ordered by a deterministic heuristic (the score, documented above ``_urgency``) that is never shown:
it is not a probability and not a promise, just a way to put the more sensible options first. This is plain code
with no model: the brain words the result for the user and may add judgement of its own, clearly as its own.

**The week, sampled** (``sample_week``): the same care over days instead of hours. Thousands of ways the week could go,
drawing how long each thing takes and how much free time each day has, with the work done soonest-due first (or what
the user would start first), and counting the runs in which each deadline is missed. It's what a point estimate can't
say: that three small things together are the risk. The ranges are only as good as what's behind them, so each item
says what its hours rest on (her records of how long such work took, the user's own estimate, or a guess), the answer
is in words, never a percentage, and only what holds even when everything goes fast (``WeekRisk.even_if_fast``) is
reason to speak up unasked.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from octavia.core.text import spoken_list
from octavia.futures.schema import STRENGTH_ORDER, Strength, split_ref
from octavia.futures.stats import percent, sample_strength

DEFAULT_HOURS = 1.0  # what something with no history for how long it takes is assumed to take
MIN_HOURS = 1 / 12  # nothing takes less than 5 minutes
MIN_PARTIAL_H = 0.5  # less time left than this isn't worth starting something that won't be finished
MIN_LINE_H = 0.25  # leftover time shorter than this doesn't get a line of its own
HORIZON_S = 72 * 3600  # work due within this long (or overdue) that a plan leaves waiting is a risk
MAX_LINES = 4  # waiting and dependency lines per branch; the rest are counted
MAX_PATTERNS = 2
CAVEAT = "These are rough projections from your own history, not promises."

REST_WORDS = frozenset({"rest", "resting", "break", "nothing", "relax", "relaxing", "sleep", "chill"})
MIX_WORDS = frozenset({"mix", "mixed", "both"})
# Words of an option that say nothing about which work it means ("do the essay first").
_FILLER = frozenset(
    "a an the my me i it that this these those to of on in at for and or with some do doing get getting go going "
    "work working finish finishing start starting first then just instead now tonight today tomorrow".split()
)
_WORD = re.compile(r"[a-z0-9]+")
_COUNTS = re.compile(r"\b(\d+) of (\d+)\b")
_NO_BASIS = re.compile(r"\b(no history|no record|assumed|guess)", re.IGNORECASE)
_EPS = 1e-6


# ---------------------------------------------------------------------------------------------- records
@dataclass(frozen=True)
class WorkItem:
    """Something that could be worked on. ``hours`` is how long it's expected to take (None: unknown) and
    ``hours_basis`` what that's based on ("you took 1.5 to 3 h on similar ones"); ``on_time`` the chance it gets
    done on time as things stand and ``on_time_basis`` the counts behind it ("3 of 7 similar to-dos"). ``goals``
    are the objectives it counts toward (their ids or texts), ``blocked_by`` the refs it waits on.

    ``hours_samples``/``on_time_samples`` say how many past cases an estimate comes from; without them the counts
    are read from the basis ("3 of 7"), and a basis without counts is weak at best."""

    ref: str
    title: str
    kind: str
    due_ts: float | None
    hours: float | None
    hours_basis: str
    on_time: float | None
    on_time_basis: str
    goals: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    priority: int | None = None
    sensitivity: str = "personal"
    hours_samples: int | None = None
    on_time_samples: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "title": self.title,
            "kind": self.kind,
            "due_ts": self.due_ts,
            "hours": self.hours,
            "hours_basis": self.hours_basis,
            "on_time": self.on_time,
            "on_time_basis": self.on_time_basis,
            "goals": list(self.goals),
            "blocked_by": list(self.blocked_by),
            "priority": self.priority,
            "sensitivity": self.sensitivity,
            "hours_samples": self.hours_samples,
            "on_time_samples": self.on_time_samples,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkItem:
        """Tolerant of missing keys: a model or an older payload may leave some out."""
        title = " ".join(str(data.get("title") or "").split())
        ref = str(data.get("ref") or "").strip() or title
        if not ref:
            raise ValueError("a work item needs a ref or a title")
        on_time = _num(data.get("on_time"))
        hours = _num(data.get("hours"))
        priority = _num(data.get("priority"))
        return cls(
            ref=ref,
            title=title or ref,
            kind=str(data.get("kind") or "task"),
            due_ts=_num(data.get("due_ts")),
            hours=None if hours is None else max(0.0, hours),
            hours_basis=str(data.get("hours_basis") or ""),
            on_time=None if on_time is None else min(1.0, max(0.0, on_time)),
            on_time_basis=str(data.get("on_time_basis") or ""),
            goals=_strs(data.get("goals")),
            blocked_by=_strs(data.get("blocked_by")),
            priority=None if priority is None else int(priority),
            sensitivity=str(data.get("sensitivity") or "personal"),
            hours_samples=_count(data.get("hours_samples")),
            on_time_samples=_count(data.get("on_time_samples")),
        )


@dataclass(frozen=True)
class Scenario:
    """What's being decided: from ``now``, with ``hours`` to spend, over ``items``. ``objectives`` are the user's
    active goals ({"id", "text", "label", "weight"}), ``options`` the alternatives the user named (none: the usual
    ones), ``focus`` what the user asked about in their own words (matched to item titles), ``patterns`` what her
    records suggest ({"statement", "strength", "claim"}), passed on as assumptions."""

    now: float
    hours: float
    items: tuple[WorkItem, ...]
    objectives: tuple[dict[str, Any], ...] = ()
    options: tuple[str, ...] = ()
    focus: str = ""
    patterns: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "now": self.now,
            "hours": self.hours,
            "items": [item.to_dict() for item in self.items],
            "objectives": [dict(o) for o in self.objectives],
            "options": list(self.options),
            "focus": self.focus,
            "patterns": [dict(p) for p in self.patterns],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Scenario:
        """A ValueError when it doesn't say when (``now``) or how much time there is (``hours``); anything else
        missing has a default."""
        now, hours = _num(data.get("now")), _num(data.get("hours"))
        if now is None:
            raise ValueError("a scenario needs the time it starts from (now)")
        if hours is None:
            raise ValueError("a scenario needs the hours there are to spend")
        items = tuple(
            item if isinstance(item, WorkItem) else WorkItem.from_dict(item)
            for item in data.get("items") or ()
            if isinstance(item, WorkItem | Mapping)
        )
        return cls(
            now=now,
            hours=max(0.0, hours),
            items=items,
            objectives=tuple(dict(o) for o in data.get("objectives") or () if isinstance(o, Mapping)),
            options=tuple(o for o in _strs(data.get("options")) if o.strip()),
            focus=" ".join(str(data.get("focus") or "").split()),
            patterns=tuple(dict(p) for p in data.get("patterns") or () if isinstance(p, Mapping)),
        )


@dataclass(frozen=True)
class Branch:
    """One way the time could go. ``uncertainty`` is a ``Strength`` value (how much the estimates it leans on can
    be trusted); ``score`` only orders branches and is never shown."""

    label: str
    plan: tuple[str, ...]
    benefits: tuple[str, ...]
    risks: tuple[str, ...]
    dependencies: tuple[str, ...]
    assumptions: tuple[str, ...]
    uncertainty: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "plan": list(self.plan),
            "benefits": list(self.benefits),
            "risks": list(self.risks),
            "dependencies": list(self.dependencies),
            "assumptions": list(self.assumptions),
            "uncertainty": self.uncertainty,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Branch:
        return cls(
            label=str(data.get("label") or ""),
            plan=_strs(data.get("plan")),
            benefits=_strs(data.get("benefits")),
            risks=_strs(data.get("risks")),
            dependencies=_strs(data.get("dependencies")),
            assumptions=_strs(data.get("assumptions")),
            uncertainty=_strength(data.get("uncertainty")).value,
            score=_num(data.get("score")) or 0.0,
        )


# ---------------------------------------------------------------------------------------------- the simulator
def simulate(scenario: Scenario, *, local_time: Callable[[float], datetime] = datetime.fromtimestamp) -> list[Branch]:
    """The ways the scenario's time could go, the most sensible first by their score. With ``options``, one branch
    per option; without, "Most urgent first", "Your goals first" (when anything counts toward a goal), the focus
    first (when ``focus`` names items), "Rest" and "Mixed". Branches with the same plan are merged."""
    sim = _Sim(scenario, local_time)
    if scenario.hours < MIN_HOURS:
        return [sim.build("No time to plan", [])]
    specs = sim.option_specs() if scenario.options else sim.default_specs()
    branches: list[Branch] = []
    by_plan: dict[tuple[str, ...], int] = {}
    for label, slots, notes, cap in specs:
        branch = sim.build(label, slots, notes=notes, cap=cap)
        if branch.plan in by_plan:
            index = by_plan[branch.plan]
            kept = branches[index]
            if branch.label.lower() not in kept.label.lower():
                branches[index] = _merge(kept, branch)
            continue
        by_plan[branch.plan] = len(branches)
        branches.append(branch)
    return sorted(branches, key=lambda b: -b.score)


def describe(branches: Sequence[Branch]) -> str:
    """The branches as plain text for the brain to put in its own words (the score left out: it's only an
    ordering)."""
    if not branches:
        return "I have nothing to compare."
    lines = [CAVEAT]
    for number, branch in enumerate(branches, 1):
        lines.append(f"{number}. {branch.label} ({branch.uncertainty} evidence)")
        lines.extend(f"   - {step}" for step in branch.plan)
        for title, part in (
            ("Good", branch.benefits),
            ("Risks", branch.risks),
            ("Depends on", branch.dependencies),
            ("Assuming", branch.assumptions),
        ):
            if part:
                lines.append(f"   {title}: " + "; ".join(part) + ".")
    return "\n".join(lines)


def hours_strength(item: WorkItem) -> Strength:
    """How far ``item``'s duration estimate can be leaned on: insufficient when it's unknown or has no basis."""
    if item.hours is None:
        return Strength.INSUFFICIENT
    counted = _counted(item.hours_samples, item.hours_basis)
    if counted is not None:
        return counted
    basis = item.hours_basis.strip()
    return Strength.WEAK if basis and not _NO_BASIS.search(basis) else Strength.INSUFFICIENT


def on_time_strength(item: WorkItem) -> Strength:
    """How far ``item``'s on-time chance can be leaned on: only counts earn more than insufficient."""
    if item.on_time is None:
        return Strength.INSUFFICIENT
    counted = _counted(item.on_time_samples, item.on_time_basis)
    return counted if counted is not None else Strength.INSUFFICIENT


@dataclass(frozen=True)
class _Slot:
    """A stretch of the plan: work on ``item`` (``partial`` when it won't be finished), or ``what`` ("rest", "free
    time", or an option she has no record of) when there's no item."""

    hours: float
    item: WorkItem | None = None
    partial: bool = False
    what: str = ""


_Spec = tuple[str, list[_Slot], tuple[str, ...], Strength | None]  # label, slots, extra assumptions, uncertainty cap


class _Sim:
    """One simulation: the scenario and what's worked out from it once."""

    def __init__(self, scenario: Scenario, local_time: Callable[[float], datetime]) -> None:
        self.s = scenario
        self.local_time = local_time
        self.now = scenario.now
        self.by_ref = {item.ref: item for item in scenario.items}
        hour = local_time(scenario.now).hour
        self.night = hour >= 22 or hour < 5

    # ------------------------------------------------------------------ which branches
    def default_specs(self) -> list[_Spec]:
        items = list(self.s.items)
        specs: list[_Spec] = []
        work: list[tuple[str, list[WorkItem]]] = [("Most urgent first", self.urgent_order(items))]
        goal_items = [item for item in items if self.objectives_of(item)]
        if goal_items:
            ordered = sorted(goal_items, key=lambda i: (-self.goal_weight(i), *_due_key(i)))
            work.append(("Your goals first", ordered + self.urgent_order(i for i in items if i not in goal_items)))
        focused = self.match(_content_words(self.s.focus), items) if self.s.focus else []
        if focused:
            ordered = self.urgent_order(focused) + self.urgent_order(i for i in items if i not in focused)
            work.append((f"{_cap(self.s.focus)} first", ordered))
        for label, order in work:
            slots = self.fill(order)
            if any(slot.item for slot in slots):  # all of it blocked: nothing to plan
                specs.append((label, slots, (), None))
        specs.append(("Rest", [_Slot(self.s.hours, what="rest")], (), None))
        mixed = self.mixed(items)
        if mixed is not None:
            specs.append(("Mixed", mixed, (), None))
        return specs

    def option_specs(self) -> list[_Spec]:
        items = list(self.s.items)
        specs: list[_Spec] = []
        for option in self.s.options:
            label, words = _cap(option), set(_words(option))
            matched = self.match(_content_words(option), items)
            if words & MIX_WORDS:
                slots = self.mixed(matched or items)
                specs.append((label, slots or [_Slot(self.s.hours, what="rest")], (), None))
            elif matched:
                specs.append((label, self.fill(self.urgent_order(matched)), (), None))
            elif words & REST_WORDS:
                specs.append((label, [_Slot(self.s.hours, what="rest")], (), None))
            else:
                note = f"I have no record of “{option.strip()}”, so I can't say how it would go"
                specs.append((label, [_Slot(self.s.hours, what=option.strip())], (note,), Strength.INSUFFICIENT))
        return specs

    # ------------------------------------------------------------------ planning
    def urgent_order(self, items: Iterable[WorkItem]) -> list[WorkItem]:
        return sorted(items, key=_due_key)

    def hours_of(self, item: WorkItem) -> float:
        return max(MIN_HOURS, item.hours) if item.hours is not None else DEFAULT_HOURS

    def fill(self, order: Sequence[WorkItem]) -> list[_Slot]:
        """Work through ``order`` while the time lasts. Something blocked waits until what blocks it is done in this
        plan; something that doesn't fit gets the rest of the time as a start (when that's worth it), and what time
        is left is free."""
        remaining, done, slots, pending = self.s.hours, set(), [], list(order)
        while pending and remaining > _EPS:
            pick = next((i for i in pending if all(ref in done for ref in i.blocked_by)), None)
            if pick is None:
                break
            pending.remove(pick)
            need = self.hours_of(pick)
            if need <= remaining + _EPS:
                slots.append(_Slot(need, pick))
                remaining -= need
                done.add(pick.ref)
            elif remaining >= MIN_PARTIAL_H:
                slots.append(_Slot(remaining, pick, partial=True))
                remaining = 0.0
        if remaining >= MIN_LINE_H:
            slots.append(_Slot(remaining, what="free time"))
        return slots

    def mixed(self, items: Sequence[WorkItem]) -> list[_Slot] | None:
        """The most urgent thing that can be started, for up to half the time, then rest."""
        first = next((i for i in self.urgent_order(items) if not i.blocked_by), None)
        if first is None:
            return None
        work = min(self.hours_of(first), self.s.hours / 2)
        slots = [_Slot(work, first, partial=work < self.hours_of(first) - _EPS)]
        if self.s.hours - work > _EPS:
            slots.append(_Slot(self.s.hours - work, what="rest"))
        return slots

    def match(self, words: Sequence[str], items: Sequence[WorkItem]) -> list[WorkItem]:
        """The items whose titles share the most of ``words`` (at least one)."""
        best, found = 0, []
        for item in items:
            title = _words(item.title)
            hits = sum(1 for word in words if any(_same(word, t) for t in title))
            if hits > best:
                best, found = hits, [item]
            elif hits and hits == best:
                found.append(item)
        return found

    # ------------------------------------------------------------------ goals
    def objectives_of(self, item: WorkItem) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for goal in item.goals:
            wanted = " ".join(goal.lower().split())
            key = split_ref(wanted)[1] if ":" in wanted else wanted
            for objective in self.s.objectives:
                oid = str(objective.get("id", "")).strip().lower()
                text = " ".join(str(objective.get("text", "")).lower().split())
                if (wanted in (oid, text) or key == oid) and objective not in found:
                    found.append(objective)
        return found

    def goal_weight(self, item: WorkItem) -> float:
        return sum(_weight(o) for o in self.objectives_of(item))

    # ------------------------------------------------------------------ one branch
    def build(
        self, label: str, slots: Sequence[_Slot], *, notes: tuple[str, ...] = (), cap: Strength | None = None
    ) -> Branch:
        plan: list[str] = []
        benefits: list[str] = []
        risks: list[str] = []
        leaned: list[Strength] = []  # the bases this branch's picture rests on
        estimated: list[WorkItem] = []  # items whose duration it mentions (their basis goes in the assumptions)
        goals_used: list[dict[str, Any]] = []
        done: dict[str, float] = {}  # ref -> the share of it this plan gets done
        partial: list[WorkItem] = []
        score = 0.0
        t = self.now
        for slot in slots:
            t += slot.hours * 3600
            item = slot.item
            if item is None:
                plan.append(f"{_span(slot.hours)}: {slot.what}")
                if slot.what == "rest":
                    late = ", and it's late" if self.night else ""
                    benefits.append(f"you'd get {_span(slot.hours)} to rest{late}")
                    score += _rest_value(slot.hours, self.night)
                continue
            total = self.hours_of(item)
            fraction = min(1.0, slot.hours / total)
            done[item.ref] = min(1.0, done.get(item.ref, 0.0) + fraction)
            estimated.append(item)
            leaned.append(hours_strength(item))
            due = _due_phrase(item.due_ts, self.now, self.local_time)
            value = _urgency(item, self.now) * (1 + (item.priority or 0) / 10) * fraction
            if slot.partial:
                partial.append(item)
                plan.append(f"{_span(slot.hours)}: {item.title} (a start: it takes about {_span(total)}; {due})")
                benefits.append(f"about {_span(slot.hours)} of {item.title} would be done")
            else:
                plan.append(f"{_span(slot.hours)}: {item.title} ({due})")
                if item.due_ts is None:
                    benefits.append(f"{item.title} would be done")
                elif item.due_ts <= self.now:
                    benefits.append(f"{item.title} would finally be done (it's overdue)")
                elif t <= item.due_ts + _EPS:
                    benefits.append(
                        f"{item.title} would be done about {_span((item.due_ts - t) / 3600)} before it's due"
                    )
                else:
                    value *= 0.5  # late anyway: doing it still counts, less
                    late_h = (t - item.due_ts) / 3600
                    risks.append(f"by these estimates {item.title} would be done about {_span(late_h)} after it's due")
            score += value
            for objective in self.objectives_of(item):
                score += 0.5 * _weight(objective) * fraction
                text = str(objective.get("text", "")).strip()
                line = f"{item.title} moves “{text}” forward"
                if text and line not in benefits:
                    benefits.append(line)
                if objective not in goals_used:
                    goals_used.append(objective)
        end = t

        for item in partial:
            risks.append(self.unfinished_line(item, done[item.ref], end))
        waiting: list[WorkItem] = []
        for item in self.urgent_order(self.s.items):
            share = done.get(item.ref, 0.0)
            if share >= 1.0 - _EPS or item.due_ts is None or item.due_ts > self.now + HORIZON_S:
                continue
            score -= _urgency(item, self.now) * (1 - self.chance(item, share, end)) * (1 - share)
            if share == 0.0:
                waiting.append(item)
        for item in waiting[:MAX_LINES]:
            risks.append(self.waiting_line(item, end, done, leaned, estimated))
        if len(waiting) > MAX_LINES:
            risks.append(f"and {len(waiting) - MAX_LINES} more things due soon would wait")

        uncertainty = min(leaned, key=STRENGTH_ORDER.__getitem__) if leaned else Strength.WEAK
        if cap is not None and STRENGTH_ORDER[cap] < STRENGTH_ORDER[uncertainty]:
            uncertainty = cap
        return Branch(
            label=label,
            plan=tuple(plan),
            benefits=tuple(benefits),
            risks=tuple(risks),
            dependencies=tuple(self.dependencies(done)),
            assumptions=tuple(self.assumptions(estimated, goals_used, notes)),
            uncertainty=uncertainty.value,
            score=round(score, 3),
        )

    def chance(self, item: WorkItem, share: float, end: float) -> float:
        """For the score only: the chance ``item`` still gets done on time after the plan. Its own on-time rate
        (0.5 when unknown or already overdue), or 0 when by the estimates what's left of it no longer fits before
        it's due."""
        assert item.due_ts is not None
        if item.due_ts <= self.now:
            return 0.5
        if (item.due_ts - end) / 3600 < self.hours_of(item) * (1 - share) - _EPS:
            return 0.0
        return item.on_time if item.on_time is not None else 0.5

    def unfinished_line(self, item: WorkItem, share: float, end: float) -> str:
        left = self.hours_of(item) * (1 - share)
        line = f"{item.title} wouldn't be finished: about {_span(left)} of it would be left"
        if item.due_ts is not None and self.now < item.due_ts:
            room_h = (item.due_ts - end) / 3600
            if room_h <= 0:
                line += ", and it would come due before this plan is over"
            elif room_h < left - _EPS:
                line += f", with only about {_span(room_h)} before it's due"
        return line

    def waiting_line(
        self, item: WorkItem, end: float, done: Mapping[str, float], leaned: list[Strength], estimated: list[WorkItem]
    ) -> str:
        """What leaving ``item`` for later would mean, with the basis of every estimate it quotes."""
        assert item.due_ts is not None
        if item.due_ts <= self.now:
            return f"{item.title} is overdue and would wait longer"
        line = f"{item.title} ({_due_phrase(item.due_ts, self.now, self.local_time)}) would wait"
        unmet = self.unmet(item, done)
        if unmet:
            line += f" (it's waiting on {self.names(unmet)})"
        leaned.append(on_time_strength(item))
        if item.on_time is not None:
            basis = item.on_time_basis.strip() or "a rough guess with nothing behind it"
            line += f"; I'd put it being finished on time at {percent(item.on_time)} — {basis}"
        else:
            line += "; I have no history to say how likely it'd still be done on time"
        room_h = (item.due_ts - end) / 3600
        total = self.hours_of(item)
        if room_h <= 0:
            line += ". It would come due before this plan is over"
        elif room_h < total - _EPS:
            line += f". After this there'd be about {_span(room_h)} before it's due, and it takes about {_span(total)}"
            leaned.append(hours_strength(item))
            estimated.append(item)
        return line

    def unmet(self, item: WorkItem, done: Mapping[str, float]) -> list[str]:
        """What ``item`` still waits on once this plan is done."""
        return [ref for ref in item.blocked_by if ref and done.get(ref, 0.0) < 1.0 - _EPS]

    def dependencies(self, done: Mapping[str, float]) -> list[str]:
        lines = []
        for item in self.urgent_order(i for i in self.s.items if i.blocked_by):
            unmet = self.unmet(item, done)
            if item.ref in done:
                lines.append(f"{item.title} comes after {self.names(item.blocked_by)}, so this plan does that first")
            elif not unmet:
                lines.append(
                    f"{item.title} could follow {self.names(item.blocked_by)}, but there's no time left for it"
                )
            else:
                lines.append(f"{item.title} is waiting on {self.names(unmet)}, so it can't be planned yet")
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES] + [f"and {len(lines) - MAX_LINES} more things are waiting on something"]
        return lines

    def assumptions(
        self, estimated: Sequence[WorkItem], goals: Sequence[dict[str, Any]], notes: Sequence[str]
    ) -> list[str]:
        lines = [f"you have {_amount(self.s.hours)}"]
        seen: set[str] = set()
        for item in estimated:
            if item.ref in seen:
                continue
            seen.add(item.ref)
            if item.hours is None:
                lines.append(f"{item.title}: no history for how long it takes; assumed {_span(DEFAULT_HOURS)}")
            elif item.hours_basis.strip():
                lines.append(f"{item.title}: about {_span(self.hours_of(item))} — {item.hours_basis.strip()}")
            else:
                lines.append(f"{item.title}: about {_span(self.hours_of(item))}, a rough guess with nothing behind it")
        for objective in goals:
            text = str(objective.get("text", "")).strip()
            label = str(objective.get("label", "")).strip()
            if text:
                lines.append(f"“{text}” is one of your goals" + (f" ({label})" if label else ""))
        lines.extend(notes)
        lines.extend(_pattern_lines(self.s.patterns))
        lines.append("nothing new comes up")
        return lines

    def names(self, refs: Iterable[str]) -> str:
        names = [name for name in dict.fromkeys(self.name_of(ref) for ref in refs) if name] or ["something"]
        return spoken_list(names)

    def name_of(self, ref: str) -> str:
        item = self.by_ref.get(ref)
        if item is not None:
            return item.title
        kind, key = split_ref(ref)
        if kind == "task":
            return "another to-do"
        if kind == "assignment":
            return "another assignment"
        return key or kind


# ---------------------------------------------------------------------------------------------- the heuristic
# A branch's score (worked out in ``_Sim.build``) orders branches, nothing more:
#   + for each thing worked on: its urgency (1 when due now or overdue, 0.5 when due in a day, 0.25 in three, 0.2
#     with no due date) x (1 + TickTick priority / 10) x the share of it done; halved when it would still be late;
#   + for each goal it counts toward: 0.5 x the goal's weight x the share done;
#   - for each thing due within three days (or overdue) left not done: urgency x (1 - its on-time chance) x the
#     share left, where the chance is its own on-time rate, 0.5 when unknown or overdue, and 0 when by the estimates
#     what's left of it wouldn't fit between the end of the plan and its due time (``_Sim.chance``);
#   + rest: 0.1 per hour, at most 0.4; two and a half times that late at night (22:00 to 5:00).
def _urgency(item: WorkItem, now: float) -> float:
    if item.due_ts is None:
        return 0.2
    left_h = (item.due_ts - now) / 3600
    if left_h <= 0:
        return 1.0
    return max(0.05, 1 / (1 + left_h / 24))


def _rest_value(hours: float, night: bool) -> float:
    return min(0.4, 0.1 * hours) * (2.5 if night else 1.0)


# ---------------------------------------------------------------------------------------------- words
def _due_key(item: WorkItem) -> tuple[bool, float, int, str]:
    return item.due_ts is None, item.due_ts or 0.0, -(item.priority or 0), item.title.lower()


def _due_phrase(due_ts: float | None, now: float, local_time: Callable[[float], datetime]) -> str:
    """ "due in 5 h", "due tomorrow 9:00 AM", "due Friday 3:30 PM", "overdue", "no due date"."""
    if due_ts is None:
        return "no due date"
    left = due_ts - now
    if left <= 0:
        return "overdue"
    if left < 12 * 3600:
        return f"due in {_span(left / 3600)}"
    due, today = local_time(due_ts), local_time(now).date()
    days = (due.date() - today).days
    clock = f"{due.hour % 12 or 12}:{due.minute:02d} {'AM' if due.hour < 12 else 'PM'}"
    if days == 0:
        return f"due today {clock}"
    if days == 1:
        return f"due tomorrow {clock}"
    if days < 7:
        return f"due {due:%A} {clock}"
    return f"due {due:%B} {due.day}"


def _span(hours: float) -> str:
    """A length of time as it's said: "30 min", "1.5 h", "13 h", "3 days"."""
    hours = max(0.0, hours)
    minutes = round(hours * 60)
    if minutes < 55:
        return f"{max(5, round(minutes / 5) * 5)} min"
    if hours < 10:
        return f"{round(hours * 2) / 2:g} h"
    if hours < 48:
        return f"{round(hours)} h"
    return f"{round(hours / 24)} days"


def _amount(hours: float) -> str:
    """ "4 hours", "1 hour", "45 minutes"."""
    if hours <= 0:
        return "no time"
    if hours < 1:
        return f"{max(5, round(hours * 12) * 5)} minutes"
    value = round(hours * 2) / 2
    return "1 hour" if value == 1 else f"{value:g} hours"


def _pattern_lines(patterns: Iterable[Mapping[str, Any]]) -> list[str]:
    """The strongest few patterns, each saying what kind of claim it is."""
    ranked = sorted(patterns, key=lambda p: -STRENGTH_ORDER[_strength(p.get("strength"))])
    lines = []
    for pattern in ranked:
        statement = " ".join(str(pattern.get("statement") or "").split()).rstrip(".")
        if not statement:
            continue
        strength = _strength(pattern.get("strength")).value
        claim = str(pattern.get("claim") or "correlation")
        if claim == "causal":
            lines.append(f"tested in an experiment: {statement} ({strength} evidence)")
        elif claim == "user":
            lines.append(f"you told me: {statement}")
        else:
            lines.append(
                f"a pattern in my records, not proof one thing causes the other: {statement} ({strength} evidence)"
            )
        if len(lines) >= MAX_PATTERNS:
            break
    return lines


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _content_words(text: str) -> list[str]:
    """The words of an option or focus that could name work: not filler, not "rest" or "mix"."""
    return [w for w in dict.fromkeys(_words(text)) if w not in _FILLER and w not in REST_WORDS and w not in MIX_WORDS]


def _same(word: str, other: str) -> bool:
    if word == other or word.rstrip("s") == other.rstrip("s"):
        return True
    return min(len(word), len(other)) >= 4 and (word.startswith(other) or other.startswith(word))


def _cap(text: str) -> str:
    text = " ".join(text.split())
    return text[:1].upper() + text[1:]


# ---------------------------------------------------------------------------------------------- parsing
def _counted(samples: int | None, basis: str) -> Strength | None:
    if samples is not None:
        return sample_strength(samples)
    found = _COUNTS.search(basis or "")
    return sample_strength(int(found.group(2))) if found else None


def _strength(value: Any) -> Strength:
    try:
        return Strength(str(value))
    except ValueError:
        return Strength.INSUFFICIENT


def _weight(objective: Mapping[str, Any]) -> float:
    value = _num(objective.get("weight", 1.0))
    return 1.0 if value is None else max(0.0, value)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _count(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else max(0, int(number))


def _strs(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value if v is not None and str(v) != "")
    return (str(value),)


def _merge(kept: Branch, other: Branch) -> Branch:
    """Two ways of putting it that come to the same plan: one branch, both names."""
    weaker = min((_strength(kept.uncertainty), _strength(other.uncertainty)), key=STRENGTH_ORDER.__getitem__)
    return Branch(
        label=f"{kept.label} / {other.label}",
        plan=kept.plan,
        benefits=tuple(dict.fromkeys((*kept.benefits, *other.benefits))),
        risks=tuple(dict.fromkeys((*kept.risks, *other.risks))),
        dependencies=tuple(dict.fromkeys((*kept.dependencies, *other.dependencies))),
        assumptions=tuple(dict.fromkeys((*kept.assumptions, *other.assumptions))),
        uncertainty=weaker.value,
        score=max(kept.score, other.score),
    )


# ---------------------------------------------------------------------------------------------- the week, sampled
WEEK_RUNS = 2000
DAY_STARTS_H = 9.0  # free time is spread over this part of the day: how much is left today, or before a due time
DAY_ENDS_H = 23.0
FREE_SPREAD = 0.5  # a day's free time is drawn from this much either side of what's assumed
MIN_SAMPLES = 5  # records of how long something took before they're drawn from instead of a stated range
MOST = 2 / 3
GOOD_PART = 1 / 3
SOME = 1 / 10
WEEK_CAVEAT = "This is sampled from rough ranges, not a prediction; it's as good as the hours behind it."


@dataclass(frozen=True)
class WeekWork:
    """One thing due. ``samples``: how long such work really took, when there are enough to draw from; else hours come
    from ``low_h`` to ``high_h``. ``basis``: what those rest on, in words. ``known``: more than a guess."""

    ref: str
    title: str
    due_ts: float
    low_h: float
    high_h: float
    samples: tuple[float, ...] = ()
    basis: str = "a guess: nothing on record for how long this takes"
    known: bool = False


@dataclass(frozen=True)
class WeekRisk:
    ref: str
    title: str
    due_ts: float
    late_runs: int
    runs: int
    even_if_fast: bool  # missed even with the shortest hours and the most free time
    basis: str
    known: bool

    @property
    def share(self) -> float:
        return self.late_runs / self.runs if self.runs else 0.0

    @property
    def words(self) -> str:
        """How often it's missed, with no false precision."""
        if self.share >= MOST:
            return "missed in most of the ways the week could go"
        if self.share >= GOOD_PART:
            return "missed in a good part of them"
        if self.share >= SOME:
            return "missed in some of them"
        return "hardly ever missed"


def _left_of_day(moment: datetime) -> float:
    """The share of the day's free time still ahead at ``moment``."""
    hour = moment.hour + moment.minute / 60
    return min(1.0, max(0.0, (DAY_ENDS_H - hour) / (DAY_ENDS_H - DAY_STARTS_H)))


def sample_week(
    work: Sequence[WeekWork],
    now: float,
    free_hours: Callable[[date], float],
    *,
    busy: Mapping[date, float] | None = None,
    first: Sequence[str] = (),
    runs: int = WEEK_RUNS,
    seed: int | None = None,
    local_time: Callable[[float], datetime] = datetime.fromtimestamp,
) -> list[WeekRisk]:
    """How often each deadline in ``work`` is missed, soonest due first. ``free_hours(day)``: what's assumed free that
    day, drawn from ``FREE_SPREAD`` either side; ``busy``: hours already taken that day; ``first``: refs to work on
    before the rest. The same inputs and ``seed`` give the same answer (the default seed is the day)."""
    items = sorted((w for w in work if w.due_ts > now), key=lambda w: w.due_ts)
    if not items:
        return []
    start = local_time(now)
    order = [w for w in items if w.ref in first] + [w for w in items if w.ref not in first]
    last_day = local_time(items[-1].due_ts).date()
    days = [start.date() + timedelta(days=i) for i in range((last_day - start.date()).days + 1)]
    rng = random.Random(seed if seed is not None else start.date().toordinal())
    taken = busy or {}
    assumed = {day: free_hours(day) for day in days}
    due_day = {w.ref: local_time(w.due_ts) for w in items}
    late = dict.fromkeys((w.ref for w in items), 0)

    def missed(hours: Mapping[str, float], free: Mapping[date, float]) -> set[str]:
        done, gone = 0.0, set()
        for w in order:
            done += hours[w.ref]
            due = due_day[w.ref]
            have = 0.0
            for day in days:
                if day > due.date():
                    break
                share = 1.0
                if day == start.date():
                    share = _left_of_day(start)
                if day == due.date():
                    share = max(0.0, share - _left_of_day(due))
                have += free[day] * share
            if done > have + _EPS:
                gone.add(w.ref)
        return gone

    for _ in range(runs):
        hours = {
            w.ref: (rng.choice(w.samples) if w.samples else rng.uniform(w.low_h, max(w.low_h, w.high_h))) for w in items
        }
        free = {
            day: max(0.0, rng.uniform(a * (1 - FREE_SPREAD), a * (1 + FREE_SPREAD)) - taken.get(day, 0.0))
            for day, a in assumed.items()
        }
        for ref in missed(hours, free):
            late[ref] += 1
    fastest = {w.ref: (min(w.samples) if w.samples else w.low_h) for w in items}
    roomiest = {day: max(0.0, a * (1 + FREE_SPREAD) - taken.get(day, 0.0)) for day, a in assumed.items()}
    always = missed(fastest, roomiest)
    return [WeekRisk(w.ref, w.title, w.due_ts, late[w.ref], runs, w.ref in always, w.basis, w.known) for w in items]


def describe_week(
    risks: Sequence[WeekRisk],
    now: float,
    *,
    versus: Sequence[WeekRisk] = (),
    first: str = "",
    assumptions: Sequence[str] = (),
    local_time: Callable[[float], datetime] = datetime.fromtimestamp,
) -> str:
    """The result for her brain to word: each deadline and how often it's missed, what rests on a guess, and what
    starting ``first`` changes against the usual order (``versus``)."""
    if not risks:
        return "Nothing with a due date in the next week."
    lines = []
    for risk in risks:
        extra = "; missed even if everything goes fast" if risk.even_if_fast else ""
        lines.append(
            f"- {risk.title} ({_due_phrase(risk.due_ts, now, local_time)}): {risk.words}{extra}. "
            f"Hours: {risk.basis}."
        )
    head = f"If you start with {first}:" if first else "How the week looks, in sampled runs:"
    parts = [head, *lines]
    before = {r.ref: r for r in versus}
    changes = [
        f"- {r.title}: {before[r.ref].words} in the usual order, {r.words} this way."
        for r in risks
        if r.ref in before and before[r.ref].words != r.words
    ]
    if versus:
        parts += ["Against soonest-due-first:", *(changes or ["- It changes nothing that shows."])]
    parts += [f"Assumed: {a}." for a in assumptions]
    if all(not r.known for r in risks):
        parts.append("Every number of hours here is a guess: ask how long things will take before leaning on this.")
    parts.append(WEEK_CAVEAT)
    return "\n".join(parts)
