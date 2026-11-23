"""How she speaks for more than one sentence: whispering, slower or faster (octavia.tts.delivery).

"Whisper it", "say that slower", "talk a bit faster for a while": a tone cue only colours the sentence it starts, so
this sets the delivery every sentence is made with, for the rest of this reply or for the conversation (until the
user says to stop, or the conversation ends). Nothing is saved: after a restart she speaks normally.
SAFE: it only changes how she sounds, and saying the opposite undoes it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tts.delivery import MAX_SPEED, MIN_SPEED, Delivery, SpeakingStyle


class SpeakingStyleArgs(ToolArgs):
    speed: float | None = Field(
        None,
        ge=MIN_SPEED,
        le=MAX_SPEED,
        description="Pace, 1 = normal: 0.85 a bit slower, 0.7 slow, 1.2 faster.",
    )
    whisper: bool | None = Field(None, description="Whisper (true) or speak aloud again (false).")
    normal: bool = Field(False, description="Back to your normal pace and voice.")
    until: Literal["reply", "conversation"] = Field(
        "conversation",
        description="reply: only what you say this turn (a one-off line). conversation: until told otherwise.",
    )


def _lasts(ctx: ToolContext, until: str) -> Callable[[], object] | None:
    """What the style holds for: the running turn's id, or the conversation's; None when there's no telling."""
    turn = ctx.extras.get("turn")
    if until == "reply" and callable(turn):
        return lambda: getattr(turn(), "id", None)
    session = ctx.extras.get("session")
    return session if callable(session) else None


class SpeakingStyleTool(Tool):
    name = "speaking_style"
    description = (
        "Whisper and/or change your speaking pace for every sentence, not just one: for the rest of this reply or "
        "for the conversation. Settings left out stay as they are. No arguments reports it."
    )
    Args = SpeakingStyleArgs
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: SpeakingStyleArgs, ctx: ToolContext) -> ToolResult:
        style = ctx.extras.get("speaking_style")
        if not isinstance(style, SpeakingStyle):
            return ToolResult.error("My voice can't whisper or change its pace right now.")
        current = style.current()
        if args.normal:
            style.reset()
            return ToolResult.success("Back to my normal voice.", data=_data(style.current()))
        if args.speed is None and args.whisper is None:
            return ToolResult.success(f"I'm speaking {current.describe()}.", data=_data(current))
        wanted = Delivery(
            speed=current.speed if args.speed is None else args.speed,
            whisper=current.whisper if args.whisper is None else args.whisper,
        )
        delivery = style.set(wanted, _lasts(ctx, args.until))
        if delivery.normal:
            return ToolResult.success("Back to my normal voice.", data=_data(delivery))
        span = "for the rest of this reply" if args.until == "reply" else "until you tell me otherwise"
        return ToolResult.success(f"Speaking {delivery.describe()} {span}.", data=_data(delivery))


def _data(delivery: Delivery) -> dict[str, object]:
    return {"speed": delivery.speed, "whisper": delivery.whisper}


TOOLS = [SpeakingStyleTool()]
