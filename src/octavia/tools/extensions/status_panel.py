"""What's running beside her, at a glance: her status panel (``octavia.visuals.status``) and the same in words.

``whats_running`` brings the panel up (unless panels are off) with every download and long command, background task,
web task, deep think, self-improvement build or install and objective that's going, and what ended in the last hour,
and returns it in words for her answer. ``hide=True`` takes it down. SAFE: it only reads her own records and draws
on the user's screen, in a window that never takes the keyboard.
"""

from __future__ import annotations

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.visuals.status import StatusBoard

UNAVAILABLE = (
    "The status panel isn't running in this session. long_job_status, list_background_tasks and objective_status "
    "say how each kind of work stands."
)


class WhatsRunningArgs(ToolArgs):
    hide: bool = Field(default=False, description="True: take the status panel down instead.")


class WhatsRunning(Tool):
    name = "whats_running"
    description = (
        "Everything running in the background right now (downloads and long commands, background and web tasks, "
        "deep thinks, your own updates being built or installed, objectives) and what ended in the last hour, with "
        'progress and failures. Shows it in a small status panel and returns it in words. For "what\'s running?", '
        '"anything going on?". hide=true takes the panel down.'
    )
    Args = WhatsRunningArgs
    risk = RiskLevel.SAFE
    category = "general"
    timeout_s = 30.0  # the panel app's first start

    async def describe_call(self, args: WhatsRunningArgs, ctx: ToolContext) -> str:
        return "take the status panel down" if args.hide else "show what's running"

    async def run(self, args: WhatsRunningArgs, ctx: ToolContext) -> ToolResult:
        board = ctx.extras.get("status_board")
        if not isinstance(board, StatusBoard):
            return ToolResult.error(UNAVAILABLE)
        if args.hide:
            hidden = await board.hide()
            return ToolResult.success("Took the status panel down." if hidden else "The status panel wasn't up.")
        said, up = await board.request()
        if up:
            where = "The status panel shows it now. Say the gist; don't read it all out."
        elif board.mode == "off":
            where = "(The status panel is off in settings, so nothing was shown.)"
        else:
            where = "(The status panel couldn't be shown just now, so tell them in words.)"
        return ToolResult.success(f"{said}\n{where}", data={"panel": up})


TOOLS = [WhatsRunning]
