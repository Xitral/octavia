"""What the study tracker brings up on its own, through Futures' proactive gate (``RemarkSource``) like everything
else she says unasked, so quiet hours, focus mode, Do Not Disturb, the daily budgets and the user's mutes all apply.

Two kinds, each at most once a day for the same thing, and none while a study session runs:

* ``study_exam``: an exam within ``study.exam_days`` days with no session on its course in the last ``RECENT_H`` hours
  ("Your Midterm for Intro to AI is tomorrow, and you're about 40% through its topics. Want to study now?"). The
  user asked her to push for prep, since they rarely study for tests on their own.
* ``study_review``: at least ``study.min_reviews`` missed questions due again, only while the course has a dated exam
  within ``REVIEW_DAYS`` days that isn't over ("Your Final for Intro to AI is in 5 days, and there are five questions
  you missed that could use a quick review..."). Once the exam is past, the user found reviews offered unasked
  (and called "due") a nag, so they wait until asked. The course's exams go along as facts for the unprompted
  gate (``core/unprompted.py``) and her own words.

Both only ask: a yes goes to the brain as an ordinary turn with a note on what was offered; a no is kept so the
same exam or course isn't brought up again that day.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING

from octavia.study.store import Course, StudyStore, days_until, when_words

if TYPE_CHECKING:
    from octavia.config import StudyConfig
    from octavia.futures.proactive import Candidate

log = logging.getLogger(__name__)

PREFIX = "study_"
EXAM = PREFIX + "exam"
REVIEW = PREFIX + "review"
RECENT_H = 36.0
REVIEW_DAYS = 14  # reviews are offered unasked only with a dated exam this close and not over yet
ANSWER_S = 15 * 60.0  # an ask that waits at the gate longer than this is stale


class StudyNudges:
    """Candidates for the gate from the study tracker. Called from the Futures worker thread: everything is quick
    and goes through the store's lock."""

    def __init__(
        self,
        store: StudyStore,
        settings: Callable[[], StudyConfig],
        *,
        today: Callable[[float], date] = lambda now: datetime.fromtimestamp(now).date(),
    ) -> None:
        self.store = store
        self.settings = settings  # read at each look, so a change in her settings applies at once
        self.today = today
        self._lock = threading.Lock()
        self._said: set[str] = set()  # keys said or turned down (they hold the day, so this only grows a little)

    def candidates(self, now: float) -> list[Candidate]:
        settings = self.settings()
        if not settings.nudges:
            return []
        try:
            courses = self.store.courses()
            if any(c.running(now) is not None for c in courses):
                return []  # studying already
            day = self.today(now)
            for course in courses:
                if (found := self._exam(course, now, day, settings.exam_days)) is not None:
                    return [found]
            for course in courses:
                if (found := self._review(course, now, day, settings.min_reviews)) is not None:
                    return [found]
        except Exception:
            log.warning("Couldn't check the study tracker for something to bring up", exc_info=True)
        return []

    def shared(self, key: str) -> None:
        with self._lock:
            self._said.add(key)

    def declined(self, key: str) -> None:
        with self._lock:
            self._said.add(key)

    def offer_tool(self, key: str) -> str | None:
        """A yes goes to the brain, which starts with the tracker (read-only)."""
        return "study_status" if key.startswith(PREFIX) else None

    def _fresh(self, key: str) -> bool:
        with self._lock:
            return key not in self._said

    def _exam(self, course: Course, now: float, day: date, within: int) -> Candidate | None:
        exam = course.next_exam(day, now)
        days = days_until(exam, day) if exam is not None else None
        if exam is None or days is None or not 0 <= days <= within:
            return None
        last = course.last_session()
        if last is not None and now - last.start < RECENT_H * 3600:
            return None
        key = f"{EXAM}:{course.name}:{exam.name}@{day.isoformat()}"
        if not self._fresh(key):
            return None
        percent = exam.percent()
        if percent is None:
            where = "and we haven't mapped out what it covers yet"
        elif percent >= 100:
            where = "and you've been through every topic once"
        else:
            where = f"and you're about {percent}% through its topics"
        weak = [t.name for t in exam.topics if t.weak]
        text = (
            f"Your {exam.name} for {course.name} is {when_words(days, exam)}, {where}. Want to do a study session now?"
        )
        note = (
            f"(Study) You just asked, unprompted, because {exam.name} in {course.name} is {when_words(days, exam)} "
            "and there's been no study session on it lately. On a yes: study_status for the course, then "
            "study_session to start, then tutor them through the next topic not done"
            + (f" (weak spots: {', '.join(weak)})" if weak else "")
            + ". On a no or not now, just say okay."
        )
        urgency = 0.9 if days <= 1 else 0.7
        return self._candidate(EXAM, key, text, note, urgency, now)

    def _review(self, course: Course, now: float, day: date, least: int) -> Candidate | None:
        exam = course.next_exam(day, now)
        days = days_until(exam, day) if exam is not None else None
        if exam is None or days is None or not 0 <= days <= REVIEW_DAYS:
            return None  # with no exam coming up, missed questions wait until the user asks for them
        due = course.due_cards(now)
        if len(due) < least:
            return None
        key = f"{REVIEW}:{course.name}@{day.isoformat()}"
        if not self._fresh(key):
            return None
        text = (
            f"Your {exam.name} for {course.name} is {when_words(days, exam)}, and there are {len(due)} questions "
            "you missed that could use a quick review. Want to run through them? It's a couple of minutes."
        )
        note = (
            f"(Study) You just offered, unprompted, a spaced review of {len(due)} missed questions in {course.name} "
            f"ahead of {exam.name}. {exam_facts(course, now, day)} On a yes: study_reviews for the course, ask them one "
            "at a time, wait for each answer, then say right or wrong with a short why and record it with quiz_record "
            "(review_id). On a no, just say okay."
        )
        return self._candidate(REVIEW, key, text, note, 0.5, now)

    @staticmethod
    def _candidate(kind: str, key: str, text: str, note: str, urgency: float, now: float) -> Candidate:
        from octavia.futures.explain import Trace
        from octavia.futures.proactive import Candidate
        from octavia.futures.schema import Domain, Policy

        return Candidate(
            kind=kind,
            key=key,
            text=text,
            policy=Policy.ASK,
            urgency=urgency,
            relevance=0.8,
            domain=Domain.SCHOOL.value,
            sensitivity="personal",
            note=note,
            trace=Trace(
                observations=({"event": "", "what": "the study tracker", "at": now},),
                assumptions=("the user asked her to push for exam prep",),
                confidence="strong",
            ).to_dict(),
            expires_at=now + ANSWER_S,
            listen=True,
        )


def exam_facts(course: Course, now: float, day: date) -> str:
    """The course's exams as facts (when the last one was, what's next), so whatever weighs a nudge (the unprompted
    gate's judge, her brain) can tell a review before an exam from one after it."""
    past = [e for e in course.exams if e.date is not None and (e.date < day or e.taken or e.over(now))]
    ahead = course.next_exam(day, now)
    parts = []
    if past:
        last = max(past, key=lambda e: (e.date or day, e.at))
        parts.append(f"Its last exam, {last.name}, was on {last.date:%A, %B} {last.date.day}.")
    if ahead is not None and ahead.date is not None:
        parts.append(f"Its next exam, {ahead.name}, is {when_words(days_until(ahead, day), ahead)}.")
    else:
        parts.append("No exam ahead is on record.")
    return " ".join(parts)


def connect_proactive(futures: object, nudges: StudyNudges) -> bool:
    """Hand the gate the study tracker's asks (``FuturesService.add_source``). False when this Futures has none."""
    add_source = getattr(futures, "add_source", None)
    if add_source is None:
        return False
    from octavia.futures.engine import RemarkSource

    add_source(
        RemarkSource(
            prefix=PREFIX,
            candidates=nudges.candidates,
            shared=nudges.shared,
            declined=nudges.declined,
            offer_tool=nudges.offer_tool,
        )
    )
    return True
