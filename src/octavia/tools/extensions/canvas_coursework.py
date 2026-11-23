"""Canvas coursework (the user's school), read-only: an assignment's or quiz's instructions, rubric and
files, a course's files, pages, modules and syllabus, and its announcements, so the user can understand and start
their work.

It uses the same token and client as ``canvas_due`` and never submits, edits or posts anything: every request is a
GET. Files are downloaded with ``CanvasClient.download`` (the token goes only to Canvas; the file storage Canvas
redirects to gets a signed link and no token) and read by ``_documents``. Canvas links files from an assignment's
description rather than attaching them, so the files are found from those links. Names are matched loosely, the way
the user says them; when several things fit, the closest are listed so the user can say which.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from pydantic import Field
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from octavia.core.text import clock as _clock
from octavia.core.text import day as _day
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._documents import extract_text, not_readable, read_html
from octavia.tools.extensions._pdf_pages import PageError, PagePicture, render_page
from octavia.tools.extensions.canvas import (
    CanvasClient,
    CanvasError,
    _parse_time,
    canvas_token,
    canvas_url,
    setup_hint,
)
from octavia.tools.extensions.canvas_grades import (
    CourseGrade,
    _number,
    _plain,
    _text,
    by_her_notes,
    matching,
    parse_course,
)

T = TypeVar("T")

PARALLEL = 4
MAX_FILE_BYTES = 30_000_000
READ_CHARS = 12_000  # of one document in one answer: about three spoken-length pages of notes
ATTACHMENT_CHARS = 24_000  # of all an assignment's files together
INSTRUCTION_CHARS = 10_000
MAX_FILES = 10
MAX_CHOICES = 6
MAX_ITEMS = 60

SUBMISSION_TYPES = {
    "online_upload": "file upload",
    "online_text_entry": "text entry",
    "online_url": "website link",
    "media_recording": "audio or video recording",
    "online_quiz": "quiz",
    "discussion_topic": "discussion post",
    "external_tool": "an external tool",
    "on_paper": "on paper",
    "none": "nothing to submit",
    "student_annotation": "annotating a document",
    "wiki_page": "a page",
}
ITEM_KINDS = {
    "File": "file",
    "Page": "page",
    "Assignment": "assignment",
    "Quiz": "quiz",
    "Discussion": "discussion",
    "ExternalUrl": "link",
    "ExternalTool": "tool",
}


# ---------------------------------------------------------------------------------------------------- helpers


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _contains(name: str, target: str) -> bool:
    """``target`` in ``name`` (both normalised), a number not taken as the start of a longer one ("hw1" isn't in
    "hw10")."""
    if not target:
        return False
    pattern = (
        ("(?<!\\d)" if target[0].isdigit() else "") + re.escape(target) + ("(?!\\d)" if target[-1].isdigit() else "")
    )
    return re.search(pattern, name) is not None


def choose(
    items: Sequence[T],
    wanted: str,
    label: Callable[[T], str],
    key: Callable[[T], Any] = id,
    group: Callable[[T], Any] | None = None,
) -> tuple[T | None, list[T]]:
    """The one item ``wanted`` names, or None and the closest few when it could be several or nothing fits well.

    An exact name wins, unless a name containing it is in another ``group`` (another course's "Homework 3: Trees"
    may be the one meant); then names containing what was said (ignoring case, spaces and punctuation, so "hw 3"
    finds "HW3: Recursion"); then a fuzzy match, taken on its own only when it's clearly the best. Items with the
    same ``key`` (one file listed under two names) count once."""
    target = _norm(wanted)
    ranked: dict[Any, tuple[float, T]] = {}
    for item in items:
        shown = label(item)
        name = _norm(shown)
        if not name:
            continue
        if name == target:
            score = 300.0
        elif _contains(name, target):
            score = 200.0 - min(len(name) - len(target), 99)
        else:
            score = float(fuzz.WRatio(wanted, shown, processor=default_process))
        ident = key(item)
        if ident not in ranked or score > ranked[ident][0]:
            ranked[ident] = (score, item)
    order = sorted(ranked.values(), key=lambda pair: -pair[0])
    exact = [item for score, item in order if score >= 300]
    if len(exact) == 1 and group is not None:
        containing = [item for score, item in order if score > 100]
        if any(group(item) != group(exact[0]) for item in containing):
            return None, containing[:MAX_CHOICES]
    for floor in (300.0, 100.5):
        best = [item for score, item in order if score >= floor]
        if len(best) == 1:
            return best[0], []
        if best:
            return None, best[:MAX_CHOICES]
    if not order:
        return None, []
    runner_up = order[1][0] if len(order) > 1 else 0.0
    if order[0][0] >= 86 and order[0][0] - runner_up >= 8:
        return order[0][1], []
    return None, [item for _, item in order[:MAX_CHOICES]]


def _when(moment: datetime) -> str:
    local = moment.astimezone()
    year = f", {local.year}" if local.year != datetime.now().astimezone().year else ""
    return f"{_day(local)}{year} at {_clock(local)}"


def _size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{max(1, round(size / 1000))} KB" if size else "size unknown"


def clip(text: str, limit: int, offset: int = 0, more: str = "") -> str:
    """At most ``limit`` characters of ``text`` from ``offset``, cut at a paragraph, line or sentence end, saying
    what part is shown and (``more``, with ``{offset}``) how to read on."""
    total = len(text)
    if offset and offset >= total:
        return f"(It has only {total} characters, so there's nothing after character {offset}.)"
    part = text[offset : offset + limit]
    if offset + len(part) < total:
        for mark in ("\n\n", "\n", ". "):
            cut = part.rfind(mark)
            if cut >= limit * 0.6:
                part = part[: cut + len(mark.rstrip(" "))]
                break
    end = offset + len(part)
    if not offset and end >= total:
        return part.strip()
    note = f"[Showing characters {offset + 1} to {end} of {total}."
    if end < total:
        note += " The rest is cut off here" + (f"; {more.format(offset=end)}" if more else "") + "."
    return part.strip() + f"\n{note}]"


_FILE_PATH = re.compile(r"(?:^|/)files/(\d+)(?:/|$)")
_PAGE_PATH = re.compile(r"/courses/\d+/(?:pages|wiki)/([^/]+)/?$")


def canvas_links(links: Sequence[str], base: str) -> tuple[list[str], list[str]]:
    """The Canvas file ids and page names that ``links`` (from a description) point to, in order, once each. Links
    to anywhere but Canvas are left alone: they stay readable in the text."""
    host = urlsplit(base).hostname
    files: list[str] = []
    pages: list[str] = []
    for link in links:
        parts = urlsplit(urljoin(base + "/", link.strip()))
        if parts.hostname != host:
            continue
        path = parts.path.removeprefix("/api/v1")
        if match := _FILE_PATH.search(path):
            if match.group(1) not in files:
                files.append(match.group(1))
        elif (match := _PAGE_PATH.search(path)) and unquote(match.group(1)) not in pages:
            pages.append(unquote(match.group(1)))
    return files, pages


@dataclass(frozen=True)
class CanvasFile:
    ident: str
    name: str
    size: int
    content_type: str
    url: str
    locked: str = ""  # why it can't be downloaded, when it can't


def parse_file(raw: Any) -> CanvasFile | None:
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    locked = ""
    if raw.get("locked_for_user") or not raw.get("url"):
        locked = read_html(str(raw.get("lock_explanation") or ""))[0] or "Canvas doesn't let the user download it yet"
    size = _number(raw.get("size"))
    return CanvasFile(
        ident=str(raw["id"]),
        name=_text(raw.get("display_name") or raw.get("filename") or f"file {raw['id']}"),
        size=int(size or 0),
        content_type=str(raw.get("content-type") or raw.get("content_type") or ""),
        url=str(raw.get("url") or ""),
        locked=locked,
    )


def describe_file(file: CanvasFile) -> str:
    locked = " (locked)" if file.locked else ""
    return f"{file.name} ({_size(file.size)}, file id {file.ident}){locked}"


async def _quietly(call: Awaitable[T], fallback: T) -> T:
    """``call``'s answer, or ``fallback`` when Canvas refuses it: a course can hide its Files or Pages tab."""
    try:
        return await call
    except CanvasError:
        return fallback


class Canvas:
    """One conversation with Canvas for one tool call: the client, its session and what's been read."""

    def __init__(self, api: CanvasClient, session: httpx.AsyncClient, memory: Any = None) -> None:
        self.api = api
        self.session = session
        self.base = api.base
        self.memory = memory  # her notes, for a course the user calls by a name Canvas doesn't use

    async def courses(self, wanted: str = "") -> list[CourseGrade]:
        """The user's current courses, or those ``wanted`` names (part of a name or code)."""
        raw = await self.api.get_all(self.session, "/api/v1/courses", {"enrollment_state": "active", "per_page": 100})
        courses = [c for c in (parse_course(r) for r in raw) if c is not None]
        if not wanted.strip():
            return courses
        if found := matching(courses, wanted):
            return found
        chosen, _ = choose(courses, wanted, lambda c: c.name)
        if chosen is not None:
            return [chosen]
        if (known_as := await by_her_notes(courses, wanted, self.memory)) is not None:
            return [known_as]
        known = "; ".join(c.name for c in courses) or "none"
        raise CanvasError(f"No current Canvas course matches {wanted!r} (the user's courses: {known})")

    async def one_course(self, wanted: str) -> CourseGrade:
        courses = await self.courses(wanted)
        if len(courses) == 1:
            return courses[0]
        if not courses:
            raise CanvasError("Canvas lists no current courses the user is a student in")
        names = "; ".join(c.name for c in courses)
        raise CanvasError(f"Say which course: {wanted!r} could be any of {names}")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.api.get_one(self.session, path, params)

    async def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        return await self.api.get_all(self.session, path, {"per_page": 100, **(params or {})})

    async def file(self, ident: str, course_id: str = "") -> CanvasFile:
        """A file's details, through its course when known (which a course's own files may need)."""
        paths = [f"/api/v1/courses/{course_id}/files/{ident}"] if course_id else []
        problem: CanvasError | None = None
        for path in [*paths, f"/api/v1/files/{ident}"]:
            try:
                found = parse_file(await self.get(path))
            except CanvasError as exc:
                problem = exc
                continue
            if found is not None:
                return found
        raise problem or CanvasError("Canvas answered with something unexpected")

    async def files(self, idents: Sequence[str], course_id: str) -> tuple[list[CanvasFile], list[str]]:
        gate = asyncio.Semaphore(PARALLEL)

        async def one(ident: str) -> CanvasFile | str:
            async with gate:
                try:
                    return await self.file(ident, course_id)
                except CanvasError as exc:
                    return f"a linked file (id {ident}) couldn't be looked up: {exc}"

        found = await asyncio.gather(*(one(i) for i in idents[:MAX_FILES]))
        return [f for f in found if isinstance(f, CanvasFile)], [p for p in found if isinstance(p, str)]

    async def read(self, file: CanvasFile, limit: int, offset: int = 0) -> str:
        """The text of ``file``, clipped, or plainly why there's none."""
        if reason := not_readable(file.name, file.content_type):
            return reason
        if file.locked:
            return f"It can't be downloaded: {file.locked}."
        if file.size > MAX_FILE_BYTES:
            return f"It's {_size(file.size)}, too big to download and read here."
        try:
            data = await self.api.download(self.session, file.url, MAX_FILE_BYTES)
        except CanvasError as exc:
            return f"It couldn't be downloaded: {exc}."
        extracted = await asyncio.to_thread(extract_text, file.name, data, file.content_type)
        if not extracted.text:
            if "pictures" in extracted.note:  # a scanned PDF, or slides saved as images
                return f"{extracted.note} Look at its pages instead: file_id={file.ident} with page=1, 2, ..."
            return extracted.note
        more = f"call canvas_course_material with file_id={file.ident} and offset={{offset}} to read on"
        return clip(extracted.text, limit, offset, more)

    async def picture(self, file: CanvasFile, page: int) -> PagePicture | str:
        """Page ``page`` of the PDF ``file`` as a picture to look at, or plainly why not."""
        if not (file.name.casefold().endswith(".pdf") or "pdf" in file.content_type.casefold()):
            return f"{file.name} isn't a PDF: only a PDF's pages can be looked at as pictures."
        if file.locked:
            return f"It can't be downloaded: {file.locked}."
        if file.size > MAX_FILE_BYTES:
            return f"It's {_size(file.size)}, too big to download here."
        try:
            data = await self.api.download(self.session, file.url, MAX_FILE_BYTES)
            return await render_page(data, page)
        except (CanvasError, PageError) as exc:
            return f"Page {page} of {file.name} couldn't be shown: {exc}"


async def _connect(transport: httpx.AsyncBaseTransport | None) -> CanvasClient:
    token = canvas_token()
    if not token:
        raise CanvasError(setup_hint())
    return CanvasClient(canvas_url(), token, transport)


# ---------------------------------------------------------------------------------------------------- assignments


@dataclass(frozen=True)
class Work:
    """An assignment or quiz: Canvas's assignment (empty for an ungraded quiz) and its classic quiz, when known."""

    course: CourseGrade
    title: str
    assignment: dict[str, Any]
    quiz: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.course.ident}:{self.assignment.get('id') or 'quiz' + str(self.quiz.get('id'))}"

    @property
    def new_quiz(self) -> bool:
        tool = self.assignment.get("external_tool_tag_attributes")
        url = str(tool.get("url") or "") if isinstance(tool, dict) else ""
        return bool(self.assignment.get("is_quiz_lti_assignment")) or "quiz-lti" in url

    @property
    def kind(self) -> str:
        types = self.assignment.get("submission_types") or []
        if self.quiz or "online_quiz" in types or self.new_quiz:
            return "quiz"
        return "discussion" if "discussion_topic" in types else "assignment"

    @property
    def due(self) -> datetime | None:
        return _parse_time(self.assignment.get("due_at") or self.quiz.get("due_at"))

    def listing(self) -> str:
        due = f", due {_when(self.due)}" if self.due else ""
        return f"{self.title} ({self.kind} in {self.course.name}{due})"


async def gather_work(canvas: Canvas, courses: Sequence[CourseGrade]) -> tuple[list[Work], list[str]]:
    """Every assignment and quiz in ``courses``, and the courses that couldn't be read."""
    gate = asyncio.Semaphore(PARALLEL)

    async def one(course: CourseGrade) -> tuple[list[Work], str]:
        async with gate:
            try:
                assignments = await canvas.get_all(f"/api/v1/courses/{course.ident}/assignments")
            except CanvasError as exc:
                return [], f"{course.name}: {exc}"
            quizzes = await _quietly(canvas.get_all(f"/api/v1/courses/{course.ident}/quizzes"), [])
        by_id = {str(q["id"]): q for q in quizzes if isinstance(q, dict) and q.get("id") is not None}
        work: list[Work] = []
        for raw in assignments:
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            quiz = by_id.pop(str(raw.get("quiz_id")), {}) if raw.get("quiz_id") else {}
            work.append(Work(course, _text(raw.get("name")) or "Untitled", raw, quiz))
        for quiz in by_id.values():  # practice quizzes and surveys, which have no assignment
            work.append(Work(course, _text(quiz.get("title")) or "Untitled quiz", {}, quiz))
        return work, ""

    results = await asyncio.gather(*(one(c) for c in courses))
    return [w for found, _ in results for w in found], [p for _, p in results if p]


def describe_rubric(rubric: Any) -> str:
    lines = []
    total = 0.0
    for criterion in rubric if isinstance(rubric, list) else []:
        if not isinstance(criterion, dict):
            continue
        points = _number(criterion.get("points"))
        total += points or 0
        line = "- " + (_text(criterion.get("description")) or "Criterion")
        if points is not None:
            line += f" ({_plain(points)} points)"
        if detail := _text(criterion.get("long_description"))[:400]:
            line += f": {detail}"
        levels = []
        for rating in criterion.get("ratings") or []:
            if not isinstance(rating, dict):
                continue
            level = _text(rating.get("description")) or "level"
            if (value := _number(rating.get("points"))) is not None:
                level += f" {_plain(value)}"
            if more := _text(rating.get("long_description"))[:200]:
                level += f" ({more})"
            levels.append(level)
        if levels:
            line += ". Levels: " + "; ".join(levels)
        lines.append(line)
    if not lines:
        return ""
    return f"Rubric (criteria worth {_plain(total)} points in all):\n" + "\n".join(lines)


QUIZ_TYPES = {
    "practice_quiz": "a practice quiz (not graded)",
    "assignment": "graded",
    "graded_survey": "a graded survey",
    "survey": "an ungraded survey",
}


def _quiz_facts(quiz: dict[str, Any]) -> str:
    facts = []
    if kind := QUIZ_TYPES.get(str(quiz.get("quiz_type") or "")):
        facts.append(kind)
    limit = _number(quiz.get("time_limit"))
    facts.append(f"time limit {_plain(limit)} minutes" if limit else "no time limit")
    if (count := _number(quiz.get("question_count"))) is not None:
        facts.append(f"{_plain(count)} question{'' if count == 1 else 's'}")
    attempts = _number(quiz.get("allowed_attempts"))
    if attempts is not None:
        facts.append(
            "unlimited attempts" if attempts < 0 else f"{_plain(attempts)} attempt{'' if attempts == 1 else 's'}"
        )
    if quiz.get("one_question_at_a_time"):
        facts.append("one question at a time" + (", with no going back" if quiz.get("cant_go_back") else ""))
    if quiz.get("has_access_code"):
        facts.append("needs an access code from the instructor")
    return "Quiz: " + ", ".join(facts)


async def assignment_details(canvas: Canvas, work: Work, read_files: bool) -> tuple[str, dict[str, Any]]:
    task, quiz, course = work.assignment, dict(work.quiz), work.course
    if task.get("quiz_id") and not quiz:
        quiz = await _quietly(canvas.get(f"/api/v1/courses/{course.ident}/quizzes/{task['quiz_id']}"), {})
    lines = [f"{work.title} ({work.kind} in {course.name})"]
    lines.append(f"Due: {_when(work.due)}" if work.due else "Due: no due date set")
    if opens := _parse_time(task.get("unlock_at") or quiz.get("unlock_at")):
        lines.append(f"Opens: {_when(opens)}")
    if closes := _parse_time(task.get("lock_at") or quiz.get("lock_at")):
        lines.append(f"Closes (no submissions after): {_when(closes)}")
    points = _number(task.get("points_possible") if task else quiz.get("points_possible"))
    if points is not None:
        lines.append(f"Points possible: {_plain(points)}")
    if (grading := str(task.get("grading_type") or "")) and grading not in ("points", "percent"):
        lines.append(f"Graded as: {grading.replace('_', ' ')}")
    if types := [SUBMISSION_TYPES.get(t, str(t).replace("_", " ")) for t in task.get("submission_types") or []]:
        lines.append("Submitted as: " + ", ".join(types))
    if extensions := [str(e) for e in task.get("allowed_extensions") or []]:
        lines.append("Allowed file types: " + ", ".join(extensions))
    if (attempts := _number(task.get("allowed_attempts"))) and attempts > 0 and not quiz:
        lines.append(f"Submission attempts allowed: {_plain(attempts)}")
    if task.get("group_category_id"):
        lines.append("It's a group assignment.")
    if quiz:
        lines.append(_quiz_facts(quiz))
    elif work.new_quiz:
        lines.append("Quiz: it's a New Quizzes quiz, whose time limit and questions Canvas doesn't share here.")
    if task.get("locked_for_user") and (why := read_html(str(task.get("lock_explanation") or ""))[0]):
        lines.append(f"Locked for the user: {why}")
    if url := str(task.get("html_url") or quiz.get("html_url") or ""):
        lines.append(f"Link: {url}")

    topic = task.get("discussion_topic") if isinstance(task.get("discussion_topic"), dict) else {}
    html = str(task.get("description") or quiz.get("description") or topic.get("message") or "")
    text, links = read_html(html, canvas.base)
    instructions = clip(text, INSTRUCTION_CHARS) if text else "(none written in Canvas)"
    lines.append(f"\nInstructions:\n{instructions}")
    if rubric := describe_rubric(task.get("rubric")):
        lines.append(f"\n{rubric}")

    ids, pages = canvas_links(links, canvas.base)
    for attachment in topic.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("id") is not None and str(attachment["id"]) not in ids:
            ids.append(str(attachment["id"]))
    files, problems = await canvas.files(ids, course.ident)
    if files:
        lines.append("\nFiles attached or linked:\n" + "\n".join(f"- {describe_file(f)}" for f in files))
    if pages:
        lines.append("\nLinked Canvas pages: " + ", ".join(pages[:MAX_FILES]))
    if problems:
        lines.append("\n" + "; ".join(problems[:3]).capitalize() + ".")
    if read_files and files:
        budget = ATTACHMENT_CHARS
        for file in files:
            if budget < 1000:
                lines.append(f"\n--- {file.name} ---\n(Not read: this answer is full. Read it with its file id.)")
                continue
            content = await canvas.read(file, min(READ_CHARS, budget))
            budget -= len(content)
            lines.append(f"\n--- {file.name} ---\n{content}")
    elif files:
        lines.append("(To read a file's text, call canvas_course_material with its file id.)")
    data = {"course": course.name, "kind": work.kind, "files": len(files)}
    return "\n".join(lines), data


class AssignmentArgs(ToolArgs):
    name: str = Field(
        min_length=1,
        max_length=150,
        description="Name part.",
    )
    course: str = Field(
        default="",
        max_length=100,
        description="Name or code part, e.g. 'ITSC 2214'; omit for all.",
    )
    read_files: bool = Field(
        default=False,
        description=("Include attached files' text, to help understand or start it."),
    )


class CanvasAssignment(Tool):
    name = "canvas_assignment"
    description = (
        "Read one Canvas assignment or quiz (read-only): instructions, points, dates, rubric, quiz limits, files. "
        "Use for 'what do I have to do for homework 3'. It never submits anything."
    )
    Args = AssignmentArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 150.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: AssignmentArgs, ctx: ToolContext) -> str | None:
        return None if canvas_token() else setup_hint(ctx.config)

    async def run(self, args: AssignmentArgs, ctx: ToolContext) -> ToolResult:
        try:
            api = await _connect(self._transport)
            async with api.session() as session:
                canvas = Canvas(api, session, ctx.memory)
                courses = await canvas.courses(args.course)
                work, problems = await gather_work(canvas, courses)
                chosen, close = choose(work, args.name, lambda w: w.title, lambda w: w.key, lambda w: w.course.ident)
                if chosen is None:
                    return _no_single_match(args.name, close, problems, "assignment or quiz", Work.listing)
                text, data = await assignment_details(canvas, chosen, args.read_files)
        except CanvasError as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(text, data=data)


def _no_single_match(
    wanted: str, close: Sequence[T], problems: Sequence[str], what: str, listing: Callable[[T], str]
) -> ToolResult:
    unread = f" (couldn't read {'; '.join(problems[:3])})" if problems else ""
    if not close:
        return ToolResult.error(f"No {what} in Canvas matches {wanted!r}{unread}.")
    lines = "\n".join(f"- {listing(item)}" for item in close)
    return ToolResult.success(
        f"More than one {what} could be {wanted!r}, or none matches it exactly. The closest:\n{lines}\n"
        f"Ask the user which one they mean{unread}.",
        data={"matches": len(close)},
    )


# ---------------------------------------------------------------------------------------------------- materials


@dataclass(frozen=True)
class Material:
    """Something in a course to read: a file, a page, a module, or the syllabus."""

    kind: Literal["file", "page", "module", "syllabus"]
    title: str
    ident: str  # the file id, the page's url name, the module id
    where: str = ""  # the module it's in

    def listing(self) -> str:
        where = f", in {self.where}" if self.where else ""
        return f"{self.title} ({self.kind}{where})"


@dataclass
class CourseMaterials:
    items: list[Material]
    modules: dict[str, list[dict[str, Any]]]  # module id -> its items
    files: dict[str, CanvasFile]
    syllabus: str  # HTML


async def course_materials(canvas: Canvas, course: CourseGrade) -> CourseMaterials:
    """What the course shows the user: modules and their items, files, pages and the syllabus. A tab the course hides
    (files and pages often are) is simply left out; the modules usually still reach the same things."""
    base = f"/api/v1/courses/{course.ident}"
    modules, files, pages, info = await asyncio.gather(
        _quietly(canvas.get_all(f"{base}/modules", {"include[]": ["items"]}), []),
        _quietly(canvas.get_all(f"{base}/files"), []),
        _quietly(canvas.get_all(f"{base}/pages"), []),
        _quietly(canvas.get(base, {"include[]": ["syllabus_body"]}), {}),
    )
    found = CourseMaterials(items=[], modules={}, files={}, syllabus=str(info.get("syllabus_body") or ""))
    if read_html(found.syllabus)[0]:
        found.items.append(Material("syllabus", "Syllabus", course.ident))
    for module in modules:
        if not isinstance(module, dict) or module.get("id") is None:
            continue
        ident, name = str(module["id"]), _text(module.get("name")) or "Module"
        items = module.get("items")
        if not isinstance(items, list):  # Canvas leaves them out when there are many
            items = await _quietly(canvas.get_all(f"{base}/modules/{ident}/items"), [])
        found.modules[ident] = [i for i in items if isinstance(i, dict)]
        found.items.append(Material("module", name, ident))
        for item in found.modules[ident]:
            title = _text(item.get("title"))
            if item.get("type") == "File" and item.get("content_id") is not None:
                found.items.append(Material("file", title or "file", str(item["content_id"]), name))
            elif item.get("type") == "Page" and item.get("page_url"):
                found.items.append(Material("page", title or str(item["page_url"]), str(item["page_url"]), name))
    for raw in files:
        if (file := parse_file(raw)) is not None:
            found.files[file.ident] = file
            found.items.append(Material("file", file.name, file.ident))
    for page in pages:
        if isinstance(page, dict) and page.get("url"):
            found.items.append(Material("page", _text(page.get("title")) or str(page["url"]), str(page["url"])))
    return found


def outline(course: CourseGrade, materials: CourseMaterials) -> str:
    """The course's modules and what's in each, or its files and pages when it has no modules."""
    lines = []
    shown = 0
    names = {m.ident: m.title for m in materials.items if m.kind == "module"}
    for ident, items in materials.modules.items():
        parts = []
        for item in items:
            if item.get("type") == "SubHeader":
                continue
            shown += 1
            if shown > MAX_ITEMS:
                break
            kind = ITEM_KINDS.get(str(item.get("type")), "item")
            parts.append(f"{_text(item.get('title')) or 'untitled'} ({kind})")
        lines.append(f"- {names.get(ident, 'Module')}: " + ("; ".join(parts) if parts else "empty"))
        if shown > MAX_ITEMS:
            lines.append("...and more.")
            break
    if lines:
        head = f"Modules in {course.name}:"
    else:
        others = [m.listing() for m in materials.items if m.kind in ("file", "page", "syllabus")]
        lines = [f"- {line}" for line in others[:MAX_ITEMS]]
        if len(others) > MAX_ITEMS:
            lines.append(f"...and {len(others) - MAX_ITEMS} more.")
        head = f"{course.name} has no modules; its files and pages:" if lines else ""
    if not lines:
        return f"Canvas shows nothing to read in {course.name}."
    if materials.items and any(m.kind == "syllabus" for m in materials.items):
        lines.append("It also has a syllabus page.")
    return head + "\n" + "\n".join(lines)


async def read_material(
    canvas: Canvas, course: CourseGrade, materials: CourseMaterials, item: Material, offset: int
) -> str:
    if item.kind == "module":
        entries = []
        for entry in materials.modules.get(item.ident, [])[:MAX_ITEMS]:
            kind = str(entry.get("type"))
            title = _text(entry.get("title")) or "untitled"
            entries.append(f"  {title}:" if kind == "SubHeader" else f"- {title} ({ITEM_KINDS.get(kind, 'item')})")
        body = "\n".join(entries) or "It's empty."
        return f"Module {item.title!r} in {course.name}:\n{body}\n(Name a file or page to read it.)"
    if item.kind == "file":
        file = materials.files.get(item.ident) or await canvas.file(item.ident, course.ident)
        return f"{describe_file(file)} from {course.name}:\n{await canvas.read(file, READ_CHARS, offset)}"
    if item.kind == "syllabus":
        html = materials.syllabus
    else:
        page = await canvas.get(f"/api/v1/courses/{course.ident}/pages/{quote(item.ident, safe='')}")
        html = str(page.get("body") or "")
    text, links = read_html(html, canvas.base)
    more = "call canvas_course_material again with offset={offset} to read on"
    lines = [f"{item.title} ({'syllabus' if item.kind == 'syllabus' else 'page'} in {course.name}):"]
    lines.append(clip(text, READ_CHARS, offset, more) if text else "(The page is empty.)")
    ids, pages = canvas_links(links, canvas.base)
    files, _ = await canvas.files(ids, course.ident)
    if files:
        lines.append("\nFiles linked from it:\n" + "\n".join(f"- {describe_file(f)}" for f in files))
    if pages:
        lines.append("\nLinked pages: " + ", ".join(pages[:MAX_FILES]))
    return "\n".join(lines)


class MaterialArgs(ToolArgs):
    course: str = Field(
        default="",
        max_length=100,
        description="Name or code part. Needed unless file_id.",
    )
    name: str = Field(
        default="",
        max_length=150,
        description=("File, page or module name part, e.g. 'syllabus'. Omit to list modules."),
    )
    kind: Literal["any", "file", "page", "module"] = Field(default="any")
    file_id: str = Field(
        default="",
        max_length=20,
        pattern=r"^\d*$",
        description="From another canvas tool.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=10_000_000,
        description="From a cut-off answer, to read on.",
    )
    page: int = Field(
        default=0,
        ge=0,
        le=2000,
        description="A PDF page to look at as a picture: slides that are images, or diagrams.",
    )


class CanvasCourseMaterial(Tool):
    name = "canvas_course_material"
    description = "Read a Canvas course's files, pages (syllabus, notes) and modules as text (read-only)."
    Args = MaterialArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 150.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: MaterialArgs, ctx: ToolContext) -> str | None:
        if not canvas_token():
            return setup_hint(ctx.config)
        if not args.course and not args.file_id:
            return "Say which course to look in."
        return None

    async def run(self, args: MaterialArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        try:
            api = await _connect(self._transport)
            async with api.session() as session:
                canvas = Canvas(api, session, ctx.memory)
                if args.file_id:
                    course_id = (await canvas.one_course(args.course)).ident if args.course else ""
                    file = await canvas.file(args.file_id, course_id)
                    if args.page:
                        return _page_result(await canvas.picture(file, args.page), file)
                    text = f"{describe_file(file)}:\n{await canvas.read(file, READ_CHARS, args.offset)}"
                    return ToolResult.success(text, data={"kind": "file"})
                course = await canvas.one_course(args.course)
                materials = await course_materials(canvas, course)
                if not args.name:
                    return ToolResult.success(outline(course, materials), data={"modules": len(materials.modules)})
                wanted = [m for m in materials.items if args.kind == "any" or m.kind == args.kind]
                if args.kind == "page":
                    wanted += [m for m in materials.items if m.kind == "syllabus"]
                chosen, close = choose(wanted, args.name, lambda m: m.title, lambda m: (m.kind, m.ident))
                if chosen is None:
                    return _no_single_match(args.name, close, [], f"thing in {course.name}", Material.listing)
                if args.page:
                    if chosen.kind != "file":
                        return ToolResult.error(f"{chosen.title} is a {chosen.kind}: only a PDF has pages to look at.")
                    file = materials.files.get(chosen.ident) or await canvas.file(chosen.ident, course.ident)
                    return _page_result(await canvas.picture(file, args.page), file)
                text = await read_material(canvas, course, materials, chosen, args.offset)
        except CanvasError as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(text, data={"kind": chosen.kind, "course": course.name})


def _page_result(found: PagePicture | str, file: CanvasFile) -> ToolResult:
    if isinstance(found, str):
        return ToolResult.error(found)
    return ToolResult.success(
        f"Page {found.page} of {found.pages} of {file.name}, as a picture (attached). Explain from what's on it.",
        data={"kind": "page", "image": found.image, "page": found.page, "pages": found.pages},
    )


# ---------------------------------------------------------------------------------------------------- announcements


class AnnouncementsArgs(ToolArgs):
    course: str = Field(
        default="",
        max_length=100,
        description="Name or code part; omit for all.",
    )
    days: int = Field(default=14, ge=1, le=180)
    title: str = Field(
        default="",
        max_length=150,
        description="Title part, to read that one announcement in full.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=10_000_000,
        description="From a cut-off answer, to read on.",
    )


def _announcement(post: dict[str, Any], when: datetime, course: str, text: str) -> str:
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    by = f" (by {_text(author.get('display_name'))})" if author.get("display_name") else ""
    head = f"{_when(when)}, {course}: {_text(post.get('title')) or 'Announcement'}{by}"
    files = [parse_file(a) for a in post.get("attachments") or []]
    attached = [describe_file(f) for f in files if f is not None]
    return head + "\n" + (text or "(no text)") + ("\nAttached: " + "; ".join(attached) if attached else "")


class CanvasAnnouncements(Tool):
    name = "canvas_announcements"
    description = (
        "Instructors' Canvas announcements, newest first (read-only). Use for 'is class cancelled'. "
        "With title, reads that one announcement in full."
    )
    Args = AnnouncementsArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 90.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: AnnouncementsArgs, ctx: ToolContext) -> str | None:
        return None if canvas_token() else setup_hint(ctx.config)

    async def run(self, args: AnnouncementsArgs, ctx: ToolContext) -> ToolResult:
        now = datetime.now(UTC)
        try:
            api = await _connect(self._transport)
            async with api.session() as session:
                canvas = Canvas(api, session, ctx.memory)
                courses = await canvas.courses(args.course)
                if not courses:
                    return ToolResult.success("Canvas lists no current courses the user is a student in.")
                raw = await canvas.get_all(
                    "/api/v1/announcements",
                    {
                        "context_codes[]": [f"course_{c.ident}" for c in courses],
                        "start_date": (now - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "end_date": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                )
        except CanvasError as exc:
            return ToolResult.error(str(exc))
        names = {f"course_{c.ident}": c.name for c in courses}
        posts = []
        for post in raw:
            if isinstance(post, dict) and (when := _parse_time(post.get("posted_at") or post.get("delayed_post_at"))):
                posts.append((when, post))
        posts.sort(key=lambda pair: pair[0], reverse=True)
        span = "the last day" if args.days == 1 else f"the last {args.days} days"
        if not posts:
            return ToolResult.success(f"No announcements in {span}.", data={"count": 0})
        if args.title:
            chosen, close = choose(
                posts, args.title, lambda p: _text(p[1].get("title")), lambda p: str(p[1].get("id") or id(p[1]))
            )
            if chosen is None:

                def listing(pair: tuple[datetime, dict[str, Any]]) -> str:
                    return f"{_text(pair[1].get('title')) or 'Announcement'} ({_when(pair[0])})"

                return _no_single_match(args.title, close, [], f"announcement from {span}", listing)
            when, post = chosen
            text = read_html(str(post.get("message") or ""), api.base)[0]
            more = f"call canvas_announcements again with title={args.title!r} and offset={{offset}} to read on"
            body = clip(text, READ_CHARS, args.offset, more) if text else ""
            course = names.get(str(post.get("context_code")), "")
            return ToolResult.success(_announcement(post, when, course, body), data={"count": 1})
        # One course's or a few announcements come whole; across many courses each is shortened to keep it brief.
        full = len(courses) == 1 or len(posts) <= 3
        each = READ_CHARS if full else 700
        parts = []
        for when, post in posts[:15]:
            text = read_html(str(post.get("message") or ""), api.base)[0]
            if len(text) > each:
                title = _text(post.get("title")) or "Announcement"
                text = (
                    clip(text, each).split("\n[Showing")[0]
                    + f" [cut short; call canvas_announcements with title={title!r} to read all of it]"
                )
            parts.append(_announcement(post, when, names.get(str(post.get("context_code")), ""), text))
        if len(posts) > 15:
            parts.append(f"...and {len(posts) - 15} older ones.")
        return ToolResult.success(
            f"Announcements from {span}, newest first:\n\n" + "\n\n".join(parts), data={"count": len(posts)}
        )


TOOLS = [CanvasAssignment, CanvasCourseMaterial, CanvasAnnouncements]
