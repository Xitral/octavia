"""The arithmetic of evidence: counts, intervals and the categories confidence is expressed in.

Octavia never says "93.7% sure". What she has is counts ("7 of the last 9"), and what she says is derived from
them: a rate with an honest interval (Wilson), a category (``Strength``) that needs both a big enough sample and a
clear enough difference, and probabilities rounded to the nearest 5 % with words for them. With few samples the
answer is "not enough evidence yet", whatever the rate looks like.

All of it is plain deterministic code: no model is asked how confident to be.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from octavia.futures.schema import Strength

Z95 = 1.96


@dataclass(frozen=True)
class Tally:
    """``successes`` out of ``trials``."""

    successes: int = 0
    trials: int = 0

    def __add__(self, other: Tally) -> Tally:
        return Tally(self.successes + other.successes, self.trials + other.trials)

    @property
    def failures(self) -> int:
        return self.trials - self.successes

    @property
    def rate(self) -> float | None:
        return self.successes / self.trials if self.trials else None

    def wilson(self, z: float = Z95) -> tuple[float, float]:
        """The 95 % Wilson score interval for the rate: honest with small samples, never outside 0..1."""
        n = self.trials
        if n == 0:
            return 0.0, 1.0
        p = self.successes / n
        centre = (p + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        return max(0.0, centre - half), min(1.0, centre + half)

    def describe(self) -> str:
        """ "7 of 9"."""
        return f"{self.successes} of {self.trials}"

    def estimate(self, prior: tuple[float, float] = (1.0, 1.0)) -> float:
        """The rate's expected value under a Beta prior (the default is "anything is equally likely"): with no data
        it's 0.5, and each trial moves it less than the last."""
        a, b = prior
        return (self.successes + a) / (self.trials + a + b)


def sample_strength(trials: int) -> Strength:
    """How much a rate from ``trials`` samples can be leaned on, whatever it is."""
    if trials < 3:
        return Strength.INSUFFICIENT
    if trials < 8:
        return Strength.WEAK
    if trials < 20:
        return Strength.MODERATE
    return Strength.STRONG


@dataclass(frozen=True)
class Comparison:
    """How one group's rate differs from another's."""

    group: Tally
    other: Tally
    difference: float  # group rate minus other rate
    z: float  # two-proportion z statistic
    strength: Strength  # of the evidence that the difference is real
    against: bool  # plenty of data and no real difference: evidence the difference isn't there

    @property
    def direction(self) -> int:
        return 0 if self.difference == 0 else (1 if self.difference > 0 else -1)


def compare(group: Tally, other: Tally) -> Comparison:
    """Is ``group``'s rate really different from ``other``'s? The category needs both samples to be big enough
    and the difference to be clear enough: a big gap in 2 vs 3 samples is still insufficient evidence."""
    diff = (group.rate or 0.0) - (other.rate or 0.0)
    n1, n2 = group.trials, other.trials
    if n1 == 0 or n2 == 0:
        return Comparison(group, other, diff, 0.0, Strength.INSUFFICIENT, False)
    pooled = (group.successes + other.successes) / (n1 + n2)
    spread = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    z = diff / spread if spread > 0 else 0.0
    total, smallest = n1 + n2, min(n1, n2)
    if smallest < 3 or total < 8:
        strength = Strength.INSUFFICIENT
    elif abs(z) >= 2.58 and total >= 30 and smallest >= 8:
        strength = Strength.STRONG
    elif abs(z) >= Z95 and total >= 12:
        strength = Strength.MODERATE
    elif abs(z) >= 1.28:
        strength = Strength.WEAK
    else:
        strength = Strength.INSUFFICIENT
    against = total >= 20 and smallest >= 6 and abs(z) < 1.0
    return Comparison(group, other, diff, z, strength, against)


# ---------------------------------------------------------------------------------------------- probabilities
def percent(p: float) -> str:
    """ "about 70%": rounded to the nearest 5, so it never pretends to more precision than there is."""
    return f"about {round(p * 20) * 5}%"


def likelihood(p: float) -> str:
    if p < 0.15:
        return "very unlikely"
    if p < 0.35:
        return "unlikely"
    if p < 0.65:
        return "a toss-up"
    if p < 0.85:
        return "likely"
    return "very likely"


def brier(pairs: Iterable[tuple[float, bool]]) -> float | None:
    """Mean squared error of probabilities against what happened: 0 is perfect, 0.25 is always saying 50 %."""
    pairs = list(pairs)
    if not pairs:
        return None
    return sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in pairs) / len(pairs)


@dataclass(frozen=True)
class Bucket:
    label: str  # "60-80%"
    predictions: int
    came_true: int
    mean_probability: float


def calibration(pairs: Iterable[tuple[float, bool]], edges: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)) -> list[Bucket]:
    """Predictions grouped by the probability given, with how many came true: if the 60-80 % ones come true
    about 70 % of the time, she's calibrated."""
    bounds = (0.0, *edges, 1.0000001)
    groups: list[list[tuple[float, bool]]] = [[] for _ in range(len(bounds) - 1)]
    for p, hit in pairs:
        for i in range(len(bounds) - 1):
            if bounds[i] <= p < bounds[i + 1]:
                groups[i].append((p, hit))
                break
    buckets = []
    for i, members in enumerate(groups):
        if not members:
            continue
        lo, hi = round(bounds[i] * 100), round(min(bounds[i + 1], 1.0) * 100)
        buckets.append(
            Bucket(
                label=f"{lo}-{hi}%",
                predictions=len(members),
                came_true=sum(1 for _, hit in members if hit),
                mean_probability=sum(p for p, _ in members) / len(members),
            )
        )
    return buckets
