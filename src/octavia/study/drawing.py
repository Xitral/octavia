"""Pictures of game trees and search graphs, drawn as SVG the way the course's slides draw them: MAX nodes as
triangles pointing up, MIN nodes pointing down, leaves as boxes numbered left to right, α and β beside each node,
pruned branches dashed red; graph states as circles with the heuristic in red and each step cost on its edge.

Each picture shows one moment of a trace (``upto``): what's known by then and nothing after, so a walkthrough can
step through the same panel without giving the next step away. ``DARK`` suits her panels' glass; ``PAPER`` is for a
worksheet, on screen or printed. ``tree_layout`` and ``graph_layout`` say where everything is, so a worksheet can put
its blanks on the drawing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from octavia.study.algorithms import ABEvent, GameNode, Graph, SearchStep, num, walk

FONT = 'font-family="Segoe UI, sans-serif"'


@dataclass(frozen=True)
class Theme:
    ink: str
    dim: str
    glow: str
    done: str
    red: str
    green: str
    fill: str  # a node's inside
    here: str  # the node being looked at, inside
    label_bg: str  # behind an edge's cost


DARK = Theme("#d8e6ff", "#6f7f99", "#7fd6ff", "#3a5a86", "#ff6b6b", "#7be3a0", "#0d1a2b", "#123049", "#0a1422")
PAPER = Theme("#111111", "#8a8a8a", "#1565c0", "#dfe8f5", "#c62828", "#2e7d32", "#ffffff", "#e3f0ff", "#ffffff")


def _t(x: float, y: float, text: str, size: int, fill: str, anchor: str = "middle", bold: bool = False) -> str:
    weight = ' font-weight="700"' if bold else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}"{weight}>'
        f"{escape(text)}</text>"
    )


# ---------------------------------------------------------------------------------------------------- game trees


@dataclass
class TreeLayout:
    x: dict[str, float]
    y: dict[str, float]
    depth: dict[str, int]
    width: float
    height: float


def tree_layout(root: GameNode, roomy: bool = False) -> TreeLayout:
    """Where each node goes: leaves evenly across, each inner node centred over its children, a row per level.
    ``roomy``: more space between leaves and rows, for blanks to write in."""
    nodes = walk(root)
    leaves = [n for n in nodes if n.is_leaf]
    depth: dict[str, int] = {}

    def levels(node: GameNode, d: int) -> None:
        depth[node.id] = d
        for child in node.children:
            levels(child, d + 1)

    levels(root, 0)
    gap = (86 if len(leaves) <= 12 else 66) if roomy else (46 if len(leaves) > 18 else 58)
    row = 120 if roomy else 92
    width = max(360, gap * len(leaves) + (130 if roomy else 70))
    height = row * max(depth.values()) + (130 if roomy else 110)
    x: dict[str, float] = {}
    for n, leaf in enumerate(leaves):
        x[leaf.id] = 45 + gap * n + gap / 2

    def place(node: GameNode) -> float:
        if node.id not in x:
            x[node.id] = sum(place(c) for c in node.children) / len(node.children)
        return x[node.id]

    place(root)
    y = {n.id: 46 + row * depth[n.id] for n in nodes}
    return TreeLayout(x, y, depth, width, height)


def tree_svg(
    root: GameNode,
    max_first: bool = True,
    events: Sequence[ABEvent] = (),
    upto: int | None = None,
    values: dict[str, float] | None = None,
    show_leaf_values: bool = True,
    theme: Theme = DARK,
    layout: TreeLayout | None = None,
    blank: bool = False,
    focus: str = "",
) -> str:
    """The tree, with what ``events[:upto]`` of an alpha-beta trace has found (values, α/β, pruned branches, the
    node being looked at), or with minimax ``values`` when given. ``blank``: no node names inside the shapes, for
    a worksheet whose boxes go there."""
    nodes = walk(root)
    lay = layout or tree_layout(root)
    x, y, depth = lay.x, lay.y, lay.depth
    seen = list(events[:upto]) if upto is not None else list(events)
    bounds: dict[str, tuple[float, float]] = {}
    known: dict[str, float] = dict(values or {})
    pruned: set[str] = set()
    current = focus or (seen[-1].node if seen else "")
    for event in seen:
        bounds[event.node] = (event.alpha, event.beta)
        if event.value is not None:
            known[event.node] = event.value
        if event.kind == "prune":
            for pid in event.pruned:
                pruned.update(n.id for n in walk(_find(root, pid)))
    tracing = bool(events)
    t = theme

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {lay.width:.0f} {lay.height:.0f}"><g {FONT}>']
    for node in nodes:  # edges first, under the nodes
        for child in node.children:
            cut = child.id in pruned
            dash = ' stroke-dasharray="5 4"' if cut else ""
            colour = t.red if cut else (t.ink if not tracing or child.id in bounds else t.dim)
            parts.append(
                f'<line x1="{x[node.id]:.1f}" y1="{y[node.id] + 14:.1f}" x2="{x[child.id]:.1f}" '
                f'y2="{y[child.id] - 14:.1f}" stroke="{colour}" stroke-width="1.6"{dash}/>'
            )
    for node in nodes:
        cx, cy = x[node.id], y[node.id]
        is_max = (depth[node.id] % 2 == 0) == max_first
        here = node.id == current
        cut = node.id in pruned
        stroke = t.glow if here else (t.dim if cut or (tracing and node.id not in bounds) else t.ink)
        fill = t.here if here else t.fill
        if node.is_leaf:
            parts.append(
                f'<rect x="{cx - 17:.1f}" y="{cy - 14:.1f}" width="34" height="28" rx="3" fill="{fill}" '
                f'stroke="{stroke}" stroke-width="{2.4 if here else 1.4}"/>'
            )
            if show_leaf_values:
                parts.append(_t(cx, cy + 5, num(node.value), 13, t.dim if cut else t.ink, bold=True))
            parts.append(_t(cx, cy + 30, str(node.leaf), 10, t.dim))
            continue
        size = 1.7 if blank else 1.0
        tri = (
            f"{cx:.1f},{cy - 17 * size:.1f} {cx - 21 * size:.1f},{cy + 15 * size:.1f} "
            f"{cx + 21 * size:.1f},{cy + 15 * size:.1f}"
            if is_max
            else f"{cx:.1f},{cy + 17 * size:.1f} {cx - 21 * size:.1f},{cy - 15 * size:.1f} "
            f"{cx + 21 * size:.1f},{cy - 15 * size:.1f}"
        )
        parts.append(f'<polygon points="{tri}" fill="{fill}" stroke="{stroke}" stroke-width="{2.4 if here else 1.4}"/>')
        if node.id in known and not cut:
            parts.append(
                _t(cx, cy + (8 if is_max else 1), num(known[node.id]), 12, t.glow if here else t.ink, bold=True)
            )
        parts.append(_t(cx - 26 * size, cy - 8, node.id, 10 if not blank else 12, t.dim, anchor="end"))
        if node.id in bounds and not cut:
            a, b = bounds[node.id]
            parts.append(_t(cx + 25, cy - 3, f"α: {num(a)}", 11, t.ink, anchor="start"))
            parts.append(_t(cx + 25, cy + 11, f"β: {num(b)}", 11, t.ink, anchor="start"))
    parts.append("</g></svg>")
    return "".join(parts)


def _find(root: GameNode, ident: str) -> GameNode:
    return next(n for n in walk(root) if n.id == ident)


# ---------------------------------------------------------------------------------------------------- graphs


def _positions(graph: Graph) -> dict[str, tuple[float, float]]:
    if graph.where:
        return dict(graph.where)
    # Columns by how many steps each state is from the start (the goal in a column of its own at the end, when it's
    # furthest), rows in alphabetical order within a column, like the course's figures read left to right.
    level = {graph.start: 0}
    queue = [graph.start]
    while queue:
        state = queue.pop(0)
        for nxt, _ in graph.neighbours(state):
            if nxt not in level:
                level[nxt] = level[state] + 1
                queue.append(nxt)
    last = max(level.values(), default=0)
    for state in graph.states:
        level.setdefault(state, last + 1)  # unreachable: after everything else
    if level[graph.goal] == max(level.values()):
        crowded = sum(1 for s, v in level.items() if v == level[graph.goal]) > 1
        if crowded:
            level[graph.goal] = max(level.values()) + 1
    columns: dict[int, list[str]] = {}
    for state in sorted(level, key=lambda s: (level[s], s)):
        columns.setdefault(level[state], []).append(state)
    out: dict[str, tuple[float, float]] = {}
    for col, states in columns.items():
        for row, state in enumerate(states):
            out[state] = (float(col), row - (len(states) - 1) / 2 + (0.18 if col % 2 else 0.0))
    return out


@dataclass
class GraphLayout:
    at: dict[str, tuple[float, float]]
    width: float
    height: float


def graph_layout(graph: Graph, roomy: bool = False) -> GraphLayout:
    where = _positions(graph)
    xs = [p[0] for p in where.values()]
    ys = [p[1] for p in where.values()]
    span_x, span_y = max(xs) - min(xs) or 1.0, max(ys) - min(ys) or 1.0
    width = 720 if roomy else 560
    height = max(260 if roomy else 220, int((150 if roomy else 120) + (110 if roomy else 90) * span_y))
    sx = (width - 100) / span_x
    sy = (height - (110 if roomy else 90)) / span_y if span_y else 0
    at = {s: (50 + (p[0] - min(xs)) * sx, 50 + (p[1] - min(ys)) * sy) for s, p in where.items()}
    return GraphLayout(at, width, height)


def graph_svg(
    graph: Graph,
    steps: Sequence[SearchStep] = (),
    upto: int | None = None,
    show_h: bool = True,
    path: Sequence[str] = (),
    theme: Theme = DARK,
    layout: GraphLayout | None = None,
    focus: str = "",
) -> str:
    """The graph, with what ``steps[:upto]`` of a search has done: states expanded (filled), on the frontier
    (outlined), the one just expanded (glowing), and ``path`` (thick) once it's found."""
    lay = layout or graph_layout(graph)
    t = theme
    done = list(steps[:upto]) if upto is not None else list(steps)
    expanded = {s.expanded for s in done}
    frontier = {e.state for e in done[-1].frontier} if done else set()
    current = focus or (done[-1].expanded if done else "")
    on_path = set(zip(path, path[1:], strict=False)) | set(zip(path[1:], path, strict=False))

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {lay.width:.0f} {lay.height:.0f}"><g {FONT}>']
    drawn: set[frozenset[str]] = set()
    for a in graph.states:
        for b, cost in graph.neighbours(a):
            key = frozenset((a, b))
            if key in drawn:
                continue
            drawn.add(key)
            (x1, y1), (x2, y2) = lay.at[a], lay.at[b]
            thick = (a, b) in on_path
            parts.append(
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{t.green if thick else t.dim}" '
                f'stroke-width="{4 if thick else 1.5}"/>'
            )
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            parts.append(
                f'<rect x="{mx - 11:.1f}" y="{my - 10:.1f}" width="22" height="16" rx="3" fill="{t.label_bg}"/>'
            )
            parts.append(_t(mx, my + 3, num(cost), 11, t.ink))
    for state in graph.states:
        cx, cy = lay.at[state]
        here = state == current
        if here:
            fill, stroke = t.here, t.glow
        elif state in expanded:
            fill, stroke = t.done, t.ink
        else:
            fill, stroke = t.fill, t.glow if state in frontier else t.ink
        dash = ' stroke-dasharray="4 3"' if state in frontier and not here else ""
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="17" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{3 if here else 1.6}"{dash}/>'
        )
        parts.append(_t(cx, cy + 5, state if len(state) <= 3 else state[:2], 14, t.ink, bold=True))
        if len(state) > 3:  # a long name ("Rimnicu Vilcea") goes under its circle
            parts.append(_t(cx, cy + 32, state.title(), 10, t.ink))
        if show_h and any(graph.h.values()):
            parts.append(_t(cx + 15, cy - 19, num(graph.h[state]), 11, t.red, anchor="start", bold=True))
    parts.append("</g></svg>")
    return "".join(parts)
