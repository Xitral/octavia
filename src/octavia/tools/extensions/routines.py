"""Learning by watching, as tools (docs/routines.md): what routines she noticed and why, and the user's say over them.
The learner is ``octavia.routines``; the running one is ``ctx.extras["routines"]``.

``routines`` reads, looks again, or forgets one routine (it only ever keeps less): nothing to ask.
``routines_settings`` asks first only when it loosens something (offers back on) or deletes everything she learned.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_OFF = "Learning by watching is off (routines.enabled in the settings), so there are no routines."


def _learner(ctx: ToolContext) -> Any:
    return ctx.extras.get("routines")


class RoutinesArgs(ToolArgs):
    action: Literal["list", "look_again", "forget"] = Field(
        description=(
            "list: the routines you noticed (learned ones you may offer, ones you're still watching, ones turned "
            "down), each with its evidence; look_again: look through the records for routines now; forget: never "
            "offer routine `id` again (the user said to forget it, or that it's wrong)."
        )
    )
    id: int | None = Field(default=None, ge=1, description="forget: the routine's number from the list.")


class RoutinesTool(Tool):
    name = "routines"
    description = (
        "The routines you noticed by watching (which app the user opens around when, which app follows which, what "
        "they ask you for at the same time of day), learned from the app in front and their requests over the last "
        "weeks. Use it for 'what routines have you noticed?', 'why did you offer that?', and 'forget that routine'. "
        "These are habits seen in the records, not plans of theirs: say them that way, with the evidence."
    )
    Args = RoutinesArgs
    risk = RiskLevel.SAFE
    category = "routines"

    async def run(self, args: RoutinesArgs, ctx: ToolContext) -> ToolResult:
        learner = _learner(ctx)
        if learner is None:
            return ToolResult.error(_OFF)
        from octavia.routines import reports

        if args.action == "list":
            return ToolResult.success(await asyncio.to_thread(reports.listing, learner))
        if args.action == "look_again":
            learned, watching = await asyncio.to_thread(learner.mine_now)
            listing = await asyncio.to_thread(reports.listing, learner)
            return ToolResult.success(f"Looked again: {learned} learned, {watching} still watching.\n{listing}")
        if args.id is None:
            return ToolResult.error("Which routine? forget needs its number from the list.")
        described = await asyncio.to_thread(learner.forget, args.id)
        if described is None:
            return ToolResult.error(f"There's no routine #{args.id}.")
        return ToolResult.success(f"Forgotten: you won't offer it again (the user {described}).")


class RoutinesSettingsArgs(ToolArgs):
    setting: Literal["offers", "reset"] = Field(
        description="offers: offer learned routines on your own (on/off); reset: delete every routine you learned."
    )
    on: bool = Field(default=False, description="offers: on or off. Ignored for reset.")


class RoutinesSettings(Tool):
    name = "routines_settings"
    description = (
        "Stop or start offering the routines you learned ('stop suggesting my routines'), or delete everything you "
        "learned about them ('forget my routines'), when the user asks."
    )
    Args = RoutinesSettingsArgs
    risk = RiskLevel.SAFE
    category = "routines"

    def assess_risk(self, args: RoutinesSettingsArgs) -> RiskLevel:
        # Loosening, or deleting everything, is asked first; stopping the offers never is.
        return RiskLevel.CONFIRM if args.setting == "reset" or args.on else RiskLevel.SAFE

    async def describe_call(self, args: RoutinesSettingsArgs, ctx: ToolContext) -> str:
        if args.setting == "reset":
            return "delete every routine I learned by watching"
        return f"{'start' if args.on else 'stop'} offering the routines I learned"

    async def run(self, args: RoutinesSettingsArgs, ctx: ToolContext) -> ToolResult:
        learner = _learner(ctx)
        if learner is None:
            return ToolResult.error(_OFF)
        if args.setting == "reset":
            removed = await asyncio.to_thread(learner.store.forget_all)
            return ToolResult.success(
                f"Deleted {removed} routine{'s' if removed != 1 else ''}. The records they came from (screen history, "
                "Futures) are untouched, so the daily look can find them again; turning offers off, or routines.enabled "
                "off in the settings, stops that."
            )
        await asyncio.to_thread(learner.set_offers, args.on)
        if args.on and not learner.settings.offers:
            return ToolResult.success("Noted, but offers are off in the settings (routines.offers), so none are made.")
        return ToolResult.success(f"Offers {'on' if args.on else 'off'}.")


TOOLS = [RoutinesTool, RoutinesSettings]
