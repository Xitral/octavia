"""The vocabulary Octavia Futures speaks: event types, domains, sensitivity, the kinds of knowledge and the
statuses its beliefs go through.

Event types are registered here, not made up where they're emitted: a typo'd type would quietly start a second
stream nobody reads, so ``EventLog.append`` refuses one it doesn't know. Each type carries the domain it belongs to
(what the user can put off-limits) and how sensitive it is by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Sensitivity(str, Enum):
    """How careful to be with something: said out loud, put in a prompt, kept at all."""

    PUBLIC = "public"  # nothing personal: Octavia started, a timer rang
    PERSONAL = "personal"  # about the user's life: tasks, requests, apps used
    SENSITIVE = "sensitive"  # grades, health, money, other people's messages: never spoken unprompted


class Knowledge(str, Enum):
    """What kind of claim a piece of knowledge is. Octavia must never present one kind as another."""

    FACT = "fact"  # read from a source of truth: a task's due date in TickTick
    OBSERVATION = "observation"  # something that happened, seen directly: an event
    CORRELATION = "correlation"  # two things go together in her records; nothing more
    HYPOTHESIS = "hypothesis"  # a possible pattern without enough evidence yet
    PREDICTION = "prediction"  # a guess about what will happen, scored later
    CAUSAL_CLAIM = "causal_claim"  # one thing makes a difference: only an experiment earns this
    USER_PREFERENCE = "user_preference"  # the user said so


class HypothesisStatus(str, Enum):
    SPECULATIVE = "speculative"  # a possible pattern, too little evidence to lean on
    OBSERVED = "observed"  # the pattern holds in her records so far (a correlation)
    TESTING = "testing"  # an experiment is running on it
    SUPPORTED = "supported"  # strong evidence, or an experiment confirmed it
    WEAKENED = "weakened"  # it used to look stronger than it does now
    REJECTED = "rejected"  # the evidence (or an experiment) went against it
    STALE = "stale"  # no new evidence for a long time


class Strength(str, Enum):
    """How much evidence there is for a pattern: a category earned from counts, never a made-up percentage."""

    INSUFFICIENT = "insufficient"
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


STRENGTH_ORDER = {Strength.INSUFFICIENT: 0, Strength.WEAK: 1, Strength.MODERATE: 2, Strength.STRONG: 3}


class Policy(str, Enum):
    """What the proactive loop does about something that might deserve attention."""

    IGNORE = "ignore"
    INFORM = "inform"  # tell the user (she was asked to: a "remind me when" firing)
    SUGGEST = "suggest"  # offer an idea once; the user may ignore it
    ASK = "ask"  # a question only the user can answer ("should I treat this as a goal?")
    ACT = "act"  # do something internal and harmless without saying so (bookkeeping)
    ACT_AND_INFORM = "act_and_inform"  # do something harmless and say so
    REQUIRE_CONFIRMATION = "require_confirmation"  # anything with side effects: only the user's yes starts it


class Domain(str, Enum):
    """What part of the user's life something belongs to: the unit of the user's privacy controls."""

    ASSISTANT = "assistant"  # Octavia herself: replies, corrections, feedback
    GENERAL = "general"
    COMPUTER = "computer"  # apps, windows, mouse and keyboard actions
    PRODUCTIVITY = "productivity"  # to-dos
    SCHOOL = "school"  # Canvas assignments and grades
    COMMUNICATIONS = "communications"  # email and messages
    TIME = "time"  # timers
    GOALS = "goals"
    ROUTINES = "routines"  # scheduled jobs
    SELF = "self"  # her self-improvement builds
    FUTURES = "futures"  # this system's own bookkeeping
    RELATIONSHIPS = "relationships"
    HEALTH = "health"
    FINANCE = "finance"
    EMPLOYMENT = "employment"
    PEOPLE = "people"


# Where Octavia may never vary what she does to see what happens. An experiment here can only be one the user runs
# on themselves, knowingly, with her just keeping count (experiments.ExperimentPolicy).
PROTECTED_DOMAINS = frozenset(
    {
        Domain.RELATIONSHIPS,
        Domain.HEALTH,
        Domain.FINANCE,
        Domain.SCHOOL,
        Domain.EMPLOYMENT,
        Domain.PEOPLE,
        Domain.COMMUNICATIONS,
    }
)


@dataclass(frozen=True)
class EventType:
    name: str
    domain: Domain
    sensitivity: Sensitivity
    description: str


_TYPE = re.compile(r"^[a-z]+(?:_[a-z]+)*\.[a-z]+(?:_[a-z]+)*$")

_P, _S, _U = Sensitivity.PERSONAL, Sensitivity.SENSITIVE, Sensitivity.PUBLIC
_D = Domain

# fmt: off
_TYPES: tuple[tuple[str, Domain, Sensitivity, str], ...] = (
    ("assistant.started", _D.ASSISTANT, _U, "Octavia started"),
    ("assistant.stopped", _D.ASSISTANT, _U, "Octavia stopped"),
    ("assistant.reply", _D.ASSISTANT, _P, "a brain turn finished: how long it took, whether it failed or was cut off"),
    ("user.request", _D.GENERAL, _P, "the user asked for something, by voice or typed"),
    ("user.feedback", _D.ASSISTANT, _P, "thanks, a dismissal, a declined action, or an explicit rating"),
    ("user.correction", _D.ASSISTANT, _P, "the user corrected what she just said or did"),
    ("user.reaction", _D.ASSISTANT, _P, "how the user took her last reply: thanked, corrected, cut short... (selfscore)"),
    ("computer.application_opened", _D.COMPUTER, _P, "an app was opened (by her, or seen coming up)"),
    ("computer.application_closed", _D.COMPUTER, _P, "an app was closed"),
    ("computer.context_changed", _D.COMPUTER, _P, "another app came to the front and stayed there"),
    ("computer.action_performed", _D.COMPUTER, _P, "one of her actions on the computer worked"),
    ("computer.action_failed", _D.COMPUTER, _P, "one of her actions failed or was refused"),
    ("assistant.claim_corrected", _D.ASSISTANT, _P, "a sentence of hers her tools contradicted was said honestly instead"),
    ("task.created", _D.PRODUCTIVITY, _P, "a to-do was created"),
    ("task.updated", _D.PRODUCTIVITY, _P, "a to-do was changed (a new due date, a new title)"),
    ("task.completed", _D.PRODUCTIVITY, _P, "a to-do was ticked off"),
    ("task.deleted", _D.PRODUCTIVITY, _P, "a to-do was deleted"),
    ("task.seen", _D.PRODUCTIVITY, _P, "a dated to-do she hadn't seen before showed up in TickTick"),
    ("task.overdue", _D.PRODUCTIVITY, _P, "a to-do's due time passed with it still open"),
    ("task.cleared", _D.PRODUCTIVITY, _P, "the last thing due today was ticked off"),
    ("canvas.assignment_created", _D.SCHOOL, _P, "a new assignment, quiz or discussion showed up in Canvas"),
    ("canvas.assignment_updated", _D.SCHOOL, _P, "an assignment's due date or title changed"),
    ("canvas.assignment_submitted", _D.SCHOOL, _P, "an assignment was submitted"),
    ("canvas.grade_posted", _D.SCHOOL, _S, "a grade was released"),
    ("gmail.message_received", _D.COMMUNICATIONS, _P, "new mail arrived (sender only; never the body)"),
    ("gmail.message_sent", _D.COMMUNICATIONS, _P, "the user sent mail"),
    ("timer.created", _D.TIME, _U, "a timer was set"),
    ("timer.completed", _D.TIME, _U, "a timer rang"),
    ("timer.cancelled", _D.TIME, _U, "a timer was cancelled"),
    ("goal.created", _D.GOALS, _P, "the user set a goal or priority"),
    ("goal.progress", _D.GOALS, _P, "something moved a goal forward"),
    ("goal.completed", _D.GOALS, _P, "a goal was reached"),
    ("routine.started", _D.ROUTINES, _P, "a scheduled job started"),
    ("routine.completed", _D.ROUTINES, _P, "a scheduled job finished (or failed)"),
    ("build.started", _D.SELF, _U, "a self-improvement build started"),
    ("build.patch_created", _D.SELF, _U, "a build produced a change and it's being checked"),
    ("build.test_passed", _D.SELF, _U, "a build passed its checks: it's ready to install"),
    ("build.test_failed", _D.SELF, _U, "a build's change failed its checks"),
    ("build.failure", _D.SELF, _U, "a build failed, changed nothing, or was refused"),
    ("build.installed", _D.SELF, _U, "an improvement was installed"),
    ("build.rolled_back", _D.SELF, _U, "an installed improvement was undone"),
    ("memory.saved", _D.GENERAL, _P, "she saved a long-term memory"),
    ("memory.forgotten", _D.GENERAL, _P, "a long-term memory was deleted"),
    ("prediction.created", _D.FUTURES, _P, "she made a prediction"),
    ("prediction.resolved", _D.FUTURES, _P, "a prediction came true or didn't"),
    ("experiment.started", _D.FUTURES, _P, "an experiment the user approved started"),
    ("experiment.completed", _D.FUTURES, _P, "an experiment finished (or was stopped)"),
    ("trigger.fired", _D.FUTURES, _P, "a \"remind me when\" condition happened"),
    ("recommendation.delivered", _D.FUTURES, _P, "she said something on her own"),
    ("recommendation.feedback", _D.FUTURES, _P, "how the user took something she said on her own"),
    ("hypothesis.changed", _D.FUTURES, _P, "a hypothesis was formed, strengthened, weakened or dropped"),
    ("futures.failure", _D.FUTURES, _U, "part of Futures itself failed (for self-healing)"),
)
# fmt: on

EVENT_TYPES: dict[str, EventType] = {name: EventType(name, domain, sens, what) for name, domain, sens, what in _TYPES}
assert all(_TYPE.match(name) for name in EVENT_TYPES), "event types look like 'area.what_happened'"


def event_type(name: str) -> EventType:
    """The registered type ``name``; a ValueError for one nobody registered."""
    found = EVENT_TYPES.get(name)
    if found is None:
        raise ValueError(f"unknown Futures event type {name!r} (register it in futures/schema.py)")
    return found


_NAME_KEYED = frozenset({"application", "person", "course", "project", "model", "skill"})


def ref(kind: str, key: object) -> str:
    """The reference of an entity: ``task:65a1...``, ``application:blender``. Keys are case-sensitive except for
    the kinds whose keys are names (``application``, ``person``, ``course``, ``project``), which are lowercased
    so "Blender" and "blender" are one app."""
    key = " ".join(str(key).split())
    if kind in _NAME_KEYED:
        key = key.lower()
    if not kind or not key:
        raise ValueError("an entity reference needs a kind and a key")
    return f"{kind}:{key}"


def split_ref(value: str) -> tuple[str, str]:
    kind, _, key = value.partition(":")
    return kind, key
