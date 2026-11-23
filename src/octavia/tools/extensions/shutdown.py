"""Shut down on request: ``shut_down_self`` closes her (the assistant, its supervisor and what they started) once
her goodbye has been said. Never the computer.

The app does the work (``App.shut_down_now``, ``ctx.extras["shut_down"]``): the same clean way out as a restart, but
with exit code 0, which her supervisor takes as "stopped on purpose" and doesn't start her again, nor undo an update
for it (``evolve/supervisor.py``). She's back when she's started again: at the next login, with autostart.
It's CONFIRM: said at the PC, the user's own words are the yes ("shut down", "turn yourself off", ``tools/asked.py``).
Always asked: one from far away (a text, a Discord call: nobody there could start her again), one outside the user's
turn, and one that stops a build or an install.
"""

from __future__ import annotations

from typing import Any

from octavia.tools.base import NoArgs, RiskFinding, RiskLevel, Tool, ToolContext, ToolResult

CANT_SHUT_DOWN = "I can't shut myself down here: only the voice assistant can. Close me yourself."
# Finishes the question "Want me to shut myself down, which ...?" asked from far away.
_FROM_AFAR = "means you can't start me again from there (I'm back at your next login)"


def _hook(ctx: ToolContext) -> Any:
    hook = ctx.extras.get("shut_down")
    return hook if callable(hook) else None


def _far_away(ctx: ToolContext) -> bool:
    """Whether the request can't have come from someone at the PC: a text, a Discord call, or no turn of theirs."""
    turn_info = ctx.extras.get("turn")
    turn = turn_info() if callable(turn_info) else None
    if turn is None or turn.remote:
        return True
    calls = getattr(ctx.extras.get("phone"), "calls", None)
    try:
        return bool(getattr(calls, "on_call", False))
    except Exception:
        return True


async def _work(ctx: ToolContext) -> list[str]:
    """What shutting down now would stop: the builds running here, an install or undo being tested."""
    manager = ctx.extras.get("evolve")
    if manager is None:
        return []
    from octavia.evolve.manager import spoken_label  # imported late: this module stays light for the registry

    stops = [f"the build of {spoken_label(b)}" for b in await manager.active_builds()]
    if getattr(manager, "changing_code", False):
        stops.append("the update I'm installing")
    return stops


class ShutDownSelf(Tool):
    name = "shut_down_self"
    description = (
        "Shut yourself (the assistant) down and close completely, when the user asks you to: 'shut down', 'shut "
        "yourself off', 'turn yourself off', 'go to sleep'. You stay off until they start you again or log in "
        "next. Never shuts down or restarts the computer: 'shut down my computer' is not this."
    )
    Args = NoArgs
    risk = RiskLevel.CONFIRM
    category = "system"

    async def precheck(self, args: NoArgs, ctx: ToolContext) -> str | None:
        return CANT_SHUT_DOWN if _hook(ctx) is None else None

    async def inspect_risk(self, args: NoArgs, ctx: ToolContext) -> RiskFinding | None:
        """Flagged, so the user's words alone don't approve it (``tools/asked.py``): a request from far away (nobody
        there could start her again), and one that stops what she's building or installing."""
        if _far_away(ctx):
            return RiskFinding(RiskLevel.CONFIRM, "remote", _FROM_AFAR)
        try:
            stops = await _work(ctx)
        except Exception:
            stops = ["what I'm working on"]
        if stops:
            return RiskFinding(RiskLevel.CONFIRM, "work", f"stops {_spoken_list(stops)}")
        return None

    async def describe_call(self, args: NoArgs, ctx: ToolContext) -> str:
        return "shut myself down"

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        hook = _hook(ctx)
        if hook is None:
            return ToolResult.error(CANT_SHUT_DOWN)
        return ToolResult.success(await hook(remote=_far_away(ctx)))


def _spoken_list(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


TOOLS = [ShutDownSelf]
