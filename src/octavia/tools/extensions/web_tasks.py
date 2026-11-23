"""Tools to hand work on websites to a web task in her own browser, follow it and stop it (see ``_web_tasks``).

Starting one is asked: on its own it clicks, types and submits on sites the user is signed in to. Stopping is safe
and never asked: it only halts her own work. Showing the browser (to sign in once) only opens a window.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._background_tasks import has_result, saved_heading
from octavia.tools.extensions._web_browser import BrowserError
from octavia.tools.extensions._web_tasks import WebTaskManager

MAX_RESULT_CHARS = 20000
_UNAVAILABLE = "Web tasks aren't available right now (they start with the voice assistant)."
_NO_BROWSER = "Web tasks need Chrome or Edge, and neither is installed."


def _manager(ctx: ToolContext) -> WebTaskManager | None:
    manager = ctx.extras.get("web_tasks")
    return manager if isinstance(manager, WebTaskManager) else None


def _missing(ref: str) -> str:
    return f"There's no web task “{ref}”; check_web_task without a task lists them."


class StartWebTaskArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=60, description="A short name: 'DataCamp chapter 2'.")
    instruction: str = Field(
        min_length=1,
        max_length=4000,
        description="Everything it needs (it can't ask): the site, what to do, how far to go, what done looks like.",
    )
    url: str = Field(default="", max_length=2000, description="The page to start on, if known.")


class StartWebTask(Tool):
    name = "start_web_task"
    description = (
        "Hand work on a website (a DataCamp course, filling in a long web form, anything of many clicks on a site) "
        "to a web task that does it in your own browser out of sight, without the user's mouse, keyboard or screen, "
        "so they keep using their computer. It stays signed in to sites the user signed in to there "
        "(open_web_task_browser). Prefer it to a screen task for work inside a website."
    )
    Args = StartWebTaskArgs
    risk = RiskLevel.CONFIRM  # it clicks, types and submits on its own, on sites the user is signed in to
    category = "tasks"

    async def precheck(self, args: StartWebTaskArgs, ctx: ToolContext) -> str | None:
        manager = _manager(ctx)
        if manager is None:
            return _UNAVAILABLE
        if not manager.browser.available:
            return _NO_BROWSER
        return None

    async def describe_call(self, args: StartWebTaskArgs, ctx: ToolContext) -> str:
        """Says what it may do that it otherwise couldn't (``_web_tasks.allowed``): the instruction is hers, and it may
        have come from something she read, so the user hears that it may send, buy or delete before saying yes."""
        from octavia.tools.extensions._web_tasks import allowed

        may = {"money": "move money", "buy": "buy", "delete": "delete", "send": "send or submit"}
        kinds = [may[scope] for scope in allowed(args.instruction)]
        if not kinds:
            return f"{args.title} in my own browser in the background, clicking there on my own (no sending, buying or deleting)"
        listed = kinds[0] if len(kinds) == 1 else f"{', '.join(kinds[:-1])} and {kinds[-1]}"
        return f"{args.title} in my own browser in the background, where it may {listed} on its own"

    async def run(self, args: StartWebTaskArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        instruction = f"Start at {args.url}\n\n{args.instruction}" if args.url else args.instruction
        try:
            task = manager.start(args.title, instruction)
        except RuntimeError:
            return ToolResult.error(_UNAVAILABLE)
        return ToolResult.success(
            f"Started web task #{task.id} “{task.title}” in your own browser, out of sight. The user hears a short "
            "line when it's done and can ask how it's going (check_web_task). Tell them briefly; don't wait for it.",
            data={"id": task.id},
        )


class CheckWebTaskArgs(ToolArgs):
    task: str = Field(default="", description="Number or name for one task's result. Empty: list them all.")


class CheckWebTask(Tool):
    name = "check_web_task"
    description = (
        "How web tasks are going (step count, what each is doing, the page it's on), or one finished task's full "
        "result. Give the gist unless they want more."
    )
    Args = CheckWebTaskArgs
    risk = RiskLevel.SAFE
    category = "tasks"

    async def run(self, args: CheckWebTaskArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        now = datetime.now()
        if not args.task:
            tasks = manager.tasks
            if not tasks:
                return ToolResult.success("No web tasks yet.")
            active = [task for task in tasks if task.active]
            finished = [task for task in tasks if not task.active][-5:]
            lines = [manager.progress(task, now) for task in (*active, *reversed(finished))]
            if not active:
                lines.insert(0, "None running right now.")
            return ToolResult.success("\n".join(lines), data={"running": len(active)})
        task = manager.find(args.task)
        if task is None:
            return ToolResult.error(_missing(args.task))
        if not has_result(task):
            return ToolResult.success(manager.progress(task, now))
        try:
            text = Path(task.file).read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult.error(
                f"The result of “{task.title}” was saved to {task.file}, but I can't read it: {exc}"
            )
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n\n[… the rest is in the file]"
        return ToolResult.success(f"{saved_heading(task)}\n\n{text}", data={"id": task.id, "file": task.file})


class StopWebTaskArgs(ToolArgs):
    task: str = Field(default="", description="Number or name. Empty: every running one.")


class StopWebTask(Tool):
    name = "stop_web_task"
    description = "Stop a running web task (or all of them). Use as soon as the user says to stop it."
    Args = StopWebTaskArgs
    risk = RiskLevel.SAFE  # halting her own work never needs a question
    category = "tasks"
    timeout_s = 30.0  # the task's brain and tab close before it returns

    async def run(self, args: StopWebTaskArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        if args.task:
            task = manager.find(args.task)
            if task is None:
                return ToolResult.error(_missing(args.task))
            targets = [task]
        else:
            targets = [task for task in manager.tasks if task.active]
            if not targets:
                return ToolResult.success("No web task is running.")
        stopped = [task for task in targets if await manager.cancel(task)]
        if not stopped:
            return ToolResult.error(f"“{targets[0].title}” isn't running anymore ({targets[0].status}).")
        names = ", ".join(f"#{task.id} “{task.title}”" for task in stopped)
        return ToolResult.success(f"Stopped web task {names}.")


class OpenBrowserArgs(ToolArgs):
    url: str = Field(min_length=1, max_length=2000, description="The site's sign-in page.")


class OpenWebTaskBrowser(Tool):
    name = "open_web_task_browser"
    description = (
        "Show your own browser (the one web tasks use) in a window, so the user can sign in to a site once; the "
        "login is kept for every web task after. Not while a web task runs."
    )
    Args = OpenBrowserArgs
    risk = RiskLevel.SAFE  # opens a window
    category = "tasks"
    timeout_s = 40.0  # the browser may have to close and start again with a window

    async def run(self, args: OpenBrowserArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        url = args.url if "://" in args.url else f"https://{args.url}"
        if not url.casefold().startswith(("https://", "http://")):
            return ToolResult.error(f"“{args.url}” isn't a web address.")
        try:
            await manager.browser.show(url)
        except BrowserError as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(
            "The browser is open in a window. Once the user has signed in they can close it, or leave it: the next "
            "web task closes it and works out of sight."
        )


TOOLS = [StartWebTask, CheckWebTask, StopWebTask, OpenWebTaskBrowser]
