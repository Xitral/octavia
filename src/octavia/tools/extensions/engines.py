"""Who answers, which voice speaks and her voice effect, switched by asking (``octavia.core.engines``).

"Only use Claude", "only use your local brain", "use Fish", "use your local voice", "use your radio voice", "back to
normal": kept for the next start, applied at once, and what she says is what really happened (a pick that can't apply now is said so).
Her quick commands run the clear phrasings without a brain, so "back to normal" works on her local brain alone too.
SAFE: it only changes which of her own engines she uses and how her voice sounds, and "back to normal" undoes it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from octavia.audio import voice_effects
from octavia.core.engines import BRAIN_NAMES, VOICE_NAMES, Engines
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

# The tool's words for a voice, to ``tts.provider``'s.
_VOICES = {"auto": "local", "local": "local_only", "fish": "fish_audio"}


class SwitchEnginesArgs(ToolArgs):
    brain: Literal["auto", "claude", "local"] | None = Field(
        None, description="Who answers: auto (normal routing), claude (only Claude), local (only your local brain)."
    )
    voice: Literal["auto", "local", "fish"] | None = Field(
        None, description="Your voice: auto (local, Fish as backup), local (local only), fish (Fish Audio only)."
    )
    effect: Literal["off", "radio", "console", "robot", "room", "deeper"] | None = Field(
        None, description="An effect on your voice: radio, console (ship computer), robot, room (reverb), deeper, off."
    )
    normal: bool = Field(False, description="All back to normal (what the settings files say).")


class SwitchEnginesTool(Tool):
    name = "switch_engines"
    description = (
        "Switch who answers (Claude only, your local brain only, or automatic routing) and/or your voice (Fish Audio "
        "only, your local voice only, or automatic) and/or an effect on your voice, when the user asks: 'use Fish', "
        "'only use Claude', 'use your radio voice', 'back to normal'. Kept across restarts. No arguments reports "
        "what's on."
    )
    Args = SwitchEnginesArgs
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: SwitchEnginesArgs, ctx: ToolContext) -> ToolResult:
        engines = ctx.extras.get("engines")
        if not isinstance(engines, Engines):
            return ToolResult.error("I can't switch my brain or voice while running like this (no voice assistant).")
        now = {"brain": engines.brain_now(), "voice": engines.voice_now(), "effect": engines.effect_now()}
        if not args.normal and args.brain is None and args.voice is None and args.effect is None:
            return ToolResult.success(
                f"Brain: {BRAIN_NAMES[now['brain']]}. Voice: {VOICE_NAMES[now['voice']]}. "
                f"Voice effect: {voice_effects.NAMES[now['effect']]}.",
                data=now,
            )
        voice = _VOICES[args.voice] if args.voice is not None else None
        try:
            said = await engines.switch(brain=args.brain, voice=voice, normal=args.normal, effect=args.effect)
        except OSError as exc:
            return ToolResult.error(f"I couldn't save that, so nothing changed: {exc}")
        now = {"brain": engines.brain_now(), "voice": engines.voice_now(), "effect": engines.effect_now()}
        return ToolResult.success(said, data=now)


TOOLS = [SwitchEnginesTool()]
