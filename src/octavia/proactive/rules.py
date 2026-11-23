"""What's worth preparing tonight, found by plain code from the user model, and what proactive work may never be.

Each candidate is one thing to prepare with its plan already written (research steps, read-only), how urgent and how
useful it looks, and the facts it came from:

* **exam**: an exam in the study tracker within the look-ahead: a review sheet and practice questions on its weak
  and untouched topics, from the course's own material in Canvas;
* **assignment** (or a Canvas quiz): what's asked, the rubric as a checklist, the course material it rests on and a
  plan to the due time. Never the graded work itself (``core/graded_work.py`` holds the line for clicks; this holds
  it for what's written);
* **task**: a TickTick to-do due soon whose words say it needs research, reading, planning or writing: the
  groundwork, or a draft for the user to send themselves when it's a message;
* **meeting**: a prep sheet for an event today or tomorrow that isn't private;
* **project** (only at the ``projects`` level, only for the projects named in ``proactive.projects``): one small
  improvement on a branch, planned by the objectives planner in the project.

``refused`` is the last check every plan passes before it runs: steps are research unless code is allowed, and no
step asks for an action proactive work never takes (sending, submitting, posting, buying, deleting, calling,
texting, taking a quiz or exam). The user's own words (a task's title, quoted) are left out of that check: they say
what the user has to do, not what she's told to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from octavia.proactive.model import UserModel

KINDS = ("exam", "assignment", "task", "meeting", "project")
VALUE = {"exam": 1.0, "assignment": 0.9, "project": 0.7, "task": 0.6, "meeting": 0.4}
MEETING_AHEAD_S = 36 * 3600.0
TASK_AHEAD_S = 3 * 86400.0
STALE_PROJECT_S = 21 * 86400.0  # a project untouched this long isn't one they're working on now

# To-dos that need groundwork she can do by reading and writing.
_PREPARABLE = re.compile(
    r"\b(?:research|look into|find|compare|choose|pick|plan|prepare|prep|outline|draft|write|read|review|study|"
    r"summari[sz]e|learn|figure out|decide|brainstorm|design|pitch|proposal|report|essay|paper|presentation|slides|"
    r"resume|cover letter|budget|itinerary|email|message|reply|respond)\b",
    re.IGNORECASE,
)
# To-dos that are a message to someone: she drafts it, they send it.
_MESSAGE = re.compile(r"\b(?:email|e-mail|message|text|dm|reply|respond|write to|follow up)\b", re.IGNORECASE)
# What proactive work never does, said as an instruction.
_FORBIDDEN = re.compile(
    r"\b(?:send|sends|sending|submit|submits|submitting|turn in|hand in|post|posting|publish|publishing|buy|buying|"
    r"purchase|place an order|order (?:it|them|the|a|an)|pay|paying|delete|deleting|remove the|trash|call|calling|phone|text them|text him|text her|"
    r"dm|merge|push|deploy|take the (?:quiz|exam|test)|start the (?:quiz|exam|test)|answer the (?:quiz|exam|test))\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"“[^”]*”")

PREPARED = (
    "\n\nThis is prepared work nobody asked for yet: {user} decides in the morning whether to use it. Write the result "
    "itself as your reply. Only read and write: you have no way to send, submit, post, buy or delete anything, and "
    "mustn't try. Never answer graded questions (a quiz, an exam, a graded problem) for them; explaining the material, "
    "practice questions on it, checklists, plans and drafts of their own messages are fine."
)


@dataclass
class Candidate:
    key: str  # stable: the same thing again tomorrow has the same key
    kind: str
    title: str  # what it is, short: "a review sheet for the AI midterm"
    goal: str  # the objective's goal
    plan: dict[str, Any] | None  # objectives' plan shape; None: the objectives planner plans it (code)
    why: str  # the facts behind it
    due: float | None = None
    score: float = 0.0
    project: str | None = None  # the project it codes in
    features: dict[str, str] = field(default_factory=dict)  # what ``learn`` judges it by
    subject: str = ""  # what it's about, without the template's words: what a claim about it must name


def candidates(
    model: UserModel, *, now: float, coding: bool = False, projects: tuple[str, ...] = ()
) -> list[Candidate]:
    """Everything worth preparing, best first."""
    user = model.user
    found: list[Candidate] = []
    for exam in model.exams:
        found.append(_exam(exam, user, now))
    for deadline in model.deadlines:
        found.append(_assignment(deadline, user, now))
    for task in model.tasks:
        if task.due - now <= TASK_AHEAD_S and _PREPARABLE.search(task.title):
            found.append(_task(task, user, now))
    for meeting in model.meetings:
        if not meeting.private and now < meeting.start <= now + MEETING_AHEAD_S and len(meeting.title.split()) >= 2:
            found.append(_meeting(meeting, user, now))
    if coding:
        wanted = {p.casefold() for p in projects}
        for project in model.projects:
            if project.name.casefold() not in wanted or project.open_objective:
                continue
            if project.last_commit is None or now - project.last_commit > STALE_PROJECT_S:
                continue
            found.append(_project(project, user))
    return sorted(found, key=lambda c: -c.score)


def _urgency(due: float | None, now: float) -> float:
    """1 at the deadline, about half two days out."""
    if due is None:
        return 0.5
    days = max(0.0, (due - now) / 86400)
    return 1.0 / (1.0 + days / 2)


def _course_key(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", name.casefold()).split())


def _research(title: str, instruction: str, acceptance: str) -> dict[str, Any]:
    return {"title": title, "kind": "research", "instruction": instruction, "acceptance": acceptance}


def _plan(title: str, approach: str, *steps: dict[str, Any]) -> dict[str, Any]:
    return {"title": title, "approach": approach, "not_doing": "Nothing is sent, submitted or handed in.",
            "steps": list(steps)}  # fmt: skip


def _exam(exam: Any, user: str, now: float) -> Candidate:
    due = datetime.fromisoformat(exam.day).timestamp() if exam.day else None
    focus = exam.weak[:6] or exam.untouched[:6]
    topics = f" Focus on: {', '.join(focus)}." if focus else " Cover the topics the course material says it includes."
    missed = f" {exam.missed} questions they missed are due for review in the study tracker." if exam.missed else ""
    title = f"a review sheet for {exam.course} {exam.name}"
    goal = f"Prepare {title} on {exam.day}, from the course's own material in Canvas.{topics}"
    instruction = (
        f"{user}'s {exam.course} {exam.name} is on {exam.day}.{topics}{missed} Read the course's material in Canvas "
        "(canvas_course_material, canvas_announcements for what the exam covers) and the study tracker "
        "(read_about_me, recall). Write a one-page review sheet (the key ideas, formulas and how to apply them, each "
        "with where in the course material it's from) and 8 to 12 practice questions of the kind the course asks, "
        "each with a worked answer, hardest topics first."
    )
    acceptance = (
        "Every point comes from the course's own material (named) or is plainly marked as general background; the "
        "practice questions are on the named topics, each with a correct worked answer; nothing is copied from a "
        "graded assignment or answers one."
    )
    return Candidate(
        key=f"exam:{_course_key(exam.course)}:{_course_key(exam.name)}",
        kind="exam",
        title=title,
        goal=goal,
        plan=_plan(title, "One research step from the course's material.", _research(title, instruction, acceptance)),
        why=f"{exam.course} {exam.name} on {exam.day}"
        + (f", {exam.percent}% through its topics" if exam.percent is not None else "")
        + (f", weak: {', '.join(exam.weak[:3])}" if exam.weak else ""),
        due=due,
        score=VALUE["exam"] * _urgency(due, now) * (1.2 if exam.weak or (exam.percent or 0) < 60 else 1.0),
        features={"kind": "exam", "course": _course_key(exam.course)},
        subject=f"{exam.course} {exam.name}",
    )


def _assignment(deadline: Any, user: str, now: float) -> Candidate:
    quiz = deadline.kind == "quiz"
    name = f"“{deadline.title}”" + (f" for {deadline.course}" if deadline.course else "")
    if quiz:
        title = f"a review sheet for the quiz {name}"
        instruction = (
            f"{user} has the Canvas quiz {name} due {_day(deadline.due)}. Read what it covers (canvas_assignment for "
            "its description, canvas_course_material, canvas_announcements). Write a short review sheet of the "
            "material it covers and a few practice questions on that material with worked answers. Never look for or "
            "write the quiz's own answers."
        )
        acceptance = (
            "The sheet covers what the quiz's description and the course material say it covers, and names them."
        )
    else:
        title = f"a brief for {name}"
        instruction = (
            f"{user} has {name} due {_day(deadline.due)}. Read the assignment (canvas_assignment: its description, "
            "rubric and attachments) and the course material it rests on (canvas_course_material). Write a brief: "
            "what's asked in plain words, the rubric as a checklist, the material and readings it needs with where "
            "to find them, questions worth asking the professor, and a plan of small steps from tonight to the due "
            "time. Don't write the graded work itself."
        )
        acceptance = (
            "The brief matches the assignment's own description and rubric (quoted where it matters), points to real "
            "course material, and doesn't do the graded work."
        )
    return Candidate(
        key=f"canvas:{deadline.key}",
        kind="assignment",
        title=title,
        goal=f"Prepare {title}, due {_day(deadline.due)}, from Canvas.",
        plan=_plan(title, "One research step from Canvas.", _research(title, instruction, acceptance)),
        why=f"{deadline.title} ({deadline.kind}) due {_day(deadline.due)} in Canvas",
        due=deadline.due,
        score=VALUE["assignment"] * _urgency(deadline.due, now),
        features={"kind": "quiz" if quiz else "assignment", "course": _course_key(deadline.course)},
        subject=f"{deadline.title} {deadline.course}".strip(),
    )


def _task(task: Any, user: str, now: float) -> Candidate:
    message = bool(_MESSAGE.search(task.title))
    if message:
        title = f"a draft for “{task.title}”"
        instruction = (
            f"{user} has the to-do “{task.title}” due {_day(task.due)}. Draft the message for them to read, change "
            "and pass on themselves: what they'd want to say, from what you know of them (read_about_me, recall) and "
            "what it's about. Give a short and a fuller version, and say what you assumed."
        )
        acceptance = "A draft that fits the to-do, says what it assumed, and that they can use as it is or change."
    else:
        title = f"the groundwork for “{task.title}”"
        instruction = (
            f"{user} has the to-do “{task.title}” due {_day(task.due)}. Do the groundwork it needs that reading and "
            "writing can do: research, a comparison, a plan, an outline or a first draft, whichever fits, using what "
            "you know of them (read_about_me, recall) and the web. Say what's left for them to do."
        )
        acceptance = "It's the groundwork the to-do needs, sources named for any facts, and it says what's left."
    return Candidate(
        key=f"task:{task.id}",
        kind="task",
        title=title,
        goal=f"Prepare {title}.",
        plan=_plan(title, "One research step.", _research(title, instruction, acceptance)),
        why=f"TickTick: {task.title} due {_day(task.due)}",
        due=task.due,
        score=VALUE["task"] * _urgency(task.due, now) * (1.2 if (task.priority or 0) >= 3 else 1.0),
        features={"kind": "task", "course": "message" if message else "groundwork"},
        subject=task.title,
    )


def _meeting(meeting: Any, user: str, now: float) -> Candidate:
    title = f"a prep sheet for “{meeting.title}”"
    instruction = (
        f"{user} has “{meeting.title}” on the calendar {_day(meeting.start)}. Make a short prep sheet: what it's "
        "likely about from the calendar entry (get_calendar_events) and what you know of them (read_about_me, "
        "recall), anything they said about it, and three questions or points worth raising. Mark guesses as guesses."
    )
    acceptance = "Short, specific to this event, guesses marked as guesses, nothing made up about people."
    return Candidate(
        key=f"meeting:{_course_key(meeting.title)}:{datetime.fromtimestamp(meeting.start):%Y-%m-%d}",
        kind="meeting",
        title=title,
        goal=f"Prepare {title}.",
        plan=_plan(title, "One research step.", _research(title, instruction, acceptance)),
        why=f"Calendar: {meeting.title} {_day(meeting.start)}",
        due=meeting.start,
        score=VALUE["meeting"] * _urgency(meeting.start, now),
        features={"kind": "meeting", "course": ""},
        subject=meeting.title,
    )


def _project(project: Any, user: str) -> Candidate:
    title = f"one small improvement to {project.name}"
    goal = (
        f"Make {title} on a branch, the most useful one its README, TODO notes or recent commits point to, small "
        f"enough to finish and check tonight, with tests. It's prepared for {user} to review in the morning: change "
        "nothing outside the branch."
    )
    return Candidate(
        key=f"project:{project.name.casefold()}",
        kind="project",
        title=title,
        goal=goal,
        plan=None,  # the objectives planner reads the project and plans it
        why=f"{project.name}: last commit {_day(project.last_commit)}",
        due=None,
        score=VALUE["project"] * 0.5,
        project=project.name,
        features={"kind": "project", "course": project.name.casefold()},
        subject=project.name,
    )


def _day(ts: float | None) -> str:
    if not ts:
        return "soon"
    return datetime.fromtimestamp(ts).strftime("%A %b %d at %H:%M").replace(" at 00:00", "")


def refused(candidate: Candidate, *, coding: bool) -> str | None:
    """Why this mustn't run as proactive work, or None. The last check before anything starts."""
    if candidate.kind not in KINDS:
        return f"unknown kind {candidate.kind!r}"
    if candidate.plan is None:
        if not (coding and candidate.kind == "project" and candidate.project):
            return "only a project of theirs, at the projects level, is planned by the objectives planner"
        texts = [candidate.goal]
    else:
        steps = candidate.plan.get("steps") or []
        if not steps:
            return "no steps"
        if any(step.get("kind") != "research" for step in steps):
            return "proactive steps here only read and write"
        texts = [candidate.goal, *(f"{s.get('instruction', '')} {s.get('title', '')}" for s in steps)]
    for text in texts:
        if match := _FORBIDDEN.search(_QUOTED.sub("", text)):
            return f"it asks for something proactive work never does ({match.group(0)!r})"
    return None


def with_limits(candidate: Candidate, user: str) -> dict[str, Any] | None:
    """The candidate's plan with the prepared-work rules added to each step (after ``refused`` has passed it)."""
    if candidate.plan is None:
        return None
    plan = {**candidate.plan, "steps": [dict(s) for s in candidate.plan["steps"]]}
    for step in plan["steps"]:
        step["instruction"] = step["instruction"] + PREPARED.format(user=user)
    return plan
