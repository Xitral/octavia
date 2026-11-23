"""What every simulation is made of: a few sliders, and the picture for each mix of their positions.

A simulation (A*'s heuristic weight, a game tree's move order, and whatever a later subject adds: a projectile's
launch angle, a CSP's variable order, a Bayes net's evidence) is one ``Simulation``: it makes a problem to play with
or takes the practice problem the user is on, says what its sliders are, and draws one ``Frame`` for each mix of
positions. ``register`` adds one; the ``simulate`` tool knows nothing about any of them, so a new subject is a new
module with its simulations and nothing else changes.

``build`` works out every frame up front (a drag on the panel never waits on her), shares the frames that come out
the same, and says the whole sweep in words, so she can talk about what the sliders do without seeing the panel.
"""

from __future__ import annotations

import itertools
import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from octavia.visuals.sim import MAX_FRAMES, SimBlock, aspect_of

SWEEP_LINES = 40  # at most this many lines of the sweep go to her


@dataclass(frozen=True)
class Param:
    """A slider: its ``label``, the ``values`` it shows (words) and the ``settings`` behind them (what ``frame``
    gets), and where it starts."""

    label: str
    values: tuple[str, ...]
    settings: tuple[Any, ...]
    start: int = 0

    def __post_init__(self) -> None:
        if len(self.values) != len(self.settings) or len(self.values) < 2:
            raise ValueError(f"slider {self.label!r} needs two or more values, each with its setting")
        if not 0 <= self.start < len(self.values):
            raise ValueError(f"slider {self.label!r} starts outside its values")


@dataclass
class Frame:
    """What the panel shows for one mix of slider positions: a drawing, a line or two under it, and up to four
    numbers (label, value) that change with the sliders."""

    svg: str
    caption: str = ""
    facts: list[tuple[str, str]] = field(default_factory=list)


class Simulation(ABC):
    """One simulation. Subclass, fill in the class attributes and methods, and ``register`` it."""

    name: ClassVar[str]  # short and unique, what the tool is called with: "astar_weight"
    topic: ClassVar[str]  # how it's said: "A* heuristic weight"
    subject: ClassVar[str]  # what it belongs to: "AI: search"
    about: ClassVar[str]  # what the sliders change and what to watch, in a sentence, for the tool's description
    problem_kinds: ClassVar[tuple[str, ...]] = ()  # practice problem kinds whose problems it can run on

    @abstractmethod
    def make(self, rng: random.Random) -> dict[str, Any]:
        """A problem to play with, as plain data, chosen so the sliders visibly change something."""

    def from_problem(self, kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """The data for a practice problem of ``kind`` (one of ``problem_kinds``), or None when it can't be used."""
        return None

    @abstractmethod
    def params(self, data: dict[str, Any]) -> list[Param]:
        """The sliders (one to three), for this problem."""

    @abstractmethod
    def frame(self, data: dict[str, Any], settings: tuple[Any, ...]) -> Frame:
        """The picture for one setting of each slider (in ``params`` order)."""

    def lesson(self, data: dict[str, Any]) -> str:
        """What to point out about the whole sweep, in a sentence or two ("" for nothing more than the numbers)."""
        return ""


_SIMS: dict[str, Simulation] = {}


def register(sim: Simulation) -> Simulation:
    """Add a simulation (a name already taken is replaced, so a subject can refine one)."""
    _SIMS[sim.name] = sim
    return sim


def simulations() -> dict[str, Simulation]:
    return dict(_SIMS)


def get_simulation(name: str) -> Simulation | None:
    return _SIMS.get(name.strip().casefold())


@dataclass
class Built:
    block: SimBlock
    sweep: list[str]  # one line per mix of positions: "Weight 1.5: Expanded 6, Path cost 15"
    frames: int


def build(sim: Simulation, data: dict[str, Any], caption: str = "") -> Built:
    """Every frame of ``sim`` on ``data``, as a panel block, and the sweep in words. Raises ValueError when there'd
    be more frames than a panel takes."""
    params = sim.params(data)
    mixes = math.prod(len(p.values) for p in params)
    if mixes > MAX_FRAMES:
        raise ValueError(f"{sim.topic} would need {mixes} pictures (at most {MAX_FRAMES})")
    frames: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    index: list[int] = []
    sweep: list[str] = []
    for picks in itertools.product(*(range(len(p.values)) for p in params)):
        frame = sim.frame(data, tuple(p.settings[k] for p, k in zip(params, picks, strict=True)))
        wire = {
            "svg": frame.svg,
            "caption": frame.caption,
            "facts": [{"label": label, "value": value} for label, value in frame.facts[:4]],
        }
        key = repr(wire)
        if key not in seen:
            seen[key] = len(frames)
            frames.append(wire)
        index.append(seen[key])
        where = ", ".join(f"{p.label} = {p.values[k]}" for p, k in zip(params, picks, strict=True))
        numbers = ", ".join(f"{label} {value}" for label, value in frame.facts)
        sweep.append(f"{where}; {numbers}" if numbers else where)
    block = SimBlock.model_validate(
        {
            "kind": "sim",
            "params": [{"label": p.label, "values": list(p.values), "start": p.start} for p in params],
            "frames": frames,
            "index": index,
            "aspect": aspect_of([f["svg"] for f in frames]),
            "alt": sim.topic,
            "caption": caption,
        }
    )
    return Built(block, sweep, len(frames))
