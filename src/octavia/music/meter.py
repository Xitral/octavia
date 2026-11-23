"""Time signatures as the composer uses them: how long a bar is, where its beats fall and which are strong.

Everything is in quarter-note beats ("qn"). 4/4 has four one-beat pulses with strong beats on 1 and 3; 6/8, 9/8
and 12/8 are compound, felt in dotted quarters; odd meters are grouped in twos and threes (5/4 as 3+2, 7/8 as
2+2+3), and the first beat of each group is strong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STEP = 0.25  # one sixteenth note, the finest grid patterns use

_SIGNATURE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*$")


class MeterError(ValueError):
    pass


@dataclass(frozen=True)
class Meter:
    num: int
    den: int

    @classmethod
    def parse(cls, text: str) -> Meter:
        match = _SIGNATURE.match(text)
        if not match:
            raise MeterError(f"'{text}' isn't a time signature; write it like 4/4, 3/4, 6/8 or 7/8.")
        num, den = int(match.group(1)), int(match.group(2))
        if den not in (2, 4, 8, 16) or not 1 <= num <= 15:
            raise MeterError(
                f"I can't write a {num}/{den} time signature: the top number goes from 1 to 15 and the bottom one "
                "is 2, 4, 8 or 16."
            )
        return cls(num, den)

    @property
    def text(self) -> str:
        return f"{self.num}/{self.den}"

    @property
    def bar(self) -> float:
        """Beats (quarter notes) in a bar."""
        return self.num * 4 / self.den

    @property
    def steps(self) -> int:
        """Sixteenths in a bar (rounded: 7/16 has 7)."""
        return max(1, round(self.bar / STEP))

    @property
    def is_common(self) -> bool:
        """4/4 (or 2/2): the meter the genre patterns are written in."""
        return self.bar == 4 and self.num in (4, 2)

    @property
    def compound(self) -> bool:
        return self.den == 8 and self.num in (6, 9, 12)

    @property
    def groups(self) -> list[float]:
        """The bar's beats, as lengths in quarter notes: 4/4 -> [1, 1, 1, 1]; 6/8 -> [1.5, 1.5]; 7/8 -> [1, 1, 1.5].
        Eighth-note meters are felt in groups of two and three eighths."""
        unit = 4 / self.den
        if self.compound:
            return [1.5] * (self.num // 3)
        if self.den >= 8:
            return [count * unit for count in _grouping(self.num)]
        return [unit] * self.num

    @property
    def pulses(self) -> list[float]:
        """Where each beat starts, in beats from the bar's start."""
        out, at = [], 0.0
        for length in self.groups:
            out.append(at)
            at += length
        return out

    @property
    def strong(self) -> set[float]:
        """The strong beats: the downbeat, and the middle of an even bar (beat 3 of 4/4, the second group of 5/4
        felt 3+2)."""
        pulses = self.pulses
        if len(pulses) % 2 == 0 and len(pulses) >= 4:
            return {pulses[0], pulses[len(pulses) // 2]}
        if len(pulses) >= 5:
            first = _grouping(len(pulses))[0]
            return {pulses[0], pulses[first]}
        return {pulses[0]}

    def strength(self, position: float) -> int:
        """How strong a moment in the bar is: 3 strong beat, 2 other beat, 1 eighth, 0 anything finer."""
        position = position % self.bar
        if any(abs(position - s) < 1e-6 for s in self.strong):
            return 3
        if any(abs(position - p) < 1e-6 for p in self.pulses):
            return 2
        if abs(position * 2 - round(position * 2)) < 1e-6:
            return 1
        return 0


def _grouping(count: int) -> list[int]:
    """Split a count of beats into twos and threes, threes last (7 -> 2+2+3; 5 -> 3+2 is the usual feel)."""
    if count == 5:
        return [3, 2]
    if count <= 3:
        return [count]
    groups = []
    rest = count
    while rest > 4:
        groups.append(2)
        rest -= 2
    if rest == 4:
        groups.extend([2, 2])
    else:
        groups.append(rest)
    return groups
