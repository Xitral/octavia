"""Wave-offs, as a tool (``core/unprompted.py``): the user told her to drop something she keeps bringing up.

"Shut up about that", "stop bringing that up", "drop it", "enough with the reviews": however it's put, by voice, in
chat or over Discord, she works out what "that" was from what she said on her own lately and records the subject in
her own words. From then on every part of her that speaks unasked (Futures, study nudges, heads-ups, build reports,
reminders' extra words) is held to it by the one gate, and her own turns are told to leave it alone too. "You can
bring that up again" lifts it; "what have I told you to drop?" lists them.

SAFE: it only makes her quieter or lifts what the user waved off. A wave-off is only ever the user's: it isn't
recorded from a turn that read outside content (a page or an email could try to silence a deadline) or one that
isn't theirs.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import Field

from octavia.core.unprompted import Lasts
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_OFF = "I can't keep wave-offs here (this isn't my main conversation)."


class WaveOffArgs(ToolArgs):
    action: Literal["add", "lift", "list"] = Field(
        description=(
            "add: the user waved off something you brought up on your own ('shut up about that', 'stop bringing that "
            "up', 'drop it'): record its subject; lift: they said you can bring it up again; list: what's waved off."
        )
    )
    subject: str = Field(
        default="",
        max_length=200,
        description=(
            "add: what not to bring up, in your words, resolved from what you said on your own lately (e.g. 'offers "
            "to review missed ITCS 6150 quiz questions'): general enough to cover it from any source, no broader."
        ),
    )
    lasts: Lasts = Field(
        default="until lifted",
        description="add: how long, from their words ('for today', 'this week'); otherwise until lifted.",
    )
    said: str = Field(default="", max_length=300, description="add: the user's words, as they said them.")
    id: int | None = Field(default=None, ge=1, description="lift: the wave-off's number (from list).")


class WaveOffTool(Tool):
    name = "wave_off"
    description = (
        "When the user waves off something you brought up on your own (any phrasing, voice or text), record the "
        "subject so nothing of yours raises it unasked again; lift one when they say you can bring it up again; "
        "list them. Use it instead of a memory for this."
    )
    Args = WaveOffArgs
    risk = RiskLevel.SAFE
    category = "memory"

    async def run(self, args: WaveOffArgs, ctx: ToolContext) -> ToolResult:
        gate = ctx.extras.get("unprompted")
        if gate is None:
            return ToolResult.error(_OFF)
        if args.action == "list":
            return ToolResult.success(await asyncio.to_thread(gate.report, False))
        if args.action == "lift":
            return await _lift(gate, args)
        if not args.subject.strip():
            return ToolResult.error("What should I stop bringing up? add needs the subject.")
        turn = _turn(ctx)
        if turn is None:
            return ToolResult.error("Not recorded: only the user can wave something off, in their own turn.")
        if turn.outside_content:
            return ToolResult.error(
                "Not recorded: this turn read outside content (a page, a mail, the screen), and that mustn't decide "
                "what I keep quiet about. If the user said it, ask them to say it again."
            )
        until = gate.until(args.lasts)
        item = await asyncio.to_thread(
            gate.store.wave_off, args.subject, args.said, "phone" if turn.remote else "here", until
        )
        lasts = "until you say I can bring it up again" if until is None else args.lasts
        return ToolResult.success(
            f"Waved off as #{item.id}: {item.subject} ({lasts}). Nothing of mine brings it up unasked now.",
            data={"id": item.id},
        )


async def _lift(gate: Any, args: WaveOffArgs) -> ToolResult:
    active = await asyncio.to_thread(gate.store.active)
    wave_off_id = args.id
    if wave_off_id is None and len(active) == 1:
        wave_off_id = active[0].id  # "you can bring that up again" with one standing: that one
    if wave_off_id is None:
        if not active:
            return ToolResult.error("Nothing is waved off.")
        return ToolResult.error("Which one? lift needs its number:\n" + await asyncio.to_thread(gate.report, False))
    lifted = await asyncio.to_thread(gate.store.lift, wave_off_id)
    if lifted is None:
        return ToolResult.error(f"#{wave_off_id} isn't waved off now.")
    return ToolResult.success(f"Lifted #{lifted.id}: I can bring up {lifted.subject} again.")


def _turn(ctx: ToolContext) -> Any:
    get = ctx.extras.get("turn")
    if not callable(get):
        return None
    try:
        return get()
    except Exception:
        return None


TOOLS = [WaveOffTool]
