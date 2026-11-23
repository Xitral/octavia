"""Her opening words in the turn loop: asked for in the user's pause, said as the start of her reply, and handed to
the brain (docs/octavia-2.0.md, decisions D-004 to D-006).

* **Asked early.** As soon as what the user has said is transcribed in a pause and sounds finished (``stt/early.py``),
  and the turn fits (``_opener_fits``: spoken, for the brain, three words or more, not an answer to her own question
  or offer, no screen task or call running), her opener (``brain/opener.py``) is asked for its phrase. Talking on
  drops it, as it drops the brain's early start.
* **Said only with ``conversation.opener: on``.** With headphones, where the early "On it!" would play 0.6 s into the
  pause, it waits up to ``opener_wait_ms`` for the opener instead. The brain's early start still begins then, with
  the acknowledgment's note, so its head start isn't lost; if the opener's phrase is said, that start is dropped and
  started again with the phrase's exact words (D-004: a running generation is never assumed to hear words said after
  it began). On speakers the phrase is said once the silence is over, waited for the same way. Either way the phrase
  plays on a speech stream of its own that the turn carries on (``_run_turn`` ``opened``), so the brain's words
  follow it in the same voice, with the next sentence's audio fetched while the phrase plays.
* **Shadow** (the default) asks and logs the verdict (``OpenerChecked``, the routing log's ``opener`` fields) and
  says nothing: the turn is exactly as without it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from octavia.brain.base import UserTurn
from octavia.core.events import OpenerChecked

if TYPE_CHECKING:
    from octavia.brain.opener import Opener, Opening
    from octavia.core.speech import SpeechStream

log = logging.getLogger(__name__)

# Shorter requests ("yes", "next song", "keep going") are answered or done at once: no opening words for them.
OPENER_MIN_WORDS = 3


@dataclass(eq=False)
class _Asked:
    """Opening words being asked for what the user said: in a pause (its endpointer ``sound_frames``) or, with
    ``sound_frames`` None, once their turn began."""

    text: str
    sound_frames: int | None
    task: asyncio.Task[Opening]
    asked: float = field(default_factory=time.perf_counter)
    stream: SpeechStream | None = None  # the stream it's said on, once it's said
    words: str | None = None  # what was said on it
    logged: bool = False


def _quiet_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("Asking for opening words failed", exc_info=task.exception())


class OpeningMixin:
    """The opener's part of ``Assistant``; see the module docstring."""

    opener: Opener | None
    _opening: _Asked | None

    # -- deciding and asking --------------------------------------------------
    def _opener_mode(self) -> str:
        """ "off", "shadow" or "on": ``conversation.opener``, and "off" when there's no opener to ask."""
        if self.opener is None:  # type: ignore[attr-defined]
            return "off"
        return self.config.conversation.opener  # type: ignore[attr-defined]

    def _opener_fits(self, text: str) -> bool:
        """Whether opening words could begin her reply to ``text``: it goes to the brain, it's long enough to have a
        subject, and it isn't her own question or offer being answered (that answer is for the brain to act on)."""
        a: Any = self
        if self._opener_mode() == "off" or not self.opener.ready:  # type: ignore[union-attr]
            return False
        if not a.speech_enabled or a.speech is None or a.on_call:
            return False
        if len(text.split()) < OPENER_MIN_WORDS:
            return False
        if a._work_running() or a._offers or a._still_asked():
            return False
        if not a._for_the_brain(text) or a._only_name_tail(text):
            return False
        # With the local brain routing, only a turn that goes straight to Claude: one the local model hears first may
        # be answered by it, and the opener speaks for Claude's answer (as the early start, ``RoutedBrain.start_early``).
        route = getattr(a.brain, "route", None)
        return route is None or route(UserTurn(text=text, source="voice")) is None

    def _ask_opener(self, text: str, sound_frames: int | None) -> _Asked | None:
        """Ask for opening words for ``text`` (once per pause); the asking already under way when it's the same."""
        asked = self._opening
        if asked is not None and asked.text == text and asked.sound_frames == sound_frames:
            return asked
        a: Any = self
        if a._opener_refused == (text, sound_frames):
            return None  # this very pause was judged already
        self._drop_opening("a newer pause")
        if not self._opener_fits(text):
            a._opener_refused = (text, sound_frames)
            return None
        history = a._chat_history()
        task = asyncio.get_running_loop().create_task(self.opener.open(text, history), name="opener")  # type: ignore[union-attr]
        task.add_done_callback(_quiet_failure)
        self._opening = _Asked(text, sound_frames, task)
        return self._opening

    def _drop_opening(self, why: str) -> None:
        """Give up on the opening words being asked for or said (the user went on talking, or it isn't the brain's
        turn after all): the asking stops, and a phrase being said stops where it is."""
        asked, self._opening = self._opening, None
        if asked is None:
            return
        if not asked.task.done():
            asked.task.cancel()
        if asked.stream is not None:
            log.info("Opening words stopped (%s): %r", why, asked.words)
            asked.stream.cancel()
            a: Any = self
            if a._early_said is not None and a._early_said[0] == asked.words:
                a._early_said = None  # cut off as they went on: a later pause may open or acknowledge afresh

    def _take_opening(self, text: str) -> _Asked | None:
        """The opening words asked for this very turn (``text``), now the turn's; any other is dropped. Words already
        said stay the turn's even when its text came out differently (what was heard after the pause, a second
        transcription): they were said in that very pause, and cutting them off mid-air would be worse."""
        asked = self._opening
        if asked is None:
            return None
        if asked.text != text and asked.stream is None:
            self._drop_opening("the turn isn't the one it was asked for")
            return None
        self._opening = None
        return asked

    # -- the verdict ----------------------------------------------------------
    async def _opening_words(self, asked: _Asked, wait_s: float) -> Opening | None:
        """The opener's verdict, waited for at most ``wait_s``; None when it isn't in by then (or failed)."""
        try:
            return await asyncio.wait_for(asyncio.shield(asked.task), max(0.0, wait_s))
        except asyncio.CancelledError:
            if asked.task.cancelled():
                return None
            raise
        except Exception:
            return None

    def _log_opening(self, asked: _Asked, got: Opening, spoken: bool) -> None:
        if asked.logged:
            return
        asked.logged = True
        a: Any = self
        a.events.emit(
            OpenerChecked(
                text=asked.text,
                said=got.text,
                reason=got.reason,
                ready_s=round(got.elapsed_s, 3),
                spoken=spoken,
                first_word_s=got.first_word_s,
                model=got.model,
                wrote=got.wrote,
            )
        )

    def _log_when_done(self, asked: _Asked) -> None:
        """Log the verdict (said nothing) whenever it comes in: the shadow's record of the turn."""

        def done(task: asyncio.Task[Opening]) -> None:
            if task.cancelled() or task.exception() is not None:
                return
            self._log_opening(asked, task.result(), spoken=False)

        if asked.task.done():
            done(asked.task)
        else:
            asked.task.add_done_callback(done)

    def _say_opening(self, asked: _Asked, words: str) -> SpeechStream:
        """Begin her reply with ``words`` on a speech stream of its own, which her turn carries on."""
        a: Any = self
        stream = a.speech.begin()
        if a.vet_claims is not None or a.known_files is not None:
            stream.vet = a._vetted
        stream.say(words)
        asked.stream, asked.words = stream, words
        self.opener.said(words)  # type: ignore[union-attr]
        log.info("Opening words: %r", words)
        return stream

    # -- the two places it's said ---------------------------------------------
    def _open_early(self, text: str, choice: tuple[str, Any] | None, pause_ms: float) -> bool:
        """In the user's pause, where the early acknowledgment (``choice``) would play: with ``opener: on`` and
        opening words being asked for this very pause, wait for them instead (``_early_open``). False: not this time,
        the acknowledgment plays as before."""
        a: Any = self
        asked = self._opening
        if (
            self._opener_mode() != "on"
            or asked is None
            or asked.text != text
            or asked.sound_frames != a.endpointer.sound_frames
            or asked.stream is not None
        ):
            return False
        if choice is not None:
            a._start_early(text, choice[0])  # its head start isn't lost; started again if the words are said
        a._early_pending = asyncio.get_running_loop().create_task(
            self._early_open(asked, text, choice), name="early-opening"
        )
        return True

    async def _early_open(self, asked: _Asked, text: str, choice: tuple[str, Any] | None) -> None:
        a: Any = self
        got = await self._opening_words(asked, a.config.conversation.opener_wait_ms / 1000)
        if self._opening is not asked or a.endpointer.sound_frames != asked.sound_frames:
            return  # they went on talking: what was asked for is someone else's now
        if got is not None and got.text:
            self._say_opening(asked, got.text)
            self._log_opening(asked, got, spoken=True)
            a._early_said = (got.text, a.endpointer.pause_ms)
            a._start_early(text, got.text, opened=True)  # the brain starts again, told the exact words (D-004)
            return
        if got is not None:
            self._log_opening(asked, got, spoken=False)
        else:
            self._log_when_done(asked)
        if choice is not None and a.speech is not None:
            a.speech.play_phrase(*choice)
            a._early_said = (choice[0], a.endpointer.pause_ms)

    async def _settle_early_open(self) -> None:
        """The wait for opening words in the pause, if one's still going, ended (bounded) before the turn reads what
        was said early."""
        a: Any = self
        pending, a._early_pending = a._early_pending, None
        if pending is None or pending.done():
            return
        try:
            await pending  # bounded: it waits at most opener_wait_ms for the words, then says them or the "On it!"
        except Exception:
            log.debug("The wait for opening words failed", exc_info=True)

    async def _open_now(self, text: str) -> tuple[str | None, SpeechStream | None]:
        """Once the user has finished (no early acknowledgment was said): the opening words for ``text`` to begin her
        reply with, said now, and their stream; (None, None) when there are none (then she acknowledges as before).
        With ``opener: shadow`` it's only asked, and logged when it comes in."""
        mode = self._opener_mode()
        if mode == "off":
            return None, None
        asked = self._take_opening(text)
        if asked is None:
            asked = self._ask_opener(text, None)
            if asked is None:
                return None, None
            self._opening = None  # the turn's own
        if asked.stream is not None:
            return asked.words, asked.stream  # said already
        if mode != "on":
            self._log_when_done(asked)
            return None, None
        a: Any = self
        got = await self._opening_words(asked, a.config.conversation.opener_wait_ms / 1000)
        if got is None:
            self._log_when_done(asked)
            return None, None
        if not got.text:
            self._log_opening(asked, got, spoken=False)
            return None, None
        stream = self._say_opening(asked, got.text)
        self._log_opening(asked, got, spoken=True)
        return got.text, stream
