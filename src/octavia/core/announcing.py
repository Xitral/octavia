"""What she says or does unprompted (mixed into ``Assistant``): announcements, scheduled turns and turns texted
from the user's phone wait in one line and are taken up only when nobody is talking with her."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

from octavia.core.approvals import _MIC_SILENT
from octavia.core.chat import SOURCE as _WINDOW
from octavia.core.chat import ChatLink
from octavia.core.events import AnnouncementStarted, AssistantState, Notice
from octavia.core.hush import asks_for_hush, keeps_hush
from octavia.core.stopping import StopKind, holds, stop_kind
from octavia.core.turn_types import (
    _REMOTE_SOURCES,
    _ROUTED_SOURCES,
    Announcement,
    BackgroundTurn,
    ListenMode,
    TurnResult,
    WorkQuestion,
    _new_future,
    _resolve,
)
from octavia.core.unprompted import ASKED, INITIATIVE, REPLY, STATUS, Kind, Line, Verdict
from octavia.core.yes_or_no import plain_answer, yes_and_more
from octavia.tools.asked import questions_in
from octavia.tools.permissions import ConfirmationRequest, Offer

log = logging.getLogger("octavia.core.assistant")  # part of the assistant: its log lines read as before


# An answer to a screen task's question, a sentence at a time: "What are you doing? Stop!" has a stop in it.
_SENTENCES = re.compile(r"(?<=[.!?;])\s+")
# How often a held question looks for her chat window having opened, to put its Yes and No there.
_WINDOW_CHECK_S = 1.0


def _answered(future: asyncio.Future[bool]) -> bool:
    """Whether a question's future ended with an answer: not taken back, nor left unanswered (``NotAnswered``)."""
    return future.done() and not future.cancelled() and future.exception() is None


# Stop words that, said to her question, decline only the step it's about.
_DECLINES_STEP = frozenset({"cancel", "cancel that", "cancel it"})

_ANNOUNCED_NOTE = "you announced this on your own since the last turn (it's current; it supersedes older status)"
_FACTS_CHARS = 800  # of a turn of her own's note, shown to the unprompted gate's judge
_LEAVE_OUT = "Leave out {why}: the user waved that off or it no longer holds. Say only the rest."
_REWORD_TURN = "(On my own) Time to say something."
_REWORD_NOTE = (
    'This is due now ({source}), and the user asked for it, so say it: "{text}". But leave out {why}: the user '
    "waved that off or it no longer holds. Say the rest in a sentence or two of your own."
)
# After "shut up": what she'd bring up unasked waits until the user talks to her again, or this long at most.
HUSH_S = 30 * 60.0


class AnnouncingMixin:
    """Part of ``Assistant`` (``octavia.core.assistant``), on its state."""

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    @property
    def idle(self) -> bool:
        """Waiting for the wake phrase with nothing to say: a moment when restarting interrupts nobody."""
        return (
            not self._turn_active
            and not self._announcing
            and self._mode is ListenMode.WAKE
            and self._wake_waiter is not None
            and not self._wake_waiter.done()
            and self._announcements.empty()
            and not self._gating
            and not self._hushed_lines
        )

    @property
    def announcing(self) -> bool:
        """Saying an announcement, or holding one to say: a restart now would cut it off."""
        # Lines held while the user wants her quiet count: a restart now would lose them (at most ``HUSH_S``).
        return self._announcing or not self._announcements.empty() or bool(self._gating) or bool(self._hushed_lines)

    def note(self, text: str) -> None:
        """Context for the brain's next turn (e.g. "you just restarted"), without saying anything."""
        self._pending_notes.append(text)

    def announce(
        self,
        text: str,
        note: str | None = None,
        listen: bool = True,
        cue: str | None = None,
        offer: Offer | None = None,
        relay: bool = True,
        full: str | None = None,
        *,
        kind: Kind = STATUS,
        source: str = "app",
        urgent: bool = False,
        facts: str = "",
    ) -> None:
        """Say something unprompted (e.g. "the weather tool is ready") as soon as it's idle.

        ``note`` goes to the brain with the user's next turn, so it knows what was just said.
        With ``listen``, a follow-up window opens afterwards so the user can answer without
        the wake phrase. ``cue`` says what kind of announcement it is (``timer``, ``reminder``,
        ``morning``), for the avatar (``AnnouncementStarted``). ``offer``: the one call it offers to make ("Want me
        to start it again?"); a plain yes in the next turn approves it (``offered``), and every other one offered
        since the last turn. Call on the event loop thread.
        Before the voice loop has started (e.g. "I'm back" right after a restart) it's queued
        and said as soon as it runs. ``relay``: it may also be texted to the user's phone (``self.relay``); False
        for what came from the phone in the first place (its own notifications, mirrored by Phone Link). ``full``:
        the whole of it when ``text`` is only what's said of it (a long report): it's what's shown and texted.

        ``kind``, ``source``, ``urgent`` and ``facts`` are for the one judgment every unprompted line passes first
        (``self.unprompted``, ``core/unprompted.py``): who wanted it said (``ASKED`` by the user: never dropped;
        ``STATUS``; ``INITIATIVE``: her own idea), where it comes from, whether it can't wait, and what bears on it.
        """
        full = full if full and full != text else None
        line = Line(text=full or text, source=source, kind=kind, urgent=urgent, facts=facts)

        def say() -> None:
            self._announce_now(text, note, listen, cue, offer, relay, full)

        def reword(why: str) -> None:
            self._say_reworded(line, note, cue, relay, why)

        self._through_gate(line, say, reword)

    def _announce_now(
        self,
        text: str,
        note: str | None,
        listen: bool,
        cue: str | None,
        offer: Offer | None,
        relay: bool,
        full: str | None,
    ) -> None:
        if relay and self.relay is not None:
            try:
                self.relay(full or text, cue)
            except Exception:
                log.warning("Couldn't pass the announcement on to the phone", exc_info=True)
        starting_voice = self.voice_enabled and self.state is AssistantState.STARTING
        if not self._running and not starting_voice:  # text chat (or stopped): show it now
            self.events.emit(Notice(source="APP", message=full or text))
            self._remember_announcement(full or text, note, offer)
            return
        self._enqueue(Announcement(text=text, note=note, listen=listen, cue=cue, offer=offer, full=full))

    def run_in_background(
        self, text: str, note: str | None = None, speak: bool = True, cue: str | None = None
    ) -> asyncio.Future[TurnResult]:
        """Run ``text`` through the brain as a turn of its own (a scheduled job) once nobody is talking with her.

        It waits in line with the announcements, so it never cuts into a conversation, and runs like any turn:
        the reply is spoken (unless ``speak`` is False: then only the returned result has it), the user can cut in,
        and a follow-up window opens after it. It isn't the user's own turn, so every risky call is asked (see
        ``unattended``). ``cue``: what kind of remark of hers it is ("morning"), for the avatar as it starts, as an
        announcement's. The future resolves with the turn's result; it fails when the voice loop isn't running.
        """
        return self._queue_turn(BackgroundTurn(text=text, note=note, speak=speak, result=_new_future(), cue=cue))

    def tell_in_own_words(
        self,
        turn: str,
        note: str,
        fallback: str,
        fallback_note: str | None = None,
        cue: str | None = None,
        relay: bool = True,
        *,
        kind: Kind = INITIATIVE,
        source: str = "app",
        urgent: bool = False,
        facts: str = "",
    ) -> None:
        """Bring something up on her own, in her own words: ``turn`` and ``note`` (the facts, and what to make of
        them) go to the brain as a turn of her own (``run_in_background``), so what she says comes from her, with
        what she knows of the user, instead of a sentence written in code. ``fallback`` is that sentence: announced
        as before (with ``fallback_note``) when her turn can't run (no voice loop: typed chat) or breaks. Her reply
        is texted to the phone as an announcement is (``self.relay``), unless ``relay`` is False: said at the PC only.
        It passes the unprompted gate first, as ``announce`` does (``kind`` and the rest: see there; ``facts``
        defaults to ``note``). Call on the event loop thread."""
        line = Line(text=fallback, source=source, kind=kind, urgent=urgent, facts=facts or note[:_FACTS_CHARS])

        def say() -> None:
            self._tell_now(turn, note, fallback, fallback_note, cue, relay)

        def reword(why: str) -> None:
            self._tell_now(turn, f"{note}\n{_LEAVE_OUT.format(why=why)}", fallback, fallback_note, cue, relay)

        self._through_gate(line, say, reword)

    def _tell_now(
        self, turn: str, note: str, fallback: str, fallback_note: str | None, cue: str | None, relay: bool
    ) -> None:
        def canned() -> None:
            self._announce_now(fallback, fallback_note, True, cue, None, relay, None)  # already past the gate

        try:
            pending = self.run_in_background(turn, note, cue=cue)
        except Exception:
            log.warning("Couldn't give her %r as a turn of her own", turn, exc_info=True)
            canned()
            return

        def done(result: asyncio.Future[TurnResult]) -> None:
            if result.cancelled():
                return
            if (exc := result.exception()) is not None:
                log.info("Said %r as written: her own turn couldn't run (%s)", turn, exc)
                canned()
                return
            outcome = result.result()
            if outcome.error is not None and not outcome.interrupted and outcome.barge_in is None:
                log.warning("Said %r as written: her own turn broke (%s)", turn, outcome.error)
                canned()
                return
            if relay and outcome.text.strip() and self.relay is not None:
                try:
                    self.relay(outcome.text.strip(), cue)
                except Exception:
                    log.warning("Couldn't pass her own words on to the phone", exc_info=True)

        pending.add_done_callback(done)

    def _through_gate(self, line: Line, say: Callable[[], None], reword: Callable[[str], None]) -> None:
        """``line`` past the unprompted gate (``self.unprompted``): said (``say``), said in her own words without
        what the judge named (``reword``: only ever an asked line), or held. What needs no model is decided at once;
        the rest is judged in the background and said when it's through (counted in ``announcing`` meanwhile, so a
        restart waits for it). Without a gate, or off the event loop, it's said as it comes. While the user wants
        her quiet (``_hush``) her own lines wait instead, and go through all this once that's over."""
        if self._held_for_hush(line, say, reword):
            return
        gate = self.unprompted
        if gate is None:
            say()
            return

        def act(verdict: Verdict) -> None:
            if not verdict.say:
                return
            if line.kind != REPLY:
                gate.said(line.text, line.source)
            if verdict.reword:
                reword(verdict.why or "what no longer holds")
            else:
                say()

        try:
            verdict = gate.fast(line)
        except Exception:
            log.warning("The unprompted gate broke on %r; saying it", line.text[:80], exc_info=True)
            say()
            return
        if verdict is not None:
            gate.decided(line, verdict)
            act(verdict)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("Said %r without its judgment: not called on the event loop", line.text[:80])
            say()
            return

        async def judged() -> None:
            try:
                verdict = await gate.check(line)
            except Exception:  # check never raises; should it, the line is said rather than lost
                log.warning("The unprompted gate broke on %r; saying it", line.text[:80], exc_info=True)
                verdict = Verdict(True, "fail-safe", "the gate broke")
            act(verdict)

        task = loop.create_task(judged(), name="unprompted-gate")
        self._gating.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self._gating.discard(finished)
            if not finished.cancelled() and (exc := finished.exception()) is not None:
                log.warning("Couldn't say %r after its judgment", line.text[:80], exc_info=exc)

        task.add_done_callback(done)

    def _heard_from_user(self, core: str) -> None:
        """The user said or typed ``core`` (``addressed``): "shut up" quiets her (``_hush``); anything else is them
        talking to her again, which ends the quiet."""
        if asks_for_hush(core):
            self._hush()
        elif self._hushed_until is not None and not keeps_hush(core) and stop_kind(core) is None:
            self._end_hush("the user talked to me again")

    def _hush(self) -> None:
        """The user told her to be quiet ("shut up"): on 2026-11-19 she announced the night's report four minutes
        after. What she'd bring up unasked (her ideas and her status news) waits until they talk to her again, or
        ``HUSH_S`` at most; what they asked for (a timer, a reminder), anything urgent and replies still come."""
        self._hushed_until = time.monotonic() + HUSH_S
        if self._hush_timer is not None:
            self._hush_timer.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._hush_timer = None  # it ends when a line comes after it (``_held_for_hush``)
        else:
            self._hush_timer = loop.call_later(HUSH_S, self._end_hush, "the quiet period is over")
        log.info("Told to be quiet: holding what I'd say on my own for up to %d min", HUSH_S // 60)

    def _end_hush(self, why: str) -> None:
        if self._hush_timer is not None:
            self._hush_timer.cancel()
            self._hush_timer = None
        self._hushed_until = None
        held, self._hushed_lines = self._hushed_lines, []
        log.info("No longer holding what I'd say on my own (%s): %d line(s) waited", why, len(held))
        for line, say, reword in held:
            self._through_gate(line, say, reword)

    def _held_for_hush(self, line: Line, say: Callable[[], None], reword: Callable[[str], None]) -> bool:
        """Whether ``line`` waits out the user's "shut up" (``_hush``); it's kept to go through once that's over."""
        if self._hushed_until is None or line.kind in (ASKED, REPLY) or line.urgent:
            return False
        if time.monotonic() >= self._hushed_until:
            self._end_hush("the quiet period is over")
            return False
        self._hushed_lines.append((line, say, reword))
        log.info("Holding %s (%s) while told to be quiet: %r", line.source, line.kind, line.text[:160])
        return True

    def _say_reworded(self, line: Line, note: str | None, cue: str | None, relay: bool, why: str) -> None:
        """An asked line (a reminder) with a part the user waved off or that no longer holds: said by her, in her
        own words, without that part; the line as written is the fallback."""
        turn_note = _REWORD_NOTE.format(source=line.source, text=line.text, why=why)
        if note:
            turn_note += f"\n{note}"
        self._tell_now(_REWORD_TURN, turn_note, line.text, note, cue, relay)

    def run_from(
        self, source: str, text: str, note: str | None = None, speak: bool = False
    ) -> asyncio.Future[TurnResult]:
        """A turn the user typed somewhere else (``source`` "discord": a text from their phone), in line like
        ``run_in_background``: it waits for a conversation or announcement at the PC to finish, never cutting in.

        It's the user's own turn, handled like typed chat (a preset reply, a quick command, or the brain, so "which
        model was that?" works the same), but its risky calls are asked where it came from (``remote_confirmers``)
        and the user's words never approve one (``_REMOTE_SOURCES``). Nothing is said at the PC unless ``speak``;
        the reply is the result's text, and no follow-up window opens, except after a spoken reply to her chat window
        that asked the user something (``_listens_after``). Fails when the voice loop isn't running.
        """
        if source not in _ROUTED_SOURCES:
            raise ValueError(f"not a remote source: {source!r}")
        item = BackgroundTurn(text=text, note=note, speak=speak, result=_new_future(), source=source)
        self._from_away.append(item)
        item.result.add_done_callback(lambda _: self._from_away.remove(item) if item in self._from_away else None)
        result = self._queue_turn(item)
        if source == _WINDOW:
            self._end_quiet_follow_up()
        return result

    def _typed_waiting(self) -> bool:
        """Whether a turn typed at the PC (her chat window, an answer sent from her panel) waits in line, not begun."""
        return any(i.source == _WINDOW and not i.started and not i.result.done() for i in self._from_away)

    def stop_from(
        self, source: str, kind: StopKind = "stop", core: str = "stop"
    ) -> tuple[list[asyncio.Future[TurnResult]], str | None]:
        """A stop texted from ``source`` ("drop it", "stop"): its turns from there that haven't been answered end,
        whether still waiting in line (taken back: never run) or running (stopped like the stop hotkey stops a turn;
        one still getting ready is stopped as soon as its turn begins, ``stop_wanted``), and a screen task working
        beside the conversation halts too, as a spoken stop halts it (``_halt_work``). Returns the futures of the
        turns it ended (a waiting one's is cancelled; a running one's resolves once it has wound down, stopped, or
        done when a quick command was already under way) and the line saying what screen work halted (None: none)."""
        ended: list[asyncio.Future[TurnResult]] = []
        for item in list(self._from_away):
            if item.source != source or item.result.done():
                continue
            ended.append(item.result)
            if not item.started:
                item.result.cancel()
                continue
            item.stop_wanted = True
            if self._turn_active and self._running_away is item:
                self._trigger_barge_in("stop", reason="you texted stop")
        line = self._halt_work(kind, core) if self._work_running() else None
        if ended:
            log.info("Stopped %d turn(s) texted from %s, at a texted stop", len(ended), source)
        return ended, line

    def waiting_from(self, source: str) -> bool:
        """Whether a turn texted from ``source`` is still waiting in line or running."""
        return any(item.source == source and not item.result.done() for item in self._from_away)

    def ask_for_work(self, request: ConfirmationRequest, lead: str = "") -> asyncio.Future[bool]:
        """Put a risky step of work running beside the conversation (a screen task typing in a terminal) to the
        user, out loud, once nobody is talking with her, and hear their yes or no: the future resolves with it.

        It waits in line with the announcements, so it never cuts into a conversation, and is asked like a question
        in a turn (``_confirm``). Cancelling the future takes the question back (the task was paused or stopped).
        Without the voice loop there is nobody to ask: it's a no at once."""
        answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        if not self._running and not (self.voice_enabled and self.state is AssistantState.STARTING):
            answer.set_result(False)
            return answer
        self._enqueue(WorkQuestion(request=request, lead=lead, answer=answer))
        return answer

    async def _ask_for_work(self, item: WorkQuestion) -> None:
        """Ask a ``WorkQuestion`` (``ask_for_work``). Waking her over it starts a conversation instead; the question
        stays open meanwhile, since what the user says may well pause the task and take it back. No answer (nothing
        heard, nothing clear) is never a no: the question is held open (``_hold_open``) and she's free again."""
        if item.answer.done():  # taken back while it waited
            return
        self._interrupt = asyncio.Event()
        self._barge_kind = None
        self._set_state(AssistantState.SPEAKING)
        self._enter_barge_mode()  # an answer said over the question is heard too
        verdict: bool | None = None
        window: asyncio.Future[bool] | None = None
        self._heard_answers = []  # in case it isn't asked at all
        try:
            async with self._confirm_lock:
                verdict, window = await self._confirm_work(item)
        except Exception:
            log.exception("Asking about %s for work beside the conversation failed", item.request.tool_name)
        finally:
            if self._mode is ListenMode.BARGE:
                self._mode = ListenMode.OFF
        if verdict is None and not item.answer.done() and self._yes_with_more(item, self._heard_answers):
            verdict = True
        halted = None if verdict or item.answer.done() else self._halt_for_answer(item, self._heard_answers)
        if halted is not None:
            if window is not None:
                window.cancel()
            _resolve(item.answer, False)  # the task is already off the screen: nothing it planned comes next
            await self._say_stopped(halted)
            return
        if self._barge_kind in ("wake", "voice", "hotkey"):
            self._open_question = item
            try:
                await self._converse()  # adopts the capture the barge-in already started
            finally:
                self._open_question = None
        if verdict is not None:
            if window is not None:
                window.cancel()
            _resolve(item.answer, verdict)
        elif item.answer.done():
            if window is not None:
                window.cancel()
        else:
            await self._hold_open(item, window)

    async def _confirm_work(self, item: WorkQuestion) -> tuple[bool | None, asyncio.Future[bool] | None]:
        """``_confirm`` for a screen task's question, put in her open chat window too, with its Yes and No buttons:
        the user working there may not hear her (or have her voice off), and their silence out loud was a no to a
        Submit they wanted done. The first answer counts, out loud or in the window. Returns it (None: none yet,
        nothing heard or nothing clear) and, with none, the window's question while it's still up, to be held open
        with it (``_hold_open``)."""
        link = self.remote_confirmers.get(_WINDOW)
        if not isinstance(link, ChatLink) or not link.open_now:
            return await self._confirm(item.request, lead=item.lead), None
        shown = asyncio.ensure_future(link.confirm(item.request, lead=item.lead, patient=True))
        said = asyncio.ensure_future(self._confirm(item.request, lead=item.lead))
        kept: asyncio.Future[bool] | None = None
        try:
            waiting: set[asyncio.Future[Any]] = {shown, said, item.answer}
            while True:
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if item.answer.done():
                    return None, None  # taken back (the task paused or stopped)
                if shown in done:
                    waiting.discard(shown)
                    if _answered(shown):
                        log.info(
                            "The screen task's question about %s was answered in the chat window",
                            item.request.tool_name,
                        )
                        return shown.result(), None
                if said in done:
                    verdict = said.result()
                    if verdict is None and not shown.done():
                        kept = shown
                    return verdict, kept
        finally:
            if not said.done() and self.speech is not None:
                self.speech.stop()  # answered in the window: the question out loud is moot
            said.cancel()
            if kept is None:
                shown.cancel()
            await asyncio.gather(said, return_exceptions=True)

    async def _hold_open(self, item: WorkQuestion, window: asyncio.Future[bool] | None) -> None:
        """No answer to a screen task's question (nothing heard, nothing clear), which is never a no: the task waits
        at that step and she's free for everything else. She says once that she's waiting; the question stays up in
        her chat window (put there again whenever it opens) with its Yes and No, and a yes or no said to her later
        ("Octavia, yes") or typed answers it (``_answer_open_question``). The task taking it back (paused, stopped,
        its own wait ran out) ends it."""
        item.held_at = time.monotonic()
        self._held_questions.append(item)
        watching = asyncio.ensure_future(self._answer_from_window(item, window))

        def ended(_: object) -> None:
            if item in self._held_questions:
                self._held_questions.remove(item)
            watching.cancel()

        item.answer.add_done_callback(ended)
        log.info("No answer to the screen task's question about %s: holding it open", item.request.tool_name)
        description = item.request.description.rstrip(".?")
        line = f"{item.lead}I'm holding at that step until you say yes or no to {description}."
        if self._unanswered_why == _MIC_SILENT:
            line += " I can't hear anything from your mic, it may be muted."
        line += " Say “yes” to me, or answer in my chat window."
        await self._say_stopped(line)

    async def _answer_from_window(self, item: WorkQuestion, window: asyncio.Future[bool] | None) -> None:
        """A held question's Yes and No in her chat window, for as long as it's held: its answer there is the
        answer. Closed, the window takes the question with it; open again, it's asked there again (never opening the
        window itself: that would take the keys from the task's own window)."""
        link = self.remote_confirmers.get(_WINDOW)
        try:
            while not item.answer.done():
                if window is None:
                    if isinstance(link, ChatLink) and link.open_now:
                        window = asyncio.ensure_future(link.confirm(item.request, lead=item.lead, patient=True))
                    else:
                        await asyncio.wait({item.answer}, timeout=_WINDOW_CHECK_S)
                        continue
                await asyncio.wait({window, item.answer}, return_when=asyncio.FIRST_COMPLETED)
                if window.done():
                    if _answered(window):
                        log.info(
                            "The screen task's held question about %s was answered in the chat window",
                            item.request.tool_name,
                        )
                        _resolve(item.answer, window.result())
                    window = None  # closed without an answer: asked again once it's open
        finally:
            if window is not None:
                window.cancel()

    def _answer_open_question(self, said: str) -> str | None:
        """A yes (one that says more too, ``yes_and_more``) or a plain no said or typed to a screen task's question,
        one the user woke her over ("Octavia, yes") or one held open for want of an answer: it's the answer, not something else to talk about, so it mustn't
        pause the task as other talk does. Anything more (a correction, a question) is left to the conversation, and
        so is a yes once she has asked something else since holding one. The line to say, or None when it wasn't an
        answer."""
        item = self._question_waiting()
        if item is None:
            return None
        verdict = True if yes_and_more(said) else plain_answer(said)
        if verdict is None:
            return None
        log.info("Took %r as the answer to the screen task's question about %s", said, item.request.tool_name)
        _resolve(item.answer, verdict)
        return "Okay, going ahead." if verdict else "Okay, I'll skip that step."

    def _question_waiting(self) -> WorkQuestion | None:
        """The screen task's question a yes or no would answer right now (``_answer_open_question``), if any."""
        item = self._open_question if self._open_question is not None else self._held_question()
        return item if item is not None and not item.answer.done() else None

    def _held_question(self) -> WorkQuestion | None:
        """The newest held question a bare yes or no would answer: none once she has asked something else since it
        was held (that yes may be to her newer question)."""
        live = [item for item in getattr(self, "_held_questions", ()) if not item.answer.done()]
        if not live:
            return None
        item = live[-1]
        spoken = getattr(self, "_last_spoken", None)
        if spoken is not None and item.held_at is not None and spoken[0] >= item.held_at and "?" in spoken[1]:
            return None
        return item

    def _yes_with_more(self, item: WorkQuestion, heard: Sequence[str]) -> bool:
        """The user's last answer to a screen task's question was a yes that said more ("Yes, paste it into the
        terminal", ``yes_and_more``), which ``_confirm`` couldn't take as a plain yes: it's the yes to the step, not
        the user stepping in, so the task goes on. Their words are kept for the brain."""
        answers = [answer.strip() for answer in heard if answer.strip()]
        if not answers or not yes_and_more(answers[-1]):
            return False
        log.info("Took %r as a yes to the screen task's question about %s", answers[-1], item.request.tool_name)
        self.note(f'Asked whether to {item.request.description} for a screen task, the user said yes: "{answers[-1]}"')
        return True

    def _halt_for_answer(self, item: WorkQuestion, heard: Sequence[str]) -> str | None:
        """What the user answered to a screen task's question, when it says more than "no to this step": a stop said
        in it ("What are you doing? Stop!") stops the task ("hold on": pauses it), and anything more than a plain yes
        or no (a correction, "you're doing it wrong", "wrong cell") pauses it, as talking to her while it works does,
        with their words kept for the brain: the task waits for them, nothing is taken as declined. The line to say
        about it, or None when the task goes on (without that step) or nothing was said (then it's held open)."""
        request = item.request
        answers = [answer.strip() for answer in heard if answer.strip()]
        for at, answer in enumerate(answers):
            for sentence in _SENTENCES.split(answer):
                core = self._addressed(sentence)
                kind = stop_kind(core)
                if kind is None or core in _DECLINES_STEP:
                    continue
                if holds(core) and at < len(answers) - 1:
                    continue  # "wait, who's it going to?", answered before they said yes or no
                if (line := self._halt_work(kind, core)) is not None:
                    return line
        if not answers:
            return None  # nothing said: no answer, held open
        if plain_answer(answers[-1]) is not None:  # an unclear try that was asked again isn't the answer
            return None
        said = " ".join(answers)
        line = self._pause_running_work()
        if line is not None:
            self.note(f'Asked whether to {request.description} for a screen task, the user answered: "{said}"')
        return line

    def _queue_turn(self, item: BackgroundTurn) -> asyncio.Future[TurnResult]:
        if not self._running and not (self.voice_enabled and self.state is AssistantState.STARTING):
            item.result.set_exception(RuntimeError("the voice assistant isn't running"))
            return item.result
        self._enqueue(item)
        return item.result

    def _enqueue(self, item: Announcement | BackgroundTurn | WorkQuestion) -> None:
        self._announcements.put_nowait(item)
        if self._mode is ListenMode.WAKE and self._wake_waiter is not None:
            self._mode = ListenMode.OFF
            _resolve(self._wake_waiter, None)

    def _remember_announcement(
        self, text: str, note: str | None, offer: Offer | None = None, told: str | None = None
    ) -> None:
        """What was announced goes to the brain's next turn with its note: an update's outcome (e.g. a failed
        install) is otherwise heard by the user but unknown to the brain, which then can't say why. Its offer, if
        any, waits for the next turn, with any other announcement's since: "Want me to start it again?" twice, for
        two builds, and the user's one "yes" is to both. One that asks something else ends them. ``told``: the part
        of ``text`` the user actually heard, when only part was said (a long report): only a question in that is
        one their "yes" can answer."""
        told = told or text
        if offer is not None:
            self._offers.append(offer)
        elif questions_in([told]):
            self._offers.clear()  # it asks something else: the user's next "yes" is to that
        self._put(told)
        if told != text:
            self._pending_notes.append(f'{_ANNOUNCED_NOTE}; out loud only: "{told}"; in the chat window, all: "{text}"')
        else:
            self._pending_notes.append(f'{_ANNOUNCED_NOTE}: "{text}"')
        if note:
            self._pending_notes.append(note)

    async def _deliver_announcement(self, item: Announcement) -> None:
        self.events.emit(Notice(source="APP", message=item.full or item.text))
        told = item.text if self.speech_enabled and self.speech is not None else None  # else shown whole
        self._remember_announcement(item.full or item.text, item.note, item.offer, told)
        if not self.speech_enabled or self.speech is None:
            self._aside("assistant", item.full or item.text)  # shown, not said: her chat window has it too
            return
        self._set_state(AssistantState.SPEAKING)
        self.events.emit(AnnouncementStarted(text=item.text, cue=item.cue, full=item.full))
        # Interruptible like a reply: the wake phrase or talk hotkey stops it and starts listening.
        self._interrupt = asyncio.Event()
        self._barge_kind = None
        self._announcing = True
        self._enter_barge_mode()
        speaking = asyncio.create_task(self.speech.say(item.text), name="announcement")
        self._spoke(item.text)  # whole, even if it's cut off: "wait, what did you say?" is about it
        interrupted = asyncio.create_task(self._interrupt.wait())
        try:
            await asyncio.wait({speaking, interrupted}, return_when=asyncio.FIRST_COMPLETED)
            if self._interrupt.is_set() and not speaking.done():
                self.speech.stop()
            outcome = await speaking
        except asyncio.CancelledError:
            speaking.cancel()
            self.speech.stop()
            raise
        finally:
            interrupted.cancel()
            self._announcing = False
            if self._mode is ListenMode.BARGE:
                self._mode = ListenMode.OFF
        if self._barge_kind in ("wake", "voice", "hotkey"):
            await self._converse()  # adopts the capture the barge-in already started
            return
        if self._barge_kind == "stop":
            return
        if item.listen and not outcome.interrupted and outcome.error is None and self.voice_enabled:
            await self._let_speech_tail_fade()
            await self._converse(follow_up=True)

    async def _run_background(self, item: BackgroundTurn) -> None:
        if item.result.done():  # given up on while it waited
            return
        item.started = True
        self._running_away = item if item.source in _ROUTED_SOURCES else None
        if item.cue and item.speak:  # the avatar greets a remark of hers by its kind, as an announcement's
            self.events.emit(AnnouncementStarted(text="", cue=item.cue))
        try:
            await self.sessions.touch()
            notes = (item.note,) if item.note else ()
            if item.source in _ROUTED_SOURCES:
                self._remote_busy = item.source if item.source in _REMOTE_SOURCES else None
                try:
                    result = await self._remote_turn(item.text, item.source, notes, item.speak)
                finally:
                    self._remote_busy = None
            else:
                result = await self._run_turn(item.text, source="scheduled", notes=notes, speak=item.speak)
        except asyncio.CancelledError:
            item.result.cancel()
            raise
        except Exception as exc:
            if not item.result.done():
                item.result.set_exception(exc)
            raise
        finally:
            self._running_away = None
        if not item.result.done():
            item.result.set_result(result)
        if result.barge_in in ("wake", "voice", "hotkey"):
            await self._converse()  # adopts the capture the barge-in already started
        elif (
            item.speak
            and result.barge_in is None
            and result.error is None
            and self.voice_enabled
            and self._listens_after(item)
        ):
            await self._let_speech_tail_fade()
            await self._converse(follow_up=True)

    def _listens_after(self, item: BackgroundTurn) -> bool:
        """Whether a follow-up window opens after ``item``'s spoken reply. Not after a text's: its answer comes as a
        text, not at the PC. After a turn from her chat window (typed there, or picked in Explorer's menu) only when
        her reply asked the user something and the window has closed: on 2026-10-30 she asked "Want me to read the
        plan?" out loud, went straight back to waiting for her name, and the user's spoken answer went unheard. With
        the window open they're typing there, and a typed answer would wait out the window."""
        if item.source not in _ROUTED_SOURCES:
            return True
        if item.source != _WINDOW or not self._just_asked():
            return False
        link = self.remote_confirmers.get(_WINDOW)
        if isinstance(link, ChatLink) and link.open_now:
            return False
        log.info("Asked something out loud after a turn from my chat window: listening for the answer")
        return True

    async def _remote_turn(self, text: str, source: str, notes: Sequence[str], speak: bool) -> TurnResult:
        """A turn texted from far away (``run_from``), handled like typed chat (``handle_text``): a preset reply, a
        quick command, or the brain. A quick command's tool, should it ever need a yes, is asked where the turn
        came from too. One a texted stop came for before it got going isn't run (``stop_from``)."""
        if self._stopped_away():
            return TurnResult(interrupted=True, barge_in="stop")
        self._heard_from_user(self._addressed(text))
        if source not in _REMOTE_SOURCES and (answered := self._answer_open_question(text)) is not None:
            return TurnResult(text=answered)  # typed at the PC to a screen task's question held open: its answer
        preset = self.quick_replies.match(text) if self.quick_replies is not None else None
        if preset is not None:
            await self._say_preset(preset, speak=speak)
            return TurnResult(text=preset.text)
        self._turn_source = source
        try:
            handled, failed = await self._fast_command(text, speak=speak)
        finally:
            self._turn_source = None
        if handled is not None:
            return TurnResult(text=handled)
        if self._stopped_away():
            return TurnResult(interrupted=True, barge_in="stop")
        # Typed in her chat window and answered out loud: the same "Got it." a spoken request gets, so a long piece of
        # work doesn't sit in silence (2026-11-04: 137 s with nothing said, and the user typed "continue" again).
        said = self._acknowledge(text) if source == _WINDOW and speak else None
        return await self._run_turn(text, source=source, notes=[*notes, *failed], speak=speak, acknowledged=said)

    def _stopped_away(self) -> bool:
        """The texted turn running now has had a texted stop come for it (``stop_from``)."""
        return self._running_away is not None and self._running_away.stop_wanted
