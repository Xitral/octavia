"""Exact answers for the search and game-tree problems of an intro AI course, worked out by code so a practice problem
is graded exactly and a walkthrough never gets a step wrong.

The conventions are the user's course's (ITCS 6150, from its activity solutions), which follow the textbook (AIMA):

* Graph search: successors are generated in alphabetical order, the goal test happens when a node is taken off the
  frontier (expanded), and ties go alphabetically. BFS and DFS never generate a state already expanded or already on
  the frontier ("not generating duplicate states"); DFS puts a node's children at the front of the frontier, in
  order. UCS, greedy best-first and A* keep the best path cost found to each state and put a state back on the
  frontier when a cheaper path to it turns up (which only happens with an inconsistent heuristic).
* Game trees: MAX at the root unless said otherwise, levels alternating, children left to right. Alpha-beta prunes
  a node's remaining children as soon as alpha >= beta at it; every node's final alpha and beta are kept, as the
  course's solutions show them.

Everything is plain data, so it can be stored, compared and drawn.
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

INF = math.inf

# ---------------------------------------------------------------------------------------------------- game trees


@dataclass
class GameNode:
    """A node of a game tree: a leaf (``value`` set, no children) or an inner node. ``id``: A, B, C... in
    breadth-first order; leaves are also numbered 1, 2, 3... left to right (``leaf``)."""

    id: str
    children: list[GameNode] = field(default_factory=list)
    value: float | None = None
    leaf: int = 0  # its number, left to right, for a leaf

    @property
    def is_leaf(self) -> bool:
        return not self.children


def _letters() -> itertools.chain[str]:
    singles = (chr(c) for c in range(ord("A"), ord("Z") + 1))
    doubles = (a + b for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return itertools.chain(singles, doubles)


def build_tree(spec: Any) -> GameNode:
    """A tree from nested lists: ``[[3, 12, 8], [2, 4, 6], [14, 5, 2]]`` is a root with three children, each with
    three leaves. Inner nodes are lettered breadth-first and leaves numbered left to right. Raises ValueError."""

    def make(part: Any, depth: int) -> GameNode:
        if depth > 8:
            raise ValueError("the tree is too deep (at most 8 levels)")
        if isinstance(part, bool | str) or part is None:
            raise ValueError("a leaf must be a number")
        if isinstance(part, int | float):
            if not math.isfinite(part):
                raise ValueError("a leaf must be a finite number")
            return GameNode(id="", value=float(part))
        if not isinstance(part, list) or not part:
            raise ValueError("every inner node needs a list of at least one child")
        return GameNode(id="", children=[make(child, depth + 1) for child in part])

    root = make(spec, 0)
    if root.is_leaf:
        raise ValueError("the tree needs at least one move: a list of children at the top")
    names, queue, leaves = _letters(), [root], 0
    while queue:  # inner nodes lettered level by level; leaves are numbered instead
        node = queue.pop(0)
        if not node.is_leaf:
            node.id = next(names)
        queue.extend(node.children)
    for node in walk(root):
        if node.is_leaf:
            leaves += 1
            node.leaf = leaves
            node.id = f"#{leaves}"
    if leaves > 81:
        raise ValueError("the tree has too many leaves (at most 81)")
    return root


def walk(root: GameNode) -> list[GameNode]:
    """Every node, depth first, left to right."""
    out = [root]
    for child in root.children:
        out += walk(child)
    return out


def tree_spec(root: GameNode) -> Any:
    """The nested lists ``build_tree`` takes, back from a tree."""
    if root.is_leaf:
        value = root.value or 0.0
        return int(value) if value.is_integer() else value
    return [tree_spec(child) for child in root.children]


def leaves_under(node: GameNode) -> list[int]:
    return [n.leaf for n in walk(node) if n.is_leaf]


def minimax(root: GameNode, max_first: bool = True) -> dict[str, float]:
    """Every node's minimax value, by id."""
    values: dict[str, float] = {}

    def value(node: GameNode, is_max: bool) -> float:
        if node.is_leaf:
            result = node.value or 0.0
        else:
            kids = [value(child, not is_max) for child in node.children]
            result = max(kids) if is_max else min(kids)
        values[node.id] = result
        return result

    value(root, max_first)
    return values


@dataclass(frozen=True)
class ABEvent:
    """One step of alpha-beta: ``kind`` is "enter" (a node is visited with these bounds), "leaf" (a leaf's value is
    read), "back" (a child's value comes back up and the node's value and bound may change) or "prune" (the node's
    remaining children are skipped because alpha >= beta)."""

    kind: Literal["enter", "leaf", "back", "prune"]
    node: str
    alpha: float
    beta: float
    value: float | None = None  # the node's value so far (its best for MAX, worst for MIN)
    child: str = ""  # "back": the child whose value came back
    pruned: tuple[str, ...] = ()  # "prune": the children skipped


@dataclass
class ABResult:
    value: float
    events: list[ABEvent]
    final: dict[str, tuple[float, float, float | None]]  # node id -> (alpha, beta, value) when it's done
    pruned_nodes: list[str]  # the roots of skipped subtrees
    pruned_leaves: list[int]  # every leaf never looked at
    visited_leaves: list[int]  # in the order they were read


def alphabeta(root: GameNode, max_first: bool = True) -> ABResult:
    by_id = {n.id: n for n in walk(root)}
    events: list[ABEvent] = []
    final: dict[str, tuple[float, float, float | None]] = {}
    pruned: list[str] = []
    visited: list[int] = []

    def search(node: GameNode, alpha: float, beta: float, is_max: bool) -> float:
        events.append(ABEvent("enter", node.id, alpha, beta))
        if node.is_leaf:
            value = node.value or 0.0
            visited.append(node.leaf)
            events.append(ABEvent("leaf", node.id, alpha, beta, value))
            final[node.id] = (alpha, beta, value)
            return value
        best = -INF if is_max else INF
        for n, child in enumerate(node.children):
            got = search(child, alpha, beta, not is_max)
            if is_max:
                best = max(best, got)
                alpha = max(alpha, best)
            else:
                best = min(best, got)
                beta = min(beta, best)
            events.append(ABEvent("back", node.id, alpha, beta, best, child=child.id))
            rest = node.children[n + 1 :]
            if alpha >= beta and rest:
                skipped = tuple(c.id for c in rest)
                pruned.extend(skipped)
                events.append(ABEvent("prune", node.id, alpha, beta, best, pruned=skipped))
                break
        final[node.id] = (alpha, beta, best)
        return best

    value = search(root, -INF, INF, max_first)
    pruned_leaves = sorted(leaf for pid in pruned for leaf in leaves_under(by_id[pid]))
    return ABResult(value, events, final, pruned, pruned_leaves, visited)


def random_tree(rng: random.Random, depth: int = 3, branching: Sequence[int] = (2, 3), low: int = -20, high: int = 20):
    """A random game tree of ``depth`` levels below the root, each inner node with a branching from ``branching``,
    leaves from ``low`` to ``high``; one that alpha-beta prunes something, so it's worth practising on."""
    for _ in range(50):

        def make(level: int) -> Any:
            if level == depth:
                return rng.randint(low, high)
            return [make(level + 1) for _ in range(rng.choice(list(branching)))]

        spec = make(0)
        root = build_tree(spec)
        if alphabeta(root).pruned_leaves:
            return root
    return root


# ---------------------------------------------------------------------------------------------------- graphs

Algorithm = Literal["bfs", "dfs", "ucs", "greedy", "astar"]
ALGORITHM_NAMES = {
    "bfs": "breadth-first search",
    "dfs": "depth-first search",
    "ucs": "uniform-cost search",
    "greedy": "greedy best-first search",
    "astar": "A* search",
}


@dataclass
class Graph:
    """An undirected graph with step costs, a heuristic per state (0 when unknown), a start and a goal. ``where``:
    a drawing position per state, (column, row), when it was generated or given."""

    edges: dict[str, dict[str, float]]
    h: dict[str, float]
    start: str
    goal: str
    where: dict[str, tuple[float, float]] = field(default_factory=dict)

    def neighbours(self, state: str) -> list[tuple[str, float]]:
        return sorted(self.edges.get(state, {}).items())

    @property
    def states(self) -> list[str]:
        return sorted(self.edges)


def _num(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{text} isn't a number") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{text} isn't a cost or heuristic value that can be used (it must be 0 or more)")
    return value


_NAME = r"([A-Za-z][\w ]{0,23}?)"  # "S", "Arad", "Rimnicu Vilcea"
_EDGE = re.compile(rf"^\s*{_NAME}\s*(?:[-–>]+|\bto\b)\s*{_NAME}\s*(?:[:=]\s*([\d.]+))?\s*$")
_H = re.compile(rf"^\s*{_NAME}\s*[:=]\s*([\d.]+)\s*$")


def _state(name: str) -> str:
    return " ".join(name.split()).upper()


def parse_graph(edges: str, start: str, goal: str, heuristics: str = "") -> Graph:
    """A graph from text: edges ``"S-A:6, S-B:5, A-E:6"`` (a missing cost is 1) and heuristics ``"S=17, A=10"``.
    Raises ValueError with what's wrong."""
    graph: dict[str, dict[str, float]] = {}
    for part in filter(str.strip, re.split(r"[,;\n]", edges)):
        found = _EDGE.match(part)
        if found is None:
            raise ValueError(f"couldn't read the edge {part.strip()!r}: write it like S-A:6")
        a, b = _state(found.group(1)), _state(found.group(2))
        if a == b:
            raise ValueError(f"the edge {part.strip()!r} goes from a state to itself")
        cost = _num(found.group(3)) if found.group(3) else 1.0
        graph.setdefault(a, {})[b] = cost
        graph.setdefault(b, {})[a] = cost
    if not graph:
        raise ValueError("no edges were given")
    if len(graph) > 26:
        raise ValueError("the graph has too many states (at most 26)")
    h: dict[str, float] = {}
    for part in filter(str.strip, re.split(r"[,;\n]", heuristics)):
        found = _H.match(part)
        if found is None:
            raise ValueError(f"couldn't read the heuristic {part.strip()!r}: write it like A=10")
        h[_state(found.group(1))] = _num(found.group(2))
    start, goal = _state(start), _state(goal)
    for name, state in (("start", start), ("goal", goal)):
        if state not in graph:
            raise ValueError(f"the {name} state {state!r} isn't in the graph")
    unknown = sorted(set(h) - set(graph))
    if unknown:
        raise ValueError(f"heuristics given for states not in the graph: {', '.join(unknown)}")
    return Graph(graph, {s: h.get(s, 0.0) for s in graph}, start, goal)


@dataclass(frozen=True)
class Entry:
    """A frontier entry: a state, the path cost to it and its priority (f, h or g, by algorithm; for BFS and DFS
    its position)."""

    state: str
    g: float
    f: float
    parent: str = ""


@dataclass(frozen=True)
class SearchStep:
    """One expansion: the state taken off the frontier, its path cost, and the frontier left after its children
    were added (in the order the algorithm takes them)."""

    expanded: str
    g: float
    f: float
    frontier: tuple[Entry, ...]
    added: tuple[str, ...] = ()  # children put on the frontier at this step
    skipped: tuple[str, ...] = ()  # children not added (already expanded or on the frontier, or no cheaper path)
    goal: bool = False


@dataclass
class SearchResult:
    algorithm: Algorithm
    steps: list[SearchStep]
    path: list[str]
    cost: float

    @property
    def order(self) -> list[str]:
        return [s.expanded for s in self.steps]


def _priority(algorithm: Algorithm, g: float, h: float, weight: float = 1.0) -> float:
    return {"ucs": g, "greedy": h, "astar": round(g + weight * h, 9)}.get(algorithm, 0.0)


def search(graph: Graph, algorithm: Algorithm, max_steps: int = 200, weight: float = 1.0) -> SearchResult:
    """Run ``algorithm`` from the start until the goal is expanded (or the frontier runs out). ``weight`` turns A*
    into weighted A* (f = g + weight × h: 0 is uniform-cost search, above 1 leans towards greedy), for the
    simulations; the course's A* is weight 1."""
    parents: dict[str, str] = {}
    steps: list[SearchStep] = []
    start = graph.start
    if algorithm in ("bfs", "dfs"):
        frontier: list[Entry] = [Entry(start, 0.0, 0.0)]
        expanded: set[str] = set()
        while frontier and len(steps) < max_steps:
            node = frontier.pop(0)
            expanded.add(node.state)
            if node.parent:
                parents[node.state] = node.parent
            if node.state == graph.goal:
                steps.append(SearchStep(node.state, node.g, node.f, tuple(frontier), goal=True))
                return SearchResult(algorithm, steps, _path(parents, graph.goal), node.g)
            on_frontier = {e.state for e in frontier}
            added, skipped, kids = [], [], []
            for child, cost in graph.neighbours(node.state):
                if child in expanded or child in on_frontier:
                    skipped.append(child)
                    continue
                kids.append(Entry(child, round(node.g + cost, 9), 0.0, node.state))
                added.append(child)
            frontier = kids + frontier if algorithm == "dfs" else frontier + kids
            steps.append(SearchStep(node.state, node.g, node.f, tuple(frontier), tuple(added), tuple(skipped)))
        return SearchResult(algorithm, steps, [], INF)

    best: dict[str, float] = {start: 0.0}
    heap: list[tuple[float, str, int, Entry]] = []
    counter = itertools.count()
    first = Entry(start, 0.0, _priority(algorithm, 0.0, graph.h[start], weight))
    heapq.heappush(heap, (first.f, first.state, next(counter), first))
    while heap and len(steps) < max_steps:
        _, _, _, node = heapq.heappop(heap)
        if node.g > best.get(node.state, INF):
            continue  # a stale entry: a cheaper path to it was found after this one was added
        if node.parent:
            parents[node.state] = node.parent
        if node.state == graph.goal:
            steps.append(SearchStep(node.state, node.g, node.f, _snapshot(heap, best), goal=True))
            return SearchResult(algorithm, steps, _path(parents, graph.goal), node.g)
        added, skipped = [], []
        for child, cost in graph.neighbours(node.state):
            g = round(node.g + cost, 9)  # 1.1 + 2.2 is 3.3, as on paper, not 3.3000000000000003
            # Greedy never comes back to a state it has reached (only path cost could, and greedy ignores it).
            if g >= best.get(child, INF) or (algorithm == "greedy" and child in best):
                skipped.append(child)
                continue
            best[child] = g
            entry = Entry(child, g, _priority(algorithm, g, graph.h[child], weight), node.state)
            heapq.heappush(heap, (entry.f, entry.state, next(counter), entry))
            added.append(child)
        steps.append(SearchStep(node.state, node.g, node.f, _snapshot(heap, best), tuple(added), tuple(skipped)))
    return SearchResult(algorithm, steps, [], INF)


def _snapshot(heap: list[tuple[float, str, int, Entry]], best: dict[str, float]) -> tuple[Entry, ...]:
    """The live frontier, in the order it would be taken (stale entries left out)."""
    live = [item for item in heap if item[3].g <= best.get(item[3].state, INF)]
    return tuple(item[3] for item in sorted(live))


def _path(parents: dict[str, str], goal: str) -> list[str]:
    path, state, seen = [goal], goal, {goal}
    while state in parents:
        state = parents[state]
        if state in seen:
            break
        seen.add(state)
        path.append(state)
    return path[::-1]


def path_cost(graph: Graph, path: Sequence[str]) -> float | None:
    """The cost of ``path``, or None when two of its states aren't joined by an edge."""
    total = 0.0
    for a, b in itertools.pairwise(path):
        cost = graph.edges.get(a, {}).get(b)
        if cost is None:
            return None
        total += cost
    return total


def random_graph(rng: random.Random, algorithm: Algorithm, size: int = 7) -> Graph:
    """A small graph laid out in columns from S (left) to G (right), with whole-number costs and, for greedy and A*,
    an admissible heuristic (never more than the true cost to G) that isn't always the exact cost; one where the
    algorithm expands at least four states, so it's worth practising on."""
    for _ in range(100):
        letters = [chr(ord("A") + n) for n in range(size - 2)]
        columns: list[list[str]] = [["S"]]
        pool = letters[:]
        while pool:
            take = min(len(pool), rng.choice((2, 2, 3)))
            columns.append(pool[:take])
            pool = pool[take:]
        columns.append(["G"])
        edges: dict[str, dict[str, float]] = {}

        def join(a: str, b: str, edges: dict[str, dict[str, float]] = edges) -> None:
            cost = float(rng.randint(1, 9))
            edges.setdefault(a, {})[b] = cost
            edges.setdefault(b, {})[a] = cost

        for left, right in itertools.pairwise(columns):
            for b in right:  # everything is reachable from the column before
                join(rng.choice(left), b)
            for a in left:
                if a not in edges or not any(n in right for n in edges[a]):
                    join(a, rng.choice(right))
            for a in left:
                for b in right:
                    if b not in edges.get(a, {}) and rng.random() < 0.3:
                        join(a, b)
        for column in columns[1:-1]:  # a few links within a column
            for a, b in itertools.pairwise(column):
                if rng.random() < 0.25:
                    join(a, b)
        where = {s: (float(c), r - (len(col) - 1) / 2) for c, col in enumerate(columns) for r, s in enumerate(col)}
        true = _distances(edges, "G")
        h = {s: float(max(0, round(true[s] - rng.choice((0, 0, 1, 2, 3))))) for s in edges}
        h["G"] = 0.0
        graph = Graph(edges, h if algorithm in ("greedy", "astar") else {s: 0.0 for s in edges}, "S", "G", where)
        result = search(graph, algorithm)
        if result.path and len(result.steps) >= 4:
            return graph
    return graph


def _distances(edges: dict[str, dict[str, float]], goal: str) -> dict[str, float]:
    dist = {goal: 0.0}
    heap = [(0.0, goal)]
    while heap:
        d, state = heapq.heappop(heap)
        if d > dist.get(state, INF):
            continue
        for nxt, cost in edges.get(state, {}).items():
            if d + cost < dist.get(nxt, INF):
                dist[nxt] = d + cost
                heapq.heappush(heap, (d + cost, nxt))
    return {s: dist.get(s, INF) for s in edges}


def num(value: float | None) -> str:
    """A number as it's written on the board: 12, -3, 2.5, ∞, -∞."""
    if value is None:
        return "?"
    if value == INF:
        return "∞"
    if value == -INF:
        return "-∞"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
