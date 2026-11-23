"""Simulations with sliders for any subject: each is a ``Simulation`` (``base``) that a subject module registers.
The built-in ones are search (``search``: A*'s heuristic weight, the search algorithms side by side) and game trees
(``game_trees``: alpha-beta's move order and tree size). A new one is a module like those, added to ``SUBJECTS``;
the ``simulate`` tool stays as it is.
"""

from __future__ import annotations

import importlib

from octavia.study.sims.base import Built, Frame, Param, Simulation, build, get_simulation, register, simulations

SUBJECTS = ("octavia.study.sims.search", "octavia.study.sims.game_trees")

for _module in SUBJECTS:
    importlib.import_module(_module)

__all__ = [
    "SUBJECTS",
    "Built",
    "Frame",
    "Param",
    "Simulation",
    "build",
    "get_simulation",
    "register",
    "simulations",
]
