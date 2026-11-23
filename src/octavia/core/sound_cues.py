"""Sounds for what she's doing, so you can follow her by ear with your back to the screen.

| You hear | When |
|---|---|
| air rising into a chord | she's started and is ready |
| a soft loop | she's thinking about your request, before she starts on it (not while she speaks or listens); once she's working, only when she's gone quiet a while, or while she searches the web |
| a tiny blip | each click, typed text, key press and scroll she makes |
| a bright step up / a soft step down | she heard your yes / your no (or got no answer) to one of her questions |
| a curious rise | right before she asks you to approve something |
| a rising arpeggio | a task of several steps is done (only with ``audio.done_sound``, off by default) |
| a three-note sparkle | she speaks up on her own (a build is ready, a reminder) |
| two soft falling notes | she stopped listening |
| a quick slide down | the stop hotkey stopped her |
| a faint breath of air / two soft low notes | she started building an improvement to herself / the build failed (not in quiet hours) |
| a low buzz | something went wrong |

| a rising sparkle / a tiny tink / two soft falling glints | a pop-up panel opened / changed / closed (not in quiet hours or a focus session) |

The wake chime, the screenshot shutter, the timer ring and the mute hotkey's step down (or up) are played elsewhere.
All of them follow ``audio.earcons``; the loop also ``audio.working_sound``. They come from the pack ``audio.sound_pack``
names (``audio/earcons.py``, ``audio/ether_sounds.py``, ``audio/falcon_sounds.py``), so the table only roughly
describes them. Only the user's own turns sound: a scheduled job she runs on her own stays silent until it speaks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from octavia.config import READ_ONLY_BUILTINS
from octavia.core.events import (
    AnnouncementStarted,
    AssistantState,
    BrainFinished,
    BrainStarted,
    Dismissed,
    Event,
    EventBus,
    ImprovementChanged,
    Interrupted,
    NoSpeech,
    PermissionResolved,
    PhrasePlaying,
    SpeakingFinished,
    SpeakingStarted,
    StateChanged,
    ToolCallFinished,
    ToolCallStarted,
)

if TYPE_CHECKING:
    from octavia.audio.earcons import Earcons
    from octavia.config import AudioConfig
    from octavia.core.speech import SpeechOutput

log = logging.getLogger(__name__)

# The mouse and keyboard tools: a blip each time she uses them.
HANDS = frozenset({"click", "type_text", "press_keys", "scroll", "drag"})
# Answers to her question, as the tool executor reports them (``PermissionResolved.reason``).
YES = frozenset({"approved"})
NO = frozenset({"declined", "timed out"})
LOOP_DELAY_S = 0.6  # a quick answer needs no loop: it starts only once she's been at it a moment
# Once she's working (a tool has run in the turn), the blips say so: the loop comes back only after this long with
# nothing happening, to say she's still on it.
WORK_GAP_S = 12.0
# Claude's own web tools: one silent step of many seconds, with no blips to say she's on it, so the loop goes on.
WEB_STEPS = READ_ONLY_BUILTINS
COMPLETE_AFTER_S = 8.0  # "done" sounds for a task that took a while...
COMPLETE_STEPS = 2  # ...and took this many tool calls
# A self-improvement build starting, or failing, in the background (a build that's ready is announced: "notify").
BUILD_SOUNDS = {"building": "building", "failed": "build_failed"}


class SoundCues:
    def __init__(
        self, events: EventBus, speech: SpeechOutput, earcons: Earcons, audio: AudioConfig, quiet_hours: str = ""
    ) -> None:
        self.speech = speech
        self.quiet_hours = quiet_hours  # "HH:MM-HH:MM" (``scheduler.quiet_hours``): her builds make no sound then
        self._builds: dict[int, str] = {}  # each improvement's status, as last heard
        self.earcons = earcons
        self.audio = audio
        self._state = AssistantState.STARTING
        self._user_turn = False  # one of the user's turns runs (not a job of her own)
        self._speaking = False
        self._phrase_until = 0.0  # an acknowledgment ("On it!") plays until then
        self._looping = False
        self._pending: asyncio.TimerHandle | None = None
        self._steps = 0  # tool calls in the running turn
        self._working = False  # a tool has run in the running turn
        self._searching: set[str] = set()  # ids of the web steps under way
        self._last_activity = 0.0  # when she last did or said something, or started thinking
        events.subscribe(
            self._on_event,
            StateChanged,
            BrainStarted,
            BrainFinished,
            SpeakingStarted,
            SpeakingFinished,
            PhrasePlaying,
            ToolCallStarted,
            ToolCallFinished,
            PermissionResolved,
            AnnouncementStarted,
            NoSpeech,
            Dismissed,
            Interrupted,
            ImprovementChanged,
        )

    # ------------------------------------------------------------------ events
    def _on_event(self, event: Event) -> None:
        match event:
            case StateChanged(old=old, new=new):
                # Up and waiting for you. When her first act is to speak (news from before a restart) or answer
                # what was typed, that says she's up: no chord over it.
                if old is AssistantState.STARTING and new is AssistantState.IDLE:
                    self.play("online")
                self._state = new
                self._last_activity = time.monotonic()
            case BrainStarted(source=source):
                self._user_turn = source in ("voice", "text")
                self._steps = 0
                self._working = False
                self._searching.clear()
                self._last_activity = time.monotonic()
            case BrainFinished(error=error, interrupted=interrupted, duration_s=duration):
                self._searching.clear()
                if self._user_turn and error:
                    self.play("error")
                elif (
                    self._user_turn
                    and self.audio.done_sound
                    and not interrupted
                    and self._steps >= COMPLETE_STEPS
                    and (duration or 0.0) >= COMPLETE_AFTER_S
                ):
                    self.play("complete")
                self._user_turn = False
            case SpeakingStarted():
                self._speaking = True
            case SpeakingFinished():
                self._speaking = False
                self._last_activity = time.monotonic()
            case PhrasePlaying(seconds=seconds):
                self._phrase_until = time.monotonic() + seconds
            case ToolCallStarted(call_id=call_id, name=name):
                self._working = True
                self._last_activity = time.monotonic()
                if name in WEB_STEPS:
                    self._searching.add(call_id)
                if self._user_turn and name in HANDS:
                    self.play("tap")
            case ToolCallFinished(call_id=call_id):
                self._searching.discard(call_id)
                self._steps += 1
                self._last_activity = time.monotonic()
            case PermissionResolved(reason=reason):
                if reason in YES:
                    self.play("approved")
                elif reason in NO:
                    self.play("declined")
            case AnnouncementStarted():
                self.play("notify")
            case NoSpeech() | Dismissed():
                self.play("sleep")
            case Interrupted(reason=reason):
                if reason == "stop hotkey":
                    self.play("stopped")
            case ImprovementChanged(improvement_id=item, status=status):
                before = self._builds.get(item)
                self._builds[item] = status
                # A failure only for a build heard starting: at start-up she re-marks old ones as failed.
                wanted = status == "building" or (status == "failed" and before == "building")
                sound = BUILD_SOUNDS.get(status)
                if sound and wanted and before != status and not self._quiet_now():
                    self.play(sound)
        self._update_loop()

    # ------------------------------------------------------------------ sounds
    def _quiet_now(self) -> bool:
        from octavia.tools.extensions._scheduler import in_quiet_hours

        try:
            return in_quiet_hours(datetime.now(), self.quiet_hours)
        except ValueError:
            return False

    def play_ui(self, name: str) -> None:
        """A sound for something on the screen, not for her work (a pop-up panel opening): never in quiet hours or
        while a focus session or snooze holds her alerts."""
        if self._quiet_now():
            return
        try:
            from octavia.tools.extensions.focus import KEEPER

            if KEEPER.remaining() > 0:
                return
        except Exception:  # no focus module: nothing holds it back
            log.debug("Couldn't check for a focus session", exc_info=True)
        self.play(name)

    def play(self, name: str) -> None:
        if not self.audio.earcons:
            return
        sound = self.earcons.get(name)
        if sound is not None:
            self.speech.play(sound)

    def _loop_from(self) -> float | None:
        """When the loop may play (a ``time.monotonic()``), or None while it mustn't at all."""
        if not (
            self.audio.earcons
            and self.audio.working_sound
            and self._user_turn
            and self._state is AssistantState.THINKING
            and not self._speaking
        ):
            return None
        # A web search starts once she has thought it over: the loop that was playing then plays straight on.
        wait = 0.0 if self._searching else WORK_GAP_S if self._working else LOOP_DELAY_S
        return max(self._last_activity + wait, self._phrase_until)

    def _update_loop(self) -> None:
        due = self._loop_from()
        now = time.monotonic()
        if due is None or due > now:
            self._stop_loop()
        if due is None or self._looping:
            self._cancel_pending()
            return
        self._cancel_pending()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending = loop.call_later(max(0.0, due - now), self._start_loop)

    def _start_loop(self) -> None:
        self._pending = None
        due = self._loop_from()
        if due is None:
            return
        if due > time.monotonic() + 0.005:  # something happened since it was scheduled
            self._pending = asyncio.get_running_loop().call_later(due - time.monotonic(), self._start_loop)
            return
        sound = self.earcons.loop("working")
        if sound is None:
            return
        self._looping = True
        self.speech.loop(sound)

    def _stop_loop(self) -> None:
        if self._looping:
            self._looping = False
            self.speech.loop(None)

    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def close(self) -> None:
        self._cancel_pending()
        self._stop_loop()
