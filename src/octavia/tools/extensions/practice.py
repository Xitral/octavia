"""Practice problems graded by code (``octavia.study.practice``), for any kind a subject registers: one set at random or
typed in from the slides, graded exactly, and walked through one step at a time on a pop-up panel that updates in
place.

``practice_problem`` sets one and shows it, without the answer; ``check_practice`` grades the user's answer and shows
the step where it went wrong; ``walk_through`` shows the solution step by step, each call one step on (or the step
asked for). None of them knows any one kind: the kind does the work. All three only draw on the screen and write her
own files, so they never ask.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, ValidationError

from octavia.study import sheet_panel
from octavia.study.practice import Marked, PracticeKind, Problem, ProblemBook, get_kind, kinds
from octavia.study.store import add_card, describe
from octavia.study.worksheet import WorksheetServer
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.study import study_store
from octavia.visuals.host import PanelHost, VisualsError
from octavia.visuals.spec import Visual

BOOK_PATH = "data/study/problems.json"
PANEL = "practice"
_BOOKS: dict[str, ProblemBook] = {}


def problem_book(ctx: ToolContext) -> ProblemBook:
    book = ctx.extras.get("problems")
    if isinstance(book, ProblemBook):
        return book
    path = ctx.config.resolve_path(BOOK_PATH)
    return _BOOKS.setdefault(str(path), ProblemBook(path))


def _catalogue(what: str) -> str:
    """Every kind and its ``what`` hint ("answer_hint" or "given_hint"), for the tools' descriptions."""
    lines = []
    for name, kind in sorted(kinds().items()):
        hint = getattr(kind, what)
        lines.append(f"{name} ({kind.topic}){': ' + hint if hint else ''}")
    return "; ".join(lines)


def _problem(ctx: ToolContext, ident: int) -> tuple[Problem, PracticeKind] | str:
    problem = problem_book(ctx).get(ident)
    if problem is None:
        return "There's no such practice problem: set one with practice_problem first."
    kind = get_kind(problem.kind)
    if kind is None:
        return f"Problem {problem.id} is a kind ({problem.kind}) that isn't available any more."
    return problem, kind


async def show(ctx: ToolContext, kind: PracticeKind, problem: Problem, at: int | None, note: str = "") -> str:
    """The problem's picture at stage ``at`` (and ``note``) on the practice panel; what to tell her about it (""
    when it's up)."""
    svg = kind.picture(problem.data, at)
    host = ctx.extras.get("visuals")
    if not isinstance(host, PanelHost) or not ctx.config.visuals.enabled:
        return " No panel could show it (pop-up panels are off or not running here), so describe it out loud."
    blocks: list[dict[str, Any]] = []
    if svg:
        blocks.append({"kind": "svg", "svg": svg, "alt": kind.describe(problem.data)[:200]})
    if note:
        blocks.append({"kind": "text", "markdown": note[:7900]})
    if not blocks:
        return ""
    try:
        visual = Visual.model_validate({"title": kind.title(problem), "blocks": blocks, "width": "wide"})
        shown = await host.show(visual, panel_id=PANEL, stay_s=0)
    except VisualsError as exc:
        return f" The panel couldn't show it ({exc}), so describe it out loud."
    except ValueError as exc:
        return f" The panel couldn't draw it ({str(exc)[:200]}), so describe it out loud."
    return f" (The panel says: {'; '.join(shown.notes)}.)" if shown.notes else ""


def _problems(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'it'}: {e['msg']}" for e in exc.errors()[:4])


# ---------------------------------------------------------------------------------------------------- setting one


class ProblemArgs(ToolArgs):
    kind: str = Field(min_length=1, max_length=40, description="The kind of problem; see the description.")
    given: dict[str, Any] | None = Field(
        default=None, description="Their own problem (slides or a practice set); omit for a made-up one."
    )
    size: Literal["exam", "big"] = Field(default="exam", description="big: long, like an activity sheet.")


class PracticeProblem(Tool):
    name = "practice_problem"
    description = (
        "Set a practice problem graded by code, made up or typed in from their slides or a practice set (never graded "
        "homework), and show it on a "
        "panel; the answer is kept back (grade with check_practice, explain with walk_through). Kinds, with what "
        "`given` takes: " + _catalogue("given_hint") + "."
    )
    Args = ProblemArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 40.0

    async def run(self, args: ProblemArgs, ctx: ToolContext) -> ToolResult:
        kind = get_kind(args.kind)
        if kind is None:
            return ToolResult.error(
                f"There's no {args.kind!r} kind of problem. The kinds: {', '.join(sorted(kinds()))}."
            )
        data: dict[str, Any] | None = None
        if args.given:
            try:
                data = kind.parse(kind.Given.model_validate(args.given))
            except ValidationError as exc:
                return ToolResult.error(f"That {kind.topic} problem can't be read: {_problems(exc)}.")
            except ValueError as exc:
                return ToolResult.error(f"That problem can't be set: {exc}.")
        book = problem_book(ctx)
        now = book.clock()
        source = "from the user" if data is not None else "made up"
        made = data if data is not None else kind.make(random.Random(), args.size)
        try:
            problem = book.add(lambda ident: Problem(ident, kind.name, now, made, source))
        except OSError as exc:
            return ToolResult.error(f"The problem couldn't be saved ({exc.strerror or exc}).")
        shown = await show(ctx, kind, problem, None, f"**Your answer:** {kind.ask(problem.data)}")
        text = (
            f"Problem {problem.id} ({kind.topic}, {source}). {kind.statement(problem.data)}{shown}\n"
            "The answer isn't given to you: wait for theirs, then grade it with check_practice. Don't work it out "
            "yourself, and don't hint at it unless they ask for a hint."
        )
        return ToolResult.success(text, data={"problem": problem.id, "kind": kind.name})


# ---------------------------------------------------------------------------------------------------- grading


class CheckArgs(ToolArgs):
    problem: int = Field(default=0, ge=0, description="Problem number; 0 for the latest.")
    answer: dict[str, Any] = Field(default_factory=dict, description="Their answer; see the description.")
    course: str = Field(default="", max_length=100, description="To record the result in the study tracker.")


class CheckPractice(Tool):
    name = "check_practice"
    description = (
        "Grade the user's answer to a practice_problem exactly, by code; on a mistake, says the first step that "
        "went wrong and why, and shows that step on the panel. Use its verdict, never your own working. The answer "
        "by kind: " + _catalogue("answer_hint") + "."
    )
    Args = CheckArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 40.0

    async def run(self, args: CheckArgs, ctx: ToolContext) -> ToolResult:
        found = _problem(ctx, args.problem)
        if isinstance(found, str):
            return ToolResult.error(found)
        problem, kind = found
        try:
            answer = kind.Answer.model_validate(args.answer)
        except ValidationError as exc:
            return ToolResult.error(f"That answer can't be read: {_problems(exc)}. Send {kind.answer_hint}.")
        grade = kind.grade(problem.data, answer)
        shown = ""
        if grade.first_wrong is not None:
            steps = kind.steps(problem.data)
            if 0 <= grade.first_wrong < len(steps):
                problem_book(ctx).walked[problem.id] = grade.first_wrong
                note = "**Where it went wrong:** " + grade.lines[-1][:600]
                shown = await show(ctx, kind, problem, steps[grade.first_wrong].at, note)
        verdict = "Right." if grade.right else "Not quite."
        text = f"Problem {problem.id}: {verdict} " + " ".join(grade.lines) + shown
        if args.course:
            text += _record(ctx, args.course, kind, problem, grade.right, grade.lines)
        return ToolResult.success(text, data={"problem": problem.id, "right": grade.right})


def _record(ctx: ToolContext, course: str, kind: PracticeKind, problem: Problem, right: bool, lines: list[str]) -> str:
    """The result in the study tracker: the score, and a miss as a question to come back to."""
    store = study_store(ctx)
    now = store.clock()

    def apply(c: Any) -> None:
        c.quiz_asked += 1
        c.quiz_right += right
        if not right:
            question = f"{kind.topic} practice problem {problem.id} (walk_through it again)"
            add_card(c, question, " ".join(lines)[:500], kind.topic, now)

    try:
        store.change(course, apply)
    except (ValueError, OSError) as exc:
        return f" (Not recorded in the study tracker: {exc}.)"
    return " Recorded in the study tracker."


# ---------------------------------------------------------------------------------------------------- walking through


class WalkArgs(ToolArgs):
    problem: int = Field(default=0, ge=0, description="Problem number; 0 for the latest.")
    step: int = Field(default=0, ge=0, le=500, description="Step to show (from 1); 0 for the next one.")
    to_end: bool = Field(default=False, description="Show the finished solution.")


class WalkThrough(Tool):
    name = "walk_through"
    description = (
        "Walk through a practice_problem's solution one step at a time on the panel (for a game tree, values rising "
        "and α/β with pruned branches; for a search, the frontier and each expansion). Each call is one step: say "
        "it, then ask a question before the next."
    )
    Args = WalkArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 40.0

    async def run(self, args: WalkArgs, ctx: ToolContext) -> ToolResult:
        found = _problem(ctx, args.problem)
        if isinstance(found, str):
            return ToolResult.error(found)
        problem, kind = found
        book = problem_book(ctx)
        steps = kind.steps(problem.data)
        total = len(steps)
        if not total:
            return ToolResult.success(kind.summary(problem.data), data={"step": 0, "of": 0})
        if args.to_end:
            at = total - 1
        elif args.step:
            at = args.step - 1
        else:
            at = book.walked.get(problem.id, -1) + 1
        if at >= total:
            return ToolResult.success(
                f"That was the last step ({total} in all). {kind.summary(problem.data)}",
                data={"step": total, "of": total},
            )
        book.walked[problem.id] = at
        done = at == total - 1
        words = steps[at].words
        note = f"**Step {at + 1} of {total}.** {words}" + (f"\n\n{kind.summary(problem.data)}" if done else "")
        shown = await show(ctx, kind, problem, steps[at].at, note)
        tail = (
            f" That's the last step. {kind.summary(problem.data)}"
            if done
            else " Say it, then check they follow before the next step."
        )
        return ToolResult.success(f"Step {at + 1} of {total}: {words}{tail}{shown}", data={"step": at + 1, "of": total})


# ---------------------------------------------------------------------------------------------------- worksheets

_SERVERS: dict[str, WorksheetServer] = {}
_PANELS: dict[int, sheet_panel.SheetPanel] = {}
_REACTING: dict[tuple[str, int], asyncio.Future[Any]] = {}  # each sheet's turn waiting for her
log = logging.getLogger(__name__)
GRADED_NOTE = (
    "(Worksheet) The user just pressed Check my work on practice problem {id} ({topic}) in their browser, and it "
    "was graded by code: {lines} Their page shows each box marked. You said a one-line verdict on your own; if they "
    "ask, explain the first mistake, or walk_through from that step (step={step})."
)
# A checked sheet is a turn of her own (``ctx.extras["background"]``): she reacts in her own words and picks what's
# next, instead of a stock line. The grade is code's; the rest is what she needs to choose well.
CHECKED_TURN = "(Worksheet checked) Problem {id}, {topic}: {verdict}"
CHECKED_NOTE = (
    "the user just pressed Check my work on the {where} worksheet for practice problem {id} ({topic}, {source}); "
    "code graded every box, so never regrade it or work it out yourself: {lines} {marks}. {history} {study}"
    "React to it out loud in your own voice and personality, briefly (a sentence or two, not a stock 'nice'), "
    "then choose the next move yourself from all this: a harder one (practice_problem size big, then "
    "practice_worksheet: it takes this sheet's place), a weaker or untouched topic from their tracker before the "
    "exam, a quick 'why does that work' question, a break if they've been at it a while, or closing it "
    '(close_visual id "worksheet") when they\'re done. {after}'
)
_MISSED_AFTER = (
    "Some boxes aren't right: don't give the answers unasked; point them at the first slip, let them fix it and "
    "check again, or offer walk_through from step {step}."
)
_RIGHT_AFTER = "Ask before replacing or closing the sheet only if it's unclear they're finished with it."


def _minutes(seconds: float) -> str:
    minutes = round(seconds / 60)
    return "under a minute" if minutes < 1 else f"{minutes} minute{'s' if minutes != 1 else ''}"


def _box_count(marked: Marked) -> str:
    marks = list(marked.marks.values())
    parts = [f"{marks.count('right')} right"] + [f"{marks.count(m)} {m}" for m in ("wrong", "missed", "extra")
                                                   if marks.count(m)]  # fmt: skip
    return "Boxes: " + ", ".join(parts)


def _history(book: ProblemBook, problem: Problem, now: float) -> str:
    """This sheet's checks so far (the last is the one just made) and how long it's been up."""
    checks = book.checks.get(problem.id, [])
    opened = book.opened.get(problem.id, problem.created)
    lines = []
    if opened and now >= opened:
        lines.append(f"The sheet's been up {_minutes(now - opened)}.")
    if len(checks) > 1:
        earlier = "; ".join("right" if right else verdict[:120] for _, right, verdict in checks[:-1][-4:])
        lines.append(f"Check {len(checks)} on this sheet; before: {earlier}.")
    else:
        lines.append("Their first check on this sheet.")
    return " ".join(lines)


def _study_context(ctx: ToolContext) -> str:
    """The course they're studying (a running session's, else the soonest exam's) from the tracker, or ""."""
    try:
        store = study_store(ctx)
        now = store.clock()
        today = datetime.fromtimestamp(now).date()
        courses = store.courses()
        course = next((c for c in courses if c.running(now) is not None), None)
        if course is None:
            ahead = [(e.date, c) for c in courses if (e := c.next_exam(today, now)) is not None and e.date is not None]
            course = min(ahead, key=lambda pair: pair[0])[1] if ahead else None
        if course is None:
            return ""
        return "Their study tracker: " + describe(course, now, today, full=False)[:1500].replace("\n", " | ") + ". "
    except Exception:
        log.warning("Couldn't read the study tracker for the worksheet", exc_info=True)
        return ""


def _announcer(ctx: ToolContext, where: str = "browser") -> Callable[..., None]:
    def said(problem: Problem, kind: PracticeKind, marked: Marked | None, shown: bool = True) -> None:
        announce = ctx.extras.get("announce")
        if marked is None:
            if callable(announce):
                announce(
                    f"I couldn't grade that sheet for problem {problem.id}, sorry. Try Check my work again.",
                    None,
                    False,
                )
            return
        grade = marked.grade
        book = problem_book(ctx)
        now = book.clock()
        book.checks.setdefault(problem.id, []).append((now, grade.right, grade.lines[0] if grade.lines else ""))
        if grade.first_wrong is not None:  # "walk me through it" starts where it went wrong
            book.walked[problem.id] = grade.first_wrong
        step = (grade.first_wrong or 0) + 1

        def canned() -> None:  # the plain verdict, when she can't take it as a turn of her own
            if not callable(announce):
                return
            where_marks = (
                "is marked on your sheet"
                if shown
                else "is in what I'll tell you; the marks didn't make it onto the sheet"
            )
            if grade.right:
                text = f"That's all right on problem {problem.id}. Nice."
            else:
                text = f"Problem {problem.id}: {grade.lines[0]} The first slip {where_marks}."
            note = GRADED_NOTE.format(id=problem.id, topic=kind.topic, lines=" ".join(grade.lines)[:900], step=step)
            announce(text, note, False)

        background = ctx.extras.get("background")
        if not callable(background):
            canned()
            return
        marks = _box_count(marked) + ("" if shown else " (the marks didn't make it onto their sheet: tell them)")
        note = CHECKED_NOTE.format(
            where=where,
            id=problem.id,
            topic=kind.topic,
            source=problem.source,
            lines=" ".join(grade.lines)[:900],
            marks=marks,
            history=_history(book, problem, now),
            study=_study_context(ctx),
            after=_RIGHT_AFTER if grade.right else _MISSED_AFTER.format(step=step),
        )
        verdict = "all right." if grade.right else "not all right yet."
        try:
            turn = background(CHECKED_TURN.format(id=problem.id, topic=kind.topic, verdict=verdict), note)
        except Exception:
            log.warning("Couldn't give her the checked worksheet as a turn", exc_info=True)
            canned()
            return

        def failed(done: asyncio.Future[Any]) -> None:  # her turn never ran (the voice loop's down) or broke
            if done.cancelled() or done.exception() is None:
                return
            log.warning("Her turn on the checked worksheet failed: %s", done.exception())
            canned()

        if isinstance(turn, asyncio.Future):
            turn.add_done_callback(failed)
            key = (str(book.path), problem.id)
            older = _REACTING.get(key)
            if older is not None and not older.done():
                older.cancel()  # checked again before she got to it: one reaction, to the latest (it has the rest)
            _REACTING[key] = turn

    return said


def open_page(url: str) -> None:
    os.startfile(url)  # type: ignore[attr-defined]  # the user's own browser


class WorksheetArgs(ToolArgs):
    problem: int = Field(default=0, ge=0, description="Problem number; 0 for the latest.")
    to_print: bool = Field(default=False, description="Open it in the browser to print (blank or filled).")


class PracticeWorksheet(Tool):
    name = "practice_worksheet"
    description = (
        "Open a practice_problem as a worksheet on one of your panels, to work on like an exam: boxes on the tree "
        "or graph for each value, α and β or expansion number, branches to click to cross out, and 'Check my work' "
        "grades every box by code and marks it in place. to_print opens it in their browser to print instead. Use "
        "it when they want to write on it."
    )
    Args = WorksheetArgs
    risk = RiskLevel.SAFE  # a page on this PC only, opened in their browser
    category = "school"
    timeout_s = 20.0

    def __init__(self, opener: Callable[[str], Any] | None = None) -> None:
        self._opener = opener or open_page
        self._windows_only = opener is None

    async def run(self, args: WorksheetArgs, ctx: ToolContext) -> ToolResult:
        found = _problem(ctx, args.problem)
        if isinstance(found, str):
            return ToolResult.error(found)
        problem, kind = found
        if kind.worksheet(problem.data) is None:
            return ToolResult.error(f"{kind.topic} problems don't have a worksheet yet; use the panel instead.")
        host = ctx.extras.get("visuals")
        if (
            not args.to_print
            and isinstance(host, PanelHost)
            and ctx.config.visuals.enabled
            and sheet_panel.supported(host)
        ):
            panel = _PANELS.get(id(host))
            if panel is None:
                panel = _PANELS[id(host)] = sheet_panel.SheetPanel(host)
            panel.verdict = _announcer(ctx, "panel")
            try:
                trouble = await panel.open(problem, kind)
            except (VisualsError, ValueError) as exc:
                trouble = f" The panel couldn't show it ({str(exc)[:200]})."
            if not trouble:
                problem_book(ctx).opened[problem.id] = problem_book(ctx).clock()
                try:  # the sheet replaces the problem's read-only panel: one panel to work on, not two
                    await host.close(PANEL)
                except VisualsError as exc:
                    log.info("The problem's panel didn't close under the worksheet: %s", exc)
                return ToolResult.success(
                    f"Problem {problem.id} ({kind.topic}) is up as a worksheet on your panel: they write on it and "
                    "press Check my work, which marks every box in place, and the code's verdict comes to you as a "
                    "turn of your own to react to and pick what's next. Don't give the answers meanwhile.",
                    data={"problem": problem.id, "where": "panel"},
                )
            log.info("Worksheet panel unavailable, using the browser:%s", trouble)
        if self._windows_only and sys.platform != "win32":
            return ToolResult.error("Worksheets open in the browser on Windows only.")
        book = problem_book(ctx)
        server = _SERVERS.get(str(book.path))
        if server is None:
            server = _SERVERS[str(book.path)] = WorksheetServer(book)
        server.on_graded = _announcer(ctx)
        try:
            await server.start()
            await asyncio.to_thread(self._opener, server.url(problem.id))
            book.opened[problem.id] = book.clock()
        except OSError as exc:
            return ToolResult.error(f"The worksheet couldn't be opened ({exc.strerror or exc}).")
        how = (
            "to print: the page has Print blank and Print with my answers"
            if args.to_print
            else "they write on it and press Check my work, and you'll hear the verdict. Don't give the answers "
            "meanwhile"
        )
        return ToolResult.success(
            f"Opened problem {problem.id} ({kind.topic}) as a worksheet in their browser, {how}.",
            data={"problem": problem.id, "where": "browser"},  # not the address: its token stays out of transcripts
        )


TOOLS = [PracticeProblem, CheckPractice, WalkThrough, PracticeWorksheet]
