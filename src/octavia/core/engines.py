"""Who answers and which voice speaks: picked by the user, switched while she runs, kept across restarts.

Three settings, each a choice in her settings window like any other (``data/settings.json``):

* ``brain.answers`` (who answers your turns): "auto", routed as ``local_brain.mode`` says (``localbrain/router.py``);
  "claude", only Claude, the local brain never answers (not even while Claude is down); "local", only the local
  brain, which says plainly what it can't do and never hands a turn to Claude. Background work keeps its own brains.
* ``tts.provider`` (her "Voice engine"): "local", her local voice with Fish Audio behind it; "local_only", her local
  voice alone; "fish_audio", Fish Audio alone. Started on her local voice (either way), the three switch live: the
  chain keeps both voices and ``FallbackTTS.only`` says which may speak. Started on Fish Audio, her local voice was
  never loaded, so going back to it waits for a restart.
* ``tts.voice_effect`` (her "Voice effect"): "off", or a preset that changes how her voice sounds on either engine
  (``audio/voice_effects.py``): radio, console, robot, room, deeper. Applies from her next reply.

``Engines`` is the one place they change: ``LiveSettings`` calls its hooks when the settings window changes one, and
``switch`` (the ``switch_engines`` tool, her quick commands "use Fish", "only use your local brain", "use your radio
voice", "back to normal", the tray's menu) saves the choice, has ``LiveSettings`` apply it at once, and says what really happened: a
pick that can't apply now (the local brain isn't running, her local voice wasn't loaded) is said so, never claimed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from octavia.audio import voice_effects
from octavia.tts.fallback import ONLY_BACKUP, ONLY_MAIN, FallbackTTS

log = logging.getLogger(__name__)

BRAIN_KEY = "brain.answers"
VOICE_KEY = "tts.provider"
EFFECT_KEY = "tts.voice_effect"
BRAINS = ("auto", "claude", "local")
VOICES = ("local", "local_only", "fish_audio")
# Which voice of her local chain may speak, for each ``tts.provider`` (``FallbackTTS.only``).
VOICE_PICK: dict[str, str | None] = {"local": None, "local_only": ONLY_MAIN, "fish_audio": ONLY_BACKUP}
# How each pick is shown (the tray, the settings window) and said.
BRAIN_NAMES = {"auto": "Automatic", "claude": "Claude only", "local": "Local brain only"}
VOICE_NAMES = {"local": "Automatic", "local_only": "Local voice only", "fish_audio": "Fish Audio only"}
_BRAIN_SAID = {
    "auto": "my brains are back on automatic",
    "claude": "only Claude answers now",
    "local": "only my local brain answers now",
}
_VOICE_SAID = {
    "local": "my voice is back on automatic, my local voice with Fish behind it",
    "local_only": "I'm speaking only with my local voice now",
    "fish_audio": "I'm speaking only with my Fish Audio voice now",
}


class Engines:
    """The brain and voice picks of a running assistant. ``brain`` is the brain she talks to (a ``RoutedBrain`` when
    the local brain answers turns), ``tts`` the voice she speaks with (or None without speech)."""

    def __init__(self, config: Any, brain: Any, tts: Any) -> None:
        self.config = config
        self.brain = brain
        self.tts = tts
        self.live: Any = None  # her ``LiveSettings``, once started: what applies a saved choice
        self.revoice: Callable[[], None] = lambda: None  # her preset phrases made again (the app sets it)
        self.fast_chat: Any = None  # her quick voice as made at startup, and the assistant holding it
        self.assistant: Any = None
        self.opener: Any = None  # her opening words as made at startup (``brain/opener.py``), None when off
        self.on_change: Callable[[], None] = lambda: None  # the tray draws its tip again
        # Her voice effect handed to what plays her voice (the app sets it: her speech and her preset phrases).
        self.set_effect: Callable[[str], None] = lambda _: None

    # ------------------------------------------------------------------ what can be picked
    @property
    def routed(self) -> bool:
        """The local brain is running and can answer turns (``local_brain.mode`` route or fallback_only)."""
        from octavia.localbrain.router import RoutedBrain

        return isinstance(self.brain, RoutedBrain)

    @property
    def local_voice(self) -> bool:
        """Her local voice is loaded, with Fish behind it: all three voice picks switch live."""
        from octavia.tts.local_voice import LocalVoiceTTS

        return isinstance(self.tts, FallbackTTS) and isinstance(self.tts.primary, LocalVoiceTTS)

    def why_not(self, key: str, value: str) -> str | None:
        """Why ``value`` can't apply to ``key`` now, in words, or None when it can."""
        if key == BRAIN_KEY and value == "local" and not self.routed:
            lb = self.config.local_brain
            if not lb.enabled:
                return "my local brain is turned off"
            if lb.mode == "shadow":
                return "my local brain is only practising in shadow mode, so it can't answer"
            return "my local brain isn't running"
        if key == VOICE_KEY and value != "fish_audio" and not self.local_voice:
            return "my local voice wasn't loaded when I started"
        if key in (VOICE_KEY, EFFECT_KEY) and self.tts is None:
            return "speech is off"
        return None

    # ------------------------------------------------------------------ what's on
    def brain_now(self) -> str:
        """Who answers right now: the pick, or what happens instead when it couldn't apply."""
        value = self.config.brain.answers
        if value == "local" and not self.routed:
            return "auto"
        return value

    def voice_now(self) -> str:
        if not self.local_voice:
            return "fish_audio" if self.tts is not None else "local"
        return self.config.tts.provider

    def effect_now(self) -> str:
        """Her voice effect right now ("off" without speech)."""
        return voice_effects.picked(self.config.tts.voice_effect) if self.tts is not None else voice_effects.OFF

    def changed(self) -> bool:
        """Something isn't on automatic, or a pick is kept: "back to normal" means something now."""
        if self.brain_now() != "auto" or (self.local_voice and self.voice_now() != "local"):
            return True
        if self.effect_now() != voice_effects.OFF:
            return True
        from octavia import user_settings

        saved = user_settings.read(self.config.root)  # started on a kept "Fish only": her local voice isn't loaded
        return BRAIN_KEY in saved or VOICE_KEY in saved or EFFECT_KEY in saved

    def summary(self) -> str:
        """The picks in a few words for her tray's tip ("Claude only, Fish Audio only"), or "" when both are
        automatic (or her local voice never loaded, which is no pick of today's)."""
        parts = []
        if self.brain_now() != "auto":
            parts.append(BRAIN_NAMES[self.brain_now()])
        if self.local_voice and self.voice_now() != "local":
            parts.append(VOICE_NAMES[self.voice_now()])
        if (effect := self.effect_now()) != voice_effects.OFF:
            parts.append(f"{voice_effects.NAMES[effect]} voice effect")
        return ", ".join(parts)

    def start_problems(self) -> str | None:
        """A pick saved before this start that can't apply now, said plainly (None when there's none)."""
        value = self.config.brain.answers
        why = self.why_not(BRAIN_KEY, value)
        if why is not None:
            return f"You set me to {BRAIN_NAMES[value].lower()}, but {why}, so I'm on automatic for now."
        return None

    # ------------------------------------------------------------------ hooks (``LiveSettings``)
    def apply_brain(self, value: str) -> bool:
        """``brain.answers`` changed (already in the config, read at each turn by the router): False when it can't
        apply now, and then it waits for a restart."""
        if self.why_not(BRAIN_KEY, value) is not None:
            return False
        if self.assistant is not None and self.fast_chat is not None:
            # Small talk to her quick voice (Haiku) would be Claude answering: not while she's set to local only.
            self.assistant.fast_chat = None if value == "local" else self.fast_chat
        if self.assistant is not None and self.opener is not None:
            self.assistant.opener = None if value == "local" else self.opener  # her opening words are Haiku's too
        log.info("Who answers: %s", BRAIN_NAMES[value])
        self.on_change()
        return True

    def apply_voice(self, value: str) -> bool:
        if self.why_not(VOICE_KEY, value) is not None:
            return value == "fish_audio" and not self.local_voice  # Fish alone is all there is: nothing to change
        self.tts.only = VOICE_PICK[value]
        self.revoice()  # "On it!" and the other presets in the voice now picked
        log.info("Her voice: %s", VOICE_NAMES[value])
        self.on_change()
        return True

    def apply_effect(self, value: str) -> bool:
        """``tts.voice_effect`` changed: heard from her next reply (and her next preset phrase)."""
        if self.tts is None:
            return value == voice_effects.OFF  # no voice: nothing to put it on
        value = voice_effects.picked(value)
        self.set_effect(value)
        log.info("Her voice effect: %s", voice_effects.NAMES[value])
        self.on_change()
        return True

    # ------------------------------------------------------------------ switching (her tool, quick commands, tray)
    async def switch(
        self, brain: str | None = None, voice: str | None = None, normal: bool = False, effect: str | None = None
    ) -> str:
        """Pick who answers, which voice speaks and/or her voice effect (``normal``: all back to what her config files
        say), keep it, apply it now, and say what really happened. Raises OSError when the choice can't be saved."""
        from octavia import user_settings

        root: Path = self.config.root
        local = self.config.sources[1] if len(getattr(self.config, "sources", ())) > 1 else None
        wanted: dict[str, Any] = {}
        refused: list[str] = []
        effect_before = self.effect_now()
        if normal:
            await asyncio.to_thread(user_settings.forget, root, [BRAIN_KEY, VOICE_KEY, EFFECT_KEY])
        else:
            if brain is not None and (why := self.why_not(BRAIN_KEY, brain)) is not None:
                # Not kept: a restart wouldn't change it, and she'd only say it can't apply at each start.
                refused.append(_sentence(f"I can't switch to {BRAIN_NAMES[brain].lower()}: {why}"))
            elif brain is not None:
                wanted[BRAIN_KEY] = brain
            if voice is not None and self.tts is None:
                refused.append("I can't pick a voice: speech is off.")
            elif voice is not None:
                wanted[VOICE_KEY] = voice  # her local voice not loaded: kept, and it applies after a restart
            if effect is not None and self.tts is None:
                refused.append("I can't put an effect on my voice: speech is off.")
            elif effect is not None:
                wanted[EFFECT_KEY] = voice_effects.picked(effect)
            if wanted:
                await asyncio.to_thread(user_settings.save, root, wanted, local)
        await self._apply_saved()
        said = self._said(wanted, normal, effect_before)
        return " ".join([*refused, said] if wanted or normal else refused) or "Nothing to change."

    async def _apply_saved(self) -> None:
        if self.live is not None:
            await self.live.reload()
            return
        # No live settings (tests, or it couldn't start): the saved choices go straight to the hooks.
        from octavia import user_settings

        hooks = ((BRAIN_KEY, self.apply_brain), (VOICE_KEY, self.apply_voice), (EFFECT_KEY, self.apply_effect))
        for key, apply in hooks:
            default = {BRAIN_KEY: "auto", VOICE_KEY: self.config.tts.provider, EFFECT_KEY: voice_effects.OFF}[key]
            value = user_settings.value(self.config.root, key, default)
            section, name = key.split(".")
            old = getattr(getattr(self.config, section), name)
            setattr(getattr(self.config, section), name, value)
            if not apply(value):
                setattr(getattr(self.config, section), name, old)

    def _said(self, wanted: dict[str, Any], normal: bool, effect_before: str = voice_effects.OFF) -> str:
        lines = []
        if normal:
            brain, voice = self.brain_now(), self.voice_now()
            done = brain == "auto" and (not self.local_voice or voice == "local")
            if done:
                lines.append("Back to normal: my brains are on automatic" + (
                    ", and my local voice speaks with Fish behind it." if self.local_voice else "."))  # fmt: skip
            else:
                lines.append(f"Back to what my settings files say: {BRAIN_NAMES[brain]} for my brain"
                             + (f", {VOICE_NAMES[voice]} for my voice." if self.local_voice else "."))  # fmt: skip
            if self.live is not None and VOICE_KEY in self.live.pending:
                lines.append("My local voice comes back after a restart: it wasn't loaded when I started.")
            if (effect := self.effect_now()) != effect_before:
                lines.append(_sentence(voice_effects.SAID[effect]))
            elif effect != voice_effects.OFF:
                name = voice_effects.NAMES[effect].lower()
                lines.append(_sentence(f"My settings files keep my {name} voice effect on"))
            return " ".join(lines)
        for key, value in wanted.items():
            if key == EFFECT_KEY:
                now = self.effect_now()
                if now == value:
                    lines.append(_sentence(f"Okay, {voice_effects.SAID[value]}"))
                else:
                    lines.append(_sentence(f"I saved that, but it didn't take, so {voice_effects.SAID[now]}"))
                continue
            now = self.brain_now() if key == BRAIN_KEY else self.voice_now()
            said = (_BRAIN_SAID if key == BRAIN_KEY else _VOICE_SAID)[value]
            if now == value:
                lines.append(_sentence(f"Okay, {said}"))
                if key == BRAIN_KEY and value == "local":
                    lines.append('Say "back to normal" to undo it.')
                continue
            why = self.why_not(key, value) or "it didn't take"
            lines.append(_sentence(f"I saved that, but {why}, so it applies after a restart"))
        return " ".join(lines)


def _sentence(text: str) -> str:
    return text[0].upper() + text[1:] + "."
