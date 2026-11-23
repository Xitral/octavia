"""Tools to hand her an objective, follow it, and take its result (see ``octavia/objectives``).

Starting one is SAFE: it plans with read-only tools, its research only reads, and its code goes to a branch of the
project in a worktree of its own, never into what the user has checked out. Cancelling one, and merging its code
into the project, are confirmed like any CONFIRM call: her quick voice lets the user's own plain "merge it" (or a yes
to her "say the word and I'll merge it") stand for the answer (``core/consent.py``), so it isn't asked twice.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from pydantic import Field

from octavia.objectives import report
from octavia.objectives.projects import Project, resolve
from octavia.objectives.runner import ObjectiveRunner
from octavia.tools.base import NoArgs, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_UNAVAILABLE = "Objectives aren't available right now (they start with the assistant, in voice mode)."


def _runner(ctx: ToolContext) -> ObjectiveRunner | None:
    runner = ctx.extras.get("objectives")
    return runner if isinstance(runner, ObjectiveRunner) else None


def _projects(ctx: ToolContext) -> list[Project]:
    find = ctx.extras.get("projects")
    return list(find()) if callable(find) else []


def _missing(ref: str) -> str:
    return f"There's no objective “{ref}”; objective_status lists them."


def _names(projects: list[Project]) -> str:
    return ", ".join(p.name for p in projects) or "none found"


class StartArgs(ToolArgs):
    goal: str = Field(
        min_length=1,
        max_length=4000,
        description="The outcome the user wants, in their words plus anything they said that matters (nobody can "
        "ask them later).",
    )
    project: str = Field(
        default="",
        max_length=120,
        description='Folder name of their project when it involves code ("my game"); empty for research-only.',
    )


class StartObjective(Tool):
    name = "start_objective"
    description = (
        'Hand off an outcome the user wants done while they\'re away ("make progress on my game", "get X sorted"): '
        "you plan it, split it into steps, run research and code steps beside the conversation, have each one "
        "checked independently, and report once at the end. Code goes to a branch of the project, never straight "
        "into it. Bigger work on yourself (several changes, or research then changes) works too, with no project; "
        "for one change to yourself use build_improvement, for one quick lookup start_background_task."
    )
    Args = StartArgs
    risk = RiskLevel.SAFE
    category = "objectives"

    async def run(self, args: StartArgs, ctx: ToolContext) -> ToolResult:
        runner = _runner(ctx)
        if runner is None:
            return ToolResult.error(_UNAVAILABLE)
        project = None
        if args.project:
            projects = await asyncio.to_thread(_projects, ctx)
            matches = resolve(projects, args.project)
            if len(matches) != 1:
                which = "several projects match" if matches else "no project matches"
                return ToolResult.error(
                    f"{which} “{args.project}”. Their projects: {_names(matches or projects)}. Ask which one (or "
                    "leave project empty for research only)."
                )
            project = matches[0]
            if not project.has_commits:
                return ToolResult.error(
                    f"{project.name} has no commits yet, so I can't work on a branch of it. They can commit it once "
                    "and ask again."
                )
        try:
            objective = runner.start(args.goal, project)
        except RuntimeError as exc:
            return ToolResult.error(f"Couldn't start it: {exc}.")
        checked = ""
        if project is not None:
            checked = (
                f" Code is checked with {' '.join(project.check)}."
                if project.check
                else f" Its code can only be reviewed, not tested: {project.check_note}."
            )
        return ToolResult.success(
            f"Started objective #{objective.id}"
            + (f" on {project.name}" if project else "")
            + ". I'm planning it now; "
            f"each step is checked independently and I'll say once when it's over.{checked} Tell the user briefly; "
            "don't wait for it.",
            data={"id": objective.id},
        )


class StatusArgs(ToolArgs):
    objective: str = Field(default="", description="Number or name for one objective's full account; empty for all.")


class ObjectiveStatus(Tool):
    name = "objective_status"
    description = (
        "What you've been doing and where your work stands: objectives (step by step, only verified steps count as "
        'done), background and web tasks, your own updates, and what needs the user. Use for "what have you been '
        'doing?", "how\'s my game coming?", "what\'s left?".'
    )
    Args = StatusArgs
    risk = RiskLevel.SAFE
    category = "objectives"

    async def run(self, args: StatusArgs, ctx: ToolContext) -> ToolResult:
        runner = _runner(ctx)
        now = datetime.now()
        if runner is not None:
            await runner.refresh_updates()
        if args.objective:
            if runner is None:
                return ToolResult.error(_UNAVAILABLE)
            objective = runner.find(args.objective)
            if objective is None:
                return ToolResult.error(_missing(args.objective))
            return ToolResult.success(report.detail(objective, now), data={"id": objective.id})
        tasks = ctx.extras.get("tasks")
        web = ctx.extras.get("web_tasks")
        text = await report.work_report(
            runner.objectives if runner else [],
            now=now,
            tasks=list(tasks.tasks) if tasks is not None else None,
            web_tasks=list(web.tasks) if web is not None else None,
            updates=ctx.extras.get("work_updates"),
        )
        return ToolResult.success(
            text + "\n\nSay it in a few sentences: what's finished and verified, what's under way, and what needs "
            "them. Don't read out numbers, paths or branch names unless asked."
        )


class ObjectiveRef(ToolArgs):
    objective: str = Field(description="Number or name.")


class CancelObjective(Tool):
    name = "cancel_objective"
    description = "Stop an objective that's under way; steps already verified (and their code branch) are kept."
    Args = ObjectiveRef
    risk = RiskLevel.CONFIRM
    category = "objectives"
    timeout_s = 60.0

    async def precheck(self, args: ObjectiveRef, ctx: ToolContext) -> str | None:
        runner = _runner(ctx)
        if runner is None:
            return _UNAVAILABLE
        objective = runner.find(args.objective)
        if objective is None:
            return _missing(args.objective)
        return None if objective.open else f"“{objective.title}” isn't under way any more ({objective.status})."

    async def describe_call(self, args: ObjectiveRef, ctx: ToolContext) -> str:
        runner = _runner(ctx)
        objective = runner.find(args.objective) if runner else None
        return f"stop the objective “{objective.title}”" if objective else "stop an objective"

    async def run(self, args: ObjectiveRef, ctx: ToolContext) -> ToolResult:
        runner = _runner(ctx)
        objective = runner.find(args.objective) if runner else None
        if runner is None or objective is None:
            return ToolResult.error(_UNAVAILABLE if runner is None else _missing(args.objective))
        if not await runner.cancel(objective):
            return ToolResult.error(f"“{objective.title}” isn't under way any more.")
        return ToolResult.success(f"Stopped objective #{objective.id} “{objective.title}”; verified work is kept.")


class MergeObjectiveWork(Tool):
    name = "merge_objective_work"
    description = (
        "Merge an ended objective's verified code from its branch into the user's project (the branch they have "
        "checked out), as one merge commit. Only when they say to."
    )
    Args = ObjectiveRef
    risk = RiskLevel.CONFIRM
    category = "objectives"
    timeout_s = 120.0

    async def precheck(self, args: ObjectiveRef, ctx: ToolContext) -> str | None:
        runner = _runner(ctx)
        if runner is None:
            return _UNAVAILABLE
        return None if runner.find(args.objective) else _missing(args.objective)

    async def describe_call(self, args: ObjectiveRef, ctx: ToolContext) -> str:
        runner = _runner(ctx)
        objective = runner.find(args.objective) if runner else None
        if objective is None or runner is None:
            return "merge an objective's code into your project"
        branch = await asyncio.to_thread(runner.target_branch, objective)
        where = f"{objective.project}'s {branch} branch" if branch else str(objective.project)
        return f"merge the code from “{objective.title}” into {where}"

    async def run(self, args: ObjectiveRef, ctx: ToolContext) -> ToolResult:
        runner = _runner(ctx)
        objective = runner.find(args.objective) if runner else None
        if runner is None or objective is None:
            return ToolResult.error(_UNAVAILABLE if runner is None else _missing(args.objective))
        try:
            return ToolResult.success(await runner.merge(objective))
        except RuntimeError as exc:
            return ToolResult.error(f"I didn't merge it: {exc}.")


class ListProjects(Tool):
    name = "list_projects"
    description = "The user's code projects you can work on with start_objective, and how each one is tested."
    Args = NoArgs
    risk = RiskLevel.SAFE
    category = "objectives"

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        projects = await asyncio.to_thread(_projects, ctx)
        if not projects:
            return ToolResult.success("No projects found (objectives.project_roots).")
        return ToolResult.success("\n".join(p.describe() for p in projects))


TOOLS = [StartObjective, ObjectiveStatus, CancelObjective, MergeObjectiveWork, ListProjects]
