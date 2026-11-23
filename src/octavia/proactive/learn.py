"""What would the user pick? Her guess at each answer from how they answered before, and how often she's right.

Plain counting, no model. A pick is "wanted": an approval, or a redirect (they wanted it, done differently); a
rejection isn't. The guess for a new item starts from all their answers, narrows to items of the same kind, then to
the same kind for the same course or project, each level pulled toward the one above it (an m-estimate) so two
answers don't make a certainty. It's made when the item is planned, before the user sees anything, and kept on the
item, so ``agreement`` scores real predictions, never hindsight.

Her guesses only steer the plan (``steering``) once there are enough answers and she's been right often enough;
until then they're recorded and scored, and the plan goes by the rules and the ranker alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from octavia.proactive.store import APPROVE, REDIRECT, REJECT, Item

PRIOR = 0.6  # before any answer: a little more likely wanted than not (it was picked as worth doing)
M = 2.0  # how many answers' weight the level above carries
DROP_BELOW = 0.3  # once steering, a candidate she guesses this unlikely to be wanted isn't prepared


@dataclass(frozen=True)
class Guess:
    p: float  # that it's wanted
    basis: str


@dataclass(frozen=True)
class Agreement:
    n: int  # decided items that had a guess
    hits: int
    brier: float | None  # mean squared error of the guesses (lower is better; always guessing 0.5 scores 0.25)

    @property
    def rate(self) -> float | None:
        return self.hits / self.n if self.n else None

    def describe(self) -> str:
        if not self.n:
            return "no answers to score my guesses against yet"
        rate = round(100 * (self.rate or 0))
        return f"I guessed {self.hits} of your last {self.n} answers right ({rate}%)"


def wanted(decision: str) -> bool | None:
    if decision in (APPROVE, REDIRECT):
        return True
    if decision == REJECT:
        return False
    return None


def _answers(history: Iterable[Item]) -> list[tuple[Mapping[str, str], bool]]:
    out = []
    for item in history:
        verdict = wanted(item.decision)
        if verdict is not None:
            out.append((item.features or {"kind": item.kind}, verdict))
    return out


def _estimate(rows: list[bool], prior: float) -> float:
    return (sum(rows) + M * prior) / (len(rows) + M)


def predict(features: Mapping[str, str], history: Iterable[Item]) -> Guess:
    """Her guess that an item with ``features`` is wanted, from the answers in ``history``."""
    answers = _answers(history)
    if not answers:
        return Guess(PRIOR, "no answers yet")
    kind, course = features.get("kind", ""), features.get("course", "")
    p = _estimate([w for _, w in answers], PRIOR)
    basis = f"{sum(w for _, w in answers)} of {len(answers)} wanted overall"
    same_kind = [w for f, w in answers if f.get("kind") == kind]
    if same_kind:
        p = _estimate(same_kind, p)
        basis = f"{sum(same_kind)} of {len(same_kind)} {kind} items wanted"
    else:  # a kind never answered: turning down meeting sheets says little about exam reviews
        return Guess(round((p + PRIOR) / 2, 3), f"no answers about {kind} items yet ({basis})")
    if course:
        same = [w for f, w in answers if f.get("kind") == kind and f.get("course") == course]
        if same:
            p = _estimate(same, p)
            basis = f"{sum(same)} of {len(same)} {kind} items for {course} wanted"
    return Guess(round(p, 3), basis)


def agreement(items: Iterable[Item], last: int | None = 50) -> Agreement:
    """How her guesses did against the answers given (the newest ``last``)."""
    scored = [(i.prediction, wanted(i.decision)) for i in items if i.prediction is not None and i.decided]
    scored = [(p, w) for p, w in scored if w is not None]
    if last is not None:
        scored = scored[-last:]
    if not scored:
        return Agreement(0, 0, None)
    hits = sum((p >= 0.5) == w for p, w in scored)
    brier = sum((p - (1.0 if w else 0.0)) ** 2 for p, w in scored) / len(scored)
    return Agreement(len(scored), hits, round(brier, 3))


def steering(items: Iterable[Item], *, min_decisions: int, min_agreement: float) -> bool:
    """Whether her guesses have earned a say in the plan."""
    score = agreement(items)
    return score.n >= min_decisions and (score.rate or 0.0) >= min_agreement
