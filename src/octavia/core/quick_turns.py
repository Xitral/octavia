"""Turns answered without the brain (mixed into ``Assistant``): a quick command run straight from a rule, a preset
reply, a screen task's stop, "say that again", and small talk in her quick voice."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from octavia.brain.fast_chat import Passed, Reply, small_talk
from octavia.core.events import (
    AssistantState,
    FastChatFinished,
    FastChatStarted,
    FastPathHandled,
    Notice,
    PresetReplied,
    SaidAgain,
)
from octavia.core.fast_path import carries_on, spoken_model
from octavia.core.quiet import asks_for_quiet, asks_for_talk
from octavia.core.say_again import asks_to_say_again
from octavia.core.speech import SpeechStream
from octavia.core.turn_types import _ASKS_WHO_ANSWERED, _ON_HOLD_NOTE, ListenMode, TurnResult
from octavia.core.yes_or_no import parse_confirmation
from octavia.emotion.tone import ToneJob
from octavia.tools.asked import questions_in, says_yes
from octavia.tts.shown import said_form

if TYPE_CHECKING:
    from octavia.core.quick_replies import QuickReply

log = logging.getLogger("octavia.core.assistant")  # part of the assistant: its log lines read as before


_BY_RULE = "no model at all: a quick-command rule answered it (the tool ran directly)"

_BY_PRESET = "no model at all: a preset reply from quick_replies.yaml"

_KEPT_SAID = 4  # what she said since the user last spoke: their answer is about the latest of it

_SAY_AGAIN_FOR_S = 600.0

_ASKED_FOR_S = 600.0  # a yes answers a question of hers from the last few minutes, not one from this morning  # "what did you say?" is about something she said lately, not this morning

_REPEAT_NOTE = "you said your last reply again, word for word, without a model, since the last turn"

_QUICK_NOTE = "handled as a quick command without you since the last turn (its tool already ran)"

# "Continue" while a screen task is paused: not media play (the quick command), but the task, through the brain.
_CARRY_ON_NOTE = "a screen task is paused"

_SMALL_TALK_NOTE = "small talk handled by your quick voice since the last turn"


def carry_on_note(text: str, paused: str | None, core: Callable[[str], str]) -> str | None:
    """The brain's note for a bare "continue" (``core``: ``FastPath.core``) while the screen task titled ``paused`` is
    paused: it's for the task, not the music. None otherwise. The eval gives its paused-task cases the same note."""
    if paused is None or not carries_on(core(text)):
        return None
    return (
        f'{_CARRY_ON_NOTE} (“{paused}”), and the user\'s "{text}" asks you to carry on with it, not the music: pick it '
        "back up with resume_screen_task"
    )


def paused_note(paused: str) -> str:
    """The brain's note while the screen task titled ``paused`` is paused and nothing runs: in her log "are you
    working?" and "why are you just sitting here?" each began with list_screen_tasks to find this out, a whole model
    round trip of silence (13 s median before her first word)."""
    return (
        f"{_CARRY_ON_NOTE} (“{paused}”) and nothing is running: no need to list the screen tasks to know that. If the "
        "user asks you to carry on with it, resume_screen_task picks it back up"
    )


class QuickTurnsMixin:
    """Part of ``Assistant`` (``octavia.core.assistant``), on its state."""

    async def _fast_command(self, text: str, speak: bool = True) -> tuple[str | None, tuple[str, ...]]:
        """A quick command answered with no brain call (``core/fast_path.py``): what was said, or None when it's not
        one (then the brain answers). A command whose tool failed goes to the brain too, with the failure as a
        note: she never just goes quiet. In "shadow" mode the rule is only logged."""
        mode = self.config.routing.fast_path
        if self.fast_path is not None and (note := carry_on_note(text, self._paused_work(), self.fast_path.core)):
            return None, (note,)
        command = self.fast_path.match(text) if self.fast_path is not None and mode != "off" else None
        if command is None:
            return None, ()
        if mode == "shadow":
            self.events.emit(FastPathHandled(text=text, rule=command.rule, tool=command.tool, shadow=True))
            return None, ()
        started = time.perf_counter()
        result = None
        if command.tool is not None:
            result = await self.brain.executor.execute(command.tool, dict(command.args))
        if result is not None and not result.ok:
            self.events.emit(
                FastPathHandled(
                    text=text, rule=command.rule, tool=command.tool, ok=False, duration_s=time.perf_counter() - started
                )
            )
            return None, (f"you tried this as a quick command ({command.tool}) and it failed: {result.content}",)
        said = command.say(result)
        if command.rule != "which_model":
            self._answered_by = _BY_RULE
        self.events.emit(
            FastPathHandled(
                text=text, rule=command.rule, tool=command.tool, said=said, duration_s=time.perf_counter() - started
            )
        )
        await self.sessions.touch()
        await self._record("user", text)
        await self._record("assistant", said)
        # The brain didn't see this turn: the next one tells it, so "make it ten minutes instead" makes sense.
        self._pending_notes.append(f'{_QUICK_NOTE}: the user said "{text}", and you answered "{said}"')
        if speak and self.speech_enabled and self.speech is not None:
            self._set_state(AssistantState.SPEAKING)
            audio = self.fast_path.audio(said) if self.fast_path is not None else None
            if audio is not None:
                await self.speech.say_prepared(said, audio)  # cached: instant
            else:
                await self.speech.say(said)
            self._spoke(said)
        return said, ()

    async def _say_stopped(self, line: str) -> None:
        """Say which screen task stopped, right away: no brain turn between the user's stop and her answer."""
        self.events.emit(Notice(source="APP", message=line))
        self._aside("assistant", line)
        if not self.speech_enabled or self.speech is None:
            return
        self._set_state(AssistantState.SPEAKING)
        await self.speech.say(line)
        self._spoke(line)

    async def _say_preset(self, reply: QuickReply, speak: bool = True) -> None:
        """Answer from personality/quick_replies.yaml: instant, and the brain is never called."""
        self._answered_by = _BY_PRESET
        self.events.emit(PresetReplied(text=reply.text, rule=reply.rule))
        self.sessions.keep_alive()
        if not speak or not self.speech_enabled or self.speech is None:
            return  # no voice: the console shows the reply
        self._set_state(AssistantState.SPEAKING)
        if reply.audio is not None:
            await self.speech.say_prepared(reply.text, reply.audio)
        else:
            await self.speech.say(reply.text)  # its audio isn't cached yet: synthesize it now
        self._spoke(reply.text)

    def last_line_at(self) -> float | None:
        """When (``time.time()``) the conversation's latest line was recorded, or None when nothing was yet."""
        return self._last_line_at

    def recent_lines(self) -> list[tuple[str, str]]:
        """This conversation's latest lines as (role, text), oldest first: every turn whoever answered it (Claude,
        the local brain, a quick command). The local brain is shown these, so both brains hear one conversation."""
        session = self.sessions.session_id
        return [(role, text) for s, role, text in self._said if s == session]

    def busy_with(self) -> str | None:
        """What the user's next words may be about that only the brain with the screen can see, or None: a screen
        task's question waiting for its answer, a task running, or one paused ("continue" carries on with it)."""
        if self._open_question is not None or self._held_question() is not None:
            return "a screen task's question"
        if self._work_running():
            return "a screen task is running"
        if (paused := self._paused_work()) is not None:
            return f"a screen task is paused ({paused})"
        return None

    def _last_reply(self) -> str | None:
        """What she last replied in this conversation, or None when she hasn't replied in it yet."""
        session = self.sessions.session_id
        return next((t for s, role, t in reversed(self._said) if s == session and role == "assistant"), None)

    def _spoke(self, text: str | None, trusted: bool = True) -> None:
        """What she last said out loud here, all of it, for "say that again": a reply, a preset, an announcement.
        None for one that was cut off before it was all there (then the brain answers), or spoken nowhere."""
        self._last_spoken = (time.monotonic(), text) if text else None
        if text:
            self._put(text, trusted)

    def _put(self, text: str, trusted: bool = True) -> None:
        """Something she said or showed the user whole: what their next answer may be a yes to (``offered``).
        ``trusted``: False for a reply of a turn that took in outside content or wasn't the user's."""
        text = said_form(text).strip()  # her words, not a subtitle's [[shown|said]] mark
        if text and (not self._put_to_user or self._put_to_user[-1][1] != text):
            self._put_to_user.append((time.monotonic(), text, trusted))
            del self._put_to_user[:-_KEPT_SAID]

    def _still_asked(self) -> tuple[str, ...]:
        """What she said to the user lately that their answer may be a yes to: the last thing of hers that asked them
        something, if it's recent and can be trusted. Asking something newer replaces what she asked before."""
        now = time.monotonic()
        for at, text, trusted in reversed(self._put_to_user):
            if questions_in([text]):
                return (text,) if trusted and now - at <= _ASKED_FOR_S else ()
        return ()

    def _to_say_again(self) -> str | None:
        if self._last_spoken is None or time.monotonic() - self._last_spoken[0] > _SAY_AGAIN_FOR_S:
            return None
        return self._last_spoken[1]

    async def _say_again(self, text: str, core: str, source: str) -> TurnResult | None:
        """ "Say that again": her last reply, word for word, with no model at all (``core/say_again.py``).

        None when it isn't one, or there's nothing to repeat yet (then the brain answers). Talking over it cuts it off,
        as with any reply; the turn goes into the transcript, and the brain's next turn is told it was repeated.
        """
        if not asks_to_say_again(core):
            return None
        said = self._to_say_again()
        if said is None:
            return None
        await self.sessions.touch()
        self._pending_notes.append(f'{_REPEAT_NOTE}: the user said "{text}"')
        log.info("Said her last words again, with no model: %r", text)
        self.events.emit(SaidAgain(text=text))
        if source != "voice" or not self.speech_enabled or self.speech is None:
            await self._record("user", text)
            await self._record("assistant", said)
            return TurnResult(text=said)
        self._set_state(AssistantState.SPEAKING)
        self._interrupt = asyncio.Event()
        self._barge_kind = None
        self._turn_active = True
        self._enter_barge_mode()
        stream = self.speech.begin()
        self._active_stream = stream
        cut_in = asyncio.create_task(self._interrupt.wait(), name="say-again-interrupt")
        try:
            stream.feed(said)
            stream.finish()
            speaking = asyncio.create_task(stream.wait(), name="say-again")
            await asyncio.wait({speaking, cut_in}, return_when=asyncio.FIRST_COMPLETED)
            if not speaking.done():  # cut off
                stream.cancel()
                speaking.cancel()
                await asyncio.gather(speaking, return_exceptions=True)
        except asyncio.CancelledError:
            stream.cancel()
            raise
        finally:
            cut_in.cancel()
            self._turn_active = False
            self._active_stream = None
            if self._mode is ListenMode.BARGE:
                self._mode = ListenMode.OFF
        outcome = stream.outcome()
        interrupted = self._interrupt.is_set() and outcome.interrupted
        await self._record("user", text)
        await self._record("assistant", said, interrupted=interrupted)
        return TurnResult(text=said, interrupted=interrupted, barge_in=self._barge_kind)

    def _chat_history(self) -> list[tuple[str, str]]:
        """The last ``fast_chat.context_turns`` lines of this conversation, oldest first."""
        n = self.config.fast_chat.context_turns
        session = self.sessions.session_id
        lines = [(role, text) for s, role, text in self._said if s == session]
        return lines[-n:] if n > 0 else []

    def _small_talk_fits(self, text: str, source: str) -> bool:
        """Whether this turn of the user's may try the quick voice: short chit-chat (``fast_chat.small_talk``) said or
        typed here, with nothing going on that only the brain knows about."""
        cfg = self.config.fast_chat
        if self.fast_chat is None or not cfg.enabled or source not in ("voice", "text"):
            return False
        if not small_talk(text, cfg.max_words) or _ASKS_WHO_ANSWERED.search(text):
            return False
        # Her own work comes first: a screen task beside the conversation, an offer waiting for its yes, a request she
        # was cut off in the middle of, or working quietly in this conversation (and asking for it, or to stop).
        if self._work_running() or self._offers or _ON_HOLD_NOTE in self._pending_notes:
            return False
        if self.circles is not None and self.circles.live() is not None:
            return False  # they just circled something on the screen: only the brain gets to see it
        if says_yes(text) and self._still_asked() and not self.on_call:
            return False  # a yes to what she just asked: the brain does it (``offered``), the quick voice can't
        session = self.sessions.session_id
        # Right after a reply in which she did something (changed a setting, looked something up), what the user says
        # is likely about it ("you're still too quiet"): the quick voice, with no tools, can only talk about it.
        if self._worked_last is not None and self._worked_last == session:
            return False
        if (self._quiet_session is not None and self._quiet_session == session) or asks_for_quiet(text):
            return False
        if asks_for_talk(text):
            return False
        # A yes or no to the question she just asked ("want me to put it on?") is for the brain, which asked it.
        last = self._last_reply()
        return not (last is not None and last.rstrip().endswith("?") and parse_confirmation(text) is not None)

    async def _small_talk(self, text: str, source: str, tone: ToneJob | None = None) -> TurnResult | None:
        """Small talk answered by her quick voice (``brain/fast_chat.py``): Haiku with no tools, in about half a second.

        None when it isn't for the quick voice or the quick voice passed (it handed off, timed out or failed): nothing
        was said, and the caller runs the brain's turn as usual. Otherwise the reply is spoken like the brain's, and
        talking over it cuts it off; the turn goes into the transcript, "which model was that?" names the quick voice's
        model, and the brain's next turn is told what was said, so it knows the conversation.
        """
        if not self._small_talk_fits(text, source):
            return None
        assert self.fast_chat is not None
        await self.sessions.touch()
        history = self._chat_history()
        started = time.perf_counter()
        self._turn_first_audio = None
        self._set_state(AssistantState.THINKING)
        self._prewarm_tts()
        # From here on the user can cut in, as in a brain turn: the wake phrase, the talk or stop hotkey, talking over.
        self._interrupt = asyncio.Event()
        self._barge_kind = None
        self._turn_active = True
        self._enter_barge_mode()
        stream: SpeechStream | None = None
        reply: Reply | None = None
        asking: asyncio.Task[Reply | Passed] | None = None
        cut_in = asyncio.create_task(self._interrupt.wait(), name="small-talk-interrupt")
        try:
            asking = asyncio.create_task(self.fast_chat.reply(text, history, source=source), name="small-talk")
            await asyncio.wait({asking, cut_in}, return_when=asyncio.FIRST_COMPLETED)
            if not asking.done():  # cut in before it said anything: what they say now is the turn
                asking.cancel()
                await asyncio.gather(asking, return_exceptions=True)
                self.events.emit(FastChatFinished(text=text, source=source, passed="interrupted", interrupted=True))
                return TurnResult(interrupted=True, barge_in=self._barge_kind)
            try:
                got = asking.result()
            except Exception as exc:
                log.warning("The quick voice failed", exc_info=True)
                got = Passed("error", f"{type(exc).__name__}: {exc}", time.perf_counter() - started)
            if isinstance(got, Passed):
                log.info(
                    "Quick voice passed (%s%s) after %.2fs: the brain answers",
                    got.reason,
                    f": {got.detail}" if got.detail else "",
                    got.elapsed_s,
                )
                self.events.emit(
                    FastChatFinished(
                        text=text,
                        source=source,
                        passed=got.reason,
                        duration_s=got.elapsed_s,
                        first_word_s=got.first_word_s,
                    )
                )
                return None
            reply = got
            self._learn_tone(tone)
            self.events.emit(FastChatStarted(text=text, source=source, first_word_s=reply.first_word_s))
            stream = self.speech.begin() if self.speech_enabled and self.speech is not None else None
            self._active_stream = stream

            async def pump() -> None:
                assert reply is not None
                if stream is not None:
                    stream.feed(reply.opening)
                async for piece in reply.rest():
                    if stream is not None:
                        stream.feed(piece)
                if stream is not None:
                    stream.finish()
                    await stream.wait()

            speaking = asyncio.create_task(pump(), name="small-talk-pump")
            await asyncio.wait({speaking, cut_in}, return_when=asyncio.FIRST_COMPLETED)
            if not speaking.done():  # cut off
                if stream is not None:
                    stream.cancel()
                reply.cancel()
                speaking.cancel()
                await asyncio.gather(speaking, return_exceptions=True)
            else:
                await speaking
        except asyncio.CancelledError:
            if asking is not None:
                asking.cancel()
            if stream is not None:
                stream.cancel()
            if reply is not None:
                reply.cancel()
            raise
        finally:
            cut_in.cancel()
            self._turn_active = False
            self._active_stream = None
            if self._mode is ListenMode.BARGE:
                self._mode = ListenMode.OFF

        said = reply.text
        outcome = stream.outcome() if stream is not None else None
        interrupted = self._interrupt.is_set() and (outcome.interrupted if outcome else True)
        heard = said
        if interrupted:
            heard = outcome.spoken_text if outcome else ""
            if outcome and outcome.partial:
                heard = (heard + " " + outcome.partial).strip()
            heard = f"{heard} [cut off there]" if heard else "nothing: they cut in before you spoke"
        await self._record("user", text)
        await self._record("assistant", said, interrupted=interrupted)
        if stream is not None:
            self._spoke(None if interrupted else said)
        # The brain didn't see this turn: its next one is told, so it knows where the conversation is.
        self._pending_notes.append(f'{_SMALL_TALK_NOTE}: the user said "{text}", and you answered "{heard}"')
        model = reply.model
        self._answered_by = spoken_model(model) if model else "a small model (your quick voice; it didn't say which)"
        duration = time.perf_counter() - started
        self.events.emit(
            FastChatFinished(
                text=text,
                source=source,
                said=said,
                model=model,
                interrupted=interrupted,
                duration_s=duration,
                first_word_s=reply.first_word_s,
                usage=reply.usage,
            )
        )
        parts = [f"first word {reply.first_word_s:.2f}s" if reply.first_word_s is not None else "no words"]
        if self._turn_first_audio is not None:
            parts.append(f"reply audible {self._turn_first_audio - started:.2f}s")
        message = f"Timing (quick voice, {self._answered_by}): " + ", ".join(parts)
        log.info(message)
        self.events.emit(Notice(source="BRAIN", message=message, level=logging.DEBUG))
        # Something of it was said whatever went wrong after (``reply.error``): the conversation carries on.
        return TurnResult(text=said, interrupted=interrupted, barge_in=self._barge_kind)
