"""The study tracker: one JSON file (``data/study/study.json``) holding, for each course, its exams and the topics
each covers, how far the user has got with each topic, their weak spots, where the last session stopped, the
sessions themselves and the questions they missed.

Everything here is what the user and she worked out together while studying (or an exam Canvas lists), never a
guess about the user, so nothing in it needs the privacy rules her inferences do. It's small and written whole on
every change, through a temporary file (``core.jsonfile``), under one lock: the tools write it on the event loop and
the proactive gate reads it from the Futures worker thread. A file that's there but can't be read is never written
over: a passing lock (an antivirus scan) fails the change, to be tried again; one that isn't valid JSON is kept beside
it (``study.json.bad-<time>``) before a fresh one is started. A course that can't be understood is written back as it
was.

Missed questions come back on a spaced schedule (a Leitner box): a miss puts a question in box 0, due a few hours
later; each right answer moves it up a box, each one further out (``INTERVALS_D``); a right answer in the last box
retires it, and a wrong one sends it back to box 0.

Names are matched the way they're said ("AI" for "Intro to AI"), but never across numbers: "Midterm 2" is never
"Midterm 1", nor "Chapter 14" "Chapter 4", nor "Calculus II" "Calculus I". Forgetting something takes a closer match
than finding it (``loose=False``): no misheard name ever deletes the wrong course.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

from rapidfuzz import fuzz

from octavia.core.jsonfile import write_json

log = logging.getLogger(__name__)

DAY_S = 86400.0
# How long until a missed question comes back, by box: later today, then tomorrow, in three days, a week, two weeks.
INTERVALS_D = (0.25, 1.0, 3.0, 7.0, 14.0)
MAX_CARDS = 400  # per course; the oldest retired ones go first, then the oldest
MAX_SESSIONS = 60  # per course
MAX_TOPICS = 40  # per exam
MAX_LINKS = 120  # per course
TEXT_CHARS = 600
FUZZY = 88  # how close a misheard name must be to count as the same one
# A session nobody ended counts as over this long after it started: the user wandered off, or she restarted.
OPEN_SESSION_S = 4 * 3600.0
STUDYING_S = 2 * 3600.0  # how long after her tools were last on a course the user counts as still studying it

Status = Literal["todo", "started", "done"]
STATUSES: tuple[Status, ...] = ("todo", "started", "done")

T = TypeVar("T")


class TrackerUnavailable(OSError):
    """The tracker's file is there but couldn't be read just now; nothing was changed."""


def clean(text: str, limit: int = TEXT_CHARS) -> str:
    return " ".join(str(text).split())[:limit]


def name_key(name: str) -> str:
    """How names are compared: "Intro to AI " and "intro to ai" are the same course."""
    return re.sub(r"[^\w]+", " ", name.casefold()).strip()


_NUMBERS = re.compile(r"\d+|\b(?:i{1,3}|iv|vi{0,3}|ix|x)\b")


def _numbers(key: str) -> set[str]:
    return set(_NUMBERS.findall(key))


def match(
    items: Sequence[T], wanted: str, names: Callable[[T], Iterable[str]], *, loose: bool = True
) -> tuple[T | None, list[T]]:
    """The one item ``wanted`` names, and the items it could equally be when it names none for sure.

    The same name; else the only one whose name holds it as whole words ("alpha beta" in "Alpha-beta pruning");
    with ``loose``, else the only one whose words all appear in it, else the only one close enough to have been
    misheard. A name with a number only ever matches one with the same numbers."""
    key = name_key(wanted)
    if not key:
        return None, []
    nums = _numbers(key)

    def keys(item: T) -> list[str]:
        return [k for n in names(item) if (k := name_key(n)) and (not nums or _numbers(k) == nums)]

    stages: list[Callable[[str], bool]] = [
        lambda k: k == key,
        lambda k: re.search(rf"\b{re.escape(key)}\b", k) is not None,
    ]
    if loose:
        words = set(key.split())
        stages.append(lambda k: set(k.split()) <= words and not (_numbers(k) - nums))
    for stage in stages:
        hits = [item for item in items if any(stage(k) for k in keys(item))]
        if len(hits) == 1:
            return hits[0], []
        if hits:
            return None, hits
    if not loose:
        return None, []
    scored = sorted(
        ((max((fuzz.token_sort_ratio(key, k) for k in keys(item)), default=0), n) for n, item in enumerate(items)),
        reverse=True,
    )
    if scored and scored[0][0] >= FUZZY:
        if len(scored) == 1 or scored[1][0] < FUZZY:
            return items[scored[0][1]], []
        return None, [items[n] for score, n in scored if score >= FUZZY]
    return None, []


def best_match(items: Sequence[T], wanted: str, name: Callable[[T], str], *, loose: bool = True) -> T | None:
    """``match`` for items with one name each: the item, or None when there's none or more than one."""
    return match(items, wanted, lambda item: (name(item),), loose=loose)[0]


@dataclass
class Topic:
    name: str
    status: Status = "todo"
    weak: bool = False
    note: str = ""  # what's shaky, or what to remember about it
    updated: float = 0.0


@dataclass
class Exam:
    name: str
    day: str = ""  # ISO date, "" when not known
    topics: list[Topic] = field(default_factory=list)
    source: str = "you"  # "you" (said while studying) or "canvas" (found in what Canvas lists)
    added: float = 0.0
    canvas_course: str = ""  # the Canvas course it was found in
    at: float = 0.0  # when it starts or is due, when Canvas says (a timestamp), else 0
    taken: bool = False  # Canvas shows it done
    ruled_out: list[str] = field(default_factory=list)  # topics the user said aren't on it: never added back unasked

    @property
    def date(self) -> date | None:
        try:
            return date.fromisoformat(self.day) if self.day else None
        except ValueError:
            return None

    def over(self, now: float) -> bool:
        return self.taken or (self.at > 0 and now >= self.at)

    def progress(self) -> tuple[int, int, int]:
        """Topics done, started, and in all."""
        done = sum(t.status == "done" for t in self.topics)
        started = sum(t.status == "started" for t in self.topics)
        return done, started, len(self.topics)

    def percent(self) -> int | None:
        """How far through the topics, a started one counting half; None without topics."""
        done, started, total = self.progress()
        return round(100 * (done + started / 2) / total) if total else None


@dataclass
class Session:
    start: float
    end: float = 0.0  # 0 while it runs
    topics: list[str] = field(default_factory=list)
    summary: str = ""
    block_min: float = 0.0  # the study block it started with

    def minutes(self, now: float) -> float:
        return max(0.0, ((self.end or now) - self.start) / 60)


@dataclass
class Card:
    """A question the user missed, to ask again on a spaced schedule."""

    id: int
    question: str
    answer: str
    topic: str = ""
    box: int = 0
    due: float = 0.0
    misses: int = 1
    hits: int = 0
    added: float = 0.0
    last: float = 0.0
    retired: bool = False


@dataclass
class Link:
    """How two of a course's topics connect, for its concept map: ``a`` leads to ``b`` (``b`` builds on it), and
    why, in a few words ("adds a heuristic")."""

    a: str
    b: str
    why: str = ""


@dataclass
class Course:
    name: str
    exams: list[Exam] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    left_off: str = ""  # where the last session stopped, in the user's terms
    left_off_at: float = 0.0
    quiz_right: int = 0  # the running quiz's score, reset when a session starts
    quiz_asked: int = 0
    aliases: list[str] = field(default_factory=list)  # other names for it: Canvas's ("ITCS-6150-001 Intelligent ...")
    links: list[Link] = field(default_factory=list)  # how its topics connect (the concept map's arrows)

    def names(self) -> list[str]:
        return [self.name, *self.aliases]

    def next_exam(self, today: date, now: float | None = None) -> Exam | None:
        """The soonest exam still ahead (today counts until it's over), else the newest one without a date."""
        ahead = [
            e
            for e in self.exams
            if e.date is not None and e.date >= today and not (e.taken or (now is not None and e.over(now)))
        ]
        if ahead:
            return min(ahead, key=lambda e: (e.date or today, e.at))
        undated = [e for e in self.exams if e.date is None and not e.taken]
        return max(undated, key=lambda e: e.added) if undated else None

    def due_cards(self, now: float) -> list[Card]:
        return sorted((c for c in self.cards if not c.retired and c.due <= now), key=lambda c: (c.due, c.id))

    def last_session(self) -> Session | None:
        return max(self.sessions, key=lambda s: s.start) if self.sessions else None

    def running(self, now: float) -> Session | None:
        """The session still going: started, not ended, and not left open past ``OPEN_SESSION_S``."""
        return next((s for s in reversed(self.sessions) if not s.end and now - s.start < OPEN_SESSION_S), None)


def _known(cls: type, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("not an object")
    return {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}


def _topic(raw: Any) -> Topic:
    topic = Topic(**_known(Topic, raw))
    if topic.status not in STATUSES:
        topic.status = "done" if str(topic.status).casefold() == "done" else "todo"
    return topic


def _links(raw: Any) -> list[Link]:
    """The links that can be read; one that can't is dropped, never the course with it."""
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("a"), str) and isinstance(entry.get("b"), str):
            out.append(Link(entry["a"], entry["b"], str(entry.get("why") or "")))
    return out


def _course(raw: dict[str, Any]) -> Course:
    exams = []
    for e in raw.get("exams") or []:
        fields = {k: v for k, v in _known(Exam, e).items() if k not in ("topics", "ruled_out")}
        ruled_out = [str(n) for n in e.get("ruled_out") or []]
        exams.append(Exam(**fields, topics=[_topic(t) for t in e.get("topics") or []], ruled_out=ruled_out))
    return Course(
        name=str(raw.get("name") or "Course"),
        exams=exams,
        sessions=[Session(**_known(Session, s)) for s in raw.get("sessions") or []],
        cards=[Card(**_known(Card, c)) for c in raw.get("cards") or []],
        left_off=str(raw.get("left_off") or ""),
        left_off_at=float(raw.get("left_off_at") or 0.0),
        quiz_right=int(raw.get("quiz_right") or 0),
        quiz_asked=int(raw.get("quiz_asked") or 0),
        aliases=[str(a) for a in raw.get("aliases") or []],
        links=_links(raw.get("links")),
    )


def _copy(course: Course) -> Course:
    return _course(asdict(course))


class StudyStore:
    """The study tracker. Each change is saved at once; every method takes the one lock, so it's safe from any
    thread. ``clock`` is wall-clock seconds (tests pass their own)."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._courses: list[Course] | None = None
        self._kept: list[Any] = []  # courses in the file that couldn't be understood, written back as they were
        self._on: tuple[str, float] | None = None  # the course her tools last worked on, and when: not saved

    # ------------------------------------------------------------------------------------------------ reading

    def _load(self) -> list[Course]:
        if self._courses is not None:
            return self._courses
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._courses, self._kept = [], []
            return self._courses
        except OSError as exc:  # there, but locked or unreadable just now: try again next time, write nothing
            raise TrackerUnavailable(f"the study tracker couldn't be read just now ({exc.strerror or exc})") from exc
        try:
            raw = json.loads(text)
            entries = raw.get("courses") if isinstance(raw, dict) else None
            if not isinstance(entries, list):
                raise ValueError("no list of courses")
        except ValueError:
            backup = self.path.with_name(f"{self.path.name}.bad-{int(self.clock())}")
            try:
                shutil.copyfile(self.path, backup)
            except OSError as exc:
                raise TrackerUnavailable(f"the study tracker is damaged and couldn't be set aside ({exc})") from exc
            log.warning("The study tracker wasn't valid JSON; kept it as %s and started a new one", backup)
            entries = []
        courses, kept = [], []
        for entry in entries:
            try:
                courses.append(_course(entry))
            except (TypeError, ValueError, AttributeError):
                kept.append(entry)  # never lost: written back as it was
        if kept:
            log.warning("%d course(s) in the study tracker couldn't be read; they're kept as they are", len(kept))
        self._courses, self._kept = courses, kept
        return courses

    def _save(self) -> None:
        courses = self._load()
        data = {"version": 1, "courses": [asdict(c) for c in courses] + self._kept}
        write_json(self.path, data, indent=1)

    def courses(self) -> list[Course]:
        """A copy of every course, for reading. Empty while the file can't be read."""
        with self._lock:
            try:
                return [_copy(c) for c in self._load()]
            except TrackerUnavailable:
                log.debug("The study tracker couldn't be read", exc_info=True)
                return []

    def find(self, wanted: str) -> Course | None:
        with self._lock:
            return match(self.courses(), wanted, Course.names)[0]

    def on_course(self, course: str) -> None:
        """Her tools are working on this course with the user now (``studying``)."""
        with self._lock:
            found = match(self.courses(), course, Course.names)[0]
            self._on = (found.name, self.clock()) if found is not None else None

    def studying(self) -> str:
        """The course the user is studying right now, or "": a session that's running, else the one her tools were
        last on, within ``STUDYING_S``. No session has to have been started for it: mostly nobody does."""
        with self._lock:
            now = self.clock()
            running = next((c.name for c in self.courses() if c.running(now) is not None), "")
            if running:
                return running
            return self._on[0] if self._on is not None and now - self._on[1] < STUDYING_S else ""

    # ------------------------------------------------------------------------------------------------ changing

    def change(self, course: str, apply: Callable[[Course], T], *, create: bool = True) -> T:
        """Run ``apply`` on the course ``course`` names (made if it's new and ``create``) and save. Raises KeyError
        when there's no such course and ``create`` is False, ValueError when the name fits more than one, and
        TrackerUnavailable (an OSError, like a failed save) when the file can't be read just now."""
        with self._lock:
            courses = self._load()
            found, ties = match(courses, course, Course.names)
            if found is None:
                if ties:
                    raise ValueError(f"{course!r} could be more than one course: " + ", ".join(c.name for c in ties))
                if not create or not name_key(course):
                    raise KeyError(course)
                found = Course(name=clean(course, 80))
                courses.append(found)
            try:
                result = apply(found)
                self._tidy(found)
                self._save()
            except BaseException:
                self._courses = None  # what ``apply`` changed in memory goes; the file still has it as it was
                raise
            return result

    def _tidy(self, course: Course) -> None:
        now = self.clock()
        for session in course.sessions:  # left open (nobody said it ended): over when its block was
            if not session.end and now - session.start >= OPEN_SESSION_S:
                session.end = session.start + (session.block_min * 60 or OPEN_SESSION_S)
        if len(course.sessions) > MAX_SESSIONS:
            course.sessions = sorted(course.sessions, key=lambda s: s.start)[-MAX_SESSIONS:]
        if len(course.links) > MAX_LINKS:
            course.links = course.links[-MAX_LINKS:]
        if len(course.cards) > MAX_CARDS:
            course.cards.sort(key=lambda c: (not c.retired, c.added))
            course.cards = course.cards[len(course.cards) - MAX_CARDS :]

    def link(self, course: str, other_name: str) -> str:
        """Give ``course`` another name (Canvas's for it), folding in a course already tracked under that name (the
        exams Canvas listed before the two were linked). What happened, in a sentence."""
        with self._lock:
            courses = self._load()
            try:
                return self._link(courses, course, other_name)
            except BaseException:
                self._courses = None  # nothing half-done stays in memory: the file has it as it was
                raise

    def _link(self, courses: list[Course], course: str, other_name: str) -> str:
        target, ties = match(courses, course, Course.names)
        if ties:
            raise ValueError(f"{course!r} could be more than one course: " + ", ".join(c.name for c in ties))
        if target is None:
            target = Course(name=clean(course, 80))
            courses.append(target)
        other = match([c for c in courses if c is not target], other_name, Course.names, loose=False)[0]
        alias = clean(other_name, 120)
        if name_key(alias) not in {name_key(n) for n in target.names()}:
            target.aliases.append(alias)
        said = f"{target.name} is also {alias} now."
        if other is not None:
            for exam in other.exams:
                mine = best_match(target.exams, exam.name, lambda e: e.name)
                if mine is None:
                    target.exams.append(exam)
                elif not mine.day and exam.day:  # what was recorded keeps its topics, and gets Canvas's date
                    mine.day, mine.at, mine.taken = exam.day, exam.at, exam.taken
            target.sessions += other.sessions
            for link in other.links:
                add_link(target, link.a, link.b, link.why)
            start = max((c.id for c in target.cards), default=0)
            for n, card in enumerate(other.cards, 1):
                card.id = start + n
                target.cards.append(card)
            target.aliases += [a for a in other.names() if name_key(a) != name_key(alias)]
            courses.remove(other)
            said += f" What was tracked under {other.name} is part of it."
        self._tidy(target)
        self._save()
        return said

    def remove_course(self, course: str) -> str | None:
        """Forget a course entirely (only by a close match of its name); its name, or None when there's no such
        course. Raises ValueError when the name fits more than one."""
        with self._lock:
            courses = self._load()
            found, ties = match(courses, course, Course.names, loose=False)
            if ties:
                raise ValueError(f"{course!r} could be more than one course: " + ", ".join(c.name for c in ties))
            if found is None:
                return None
            courses.remove(found)
            try:
                self._save()
            except BaseException:
                self._courses = None
                raise
            return found.name


# ---------------------------------------------------------------------------------------------------- course edits
# Small functions over one Course, run inside ``StudyStore.change``.


def exam_for(course: Course, wanted: str, today: date, now: float, *, create: bool = True) -> Exam:
    """The exam ``wanted`` names, or the next one when it's empty; made when it's new (or there's none at all).
    Without ``create`` only a close match counts, and KeyError says there's none."""
    if wanted:
        found, ties = match(course.exams, wanted, lambda e: (e.name,), loose=create)
        if found is not None:
            return found
        if ties:
            raise ValueError(f"{wanted!r} could be more than one exam: " + ", ".join(e.name for e in ties))
    else:
        found = course.next_exam(today, now)
        if found is not None:
            return found
    if not create:
        raise KeyError(wanted or "next exam")
    exam = Exam(name=clean(wanted, 80) or "Next exam", added=now)
    course.exams.append(exam)
    return exam


def topic_for(exam: Exam, wanted: str, now: float, *, create: bool = True) -> Topic | None:
    """The topic ``wanted`` names; made when it's new and ``create`` (and the exam has room). Without ``create``
    only a close match counts."""
    found = best_match(exam.topics, wanted, lambda t: t.name, loose=create)
    if found is None and create and name_key(wanted) and len(exam.topics) < MAX_TOPICS:
        found = Topic(name=clean(wanted, 80), updated=now)
        exam.topics.append(found)
    return found


def _holds(name: str, wanted: str) -> bool:
    """Whether ``name`` holds ``wanted`` as whole words, numbers alike ("GPU Architecture (Module 10)" holds "GPU
    architecture"; "Chapter 14" doesn't hold "Chapter 4")."""
    return match([name], wanted, lambda n: (n,), loose=False)[0] is not None


def ruled_out(exam: Exam, wanted: str) -> bool:
    """Whether ``wanted`` is a topic the user said isn't on this exam, so reading the syllabus again doesn't bring
    it back. Strict on purpose: "architecture" alone isn't kept off for "GPU architecture"."""
    return any(_holds(wanted, name) for name in exam.ruled_out)


def rule_out(exam: Exam, name: str) -> None:
    """The user said ``name`` isn't on this exam: off its topics, and kept off."""
    exam.topics = [t for t in exam.topics if not _holds(t.name, name)]
    if not any(name_key(n) == name_key(name) for n in exam.ruled_out):
        exam.ruled_out.append(clean(name, 80))


def rule_in(exam: Exam, name: str) -> bool:
    """The user said ``name`` is on this exam after all; whether it had been ruled out."""
    kept = [n for n in exam.ruled_out if not (_holds(name, n) or _holds(n, name))]
    gone = len(kept) < len(exam.ruled_out)
    exam.ruled_out = kept
    return gone


def add_card(course: Course, question: str, answer: str, topic: str, now: float) -> Card:
    """A missed question, due again later today; the same question missed again goes back to box 0 instead."""
    key = name_key(question)
    for card in course.cards:
        if name_key(card.question) == key:
            card.box, card.due, card.retired = 0, now + INTERVALS_D[0] * DAY_S, False
            card.misses += 1
            card.last = now
            if answer:
                card.answer = clean(answer)
            return card
    card = Card(
        id=max((c.id for c in course.cards), default=0) + 1,
        question=clean(question),
        answer=clean(answer),
        topic=clean(topic, 80),
        due=now + INTERVALS_D[0] * DAY_S,
        added=now,
        last=now,
    )
    course.cards.append(card)
    return card


def add_link(course: Course, a: str, b: str, why: str = "") -> bool:
    """A link from topic ``a`` to ``b`` (``b`` builds on ``a``); one already there (either way round) gets the new
    ``why`` instead. Whether it's new."""
    a, b, why = clean(a, 80), clean(b, 80), clean(why, 80)
    ends = {name_key(a), name_key(b)}
    if len(ends) < 2 or "" in ends:
        raise ValueError(f"a link needs two different topics, not {a!r} and {b!r}")
    for link in course.links:
        if {name_key(link.a), name_key(link.b)} == ends:
            link.a, link.b = a, b
            link.why = why or link.why
            return False
    course.links.append(Link(a, b, why))
    return True


def remove_link(course: Course, a: str, b: str) -> bool:
    """The link between ``a`` and ``b`` (either way round) taken out; whether there was one."""
    ends = {name_key(a), name_key(b)}
    kept = [link for link in course.links if {name_key(link.a), name_key(link.b)} != ends]
    gone = len(kept) < len(course.links)
    course.links = kept
    return gone


def review(card: Card, right: bool, now: float) -> None:
    """A spaced review's answer: up a box (retired past the last), or back to box 0."""
    card.last = now
    if right:
        card.hits += 1
        if card.box >= len(INTERVALS_D) - 1:
            card.retired = True
            return
        card.box += 1
    else:
        card.misses += 1
        card.box = 0
    card.due = now + INTERVALS_D[card.box] * DAY_S


def same_question(a: str, b: str) -> bool:
    """Whether two wordings are the same question (a review asked in slightly different words)."""
    return fuzz.token_set_ratio(name_key(a), name_key(b)) >= 70


# ---------------------------------------------------------------------------------------------------- saying it


def days_until(exam: Exam, today: date) -> int | None:
    return (exam.date - today).days if exam.date is not None else None


def when_words(days: int | None, exam: Exam) -> str:
    if days is None:
        return "date not known"
    if days == 0:
        return "today" + (f" at {datetime.fromtimestamp(exam.at):%I:%M %p}".replace(" at 0", " at ") if exam.at else "")
    if days == 1:
        return "tomorrow"
    if days < 0:
        return f"was {-days} day{'s' if days != -1 else ''} ago"
    day = exam.date
    return f"in {days} days ({day:%A %B} {day.day})" if day is not None else f"in {days} days"


def describe_exam(exam: Exam, today: date) -> list[str]:
    lines = [f"{exam.name}: {when_words(days_until(exam, today), exam)}."]
    done, started, total = exam.progress()
    if total:
        lines.append(
            f"Topics: {done} of {total} done"
            + (f", {started} started" if started else "")
            + f" (about {exam.percent()}% through)."
        )
        for t in exam.topics:
            mark = {"done": "done", "started": "started"}.get(t.status, "not yet")
            extra = (", weak spot" if t.weak else "") + (f": {t.note}" if t.note else "")
            lines.append(f"- {t.name} ({mark}{extra})")
    else:
        lines.append("No topics recorded for it yet: find what it covers (its announcement, the syllabus) first.")
    if exam.ruled_out:
        lines.append("Not on it, the user said (leave out of plans and estimates): " + ", ".join(exam.ruled_out))
    return lines


def describe(course: Course, now: float, today: date, *, full: bool = True) -> str:
    lines = [course.name + (f" (also: {', '.join(course.aliases)})" if course.aliases else "")]
    exam = course.next_exam(today, now)
    if exam is not None:
        lines += describe_exam(exam, today)
    later = [e for e in course.exams if e is not exam and e.date is not None and e.date >= today and not e.over(now)]
    if later:
        lines.append("Later exams: " + "; ".join(f"{e.name} ({e.day})" for e in sorted(later, key=lambda e: e.day)))
    if course.left_off:
        when = datetime.fromtimestamp(course.left_off_at).strftime("%a %b %d") if course.left_off_at else ""
        lines.append(f"Left off{f' ({when})' if when else ''}: {course.left_off}")
    if full and (last := course.last_session()) is not None:
        running = course.running(now) is last
        span = "running now" if running else f"{round(last.minutes(now))} minutes"
        on = datetime.fromtimestamp(last.start).strftime("%a %b %d")
        lines.append(f"Last session: {on}, {span}" + (f". {last.summary}" if last.summary else "."))
    due = course.due_cards(now)
    waiting = sum(not c.retired for c in course.cards)
    if waiting:
        lines.append(f"Missed questions to review: {len(due)} due now, {waiting} in all.")
    return "\n".join(lines)


def card_lines(cards: Iterable[Card]) -> list[str]:
    return [f"- #{c.id}" + (f" [{c.topic}]" if c.topic else "") + f": {c.question} (answer: {c.answer})" for c in cards]
