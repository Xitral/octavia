"""Which voice she spoke in: her Fish Audio voice or the Windows backup (octavia.tts.fallback), and why; or, with
her local voice in front (``tts.provider: local``), which of local, Fish and Windows.

"Was that your real voice?", "why do you sound different?": the backup takes over without a pause when Fish Audio
fails, so only this can tell which voice the last sentence was heard in, which Fish model and voice she's set to
(her quick phrases are made with that same one), and Fish Audio's last error.
SAFE: it only reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from octavia.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from octavia.tts.fallback import BACKUP, MAIN, ONLY_BACKUP, ONLY_MAIN, FallbackTTS, voices
from octavia.tts.fish_audio import FishAudioTTS
from octavia.tts.local_voice import LocalVoiceTTS


def _fish(fish: FishAudioTTS) -> str:
    voice = f"voice {fish.reference_id}" if fish.reference_id else "the model's default voice"
    return f"Fish Audio model {fish.cfg.model}, {voice}"


class VoiceEngineTool(Tool):
    name = "voice_engine"
    description = (
        "Which voice engine you're speaking with: your local voice, Fish Audio or the Windows backup, which one said "
        "your last sentence, the Fish model and voice id, and why your voice last failed."
    )
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        tts = ctx.extras.get("tts")
        if tts is None:
            return ToolResult.error("I'm not speaking with any voice right now: speech output is off.")
        if isinstance(tts, FallbackTTS) and isinstance(tts.primary, LocalVoiceTTS):
            return _local_first(tts)
        fish = getattr(tts, "primary", tts)
        main = _fish(fish) if isinstance(fish, FishAudioTTS) else f"the {getattr(fish, 'name', 'main')} voice"
        if not isinstance(tts, FallbackTTS):
            return ToolResult.success(
                f"I speak only with {main}; there's no backup voice.", data={"engine": MAIN, "backup": False}
            )
        lines = [f"My voice is {main}; my quick phrases are made with it too, never with the backup."]
        if tts.last_engine == BACKUP:
            lines.append("My last sentence was said by the Windows backup voice.")
        elif tts.last_engine == MAIN:
            lines.append("My last sentence was said in my Fish Audio voice.")
        if tts.backup is None:
            lines.append("There's no backup voice this time.")
        elif tts.on_backup:
            lines.append(f"The backup is speaking for about {tts.backup_left_s():.0f} s more.")
        if tts.last_failure:
            at = datetime.fromtimestamp(tts.last_failure_at).strftime("%H:%M")
            lines.append(f"Fish Audio last failed at {at}: {tts.last_failure}")
        else:
            lines.append("Fish Audio hasn't failed since I started.")
        data = {"engine": tts.last_engine or None, "on_backup": tts.on_backup, "last_failure": tts.last_failure}
        return ToolResult.success(" ".join(lines), data=data)


def _local_first(tts: FallbackTTS) -> ToolResult:
    """Her local voice in front, Fish Audio behind it, then (usually) the Windows voice."""
    local: LocalVoiceTTS = tts.primary  # type: ignore[assignment]
    chain = voices(tts)
    fish = next((v for v in chain if isinstance(v, FishAudioTTS)), None)
    lines = [
        f"My voice is made on this PC (Qwen3-TTS on the GPU, cloned from {local.voice.name}); my quick phrases are made with "
        "it too. "
        + (f"Behind it is {_fish(fish)}" if fish is not None else "Behind it is Fish Audio")
        + (", then the Windows voice." if len(chain) > 2 else ".")
    ]
    spoke, level, names = "", tts, ("my local voice", "Fish Audio", "the Windows voice")
    for name, below in zip(names, names[1:], strict=False):  # follow the chain down to the voice that spoke last
        if not isinstance(level, FallbackTTS) or not level.last_engine:
            break
        if level.last_engine == MAIN:
            spoke = name
            break
        level = level.backup
        if level is not None and not isinstance(level, FallbackTTS):
            spoke = below
    if tts.only == ONLY_BACKUP:
        lines.append("You set me to Fish Audio only, so my local voice isn't speaking.")
    elif tts.only == ONLY_MAIN:
        lines.append("You set me to my local voice only: Fish Audio never takes over, so if it fails I go quiet.")
    if spoke:
        lines.append(f"My last sentence was said by {spoke}.")
    if local.unloaded_for_game:
        lines.append("My local voice is unloaded while a game is full screen; Fish Audio speaks until it's over.")
    elif not local._ready.is_set():
        lines.append("My local voice is still loading" + (f" or failed: {local.start_error}" if local.start_error else
                                                           "; Fish Audio speaks until it's ready."))  # fmt: skip
    if tts.on_backup and tts.only != ONLY_BACKUP:
        lines.append(f"Fish Audio is speaking for about {tts.backup_left_s():.0f} s more.")
    level = tts
    while isinstance(level, FallbackTTS):
        if level.last_failure:
            at = datetime.fromtimestamp(level.last_failure_at).strftime("%H:%M")
            name = "My local voice" if level is tts else level.main_label
            lines.append(f"{name} last failed at {at}: {level.last_failure}")
        level = level.backup
    if not tts.last_failure:
        lines.append("My local voice hasn't failed since I started.")
    data = {"engine": spoke or None, "on_backup": tts.on_backup, "last_failure": tts.last_failure,
            "local_ready": local._ready.is_set(), "only": tts.only}  # fmt: skip
    return ToolResult.success(" ".join(lines), data=data)


TOOLS = [VoiceEngineTool()]
