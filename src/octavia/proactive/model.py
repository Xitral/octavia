"""Her picture of the user's week, rebuilt from what she already keeps each time it's needed.

Nothing here is a new memory. Each part is read from its own home when the model is built (``build``): work due in
Canvas, exams and weak topics in the study tracker, TickTick tasks, the calendar, the user's projects and the
objectives on them, the notes she keeps about the user (only the ones about priorities, school, work and how they
like things done), the hours they're usually around (from her own records), the hunches about them that Futures has
tested against what happened and that held (its hypotheses: correlations in her records, never causes), and how they
answered her overnight work before. That last part is the user's "curiosity loop": hunches formed and tested while
they're away, and only the ones that hold brought into what she plans. Every fact keeps where it came from (``Fact.source`` and ``ref``), so the planner's reasons can be traced.

A source that can't be read is named in ``unread`` and left out; nothing fails because one of them is down. The last
model built is kept in ``model.json`` for ``my_picture_of_you`` and the report, overwritten each time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SOURCE_TIMEOUT_S = 60.0
MAX_NOTES = 40
NOTE_CHARS = 240
HABIT_DAYS = 28
_CLIP = 160

# Notes worth having in the picture: what matters to them now, school, their work and company, how they like things.
_NOTE_WORDS = re.compile(
    r"\b(?:priorit\w*|focus\w*|goal\w*|important|deadline\w*|want\w* to|plan\w* to|working on|"
    r"class\w*|course\w*|major|semester|exam\w*|midterm\w*|final\w*|professor|ta\b|"
    r"arden|meridian|compan\w*|business|client\w*|startup|launch\w*|product\w*|"
    r"prefer\w*|likes?|hates?|dislikes?|always|never|don'?t|usually|every (?:day|morning|night|week))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Fact:
    area: str  # deadline | exam | meeting | task | project | note | habit | decision
    text: str
    source: str  # canvas | study | ticktick | calendar | projects | objectives | memory | records | decisions
    ref: str = ""  # the record it came from ("canvas:assignment:123", "memory:42")


@dataclass
class Deadline:
    key: str  # Canvas's stable key ("assignment:123")
    title: str
    course: str
    kind: str  # assignment | quiz | discussion
    due: float  # epoch seconds
    url: str = ""


@dataclass
class ExamAhead:
    course: str
    name: str
    day: str  # ISO date
    percent: int | None  # how far through its topics
    weak: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)
    missed: int = 0  # questions missed and due for review


@dataclass
class Meeting:
    title: str
    start: float
    end: float
    private: bool = False  # about health, money, relationships or work: not named, not prepared for


@dataclass
class TaskAhead:
    id: str
    title: str
    due: float
    priority: int | None = None


@dataclass
class ProjectState:
    name: str
    path: str
    last_commit: float | None = None  # epoch seconds
    open_objective: str = ""  # the open objective on it, if any


@dataclass
class UserModel:
    built_at: float
    user: str = "the user"
    deadlines: list[Deadline] = field(default_factory=list)
    exams: list[ExamAhead] = field(default_factory=list)
    meetings: list[Meeting] = field(default_factory=list)
    tasks: list[TaskAhead] = field(default_factory=list)
    projects: list[ProjectState] = field(default_factory=list)
    notes: list[Fact] = field(default_factory=list)
    habits: list[Fact] = field(default_factory=list)
    hunches: list[Fact] = field(default_factory=list)
    decisions: list[Fact] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)  # sources that couldn't be read this time

    def facts(self) -> list[Fact]:
        """Everything, as traceable lines."""
        out = [
            Fact("deadline", f"{d.title} ({d.kind}{', ' + d.course if d.course else ''}) due {_when(d.due)}", "canvas",
                 f"canvas:{d.key}")
            for d in self.deadlines
        ]  # fmt: skip
        for e in self.exams:
            how = f"{e.percent}% through its topics" if e.percent is not None else "no topics tracked"
            weak = f"; weak: {', '.join(e.weak[:5])}" if e.weak else ""
            todo = f"; not started: {', '.join(e.untouched[:5])}" if e.untouched else ""
            out.append(Fact("exam", f"{e.course} {e.name} on {e.day} ({how}{weak}{todo})", "study",
                            f"exam:{e.course}:{e.name}"))  # fmt: skip
        out += [
            Fact("meeting", f"{'a private event' if m.private else m.title} at {_when(m.start)}", "calendar",
                 f"meeting:{m.title}:{int(m.start)}")
            for m in self.meetings
        ]  # fmt: skip
        out += [Fact("task", f"{t.title} due {_when(t.due)}", "ticktick", f"task:{t.id}") for t in self.tasks]
        for p in self.projects:
            last = f"last commit {_when(p.last_commit)}" if p.last_commit else "no commits"
            busy = f"; objective under way: {p.open_objective}" if p.open_objective else ""
            out.append(Fact("project", f"{p.name} ({last}{busy})", "projects", f"project:{p.name}"))
        return [*out, *self.notes, *self.habits, *self.hunches, *self.decisions]

    def text(self, limit: int = 6000) -> str:
        """For the planner's prompt: one line per fact, grouped, with where it came from."""
        lines = [f"What you know of {self.user}'s week ({_when(self.built_at)}):"]
        area = ""
        for fact in self.facts():
            if fact.area != area:
                area = fact.area
                lines.append(f"\n{_AREA_TITLES.get(area, area)}:")
            lines.append(f"- {fact.text} [{fact.source}]")
        if self.unread:
            lines.append(f"\n(Couldn't read: {', '.join(self.unread)}.)")
        out = "\n".join(lines)
        return out if len(out) <= limit else out[: limit - 20].rsplit("\n", 1)[0] + "\n(more left out)"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> UserModel:
        def rows(name: str, kind: type) -> list[Any]:
            names = set(kind.__dataclass_fields__)
            return [kind(**{k: v for k, v in r.items() if k in names}) for r in data.get(name) or []]

        return cls(
            built_at=float(data.get("built_at") or 0),
            user=str(data.get("user") or "the user"),
            deadlines=rows("deadlines", Deadline),
            exams=rows("exams", ExamAhead),
            meetings=rows("meetings", Meeting),
            tasks=rows("tasks", TaskAhead),
            projects=rows("projects", ProjectState),
            notes=rows("notes", Fact),
            habits=rows("habits", Fact),
            hunches=rows("hunches", Fact),
            decisions=rows("decisions", Fact),
            unread=[str(u) for u in data.get("unread") or []],
        )


_AREA_TITLES = {
    "deadline": "Due in Canvas",
    "exam": "Exams (study tracker)",
    "meeting": "Calendar",
    "task": "TickTick",
    "project": "Projects",
    "note": "What they've told you (your notes)",
    "habit": "Their hours",
    "hunch": "Hunches that held when tested (correlations in your records, not causes)",
    "decision": "How they answered your overnight work before",
}


def _when(ts: float | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%a %b %d %H:%M")


# ---------------------------------------------------------------------------------------------------- the sources


@dataclass
class Sources:
    """Where each part comes from; any may be None (not connected, switched off, or kept out of what she learns)."""

    canvas: Callable[[datetime, datetime], Awaitable[Sequence[Any]]] | None = None  # CanvasItem
    study: Callable[[], Sequence[Any]] | None = None  # Course (the tracker's)
    ticktick: Callable[[], Awaitable[Sequence[Any]]] | None = None  # DueTask
    calendar: Callable[[date], Awaitable[Sequence[Any] | None]] | None = None  # CalendarItem, one day
    memories: Callable[[], Awaitable[Sequence[Any]]] | None = None  # MemoryItem
    projects: Callable[[], Sequence[Any]] | None = None  # objectives.projects.Project
    objectives: Callable[[], Sequence[Any]] | None = None  # Objective
    routing_log: Path | None = None
    hunches: Callable[[], Awaitable[Sequence[Fact]]] | None = None  # Futures' tested hunches (``hunch_facts``)
    decisions: Callable[[], Sequence[Fact]] | None = None


async def build(
    sources: Sources,
    *,
    user: str = "the user",
    look_ahead_days: int = 4,
    now: float | None = None,
    timeout_s: float = SOURCE_TIMEOUT_S,
) -> UserModel:
    """The model as of ``now``, from every source that answers."""
    now = time.time() if now is None else now
    model = UserModel(built_at=now, user=user)
    horizon = now + look_ahead_days * 86400

    async def read(name: str, call: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await asyncio.wait_for(call(), timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("Proactive: couldn't read %s (%s)", name, type(exc).__name__)
            model.unread.append(name)
            return None

    async def thread(fn: Callable[[], Any]) -> Any:
        return await asyncio.to_thread(fn)

    today = datetime.fromtimestamp(now).date()
    canvas_task = read("Canvas", lambda: _canvas(sources.canvas, now, horizon)) if sources.canvas else _none()
    calendar_task = read("the calendar", lambda: _calendar(sources.calendar, today)) if sources.calendar else _none()
    ticktick_task = read("TickTick", sources.ticktick) if sources.ticktick else _none()
    memories_task = read("your notes", sources.memories) if sources.memories else _none()
    study_task = read("the study tracker", lambda: thread(sources.study)) if sources.study else _none()
    projects_task = (
        read("your projects", lambda: thread(lambda: [(p, last_commit(p.path)) for p in sources.projects()]))
        if sources.projects
        else _none()
    )
    habits_task = (
        read("her records", lambda: thread(lambda: habits(sources.routing_log, now)))
        if sources.routing_log
        else _none()
    )
    hunches_task = read("Futures' hunches", sources.hunches) if sources.hunches else _none()
    canvas, calendar, tasks, memories, courses, projects, hours, hunches = await asyncio.gather(
        canvas_task, calendar_task, ticktick_task, memories_task, study_task, projects_task, habits_task, hunches_task
    )
    model.deadlines = _deadlines(canvas or [], now, horizon)
    model.meetings = _meetings(calendar or [])
    model.tasks = _tasks(tasks or [], now, horizon)
    model.notes = notes(memories or [])
    model.exams = _exams(courses or [], today, now, look_ahead_days)
    open_objectives = list(sources.objectives()) if sources.objectives else []
    model.projects = _projects(projects or [], open_objectives)
    model.habits = hours or []
    model.hunches = list(hunches or [])
    if sources.decisions is not None:
        try:
            model.decisions = list(sources.decisions())
        except Exception:
            model.unread.append("your past decisions")
    return model


async def _none() -> None:
    return None


async def _canvas(fetch: Any, now: float, horizon: float) -> Sequence[Any]:
    start = datetime.fromtimestamp(now).astimezone()
    return await fetch(start - timedelta(hours=12), datetime.fromtimestamp(horizon).astimezone())


async def _calendar(fetch: Any, today: date) -> list[Any]:
    """Today's rest and tomorrow: what's worth preparing for tonight."""
    days = await asyncio.gather(fetch(today), fetch(today + timedelta(days=1)))
    return [item for day in days if day for item in day]


def _deadlines(items: Sequence[Any], now: float, horizon: float) -> list[Deadline]:
    out = []
    for item in items:
        if getattr(item, "done", False):
            continue
        due = item.due.timestamp()
        if now <= due <= horizon:
            out.append(Deadline(item.key, _clip(item.title), _clip(item.course or ""), item.kind, due, item.url or ""))
    return sorted(out, key=lambda d: d.due)


def _meetings(items: Sequence[Any]) -> list[Meeting]:
    out, seen = [], set()
    for item in items:
        if getattr(item, "all_day", False):
            continue
        start, end = item.start.timestamp(), item.end.timestamp()
        if (item.title, start) in seen:
            continue
        seen.add((item.title, start))
        out.append(Meeting(_clip(item.title), start, end, bool(getattr(item, "private", False))))
    return sorted(out, key=lambda m: m.start)


def _tasks(items: Sequence[Any], now: float, horizon: float) -> list[TaskAhead]:
    out = []
    for item in items:
        due = item.due.timestamp()
        if due <= horizon:
            out.append(TaskAhead(str(item.id), _clip(item.title), due, item.priority))
    return sorted(out, key=lambda t: t.due)


def notes(memories: Sequence[Any]) -> list[Fact]:
    """The notes about what matters to them now, school, their work and how they like things: newest first."""
    from octavia.futures.privacy import looks_secret

    kept = []
    for item in sorted(memories, key=lambda m: -float(getattr(m, "updated_at", 0) or 0)):
        text = " ".join(str(item.text).split())
        if getattr(item, "superseded_by", None) or not _NOTE_WORDS.search(text) or looks_secret(text):
            continue
        kept.append(Fact("note", text[:NOTE_CHARS], "memory", f"memory:{item.id}"))
        if len(kept) >= MAX_NOTES:
            break
    return kept


def _exams(courses: Sequence[Any], today: date, now: float, look_ahead_days: int) -> list[ExamAhead]:
    out = []
    for course in courses:
        exam = course.next_exam(today, now)
        if exam is None or exam.date is None or (exam.date - today).days > look_ahead_days:
            continue
        out.append(
            ExamAhead(
                course=course.name,
                name=exam.name,
                day=exam.day,
                percent=exam.percent(),
                weak=[t.name for t in exam.topics if t.weak],
                untouched=[t.name for t in exam.topics if t.status == "todo" and not t.weak],
                missed=len(course.due_cards(now)),
            )
        )
    return sorted(out, key=lambda e: e.day)


def _projects(projects: Sequence[tuple[Any, float | None]], objectives: Sequence[Any]) -> list[ProjectState]:
    busy = {o.project: o.title for o in objectives if getattr(o, "open", False) and o.project}
    return [ProjectState(p.name, str(p.path), last, busy.get(p.name, "")) for p, last in projects]


def last_commit(path: Path | str) -> float | None:
    """When the project's checked-out branch last had a commit (epoch seconds), or None."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(path), "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def habits(path: Path | None, now: float, days: int = HABIT_DAYS) -> list[Fact]:
    """When the user is usually around, from her routing log: the median first and last of their turns a day."""
    if path is None or not path.exists():
        return []
    since = now - days * 86400
    first: dict[date, float] = {}
    last: dict[date, float] = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
                ts = float(row.get("ts") or 0)
            except (ValueError, TypeError, AttributeError):
                continue
            if ts < since or row.get("source") not in ("voice", "text", "window", "discord"):
                continue
            moment = datetime.fromtimestamp(ts)
            # A day runs 05:00 to 05:00: a 2 am turn ends the evening before.
            day = (moment - timedelta(hours=5)).date()
            hour = (moment - datetime.combine(day, datetime.min.time())).total_seconds() / 3600
            first[day] = min(first.get(day, 99.0), hour)
            last[day] = max(last.get(day, -1.0), hour)
    if len(first) < 3:
        return []

    def clock(hours: float) -> str:
        hours %= 24
        return f"{int(hours):02d}:{int(round((hours % 1) * 60)) % 60:02d}"

    start = statistics.median(first.values())
    end = statistics.median(last.values())
    return [
        Fact("habit", f"usually first talks to you around {clock(start)} and last around {clock(end)} "
             f"(over {len(first)} days)", "records", "routing_log")
    ]  # fmt: skip


# Futures' metrics about the user rather than about her (her clicks, replies and builds are hers to improve).
HUNCH_METRICS = frozenset({"task_on_time", "recommendation"})
HELD = ("observed", "supported")
MAX_HUNCHES = 12


def hunch_facts(hypotheses: Sequence[Any], allowed: Callable[[str], bool]) -> list[Fact]:
    """Futures' hunches about the user that held when tested against what happened (observed or supported, never
    speculative, weakened or rejected), from domains the user lets her learn about and bring up; each said as the
    correlation it is."""
    out = []
    for h in hypotheses:
        if h.status not in HELD or h.metric not in HUNCH_METRICS or not allowed(h.domain):
            continue
        claim = "tested on purpose" if h.claim == "causal" else "a correlation, not a cause"
        out.append(Fact("hunch", f"{_clip(h.statement)} ({h.status}; {claim})", "futures", f"hypothesis:{h.id}"))
        if len(out) >= MAX_HUNCHES:
            break
    return out


def _clip(text: str) -> str:
    return " ".join(str(text).split())[:_CLIP]


def save(model: UserModel, path: Path) -> None:
    from octavia.core.jsonfile import write_json

    try:
        write_json(path, model.to_json(), indent=1)
    except OSError:
        log.warning("Couldn't save her picture of the user", exc_info=True)


def load(path: Path) -> UserModel | None:
    from octavia.core.jsonfile import read_json

    raw = read_json(path, "her picture of the user")
    try:
        return UserModel.from_json(raw) if isinstance(raw, dict) else None
    except (TypeError, ValueError):
        return None
