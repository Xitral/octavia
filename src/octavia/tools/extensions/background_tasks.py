"""Tools to hand longer jobs to background tasks and follow them (see ``_background_tasks`` for how they run).

Starting one is SAFE: a task can only search and read the web and use read-only tools, and it writes nothing but
its own result file. Cancelling one is asked, since the work done so far is lost.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._background_tasks import MAX_RUNNING, TaskManager, has_result, saved_heading

MAX_RESULT_CHARS = 20000  # what get_background_task_result hands the model; the file keeps it all
_UNAVAILABLE = "Background tasks aren't available right now (they start with the assistant)."


class StartTaskArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=80, description="A short name.")
    instruction: str = Field(
        min_length=1,
        max_length=4000,
        description=("Everything it needs (it can't ask): what to find or write, length, form, what the user said."),
    )
    delivery: Literal["speak", "notify"] = Field(
        default="speak",
        description="How the user hears it's done.",
    )


class TaskRef(ToolArgs):
    task: str = Field(description="Number or name.")


def _manager(ctx: ToolContext) -> TaskManager | None:
    manager = ctx.extras.get("tasks")
    return manager if isinstance(manager, TaskManager) else None


def _missing(ref: str) -> str:
    return f"There's no background task “{ref}”; list_background_tasks shows them."


class StartBackgroundTask(Tool):
    name = "start_background_task"
    description = (
        "Hand a longer job (research, a report, comparing options) to a background task that works while you keep "
        "talking; it reads the web, Canvas and the user's documents and uses read-only tools, never the screen, mouse or keyboard. Use for jobs "
        "over a minute or when asked."
    )
    Args = StartTaskArgs
    risk = RiskLevel.SAFE
    category = "tasks"

    async def run(self, args: StartTaskArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        try:
            task = manager.start(args.title, args.instruction, args.delivery)
        except RuntimeError:
            return ToolResult.error(_UNAVAILABLE)
        running = sum(1 for other in manager.tasks if other.status == "running" and other.id != task.id)
        wait = f" It waits until one of the {MAX_RUNNING} running tasks is done." if running >= MAX_RUNNING else ""
        told = "a short line is spoken" if args.delivery == "speak" else "a notification is shown"
        return ToolResult.success(
            f"Started background task #{task.id} “{task.title}”.{wait} When it's done {told} and the full result is "
            "saved. Tell the user briefly; don't wait for it.",
            data={"id": task.id},
        )


class ListBackgroundTasks(Tool):
    name = "list_background_tasks"
    description = "List background tasks with their numbers, progress and outcomes."
    risk = RiskLevel.SAFE
    category = "tasks"

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        tasks = manager.tasks
        if not tasks:
            return ToolResult.success("No background tasks yet.")
        now = datetime.now()
        active = [task for task in tasks if task.active]
        finished = [task for task in tasks if not task.active][-10:]
        lines = [task.progress(now) for task in (*active, *reversed(finished))]
        if not active:
            lines.insert(0, "None running right now.")
        return ToolResult.success("\n".join(lines), data={"running": len(active), "count": len(tasks)})


class GetBackgroundTaskResult(Tool):
    name = "get_background_task_result"
    description = "A background task's full result and file, or its progress. Give the gist unless they want more."
    Args = TaskRef
    risk = RiskLevel.SAFE
    category = "tasks"

    async def run(self, args: TaskRef, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        task = manager.find(args.task)
        if task is None:
            return ToolResult.error(_missing(args.task))
        if not has_result(task):
            return ToolResult.success(f"No result yet. {task.progress(datetime.now())}")
        try:
            text = Path(task.file).read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult.error(
                f"The result of “{task.title}” was saved to {task.file}, but I can't read it: {exc}"
            )
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n\n[… the rest is in the file]"
        return ToolResult.success(
            f"{saved_heading(task)}\n\n{text}", data={"id": task.id, "file": task.file, "status": task.status}
        )


class CancelBackgroundTask(Tool):
    name = "cancel_background_task"
    description = "Cancel a running or waiting background task; its work so far is lost."
    Args = TaskRef
    risk = RiskLevel.CONFIRM
    category = "tasks"
    timeout_s = 30.0  # the task's brain closes before it returns

    async def precheck(self, args: TaskRef, ctx: ToolContext) -> str | None:
        manager = _manager(ctx)
        if manager is None:
            return _UNAVAILABLE
        task = manager.find(args.task)
        if task is None:
            return _missing(args.task)
        if not task.active:
            return f"“{task.title}” isn't running anymore ({task.status})."
        return None

    async def describe_call(self, args: TaskRef, ctx: ToolContext) -> str:
        manager = _manager(ctx)
        task = manager.find(args.task) if manager else None
        return f"cancel the background task “{task.title}”" if task else "cancel a background task"

    async def run(self, args: TaskRef, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        task = manager.find(args.task)
        if task is None:
            return ToolResult.error(_missing(args.task))
        if not await manager.cancel(task):
            return ToolResult.error(f"“{task.title}” isn't running anymore ({task.status}).")
        return ToolResult.success(f"Cancelled background task #{task.id} “{task.title}”.")


TOOLS = [StartBackgroundTask, ListBackgroundTasks, GetBackgroundTaskResult, CancelBackgroundTask]
