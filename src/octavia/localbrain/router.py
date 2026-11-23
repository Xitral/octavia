"""The local model answering turns (phase 3 of docs/local-brain.md): ``local_brain.mode`` "route" or "fallback_only".

``RoutedBrain`` wraps the brain and is what the assistant talks to, so nothing else changes:

* **Routed turns** ("route"): the local model hears each of your own turns first and, in the same single reply,
  answers it (with only ``route_tools``) or hands it to Claude (``delegation``). A turn that isn't eligible goes
  straight to Claude without asking it: not yours, a note on it that isn't only context (``delegation.required_note``:
  interrupted, a request on hold, an announcement...; the time, what she already said, a tone guess, quiet mode and
  memories looked up are only context, and the local model is shown them), longer than ``route_max_words``, or risky
  or complex by ``delegation.policy`` (sending, deleting, the screen, several steps, anything to look up...).
* **Nothing is said until it's checked**: its first step is asked whole (no streaming), within ``answer_timeout_s``.
  Handing over, a failure, a timeout, a tool it may not use (not in ``route_tools`` or not safe), arguments that don't
  validate, no tool call for a command (it would only say it did it), or markdown in what would be spoken: Claude
  answers instead, and the user hears only Claude. Once a tool has run there's no going back: what follows is the
  local model's, or a plain "Done.".
* **Claude down** (either mode): a turn of yours that Claude fails before saying anything (out of usage, no
  connection, ...) is answered by the local model, saying once that it's on its local brain. For
  ``claude_retry_s`` your turns go to the local model first; then Claude is tried again. If the local model can't
  answer either, the user hears Claude's own error, or, when it tried something it can't do (a tool it may not use,
  a malformed call, an action it only claimed), that it needs the cloud brain for that, plainly.
* **Its tools** (either way): ``route_tools`` and ``delegation.READ_TOOLS`` (find_on_screen, list_running_apps,
  get_current_time), which only look; while Claude is down, also ``delegation.ACT_TOOLS`` (click, type_text,
  press_keys, scroll, resume_screen_task, media_control), one per turn, through the same executor and approvals as
  Claude's calls: a second action is the cloud brain's. Calls come in the server's parsed list or as Qwen's own
  ``<tool_call>`` blocks, and each is checked strictly; a failed tool is told to it as a failure, and it never says it
  did something unless a tool that acts succeeded. Bracketed context it echoes is never said.
* **Picked by the user** (``brain.answers``, read at each turn through ``answers``; ``core/engines.py``): "claude",
  every turn is Claude's and the local model never answers, not even while Claude is down; "local", every one of
  your turns is the local model's as while Claude is down (its tools, one action), without handing any to Claude:
  what it can't do, or can't do now (not loaded), is said plainly.
* Claude's next turn is told what the local model said meanwhile, so the conversation still makes sense to it.
* One line per turn in ``local_brain.route_log_file``: which brain answered (``brain``), why (``why``: "route",
  "down", "claude"), whether the local model was asked (``consulted``), the routing decision (``decision``: "answer",
  "delegate", "fallback" or "policy", with the word in ``policy`` and, for "notes", the note's first words in
  ``note``), why it handed the turn over (``delegated_because``) and, when Claude took over from it, what went wrong
  (``fallback``). ``ResponseDone.model`` says it too ("local:<model>").
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from octavia.brain.base import BrainEvent, BrainProvider, ResponseDone, TextDelta, ToolUseStarted, UserTurn
from octavia.config import LocalBrainConfig
from octavia.localbrain.delegation import (
    ACT_TOOLS,
    ACTED,
    CLAIMED,
    COMMAND,
    DELEGATE,
    READ_TOOLS,
    UNCHECKED,
    delegate_odds,
    delegation_reason,
    echoes_context,
    follows_up,
    policy,
    required_note,
)
from octavia.localbrain.server import local_client
from octavia.localbrain.shadow import _CLIP, _RETENTION_S, _WRITTEN, USER_SOURCES, LocalShadow, _ts
from octavia.tools.base import RiskLevel

log = logging.getLogger(__name__)

LOCAL_LINE = "Heads up, Claude's not available, so I'm on my local brain for now."
# Said instead of pretending, when Claude can't answer either: a tool it may not use, or an action it only claimed.
NEEDS_CLOUD = "I can't do that on my local brain. I need my cloud brain for that."
# Set to local only, nothing is handed to Claude: what it can't do is said, with the way back.
LOCAL_ONLY = "I can't do that on my local brain, and I'm set to local only. Say \"back to normal\" to let my cloud brain take it."
NOT_LOADED = "My local brain isn't loaded right now, and I'm set to local only, so I can't answer that."
BAD_CALL = "I tried to use one of my tools but got the request wrong, so I couldn't do that."
SEVERAL_STEPS = "That takes several steps, and I need my cloud brain for that."
ONE_STEP = "I did the first step, but the rest needs my cloud brain."
CANT_CHECK = "I can't check on my updates from my local brain, so I don't know where that stands."
_FAILED_NOTE = "(That failed: tell the user plainly it didn't work, and don't say it did.)"
_MAX_STEPS = 3  # tool rounds in one local turn
_NOTES_KEPT = 4  # what it said while Claude was away, passed on to Claude's next turn
_LINE_CHARS = 400  # each line of the conversation it's shown, at most
_TOP_LOGPROBS = 5  # the first token's alternatives asked for, to tell how sure it was (``delegate_odds``)
# Its own tool calls left in its words (Qwen's format), and the bracketed context it may echo back.
_TOOL_CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.S)
_CONTEXT_LINE = re.compile(r"^[ \t]*\[[^\]\n]*\][ \t]*$\n?", re.M)
_CONTEXT_START = re.compile(r"^\[[^\]\n]*\]")


def _plainly_asked(said: str) -> str | None:
    """The tool a turn plainly asks for that no model should be trusted to answer about (``asked``'s own matchers):
    restarting or shutting herself down."""
    from octavia.tools.asked import asks_to_restart, asks_to_shut_down

    for tool, asks in (("restart_now", asks_to_restart), ("shut_down_self", asks_to_shut_down)):
        if asks(said):
            return tool
    return None


# Words that ask for something to be done or checked: only then are tools offered when set to local only.
_WANTS_TOOL = re.compile(
    r"\b(?:click|press|tap|hit|type|scroll|find|open|close|switch|timers?|volume|louder|quieter|mute|unmute|pause|"
    r"resume|play|skip|next|previous|stop|time|clock|apps?|running|programs?|windows?|screen|button)\b"
)


# Set to local only, its second try after a handover or a refused reply (``RoutedBrain.respond``): plain words.
_PLAIN_RETRY = frozenset({"delegated", "claimed_action", "bad_reply", "echoed_context"})
_PLAIN = (
    f"\n\n(You're set to answer everything yourself, so never reply with {DELEGATE}. Reply in plain spoken words, "
    "a sentence or two, the way you'd say it out loud. Don't call a tool, and don't say you did, started or will do "
    "anything: just answer what they said.)"
)


try:  # left out of the public build (localbrain/uncensored.py)
    from octavia.localbrain.uncensored import NOTE as _UNCENSORED
except ImportError:  # pragma: no cover - only in the public build
    _UNCENSORED = ""


def _on_your_own(tools: frozenset[str], why: str = "down") -> str:
    """While Claude is down (or the user set it to local only) there's no one to hand the turn to, and only these
    tools to use."""
    alone = "You're set to answer everything yourself" if why == "local" else "Claude isn't available right now"
    return (
        f"\n\n({alone}: answer this yourself, never with {DELEGATE}. The only tools you can "
        f"use now are {', '.join(sorted(tools))}. Click, type, press keys or scroll only for one clear step the user "
        "asked for, and only one action per turn: to click something, find_on_screen first, then click its element "
        "number. Say an action happened only after its tool succeeded. For anything else, several steps, or anything "
        "unclear, say plainly that you need your cloud brain for that, and never say you did or started it.)"
    )


def _after_tools(ok: bool, ran: set[str]) -> str:
    """What's said when its tools ran but no reply came after them."""
    if not ok:
        return "That didn't work."
    return "I checked, but lost my answer. Try asking again." if ran <= READ_TOOLS else "Done."


# The picture of what they circled doesn't reach the local model, and it must not act as if it had.
_CIRCLE_UNSEEN = (
    "the user just circled part of their screen with the mouse, but you can't see pictures: if their words point at "
    "it, say plainly that you can't see it right now rather than guess"
)


def _without_picture(turn: UserTurn) -> UserTurn:
    if turn.picture is None:
        return turn
    return dataclasses.replace(turn, picture=None, notes=(*turn.notes, _CIRCLE_UNSEEN))


class LocalFailed(Exception):
    """The local model's turn can't be used (nothing has been said or run yet): ``reason`` goes in the log."""

    def __init__(self, reason: str, because: str = "", say: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.because = because  # its own words on why, when it handed the turn over
        self.say = say  # what the user hears if Claude can't answer either, instead of Claude's error


@dataclasses.dataclass
class _Call:
    id: str
    name: str
    args: dict[str, Any]
    raw: str


class RoutedBrain(BrainProvider):
    def __init__(
        self,
        inner: BrainProvider,
        local: LocalShadow,
        config: LocalBrainConfig,
        log_path: Path,
        *,
        model: str = "local",
        core: Callable[[str], str] = lambda text: text,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(inner.tools, inner.executor, inner.system_prompt)
        self.inner = inner
        self.local = local
        self.config = config
        self.log_path = log_path
        self.name = inner.name
        self.model = f"local:{model}"
        self._core = core
        self._clock = clock
        self._route_tools = frozenset(config.route_tools)
        self._down_until = 0.0  # Claude failed: until then, your turns go to the local model first
        self._announced = False  # said once per outage that it's on its local brain
        self._said: deque[tuple[str, str]] = deque(maxlen=_NOTES_KEPT)  # its exchanges Claude hasn't heard about
        self._stop = asyncio.Event()
        self._confidence: float | None = None  # the last turn's: how sure it was it should answer (``delegate_odds``)
        self._heartbeat: Callable[[], None] | None = None
        # Set by the app from the assistant. ``conversation``: the conversation's latest lines as (role, text), oldest
        # first, the same transcript Claude's turns come from (quick commands and the local model's own answers
        # included), so both brains hear one conversation. ``busy``: what a turn may be about that only Claude can
        # see (a screen task running or paused, a question of it waiting), or None.
        self.conversation: Callable[[], list[tuple[str, str]]] | None = None
        self.busy: Callable[[], str | None] | None = None
        # ``brain.answers`` as it is now: "auto", "claude" or "local" (set by the app from her config).
        self.answers: Callable[[], str] = lambda: "auto"
        self.transport: httpx.AsyncBaseTransport | None = None  # tests answer for the server
        self._pruned = False

    # The assistant sets the watchdog's heartbeat on the brain it holds: Claude's turns need it.
    @property  # type: ignore[override]
    def heartbeat(self) -> Callable[[], None] | None:
        return self._heartbeat

    @heartbeat.setter
    def heartbeat(self, value: Callable[[], None] | None) -> None:
        self._heartbeat = value
        self.inner.heartbeat = value

    def __getattr__(self, name: str) -> Any:
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)  # anything else the brain offers (only called for what isn't found here)

    @property
    def session_id(self) -> str | None:
        return self.inner.session_id

    async def start(self) -> None:
        await self.inner.start()

    async def interrupt(self) -> None:
        self._stop.set()
        await self.inner.interrupt()

    async def abandon_turn(self) -> None:
        self._stop.set()
        await self.inner.abandon_turn()

    async def reset(self) -> None:
        self._said.clear()
        await self.inner.reset()

    async def start_early(self, turn: UserTurn) -> bool:
        """Only a turn going straight to Claude starts early: one the local model hears first may never reach it."""
        if self.route(turn) is not None:
            return False
        return await self.inner.start_early(self._with_notes(turn))

    def drop_early(self, why: str = "") -> None:
        self.inner.drop_early(why)

    async def close(self) -> None:
        await self.inner.close()

    # ------------------------------------------------------------------ a turn
    def route(self, turn: UserTurn) -> str | None:
        """Whether the local model hears this turn first ("route", or "down" while Claude is), or None: Claude's."""
        if turn.source not in USER_SOURCES:
            return None
        answers = self._answers()
        if answers == "claude":
            return None
        if answers == "local":
            return "local"
        if self._clock() < self._down_until:
            return "down"
        if self.config.mode != "route" or self.claudes(turn):
            return None
        return "route"

    def claudes(self, turn: UserTurn) -> str | None:
        """Why one of your turns goes straight to Claude in "route" mode, without asking the local model, or None."""
        if turn.picture is not None:
            return "picture"  # what they circled: the local model can't see it
        if required_note(turn.notes):
            return "notes"
        said = self._core(turn.text)
        if not said:
            return "empty"
        if len(said.split()) > self.config.route_max_words:
            return "long"
        if self._busy():
            return "work"  # "continue", "yes", "is it done?" may be about the screen task: only Claude sees it
        if (why := follows_up(said, self._lines(turn.text))) is not None:
            return f"context: {why}"  # a yes, a "continue", an answer to her question: Claude has the conversation
        return policy(said)

    def _answers(self) -> str:
        try:
            return self.answers()
        except Exception:
            log.warning("Reading who answers failed", exc_info=True)
            return "auto"

    def _busy(self) -> str | None:
        try:
            return self.busy() if self.busy is not None else None
        except Exception:
            log.warning("Asking whether a screen task is going failed", exc_info=True)
            return "unknown"  # can't tell: Claude, which can

    def _lines(self, now: str) -> list[tuple[str, str]]:
        """The conversation's latest lines before this turn (``now``, which the assistant records before asking a
        brain: it isn't history), from ``conversation``, or the local model's own record of it when nothing shares
        the transcript (tests, the shadow)."""
        if self.conversation is None:
            return [line for said, reply in self.local.history() for line in (("user", said), ("assistant", reply))]
        try:
            lines = list(self.conversation())
        except Exception:
            log.warning("Reading the conversation for the local brain failed", exc_info=True)
            return []
        if lines and lines[-1] == ("user", now):
            lines.pop()
        return lines

    def _history(self, now: str) -> list[tuple[str, str]]:
        """What the local model is shown of the conversation before this turn (``now``): its last ``history_turns``
        exchanges, as (what they said, what she said), each line clipped."""
        pairs: list[tuple[str, str]] = []
        for role, text in self._lines(now):
            text = " ".join(text.split())[:_LINE_CHARS]
            if role == "user":
                pairs.append((text, ""))
            elif pairs and not pairs[-1][1]:
                pairs[-1] = (pairs[-1][0], text)
            else:
                pairs.append(("", text))  # she spoke first (an announcement)
        keep = self.config.history_turns
        return pairs[-keep:] if keep else []

    async def respond(self, turn: UserTurn) -> AsyncIterator[BrainEvent]:
        self._stop = asyncio.Event()
        started = time.perf_counter()
        record: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "source": turn.source,
            "text": turn.text[:_CLIP],
            "consulted": False,
        }
        why = self.route(turn)
        if why is None and self.config.mode == "route" and turn.source in USER_SOURCES and self._answers() == "auto":
            record |= {"decision": "policy", "policy": self.claudes(turn)}
            if record["policy"] == "notes":
                record["note"] = required_note(turn.notes)
        tried_local = False
        local_says = ""  # the local model's plain line on what it couldn't do, said if Claude can't answer either
        self._confidence = None
        if why is not None:
            self.inner.drop_early("the local brain hears it first")  # Claude may never get it
        if why is not None and (tool := _plainly_asked(self._core(turn.text))) is not None:
            # "restart", "shut down": run as asked, never left to the model, which once said "You got it, restarting
            # now." and did nothing (local only, 2026-10-26). Approvals as for any call; it says what really happened.
            result = await self.executor.execute(tool, {})
            line = result.content if result.ok else f"That didn't work: {result.content}"
            async for event in self._say_plainly(turn, line, why):
                yield event
            self._log(record | {"brain": "local", "why": why, "decision": "direct", "tool": tool}, started)
            return
        if why is not None and await self.local.ready():
            tried_local = True
            record["consulted"] = True
            try:
                try:
                    async for event in self._local_turn(turn, why):
                        yield event
                except LocalFailed as first:
                    # Set to local only, a handover or a reply the checks refuse has no one to go to: one more try
                    # in plain words, no tools, before the plain "can't" line (2026-10-26: "Now go ahead and moan"
                    # got that line every time while the model only wanted to hand it over).
                    if why != "local" or first.reason not in _PLAIN_RETRY or self._stop.is_set():
                        raise
                    record["retried"] = first.reason
                    async for event in self._local_turn(turn, why, plain=True):
                        yield event
                self._log(record | self._sure() | {"brain": "local", "why": why, "decision": "answer"}, started)
                return
            except LocalFailed as exc:
                record |= self._sure()
                if self._stop.is_set():
                    self._log(record | {"brain": "local", "why": why, "interrupted": True}, started)
                    yield ResponseDone(text="", interrupted=True, model=self.model)
                    return
                local_says = exc.say
                if exc.reason == "delegated":
                    log.info("Local brain handed the turn to Claude (%s)", exc.because or "no reason given")
                    record |= {"decision": "delegate", "delegated_because": exc.because}
                else:
                    log.info("Local brain couldn't answer (%s): Claude answers", exc.reason)
                    record |= {"decision": "fallback", "fallback": exc.reason}
        elif why is not None:
            record |= {"decision": "fallback", "fallback": "not_loaded"}
        if why == "local":  # set to local only: never Claude's
            async for event in self._say_plainly(turn, local_says or (LOCAL_ONLY if tried_local else NOT_LOADED), why):
                yield event
            self._log(record | {"brain": "local", "why": why}, started)
            return

        said_anything = False
        async for event in self.inner.respond(self._with_notes(turn)):
            if isinstance(event, ResponseDone):
                if event.error and not said_anything and not event.text and not event.interrupted:
                    if turn.source in USER_SOURCES and not self._stop.is_set() and self._answers() != "claude":
                        if self._clock() >= self._down_until:
                            log.warning("Claude failed (%s): your turns go to the local brain for now", event.error)
                        self._down_until = self._clock() + self.config.claude_retry_s
                        if not tried_local and await self.local.ready():
                            record["consulted"] = True
                            try:
                                async for local_event in self._local_turn(turn, "down"):
                                    yield local_event
                                self._log(record | {"brain": "local", "why": "down", "claude": event.error}, started)
                                return
                            except LocalFailed as exc:
                                record["fallback"] = exc.reason
                                local_says = exc.say
                        if local_says:
                            async for local_event in self._say_plainly(turn, local_says):
                                yield local_event
                            self._log(record | {"brain": "local", "why": "down", "claude": event.error}, started)
                            return
                elif not event.error:
                    if self._down_until:
                        log.info("Claude is answering again")
                    self._down_until = 0.0
                    self._announced = False
                    self._said.clear()
                self._log(record | {"brain": "claude", "why": why or "claude"}, started, error=event.error)
                yield event
                return
            said_anything = True
            yield event

    def _sure(self) -> dict[str, float]:
        """How sure the local model was that it should answer the turn itself, for the log (when it was asked)."""
        return {} if self._confidence is None else {"confidence": round(self._confidence, 3)}

    def _with_notes(self, turn: UserTurn) -> UserTurn:
        """Claude's turn, told what the local model said since Claude last answered."""
        if not self._said:
            return turn
        heard = "; ".join(f'they said "{said}", you said "{reply}"' for said, reply in self._said)
        note = f"since your last answer, your local brain answered them itself: {heard}"
        return dataclasses.replace(turn, notes=(*turn.notes, note))

    async def _say_plainly(self, turn: UserTurn, line: str, why: str = "down") -> AsyncIterator[BrainEvent]:
        """Claude can't answer (or may not: local only) and the local model couldn't do it: what it couldn't do,
        said plainly."""
        if why == "down" and not self._announced:
            self._announced = True
            line = f"{LOCAL_LINE} {line}"
        self._said.append((turn.text[:_CLIP], line[:_CLIP]))
        yield TextDelta(line)
        yield ResponseDone(text=line, model=self.model)

    async def _local_turn(self, turn: UserTurn, why: str, *, plain: bool = False) -> AsyncIterator[BrainEvent]:
        """The local model's turn. Raises ``LocalFailed`` only before anything is yielded. ``plain``: words only, no
        tools (local only's second try); every check still applies."""
        turn = _without_picture(turn)
        down = why in ("down", "local")  # no one to hand it to: Claude is down, or it's set to local only
        # Set to local only, talk that asks for nothing to be done gets no tools: it checked the time and listed the
        # apps all through a chat on 2026-10-26 (14 of 63 turns).
        plain = plain or (why == "local" and not _WANTS_TOOL.search(self._core(turn.text)))
        allowed = frozenset() if plain else self._route_tools | READ_TOOLS | (ACT_TOOLS if down else frozenset())
        timeout = self.config.fallback_timeout_s if down else self.config.answer_timeout_s
        note = _PLAIN if plain else _on_your_own(allowed, why) if down else ""
        if why == "local" and self.config.uncensored and _UNCENSORED:
            note += _UNCENSORED
        body = await self.local.body(turn.render() + note, self._history(turn.text))
        if plain:
            body |= {"tool_choice": "none"}
        messages: list[dict[str, Any]] = body["messages"]
        # Its first token's alternatives: how likely it was to hand the turn over, which says how sure it is.
        first = await self._ask(body | {"logprobs": True, "top_logprobs": _TOP_LOGPROBS}, timeout)
        if (because := delegation_reason(_words(first))) is not None:
            raise LocalFailed("delegated", because)
        odds = delegate_odds(first)
        self._confidence = None if odds is None else 1.0 - odds
        if not down and self._confidence is not None and self._confidence < self.config.route_min_confidence:
            raise LocalFailed("unsure")  # it nearly handed it over: a coin flip isn't an answer
        text, calls = self._parse(first, allowed, safe_only=not down)
        if echoes_context(text, (turn.render().split("\n", 1)[0], *turn.notes, note)):
            raise LocalFailed("echoed_context")  # it read its notes back instead of answering
        if UNCHECKED.search(text):
            raise LocalFailed("unchecked_claim", say=CANT_CHECK)  # her updates: it can't know, it would only guess
        if down and _actions(calls) > 1:
            raise LocalFailed("several_actions", say=SEVERAL_STEPS)
        if not calls:
            if not down and COMMAND.search(self._core(turn.text)):
                raise LocalFailed("no_tool")
            if CLAIMED.search(text):
                raise LocalFailed("claimed_action", say=NEEDS_CLOUD)  # it would only say it did it
            if not text or _WRITTEN.search(text):
                raise LocalFailed("bad_reply")

        pieces: list[str] = []
        if why == "down" and not self._announced:
            self._announced = True
            pieces.append(LOCAL_LINE + " ")
            yield TextDelta(LOCAL_LINE + " ")
        results_ok = True
        ran: set[str] = set()
        succeeded: set[str] = set()
        steps = 0
        while True:
            if text:
                pieces.append(text)
                yield TextDelta(text)
            if not calls or self._stop.is_set() or steps == _MAX_STEPS:
                break
            steps += 1
            messages.append(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.raw}}
                        for c in calls
                    ],
                }
            )
            for call in calls:
                yield ToolUseStarted(call_id=call.id, name=call.name)
                result = await self.executor.execute(call.name, call.args, call_id=call.id)
                results_ok = results_ok and result.ok
                ran.add(call.name)
                if result.ok:
                    succeeded.add(call.name)
                content = result.content if result.ok else f"{result.content}\n{_FAILED_NOTE}"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if self._stop.is_set():
                break
            try:
                reply = await self._ask(body | {"messages": messages}, timeout)
                text, calls = self._parse(reply, allowed, safe_only=not down)
            except LocalFailed as exc:
                log.info("Local brain: no reply after its tools (%s)", exc.reason)
                text, calls = (exc.say or _after_tools(results_ok, ran)), []
            if _WRITTEN.search(text):
                text = _WRITTEN.sub("", text).strip()
            if echoes_context(text, turn.notes):
                text, calls = _after_tools(results_ok, ran), []
            if down and _actions(calls) and (ran & ACT_TOOLS or _actions(calls) > 1):
                # another action on the screen makes it several steps: the rest is the cloud brain's
                text, calls = (ONE_STEP if results_ok else _after_tools(False, ran)), []
            # after looking (the time, the apps open), "I checked" or "I found it" is true: only a change it claims
            claims = ACTED if succeeded else CLAIMED
            if not calls and not succeeded - READ_TOOLS and claims.search(text):
                # nothing that acts succeeded: whatever it says it did, it didn't
                text = NEEDS_CLOUD if ran <= READ_TOOLS else _after_tools(False, ran)
        full = "".join(pieces).strip()
        if full:
            self._said.append((turn.text[:_CLIP], full[:_CLIP]))
        yield ResponseDone(text=full, interrupted=self._stop.is_set(), model=self.model)

    async def _ask(self, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        """One whole reply from the server, or ``LocalFailed``; an interrupt ends it at once."""

        async def post() -> dict[str, Any]:
            async with local_client(timeout=timeout, transport=self.transport) as client:
                response = await client.post(
                    f"{self.local.server.url}/v1/chat/completions", json=body | {"stream": False}
                )
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            return response.json()

        request = asyncio.create_task(post())
        stopped = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait({request, stopped}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
            if not request.done():
                request.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await request
        if request not in done:
            raise LocalFailed("interrupted" if self._stop.is_set() else "timeout")
        try:
            choice = request.result()["choices"][0]
            message = choice["message"]
            if choice.get("logprobs"):  # beside the message in the server's reply: kept with it
                message = {**message, "logprobs": choice["logprobs"]}
            return message
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise LocalFailed(f"error: {type(exc).__name__}: {exc}"[:200]) from None

    def _parse(self, message: dict[str, Any], allowed: frozenset[str], *, safe_only: bool) -> tuple[str, list[_Call]]:
        """Its words and its tool calls, every call checked against the tools it may use and their arguments."""
        text = _words(message)
        calls: list[_Call] = []
        for i, (call_id, name, raw) in enumerate(_tool_calls(message)):
            tool = self.executor.registry.get(name)
            if tool is None or name not in allowed:
                raise LocalFailed(f"tool_not_allowed: {name}"[:200], say=NEEDS_CLOUD)
            if safe_only and tool.risk != RiskLevel.SAFE:
                # a routed turn never takes one the user approves
                raise LocalFailed(f"tool_not_safe: {name}"[:200], say=NEEDS_CLOUD)
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(args, dict):
                    raise ValueError("not an object")
                tool.Args.model_validate(args)
            except (ValueError, ValidationError):
                raise LocalFailed(f"bad_args: {name}", say=BAD_CALL) from None
            calls.append(_Call(call_id or f"local-{i}", name, args, json.dumps(args)))
        return text, calls

    def _log(self, record: dict[str, Any], started: float, error: str | None = None) -> None:
        record["total_s"] = round(time.perf_counter() - started, 3)
        if error:
            record["error"] = error[:200]
        log.info(
            "Turn answered by %s (%s)",
            record["brain"],
            record.get("fallback") or record.get("policy") or record.get("decision") or record["why"],
        )
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._pruned:
                self._pruned = True
                self._prune()
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("Couldn't write the local brain route log", exc_info=True)

    def _prune(self) -> None:
        """Lines older than the shadow log keeps go, once: it holds what you said."""
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return
        cutoff = time.time() - _RETENTION_S
        kept = [line for line in lines if _ts(line) >= cutoff]
        if len(kept) < len(lines):
            self.log_path.write_text("".join(kept), encoding="utf-8")


def _words(message: dict[str, Any]) -> str:
    """What it said, without its thinking, tool calls written into its reply, or the bracketed context before the
    user's words (the time, notes) when it echoes it: none of that is ever spoken."""
    text = re.sub(r"<think>.*?</think>", "", message.get("content") or "", flags=re.S)
    # Curly apostrophes as straight ones: "I’ll read it" got past every check written for "I'll" (2026-10-26)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = _CONTEXT_LINE.sub("", _TOOL_CALL.sub("", text))
    return _CONTEXT_START.sub("", text.strip()).strip()


def _actions(calls: list[_Call]) -> int:
    """How many of these calls act on the screen or the music (``ACT_TOOLS``)."""
    return sum(call.name in ACT_TOOLS for call in calls)


def _tool_calls(message: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """Its tool calls as (id, name, arguments): the server's parsed list, or else the ``<tool_call>{"name": ...,
    "arguments": {...}}</tool_call>`` blocks of Qwen's own format left in its words when the server didn't parse them.
    A call that isn't exactly that shape is ``LocalFailed``, never guessed at."""
    found: list[tuple[str, str, Any]] = []
    for call in message.get("tool_calls") or ():
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
            raise LocalFailed("bad_tool_call", say=BAD_CALL)
        found.append((call.get("id") or "", fn["name"], fn.get("arguments") or "{}"))
    if found:
        return found
    for block in _TOOL_CALL.findall(message.get("content") or ""):
        try:
            call = json.loads(block)
        except ValueError:
            raise LocalFailed("bad_tool_call", say=BAD_CALL) from None
        if not isinstance(call, dict) or set(call) - {"name", "arguments"} or not isinstance(call.get("name"), str):
            raise LocalFailed("bad_tool_call", say=BAD_CALL)
        found.append(("", call["name"], call.get("arguments", {})))
    return found
