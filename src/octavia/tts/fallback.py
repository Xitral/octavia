"""A backup voice for when the main one (Fish Audio) can't speak.

``FallbackTTS`` wraps the main provider. Each sentence goes to it first; if it
fails before any audio arrives (refused, unreachable, a handshake timeout),
that same sentence is spoken by the backup (the built-in Windows voice), so the
reply is heard instead of lost. After a failure the backup speaks everything
for ``cooldown_s``, so the following sentences don't each wait out the same
failure, and then the main voice is tried again.

If the main voice fails part-way through a sentence, what was already heard
isn't repeated: the rest of that sentence is dropped and the next sentence is
spoken by the backup.

The wrapper looks like the main voice to everything else: same audio format
(the backup is asked for that rate), same tone cues and style hint (the backup
drops cues itself, so the chunker's handling stays the same whichever voice
ends up speaking a sentence), and phrases are only ever cached from the main
voice (``cacheable``).

The switch is never silent: the reason is logged, and with ``announce`` set
(the assistant's) she says once per outage, in plain words, why her voice
changed. ``last_engine`` and ``last_failure`` record which voice spoke last
and why the main one last failed, for the voice_engine tool.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from octavia.core.events import EventBus, Notice
from octavia.tts.base import STARTED, NotReady, TTSError, TTSProvider
from octavia.tts.delivery import SpeakingStyle

log = logging.getLogger(__name__)

NOTICE = "Fish Audio isn't answering, so I'm using the Windows voice for now"
# What counts as the main voice failing; anything else (a bug) still surfaces as a speech error.
# TimeoutError is an OSError.
PRIMARY_FAILURES = (TTSError, OSError)
MAIN, BACKUP = "fish_audio", "windows"
ONLY_MAIN, ONLY_BACKUP = "main", "backup"  # ``FallbackTTS.only``
# Sentences closer together than this are one reply, said in one voice: switching voices mid-reply sounds like two
# people (the user heard it as swapping). Tool steps in the middle of a reply take ~2 s.
REPLY_GAP_S = 10.0
# How long a sentence may wait for the one before it to be made (a voice that ``waits_its_turn``).
TURN_S = 30.0
# Fish Audio's errors (fish_audio._describe_http_status) as a few words that finish "because Fish Audio ...".
_SPOKEN_STATUS = {
    "(401)": "rejected the API key",
    "(402)": "says the account is out of credit",
    "(403)": "refused the request",
    "(404)": "couldn't find the voice I'm set to",
    "(429)": "hit its rate limit",
}


def spoken_reason(exc: BaseException) -> str:
    """Why the main voice failed, short enough to say out loud (the full error goes to the log and the brain)."""
    message = str(exc)
    for code, words in _SPOKEN_STATUS.items():
        if code in message:
            return words
    if message.startswith("no audio from "):
        return "took too long to start speaking"
    if isinstance(exc, OSError) or "Can't reach" in message or "connection" in message.lower():
        return "couldn't be reached"
    return "returned an error"


class FallbackTTS(TTSProvider):
    def __init__(
        self,
        primary: TTSProvider,
        backup: TTSProvider,
        events: EventBus | None = None,
        cooldown_s: float = 120.0,
        first_audio_s: float = 5.0,
        main_label: str = "Fish Audio",
        backup_label: str = "Windows voice",
    ) -> None:
        self.primary = primary
        self.backup: TTSProvider | None = backup  # None once it couldn't start: then it's just the main voice
        self.events = events
        # How the voices are named in what she says and logs: "Fish Audio" and "Windows voice", or, with her local
        # voice in front, "my local voice" and "Fish Audio voice".
        self.main_label = main_label
        self.backup_label = backup_label
        self.cooldown_s = cooldown_s
        # The main voice's first audio of a sentence must come this soon, or the backup says it: a stalled
        # connection (Fish's own retries can take 40 s) would otherwise be that long a silence.
        self.first_audio_s = first_audio_s
        self.name = primary.name
        self.format = primary.format
        self.allowed_tags = getattr(primary, "allowed_tags", frozenset())
        self.pause_tags = getattr(primary, "pause_tags", frozenset())
        self.inline_tags = getattr(primary, "inline_tags", frozenset())
        self._down_until = 0.0  # monotonic time until which the backup speaks everything
        self._outage = False  # told the user already; cleared when the main voice works again
        self.announce: Callable[..., Any] | None = None  # the assistant's ``announce``, set by the app
        self.last_engine = ""  # MAIN or BACKUP: which voice the last sentence was heard in
        self.last_failure = ""  # the main voice's last error, in full
        self.last_failure_at = 0.0  # wall-clock time of it
        # After a sentence gave up on the first audio: how much longer to wait to learn what really went wrong.
        self.learn_why_s = 10.0
        self._learning: asyncio.Task[None] | None = None  # ``_learn_why``, while it waits
        self._tell_later = False  # the switch is said once ``_learn_why`` knows why
        # One voice per reply: which one this reply is in (None until its first sentence), and when the last
        # sentence ended; a longer pause than REPLY_GAP_S starts a new reply.
        self._reply_on_backup: bool | None = None
        self._last_said = float("-inf")
        # A voice picked by the user (``tts.provider``, ``core/engines.py``): ONLY_MAIN, the main voice alone (it
        # failing is a speech error, never the backup); ONLY_BACKUP, the backup alone (chosen, so never announced);
        # None, both as above.
        self.only: str | None = None

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        await self.primary.start()  # its setup problems (no key) are still reported as before
        try:
            await self.backup.start()
        except Exception as exc:
            log.warning("No backup voice (%s); speech stops if Fish Audio fails", exc)
            self.backup = None
        else:
            log.info("Backup voice ready: %s", getattr(self.backup, "voice_name", "") or self.backup.name)

    async def close(self) -> None:
        learning, self._learning = self._learning, None
        if learning is not None and not learning.done():
            learning.cancel()
            await asyncio.wait({learning})  # it closes the stream it waited on
        try:
            await self.primary.close()
        finally:
            if self.backup is not None:
                await self.backup.close()

    def prewarm(self) -> None:
        if not self.on_backup:
            self.primary.prewarm()
        elif self.only == ONLY_BACKUP and self.backup is not None:
            self.backup.prewarm()

    # -- what the rest of the app sees -----------------------------------------------
    @property
    def notice(self) -> str:
        if (self.main_label, self.backup_label) == ("Fish Audio", "Windows voice"):
            return NOTICE
        return f"{self.main_label} isn't answering, so I'm using the {self.backup_label} for now"

    @property
    def on_backup(self) -> bool:
        """True while the backup speaks everything: the cooldown after the main voice failed, and the rest of a
        reply the backup started (one voice per reply)."""
        if self.backup is None or self.only == ONLY_MAIN:
            return False
        if self.only == ONLY_BACKUP:
            return True
        in_reply = self._reply_on_backup is True and time.monotonic() - self._last_said <= REPLY_GAP_S
        return time.monotonic() < self._down_until or in_reply

    def backup_left_s(self) -> float:
        """How much longer the backup speaks everything before the main voice is tried again (0 when it isn't)."""
        return max(0.0, self._down_until - time.monotonic()) if self.on_backup else 0.0

    def _picked(self) -> TTSProvider:
        """The voice that speaks when nothing fails: the main one, or the backup when it alone was picked."""
        return self.backup if self.only == ONLY_BACKUP and self.backup is not None else self.primary

    def style_hint(self) -> str | None:
        return self.primary.style_hint()

    def use_style(self, style: SpeakingStyle) -> bool:
        """Hand ``style`` to the voice speaking; False when it can't perform one (the backup speaks normally)."""
        use = getattr(self._picked(), "use_style", None)
        return bool(use(style)) if use is not None else False

    def cache_key(self) -> str:
        return self._picked().cache_key()

    def cacheable(self) -> TTSProvider:
        # Her preset phrases in the voice picked: "On it!" in her local voice and the reply in Fish's would be two voices.
        return self._picked().cacheable()

    # -- speaking ------------------------------------------------------------------
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if self.backup is None or self.only == ONLY_MAIN:
            for attempt in range(2):
                try:
                    async with contextlib.aclosing(self.primary.synthesize(text)) as stream:
                        async for chunk in stream:
                            if chunk == STARTED:
                                continue
                            self.last_engine = MAIN
                            yield chunk
                    return
                except NotReady as exc:
                    # Nothing else may say it (no backup, or the main voice alone was picked): wait for it to load.
                    wait_ready = getattr(self.primary, "wait_ready", None)
                    if attempt or wait_ready is None or not await wait_ready():
                        self._record_failure(exc)
                        raise
                except PRIMARY_FAILURES as exc:
                    self._record_failure(exc)
                    raise
            return
        if not text.strip():
            return
        if time.monotonic() - self._last_said > REPLY_GAP_S:
            self._reply_on_backup = None  # a new reply: the main voice gets its chance again
        try:
            async with contextlib.aclosing(self._speak(text)) as stream:
                async for chunk in stream:
                    yield chunk
        finally:
            self._last_said = time.monotonic()

    async def _speak(self, text: str) -> AsyncIterator[bytes]:
        if not self.on_backup:
            heard = False
            try:
                stream = self.primary.synthesize(text)
                handed_on = False  # the stream went to ``_learn_why``, which closes it
                try:
                    # A voice that makes one sentence at a time says STARTED when this one's turn comes: until then
                    # the wait is the sentence before it being made, which gets TURN_S, not the first-audio limit.
                    waiting_turn = bool(getattr(self.primary, "waits_its_turn", False))
                    while True:
                        first_chunk = asyncio.ensure_future(anext(stream, None))
                        limit = TURN_S if waiting_turn else self.first_audio_s
                        try:
                            await asyncio.wait({first_chunk}, timeout=limit)
                        except BaseException:  # cancelled (the user cut in): let it stop before the stream closes
                            first_chunk.cancel()
                            await asyncio.wait({first_chunk})
                            _settle(first_chunk)
                            raise
                        if not first_chunk.done():
                            # Too slow for this sentence, but not given up on: the real error (out of credit, a
                            # rejected key) often comes just after, and that's what she should say, not "too slow".
                            timeout = TTSError(f"no audio from {self.main_label} within {limit:.0f} s")
                            handed_on = True
                            self._learning = asyncio.create_task(self._learn_why(first_chunk, stream, str(timeout)))
                            raise timeout
                        first = first_chunk.result()
                        if first != STARTED:
                            break
                        waiting_turn = False
                        # The sentence waited its turn behind the one before it: the clock starts now, so the wait
                        # isn't taken for a voice that's failing.
                    if first is not None:
                        heard = True
                        self.last_engine = MAIN
                        self._reply_on_backup = False
                        yield first
                        async for chunk in stream:
                            if chunk != STARTED:
                                yield chunk
                finally:
                    if not handed_on:
                        await stream.aclose()
            except NotReady as exc:
                log.debug("%s isn't ready (%s); the backup says this reply", self.main_label, exc)
                self._reply_on_backup = True  # one voice per reply: never back and forth in the middle of one
            except PRIMARY_FAILURES as exc:
                self._primary_failed(exc)
                if heard:
                    return  # part of it was heard: don't repeat it; the next sentence is the backup's
            else:
                self._primary_worked()
                return
        try:
            async with contextlib.aclosing(self.backup.synthesize(text)) as stream:
                async for chunk in stream:
                    self.last_engine = BACKUP
                    yield chunk
        except Exception as exc:
            raise TTSError(f"{self.main_label} failed and so did the backup voice: {exc}") from exc

    def _record_failure(self, exc: BaseException) -> None:
        self.last_failure = str(exc) or type(exc).__name__
        self.last_failure_at = time.time()

    def _primary_failed(self, exc: BaseException) -> None:
        self._down_until = time.monotonic() + self.cooldown_s
        self._reply_on_backup = True  # and the rest of this reply, even if the cooldown ends during it
        self._record_failure(exc)
        if self._outage:
            log.info("%s still failing (%s); backup voice for another %.0f s", self.main_label, exc, self.cooldown_s)
            return
        self._outage = True
        log.warning("%s failed (%s); using the %s for the next %.0f s", self.main_label, exc, self.backup_label,
                    self.cooldown_s)  # fmt: skip
        if self.events is not None:
            self.events.emit(Notice(source="TTS", message=self.notice, level=logging.WARNING))
        if self._learning is not None and not self._learning.done():
            self._tell_later = True  # said once ``_learn_why`` knows the real reason
            return
        self._tell(exc)

    async def _learn_why(self, first_chunk: asyncio.Future[Any], stream: Any, timeout: str) -> None:
        """After a sentence gave up waiting for Fish Audio's first audio: wait a little longer for what it says, so the
        reason she gives and ``last_failure`` (the voice_engine tool) are the real one ("out of credit"), not the
        timeout. Then close the stream, and say the switch if ``_primary_failed`` left it for this."""
        found: BaseException | None = None
        try:
            await asyncio.wait({first_chunk}, timeout=self.learn_why_s)
            if isinstance(_settle(first_chunk), PRIMARY_FAILURES):
                found = _settle(first_chunk)
        finally:
            # Still nothing (or she's shutting down): the stream can only be closed once that stops.
            if not first_chunk.done():
                first_chunk.cancel()
                await asyncio.wait({first_chunk})
                _settle(first_chunk)
            with contextlib.suppress(Exception):
                await stream.aclose()
        # Not reached when she's shutting down (cancelled): nothing is said then.
        if found is not None and self.last_failure == timeout:  # nothing newer has failed since
            self.last_failure = str(found) or type(found).__name__
            log.warning("%s's actual problem: %s", self.main_label, self.last_failure)
        if self._tell_later:
            self._tell_later = False
            self._tell(found or TTSError(timeout))

    def _tell(self, exc: BaseException) -> None:
        """Say, once per outage, that the backup voice is speaking and why."""
        if self.announce is not None:
            try:
                self.announce(
                    f"I'm using my backup {self.backup_label} for now, because {self.main_label} {spoken_reason(exc)}.",
                    note=f"Your {self.main_label} voice failed ({self.last_failure}); the backup {self.backup_label} "
                    "is speaking.",
                    listen=False,
                    relay=False,
                )
            except Exception:
                log.warning("Couldn't announce the switch to the backup voice", exc_info=True)

    def _primary_worked(self) -> None:
        if self._outage:
            self._outage = False
            log.info("%s is answering again; back to her own voice", self.main_label)


def voices(tts: TTSProvider | None) -> list[TTSProvider]:
    """Every voice behind ``tts``, main voice first: [local, Fish, Windows] with her local voice in front."""
    found: list[TTSProvider] = []
    while isinstance(tts, FallbackTTS):
        found.append(tts.primary)
        tts = tts.backup
    if tts is not None:
        found.append(tts)
    return found


def _settle(task: asyncio.Future[Any]) -> BaseException | None:
    """The finished ``task``'s error (None when it worked, was cancelled or hasn't finished), read so it's never
    reported as an error nobody looked at."""
    if not task.done() or task.cancelled():
        return None
    return task.exception()
