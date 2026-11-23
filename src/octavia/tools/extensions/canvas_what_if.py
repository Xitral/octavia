"""Canvas what-if grades (the user's school): one course's assignment groups, their weights and its
graded and ungraded work, the course grade with scores the user imagines, and the score they'd need for a grade.

Read-only, with the same token and client as the other Canvas tools. The groups come from
``/api/v1/courses/:id/assignment_groups`` with each assignment and the user's own submission, and whether the groups
are weighted from the course (``apply_assignment_group_weights``). The grade is worked out the way Canvas's own
What-If Scores do: only work with a score counts, real or imagined, so ungraded work is left out unless the user says
what to assume for it. A group's percent is its points over its points possible; weighted groups are averaged by
weight over the groups that have scored work, unweighted ones pool their points. Drop rules drop the lowest (then
highest) percentages, never the work marked never to drop: Canvas picks whichever drops help most, the same thing for
equal-points work and close otherwise. Canvas's own current grade is shown beside the one worked out here, so a gap
(grading periods, a hidden score) is said plainly instead of passing off an estimate as exact.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import Field

from octavia.core.text import day as _day
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.canvas import MAX_LISTED, CanvasError, _parse_time, canvas_token, setup_hint
from octavia.tools.extensions.canvas_coursework import Canvas, _connect, _no_single_match, choose
from octavia.tools.extensions.canvas_grades import CourseGrade, _number, _plain, _text, describe_course, parse_course

CEILING = 200.0  # the most percent a "what's needed" answer looks for: past full marks only extra credit gets there
MISMATCH = 0.5  # percentage points between Canvas's grade and the one worked out here worth mentioning
MAX_NAMED = 6


@dataclass(frozen=True)
class Assignment:
    ident: str
    name: str
    group: str  # its assignment group's id
    points: float
    score: float | None  # None: not graded yet, or not released
    excused: bool = False
    due: datetime | None = None  # aware

    @property
    def graded(self) -> bool:
        return self.score is not None and not self.excused

    @property
    def left(self) -> bool:
        """Still to be graded, and worth something."""
        return not self.graded and not self.excused and self.points > 0


@dataclass(frozen=True)
class Group:
    ident: str
    name: str
    weight: float  # percent of the course grade, when the course weights its groups
    drop_lowest: int = 0
    drop_highest: int = 0
    never_drop: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Gradebook:
    course: CourseGrade
    weighted: bool
    groups: tuple[Group, ...]
    assignments: tuple[Assignment, ...]

    def remaining(self) -> list[Assignment]:
        return [a for a in self.assignments if a.left]

    def group_name(self, ident: str) -> str:
        return next((g.name for g in self.groups if g.ident == ident), "")

    def label(self, work: Assignment) -> str:
        return f"{work.name} ({self.group_name(work.group)}, {_plain(work.points)} points)"


def parse_assignment(raw: Any, group: str) -> Assignment | None:
    """An assignment that counts toward the course grade, with the user's score once it's graded and released."""
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    if raw.get("omit_from_final_grade") or raw.get("grading_type") == "not_graded" or raw.get("published") is False:
        return None
    points = _number(raw.get("points_possible"))
    if points is None or points < 0:
        return None
    submission = raw.get("submission") if isinstance(raw.get("submission"), dict) else {}
    excused = bool(submission.get("excused"))
    return Assignment(
        ident=str(raw["id"]),
        name=_text(raw.get("name")) or "Untitled",
        group=group,
        points=points,
        score=None if excused else _number(submission.get("score")),
        excused=excused,
        due=_parse_time(raw.get("due_at")),
    )


def parse_groups(raw: Sequence[Any]) -> tuple[list[Group], list[Assignment]]:
    """The assignment groups, and every assignment in them that counts, from ``assignment_groups``."""
    groups: list[Group] = []
    assignments: list[Assignment] = []
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("id") is None:
            continue
        ident = str(entry["id"])
        rules = entry.get("rules") if isinstance(entry.get("rules"), dict) else {}
        never = rules.get("never_drop") if isinstance(rules.get("never_drop"), list) else []
        groups.append(
            Group(
                ident=ident,
                name=_text(entry.get("name")) or "Assignments",
                weight=_number(entry.get("group_weight")) or 0.0,
                drop_lowest=max(0, int(_number(rules.get("drop_lowest")) or 0)),
                drop_highest=max(0, int(_number(rules.get("drop_highest")) or 0)),
                never_drop=frozenset(str(i) for i in never),
            )
        )
        for task in entry.get("assignments") if isinstance(entry.get("assignments"), list) else []:
            if (found := parse_assignment(task, ident)) is not None:
                assignments.append(found)
    return groups, assignments


# ---------------------------------------------------------------------------------------------------- the arithmetic

Scored = tuple[float, float, str]  # score, points possible, assignment id


def _after_drops(group: Group, scored: list[Scored]) -> list[Scored]:
    """What of a group's scored work still counts after its drop rules: the lowest, then the highest, percentages
    go, never the work marked never to drop, and at least one score always stays."""
    kept = [s for s in scored if s[2] in group.never_drop or s[1] <= 0]
    droppable = sorted((s for s in scored if s[2] not in group.never_drop and s[1] > 0), key=lambda s: s[0] / s[1])
    spare = 0 if kept else 1
    low = max(0, min(group.drop_lowest, len(droppable) - spare))
    droppable = droppable[low:]
    high = max(0, min(group.drop_highest, len(droppable) - spare))
    return kept + droppable[: len(droppable) - high]


def group_totals(book: Gradebook, scores: Mapping[str, float]) -> dict[str, tuple[float, float]]:
    """Each group's points earned and possible after drops, with ``scores`` (assignment id to points) in place of
    Canvas's and only work that has a score counted."""
    by_group: dict[str, list[Scored]] = {}
    for work in book.assignments:
        if work.ident in scores:
            score = scores[work.ident]
        elif work.graded and work.score is not None:
            score = work.score
        else:
            continue
        by_group.setdefault(work.group, []).append((score, work.points, work.ident))
    totals = {}
    for group in book.groups:
        kept = _after_drops(group, by_group.get(group.ident, []))
        totals[group.ident] = (sum(s for s, _, _ in kept), sum(p for _, p, _ in kept))
    return totals


def course_percent(book: Gradebook, scores: Mapping[str, float] | None = None) -> float | None:
    """The course grade in percent, counting only work with a score; None when nothing has one yet."""
    totals = group_totals(book, scores or {})
    earned = possible = weighted = weights = 0.0
    for group in book.groups:
        got, out_of = totals[group.ident]
        if out_of <= 0:
            continue
        earned += got
        possible += out_of
        weighted += group.weight * got / out_of
        weights += group.weight
    if book.weighted:
        return weighted / weights * 100 if weights > 0 else None
    return earned / possible * 100 if possible > 0 else None


def needed_percent(
    book: Gradebook, scores: Mapping[str, float], solve: Sequence[Assignment], target: float
) -> float | None:
    """The percent to score on each of ``solve`` for the course grade to reach ``target``: 0 when even zeros reach
    it, None when not even ``CEILING`` percent would."""

    def grade_at(percent: float) -> float:
        trial = {**scores, **{w.ident: w.points * percent / 100 for w in solve}}
        return course_percent(book, trial) or 0.0

    if grade_at(0) >= target:
        return 0.0
    if grade_at(CEILING) < target:
        return None
    low, high = 0.0, CEILING
    for _ in range(60):
        middle = (low + high) / 2
        if grade_at(middle) >= target:
            high = middle
        else:
            low = middle
    return high


def _pct(value: float | None) -> str:
    return "no grade yet" if value is None else f"{_plain(round(value, 1))}%"


def _up(value: float) -> float:
    """A needed percent rounded up to a tenth, so reaching what's said is always enough."""
    return math.ceil(value * 10 - 1e-6) / 10


def _names(work: Sequence[Assignment]) -> str:
    shown = ", ".join(w.name for w in work[:MAX_NAMED])
    return shown + (f" and {len(work) - MAX_NAMED} more" if len(work) > MAX_NAMED else "")


# ---------------------------------------------------------------------------------------------------- the answer


def overview(book: Gradebook) -> list[str]:
    """The groups with their weights, drop rules and percent so far, and each one's graded and ungraded work."""
    course = book.course
    if book.weighted:
        weights = ", ".join(f"{g.name} {_plain(g.weight)}%" for g in book.groups)
        lines = [f"{course.name}: its assignment groups are weighted ({weights})."]
    else:
        lines = [f"{course.name}: its grade is total points (the groups aren't weighted)."]
    mine = course_percent(book)
    if course.score is not None or course.grade:
        lines.append("Canvas's current grade: " + describe_course(course).removeprefix(f"{course.name}: ") + ".")
        if course.score is not None and mine is not None and abs(mine - course.score) > MISMATCH:
            lines.append(
                f"Counted from the scores here it comes to {_pct(mine)}, so Canvas counts something differently "
                "(a grading period, a hidden score or a drop rule): treat the what-if figures as close estimates."
            )
    else:
        lines.append(f"Counted from the scores Canvas shows: {_pct(mine)}.")

    totals = group_totals(book, {})
    listed = 0
    for group in book.groups:
        head = f"- {group.name}"
        notes = [f"{_plain(group.weight)}% of the grade"] if book.weighted else []
        if group.drop_lowest:
            notes.append(f"lowest {group.drop_lowest} dropped")
        if group.drop_highest:
            notes.append(f"highest {group.drop_highest} dropped")
        got, out_of = totals[group.ident]
        so_far = f"{_pct(got / out_of * 100)} so far" if out_of > 0 else "nothing graded yet"
        head += (f" ({', '.join(notes)})" if notes else "") + f": {so_far}"
        work = [w for w in book.assignments if w.group == group.ident]
        graded, left, parts = [], [], []
        for item in work:
            if listed >= MAX_LISTED:
                break
            listed += 1
            if item.excused:
                graded.append(f"{item.name} excused")
            elif item.graded and item.score is not None:
                graded.append(f"{item.name} {_plain(item.score)}/{_plain(item.points)}")
            elif item.points > 0:
                due = f", due {_day(item.due.astimezone())}" if item.due else ""
                left.append(f"{item.name} ({_plain(item.points)} points{due})")
        if graded:
            parts.append("graded: " + ", ".join(graded))
        if left:
            parts.append("not graded yet: " + ", ".join(left))
        lines.append(head + ("; " + "; ".join(parts) if parts else ""))
    if listed < len(book.assignments):
        lines.append(f"...and {len(book.assignments) - listed} more assignments not listed.")

    remaining = book.remaining()
    if remaining:
        zeros = course_percent(book, {w.ident: 0.0 for w in remaining})
        full = course_percent(book, {w.ident: w.points for w in remaining})
        lines.append(
            f"With all {len(remaining)} ungraded assignments counted: {_pct(zeros)} if each got a zero, "
            f"{_pct(full)} with full marks on each."
        )
    else:
        lines.append("Everything that counts has been graded.")
    return lines


async def fetch_gradebook(canvas: Canvas, wanted: str) -> Gradebook:
    """The course ``wanted`` names, with its groups, weights and assignments and the user's scores."""
    chosen = await canvas.one_course(wanted)
    raw = await canvas.get(
        f"/api/v1/courses/{chosen.ident}", {"include[]": ["total_scores", "current_grading_period_scores"]}
    )
    course = parse_course(raw) or chosen
    entries = await canvas.get_all(
        f"/api/v1/courses/{chosen.ident}/assignment_groups",
        {"include[]": ["assignments", "submission"], "exclude_response_fields[]": ["description", "rubric"]},
    )
    groups, assignments = parse_groups(entries)
    return Gradebook(
        course=course,
        weighted=bool(raw.get("apply_assignment_group_weights")),
        groups=tuple(groups),
        assignments=tuple(assignments),
    )


class WhatIfScore(ToolArgs):
    assignment: str = Field(min_length=1, max_length=150, description="Name part, e.g. 'test 2'.")
    score: float = Field(ge=0, le=10_000)
    unit: Literal["percent", "points"] = Field(default="percent")


class WhatIfArgs(ToolArgs):
    course: str = Field(min_length=1, max_length=100, description="Name or code part, e.g. '6150'.")
    scores: list[WhatIfScore] = Field(default_factory=list, max_length=30, description="Imagined scores.")
    rest_percent: float | None = Field(
        default=None,
        ge=0,
        le=CEILING,
        description="Percent assumed on all other ungraded work; omit to leave it out, as Canvas's What-If does.",
    )
    target_percent: float | None = Field(
        default=None, gt=0, le=CEILING, description="Course grade wanted; answers the score needed for it."
    )
    solve_for: str = Field(
        default="",
        max_length=150,
        description="Assignment the needed score is for; omit for the same score on all ungraded work.",
    )


class CanvasWhatIf(Tool):
    name = "canvas_what_if"
    description = (
        "Canvas what-if grades for one course (read-only): assignment groups, weights, graded and ungraded work, the "
        "grade with imagined scores, or the score needed for a target grade. Use for 'what if I get 85 on test 2' "
        "or 'what do I need on the final for a 90'."
    )
    Args = WhatIfArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 90.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: WhatIfArgs, ctx: ToolContext) -> str | None:
        return None if canvas_token() else setup_hint(ctx.config)

    async def run(self, args: WhatIfArgs, ctx: ToolContext) -> ToolResult:
        try:
            api = await _connect(self._transport)
            async with api.session() as session:
                book = await fetch_gradebook(Canvas(api, session, ctx.memory), args.course)
        except CanvasError as exc:
            return ToolResult.error(str(exc))
        if not book.assignments:
            return ToolResult.success(f"Canvas shows no assignments that count toward {book.course.name}'s grade.")
        what = f"assignment in {book.course.name}"

        imagined: dict[str, float] = {}
        named: list[tuple[Assignment, float]] = []
        for wish in args.scores:
            work, close = choose(book.assignments, wish.assignment, lambda w: w.name, lambda w: w.ident)
            if work is None:
                return _no_single_match(wish.assignment, close, [], what, book.label)
            if wish.unit == "percent" and work.points <= 0:
                return ToolResult.error(f"{work.name} is worth no points, so say its score in points.")
            points = work.points * wish.score / 100 if wish.unit == "percent" else wish.score
            imagined[work.ident] = points
            named.append((work, points))

        solve: list[Assignment] = []
        if args.target_percent is not None:
            if args.solve_for:
                work, close = choose(book.assignments, args.solve_for, lambda w: w.name, lambda w: w.ident)
                if work is None:
                    return _no_single_match(args.solve_for, close, [], what, book.label)
                if work.points <= 0:
                    return ToolResult.error(f"{work.name} is worth no points, so no score on it changes the grade.")
                solve = [work]
            else:
                solve = [w for w in book.remaining() if w.ident not in imagined]

        assumed = []
        if args.rest_percent is not None:
            assumed = [w for w in book.remaining() if w.ident not in imagined and w not in solve]
            imagined.update({w.ident: w.points * args.rest_percent / 100 for w in assumed})

        lines = overview(book)
        data: dict[str, Any] = {"course": book.course.name, "current": course_percent(book)}
        if named or assumed:
            given = [f"{w.name} at {_plain(round(p, 2))} of {_plain(w.points)} points" for w, p in named]
            if assumed:
                given.append(f"{_plain(args.rest_percent or 0)}% on the other {len(assumed)} ungraded assignments")
            result = course_percent(book, imagined)
            data["what_if"] = result
            line = f"What-if: with {', '.join(given)}, the course grade would be {_pct(result)}."
            unsaid = [w for w in book.remaining() if w.ident not in imagined and w not in solve]
            if unsaid:
                line += f" Not counted, as in Canvas's What-If: {_names(unsaid)}, still ungraded."
            lines.append(line)
        if args.target_percent is not None:
            lines.append(self._needed(book, imagined, solve, args.target_percent, data))
        return ToolResult.success("\n".join(lines), data=data)

    @staticmethod
    def _needed(
        book: Gradebook, scores: Mapping[str, float], solve: Sequence[Assignment], target: float, data: dict[str, Any]
    ) -> str:
        goal = _pct(target)
        if not solve:
            return f"Nothing is left ungraded to reach {goal} with: the grade stands at {_pct(course_percent(book))}."
        which = solve[0].name if len(solve) == 1 else f"each of the {len(solve)} ungraded assignments ({_names(solve)})"
        full = course_percent(book, {**scores, **{w.ident: w.points for w in solve}})
        needed = needed_percent(book, scores, solve, target)
        data["needed"] = needed
        if needed is None:
            return f"{goal} can't be reached: even full marks on {which} give {_pct(full)}."
        if needed == 0:
            zeros = course_percent(book, {**scores, **{w.ident: 0.0 for w in solve}})
            return f"{goal} is already secured: even a zero on {which} leaves {_pct(zeros)}."
        needed = _up(needed)
        points = f" ({_plain(_up(solve[0].points * needed / 100))} of {_plain(solve[0].points)} points)"
        line = f"To reach {goal}: {which} needs {_plain(needed)}%" + (points if len(solve) == 1 else "") + "."
        if needed > 100:
            line += f" That's more than full marks, so only extra credit gets there; full marks give {_pct(full)}."
        return line


TOOLS = [CanvasWhatIf]
