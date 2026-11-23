"""Sending the desktop avatar out of the way and bringing her back: "hide", "go away, you're covering my screen",
"come back".

The bridge (``ctx.extras["avatar"]``, ``octavia.avatar.bridge.AvatarBridge``) does the work: sent away she leaves in
either mode and stays away, even while the user talks to the assistant, until they ask her back or the minutes they
gave are up. It only lasts while the assistant runs: a restart brings her back as configured.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from octavia.tools.base import NoArgs, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.timers import spoken_duration

log = logging.getLogger(__name__)

MAX_MINUTES = 24 * 60.0
NO_AVATAR = "The avatar isn't on (avatar.enabled in the config), so there's nothing to hide or show."


def _avatar(ctx: ToolContext) -> Any | None:
    """The running avatar bridge, when it can be sent away; else None."""
    avatar = ctx.extras.get("avatar")
    if avatar is None or not callable(getattr(avatar, "send_away", None)):
        return None
    return avatar


class HideAvatarArgs(ToolArgs):
    minutes: float | None = Field(
        default=None,
        gt=0,
        le=MAX_MINUTES,
        description="Only if the user gives a duration.",
    )


class HideAvatar(Tool):
    name = "hide_avatar"
    description = (
        "Hide the desktop avatar when the user wants her out of the way. She stays hidden, even while you "
        "talk, until asked back (show_avatar) or the minutes are up."
    )
    Args = HideAvatarArgs
    risk = RiskLevel.SAFE  # undone by show_avatar
    category = "avatar"

    async def run(self, args: HideAvatarArgs, ctx: ToolContext) -> ToolResult:
        avatar = _avatar(ctx)
        if avatar is None:
            return ToolResult.error(NO_AVATAR)
        try:
            problem = avatar.send_away(args.minutes)
        except Exception:
            log.warning("Sending the avatar away failed", exc_info=True)
            return ToolResult.error("The avatar didn't respond.")
        if problem:
            return ToolResult.error(problem)
        if args.minutes:
            until = f"for {spoken_duration(args.minutes * 60)}, then she comes back by herself"
        else:
            until = "until the user asks to see her again"
        return ToolResult.success(f"The avatar is hidden {until}.")


class ShowAvatar(Tool):
    name = "show_avatar"
    description = "Show the avatar again after hide_avatar ('come back')."
    Args = NoArgs
    risk = RiskLevel.SAFE
    category = "avatar"

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        avatar = _avatar(ctx)
        if avatar is None:
            return ToolResult.error(NO_AVATAR)
        try:
            problem = avatar.come_back()
        except Exception:
            log.warning("Bringing the avatar back failed", exc_info=True)
            return ToolResult.error("The avatar didn't respond.")
        if problem:
            return ToolResult.error(problem)
        return ToolResult.success("The avatar is back on screen.")


TOOLS = [HideAvatar(), ShowAvatar()]
