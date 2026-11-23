"""Why she said it: every recommendation keeps the reasons it was made from.

A ``Trace`` is the record behind one remark: the events she saw, the patterns and predictions she leaned on, the
user's goals it serves, what she assumed, how sure she was and what reasoned it. It is stored with the
recommendation (``proactive``) as plain JSON, so "why did you say that?" is answered from what she actually had,
never made up after the fact.

``explain`` turns a trace into plain text for the brain to put in its own words. It keeps the kinds of knowledge
apart (knowledge in ``schema.Knowledge``): a pattern in her records is called a correlation and never proof,
unless an experiment where she varied one thing on purpose made it evidence of cause; a goal says whether the user
said it or she only noticed it; a probability comes with the counts it's from.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from octavia.futures.stats import percent

if TYPE_CHECKING:
    from octavia.futures.proactive import Recommendation

MAX_LISTED = 5  # a section names this many items, then counts the rest

_STRENGTH = {
    "insufficient": "not enough evidence yet",
    "weak": "weak evidence",
    "moderate": "moderate evidence",
    "strong": "strong evidence",
}
_SURE = {"insufficient": "not very (too little evidence yet)"}
_CLAIM = {
    "correlation": "a correlation in my records, not proof one causes the other",
    "causal": "tested in an experiment, so evidence of cause",
    "user": "something you told me",
}
_STATUS = {
    "speculative": "only a possible pattern so far",
    "testing": "I'm testing it now",
    "weakened": "it used to look stronger",
    "rejected": "the evidence has since gone against it",
    "stale": "nothing new about it in a while",
}
_POLICY = {
    "inform": "You asked me to let you know.",
    "suggest": "It was only a suggestion; you're free to ignore it.",
    "ask": "It's a question only you can answer.",
    "act": "It was harmless bookkeeping, so I did it without asking.",
    "act_and_inform": "It was harmless, so I did it and told you.",
    "require_confirmation": "Nothing happens unless you say yes.",
}
_HELD = {"suppressed": "I didn't say it", "pending": "I'm waiting for a better moment", "expired": "it was too late"}


@dataclass(frozen=True)
class Trace:
    """The reasons behind one remark. Each item is a small dict (shapes below) so it stores as JSON as is."""

    observations: tuple[dict[str, Any], ...] = ()  # {"event": id, "what": "…", "at": ts}
    hypotheses: tuple[dict[str, Any], ...] = ()  # {"id", "statement", "strength", "claim", "status"}
    predictions: tuple[dict[str, Any], ...] = ()  # {"id", "statement", "probability", "basis"}
    goals: tuple[dict[str, Any], ...] = ()  # {"id", "text", "label"} (label from Objective.label)
    assumptions: tuple[str, ...] = ()
    confidence: str = ""  # a Strength value
    provider: str = "deterministic"

    @property
    def empty(self) -> bool:
        """Nothing recorded: no reasons at all, however it was made."""
        return not (
            self.observations
            or self.hypotheses
            or self.predictions
            or self.goals
            or self.assumptions
            or self.confidence
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": [dict(o) for o in self.observations],
            "hypotheses": [dict(h) for h in self.hypotheses],
            "predictions": [dict(p) for p in self.predictions],
            "goals": [dict(g) for g in self.goals],
            "assumptions": list(self.assumptions),
            "confidence": self.confidence,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Trace:
        """A trace from ``to_dict``'s shape. Missing or malformed parts are left empty, never an error: an old
        record still explains what it can."""
        if not isinstance(data, Mapping):
            return cls()

        def items(name: str) -> tuple[dict[str, Any], ...]:
            raw = data.get(name)
            if not isinstance(raw, list | tuple):
                return ()
            return tuple(dict(item) for item in raw if isinstance(item, Mapping))

        raw_assumptions = data.get("assumptions")
        assumptions = (
            tuple(str(a) for a in raw_assumptions if a not in (None, ""))
            if isinstance(raw_assumptions, list | tuple)
            else ()
        )
        return cls(
            observations=items("observations"),
            hypotheses=items("hypotheses"),
            predictions=items("predictions"),
            goals=items("goals"),
            assumptions=assumptions,
            confidence=_text(data.get("confidence")),
            provider=_text(data.get("provider")) or "deterministic",
        )


# ---------------------------------------------------------------------------------------------- building traces
def observation(event_id: str | None, what: str, at: float | None = None) -> dict[str, Any]:
    """One observation for a trace: which event, what it was in a few words, when."""
    return {"event": event_id, "what": what, "at": at}


def hypothesis_entry(h: Mapping[str, Any] | object) -> dict[str, Any]:
    """A hypothesis (``hypotheses.Hypothesis`` or a mapping with its fields) as a trace item."""
    return {
        "id": _field(h, "id"),
        "statement": _text(_field(h, "statement")),
        "strength": _text(_field(h, "strength")),
        "claim": _text(_field(h, "claim")),
        "status": _text(_field(h, "status")),
    }


def prediction_entry(p: Mapping[str, Any] | object) -> dict[str, Any]:
    """A prediction (``predictions.Prediction`` or a mapping with its fields) as a trace item."""
    probability = _field(p, "probability")
    return {
        "id": _field(p, "id"),
        "statement": _text(_field(p, "statement")),
        "probability": None if probability is None else float(probability),
        "basis": _text(_field(p, "basis")),
    }


def goal_entry(o: Mapping[str, Any] | object) -> dict[str, Any]:
    """An objective (``objectives.Objective`` or a mapping with its fields) as a trace item."""
    return {"id": _field(o, "id"), "text": _text(_field(o, "text")), "label": _text(_field(o, "label"))}


# ---------------------------------------------------------------------------------------------- saying it
def describe_hypothesis(h: Mapping[str, Any] | object, *, quoted: bool = True) -> str:
    """One line for a hypothesis, with how much evidence there is and what kind of claim it is: '"To-dos due in
    the evening were finished on time 2 of 7 times..." (moderate evidence; a correlation in my records, not proof
    one causes the other)'."""
    statement = _text(_field(h, "statement")).strip() or "a pattern"
    notes = []
    strength = _text(_field(h, "strength"))
    if strength:
        notes.append(_STRENGTH.get(strength, f"{strength} evidence"))
    claim = _text(_field(h, "claim")) or "correlation"
    notes.append(_CLAIM.get(claim, _CLAIM["correlation"]))
    status = _text(_field(h, "status"))
    if status in _STATUS:
        notes.append(_STATUS[status])
    head = f'"{statement}"' if quoted else statement
    return f"{head} ({'; '.join(notes)})"


def explain(
    trace: Trace | Mapping[str, Any] | None,
    *,
    said: str = "",
    policy: str = "",
    status: str = "",
    reason: str = "",
    now: float | None = None,
    local_time: Callable[[float], datetime] = datetime.fromtimestamp,
) -> str:
    """The reasons in ``trace`` as plain text for the brain to put in its own words. ``said`` is the remark,
    ``policy`` and ``status`` how the gate treated it (``reason``: why it was held back). Observation times are
    local (``local_time``); with ``now``, one from another day also says which day."""
    if not isinstance(trace, Trace):
        trace = Trace.from_dict(trace)
    policy, status = _text(policy), _text(status)
    remark = f'"{said.strip()}"' if said.strip() else "that"
    if trace.empty:
        return f"I have no record of why I said {remark}." if said.strip() else "I have no record of why."
    lines = [_header(remark, status, reason)]
    if trace.observations:
        seen = [_observation(o, now, local_time) for o in trace.observations]
        lines.append("- What I saw: " + _listed([s for s in seen if s]))
    if trace.hypotheses:
        lines.append("- Patterns: " + _listed([describe_hypothesis(h) for h in trace.hypotheses]))
    if trace.predictions:
        lines.append("- Prediction: " + _listed([_prediction(p) for p in trace.predictions]))
    if trace.goals:
        lines.append("- Your goals: " + _listed([_goal(g) for g in trace.goals]))
    if trace.assumptions:
        lines.append("- Assumptions: " + _listed(list(trace.assumptions)))
    if policy in _POLICY:
        lines.append("- " + _POLICY[policy])
    made_by = "deterministic rules" if trace.provider == "deterministic" else trace.provider
    sure = f"How sure I am: {_SURE.get(trace.confidence, trace.confidence)}. " if trace.confidence else ""
    lines.append(f"- {sure}Made by: {made_by}.")
    return "\n".join(line for line in lines if not line.endswith(": "))


def explain_recommendation(
    recommendation: Recommendation,
    *,
    now: float | None = None,
    local_time: Callable[[float], datetime] = datetime.fromtimestamp,
) -> str:
    """``explain`` for a recommendation the gate kept: its own trace, text, policy, status and reason."""
    return explain(
        Trace.from_dict(recommendation.trace),
        said=recommendation.text,
        policy=_text(recommendation.policy),
        status=recommendation.status,
        reason=recommendation.reason,
        now=now,
        local_time=local_time,
    )


# ---------------------------------------------------------------------------------------------- helpers
def _header(remark: str, status: str, reason: str) -> str:
    held = _HELD.get(status)
    if held is None:
        return f"Why I said {remark}:"
    why = f": {reason}" if reason and reason != "expired" else ""
    return f"Why I thought of saying {remark} ({held}{why}):"


def _observation(o: Mapping[str, Any], now: float | None, local_time: Callable[[float], datetime]) -> str:
    what = _text(o.get("what")).strip()
    if not what:
        return ""
    at = o.get("at")
    if not isinstance(at, int | float) or isinstance(at, bool) or at <= 0:
        return what
    return f"{what} (at {_when(float(at), now, local_time)})"


def _when(ts: float, now: float | None, local_time: Callable[[float], datetime]) -> str:
    moment = local_time(ts)
    clock = f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
    if now is None or local_time(now).date() == moment.date():
        return clock
    return f"{clock} on {moment.strftime('%a')} {moment.day} {moment.strftime('%b')}"


def _prediction(p: Mapping[str, Any]) -> str:
    statement = _text(p.get("statement")).strip() or "a prediction"
    probability, basis = p.get("probability"), _text(p.get("basis")).strip()
    notes = []
    if isinstance(probability, int | float) and not isinstance(probability, bool):
        notes.append(percent(float(probability)))
    if basis:
        notes.append(basis)
    return f'"{statement}"' + (f" ({': '.join(notes)})" if notes else "")


def _goal(g: Mapping[str, Any]) -> str:
    text, label = _text(g.get("text")).strip(), _text(g.get("label")).strip()
    return f'"{text}"' + (f" ({label})" if label else "") if text else ""


def _listed(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) > MAX_LISTED:
        items = [*items[:MAX_LISTED], f"and {len(items) - MAX_LISTED} more"]
    return "; ".join(items)


def _field(obj: Mapping[str, Any] | object, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.value if isinstance(value, Enum) else str(value)
