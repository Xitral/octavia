"""Studying with her: the study tracker's tools (``octavia.study``, docs/study.md).

``study_status`` says where the user stands for a course's next exam (what it covers, what's done, weak spots, where
they left off, missed questions due), ``study_update`` records it as they go, ``quiz_record`` keeps each practice
question's result (a miss becomes a question to review later, a review moves it along its schedule),
``study_reviews`` lists the missed questions due again, and ``study_session`` starts and ends a session: the music
paused, her own remarks and the spoken alerts held, and a break called when the block is up.

All of it writes only her own tracker file, so it's safe without asking. Nothing here reads or touches Canvas: the
brain reads the course material with the Canvas tools and tells the tracker what it found.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from octavia.core.known_documents import STORE_PATH as KNOWN_DOCUMENTS_PATH
from octavia.core.known_documents import describe as describe_document
from octavia.core.known_documents import known_documents
from octavia.study.store import (
    MAX_TOPICS,
    Course,
    Session,
    StudyStore,
    Topic,
    TrackerUnavailable,
    add_card,
    card_lines,
    clean,
    describe,
    exam_for,
    review,
    rule_in,
    rule_out,
    ruled_out,
    same_question,
    topic_for,
)
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions import focus
from octavia.tools.extensions.timers import spoken_duration

log = logging.getLogger(__name__)

STORE_PATH = "data/study/study.json"
MAX_REVIEWS = 12  # questions listed for one review
_STORES: dict[Path, StudyStore] = {}


def study_store(ctx: ToolContext) -> StudyStore:
    """The tracker for this config's data folder (one per file, so every tool and the gate share its lock)."""
    store = ctx.extras.get("study")
    if isinstance(store, StudyStore):
        return store
    path = ctx.config.resolve_path(STORE_PATH)
    return _STORES.setdefault(path, StudyStore(path))


def studying(ctx: ToolContext) -> str:
    """The course the user is on just now (``StudyStore.studying``), or ""."""
    return study_store(ctx).studying()


def _today(store: StudyStore) -> date:
    return datetime.fromtimestamp(store.clock()).date()


def _split_note(item: str) -> tuple[str, str]:
    """ "minimax: forgets which level is MAX" → the topic and its note."""
    name, _, note = item.partition(":")
    return name.strip(), note.strip()


# ---------------------------------------------------------------------------------------------------- status


class StatusArgs(ToolArgs):
    course: str = Field(default="", max_length=100, description="Course name or code part; omit for all.")


class StudyStatus(Tool):
    name = "study_status"
    description = (
        "Study tracker: a course's next exam, its topics and how far along the user is, weak spots, where they left "
        "off, last session, missed questions due. Check it when they start studying or ask how far along they are."
    )
    Args = StatusArgs
    risk = RiskLevel.SAFE
    category = "school"

    async def run(self, args: StatusArgs, ctx: ToolContext) -> ToolResult:
        store = study_store(ctx)
        now, today = store.clock(), _today(store)
        if args.course:
            course = store.find(args.course)
            if course is None:
                names = ", ".join(c.name for c in store.courses()) or "none yet"
                return ToolResult.success(
                    f"Nothing tracked for {args.course!r} (tracked: {names}). Find what its next exam covers (its "
                    "announcement, syllabus or modules) and record it with study_update."
                )
            store.on_course(course.name)
            text = describe(course, now, today)
            read = known_documents(ctx.config.resolve_path(KNOWN_DOCUMENTS_PATH)).for_course(course.names())
            if read:  # whatever her context has forgotten: study_status is where a session picks up
                text += "\nDocuments you read for it (read_document with the path):\n"
                text += "\n".join(describe_document(d) for d in read)
            return ToolResult.success(text, data={"course": course.name})
        courses = store.courses()
        if not courses:
            return ToolResult.success("Nothing tracked yet. Record a course's next exam with study_update.")
        return ToolResult.success("\n\n".join(describe(c, now, today, full=False) for c in courses))


# ---------------------------------------------------------------------------------------------------- update


def _failed(exc: OSError) -> ToolResult:
    if isinstance(exc, TrackerUnavailable):
        return ToolResult.error(f"Nothing was changed: {exc}. Try again in a moment.")
    return ToolResult.error(f"The study tracker couldn't be saved ({exc.strerror or exc}).")


class UpdateArgs(ToolArgs):
    course: str = Field(
        min_length=1, max_length=100, description="Course name; use a tracked one's when it fits (study_status)."
    )
    exam: str = Field(default="", max_length=80, description="Exam name; omit for the next one.")
    exam_date: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$", description="YYYY-MM-DD.")
    covers: list[str] = Field(default_factory=list, max_length=30, description="Topics the exam covers, in order.")
    done: list[str] = Field(default_factory=list, max_length=30, description="Topics finished.")
    started: list[str] = Field(default_factory=list, max_length=30)
    todo: list[str] = Field(default_factory=list, max_length=30, description="Topics back to not started.")
    weak: list[str] = Field(default_factory=list, max_length=30, description="'topic: what's shaky'.")
    not_weak: list[str] = Field(default_factory=list, max_length=30)
    remove: list[str] = Field(
        default_factory=list, max_length=30, description="Topics the user says aren't on it: kept off for good."
    )
    restore: list[str] = Field(
        default_factory=list, max_length=30, description="Removed topics the user says are on it after all."
    )
    left_off: str = Field(default="", max_length=300, description="Where this session stopped.")
    canvas_name: str = Field(
        default="", max_length=120, description="Canvas's name for this course, to link it with what Canvas lists."
    )
    forget: Literal["", "exam", "course"] = Field(default="", description="Drop the exam, or the whole course.")


class StudyUpdate(Tool):
    name = "study_update"
    description = (
        "Record study progress for a course: its next exam's date and topics, topics done or started, weak spots, "
        "where you left off. Topics are matched loosely ('chapter 4')."
    )
    Args = UpdateArgs
    risk = RiskLevel.SAFE  # her own tracker file only
    category = "school"

    async def run(self, args: UpdateArgs, ctx: ToolContext) -> ToolResult:
        store = study_store(ctx)
        now, today = store.clock(), _today(store)
        try:
            if args.forget == "course":
                gone = store.remove_course(args.course)
                return ToolResult.success(f"Forgot {gone}." if gone else f"Nothing was tracked for {args.course!r}.")
            linked = store.link(args.course, args.canvas_name) if args.canvas_name and not args.forget else ""
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return _failed(exc)
        unmatched: list[str] = []
        full: list[str] = []
        kept_off: list[str] = []

        def apply(course: Course) -> str:
            if args.forget == "exam":
                exam = exam_for(course, args.exam, today, now, create=False)
                course.exams.remove(exam)
                return f"Dropped {exam.name} from {course.name}."
            exam = exam_for(course, args.exam, today, now)
            if args.exam_date:
                exam.day = args.exam_date
                if exam.at and datetime.fromtimestamp(exam.at).date().isoformat() != args.exam_date:
                    exam.at = 0.0  # Canvas's time was for the old day
            for name in args.restore:
                if not rule_in(exam, name):
                    unmatched.append(name)
                elif topic_for(exam, name, now) is None:
                    full.append(name)

            def topic(name: str) -> Topic | None:
                if ruled_out(exam, name):
                    kept_off.append(name)
                    return None
                found = topic_for(exam, name, now)
                if found is None:
                    full.append(name)
                return found

            for name in args.covers:
                topic(name)
            for wanted, status in ((args.done, "done"), (args.started, "started"), (args.todo, "todo")):
                for name in wanted:
                    if (found := topic(name)) is not None:
                        found.status, found.updated = status, now  # type: ignore[assignment]
            for item in args.weak:
                name, note = _split_note(item)
                if (found := topic(name)) is not None:
                    found.weak, found.updated = True, now
                    if note:
                        found.note = clean(note, 200)
            for name in args.not_weak:
                found = topic_for(exam, name, now, create=False)
                if found is None:
                    unmatched.append(name)
                else:
                    found.weak, found.note, found.updated = False, "", now
            for name in args.remove:
                found = topic_for(exam, name, now, create=False)
                rule_out(exam, name)
                if found is not None and not ruled_out(exam, found.name):  # a loose match ("GPU" for its topic)
                    rule_out(exam, found.name)
            if args.left_off:
                course.left_off, course.left_off_at = clean(args.left_off, 300), now
            return describe(course, now, today, full=False)

        try:
            text = store.change(args.course, apply, create=not args.forget)
            store.on_course(args.course)
        except KeyError:
            return ToolResult.error(f"Nothing tracked for {args.course!r}{' with that exam' if args.exam else ''}.")
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return _failed(exc)
        if unmatched:
            text += "\nNo such topic (or more than one fits), so left alone: " + ", ".join(unmatched)
        if full:
            text += f"\nNot added, the exam already has {MAX_TOPICS} topics: " + ", ".join(full)
        if kept_off:
            text += (
                "\nLeft off, the user said these aren't on this exam (restore puts one back if they say it is): "
                + ", ".join(kept_off)
            )
        return ToolResult.success("Saved." + (f" {linked}" if linked else "") + "\n" + text)


# ---------------------------------------------------------------------------------------------------- quizzes


class QuizArgs(ToolArgs):
    course: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=600, description="As you asked it.")
    answer: str = Field(default="", max_length=600, description="The right answer, briefly.")
    right: bool = Field(description="Whether the user got it right.")
    topic: str = Field(default="", max_length=80)
    review_id: int = Field(default=0, ge=0, description="The # of a missed question being reviewed.")


class QuizRecord(Tool):
    name = "quiz_record"
    description = (
        "Record one practice or review question's result while quizzing the user. A miss is saved to come back on a "
        "spaced schedule and marks its topic weak; returns the running score."
    )
    Args = QuizArgs
    risk = RiskLevel.SAFE  # her own tracker file only
    category = "school"

    async def run(self, args: QuizArgs, ctx: ToolContext) -> ToolResult:
        store = study_store(ctx)
        now, today = store.clock(), _today(store)

        def apply(course: Course) -> str:
            course.quiz_asked += 1
            course.quiz_right += args.right
            notes = []
            if args.review_id:
                card = next((c for c in course.cards if c.id == args.review_id), None)
                if card is not None and not same_question(card.question, args.question):
                    notes.append(f"#{args.review_id} in {course.name} is a different question, so it's left as is.")
                    card = None
                elif card is None:
                    notes.append(f"No missed question #{args.review_id} in {course.name}.")
                if card is not None:
                    review(card, args.right, now)
                    if card.retired:
                        notes.append(f"#{card.id} is learned: it won't come back.")
                    else:
                        notes.append(f"#{card.id} comes back in {_days((card.due - now) / 86400)}.")
                    return _score(course, notes)
                notes.append("Counted it as a new question.")
            if not args.right:
                card = add_card(course, args.question, args.answer, args.topic, now)
                notes.append(f"Saved as missed question #{card.id}, back later today.")
                exam = course.next_exam(today, now)
                topic = topic_for(exam, args.topic, now, create=False) if exam is not None and args.topic else None
                if topic is not None and not topic.weak:
                    topic.weak, topic.updated = True, now
                    notes.append(f"Marked {topic.name} as a weak spot.")
            return _score(course, notes)

        try:
            said = store.change(args.course, apply)
            store.on_course(args.course)
            return ToolResult.success(said)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return _failed(exc)


def _days(days: float) -> str:
    if days < 0.75:
        return "a few hours"
    whole = round(days)
    return "a day" if whole == 1 else f"{whole} days"


def _score(course: Course, notes: list[str]) -> str:
    return " ".join([*notes, f"Score this session: {course.quiz_right} of {course.quiz_asked}."])


class ReviewsArgs(ToolArgs):
    course: str = Field(default="", max_length=100, description="Omit for all courses.")


class StudyReviews(Tool):
    name = "study_reviews"
    description = (
        "Missed questions due for a spaced review, with their answers (keep the answers to yourself until the user "
        "has tried). Ask them one at a time and record each with quiz_record and its review_id."
    )
    Args = ReviewsArgs
    risk = RiskLevel.SAFE
    category = "school"

    async def run(self, args: ReviewsArgs, ctx: ToolContext) -> ToolResult:
        store = study_store(ctx)
        now = store.clock()
        if args.course:
            course = store.find(args.course)
            courses = [course] if course is not None else []
        else:
            courses = store.courses()
        parts, count = [], 0
        for course in courses:
            due = course.due_cards(now)[:MAX_REVIEWS]
            if due:
                count += len(due)
                parts.append(f"{course.name}:\n" + "\n".join(card_lines(due)))
        if not parts:
            ahead = [c.due for course in courses for c in course.cards if not c.retired]
            nxt = f" The next one is due in {_days((min(ahead) - now) / 86400)}." if ahead else ""
            return ToolResult.success("No missed questions are due for review." + nxt, data={"due": 0})
        return ToolResult.success("\n\n".join(parts), data={"due": count})


# ---------------------------------------------------------------------------------------------------- sessions

BREAK_NOTE = (
    "(Study session) The study block just ran out: you said so on your own. If the user wants a break, suggest about "
    "ten minutes; when they come back, pick up where they left off (study_status)."
)


class SessionArgs(ToolArgs):
    action: Literal["start", "end", "status"] = Field(default="start")
    course: str = Field(default="", max_length=100, description="Needed to start.")
    minutes: float = Field(default=45, ge=10, le=180, description="Block length before a break.")
    summary: str = Field(default="", max_length=400, description="On end: what was covered, briefly.")
    left_off: str = Field(default="", max_length=300, description="On end: where to pick up next time.")


class StudySession(Tool):
    name = "study_session"
    description = (
        "Start or end a study session. Start pauses the music and holds her own remarks and spoken alerts for a "
        "block, then she calls a break; end saves how long it ran, a summary and where you left off."
    )
    Args = SessionArgs
    risk = RiskLevel.SAFE
    category = "school"

    def __init__(self, keeper: focus.FocusKeeper | None = None, pause_media: Callable[[], Any] | None = None) -> None:
        self.keeper = keeper if keeper is not None else focus.KEEPER
        self._pause_media = pause_media or focus.pause_playing_media
        self._block: focus.Session | None = None  # the keeper's session this tool started, to end only that one
        self._break: Callable[..., None] | None = None  # what says the break is due (for tests)

    def _ours(self) -> bool:
        """Whether the keeper's session is the study block this tool started (not the user's own focus or snooze,
        and not one the user turned into focus mode since)."""
        session = self.keeper.session
        return session is not None and session is self._block and session.snooze

    async def run(self, args: SessionArgs, ctx: ToolContext) -> ToolResult:
        store = study_store(ctx)
        now = store.clock()
        if args.action == "status":
            running = [(c, s) for c in store.courses() if (s := c.running(now)) is not None]
            if not running:
                return ToolResult.success("No study session is running.", data={"on": False})
            course, session = running[0]
            left = self.keeper.remaining() if self._ours() else 0.0
            block = f" {spoken_duration(left)} left in this block." if left else ""
            return ToolResult.success(
                f"Studying {course.name} for {round(session.minutes(now))} minutes so far.{block}", data={"on": True}
            )
        if args.action == "end":
            return await self._end(store, args, now)
        if not args.course:
            return ToolResult.error("Say which course the session is for.")
        seconds = args.minutes * 60

        def begin(course: Course) -> tuple[str, bool]:
            already = course.running(now)
            if already is not None:
                return course.name, True
            course.sessions.append(Session(start=now, block_min=args.minutes))
            course.quiz_right = course.quiz_asked = 0
            return course.name, False

        try:
            self._end_others(store, args.course, now)
            name, already = store.change(args.course, begin)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return _failed(exc)
        media = await self._pause_media()
        announce = ctx.extras.get("announce")

        def call_break(text: str, note: str, listen: bool, **kwargs: Any) -> None:
            if callable(announce):
                announce(
                    f"That's {spoken_duration(seconds)} of {name}. Time for a short break?", BREAK_NOTE, True, **kwargs
                )

        self._break = call_break
        if self.keeper.session is not None and not self._ours():
            # The user's own focus session or snooze: it already holds the alerts and her remarks, and it stays
            # theirs (its length, its end and what it says at the end).
            held = "Your focus session already holds her remarks and the spoken alerts; it ends when you set it to."
        else:
            self._block, _ = await self.keeper.start(seconds, call_break, snooze=True)
            held = "Her own remarks and the spoken alerts wait until the block ends."
        lines = [
            f"{'Still studying' if already else 'Study session started for'} {name}: a {spoken_duration(seconds)} "
            "block, then you call a break.",
            "Paused " + ", ".join(media) + "." if media else "",
            held,
        ]
        return ToolResult.success(" ".join(line for line in lines if line), data={"on": True, "course": name})

    @staticmethod
    def _end_others(store: StudyStore, keep: str, now: float) -> None:
        """Another course's running session ends when one starts: one session at a time."""
        keep_course = store.find(keep)
        for course in store.courses():
            if course.running(now) is not None and (keep_course is None or course.name != keep_course.name):

                def stop(c: Course) -> None:
                    for s in c.sessions:
                        if not s.end:
                            s.end = now

                store.change(course.name, stop, create=False)

    async def _end(self, store: StudyStore, args: SessionArgs, now: float) -> ToolResult:
        running = [c for c in store.courses() if c.running(now) is not None]
        if args.course:
            wanted = store.find(args.course)
            running = [c for c in running if wanted is not None and c.name == wanted.name]
        if not running:
            return ToolResult.success("No study session was running.", data={"on": False})
        if self._ours():
            await self.keeper.end()
        self._block = None

        def finish(course: Course) -> str:
            session = course.running(now)
            if session is None:
                return ""
            session.end = now
            session.summary = clean(args.summary, 400)
            if args.left_off:
                course.left_off, course.left_off_at = clean(args.left_off, 300), now
            score = f" Quiz score: {course.quiz_right} of {course.quiz_asked}." if course.quiz_asked else ""
            return f"Saved a {round(session.minutes(now))}-minute session on {course.name}.{score}"

        try:
            text = " ".join(store.change(c.name, finish, create=False) for c in running)
        except OSError as exc:
            return _failed(exc)
        if not args.left_off:
            text += " Nothing saved about where you left off: pass left_off so the next session can pick up there."
        return ToolResult.success(text, data={"on": False})


TOOLS = [StudyStatus, StudyUpdate, QuizRecord, StudyReviews, StudySession]
