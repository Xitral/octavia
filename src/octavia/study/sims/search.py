"""Search simulations: A*'s heuristic weight (f = g + w × h, from uniform-cost search at 0 to nearly greedy), and
the same graph under each search algorithm. Both run on the course's rules (``study.algorithms``) and on the user's
own graph when the practice problem they're on is a search one.

A simulation's data is a search problem's: ``edges``, ``h``, ``start``, ``goal`` and maybe ``where``.
"""

from __future__ import annotations

import random
from typing import Any

from octavia.study import drawing
from octavia.study.algorithms import ALGORITHM_NAMES, Graph, SearchResult, num, random_graph, search
from octavia.study.sims.base import Frame, Param, Simulation, register

GRAPH_KINDS = ("bfs", "dfs", "ucs", "greedy", "astar")
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0)
TRIES = 40


def graph(data: dict[str, Any]) -> Graph:
    where = {k: (v[0], v[1]) for k, v in (data.get("where") or {}).items()}
    return Graph(data["edges"], data["h"], data["start"], data["goal"], where)


def _data(g: Graph) -> dict[str, Any]:
    return {
        "edges": g.edges,
        "h": g.h,
        "start": g.start,
        "goal": g.goal,
        "where": {s: [p[0], p[1]] for s, p in g.where.items()},
    }


def _picture(g: Graph, result: SearchResult, show_h: bool) -> str:
    return drawing.graph_svg(g, result.steps, None, show_h=show_h, path=result.path)


SHOWN_STATES = 8  # in a caption, so a big graph (the Romania map) still fits under the picture


def _states(states: list[str]) -> str:
    if len(states) <= SHOWN_STATES:
        return ", ".join(states)
    return ", ".join(states[:SHOWN_STATES]) + f" … ({len(states)} in all)"


def _route(path: list[str]) -> str:
    if len(path) <= SHOWN_STATES:
        return " → ".join(path)
    return " → ".join(path[:3]) + " → … → " + " → ".join(path[-2:])


def _found(result: SearchResult, cheapest: float) -> str:
    if not result.path:
        return "It never reaches the goal."
    best = "the cheapest there is" if result.cost <= cheapest else f"the cheapest is {num(cheapest)}"
    return f"It finds {_route(result.path)}, cost {num(result.cost)} ({best})."


def _facts(result: SearchResult, cheapest: SearchResult) -> list[tuple[str, str]]:
    return [
        ("Expanded", str(len(result.steps))),
        ("Path cost", num(result.cost) if result.path else "none"),
        ("Cheapest", num(cheapest.cost) if cheapest.path else "none"),
    ]


def _from_search_problem(kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if kind not in GRAPH_KINDS or not isinstance(data.get("edges"), dict):
        return None
    return {k: data[k] for k in ("edges", "h", "start", "goal", "where") if k in data}


class AStarWeight(Simulation):
    name = "astar_weight"
    topic = "A* heuristic weight"
    subject = "AI: search"
    about = (
        "a slider for the weight w in f = g + w×h: 0 is uniform-cost search, 1 the course's A*, higher leans "
        "towards greedy; watch how many states get expanded and whether the path is still the cheapest"
    )
    problem_kinds = ("greedy", "astar")  # a heuristic is needed; the others' problems have none

    def make(self, rng: random.Random) -> dict[str, Any]:
        """A graph where the weight visibly matters: the most different expansion orders, ideally with a heavy
        weight missing the cheapest path."""
        best, score = None, -1
        for _ in range(TRIES):
            g = random_graph(rng, "astar", size=rng.choice((7, 8)))
            cheapest = search(g, "ucs").cost
            runs = [search(g, "astar", weight=w) for w in WEIGHTS]
            orders = {tuple(r.order) for r in runs}
            worse = any(r.path and r.cost > cheapest for r in runs)
            points = len(orders) + (3 if worse else 0)
            if points > score:
                best, score = g, points
        assert best is not None
        return _data(best)

    def from_problem(self, kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
        found = _from_search_problem(kind, data) if kind in self.problem_kinds else None
        if found is None or not any(found["h"].values()):
            return None
        return found

    def params(self, data: dict[str, Any]) -> list[Param]:
        return [Param("Weight w", tuple(num(w) for w in WEIGHTS), WEIGHTS, start=WEIGHTS.index(1.0))]

    def frame(self, data: dict[str, Any], settings: tuple[Any, ...]) -> Frame:
        (w,) = settings
        g = graph(data)
        result = search(g, "astar", weight=w)
        ucs = search(g, "ucs")
        cheapest = ucs.cost
        if w == 0:
            what = "w = 0 ignores h: it's uniform-cost search."
        elif w == 1:
            what = "w = 1 is the course's A*."
        elif w < 1:
            what = f"w = {num(w)} trusts h less than A* does: closer to uniform-cost search."
        else:
            what = f"w = {num(w)} trusts h more than A* does: closer to greedy, and no longer sure to be cheapest."
        caption = f"f = g + {num(w)}×h. {what} Expanded {_states(result.order)}. {_found(result, cheapest)}"
        return Frame(_picture(g, result, True), caption, _facts(result, ucs))

    def lesson(self, data: dict[str, Any]) -> str:
        g = graph(data)
        ucs = search(g, "ucs")
        if not ucs.path:
            return f"{g.goal} can't be reached from {g.start}, so no weight finds a path."
        runs = {w: search(g, "astar", weight=w) for w in WEIGHTS}
        worse = [w for w, r in runs.items() if r.cost > ucs.cost]
        if not worse:
            counts = [len(runs[w].steps) for w in WEIGHTS]
            fewer = counts[-1] < counts[0] and all(b <= a for a, b in zip(counts, counts[1:], strict=False))
            return "Here every weight still finds the cheapest path" + (
                ", and heavier weights expand fewer states to get there." if fewer else "."
            )
        which = ", ".join(num(w) for w in worse)
        said = f"w = {which} {'misses' if len(worse) == 1 else 'miss'} the cheapest path ({num(ucs.cost)})."
        if min(worse) <= 1:
            return (
                said + " Even at w = 1 or below, so h overestimates somewhere (it isn't admissible), and A* isn't "
                "sure to find the cheapest."
            )
        return (
            said + " Above 1 the heuristic counts for more than the true cost, so the guarantee an admissible h "
            "gives A* is gone (which weights go wrong depends on the graph)."
        )


class SearchAlgorithms(Simulation):
    name = "search_algorithms"
    topic = "Search algorithms side by side"
    subject = "AI: search"
    about = (
        "one graph, a slider through BFS, DFS, uniform-cost, greedy and A*; watch which states each expands and "
        "which path each finds"
    )
    problem_kinds = GRAPH_KINDS

    ORDER = ("bfs", "dfs", "ucs", "greedy", "astar")
    SHORT = {"bfs": "BFS", "dfs": "DFS", "ucs": "UCS", "greedy": "Greedy", "astar": "A*"}

    def make(self, rng: random.Random) -> dict[str, Any]:
        best, score = None, -1
        for _ in range(TRIES):
            g = random_graph(rng, "astar", size=rng.choice((7, 8)))
            runs = [search(g, a) for a in self.ORDER]
            points = len({tuple(r.order) for r in runs}) + len({tuple(r.path) for r in runs})
            if points > score:
                best, score = g, points
        assert best is not None
        return _data(best)

    def from_problem(self, kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
        return _from_search_problem(kind, data)

    def _algorithms(self, data: dict[str, Any]) -> tuple[str, ...]:
        informed = any(data["h"].values())
        return tuple(a for a in self.ORDER if informed or a not in ("greedy", "astar"))

    def params(self, data: dict[str, Any]) -> list[Param]:
        algorithms = self._algorithms(data)
        return [Param("Algorithm", tuple(self.SHORT[a] for a in algorithms), algorithms, start=0)]

    def frame(self, data: dict[str, Any], settings: tuple[Any, ...]) -> Frame:
        (algorithm,) = settings
        g = graph(data)
        result = search(g, algorithm)
        ucs = search(g, "ucs")
        caption = (
            f"{ALGORITHM_NAMES[algorithm].capitalize()}: expanded {_states(result.order)}. {_found(result, ucs.cost)}"
        )
        return Frame(_picture(g, result, algorithm in ("greedy", "astar")), caption, _facts(result, ucs))

    def lesson(self, data: dict[str, Any]) -> str:
        if len(self._algorithms(data)) < len(self.ORDER):
            return "This graph has no heuristic, so greedy and A* are left out."
        return ""


register(AStarWeight())
register(SearchAlgorithms())
