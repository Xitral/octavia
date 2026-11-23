"""The assistant as the tool executor's ``Confirmer`` (mixed into ``Assistant``): when a risky call needs no
question (the user's own words asked for it, they said yes to the same kind just before, it was the call she
offered), and otherwise asking out loud and hearing the answer."""

from __future__ import annotations

import logging
import re
import time

from octavia.core import consent
from octavia.core.events import AssistantState, Notice, ToolCallFinished, ToolCallStarted
from octavia.core.submit_sites import submits_freely
from octavia.core.turn_types import _REMOTE_SOURCES, _ROUTED_SOURCES, _resolve
from octavia.core.yes_or_no import parse_confirmation, plain_answer
from octavia.tools.asked import NOT_ON_CALLS, questions_in, says_yes, steps_after, withdraws_send
from octavia.tools.permissions import READS_OUTSIDE, ConfirmationRequest, Confirmer, NotAnswered, terminal_of
from octavia.vad.endpointer import EndpointEvent, EndpointKind

log = logging.getLogger("octavia.core.assistant")  # part of the assistant: its log lines read as before


_REASK_UNCLEAR = "Sorry, was that a yes or a no?"

# An answer to her approval question that's a question itself: "wait, which one?", "what's in it?", "who to?".
_SOUNDS_LIKE_A_QUESTION = re.compile(
    r"\?\s*$|^(?:(?:wait|hold on|hang on|hmm+|um+|uh+|so|okay|ok|actually|sorry)[\s,.!]+)*"
    r"(?:what|what's|whats|who|whose|which|where|when|why|how|is|are|does|do|can|could|will|would|did|tell me)\b",
    re.IGNORECASE,
)

# Questions about an approval answered before asking again (``_confirm``), and the time each one adds.
_MAX_QUESTIONS = 3

_QUESTION_TIME_S = 20.0

_REASK_SILENT = "Sorry, I didn't catch that, yes or no?"

# The tool executor gives up on a confirmation after permissions.confirm_timeout_s; stop listening this much
# earlier so an answer that was heard is still transcribed in time instead of being lost to the timeout.
_CONFIRM_MARGIN_S = 3.0

_MIN_CONFIRM_LISTEN_S = 1.5

# Why a question got no answer (``NotAnswered``), in words for the model and the log.
_HEARD_NOTHING = "nothing was heard"
_MIC_SILENT = "her microphone is sending pure silence (muted, or the wrong device), so nothing could be heard"
_EARS_OFF = "her listening is muted from the tray"
_UNCLEAR = "the answer wasn't a clear yes or no"
_TALKED_OVER = "the user started talking about something else over it"
_NO_TIME = "no answer came in time"

# The tools that only read the screen: a yes to one lets her look for the rest of the conversation (``_looks_ok``).
_LOOKS = frozenset({"look_at_screen", "find_on_screen"})


class ApprovalsMixin(Confirmer):
    """Part of ``Assistant`` (``octavia.core.assistant``), on its state."""

    def _remote(self) -> bool:
        """The running turn was typed somewhere else (a text from the user's phone): see ``_REMOTE_SOURCES``."""
        return self._turn_source in _REMOTE_SOURCES

    def _routed(self) -> bool:
        """The running turn's questions are asked where it was typed (a text, her chat window): ``_ROUTED_SOURCES``."""
        return self._turn_source in _ROUTED_SOURCES

    def _working_with_user(self) -> bool:
        """The user is right there: one of their own turns runs, they haven't cut it off, and it hasn't taken in
        outside content (a web page can tell the model to do things)."""
        return self._turn_text is not None and not self._turn_used_builtin and not self._turn_interrupted()

    def unattended(self, request: ConfirmationRequest) -> bool:
        """An AUTO call goes ahead without a question while the user is working with her (``_working_with_user``).
        Anything outside their turns (an announcement, a background job) is asked, like any confirmation, and so is
        one after a tool read others' words in this turn (an email, a Canvas post, the screen: ``READS_OUTSIDE``),
        unless the user's own words asked for it (``requested``, ``_said_already``). Another read is no action: a
        second look at the screen goes ahead as the first did."""
        if not self._working_with_user():
            return False
        if request.tool_name in READS_OUTSIDE and not request.flagged:
            return True
        return not getattr(self, "_turn_read_outside", False)

    def already_approved(self, request: ConfirmationRequest) -> bool:
        """The user said yes to this kind of call (the same tool, the same kind of thing) just before, in this task:
        deleting a second note, closing the next game. Anything else in between ends that (``_on_tool_started``).

        Looking at the screen is the exception: once the user let her look in this conversation, she keeps looking
        without asking, between clicks too, while she edits their slides step by step (``_looks_ok``)."""
        if not request.scope or not self._working_with_user() or self._always_asked(request) or self._remote():
            return False  # a texted turn: each risky call gets its own question
        return (
            request.scope in self._turn_approved
            or self._standing_terminal(request)
            or self._standing_look(request)
            or self._a_step_of(request)
        )

    def _standing_terminal(self, request: ConfirmationRequest) -> bool:
        """Everyday input into a terminal the user said yes to typing in, earlier in this turn: unlike other kinds of
        call, it doesn't end when another tool runs (a look, a wait, a click on the other tab), since only commands
        nothing risky is in get this scope (``computer_control.standing_terminal``)."""
        terminal = terminal_of(request.scope)
        return bool(terminal) and terminal in getattr(self, "_turn_terminals", ())

    def _a_step_of(self, request: ConfirmationRequest) -> bool:
        """A call that carries out one the user approved earlier in this turn (``asked.steps_after``): rebuilding an
        update whose approved install found it clashing with newer code. Only for the real tool (one that can be
        offered at all, ``asked.offer_matcher_for``)."""
        if request.accepts_offer is None:
            return False
        for step in self._turn_steps:
            try:
                if step(request.tool_name, request.args):
                    log.info("Not asking about %s: it carries out what the user approved", request.tool_name)
                    return True
            except Exception:
                log.exception("Couldn't tell whether %s carries out an approved call", request.tool_name)
        return False

    def _is_look(self, request: ConfirmationRequest) -> bool:
        """A plain look at the screen: one of the reading tools, and nothing its own look flagged as risky."""
        return request.tool_name in _LOOKS and request.scope == request.tool_name

    def _standing_look(self, request: ConfirmationRequest) -> bool:
        """A look in the conversation where the user already said yes to one, and hasn't taken it back since."""
        session = self.sessions.session_id
        return self._is_look(request) and session is not None and self._looks_ok == session

    def _on_tool_started(self, event: ToolCallStarted) -> None:
        """A yes covers calls of that same kind that follow straight on. Once another tool runs, whatever it brought
        in (a screenshot, a page, a file) may be what asks for the next one, so the next risky call is asked again:
        text on the screen mustn't ride the user's earlier yes."""
        if self._turn_approved:
            self._turn_approved = {s for s in self._turn_approved if s.partition(":")[0] == event.name}
        if event.name != "build_improvement":
            self._turn_steps = []  # whatever another tool brought in mustn't ride the approved install either

    def _on_tool_finished(self, event: ToolCallFinished) -> None:
        """A call that ran was approved (or needed no yes): what carrying it out takes is part of it, for the rest
        of this turn (``asked.steps_after``), from what it says it was about (an install: the updates it tried)."""
        if not event.ok:
            self._tools_failed += 1
        if self._working_with_user() and (step := steps_after(event.name, event.data or {})) is not None:
            self._turn_steps.append(step)

    def _always_asked(self, request: ConfirmationRequest) -> bool:
        """A send or submit in a place the user counts as high-risk (``permissions.always_ask_before_sending``: their
        email, Canvas): asked every time. Words, a standing approval, an earlier yes and ``submit_without_asking``
        don't skip it."""
        if not request.window or not request.scope.endswith(":send"):
            return False
        places = [p.casefold() for p in self.config.permissions.always_ask_before_sending if p.strip()]
        window = request.window.casefold()
        return any(place in window for place in places)

    def requested(self, request: ConfirmationRequest) -> bool:
        """Whether what the user said or typed in the running turn already asks for this call (then it's the yes).

        Only the user's own words count: never an announcement, a tool's output, or a call made outside a turn.
        Nor does a turn the user cut off: what they said over it may well be "no, don't". Nor the rest of a turn
        that used a tool the brain runs itself, such as a web search or fetch: a page can tell the model to look at
        the screen, and the user's "what's on my screen?" mustn't wave that through.
        """
        text = self._turn_text
        if not text or request.requested_by is None or self._turn_used_builtin or self._turn_interrupted():
            return False
        if self._remote():
            return False  # texted: "you don't need to ask, send it" may not be the user's, and isn't a yes
        if self.on_call and request.tool_name in NOT_ON_CALLS:
            return False  # on a call, "can you do this for me" may be anyone's
        try:
            asked = request.requested_by(text) is True
        except Exception:
            log.exception("Couldn't tell whether the user asked for %s", request.tool_name)
            return False
        if self._always_asked(request):
            return False  # a high-risk place (email, Canvas): every send there is asked, whatever was said
        if not request.place:
            return asked
        # Submitting where the user already said she may, earlier in this conversation ("you don't need approval to
        # submit these"): it stands for that window until they hold it back ("ask me before you submit") or the
        # conversation ends. Only for sends, the one kind of risky call the user's words can approve.
        session = self.sessions.session_id
        here = (session, request.place)
        permissions = self.config.permissions
        if submits_freely(request, permissions.submit_without_asking) and not withdraws_send(text):
            return True  # a site the user lets her submit on whenever she's working there with them
        if withdraws_send(text):
            self._standing.discard(here)
            self._waived = None
            return False
        unplaced = self._waived == session and not any(s == session for s, _ in self._standing)
        if asked or unplaced:
            self._standing.add(here)
            return True
        return here in self._standing

    def offered(self, request: ConfirmationRequest) -> bool:
        """Whether the running turn is the user's plain yes to what she just put to them, and this is a call it
        covers. A "yes" has no words of its own: it means what she asked, so that is what's judged.

        * An announcement that offered this exact call (``Offer``): "My build was cut off. Want me to start it
          again?" / "Yes." starts it without a second question. Every offer made since the user last spoke counts
          (two builds cut off, two offers, one yes), each for one call.
        * Her own question or offer since they last spoke, read as their request by the protected words matchers
          (``octavia.tools.asked``): "Want me to install them?" / "Yes." installs them. Never for a send or submit
          somewhere (those have their own rules, ``requested``) nor a place the user counts as high-risk.

        The same limits as ``requested``: their own words, a turn they didn't cut off, nothing from outside taken
        in, not texted from far away."""
        text = self._turn_text
        if not text or not self._working_with_user() or self._remote():
            return False  # a texted "yes" gets the question of its own
        if request.accepts_offer is not None:
            for offer in self._turn_offers:
                if not offer.covers(request.tool_name, request.args):
                    continue
                try:
                    if request.accepts_offer(text) is not True:
                        return False
                except Exception:
                    log.exception("Couldn't tell whether the user took up the offer of %s", request.tool_name)
                    return False
                self._turn_offers.remove(offer)  # one call per offer
                return True
        if request.requested_by is None or request.place or self._always_asked(request) or not says_yes(text):
            return False
        if self.on_call:
            return False  # on a call, a "yeah" may be anyone's
        for question in reversed(questions_in(self._turn_asked)):
            try:
                if request.requested_by(question) is True:
                    log.info('Not asking about %s: the user said yes to "%s"', request.tool_name, question)
                    return True
            except Exception:
                log.exception("Couldn't tell whether her question asked for %s", request.tool_name)
                return False
        return False

    async def confirm(self, request: ConfirmationRequest) -> bool:
        """Confirmer used by the tool executor: ask, and remember a yes for the rest of the task, so the same kind
        of call later in this turn isn't asked again (``already_approved``); a yes to a look, for the rest of the
        conversation.

        One question at a time. Calls the model made together (four looks at once) wait in line behind the one
        being asked, and take its answer: a yes covers them like any later call of that kind, and a no to the very
        same question in this turn isn't asked again."""
        async with self._confirm_lock:
            turn = self._turn_id
            # The exact call too: two commands can share their plain words ("run a command to use Git").
            asked = (request.scope, request.description, request.detail)
            if self.already_approved(request):
                return True
            if asked in self._turn_declined:
                return False
            approved = await self._said_already(request) or await self._confirm(request)
            if approved is None:
                raise NotAnswered(self._unanswered_why)  # never a no, nor remembered as one
            if self._turn_text is None or self._turn_id != turn:
                return approved
            if approved and request.scope:
                self._turn_approved.add(request.scope)
                terminals = getattr(self, "_turn_terminals", None)
                if (terminal := terminal_of(request.scope)) and terminals is not None:
                    terminals.add(terminal)
                if self._is_look(request) and self._working_with_user() and not self._remote():
                    self._looks_ok = self.sessions.session_id
            elif not approved:
                self._turn_declined.add(asked)
            return approved

    def timeout_s(self, request: ConfirmationRequest) -> float | None:
        """A texted turn's question waits as long as the place it's asked in says (the Discord bridge's
        ``confirm_timeout_s``); one asked in the room, ``permissions.confirm_timeout_s``, plus time for a few
        questions about it when her quick voice can answer them (``_confirm``)."""
        confirmer = self.remote_confirmers.get(self._turn_source or "") if self._routed() else None
        if confirmer is not None:
            return confirmer.timeout_s(request)
        if self.fast_chat is not None and self.voice_enabled:
            return self.config.permissions.confirm_timeout_s + _QUESTION_TIME_S * _MAX_QUESTIONS
        return None

    async def _said_already(self, request: ConfirmationRequest) -> bool:
        """Whether the user's own words in this turn plainly asked for exactly this call, so asking again would be
        redundant ("close Spotify" / "Want me to close Spotify?"), or were a yes to her own offer of exactly it ("If
        you want, I'll put both updates in during that restart." / "Yes."). Her quick voice judges it from their
        words, what she said to them since they last spoke, and the call's description only. Never for what they
        count as high-risk, nor for any risky kind of mouse or keyboard action, a restricted tool, a texted turn, or a
        turn that took in outside content (``consent``)."""
        fast = self.fast_chat
        text = self._turn_text
        if fast is None or not fast.ready or not text or not self._working_with_user() or self._remote():
            return False
        risk = getattr(request.risk, "value", str(request.risk))
        always_ask = self.config.permissions.always_ask_before_sending
        about = consent.details(request.description, request.tool_name, request.args)
        if consent.may_skip_question(request.tool_name, request.scope or "", request.window, risk, always_ask):
            if await consent.asked_for(fast, text, about):
                log.info("Not asking about %s: the user's own words asked for it", request.tool_name)
                return True
        # Only a plain yes: "sure, but just the mouse one" or "yes, after this game" is a request of its own.
        if not says_yes(text) or not self._turn_asked or request.place or self.on_call:
            return False
        if not consent.may_skip_question(
            request.tool_name, request.scope or "", request.window, risk, always_ask, offered=True
        ):
            return False
        if not await consent.took_offer(fast, self._turn_asked, text, about):
            return False
        log.info("Not asking about %s: the user said yes to her offer of it", request.tool_name)
        return True

    async def _confirm(self, request: ConfirmationRequest, lead: str = "") -> bool | None:
        """Ask out loud and listen for yes/no (typed chat asks on the terminal; a texted turn, where it came from).
        ``lead`` goes before the question, for one that isn't about the conversation ("For “the form”: ").

        A reply that was unclear, or silence, gets one more question: on speakers the mic can't pick out a "yes" said
        over the question, so the user may well have answered already. After that it's None: no answer, which is
        never a no (``_unanswered_why`` says why: nothing heard, a mic sending pure silence, an unclear reply). Only
        their no, or the stop hotkey, is False. What they said to it is kept in ``_heard_answers`` until the next
        question.
        """
        self._heard_answers = []
        if self._routed():
            # Asked in the DM (or window) the turn came from, never out loud at the PC to whoever is in the room.
            confirmer = self.remote_confirmers.get(self._turn_source or "")
            return await confirmer.confirm(request) if confirmer is not None else False
        if not self.voice_enabled or self.stt is None:
            return await self.text_confirmer.confirm(request)
        if self._turn_interrupted():
            return self._no_answer(_TALKED_OVER)  # a call racing the interrupt: not asked over their new utterance
        deadline = time.monotonic() + self.config.permissions.confirm_timeout_s - _CONFIRM_MARGIN_S
        prompt = f"{lead}Want me to {request.description}?"
        self._earcon("ask")
        if not await self._ask_confirmation(prompt):
            # Her voice is down (the TTS service refused or timed out): the question goes on screen instead, after
            # its chime, and she listens for the answer as usual. Never a yes to a question that wasn't put to them:
            # it's shown before she listens, and an answer only counts from then on.
            self.events.emit(Notice(source="CONFIRM", message=f"{prompt} (I can't speak right now: say yes or no)"))
            _resolve(self._capture_waiter, EndpointEvent(EndpointKind.NO_SPEECH))  # nothing said before it was shown
        if self._turn_interrupted():
            return self._no_answer(_TALKED_OVER)

        previous_state = self.state
        try:
            reask: str | None = None
            why = _HEARD_NOTHING
            attempts = questions = 0
            while attempts < 2:
                attempts += 1
                if reask is not None:
                    if deadline - time.monotonic() < _MIN_CONFIRM_LISTEN_S:
                        log.info("Confirmation: no time left to ask again")
                        return self._no_answer(why)
                    # Back to how the question was asked, so an answer over the re-ask is heard too.
                    self._set_state(previous_state)
                    self._enter_barge_mode()
                    if not await self._ask_confirmation(reask) or self._turn_interrupted():
                        return self._no_answer(_TALKED_OVER if self._turn_interrupted() else why)
                listen_s = min(self.config.conversation.confirm_listen_s, deadline - time.monotonic())
                if listen_s < _MIN_CONFIRM_LISTEN_S and self._capture_waiter is None:
                    log.info("Confirmation: no time left to listen once the question was said")
                    return self._no_answer(_NO_TIME)  # no time left to hear and transcribe an answer
                # CONFIRMING (not TRANSCRIBING) so the stop hotkey is routed to this capture.
                self._set_state(AssistantState.CONFIRMING)
                early = self._capture_waiter is not None
                event = await self._capture(listen_s, pre_roll=False)  # adopts an answer given over the question
                log.info(
                    "Confirmation try %d: %s%s",
                    attempts,
                    event.kind.value,
                    " (started over the question)" if early else f" (listened {listen_s:.1f}s)",
                )
                if event.kind is EndpointKind.NO_SPEECH or self._turn_interrupted():
                    if self._stop_requested:  # stop hotkey while we were asking: decline and end the turn
                        self._stop_requested = False
                        self._trigger_barge_in("stop")
                        return False
                    if self._turn_interrupted():
                        return self._no_answer(_TALKED_OVER)
                    why = self._why_unheard(listen_s, early)
                    reask = _REASK_SILENT
                    continue
                heard = await self._transcribe(event)
                if not heard.strip():
                    log.info("Confirmation: the answer came back empty (%.1f s of speech)", event.duration_s or 0.0)
                self._heard_answers.append(heard)
                self._aside("user", heard)
                # A question about it ("wait, who's it going to?") isn't a no for its "wait": her quick voice reads
                # it first, when it can. Anything else, a plain yes or no, is taken as it is.
                asking = _SOUNDS_LIKE_A_QUESTION.search(heard) is not None and self._can_judge()
                answer = None if asking else parse_confirmation(heard)
                log.info('Confirmation heard "%s": %s', heard, {True: "yes", False: "no", None: "unclear"}[answer])
                if self._turn_interrupted():
                    return self._no_answer(_TALKED_OVER)
                if answer is False and plain_answer(heard) is None and self._can_judge():
                    # A no with more after it may be about something else: "No, you didn't even read the whole
                    # thing" declined her fix on 2026-11-04, and she then said the change was declined. Her quick voice
                    # reads it; only a no to this action is a no. Either way the step isn't done.
                    verdict = await self._judge_answer(prompt, heard, request)
                    if verdict == "other":
                        return self._no_answer(f'the user talked about something else instead: "{heard}"')
                    return False
                if answer is not None:
                    return answer
                verdict = await self._judge_answer(prompt, heard, request)
                if verdict == "yes":
                    return True
                if verdict == "no":
                    return False
                if verdict == "other":
                    return self._no_answer(f'the user talked about something else instead: "{heard}"')
                if verdict == "question" and questions < _MAX_QUESTIONS and self.fast_chat is not None:
                    # A question about it ("wait, who's it going to?"): answered, then asked again. It isn't one of
                    # the two tries, and it buys the time to answer.
                    questions += 1
                    attempts -= 1
                    about = consent.details(request.description, request.tool_name, request.args)
                    told = await consent.answer_question(self.fast_chat, heard, about)
                    reask = (
                        f"{told} So, want me to {request.description}?"
                        if told
                        else (f"I'm not sure, sorry. Want me to {request.description}?")
                    )
                    deadline = max(deadline, time.monotonic() + _QUESTION_TIME_S)
                    continue
                reask = _REASK_UNCLEAR
                why = _UNCLEAR
            return self._no_answer(why)
        finally:
            # Only restore our state if the turn is still ours to manage.
            if self._turn_active and not self._turn_interrupted():
                self._set_state(previous_state)
                self._enter_barge_mode()

    def _no_answer(self, why: str) -> None:
        """The question got no answer (never a no): ``why`` is kept for ``NotAnswered``."""
        self._unanswered_why = why
        log.info("Confirmation: no answer (%s)", why)

    def _why_unheard(self, listen_s: float, early: bool) -> str:
        """Why a listen for the answer heard no speech: listening muted from the tray, the mic sending exact zeros the
        whole time (a headset's mute: nobody's answer could be heard), or just nothing said."""
        if self._ears_off and not self._talk_pressed:
            return _EARS_OFF
        if not early and self.mic_silent_s() >= 0.9 * listen_s:
            log.warning("The microphone sent pure silence the whole time I listened for an answer: is it muted?")
            return _MIC_SILENT
        return _HEARD_NOTHING

    def _can_judge(self) -> bool:
        return self.fast_chat is not None and self.fast_chat.ready

    async def _judge_answer(self, question: str, heard: str, request: ConfirmationRequest) -> consent.Verdict:
        """What an answer that isn't a plain yes or no means ("sure, go for it", "nah", "wait, what's in it?"), as her
        quick voice reads it; "unclear" when there's no quick voice or nothing was heard."""
        fast = self.fast_chat
        if fast is None or not fast.ready or not heard.strip():
            return "unclear"
        about = consent.details(request.description, request.tool_name, request.args)
        return await consent.judge_answer(fast, question, heard, about)

    async def _ask_confirmation(self, question: str) -> bool:
        """``_ask``, with talking over the question taken as its answer rather than as an interruption."""
        self._asking = True
        self._question_cut = False
        try:
            said = await self._ask(question)
            return said or self._question_cut  # cut short by the answer: it was heard far enough to be answered
        finally:
            self._asking = False

    async def _ask(self, question: str) -> bool:
        """Say a question and wait until it has been heard. False if it couldn't be spoken."""
        self._aside("assistant", question)
        if not self.speech_enabled or self.speech is None:
            # No voice at all (e.g. no TTS key): the console shows the question instead.
            self.events.emit(Notice(source="CONFIRM", message=question + " (say yes or no)"))
            return True
        stream = self._active_stream
        if stream is not None and stream.usable:
            before = len(stream.outcome().spoken)
            stream.say(question)
            await stream.wait_idle()
            outcome = stream.outcome()
            return outcome.error is None and len(outcome.spoken) > before
        outcome = await self.speech.say(question)
        return outcome.error is None and not outcome.interrupted and bool(outcome.spoken)
