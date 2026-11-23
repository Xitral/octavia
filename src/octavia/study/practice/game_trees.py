"""Game-tree practice: minimax and alpha-beta pruning, on the course's conventions (``study.algorithms``).

A problem's data: ``tree`` (nested lists of leaf values) and ``max_first``. Leaves are numbered 1, 2, 3... left to
right and inner nodes lettered A, B, C... level by level, which is how answers and walkthroughs name them.
"""

from __future__ import annotations

import math
import random
import re
from typing import Any

from pydantic import Field

from octavia.study import drawing
from octavia.study.algorithms import (
    INF,
    ABEvent,
    ABResult,
    GameNode,
    alphabeta,
    build_tree,
    minimax,
    num,
    random_tree,
    tree_spec,
    walk,
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


class TreeGiven(Parts):
    tree: list[Any] = Field(description="Nested lists of leaf values, e.g. [[3, 12, 8], [2, 4, 6]].")
    max_first: bool = True


class TreeAnswer(Parts):
    root_value: float | None = None
    node_values: dict[str, float] | None = Field(default=None, description="Inner node letter -> value.")
    pruned_leaves: list[int] | None = Field(default=None, description="Leaf numbers alpha-beta never looks at.")
    pruned_nodes: list[str] | None = Field(default=None, description="Inner nodes cut off whole, by letter.")


def _depths(root: GameNode) -> dict[str, int]:
    out: dict[str, int] = {}

    def go(node: GameNode, d: int) -> None:
        out[node.id] = d
        for child in node.children:
            go(child, d + 1)

    go(root, 0)
    return out


class _GameTree(PracticeKind):
    subject = "AI: game trees"
    Given = TreeGiven
    Answer = TreeAnswer
    given_hint = '{"tree": nested leaf lists, "max_first": true}'

    bounds = False  # alpha-beta's worksheet also has α and β boxes and branches to cross out

    def make(self, rng: random.Random, size: Size = "exam") -> dict[str, Any]:
        if size == "big":  # like the course's 15a sheet: three moves at every node, 27 leaves
            root = random_tree(rng, depth=3, branching=(3,))
        else:  # like an exam question: 8 or 9 leaves
            depth, branching = rng.choice(((3, (2,)), (2, (3,)), (3, (2,))))
            root = random_tree(rng, depth=depth, branching=branching)
        return {"tree": tree_spec(root), "max_first": True}

    def parse(self, given: Parts) -> dict[str, Any]:
        assert isinstance(given, TreeGiven)
        return {"tree": tree_spec(build_tree(given.tree)), "max_first": given.max_first}

    @staticmethod
    def root(data: dict[str, Any]) -> GameNode:
        return build_tree(data["tree"])

    def statement(self, data: dict[str, Any]) -> str:
        root = self.root(data)
        leaves = [n for n in walk(root) if n.is_leaf]
        top = "MAX" if data.get("max_first", True) else "MIN"
        return (
            f"Game tree, {top} moves first at the root, children left to right. Nested leaf values: "
            f"{tree_spec(root)}. Leaves numbered 1 to {len(leaves)} left to right; inner nodes lettered A, B, C... "
            f"level by level.\nAsk: {self.ask(data)}"
        )

    def describe(self, data: dict[str, Any]) -> str:
        return f"A game tree with {len(data['tree'])} moves at the root"

    # ------------------------------------------------------------------------------------------------ worksheet

    def worksheet(self, data: dict[str, Any], *, theme: Theme = "paper", highlight: int | None = None) -> Worksheet:
        root = self.root(data)
        max_first = data.get("max_first", True)
        lay = drawing.tree_layout(root, roomy=True)
        focus = ""
        if highlight is not None:
            steps = self.steps(data)
            if 0 <= highlight < len(steps):
                focus = _step_node(steps[highlight].words)
        colours = drawing.PAPER if theme == "paper" else drawing.DARK
        svg = drawing.tree_svg(root, max_first, theme=colours, layout=lay, blank=True, focus=focus)
        blanks: list[Blank] = []
        for node in walk(root):
            if node.is_leaf:
                continue
            is_max = (lay.depth[node.id] % 2 == 0) == max_first
            blanks.append(Blank(f"v:{node.id}", lay.x[node.id], lay.y[node.id] + (9 if is_max else -5), width=34))
            if self.bounds:
                blanks.append(Blank(f"a:{node.id}", lay.x[node.id] + 66, lay.y[node.id] - 11, label="α", width=46))
                blanks.append(Blank(f"b:{node.id}", lay.x[node.id] + 66, lay.y[node.id] + 15, label="β", width=46))
                for child in node.children:
                    mx = (lay.x[node.id] + lay.x[child.id]) / 2
                    my = (lay.y[node.id] + lay.y[child.id]) / 2 + 4
                    blanks.append(Blank(f"cut:{child.id}", mx, my, "cut", width=22))
        top = "MAX" if max_first else "MIN"
        what = (
            "Write each node's value in its triangle and its final α and β beside it (∞ and -∞ are fine), and "
            "click the branches alpha-beta never follows to cross them out. Leave pruned nodes empty."
            if self.bounds
            else "Write each node's minimax value in its triangle."
        )
        return Worksheet(svg, lay.width, lay.height, blanks, f"{top} moves first, children left to right. {what}")

    def expected(self, data: dict[str, Any]) -> tuple[dict[str, float | None], set[str]]:
        """What belongs in each box (None: nothing, the node is pruned) and the branches cut (by child)."""
        values = minimax(self.root(data), data.get("max_first", True))
        return {f"v:{k}": v for k, v in values.items() if not k.startswith("#")}, set()

    def mark(self, data: dict[str, Any], filled: dict[str, str]) -> Marked:
        root = self.root(data)
        sheet = self.worksheet(data)
        boxes, cuts = self.expected(data)
        above = _ancestors(root)
        marks: dict[str, str] = {}
        expected: dict[str, str] = {}
        drawn = {b.id.split(":", 1)[1] for b in sheet.blanks if b.kind == "cut" and _ticked(filled.get(b.id, ""))}
        for blank in sheet.blanks:
            written = str(filled.get(blank.id, "")).strip()
            if blank.kind == "cut":
                child = blank.id.split(":", 1)[1]
                up = above[child][:-1]  # the nodes above it
                if child in drawn:  # crossing out inside a pruned branch as well is fine
                    right_cut = child in cuts or any(a in cuts for a in up)
                    marks[blank.id] = "right" if right_cut else "extra"
                    if not right_cut:
                        expected[blank.id] = ""
                elif child in cuts and not any(a in drawn for a in up):
                    marks[blank.id], expected[blank.id] = "missed", "x"
                continue
            want = boxes.get(blank.id)
            if want is None:
                if written:
                    marks[blank.id], expected[blank.id] = "extra", ""
                continue
            if not written:
                marks[blank.id], expected[blank.id] = "missed", num(want)
            elif _same(written, want):
                marks[blank.id] = "right"
            else:
                marks[blank.id], expected[blank.id] = "wrong", num(want)
        right = sum(1 for m in marks.values() if m == "right")
        bad = [b for b, m in marks.items() if m != "right"]
        lines = [f"{right} of {right + len(bad)} right." if bad else f"All {right} right."]
        first = None
        if bad:
            nodes = {self._node_of(b, root) for b in bad}
            steps = self.steps(data)
            first = next((n for n, st in enumerate(steps) if _step_node(st.words) in nodes), None)
            if first is not None:
                lines.append(f"Where it first goes wrong: {steps[first].words}")
        return Marked(Grade(not bad, lines, first), marks, expected)

    @staticmethod
    def _node_of(blank: str, root: GameNode) -> str:
        """The node a blank is about: its own for a value or a bound, the one above the branch for a cut."""
        kind, ident = blank.split(":", 1)
        if kind != "cut":
            return ident
        return next(n.id for n in walk(root) if any(c.id == ident for c in n.children))

    def _node_values(
        self, data: dict[str, Any], answer: TreeAnswer, lines: list[str], values: dict[str, float] | None = None
    ) -> bool:
        """Checks ``answer.node_values`` bottom up against ``values`` (minimax's, unless given: alpha-beta's, where
        a node only has the children it looked at and a pruned one has none); whether they're all right."""
        if not answer.node_values:
            return True
        root = self.root(data)
        max_first = data.get("max_first", True)
        values = values if values is not None else minimax(root, max_first)
        inner = {n.id for n in walk(root) if not n.is_leaf}
        given = {k.strip().upper(): v for k, v in answer.node_values.items()}
        unknown = sorted(k for k in given if k not in inner)
        if unknown:
            lines.append("No such inner node: " + ", ".join(unknown) + " (inner nodes are lettered A, B, C...).")
        skipped = sorted(k for k in given if k in inner and k not in values)
        if skipped:
            lines.append(f"Pruned, so never given a value: {', '.join(skipped)}.")
        order = [n.id for n in reversed(walk(root)) if n.id in given and n.id in values and n.id in inner]
        bad = [i for i in order if given[i] != values[i]]
        if not bad:
            if order:
                lines.append(f"Node values ({', '.join(order)}): right.")
            return not unknown and not skipped
        ident = bad[0]
        node = next(n for n in walk(root) if n.id == ident)
        kids = ", ".join(num(values[c.id] if not c.is_leaf else c.value) for c in node.children if c.id in values)
        is_max = (_depths(root)[ident] % 2 == 0) == max_first
        lines.append(
            f"First wrong value from the bottom: {ident} is {num(values[ident])}, not {num(given[ident])}. "
            f"It's a {'MAX' if is_max else 'MIN'} node over {kids}"
            + (" (the children it looked at)" if len(kids.split(", ")) < len(node.children) else "")
            + f", so it takes the {'largest' if is_max else 'smallest'}."
        )
        return False


class Minimax(_GameTree):
    name = "minimax"
    topic = "Minimax"
    answer_hint = '{"root_value": n, "node_values": {"B": n, ...} optional}'

    def ask(self, data: dict[str, Any]) -> str:
        return "the minimax value of the root (and of every inner node, if they like)."

    def grade(self, data: dict[str, Any], answer: Parts) -> Grade:
        assert isinstance(answer, TreeAnswer)
        value = minimax(self.root(data), data.get("max_first", True))["A"]
        lines: list[str] = []
        right = True
        if answer.root_value is not None:
            right = answer.root_value == value
            lines.append(
                f"Root value {num(value)}: right."
                if right
                else f"Root value: they said {num(answer.root_value)}, it's {num(value)}."
            )
        right = self._node_values(data, answer, lines) and right
        if not lines:
            return Grade(False, ["Nothing to check: give the root value."])
        return Grade(right, lines)

    def steps(self, data: dict[str, Any]) -> list[Step]:
        root = self.root(data)
        max_first = data.get("max_first", True)
        values = minimax(root, max_first)
        depth = _depths(root)
        out = []
        for node in reversed(walk(root)):  # bottom up, as values are known
            if node.is_leaf:
                continue
            is_max = (depth[node.id] % 2 == 0) == max_first
            kids = ", ".join(num(values[c.id]) for c in node.children)
            out.append(
                Step(
                    f"{node.id} ({'MAX' if is_max else 'MIN'}) takes the {'largest' if is_max else 'smallest'} of "
                    f"{kids}: {num(values[node.id])}.",
                    len(out) + 1,
                )
            )
        return out

    def summary(self, data: dict[str, Any]) -> str:
        return f"Root value {num(minimax(self.root(data), data.get('max_first', True))['A'])}."

    def picture(self, data: dict[str, Any], at: int | None = None) -> str:
        root = self.root(data)
        max_first = data.get("max_first", True)
        if at is None:
            return drawing.tree_svg(root, max_first)
        values = minimax(root, max_first)
        order = [n.id for n in reversed(walk(root)) if not n.is_leaf][: max(0, at)]
        shown = {k: v for k, v in values.items() if k in order or next(n for n in walk(root) if n.id == k).is_leaf}
        return drawing.tree_svg(root, max_first, values=shown)


class AlphaBeta(_GameTree):
    name = "alphabeta"
    topic = "Alpha-beta pruning"
    bounds = True

    def expected(self, data: dict[str, Any]) -> tuple[dict[str, float | None], set[str]]:
        result = self.solve(data)
        boxes: dict[str, float | None] = {}
        for node in walk(self.root(data)):
            if node.is_leaf:
                continue
            final = result.final.get(node.id)
            boxes[f"v:{node.id}"] = final[2] if final else None
            boxes[f"a:{node.id}"] = final[0] if final else None
            boxes[f"b:{node.id}"] = final[1] if final else None
        return boxes, set(result.pruned_nodes)

    answer_hint = '{"root_value": n, "pruned_leaves": [leaf numbers], "pruned_nodes": [letters] optional}'

    def ask(self, data: dict[str, Any]) -> str:
        return "the root's value, and which leaves alpha-beta never looks at (by number)."

    def solve(self, data: dict[str, Any]) -> ABResult:
        return alphabeta(self.root(data), data.get("max_first", True))

    def steps(self, data: dict[str, Any]) -> list[Step]:
        return [Step(s[0], s[1]) for s in self._walk(data)]

    def _walk(self, data: dict[str, Any]) -> list[tuple[str, int, frozenset[int], frozenset[int]]]:
        """One step per inner node, as each finishes (all its children seen, or the rest pruned): the words, how
        many trace events the picture shows, the leaves it read, and the leaves its cut skipped."""
        root = self.root(data)
        max_first = data.get("max_first", True)
        result = self.solve(data)
        by_id = {n.id: n for n in walk(root)}
        depth = _depths(root)

        def is_max(ident: str) -> bool:
            return (depth[ident] % 2 == 0) == max_first

        def who(ident: str) -> str:
            node = by_id[ident]
            return f"leaf {node.leaf}" if node.is_leaf else f"{ident} ({'MAX' if is_max(ident) else 'MIN'})"

        entered: dict[str, ABEvent] = {}
        backs: dict[str, list[ABEvent]] = {}
        out = []
        events = result.events
        for n, event in enumerate(events):
            if event.kind == "enter":
                entered[event.node] = event
                continue
            if event.kind == "back":
                backs.setdefault(event.node, []).append(event)
            node = by_id[event.node]
            if node.is_leaf:
                continue
            finished = event.kind == "prune" or (event.kind == "back" and event.child == node.children[-1].id)
            if event.kind == "back" and n + 1 < len(events) and events[n + 1].kind == "prune":
                continue  # said with the prune that follows
            if not finished:
                continue
            start, came = entered[event.node], backs.get(event.node, [])
            mine, best = is_max(event.node), "largest" if is_max(event.node) else "smallest"
            kids = [by_id[e.child] for e in came]
            if all(k.is_leaf for k in kids):
                seen = ", ".join(f"{k.leaf}: {num(k.value)}" for k in kids)
                which = "leaves" if len(kids) > 1 else "leaf"
                middle = f"reads {which} {seen} and keeps the {best}, {num(came[-1].value)}"
            else:
                seen = ", ".join(f"{k.id} = {num(result.final[k.id][2])}" for k in kids)
                middle = f"gets back {seen} and keeps the {best}, {num(came[-1].value)}"
            before, after = (start.alpha, event.alpha) if mine else (start.beta, event.beta)
            name = "α" if mine else "β"
            bound = f"{name} stays {num(after)}" if before == after else f"{name} = {num(after)}"
            words = (
                f"{who(event.node)} starts with α = {num(start.alpha)}, β = {num(start.beta)}, {middle}, so {bound}."
            )
            cut: frozenset[int] = frozenset()
            if event.kind == "prune":
                cut = frozenset(x.leaf for p in event.pruned for x in walk(by_id[p]) if x.is_leaf)
                skipped = ", ".join(who(p) for p in event.pruned)
                reason = (
                    "MAX can already get α here, more than MIN above would ever allow"
                    if mine
                    else "MIN can already hold MAX to β here, less than MAX above can get elsewhere"
                )
                words += f" Now α = {num(event.alpha)} ≥ β = {num(event.beta)}: prune {skipped} ({reason})."
            out.append((words, n + 1, frozenset(k.leaf for k in kids if k.is_leaf), cut))
        return out

    def grade(self, data: dict[str, Any], answer: Parts) -> Grade:
        assert isinstance(answer, TreeAnswer)
        result = self.solve(data)
        lines: list[str] = []
        right = True
        first: int | None = None
        if answer.root_value is not None:
            if answer.root_value == result.value:
                lines.append(f"Root value {num(result.value)}: right.")
            else:
                right = False
                lines.append(f"Root value: they said {num(answer.root_value)}, it's {num(result.value)}.")
        values = {k: v for k, (_, _, v) in result.final.items() if v is not None}
        right = self._node_values(data, answer, lines, values) and right
        root = self.root(data)
        count = sum(1 for n in walk(root) if n.is_leaf)
        named: set[int] = set()
        if answer.pruned_nodes:
            by_id = {n.id: n for n in walk(root) if not n.is_leaf}
            for ident in answer.pruned_nodes:
                node = by_id.get(ident.strip().upper())
                if node is None:
                    right = False
                    lines.append(f"There's no inner node {ident.strip().upper()} (they're lettered A, B, C...).")
                else:
                    named |= {x.leaf for x in walk(node) if x.is_leaf}
        if answer.pruned_leaves is not None or answer.pruned_nodes:
            given = set(answer.pruned_leaves or []) | named
            bad = sorted(n for n in given if not 1 <= n <= count)
            if bad:
                right = False
                lines.append(f"There's no leaf {', '.join(map(str, bad))}: the leaves are numbered 1 to {count}.")
                given -= set(bad)
            steps = self._walk(data)
            theirs, ours = given, set(result.pruned_leaves)
            missing, extra = sorted(ours - theirs), sorted(theirs - ours)
            if not missing and not extra:
                lines.append("Pruned leaves " + (", ".join(map(str, sorted(ours))) or "none") + ": right.")
            else:
                right = False
                if missing:
                    k = next(n for n, st in enumerate(steps) if missing[0] in st[3])
                    first = k
                    lines.append(
                        f"Missed: leaves {', '.join(map(str, missing))} are pruned. The first of them: {steps[k][0]}"
                    )
                if extra:
                    leaf = extra[0]
                    found = next((n for n in walk(root) if n.is_leaf and n.leaf == leaf), None)
                    if found is not None:
                        event = next(e for e in result.events if e.kind == "leaf" and e.node == found.id)
                        k = next(n for n, st in enumerate(steps) if leaf in st[2])
                        first = k if first is None else min(first, k)
                        lines.append(
                            f"Not pruned: leaves {', '.join(map(str, extra))} are looked at. When the search reaches "
                            f"leaf {leaf}, α = {num(event.alpha)} and β = {num(event.beta)}: α < β, so nothing above "
                            "it can be cut yet."
                        )
        if not lines:
            return Grade(False, ["Nothing to check: give the root value and the pruned leaves."])
        return Grade(right, lines, first)

    def summary(self, data: dict[str, Any]) -> str:
        result = self.solve(data)
        return f"Root value {num(result.value)}. Pruned leaves: {', '.join(map(str, result.pruned_leaves)) or 'none'}."

    def picture(self, data: dict[str, Any], at: int | None = None) -> str:
        root = self.root(data)
        if at is None:
            return drawing.tree_svg(root, data.get("max_first", True))
        return drawing.tree_svg(root, data.get("max_first", True), self.solve(data).events, at)


register(Minimax())
register(AlphaBeta())


def read_number(text: str) -> float | None:
    """A value written in a box: "12", "-3", "2.5", "∞", "-inf", "+∞"; None when it isn't one."""
    said = text.strip().replace(" ", "").replace("−", "-").replace("–", "-").casefold()
    said = re.sub(r"^(?:≤|≥|<=|>=|<|>)", "", said)  # a pruned node's value written as a bound: "≤2"
    if said in ("∞", "+∞", "inf", "+inf", "infinity", "+infinity"):
        return INF
    if said in ("-∞", "-inf", "-infinity"):
        return -INF
    try:
        value = float(said)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _ticked(text: object) -> bool:
    return str(text).strip().casefold() in ("x", "1", "true", "cut", "yes")


def _same(text: str, expected: float) -> bool:
    value = read_number(text)
    return value is not None and value == expected


def _ancestors(root: GameNode) -> dict[str, list[str]]:
    """Each node's ancestors, root first, the node itself last."""
    out: dict[str, list[str]] = {}

    def go(node: GameNode, above: list[str]) -> None:
        out[node.id] = [*above, node.id]
        for child in node.children:
            go(child, out[node.id])

    go(root, [])
    return out


def _step_node(words: str) -> str:
    """The node a walkthrough step is about: its first word ("E (MAX) starts with..." is E)."""
    return words.split(" ", 1)[0]
