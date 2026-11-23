"""Tools to hand hands-on screen work to a task that runs beside the conversation, and to follow and stop it (see
``_screen_tasks`` for how they run).

Starting one is asked: it looks at the screen and uses the mouse and keyboard while nobody watches. Stopping is safe
and never asked: it only halts her own work, and a stop has to be instant. Resuming a paused one isn't asked either:
it carries on with work the user already approved, and only in their own turn, when they ask for it.

Starting one is a long call (a whole instruction), and the model wrote it before saying a word: 8 to 14 s of silence
in her log, so its description has her say a short line first.
"""

from __future__ import annotations

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult, TurnInfo
from octavia.tools.extensions._screen_tasks import ScreenTask, ScreenTaskManager

_UNAVAILABLE = "Screen tasks aren't available right now (they run with the voice assistant)."
NOT_IN_TURN = "I only start screen work while you're talking with me."


def _manager(ctx: ToolContext) -> ScreenTaskManager | None:
    manager = ctx.extras.get("screen_tasks")
    return manager if isinstance(manager, ScreenTaskManager) else None


def _in_turn(ctx: ToolContext) -> bool:
    get = ctx.extras.get("turn")
    return callable(get) and isinstance(get(), TurnInfo)


class StartScreenTaskArgs(ToolArgs):
    title: str = Field(
        min_length=1,
        max_length=60,
        description="A short verb phrase to refer to it by: 'edit the slides'.",
    )
    instruction: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "Everything it needs, for an assistant who sees the screen but can't ask questions: which app or "
            "file, what to change, exactly what to type, what done looks like."
        ),
    )


class StartScreenTask(Tool):
    name = "start_screen_task"
    description = (
        "Hand a hands-on job on the user's screen (editing slides or a document, filling in a form, anything "
        "over a couple of clicks) to a task that does it with the mouse and keyboard on its own. It pauses when the "
        "user speaks to you, until they ask to carry on (resume_screen_task). One runs at a time; new ones queue. "
        "While one runs, don't use the mouse or keyboard tools yourself: queue more screen work here. Do one or two "
        "quick clicks yourself. When the user says to continue or keep working on a job of several steps, give one "
        "task every step you can do on the screen without them, in order (check the job, pull the results, make the "
        "plots), not just the next one: it reports when it ends, and a task per step leaves them saying 'continue' "
        "after each. Writing the instruction takes you many seconds of silence, so say one short line out loud first "
        "(what you'll do, e.g. “I'll fix that figure.”), then call this in the same reply."
    )
    Args = StartScreenTaskArgs
    risk = RiskLevel.CONFIRM  # it looks at the screen and uses the mouse and keyboard on its own
    category = "tasks"

    async def precheck(self, args: StartScreenTaskArgs, ctx: ToolContext) -> str | None:
        if _manager(ctx) is None:
            return _UNAVAILABLE
        if not _in_turn(ctx):
            return NOT_IN_TURN
        return None

    async def describe_call(self, args: StartScreenTaskArgs, ctx: ToolContext) -> str:
        return f"{args.title} on your screen, using the mouse and keyboard while we talk"

    async def run(self, args: StartScreenTaskArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        if not _in_turn(ctx):
            return ToolResult.error(NOT_IN_TURN)
        ahead = [task for task in (manager.current, *manager.waiting) if task is not None and task.active]
        try:
            task = manager.start(args.title, args.instruction)
        except RuntimeError:
            return ToolResult.error(_UNAVAILABLE)
        if ahead:
            more = f" and {len(ahead) - 1} more" if len(ahead) > 1 else ""
            when = f"It waits its turn behind “{ahead[0].title}”{more}."
        else:
            when = "It's starting now."
        if paused := [t for t in manager.paused if t is not task]:
            when += f" “{paused[-1].title}” stays paused until the user asks to carry on with it."
        return ToolResult.success(
            f"Started screen task #{task.id} “{task.title}”. {when} Tell the user in a few words, unless you already "
            "have, and carry on; don't wait for it. The user hears a short line when it's done.",
            data={"id": task.id},
        )


class ListScreenTasks(Tool):
    name = "list_screen_tasks"
    description = (
        "List the screen tasks (working, paused, waiting, recently finished), for how one is going or what's queued."
    )
    risk = RiskLevel.SAFE
    category = "tasks"

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        current, waiting = manager.current, manager.waiting
        finished = [task for task in manager.tasks if not task.stoppable][-5:]
        lines = [current.progress()] if current is not None and current.active else ["Nothing working right now."]
        lines += [task.progress(held=task is manager.holding) for task in manager.paused]
        lines += [task.progress(place) for place, task in enumerate(waiting, 1)]
        lines += [task.progress() for task in reversed(finished)]
        return ToolResult.success("\n".join(lines), data={"waiting": len(waiting), "busy": manager.busy})


class StopScreenTaskArgs(ToolArgs):
    everything: bool = Field(
        default=False, description="True to also clear the queue, paused ones too ('stop everything')."
    )
    task: str = Field(
        default="",
        description="To drop one waiting task: its number or name. Empty: the one working now.",
    )


class StopScreenTask(Tool):
    name = "stop_screen_task"
    description = (
        "Stop the screen task working now (the next waiting one then starts; with nothing working, it drops the "
        "paused one). Use as soon as the user says to stop or cancel the work."
    )
    Args = StopScreenTaskArgs
    risk = RiskLevel.SAFE  # halting her own work never needs a question
    category = "tasks"

    async def run(self, args: StopScreenTaskArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        if args.task and not args.everything:
            task = manager.find(args.task)
            if task is None:
                return ToolResult.error(f"There's no screen task “{args.task}”; list_screen_tasks shows them.")
            if manager.stop(task) is None:
                return ToolResult.error(f"“{task.title}” isn't running or waiting anymore ({task.status}).")
            return ToolResult.success(f"Stopped screen task #{task.id} “{task.title}”.")
        line = manager.stop_line(args.everything)
        if line is None and (paused := manager.paused):
            manager.stop(paused[-1])
            line = f"Okay, I dropped “{paused[-1].title}”."
        if line is None:
            return ToolResult.success("No screen task is running or waiting.")
        return ToolResult.success(line)


class ResumeScreenTaskArgs(ToolArgs):
    task: str = Field(default="", description="The paused task's number or name. Empty: the one paused last.")


class ResumeScreenTask(Tool):
    name = "resume_screen_task"
    description = (
        "Carry on with a paused screen task where it left off. Only when the user asks you to carry on, continue "
        "or finish it, never on your own after answering something else. With no task named it picks up the one "
        "paused last: no need to list them first."
    )
    Args = ResumeScreenTaskArgs
    risk = RiskLevel.SAFE  # work the user already approved, picked up again at their asking (in their own turn)
    category = "tasks"

    async def precheck(self, args: ResumeScreenTaskArgs, ctx: ToolContext) -> str | None:
        if _manager(ctx) is None:
            return _UNAVAILABLE
        if not _in_turn(ctx):
            return NOT_IN_TURN
        return None

    async def run(self, args: ResumeScreenTaskArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        if not _in_turn(ctx):
            return ToolResult.error(NOT_IN_TURN)
        task = manager.find(args.task) if args.task else None
        if args.task and task is None:
            return ToolResult.error(f"There's no screen task “{args.task}”; list_screen_tasks shows them.")
        if task is not None and task.status == "queued" and manager.go_on_with_line() is not None:
            return _line_going(task)
        if task is not None and task.active:
            return _already_going(task)
        if task is not None and task.status != "paused":
            return ToolResult.error(f"“{task.title}” isn't paused ({task.status}).")
        resumed = manager.resume(task)
        if resumed is None:
            if (started := manager.go_on_with_line()) is not None:  # waiting behind an earlier pause: it goes now
                return _line_going(started)
            going = next((t for t in (manager.current, *manager.waiting) if t is not None and t.active), None)
            return _already_going(going) if going is not None else ToolResult.error("No screen task is paused.")
        running = manager.current
        if running is not None and running is not resumed and running.active:
            when = f"It goes next, once “{running.title}” is done."
        else:
            when = "It's carrying on now."
        return ToolResult.success(
            f"Picked screen task #{resumed.id} “{resumed.title}” back up. {when} Tell the user in a few words.",
            data={"id": resumed.id},
        )


def _line_going(task: ScreenTask) -> ToolResult:
    """A "carry on" for a task waiting behind an earlier pause, which held the line: the line runs again now."""
    return ToolResult.success(
        f"Screen task #{task.id} “{task.title}” was waiting behind a pause; the line runs again now, so it starts in "
        "its turn.",
        data={"id": task.id, "status": task.status},
    )


def _already_going(task: ScreenTask) -> ToolResult:
    """A "carry on" for a task already working or waiting its turn: what the user asked for is already so, so it's
    no failure (an error here read to her as something gone wrong, during a DataCamp practice set in Vivaldi)."""
    state = "is already working on it" if task.status == "running" else "is already waiting its turn"
    return ToolResult.success(
        f"Screen task #{task.id} “{task.title}” {state}: nothing to resume. Tell the user in a few words that it's "
        "carrying on.",
        data={"id": task.id, "status": task.status},
    )


TOOLS = [StartScreenTask, ListScreenTasks, StopScreenTask, ResumeScreenTask]
