"""How far other apps are turned down while she speaks (octavia.audio.ducking), which are left alone, or not at all.

The setting lives in ``data/ducking.json`` rather than the config files, so the user can change it by asking ("keep
the music a bit louder when you talk", "stop lowering Discord"). The ducker reads it each time she starts speaking.
SAFE: it only changes how loud other apps play under her voice, and saying the opposite undoes it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import Field

from octavia.audio.ducking import ROUTERS, SETTINGS_FILE, DuckSettings, app_name
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult


class AudioDuckingArgs(ToolArgs):
    enabled: bool | None = Field(None)
    level_percent: int | None = Field(
        None,
        ge=0,
        le=100,
        description="Other apps' volume while you speak, % of normal (0 = silent).",
    )
    leave_alone: str | None = Field(None, description="App never to turn down, by process name (e.g. Discord.exe).")
    duck_again: str | None = Field(None, description="App to turn down again after leave_alone.")


def _describe(settings: DuckSettings) -> str:
    if not settings.enabled:
        return "Other apps aren't turned down while I speak."
    text = f"While I speak, other apps play at {round(settings.level * 100)}% of their volume."
    if settings.skip:
        text += " Left alone: " + ", ".join(sorted(settings.skip)) + "."
    return text


def _data(settings: DuckSettings) -> dict[str, Any]:
    return {"enabled": settings.enabled, "level": settings.level, "skip": sorted(settings.skip)}


class AudioDucking(Tool):
    name = "audio_ducking"
    description = (
        "Ducking lowers other apps while you speak. Set whether, how far and which apps to leave alone; "
        "no arguments reports the setting."
    )
    Args = AudioDuckingArgs
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: AudioDuckingArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.config.resolve_path(SETTINGS_FILE)
        settings = await asyncio.to_thread(DuckSettings.load, path)
        leave = app_name(args.leave_alone or "")
        again = app_name(args.duck_again or "")
        if args.enabled is None and args.level_percent is None and not leave and not again:
            return ToolResult.success(_describe(settings), data=_data(settings))
        if args.enabled is not None:
            settings.enabled = args.enabled
        if args.level_percent is not None:
            settings.level = args.level_percent / 100
            if args.enabled is None:
                settings.enabled = True  # asking for a level means wanting it
        if leave:
            settings.skip = sorted({*settings.skip, leave})
        if again:
            settings.skip = [s for s in settings.skip if s != again]
        try:
            await asyncio.to_thread(settings.save, path)
        except OSError as exc:
            return ToolResult.error(f"Couldn't save the setting: {exc}")
        text = "Saved. " + _describe(settings)
        if again in ROUTERS:
            text += f" ({again} carries my own voice too, so it's never turned down.)"
        return ToolResult.success(text, data=_data(settings))


TOOLS = [AudioDucking()]
