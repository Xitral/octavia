"""How loud her own voice is, apart from the system volume (octavia.audio.voice_volume).

"Talk louder", "speak a bit quieter", "set your voice to 150 percent": the change is heard from her very next word,
and kept for the next start. Above 100% the peaks are rounded off rather than clipped, so a boost stays clean.
SAFE: it only changes how loud she speaks, and saying the opposite undoes it.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field, model_validator

from octavia.audio.voice_volume import MAX_GAIN, SETTINGS_FILE, VoiceVolume
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

STEP_PERCENT = 25
MIN_PERCENT = 10  # "quieter" said a few times stops here instead of muting her
MAX_PERCENT = round(MAX_GAIN * 100)


class VoiceVolumeArgs(ToolArgs):
    percent: int | None = Field(
        None, ge=MIN_PERCENT, le=MAX_PERCENT, description="Your voice's volume, % of normal (100 = normal)."
    )
    change: Literal["louder", "quieter", "normal"] | None = Field(
        None, description=f"Louder/quieter step {STEP_PERCENT} points."
    )

    @model_validator(mode="after")
    def _check(self) -> VoiceVolumeArgs:
        if self.percent is not None and self.change is not None:
            raise ValueError("give either a percent or a change, not both")
        return self


def _percent(gain: float) -> int:
    return round(gain * 100)


class VoiceVolumeTool(Tool):
    name = "voice_volume"
    description = (
        "Your own speaking voice's volume, not the system's: set a percent (up to "
        f"{MAX_PERCENT}%) or louder/quieter/normal. No arguments reports it."
    )
    Args = VoiceVolumeArgs
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: VoiceVolumeArgs, ctx: ToolContext) -> ToolResult:
        volume = ctx.extras.get("voice_volume")
        if not isinstance(volume, VoiceVolume):  # no voice running: what's saved is used at the next start
            path = ctx.config.resolve_path(SETTINGS_FILE)
            volume = await asyncio.to_thread(VoiceVolume.load, path, ctx.config.audio.output_volume)
        current = _percent(volume.gain)
        if args.percent is None and args.change is None:
            return ToolResult.success(f"My voice is at {current}% of normal.", data={"percent": current})
        if args.percent is not None:
            target = args.percent
        elif args.change == "normal":
            target = 100
        elif args.change == "louder":
            target = max(current, min(MAX_PERCENT, current + STEP_PERCENT))
        else:
            target = min(current, max(MIN_PERCENT, current - STEP_PERCENT))
        if target == current and args.change in ("louder", "quieter"):
            limit = "loudest" if args.change == "louder" else "quietest"
            return ToolResult.success(f"My voice is already at its {limit}, {current}%.", data={"percent": current})
        percent = _percent(volume.set(target / 100))
        try:
            await asyncio.to_thread(volume.save)
        except OSError as exc:
            return ToolResult.success(
                f"My voice is at {percent}% now, but I couldn't save it for next time: {exc}", data={"percent": percent}
            )
        return ToolResult.success(f"My voice is at {percent}% now.", data={"percent": percent})


TOOLS = [VoiceVolumeTool()]
