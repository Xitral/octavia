"""Exams noticed in what Canvas lists, so the study tracker knows one is coming before the user mentions it.

Canvas has no "exam" type: an exam is graded work (an assignment or a quiz) whose title says so ("Midterm",
"Class Test 2", "Final Exam"). Those due within ``AHEAD_D`` days go into the tracker under their course, dated by
when they're due (in local time), once: an exam already there (same name, or the same day in that course) is left
as it is, so whatever the user and she recorded about it stands.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from octavia.study.store import Course, Exam, StudyStore, best_match, clean, match, name_key

if TYPE_CHECKING:
    from octavia.tools.extensions.canvas import CanvasItem

log = logging.getLogger(__name__)

AHEAD_D = 30
# "Midterm", "Final Exam", "Exam 2", "Class Test 2", "Test 1", "Prelim"; never "Test Corrections", "Pre-test
# survey" or a quiz (there are dozens of those, and most are small).
_EXAM = re.compile(
    r"\b(?:mid-?terms?|finals?(?: exam)?|exams?|prelims?|(?:class |unit |chapter )?tests? ?\d+|class tests?)\b",
    re.IGNORECASE,
)
_NOT_EXAM = re.compile(
    r"\b(?:corrections?|review|study guide|survey|reflection|practice|pre-?test|retake form|wrapper|schedule"
    r"|project|paper|presentation|essay|report|proposal|portfolio|lab)\b",
    re.IGNORECASE,
)


def is_exam(title: str) -> bool:
    return bool(_EXAM.search(title)) and not _NOT_EXAM.search(title)


def course_for(courses: Sequence[Course], canvas_name: str) -> Course | None:
    """The tracked course a Canvas course is: one that has it as a name (linked with ``study_update``'s
    ``canvas_name``) or holds its exams, else one its name matches."""
    key = name_key(canvas_name)
    for course in courses:
        if key in {name_key(n) for n in course.names()}:
            return course
        if any(e.source == "canvas" and name_key(e.canvas_course) == key for e in course.exams):
            return course
    return match(courses, canvas_name, Course.names)[0]


def _same_exam(course: Course, item: CanvasItem) -> Exam | None:
    """The exam already tracked that ``item`` is: Canvas's own (by name), else one recorded by the user under a name
    close to Canvas's ("Midterm" for "Midterm Exam") that has no date or Canvas's date."""
    for exam in course.exams:
        if exam.source == "canvas" and name_key(exam.name) == name_key(item.title):
            return exam
    day = item.due.astimezone().date().isoformat()
    recorded = [e for e in course.exams if e.source != "canvas" and (not e.day or e.day == day)]
    found = best_match(recorded, item.title, lambda e: e.name)
    if found is None:  # "Midterm" recorded, "Midterm Exam" in Canvas: the shorter name inside the longer
        inside = [e for e in recorded if re.search(rf"\b{re.escape(name_key(e.name))}\b", name_key(item.title))]
        found = inside[0] if len(inside) == 1 else None
    return found


def add_exams(store: StudyStore, items: Sequence[CanvasItem]) -> list[str]:
    """Put the exams among ``items`` into the tracker, or bring them up to date (a moved date, an exam now done);
    what was added ("Midterm (Intro to AI)")."""
    added: list[str] = []
    now = store.clock()
    for item in items:
        if not is_exam(item.title):
            continue
        target = course_for(store.courses(), item.course)
        name = target.name if target is not None else item.course or "Course"

        def apply(course: Course, item: CanvasItem = item) -> str | None:
            day, at = item.due.astimezone().date().isoformat(), item.due.timestamp()
            exam = _same_exam(course, item)
            if exam is not None:  # what was recorded stands; Canvas only says when, and whether it's done
                exam.day, exam.at, exam.taken = day, at, item.done
                exam.canvas_course = exam.canvas_course or item.course
                return None
            if item.done:
                return None
            course.exams.append(
                Exam(
                    name=clean(item.title, 80),
                    day=day,
                    source="canvas",
                    added=now,
                    canvas_course=item.course,
                    at=at,
                )
            )
            return f"{clean(item.title, 80)} ({course.name})"

        try:
            if (what := store.change(name, apply, create=not item.done)) is not None:
                added.append(what)
        except (KeyError, ValueError, OSError):
            log.debug("Couldn't add the exam %r to the study tracker", item.title, exc_info=True)
    return added


async def sync(store: StudyStore, fetch: Callable[[datetime, datetime], Awaitable[Sequence[CanvasItem]]]) -> list[str]:
    """Look at Canvas's planner for the next ``AHEAD_D`` days and add the exams in it."""
    start = datetime.now().astimezone()
    items = await fetch(start - timedelta(days=1), start + timedelta(days=AHEAD_D))
    return add_exams(store, items)
