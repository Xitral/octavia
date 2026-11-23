"""Practice problems graded by code, for any subject: each kind of problem is a ``PracticeKind`` (``base``) that a
subject module registers. The built-in subjects are AI search (``graph_search``: BFS, DFS, UCS, greedy, A*) and game
trees (``game_trees``: minimax, alpha-beta). A new one (logic, CSPs, probability...) is a module like those, added to
``SUBJECTS``; the tools and the problem book stay as they are.
"""

from __future__ import annotations

import importlib

from octavia.study.practice.base import (
    KEEP,
    Blank,
    Grade,
    Marked,
    NoGiven,
    Parts,
    PracticeKind,
    Problem,
    ProblemBook,
    Size,
    Step,
    Theme,
    Worksheet,
    get_kind,
    kinds,
    register,
)

SUBJECTS = ("octavia.study.practice.graph_search", "octavia.study.practice.game_trees")

for _module in SUBJECTS:
    importlib.import_module(_module)

__all__ = [
    "KEEP",
    "SUBJECTS",
    "Blank",
    "Grade",
    "Marked",
    "NoGiven",
    "Parts",
    "PracticeKind",
    "Problem",
    "ProblemBook",
    "Size",
    "Step",
    "Theme",
    "Worksheet",
    "get_kind",
    "kinds",
    "register",
]
