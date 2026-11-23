"""Canvas grades (the user's school): each course's current grade, and what was graded lately.

Read-only, with the same token and client as ``canvas_due``. The course list (``/api/v1/courses`` with
``total_scores``) carries each course's current score and letter grade, preferring the current grading period's when
the course has periods, as Canvas's own Grades page does. Recently graded work comes from each course's submissions
(``/api/v1/courses/:id/students/submissions``, which for a student lists only their own), asked for per course and a
few at a time, since Canvas has no single cross-course list of grades. Scores the instructor hasn't released come
back empty from Canvas and are left out rather than read as zero.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import Field

from octavia.core.text import day as _day
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.canvas import (
    MAX_LISTED,
    CanvasClient,
    CanvasError,
    _parse_time,
    canvas_token,
    canvas_url,
    setup_hint,
)

log = logging.getLogger(__name__)

PARALLEL = 4


@dataclass(frozen=True)
class CourseGrade:
    ident: str
    name: str
    code: str
    score: float | None
    grade: str
    hidden: bool = False


@dataclass(frozen=True)
class GradedWork:
    title: str
    course: str
    graded_at: datetime  # aware
    score: float | None
    grade: str
    points: float | None
    late: bool = False
    excused: bool = False


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_course(raw: Any) -> CourseGrade | None:
    """A course the user is a student in, with its current grade; None for anything else."""
    if not isinstance(raw, dict) or raw.get("id") is None or raw.get("access_restricted_by_date"):
        return None
    enrollments = raw.get("enrollments") if isinstance(raw.get("enrollments"), list) else []
    student = next(
        (
            e
            for e in enrollments
            if isinstance(e, dict) and str(e.get("type") or "").lower() in ("student", "studentenrollment")
        ),
        None,
    )
    if student is None:
        return None
    score = _number(student.get("computed_current_score"))
    grade = _text(student.get("computed_current_grade"))
    if raw.get("has_grading_periods") and _number(student.get("current_period_computed_current_score")) is not None:
        score = _number(student.get("current_period_computed_current_score"))
        grade = _text(student.get("current_period_computed_current_grade"))
    return CourseGrade(
        ident=str(raw["id"]),
        name=_text(raw.get("name") or raw.get("course_code") or "Unnamed course"),
        code=_text(raw.get("course_code")),
        score=score,
        grade=grade,
        hidden=bool(raw.get("hide_final_grades")),
    )


def parse_submission(raw: Any, course: str) -> GradedWork | None:
    """A graded submission whose result the user can see; None if it isn't graded or isn't released yet."""
    if not isinstance(raw, dict):
        return None
    graded_at = _parse_time(raw.get("graded_at"))
    score = _number(raw.get("score"))
    grade = _text(raw.get("grade"))
    excused = bool(raw.get("excused"))
    if graded_at is None or (score is None and not grade and not excused):
        return None
    assignment = raw.get("assignment") if isinstance(raw.get("assignment"), dict) else {}
    return GradedWork(
        title=_text(assignment.get("name") or "Untitled")[:200],
        course=course,
        graded_at=graded_at,
        score=score,
        grade=grade,
        points=_number(assignment.get("points_possible")),
        late=bool(raw.get("late")),
        excused=excused,
    )


def _plain(value: float) -> str:
    return f"{round(value, 2):g}"


def describe_course(course: CourseGrade) -> str:
    if course.score is None and not course.grade:
        state = "hidden by the instructor" if course.hidden else "no grade yet"
        return f"{course.name}: {state}"
    score = f"{_plain(course.score)}%" if course.score is not None else ""
    letter = course.grade if course.grade != score else ""
    return f"{course.name}: " + (f"{score} ({letter})" if score and letter else score or letter)


def describe_work(work: GradedWork) -> str:
    if work.excused:
        result = "excused"
    elif work.score is not None and work.points:
        result = f"{_plain(work.score)} out of {_plain(work.points)} ({work.score / work.points * 100:.0f}%)"
        if work.grade and not re.fullmatch(r"-?[\d.]+%?", work.grade):
            result += f", {work.grade}"
    elif work.score is not None:
        result = f"{_plain(work.score)} points"
    else:
        result = work.grade
    late = ", late" if work.late else ""
    course = f" for {work.course}" if work.course else ""
    return f"{work.title}{course}: {result}{late} (graded {_day(work.graded_at.astimezone())})"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def matching(courses: list[CourseGrade], wanted: str) -> list[CourseGrade]:
    """The courses whose name or code contains ``wanted``, ignoring case, spaces and dashes ("ITSC 2214" finds
    "ITSC-2214-001")."""
    key = _norm(wanted)
    return [c for c in courses if key and (key in _norm(c.name) or key in _norm(c.code))]


_COURSE_NUMBER = re.compile(r"\b[A-Z]{2,5}[\s-]?(\d{4})\b")


async def by_her_notes(courses: list[CourseGrade], wanted: str, memory: Any) -> CourseGrade | None:
    """The course the user's own name for it means, by what she knows: Canvas calls the course "ITCS-6150-091:
    ITCS-8150-091_Combined", the user calls it "Intro to AI", and her note says "Intro to AI (ITCS 6150)". A note that
    has ``wanted`` in it and names exactly one of ``courses`` by its number decides; None otherwise. (2026-10-26
    05:29 four calls for "Intro to AI" failed this way, with that note in her prompt.)"""
    key = _norm(wanted)
    if memory is None or len(key) < 2:
        return None
    try:
        notes = await memory.search(wanted, limit=8)
    except Exception:
        log.debug("Couldn't search her notes for %r", wanted, exc_info=True)
        return None
    numbers = {c.ident: set(_COURSE_NUMBER.findall(f"{c.code} {c.name}")) for c in courses}
    for note in notes:
        text = str(getattr(note, "text", note))
        if key not in _norm(text):
            continue
        said = set(_COURSE_NUMBER.findall(text))
        named = [c for c in courses if numbers[c.ident] & said]
        if len(named) == 1:
            return named[0]
    return None


@dataclass
class Grades:
    courses: list[CourseGrade]
    recent: list[GradedWork]
    problems: list[str]


async def fetch_grades(
    since: datetime, course: str = "", transport: httpx.AsyncBaseTransport | None = None, memory: Any = None
) -> Grades:
    """The user's current courses with their grades, and work graded since ``since``, newest first. ``memory``: her
    notes, for a course the user calls by a name Canvas doesn't use (``by_her_notes``)."""
    token = canvas_token()
    if not token:
        raise CanvasError(setup_hint())
    client = CanvasClient(canvas_url(), token, transport)
    async with client.session() as session:
        raw = await client.get_all(
            session,
            "/api/v1/courses",
            {
                "enrollment_state": "active",
                "include[]": ["total_scores", "current_grading_period_scores"],
                "per_page": 100,
            },
        )
        courses = [c for c in (parse_course(r) for r in raw) if c is not None]
        if course.strip():
            chosen = matching(courses, course)
            if not chosen and (known_as := await by_her_notes(courses, course, memory)) is not None:
                chosen = [known_as]
            if not chosen:
                known = "; ".join(c.name for c in courses) or "none"
                raise CanvasError(f"No current Canvas course matches {course!r} (the user's courses: {known})")
            courses = chosen

        gate = asyncio.Semaphore(PARALLEL)
        params = {
            "include[]": ["assignment"],
            "workflow_state": "graded",
            "graded_since": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "order": "graded_at",
            "order_direction": "descending",
            "per_page": 100,
        }

        async def graded(one: CourseGrade) -> tuple[list[GradedWork], str]:
            async with gate:
                try:
                    entries = await client.get_all(session, f"/api/v1/courses/{one.ident}/students/submissions", params)
                except CanvasError as exc:
                    return [], f"{one.name}: {exc}"
            work = [w for w in (parse_submission(e, one.name) for e in entries) if w is not None]
            return [w for w in work if w.graded_at >= since], ""

        results = await asyncio.gather(*(graded(c) for c in courses))
    recent = sorted((w for found, _ in results for w in found), key=lambda w: w.graded_at, reverse=True)
    return Grades(courses=courses, recent=recent, problems=[p for _, p in results if p])


class GradesArgs(ToolArgs):
    course: str = Field(
        default="",
        max_length=100,
        description="Name or code part; omit for all.",
    )
    days: int = Field(default=14, ge=1, le=120, description="Days back for graded work.")


class CanvasGrades(Tool):
    name = "canvas_grades"
    description = (
        "Canvas grades (read-only): each course's current grade and recent scores. Use for 'did my essay get graded'."
    )
    Args = GradesArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 90.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: GradesArgs, ctx: ToolContext) -> str | None:
        return None if canvas_token() else setup_hint(ctx.config)

    async def run(self, args: GradesArgs, ctx: ToolContext) -> ToolResult:
        since = datetime.now(UTC) - timedelta(days=args.days)
        try:
            grades = await fetch_grades(since, args.course, self._transport, ctx.memory)
        except CanvasError as exc:
            return ToolResult.error(str(exc))
        if not grades.courses:
            return ToolResult.success("Canvas lists no current courses the user is a student in.")
        parts = ["Current course grades:\n" + "\n".join(describe_course(c) for c in grades.courses)]
        span = "the last day" if args.days == 1 else f"the last {args.days} days"
        if grades.recent:
            lines = [describe_work(w) for w in grades.recent[:MAX_LISTED]]
            if len(grades.recent) > MAX_LISTED:
                lines.append(f"...and {len(grades.recent) - MAX_LISTED} more.")
            parts.append(f"Graded in {span}, newest first:\n" + "\n".join(lines))
        else:
            parts.append(f"Nothing was graded in {span}.")
        if grades.problems:
            parts.append("Couldn't read recent grades for " + "; ".join(grades.problems[:5]))
        data = {"courses": len(grades.courses), "graded": len(grades.recent), "problems": len(grades.problems)}
        return ToolResult.success("\n\n".join(parts), data=data)


TOOLS = [CanvasGrades]
