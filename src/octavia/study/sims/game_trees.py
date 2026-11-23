"""Game-tree simulation: how much alpha-beta prunes as the move order and the tree's size change. The root's value
never changes (pruning only skips what can't matter); how many leaves it has to look at does, from all of them with
the worst moves first down to about b^(d/2) with the best first.

A simulation's data: ``trees`` (one per tree size, nested lists of leaf values, as ``build_tree`` takes them) with
``sizes`` (their labels) and ``max_first``.
"""

from __future__ import annotations

import random
from typing import Any

from octavia.study import drawing
from octavia.study.algorithms import GameNode, alphabeta, build_tree, minimax, num, tree_spec, walk
from octavia.study.sims.base import Frame, Param, Simulation, register

# (depth below the root, moves at every node): small enough to read on a panel, at most 32 leaves.
SHAPES = ((2, 2), (2, 3), (3, 2), (3, 3), (4, 2), (5, 2))
ORDERS = ("as drawn", "best first", "worst first")


def _uniform(rng: random.Random, depth: int, branching: int) -> Any:
    if depth == 0:
        return rng.randint(-9, 9)
    return [_uniform(rng, depth - 1, branching) for _ in range(branching)]


def reorder(spec: Any, max_first: bool, order: str) -> Any:
    """The tree with each node's moves sorted: "best first" puts the move best for whoever moves there first (the
    highest value at MAX, the lowest at MIN), "worst first" the opposite; ties keep their order. "as drawn" leaves
    it as it is."""
    if order == "as drawn" or not isinstance(spec, list):
        return spec

    def value(part: Any, is_max: bool) -> float:
        root = build_tree(part) if isinstance(part, list) else None
        return float(part) if root is None else minimax(root, is_max)[root.id]

    def go(part: Any, is_max: bool) -> Any:
        if not isinstance(part, list):
            return part
        kids = [go(child, not is_max) for child in part]
        best_first = sorted(kids, key=lambda k: value(k, not is_max), reverse=is_max)
        return best_first if order == "best first" else best_first[::-1]

    return go(spec, max_first)


def _uniform_shape(root: GameNode) -> tuple[int, int] | None:
    """(depth, branching) when every inner node has the same number of moves and every leaf is as deep."""
    widths, depths = set(), set()

    def go(node: GameNode, d: int) -> None:
        if node.is_leaf:
            depths.add(d)
            return
        widths.add(len(node.children))
        for child in node.children:
            go(child, d + 1)

    go(root, 0)
    return (depths.pop(), widths.pop()) if len(widths) == 1 and len(depths) == 1 else None


class AlphaBetaOrder(Simulation):
    name = "alphabeta_order"
    topic = "Alpha-beta move order"
    subject = "AI: game trees"
    about = (
        "sliders for the tree's size and the move order (as drawn, best first, worst first); watch the pruned "
        "branches and the count of leaves alpha-beta looks at change while the root's value stays the same"
    )
    problem_kinds = ("minimax", "alphabeta")

    def make(self, rng: random.Random) -> dict[str, Any]:
        trees = [_uniform(rng, d, b) for d, b in SHAPES]
        return {"trees": trees, "sizes": [f"{d} deep, {b} moves" for d, b in SHAPES], "max_first": True}

    def from_problem(self, kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
        if kind not in self.problem_kinds or not isinstance(data.get("tree"), list):
            return None
        try:
            spec = tree_spec(build_tree(data["tree"]))
        except ValueError:
            return None
        return {"trees": [spec], "sizes": ["your tree"], "max_first": bool(data.get("max_first", True))}

    def params(self, data: dict[str, Any]) -> list[Param]:
        sliders = [Param("Move order", ORDERS, ORDERS, start=0)]
        if len(data["trees"]) > 1:
            sizes = tuple(data["sizes"])
            sliders.insert(0, Param("Tree", sizes, tuple(range(len(sizes))), start=min(2, len(sizes) - 1)))
        return sliders

    def frame(self, data: dict[str, Any], settings: tuple[Any, ...]) -> Frame:
        which, order = (0, settings[0]) if len(settings) == 1 else settings
        max_first = data.get("max_first", True)
        root = build_tree(reorder(data["trees"][which], max_first, order))
        result = alphabeta(root, max_first)
        leaves = sum(1 for n in walk(root) if n.is_leaf)
        looked = len(result.visited_leaves)
        caption = (
            f"Moves {order}: alpha-beta looks at {looked} of {leaves} leaves and prunes {leaves - looked}. "
            f"The root's value is {num(result.value)} whatever the order."
        )
        shape = _uniform_shape(root)
        if shape is not None:
            d, b = shape
            least = b ** ((d + 1) // 2) + b ** (d // 2) - 1  # Knuth and Moore's count for a perfect order
            caption += f" A perfect order on a tree like this, with no ties, looks at {least}."
        facts = [("Leaves looked at", f"{looked} of {leaves}"), ("Pruned", str(leaves - looked)),
                 ("Root value", num(result.value))]  # fmt: skip
        return Frame(drawing.tree_svg(root, max_first, result.events), caption, facts)

    def lesson(self, data: dict[str, Any]) -> str:
        return (
            "Best moves first prunes the most and worst first the least (often nothing); the root's value is the "
            "same every time, because pruning only skips moves that can't change the answer."
        )


register(AlphaBetaOrder())
