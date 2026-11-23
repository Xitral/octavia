"""Conversation sessions: one brain context per stretch of conversation.

A session starts at the first interaction and expires after
``conversation.session_timeout_s`` of inactivity. Expiry is checked lazily
at the next interaction (or ended early by ``expire`` from an idle timer),
and then the brain context is reset so a new conversation starts clean. Turn
history goes to the ConversationStore for continuity and debugging, and a
summary of the conversation is saved with it when it ends.

A long conversation is also started afresh for the brain once what it carries
gets big (``conversation.max_context_tokens``): a very long context makes her
slower to answer (measured 2026-11-22 on 1,069 of her turns that read their
prompt from the model's cache: 1.5 to 2.1 s to the first word anywhere from 30k
to 90k tokens, 2.3 s at 90-120k, 5.6 s past 120k). Only the brain starts over:
the conversation itself goes on, with what was asked, approved or set in it
kept, and her fresh prompt carries it on from the transcript
(``conversation_memory``: its summary and its last exchanges, words only). That
happens between turns, never while a turn, a screen task or a question of hers
waits on the old context (``may_roll``).

Starting afresh costs too: the brain reconnects and reads a prompt the model
has never seen (about 6 s before it answers at full speed again), and the
tools it had looked up and what they brought back are gone, so the turns right
after one are slower. The limit in use (``limit``) therefore never sits closer
than ``MIN_ROOM_TOKENS`` above what a fresh prompt carries (``fresh_tokens``,
learned from the first model call after each fresh start): her prompt grew
from 31k to 47k tokens in a month, and at 47k a limit of 50k started her
afresh every fourth turn (75 times in 328 spoken turns, 2026-11-12 to 11-22).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable

from octavia.brain.base import BrainProvider
from octavia.core.conversation_memory import session_summary
from octavia.core.events import EventBus, SessionEnded, SessionStarted
from octavia.memory.base import ConversationStore

log = logging.getLogger(__name__)

# The least a conversation may add to a fresh prompt before the brain starts afresh for its size: a limit set (or
# left) closer than this to what the prompt itself carries would start her afresh every few turns.
MIN_ROOM_TOKENS = 20_000


class SessionManager:
    def __init__(
        self,
        brain: BrainProvider,
        events: EventBus,
        timeout_s: float,
        store: ConversationStore | None = None,
        max_context_tokens: int = 0,
    ) -> None:
        self.brain = brain
        self.events = events
        self.timeout_s = timeout_s
        self.store = store
        self.max_context_tokens = max_context_tokens  # 0 = never start the brain afresh for its size
        self.session_id: str | None = None
        self.last_activity = 0.0
        self.context_tokens: int | None = None  # what the brain carried in the last finished turn
        # What a fresh prompt carries: the first model call's whole input after the brain last started afresh.
        self.fresh_tokens: int | None = None
        self._fresh_next = False  # the brain was just started afresh: its next turn's first call tells
        self._told_room = False  # the log has said once that the limit set leaves a conversation too little room
        # Whether nothing waits on the brain's context right now (set by the app: no turn running, no screen task,
        # no question of hers waiting for its answer).
        self.may_roll: Callable[[], bool] = lambda: True
        # A background expire() may still be resetting the brain when the user wakes it up.
        self._lock = asyncio.Lock()

    def expired(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.session_id is None or (now - self.last_activity) > self.timeout_s

    async def touch(self) -> str:
        """Call at every interaction; rotates the session if it has gone stale, and starts the brain afresh if its
        context has grown too big (``roll_due``) and nothing waits on it."""
        async with self._lock:
            if self.expired():
                await self._rotate("inactivity" if self.session_id else "start")
            elif self.roll_due:
                await self._roll()
            self.last_activity = time.monotonic()
            assert self.session_id is not None
            return self.session_id

    def note_turn(self, tokens: int | None, first: int | None = None) -> None:
        """A brain turn finished carrying ``tokens`` (its last model call's whole input), for ``roll_due``. A turn
        that didn't say leaves the last figure as it was. ``first``: its first model call's whole input, which on
        the turn right after a fresh start is what a fresh prompt carries (``fresh_tokens``)."""
        if tokens is not None:
            self.context_tokens = tokens
        if self._fresh_next and first is not None:
            self._fresh_next = False
            # A first request that's huge itself (a pasted document) isn't the prompt having grown: the last figure
            # stands, so one such turn doesn't push the limit out for the conversations after it.
            if self.fresh_tokens is None or first <= self.fresh_tokens + MIN_ROOM_TOKENS:
                self.fresh_tokens = first

    @property
    def limit(self) -> int:
        """The context size that starts the brain afresh: ``max_context_tokens``, and never less than
        ``MIN_ROOM_TOKENS`` above what a fresh prompt carries, once that's known (0 = never)."""
        limit = max(0, self.max_context_tokens)
        if limit == 0 or self.fresh_tokens is None:
            return limit
        return max(limit, self.fresh_tokens + MIN_ROOM_TOKENS)

    @property
    def roll_due(self) -> bool:
        """The brain's context has passed ``limit``: start it afresh before the next turn."""
        limit = self.limit
        return bool(limit > 0 and self.session_id and self.context_tokens is not None and self.context_tokens >= limit)

    async def roll_over(self) -> bool:
        """Start the brain afresh now if it's due and nothing waits on it (between turns, from an idle timer, so the
        next turn finds it ready). True when it did."""
        async with self._lock:
            if not self.roll_due:
                return False
            return await self._roll()

    async def tidy(self, idle: bool, early: bool = False) -> None:
        """The background check (every second or so): a session gone stale ends while she's idle, and a context grown
        too big starts afresh as soon as nothing waits on it (``may_roll``), idle or not. She's usually listening for
        their follow-up right after a reply, and the user often answers within seconds: starting afresh then, rather
        than when their next turn comes, lets the new conversation connect and warm its prompt cache before it's
        needed (a turn that started it afresh itself waited 1 to 2 s to connect and read its prompt uncached). Not
        while a turn started ``early`` in their pause waits: it went to the context there is."""
        if not self.session_id:
            return
        if self.expired():
            if idle:
                await self.expire("inactivity")
        elif self.roll_due and not early:
            await self.roll_over()

    async def _roll(self) -> bool:
        try:
            if not self.may_roll():
                return False
        except Exception:
            log.warning("Couldn't tell whether the brain may start afresh", exc_info=True)
            return False
        log.info(
            "Brain context at %d tokens (conversation.max_context_tokens %d%s): starting the brain afresh, the "
            "conversation goes on from its transcript (reason: context)",
            self.context_tokens or 0,
            self.max_context_tokens,
            self._room_note(),
        )
        self.context_tokens = None
        await self._reset_brain()
        return True

    def _room_note(self) -> str:
        """For the log when the limit in use isn't the one set, and once, plainly, why."""
        if self.fresh_tokens is None or self.limit == self.max_context_tokens:
            return ""
        if not self._told_room:
            self._told_room = True
            log.warning(
                "conversation.max_context_tokens is %d, but her fresh prompt alone carries %d tokens: starting afresh "
                "at %d instead, so a conversation has room for %d of its own",
                self.max_context_tokens,
                self.fresh_tokens,
                self.limit,
                MIN_ROOM_TOKENS,
            )
        return f", in use {self.limit}: a fresh prompt carries {self.fresh_tokens}"

    async def _reset_brain(self) -> None:
        try:
            await self.brain.reset()
        except Exception:
            log.exception("Brain reset failed")
        else:
            self._fresh_next = True  # its next turn's first model call reads the fresh prompt: that's its size

    def keep_alive(self) -> None:
        """Activity that needs no brain (a preset reply): extends a live session, never starts one."""
        if not self.expired():
            self.last_activity = time.monotonic()

    async def expire(self, reason: str) -> None:
        """End the session now (resetting the brain) so the next ``touch`` starts fresh without waiting."""
        async with self._lock:
            await self._end(reason)

    async def _rotate(self, reason: str) -> None:
        await self._end(reason)
        self.session_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        await self._safe(self.store.start_session(self.session_id, self.brain.name) if self.store else None)
        self.events.emit(SessionStarted(session_id=self.session_id))

    async def _end(self, reason: str) -> None:
        old, self.session_id = self.session_id, None
        self.context_tokens = None
        if old is None:
            return
        await self._safe(self.store.end_session(old, reason, await self._summary(old)) if self.store else None)
        self.events.emit(SessionEnded(session_id=old, reason=reason))
        await self._reset_brain()

    async def record_turn(self, role: str, text: str, interrupted: bool = False) -> None:
        if self.store and self.session_id and text:
            await self._safe(self.store.add_turn(self.session_id, role, text, interrupted))

    async def record_brain_session(self) -> None:
        brain_session = self.brain.session_id
        if self.store and self.session_id and brain_session:
            await self._safe(self.store.set_brain_session(self.session_id, brain_session))

    async def close(self) -> None:
        if self.session_id is not None and self.store:
            summary = await self._summary(self.session_id)
            await self._safe(self.store.end_session(self.session_id, "shutdown", summary))

    async def _summary(self, session_id: str) -> str | None:
        """What the ended conversation was about, saved with it for the next ones (``octavia.core.conversation_memory``)."""
        if self.store is None:
            return None
        try:
            return await session_summary(self.store, session_id) or None
        except Exception:
            log.exception("Couldn't summarize the conversation")
            return None

    @staticmethod
    async def _safe(coro) -> None:  # noqa: ANN001
        if coro is None:
            return
        try:
            await coro
        except Exception:
            log.exception("Conversation store write failed")
