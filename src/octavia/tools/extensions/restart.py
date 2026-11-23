"""Restart on request: ``restart_now`` restarts her right away, into whatever update is installed and not running
yet, instead of waiting for the builds in progress to finish (installs restart only after them).

The app does the work (``App.restart_now``, ``ctx.extras["restart_now"]``): only the voice assistant running
under the supervisor with self-improvement on has it. A build cut off by it starts over after the restart.
It's CONFIRM: the user hears what will stop and says yes first, unless their own words plainly asked for a restart
that cuts nothing off ("can you do a restart?", ``tools/asked.py``). One that stops a build is always asked.
"""

from __future__ import annotations

from typing import Any

from octavia.evolve.models import EvolveError
from octavia.tools.base import NoArgs, RiskFinding, RiskLevel, Tool, ToolContext, ToolResult

CANT_RESTART = (
    "I can't restart myself here: that needs the voice assistant with self-improvement on, started under its "
    "supervisor. Restart me yourself."
)


def _hook(ctx: ToolContext) -> Any:
    hook = ctx.extras.get("restart_now")
    return hook if callable(hook) else None


class RestartNow(Tool):
    name = "restart_now"
    description = (
        "Restart yourself now, into any installed update, when the user asks. Builds in progress restart "
        "themselves after. The conversation continues."
    )
    Args = NoArgs
    risk = RiskLevel.CONFIRM
    category = "self_improvement"

    async def precheck(self, args: NoArgs, ctx: ToolContext) -> str | None:
        manager = ctx.extras.get("evolve")
        if _hook(ctx) is None or not getattr(manager, "supervised", False):
            return CANT_RESTART
        return None

    async def inspect_risk(self, args: NoArgs, ctx: ToolContext) -> RiskFinding | None:
        """A restart that stops builds in progress is flagged: the user's "restart" alone doesn't approve it (their
        words count only for a restart that cuts nothing off, ``tools/asked.py``), so they hear what would stop."""
        manager = ctx.extras.get("evolve")
        builds = await manager.active_builds() if manager is not None else []
        if builds:
            return RiskFinding(RiskLevel.CONFIRM, "builds", "stops what I'm building")
        return None

    async def describe_call(self, args: NoArgs, ctx: ToolContext) -> str:
        manager = ctx.extras.get("evolve")
        try:
            builds = await manager.active_builds() if manager is not None else []
        except Exception:
            builds = []
        if not builds:
            return "restart now"
        from octavia.evolve.manager import spoken_label  # imported late: this module stays light for the registry

        if len(builds) == 1:
            return f"restart now and stop building {spoken_label(builds[0])}, which starts over afterwards"
        names = [spoken_label(b) for b in builds]
        return f"restart now and stop building {', '.join(names[:-1])} and {names[-1]}, which start over afterwards"

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        hook = _hook(ctx)
        if hook is None:
            return ToolResult.error(CANT_RESTART)
        try:
            return ToolResult.success(await hook())
        except EvolveError as exc:
            return ToolResult.error(str(exc))


TOOLS = [RestartNow]
