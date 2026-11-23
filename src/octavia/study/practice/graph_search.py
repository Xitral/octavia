"""Graph-search practice: BFS, DFS, uniform-cost, greedy best-first and A*, on the course's conventions
(``study.algorithms``). One kind per algorithm, all sharing this code.

A problem's data: ``edges`` (state -> neighbour -> cost, both ways), ``h``, ``start``, ``goal`` and, for one that was
made up, ``where`` (drawing positions).
"""

from __future__ import annotations

import random
import re
from typing import Any

from pydantic import Field

from octavia.study import drawing
from octavia.study.algorithms import (
    ALGORITHM_NAMES,
    Algorithm,
    Graph,
    SearchResult,
    num,
    parse_graph,
    path_cost,
    random_graph,
    search,
)
from octavia.study.practice.base import (
    Blank,
    Grade,
    Marked,
    Parts,
    PracticeKind,
    Size,
    Step,
    Theme,
    Worksheet,
    register,
)


class GraphGiven(Parts):
    edges: str = Field(description="'S-A:6, S-B:5, A-E:6' (undirected; a missing cost is 1).")
    heuristics: str = Field(default="", description="'S=17, A=10' for greedy and A*.")
    start: str = "S"
    goal: str = "G"


class SearchAnswer(Parts):
    order: list[str] | str | None = Field(default=None, description="States in the order expanded.")
    path: list[str] | str | None = None


def _states(value: list[str] | str | None, known: set[str]) -> list[str] | None:
    """A list of states as the user said it: ["S", "A"], "S A G", "S, A, G", "S→A→G", or "SAG" when every state's
    name is one letter. Names of more than one word ("Rimnicu Vilcea") are kept whole when the graph has them."""
    if value is None:
        return None
    items = [value] if isinstance(value, str) else [str(v) for v in value]
    out: list[str] = []
    for item in items:
        whole = " ".join(item.split()).upper()
        if whole in known:
            out.append(whole)
            continue
        for part in re.split(r"[,;>→]+|\s+-+\s*|-+>|\s-|-\s|(?<=\w)-(?=\w)", item):
            part = " ".join(part.split()).upper()
            if not part:
                continue
            if part in known:
                out.append(part)
            elif all(len(k) == 1 for k in known) and all(ch in known for ch in part.replace(" ", "")):
                out.extend(part.replace(" ", ""))  # "SAG", or "S A G" said as one
            else:
                out.extend(part.split())
    return out


class GraphSearch(PracticeKind):
    subject = "AI: search"
    Given = GraphGiven
    Answer = SearchAnswer
    given_hint = '{"edges": "S-A:6, ...", "heuristics": "S=17, ...", "start": "S", "goal": "G"}'
    answer_hint = '{"order": ["S", "A", ...], "path": ["S", ..., "G"]}'

    def __init__(self, algorithm: Algorithm, topic: str) -> None:
        self.name = algorithm  # type: ignore[misc]
        self.topic = topic  # type: ignore[misc]
        self.algorithm = algorithm

    @property
    def informed(self) -> bool:
        return self.algorithm in ("greedy", "astar")

    def make(self, rng: random.Random, size: Size = "exam") -> dict[str, Any]:
        graph = random_graph(rng, self.algorithm, size=rng.choice((8, 9) if size == "big" else (6, 7)))
        return {
            "edges": graph.edges,
            "h": graph.h,
            "start": graph.start,
            "goal": graph.goal,
            "where": {s: [p[0], p[1]] for s, p in graph.where.items()},
        }

    def parse(self, given: Parts) -> dict[str, Any]:
        assert isinstance(given, GraphGiven)
        graph = parse_graph(given.edges, given.start, given.goal, given.heuristics)
        if self.informed:
            given_h = {s.split("=")[0].split(":")[0].strip().upper() for s in re.split(r"[,;\n]", given.heuristics)}
            missing = sorted(s for s in graph.states if s != graph.goal and " ".join(s.split()) not in given_h)
            if missing:
                raise ValueError(f"{self.topic} needs a heuristic value for every state; missing: {', '.join(missing)}")
        return {"edges": graph.edges, "h": graph.h, "start": graph.start, "goal": graph.goal}

    @staticmethod
    def graph(data: dict[str, Any]) -> Graph:
        where = {k: (v[0], v[1]) for k, v in (data.get("where") or {}).items()}
        return Graph(data["edges"], data["h"], data["start"], data["goal"], where)

    def solve(self, data: dict[str, Any]) -> SearchResult:
        return search(self.graph(data), self.algorithm)

    def ask(self, data: dict[str, Any]) -> str:
        return "the order states are expanded in, and the path found (and its cost)."

    def statement(self, data: dict[str, Any]) -> str:
        graph = self.graph(data)
        seen: set[frozenset[str]] = set()
        edges = []
        for a in sorted(graph.states, key=lambda s: (s != graph.start, s == graph.goal, s)):
            for b, cost in graph.neighbours(a):
                if frozenset((a, b)) not in seen:
                    seen.add(frozenset((a, b)))
                    edges.append(f"{a}-{b}:{num(cost)}")
        lines = [
            f"{ALGORITHM_NAMES[self.algorithm].capitalize()} from {graph.start} to {graph.goal}. Edges (undirected): "
            + ", ".join(edges)
            + "."
        ]
        if self.informed:
            lines.append("Heuristic h: " + ", ".join(f"{s}={num(v)}" for s, v in sorted(graph.h.items())) + ".")
        lines.append(
            "Course rules: successors in alphabetical order, ties broken alphabetically, the goal test when a node "
            "is taken off the frontier"
            + {
                "bfs": ", no duplicate states generated",
                "dfs": ", no duplicate states generated, children put at the front of the frontier",
                "greedy": ", a state already reached is never added again",
                "ucs": ", a state is added again when a cheaper path to it is found",
                "astar": ", a state is added again (reopened) when a cheaper path to it is found",
            }[self.algorithm]
            + "."
        )
        lines.append(f"Ask: {self.ask(data)}")
        return "\n".join(lines)

    def describe(self, data: dict[str, Any]) -> str:
        return f"A graph from {data['start']} to {data['goal']}"

    # ------------------------------------------------------------------------------------------------ worksheet

    def worksheet(self, data: dict[str, Any], *, theme: Theme = "paper", highlight: int | None = None) -> Worksheet:
        graph = self.graph(data)
        lay = drawing.graph_layout(graph, roomy=True)
        lay.height += 50  # room for the path under it
        focus = ""
        if highlight is not None:
            order = self.solve(data).order
            focus = order[highlight] if 0 <= highlight < len(order) else ""
        colours = drawing.PAPER if theme == "paper" else drawing.DARK
        svg = drawing.graph_svg(graph, show_h=self.informed, theme=colours, layout=lay, focus=focus)
        blanks = [  # beside a long name ("Rimnicu Vilcea", written under its circle), else under the circle
            Blank(f"n:{state}", x + 40, y, label="#", width=34)
            if len(state) > 3
            else Blank(f"n:{state}", x, y + 36, label="#", width=34)
            for state, (x, y) in sorted(lay.at.items())
        ]
        blanks.append(Blank("path", lay.width / 2, lay.height - 24, "text", label="Path found", width=260))
        return Worksheet(
            svg,
            lay.width,
            lay.height,
            blanks,
            "Number each state in the order it's expanded, starting from 1 (a state expanded twice gets both "
            "numbers, like 4, 7; one never expanded stays empty; the goal gets a number too, since it's expanded when "
            "it comes off the frontier), then write the path found. " + self.statement(data).split("\n")[-2],
        )

    def mark(self, data: dict[str, Any], filled: dict[str, str]) -> Marked:
        result = self.solve(data)
        numbers: dict[str, list[int]] = {}
        for n, state in enumerate(result.order, 1):
            numbers.setdefault(state, []).append(n)
        marks: dict[str, str] = {}
        expected: dict[str, str] = {}
        wrong_at: list[int] = []
        for state in self.graph(data).states:
            key = f"n:{state}"
            written = str(filled.get(key, "")).strip()
            want = numbers.get(state, [])
            got = sorted(int(float(x)) for x in re.findall(r"\d+(?:\.0+)?", written))
            if not want and not got:
                continue
            if got == want:
                marks[key] = "right"
                continue
            marks[key] = "missed" if not got else ("extra" if not want else "wrong")
            expected[key] = ", ".join(map(str, want))
            wrong_at += [n for n in want if n not in got] + [n for n in got if n not in want]
        goal = f"n:{data['goal']}"
        if marks.get(goal) == "missed" and len(marks) > 1 and all(m == "right" for k, m in marks.items() if k != goal):
            marks[goal] = "right"  # stopping at the goal without numbering it is how some write it: fine
            expected.pop(goal, None)
            wrong_at = [n for n in wrong_at if n != len(result.order)]
        written_path = str(filled.get("path", "")).strip()
        if not result.path:
            said = _states(written_path, set(self.graph(data).states)) or []
            if not said or said in (["NO", "PATH"], ["NONE"], ["NOTHING"]):
                marks["path"] = "right"
            else:
                marks["path"], expected["path"] = "wrong", "no path"
        elif written_path or result.path:
            path = _states(written_path, set(self.graph(data).states)) or []
            if path == result.path:
                marks["path"] = "right"
            else:
                marks["path"] = "missed" if not path else "wrong"
                expected["path"] = " → ".join(result.path) or "no path"
        right = sum(1 for m in marks.values() if m == "right")
        bad = len(marks) - right
        lines = [f"{right} of {len(marks)} right." if bad else f"All {right} right."]
        first = None
        if wrong_at:
            first = max(0, min(wrong_at) - 1)
            first = min(first, len(result.steps) - 1)
            lines.append(f"Where it first goes wrong: {self._say(result, first)}")
        elif marks.get("path") not in (None, "right"):
            lines.append(
                f"The order is right, but the path is {expected['path']}: follow each state back to its parent."
            )
        return Marked(Grade(bad == 0, lines, first), marks, expected)

    def _say(self, result: SearchResult, k: int) -> str:
        step = result.steps[k]
        before = result.steps[k - 1].frontier if k else ()
        measure = {"ucs": "g", "greedy": "h", "astar": "f = g + h"}.get(self.algorithm, "")
        if k == 0:
            why = "It starts at the start state."
        elif self.algorithm == "bfs":
            why = "It's the oldest state on the frontier (first in, first out)."
        elif self.algorithm == "dfs":
            why = "It's at the front of the frontier (the newest children go first)."
        else:
            tied = [e.state for e in before if e.f == step.f]
            why = f"It has the lowest {measure} on the frontier ({num(step.f)})" + (
                f", tied with {', '.join(s for s in tied if s != step.expanded)}, and comes first alphabetically."
                if len(tied) > 1
                else "."
            )
        cost = f" (g = {num(step.g)})" if self.algorithm != "bfs" else ""
        head = f"Step {k + 1}: expand {step.expanded}{cost}."
        if step.goal:
            return f"{head} {why} It's the goal, so the search stops here."
        parts = [head, why]
        if step.added:
            parts.append("Add " + ", ".join(step.added) + ".")
        if step.skipped:
            reason = (
                "already expanded or on the frontier" if self.algorithm in ("bfs", "dfs") else "no cheaper path to them"
            )
            parts.append("Skip " + ", ".join(step.skipped) + f" ({reason}).")
        if self.algorithm in ("bfs", "dfs"):
            frontier = ", ".join(e.state for e in step.frontier)
        else:
            frontier = ", ".join(f"{e.state} ({num(e.f)})" for e in step.frontier)
        parts.append(f"Frontier: {frontier or 'empty'}.")
        return " ".join(parts)

    def steps(self, data: dict[str, Any]) -> list[Step]:
        result = self.solve(data)
        return [Step(self._say(result, k), k + 1) for k in range(len(result.steps))]

    def grade(self, data: dict[str, Any], answer: Parts) -> Grade:
        assert isinstance(answer, SearchAnswer)
        result = self.solve(data)
        lines: list[str] = []
        right = True
        first: int | None = None
        known = set(self.graph(data).states)
        order = _states(answer.order, known)
        if order is not None and order == result.order[:-1] and result.path:
            lines.append(
                "Expansion order " + ", ".join(result.order) + ": right (the goal counts too: it's expanded, and the "
                "search stops when it comes off the frontier)."
            )
            order = None
        if order is not None:
            ours = result.order
            k = next((n for n, (a, b) in enumerate(zip(order, ours, strict=False)) if a != b), None)
            if k is None and len(order) == len(ours):
                lines.append("Expansion order " + ", ".join(ours) + ": right.")
            else:
                right = False
                if k is not None:
                    lines.append(
                        f"The first {k} expansions are right; step {k + 1} is {ours[k]}, not {order[k]}. "
                        + self._say(result, k)
                    )
                elif len(order) < len(ours):
                    k = len(order)
                    lines.append(
                        f"Order right as far as it goes, but it stops after {len(order)} expansions; the search goes "
                        f"on: {', '.join(ours[len(order) :])}."
                    )
                else:
                    k = len(ours) - 1
                    lines.append(
                        f"Order right up to {ours[-1]}, which is the goal: the search stops when it's expanded."
                    )
                first = max(0, min(k, len(ours) - 1))
        path = _states(answer.path, known)
        if path is not None and not result.path:
            if not path or path in (["NO", "PATH"], ["NONE"], ["NOTHING"]):
                lines.append(f"No path: right, {data['goal']} can't be reached.")
            else:
                right = False
                lines.append(f"There's no path: {data['goal']} can't be reached from {data['start']}.")
            path = None
        if path is not None:
            if path == result.path:
                lines.append(f"Path {' → '.join(path)}, cost {num(result.cost)}: right.")
            else:
                right = False
                cost = path_cost(self.graph(data), path)
                note = "" if cost is not None else " (two of its states aren't joined by an edge)"
                found = " → ".join(result.path) or "no path"
                lines.append(
                    f"Path: {ALGORITHM_NAMES[self.algorithm]} finds {found} (cost {num(result.cost)}), "
                    f"not {' → '.join(path) or 'nothing'}{note}."
                )
        if not lines:
            return Grade(False, ["Nothing to check: give the expansion order and the path."])
        return Grade(right, lines, first)

    def summary(self, data: dict[str, Any]) -> str:
        result = self.solve(data)
        if not result.path:
            return "The goal can't be reached."
        return f"Expanded: {', '.join(result.order)}. Path {' → '.join(result.path)}, cost {num(result.cost)}."

    def picture(self, data: dict[str, Any], at: int | None = None) -> str:
        graph = self.graph(data)
        if at is None:
            return drawing.graph_svg(graph, show_h=self.informed)
        result = self.solve(data)
        path = result.path if at >= len(result.steps) else ()
        return drawing.graph_svg(graph, result.steps, at, show_h=self.informed, path=path)


for _algorithm, _topic in (
    ("bfs", "Breadth-first search"),
    ("dfs", "Depth-first search"),
    ("ucs", "Uniform-cost search"),
    ("greedy", "Greedy best-first search"),
    ("astar", "A* search"),
):
    register(GraphSearch(_algorithm, _topic))  # type: ignore[arg-type]
