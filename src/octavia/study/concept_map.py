"""A course's concept map, grown from the study tracker: every topic of its exams (and every topic a missed question
was about) as a box, coloured by how well the user knows it, with arrows for the links she has recorded between
them (what builds on what). Weak spots stand out in red.

How well a topic is known (its ``band``) comes only from what the tracker holds, in this order:

* ``weak``: marked a weak spot, or two or more misses on questions about it still waiting for review
* ``shaky``: a missed question about it still waiting for review
* ``solid``: done, with nothing waiting
* ``learning``: started
* ``new``: not started yet

The picture is laid out here in rows down the links: a topic sits below every topic it builds on, so the map reads
from the basics at the top to what builds on them; topics with no links yet go in a last block of their own. The
panel gets it as a scene (``map_scene``): every topic card, arrow and legend entry an element with its own id
("topic-<name>", "link-<from>-<to>"), so she can change, move or add to any part of it later. ``map_svg`` draws the
same as one SVG picture.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from html import escape

from octavia.study.store import Course, best_match, name_key

FONT = 'font-family="Segoe UI, sans-serif"'
BANDS = ("weak", "shaky", "learning", "new", "solid")
SAYING = {
    "weak": "weak spot",
    "shaky": "shaky",
    "learning": "learning",
    "new": "not started",
    "solid": "solid",
}
# (fill, stroke) on the panels' dark glass
COLOURS = {
    "weak": ("#3a1420", "#ff6b6b"),
    "shaky": ("#33270f", "#ffc46b"),
    "learning": ("#15304d", "#7fa7cf"),
    "new": ("#0d1a2b", "#5d6d86"),
    "solid": ("#123a2c", "#7be3a0"),
}
INK = "#d8e6ff"
DIM = "#7f97b8"
EDGE = "#4c6f99"

WIDTH = 680
BOX_W = 118
GAP_X = 16
PER_ROW = 5
LINE_H = 15
GAP_Y = 40
CHARS = 17  # per line of a box's label, at 12.5px
MAX_LINES = 3


@dataclass
class Concept:
    name: str
    exams: list[str] = field(default_factory=list)
    status: str = "todo"
    weak: bool = False
    note: str = ""
    waiting: int = 0  # missed questions about it still to review
    missed: int = 0  # misses on those
    retired: int = 0  # questions about it answered right enough times to retire

    @property
    def band(self) -> str:
        if self.weak or self.missed >= 2:
            return "weak"
        if self.waiting:
            return "shaky"
        if self.status == "done":
            return "solid"
        if self.status == "started":
            return "learning"
        return "new"

    def why(self) -> str:
        """Why it's in its band, in a few words."""
        parts = [{"done": "done", "started": "started"}.get(self.status, "not started")]
        if self.weak:
            parts.append("marked weak" + (f" ({self.note})" if self.note else ""))
        if self.waiting:
            parts.append(f"{self.waiting} missed question{'s' if self.waiting != 1 else ''} to review")
        return ", ".join(parts)


@dataclass
class ConceptMap:
    course: str
    concepts: list[Concept]
    links: list[tuple[int, int, str]]  # (from, to, why), by index into concepts
    loose: list[str]  # recorded links one of whose topics isn't on the map

    def weak_spots(self) -> list[Concept]:
        return sorted(
            (c for c in self.concepts if c.band in ("weak", "shaky")), key=lambda c: (c.band != "weak", -c.missed)
        )


def build_map(course: Course) -> ConceptMap:
    """The map of ``course``: its exams' topics (one box per topic, however many exams cover it), then the topics of
    missed questions that match none of them, and the links whose ends are both on it."""
    concepts: list[Concept] = []
    for exam in sorted(course.exams, key=lambda e: (e.day or "9999", e.added)):
        for topic in exam.topics:
            same = next((c for c in concepts if name_key(c.name) == name_key(topic.name)), None)
            if same is None:
                same = Concept(topic.name, status=topic.status)
                concepts.append(same)
            elif {"done": 2, "started": 1}.get(topic.status, 0) > {"done": 2, "started": 1}.get(same.status, 0):
                same.status = topic.status
            same.exams.append(exam.name)
            same.weak = same.weak or topic.weak
            same.note = same.note or topic.note
    for card in course.cards:
        if not card.topic:
            continue
        concept = best_match(concepts, card.topic, lambda c: c.name)
        if concept is None:
            concept = Concept(card.topic, status="started")
            concepts.append(concept)
        if card.retired:
            concept.retired += 1
        else:
            concept.waiting += 1
            concept.missed += card.misses
    pairs: dict[frozenset[int], tuple[int, int, str]] = {}  # two wordings of one pair: the later one stands
    loose: list[str] = []
    for link in course.links:
        a = best_match(concepts, link.a, lambda c: c.name)
        b = best_match(concepts, link.b, lambda c: c.name)
        if a is None or b is None or a is b:
            loose.append(f"{link.a} → {link.b}")
            continue
        ia, ib = concepts.index(a), concepts.index(b)
        pairs.pop(frozenset((ia, ib)), None)
        pairs[frozenset((ia, ib))] = (ia, ib, link.why)
    return ConceptMap(course.name, concepts, list(pairs.values()), loose)


# ---------------------------------------------------------------------------------------------------- layout


def _layers(links: list[tuple[int, int, str]]) -> dict[int, int]:
    """Each linked concept's row: one below the lowest topic it builds on (the longest chain of links above it).
    Links that would close a loop are left out of the reckoning (they're still drawn)."""
    after: dict[int, list[int]] = {}
    for a, b, _ in links:
        after.setdefault(a, []).append(b)
    state: dict[int, int] = {}  # 1 on the way, 2 done
    order: list[int] = []
    kept: list[tuple[int, int]] = []

    def visit(n: int) -> None:
        state[n] = 1
        for m in after.get(n, []):
            if state.get(m) == 1:
                continue  # a loop: this link doesn't count for rows
            kept.append((n, m))
            if m not in state:
                visit(m)
        state[n] = 2
        order.append(n)

    linked = sorted({a for a, _, _ in links} | {b for _, b, _ in links})
    for n in linked:
        if n not in state:
            visit(n)
    level = {n: 0 for n in linked}
    for n in reversed(order):  # topological: every n before what it leads to
        for a, b in kept:
            if a == n:
                level[b] = max(level[b], level[n] + 1)
    # A topic nothing leads to sits just above the first topic it leads to, not at the top, so its arrows stay short
    # (a long one would run behind the boxes in between).
    has_parent = {b for _, b in kept}
    for n in order:  # what it leads to is settled before it
        if n not in has_parent:
            below = [level[b] for a, b in kept if a == n]
            if below:
                level[n] = max(level[n], min(below) - 1)
    return level


@dataclass
class Box:
    index: int
    x: float
    y: float
    lines: list[str]
    note: str
    h: float = 0.0


def _wrap(name: str) -> list[str]:
    lines = textwrap.wrap(name, CHARS, break_long_words=True) or [name]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        lines[-1] = lines[-1][: CHARS - 1].rstrip() + "…"
    return lines


def _place(cmap: ConceptMap) -> tuple[list[Box], float, float | None]:
    """Every box's place, the drawing's height, and where the "not linked yet" block starts (None without one)."""
    level = _layers(cmap.links)
    parents: dict[int, list[int]] = {}
    children: dict[int, list[int]] = {}
    for a, b, _ in cmap.links:
        parents.setdefault(b, []).append(a)
        children.setdefault(a, []).append(b)
    rows: list[list[int]] = []
    for depth in range(max(level.values(), default=-1) + 1):
        here = sorted(n for n in level if level[n] == depth)
        rows += [here[start : start + PER_ROW] for start in range(0, len(here), PER_ROW)]
    # Fewer crossings: each row in the order of the mean place of the topics it builds on (going down), then of
    # what builds on it (going up), a few times over (the barycentre heuristic).
    column: dict[int, float] = {}

    def settle(row: list[int]) -> None:
        for k, n in enumerate(row):
            column[n] = k - (len(row) - 1) / 2

    def mean(n: int, near: dict[int, list[int]]) -> float:
        known = [column[m] for m in near.get(n, []) if m in column]
        return sum(known) / len(known) if known else column.get(n, 0.0)

    for row in rows:
        settle(row)
    for _ in range(3):
        for row in rows[1:]:
            row.sort(key=lambda n: (mean(n, parents), n))
            settle(row)
        for row in reversed(rows[:-1]):
            row.sort(key=lambda n: (mean(n, children), n))
            settle(row)
    linked_rows = len(rows)
    unlinked = [n for n in range(len(cmap.concepts)) if n not in level]
    for start in range(0, len(unlinked), PER_ROW):
        rows.append(unlinked[start : start + PER_ROW])

    boxes: list[Box] = []
    y = 24.0
    split: float | None = None
    for r, row in enumerate(rows):
        if r == linked_rows and unlinked:
            split = y - (GAP_Y / 2 if r else 0)
            y += 26 if r else 22
        made = []
        for k, n in enumerate(row):
            concept = cmap.concepts[n]
            lines = _wrap(concept.name)
            note = SAYING[concept.band] + (f" · {concept.waiting} to review" if concept.waiting else "")
            x = WIDTH / 2 + (k - (len(row) - 1) / 2) * (BOX_W + GAP_X) - BOX_W / 2
            box = Box(n, x, y, lines, note)
            box.h = 14 + LINE_H * len(lines) + 14
            made.append(box)
        tallest = max(b.h for b in made)
        for box in made:
            box.y += (tallest - box.h) / 2
        boxes += made
        y += tallest + GAP_Y
    return boxes, y - GAP_Y + 50, split


def _t(x: float, y: float, text: str, size: float, fill: str, anchor: str = "middle", bold: bool = False) -> str:
    weight = ' font-weight="700"' if bold else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}"{weight}>'
        f"{escape(text)}</text>"
    )


def map_svg(cmap: ConceptMap) -> str:
    """The map as SVG, for a panel."""
    boxes, height, split = _place(cmap)
    at = {b.index: b for b in boxes}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height:.0f}"><defs>'
        '<marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 L2.5,5 Z" fill="{EDGE}"/></marker></defs><g {FONT}>'
    ]
    if split is not None:
        parts.append(
            f'<line x1="20" y1="{split:.1f}" x2="{WIDTH - 20}" y2="{split:.1f}" stroke="{EDGE}" '
            'stroke-dasharray="3 5" stroke-width="1"/>'
        )
        parts.append(_t(24, split + 15, "Not linked yet", 11, DIM, anchor="start"))
    for a, b, _ in cmap.links:
        ba, bb = at[a], at[b]
        x1, x2 = ba.x + BOX_W / 2, bb.x + BOX_W / 2
        if bb.y > ba.y + ba.h:  # downwards, the usual way
            y1, y2 = ba.y + ba.h, bb.y
            bend = max(18.0, (y2 - y1) / 2)
            d = f"M{x1:.1f},{y1:.1f} C{x1:.1f},{y1 + bend:.1f} {x2:.1f},{y2 - bend:.1f} {x2:.1f},{y2:.1f}"
        else:  # sideways or back up (a loop): round the side
            side = 1 if x2 >= x1 else -1
            x1, x2 = ba.x + (BOX_W if side > 0 else 0), bb.x + (0 if side > 0 else BOX_W)
            y1, y2 = ba.y + ba.h / 2, bb.y + bb.h / 2
            d = f"M{x1:.1f},{y1:.1f} C{x1 + 30 * side:.1f},{y1:.1f} {x2 - 30 * side:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
        parts.append(f'<path d="{d}" fill="none" stroke="{EDGE}" stroke-width="1.5" marker-end="url(#head)"/>')
    for box in boxes:
        concept = cmap.concepts[box.index]
        fill, stroke = COLOURS[concept.band]
        if concept.band == "weak":  # a soft red ring around a weak spot
            parts.append(
                f'<rect x="{box.x - 4:.1f}" y="{box.y - 4:.1f}" width="{BOX_W + 8}" height="{box.h + 8:.1f}" rx="9" '
                f'fill="none" stroke="{stroke}" stroke-opacity="0.35" stroke-width="4"/>'
            )
        parts.append(
            f'<rect x="{box.x:.1f}" y="{box.y:.1f}" width="{BOX_W}" height="{box.h:.1f}" rx="6" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{2.2 if concept.band == "weak" else 1.4}"/>'
        )
        cx = box.x + BOX_W / 2
        for k, line in enumerate(box.lines):
            parts.append(_t(cx, box.y + 17 + LINE_H * k, line, 12.5, INK, bold=concept.band == "weak"))
        parts.append(_t(cx, box.y + box.h - 6, box.note, 9.5, stroke))
    legend_y = height - 18
    x = 24.0
    for band in BANDS:
        fill, stroke = COLOURS[band]
        parts.append(
            f'<rect x="{x:.1f}" y="{legend_y - 9:.1f}" width="12" height="12" rx="2" fill="{fill}" stroke="{stroke}"/>'
        )
        parts.append(_t(x + 17, legend_y + 1, SAYING[band], 11, DIM, anchor="start"))
        x += 34 + 6.2 * len(SAYING[band])
    parts.append("</g></svg>")
    return "".join(parts)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "topic"


def topic_ids(cmap: ConceptMap) -> list[str]:
    """Each topic's element id on the map ("topic-dynamic-programming"), unique even for names that slug alike."""
    ids: list[str] = []
    for concept in cmap.concepts:
        base = f"topic-{_slug(concept.name)}"[:36]
        name, n = base, 2
        while name in ids:
            name, n = f"{base}-{n}", n + 1
        ids.append(name)
    return ids


def map_scene(cmap: ConceptMap) -> dict:
    """The map as a panel scene block: the same picture as ``map_svg``, made of separate elements she can address."""
    boxes, height, split = _place(cmap)
    at = {b.index: b for b in boxes}
    ids = topic_ids(cmap)
    elements: list[dict] = []
    if split is not None:
        elements.append({"id": "unlinked-line", "type": "line", "x": 20, "y": split, "x2": WIDTH - 20, "y2": split,
                         "stroke": EDGE, "stroke_width": 1, "dashed": True})  # fmt: skip
        elements.append({"id": "unlinked-label", "type": "text", "x": 24, "y": split + 12, "text": "Not linked yet",
                         "size": 11, "color": DIM, "anchor": "start"})  # fmt: skip
    for a, b, _ in cmap.links:
        ba, bb = at[a], at[b]
        x1, x2 = ba.x + BOX_W / 2, bb.x + BOX_W / 2
        if bb.y > ba.y + ba.h:
            y1, y2 = ba.y + ba.h, bb.y
            bend = max(18.0, (y2 - y1) / 2)
            d = f"M{x1:.1f},{y1:.1f} C{x1:.1f},{y1 + bend:.1f} {x2:.1f},{y2 - bend:.1f} {x2:.1f},{y2:.1f}"
        else:
            side = 1 if x2 >= x1 else -1
            x1, x2 = ba.x + (BOX_W if side > 0 else 0), bb.x + (0 if side > 0 else BOX_W)
            y1, y2 = ba.y + ba.h / 2, bb.y + bb.h / 2
            d = f"M{x1:.1f},{y1:.1f} C{x1 + 30 * side:.1f},{y1:.1f} {x2 - 30 * side:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
        link = f"link-{ids[a][6:]}-{ids[b][6:]}"[:40]
        while any(e["id"] == link for e in elements):
            link = f"{link[:36]}-{len(elements)}"
        elements.append({"id": link, "type": "path", "d": d, "stroke": EDGE, "stroke_width": 1.5, "arrow": True})
    for box in boxes:
        concept = cmap.concepts[box.index]
        fill, stroke = COLOURS[concept.band]
        weak = concept.band == "weak"
        if weak:
            elements.append({"id": f"ring-{ids[box.index][6:]}"[:40], "type": "rect", "x": box.x - 4, "y": box.y - 4,
                             "w": BOX_W + 8, "h": box.h + 8, "round": 9, "fill": "transparent", "stroke": stroke,
                             "stroke_width": 4, "opacity": 0.35})  # fmt: skip
        elements.append({"id": ids[box.index], "type": "card", "x": box.x, "y": box.y, "w": BOX_W, "h": box.h,
                         "text": "\n".join(box.lines), "note": box.note, "size": 12.5, "color": INK, "fill": fill,
                         "stroke": stroke, "stroke_width": 2.2 if weak else 1.4, "bold": weak})  # fmt: skip
    legend_y = height - 18
    x = 24.0
    for band in BANDS:
        fill, stroke = COLOURS[band]
        elements.append({"id": f"legend-{band}", "type": "rect", "x": x, "y": legend_y - 9, "w": 12, "h": 12, "round": 2,
                         "fill": fill, "stroke": stroke, "stroke_width": 1})  # fmt: skip
        elements.append({"id": f"legend-{band}-text", "type": "text", "x": x + 17, "y": legend_y - 3,
                         "text": SAYING[band], "size": 11, "color": DIM, "anchor": "start"})  # fmt: skip
        x += 34 + 6.2 * len(SAYING[band])
    return {"kind": "scene", "id": "map", "width": WIDTH, "height": round(height), "elements": elements}


def describe_map(cmap: ConceptMap) -> list[str]:
    """The map in words, for her: each topic with its band and why, the links, and links that didn't fit."""
    lines = [f"{cmap.course}: {len(cmap.concepts)} topics, {len(cmap.links)} links."]
    for band in BANDS:
        here = [c for c in cmap.concepts if c.band == band]
        if here:
            lines.append(f"{SAYING[band].capitalize()}: " + "; ".join(f"{c.name} ({c.why()})" for c in here))
    if cmap.links:
        lines.append(
            "Links: "
            + "; ".join(
                f"{cmap.concepts[a].name} → {cmap.concepts[b].name}" + (f" ({why})" if why else "")
                for a, b, why in cmap.links
            )
        )
    if cmap.loose:
        lines.append("Links to topics not on the map (not drawn): " + "; ".join(cmap.loose))
    return lines
