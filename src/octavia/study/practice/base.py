"""What every kind of practice problem is made of, and the book that keeps them.

A kind of problem (alpha-beta pruning, A* search, and whatever a later subject adds: resolution proofs, CSP
backtracking, Bayes nets, a physics formula) is one ``PracticeKind``: it makes a problem at random or reads one the user
gives, says it in words without the answer, grades an answer exactly, walks through the solution step by step and
draws it. ``register`` adds a kind; the tools (``tools/extensions/practice.py``) know nothing about any one of them, so
a new subject is a new module with its kinds and nothing else changes.

A problem is kept as plain data (``Problem.data``, whatever its kind needs), in ``data/study/problems.json`` (the
last ``KEEP``), so "check my answer" works after a restart.
"""

from __future__ import annotations

import random
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from octavia.core.jsonfile import read_json, write_json

KEEP = 30
Theme = Literal["paper", "panel"]
Size = Literal["exam", "big"]  # exam: the size of an exam question (the default); big: like a long activity sheet


class Parts(BaseModel):
    """The base of a kind's ``Given`` (a problem the user typed in) and ``Answer`` models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoGiven(Parts):
    pass


@dataclass
class Problem:
    id: int
    kind: str
    created: float
    data: dict[str, Any] = field(default_factory=dict)
    source: str = "made up"  # or "from the user" (typed in from slides or homework)


@dataclass
class Grade:
    right: bool
    lines: list[str]
    first_wrong: int | None = None  # the walkthrough step (from 0) to show where it went wrong


@dataclass(frozen=True)
class Blank:
    """A place on a worksheet to write in: at (``x``, ``y``) in its drawing's coordinates. ``kind``: "number" or
    "text" (a box to type in), or "cut" (a mark to click on, for crossing out a branch)."""

    id: str
    x: float
    y: float
    kind: Literal["number", "text", "cut"] = "number"
    label: str = ""  # shown faintly in the box ("α", "#"), or beside it
    width: float = 44


@dataclass
class Worksheet:
    """A problem to work on the page: its drawing (``svg``, ``width`` x ``height``) with ``blanks`` on it, and what
    to do, in a line or two."""

    svg: str
    width: float
    height: float
    blanks: list[Blank]
    instructions: str


@dataclass
class Marked:
    """A worksheet graded: the grade, each blank's mark ("right", "wrong", "missed" or "extra"; a blank left empty
    that should be is unmarked), and what belongs in each one that isn't right, shown after grading."""

    grade: Grade
    marks: dict[str, str]
    expected: dict[str, str]


@dataclass(frozen=True)
class Step:
    """One step of a walkthrough: what she says, and ``at``, what the kind's ``picture`` shows for it."""

    words: str
    at: int


class PracticeKind(ABC):
    """One kind of practice problem. Subclass, fill in the class attributes and methods, and ``register`` it."""

    name: ClassVar[str]  # short and unique, what the tools are called with: "alphabeta"
    topic: ClassVar[str]  # how it's said and recorded: "Alpha-beta pruning"
    subject: ClassVar[str]  # what it belongs to: "AI: game trees"
    Given: ClassVar[type[Parts]] = NoGiven  # a problem the user types in, from their slides or homework
    Answer: ClassVar[type[Parts]]  # what an answer is made of
    given_hint: ClassVar[str] = ""  # how to give one, for the tool's description
    answer_hint: ClassVar[str] = ""  # what to send as the answer, for the tool's description

    @abstractmethod
    def make(self, rng: random.Random, size: Size = "exam") -> dict[str, Any]:
        """A new problem worth practising on, as plain data, of ``size``."""

    def parse(self, given: Parts) -> dict[str, Any]:
        """The problem the user gave, as plain data. Raises ValueError saying what's wrong."""
        raise ValueError(f"{self.topic} problems can't be typed in yet; ask for a made-up one")

    @abstractmethod
    def statement(self, data: dict[str, Any]) -> str:
        """The problem in words (for the brain, and the user when no panel can show it). Never the answer."""

    @abstractmethod
    def ask(self, data: dict[str, Any]) -> str:
        """What to answer, in a line: "the root's value, and which leaves are pruned"."""

    @abstractmethod
    def grade(self, data: dict[str, Any], answer: Parts) -> Grade:
        """``answer`` checked exactly, with the first step that went wrong."""

    @abstractmethod
    def steps(self, data: dict[str, Any]) -> list[Step]:
        """The solution, one step at a time."""

    @abstractmethod
    def summary(self, data: dict[str, Any]) -> str:
        """The finished answer, in a line."""

    def picture(self, data: dict[str, Any], at: int | None = None) -> str:
        """An SVG of the problem (``at`` None) or of walkthrough stage ``at``; "" when the kind has no drawing."""
        return ""

    def worksheet(
        self, data: dict[str, Any], *, theme: Theme = "paper", highlight: int | None = None
    ) -> Worksheet | None:
        """The problem to work on the page, with blanks to fill in; None when the kind has none. ``theme``: "paper"
        (white, for the browser and printing) or "panel" (her pop-up panels' dark glass). ``highlight``: a
        walkthrough step (from 0) whose node or state is lit up, where an answer first went wrong."""
        return None

    def mark(self, data: dict[str, Any], filled: dict[str, str]) -> Marked:
        """A worksheet's blanks graded (``filled``: blank id -> what was written; a cut is "x" or "")."""
        raise NotImplementedError

    def describe(self, data: dict[str, Any]) -> str:
        """The drawing, in words."""
        return self.topic

    def title(self, problem: Problem) -> str:
        return f"Practice {problem.id}: {self.topic}"


_KINDS: dict[str, PracticeKind] = {}


def register(kind: PracticeKind) -> PracticeKind:
    """Add a kind of problem (a name already taken is replaced, so a subject can refine one)."""
    _KINDS[kind.name] = kind
    return kind


def kinds() -> dict[str, PracticeKind]:
    return dict(_KINDS)


def get_kind(name: str) -> PracticeKind | None:
    return _KINDS.get(name.strip().casefold())


# ---------------------------------------------------------------------------------------------------- keeping them


class ProblemBook:
    """The practice problems set lately, kept in a small JSON file. Thread-safe."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self.walked: dict[int, int] = {}  # problem id -> the walkthrough step shown last (from 0)
        self.opened: dict[int, float] = {}  # problem id -> when its worksheet was last opened
        self.checks: dict[int, list[tuple[float, bool, str]]] = {}  # problem id -> each check: when, right, verdict

    def _all(self) -> list[Problem]:
        raw = read_json(self.path, "the practice problems")
        if raw is None and self.path.exists():  # damaged: kept aside, so numbers are never reused for other problems
            try:
                self.path.replace(self.path.with_name(f"{self.path.name}.bad-{int(self.clock())}"))
            except OSError:
                raise OSError("the practice problems file can't be read") from None
        out = []
        for entry in raw.get("problems", []) if isinstance(raw, dict) else []:
            try:
                problem = Problem(**{k: v for k, v in entry.items() if k in Problem.__dataclass_fields__})
            except (TypeError, ValueError):
                continue
            if isinstance(problem.data, dict):
                out.append(problem)
        return out

    def add(self, make: Callable[[int], Problem]) -> Problem:
        """``make(id)`` builds the problem; it's numbered after the last one and saved."""
        with self._lock:
            problems = self._all()
            problem = make(max((p.id for p in problems), default=0) + 1)
            problems = (problems + [problem])[-KEEP:]
            write_json(self.path, {"problems": [asdict(p) for p in problems]})
            return problem

    def get(self, ident: int = 0) -> Problem | None:
        """Problem ``ident``, or the latest when it's 0."""
        with self._lock:
            problems = self._all()
        if not problems:
            return None
        if not ident:
            return problems[-1]
        return next((p for p in problems if p.id == ident), None)
