"""What she tells the user about her work: "what have you been doing?" across objectives, background tasks, web
tasks and her own updates, and the full account of one objective.

Every line keeps apart what was asked, planned, attempted, verified and left unresolved; a step counts as done only
when it's verified. The text goes to the brain, which says it in her own words.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from octavia.core import usage as claude_usage
from octavia.objectives.models import (
    CHECKING,
    DONE,
    FAILED,
    OBJECTIVE_CANCELLED,
    OBJECTIVE_FAILED,
    PENDING,
    PLANNING,
    RUNNING,
    SKIPPED,
    VERIFIED,
    Objective,
    Step,
)

RECENT = timedelta(days=3)  # ended objectives and tasks still worth mentioning
_STATUS = {
    PLANNING: "planning it",
    "working": "working on it",
    DONE: "done",
    "unfinished": "ended unfinished",
    OBJECTIVE_FAILED: "couldn't start",
    OBJECTIVE_CANCELLED: "cancelled",
}


def _when(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def recent(objectives: Iterable[Objective], now: datetime) -> list[Objective]:
    """The open ones, and the ones that ended lately (cancelled ones only when today)."""
    out = []
    for o in objectives:
        ended = _when(o.finished)
        if o.open or (
            ended is not None and now - ended <= (RECENT if o.status != OBJECTIVE_CANCELLED else timedelta(hours=12))
        ):
            out.append(o)
    return out


def needs_you(objective: Objective) -> str | None:
    """What this objective waits on from the user, if anything."""
    if objective.open:
        return None
    ready = [s.improvement for s in objective.steps if s.status == VERIFIED and s.awaiting_install and s.improvement]
    if ready:
        numbers = ", ".join(f"#{n}" for n in ready)
        return f"update {numbers} from “{objective.title}”, built and checked, waiting for your OK to install"
    if objective.code_to_merge:
        return (
            f"verified code for “{objective.title}” is on a branch of {objective.project}, waiting for your OK to merge"
        )
    return None


def line(objective: Objective, now: datetime) -> str:
    """One line: where it stands, counting only verified steps as done."""
    head = f"#{objective.id} “{objective.title}”" + (f" (on {objective.project})" if objective.project else "")
    if objective.status == PLANNING:
        return f"{head}: planning it." + (f" Held back: {objective.waiting}." if objective.waiting else "")
    if objective.status == OBJECTIVE_FAILED:
        return f"{head}: couldn't start. {objective.error or ''}".strip()
    total = len(objective.steps)
    verified = sum(1 for s in objective.steps if s.status == VERIFIED)
    unresolved = [s for s in objective.steps if s.status in (FAILED, SKIPPED)]
    parts = [f"{verified} of {total} steps verified"]
    active = [s for s in objective.steps if s.status in (RUNNING, CHECKING)]
    if active:
        parts.append("now " + "; ".join(f"step {s.id} “{s.title}” ({_doing(s)})" for s in active))
    if unresolved:
        parts.append(f"{len(unresolved)} unresolved (first: “{unresolved[0].title}”: {unresolved[0].problem})")
    if objective.open and objective.waiting:
        parts.append(f"held back: {objective.waiting}")
    status = _STATUS.get(objective.status, objective.status)
    ended = _when(objective.finished)
    if not objective.open and ended is not None:
        status += f" {_ago(ended, now)}"
    if objective.merged:
        parts.append("its code is merged")
    return f"{head}, {status}: " + ", ".join(parts) + "."


def detail(objective: Objective, now: datetime) -> str:
    """The full account of one objective, step by step, with the evidence."""
    lines = [line(objective, now), f"Asked: {objective.goal}"]
    if objective.approach:
        lines.append(f"Plan: {objective.approach}")
    if objective.not_doing:
        lines.append(f"Left out on purpose: {objective.not_doing}")
    if objective.baseline and objective.baseline.startswith("failed"):
        lines.append("The project's checks already failed before I changed anything, so they can't vouch for my code.")
    elif objective.baseline == "none":
        lines.append("The project has no checks I can run, so code steps were only reviewed, not tested.")
    for s in objective.steps:
        lines.append(_step(s))
    if objective.branch:
        where = "merged" if objective.merged else "not merged yet"
        lines.append(f"Code branch: {objective.branch} in {objective.project_path} ({where}).")
    cost = _cost(objective)
    if cost:
        lines.append(cost)
    if (ask := needs_you(objective)) is not None:
        lines.append(f"Needs the user: {ask}.")
    return "\n".join(lines)


def _step(s: Step) -> str:
    deps = f", needs {', '.join(str(d) for d in s.depends_on)}" if s.depends_on else ""
    text = f"Step {s.id} ({s.kind}{deps}) “{s.title}”: {s.status}"
    if s.status in (RUNNING, CHECKING):
        text += f", {_doing(s)}"
    if s.status == PENDING and s.attempts:
        text += f" (attempt {s.attempts} didn't pass)"
    parts = [text]
    if s.result:
        parts.append(f"Its agent says: {s.result}")
    if s.evidence:
        parts.append(f"Checked: {s.evidence}")
    if s.problem and s.status != VERIFIED:
        parts.append(f"Problem: {s.problem}")
    if s.file:
        parts.append(f"Result saved in {s.file}")
    return " ".join(part.strip().rstrip(".") + "." for part in parts)


def _doing(s: Step) -> str:
    return s.activity or ("being checked" if s.status == CHECKING else "working")


def _cost(objective: Objective) -> str:
    if objective.started_at is None:
        return ""
    end = objective.ended_at or datetime.now().timestamp()
    used = claude_usage.used_between(objective.started_at, end)
    if used is None:
        return ""
    return (
        f"Claude usage while it ran: {round(used * 100)}% of your weekly limit (anything else running then counts too)."
    )


def _ago(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"on {then:%A} at {then:%H:%M}"


def _tasks(label: str, tasks: list[Any], now: datetime) -> list[str]:
    """Background or web tasks: the running ones and the ones that ended lately."""
    out = []
    for task in tasks:
        finished = _when(getattr(task, "finished", None))
        if task.active or (finished is not None and now - finished <= timedelta(hours=24)):
            out.append(task.progress(now))
    return [f"{label}:", *(f"- {t}" for t in out)] if out else []


async def work_report(
    objectives: list[Objective],
    *,
    now: datetime,
    tasks: list[Any] | None = None,
    web_tasks: list[Any] | None = None,
    updates: Callable[[], Awaitable[str]] | None = None,
) -> str:
    """Everything she has been doing, for "what have you been doing?": needs-you first, then the work."""
    shown = recent(objectives, now)
    asks = [ask for o in shown if (ask := needs_you(o)) is not None]
    sections: list[str] = []
    if asks:
        sections += ["Needs the user:", *(f"- {a}" for a in asks)]
    if shown:
        sections += ["Objectives (only verified steps count as done):", *(f"- {line(o, now)}" for o in shown)]
    sections += _tasks("Background tasks", tasks or [], now)
    sections += _tasks("Web tasks", web_tasks or [], now)
    if updates is not None:
        try:
            text = (await updates()).strip()
        except Exception:
            text = ""
        if text:
            sections += ["Her own updates (self-improvement):", f"- {text}"]
    if not sections:
        return "Nothing: no objectives, background tasks or updates lately."
    return "\n".join(sections)


def summary_for_prompt(objectives: list[Objective], now: datetime) -> str:
    """One line for the system prompt: what's under way and what needs the user."""
    shown = recent(objectives, now)
    if not shown:
        return ""
    under_way = [o for o in shown if o.open]
    ended = [o for o in shown if not o.open and o.status != OBJECTIVE_CANCELLED]
    asks = [o for o in shown if needs_you(o) is not None]
    parts = []
    if under_way:
        parts.append(f"{len(under_way)} under way (" + "; ".join(f"#{o.id} {o.title}" for o in under_way) + ")")
    if ended:
        parts.append(f"{len(ended)} ended lately")
    if asks:
        parts.append(f"{len(asks)} waiting for the user's OK to merge")
    return "Objectives: " + ", ".join(parts) + "."
