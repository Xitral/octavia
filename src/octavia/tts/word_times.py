"""When each word of her sentence is said, from the sentence's own audio, for her subtitles (docs/subtitles.md).

Neither of her voices (Fish, Qwen) says when its words come, so the sentence's audio, once all of it is made
(``SpeechSegmentAudio``, usually before it plays), goes through her own speech recognizer (Parakeet, already loaded
for hearing you, on the CPU) with token times. Its words are matched to the words her voice was given: a laugh, a
breath or a filler her voice adds matches nothing and is skipped; a word it heard differently ("3x" for "three x")
takes a time between its matched neighbours. The audio is her voice before any effect (radio, robot), which the
recognizer would hear worse. It shares the recognizer's lock with hearing you, so the two never run at once.

Then, while the sentence plays, the speaker's own count of the audio it has played (``played_seconds``, which stands
still when the audio runs late) says which word she's on, and each is announced as she reaches it
(``SpeechWordStarted``). With no times yet (the first sentence of a reply often starts before its audio is all in),
the subtitles pace the words themselves until the first word comes.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import numpy as np

from octavia.core.events import (
    Event,
    EventBus,
    Interrupted,
    SpeakingFinished,
    SpeechSegmentAudio,
    SpeechSegmentMaking,
    SpeechSegmentStarted,
    SpeechWordStarted,
)

log = logging.getLogger(__name__)

RATE = 16000  # what the recognizer takes
MAX_AUDIO_S = 40.0  # a sentence longer than this isn't timed (paced instead): it'd hold the recognizer too long
LOOK_S = 0.04  # how often the playing position is read between words, at most
# Timing a sentence from its audio so far (the first of a reply plays long before all of it is in; measured
# 2026-11-22: 17 of 36 sentences waited a median 2.4 s, up to 6.6 s, for their whole audio): once this much is in,
# again each time this much more is, trusting only words starting this far before its end (the last may be cut).
EARLY_MIN_S = 0.8
EARLY_STEP_S = 0.6
EARLY_GUARD_S = 0.45
EARLY_LOOK_S = 0.1
EARLY_BACK_S = 0.5  # each pass starts this long before the last word timed
_CUE = re.compile(r"\[[a-z][a-z ]{0,30}\]")  # a voice cue ([laughs]): performed, never a word of the text
_CONTROL = re.compile(r"<\|[^|>]*\|>")
_NORM = re.compile(r"[^a-z0-9]+")


def _tail(parts: list[np.ndarray], skip: int) -> list[np.ndarray]:
    """The pieces of a sentence's audio from sample ``skip`` on."""
    out: list[np.ndarray] = []
    for part in parts:
        if skip >= part.size:
            skip -= part.size
            continue
        out.append(part[skip:] if skip else part)
        skip = 0
    return out or [np.zeros(0, np.float32)]


@dataclass(frozen=True)
class Timed:
    said: str  # the sentence, as her voice was given it
    times: list[float]  # its words' starts so far
    of: int  # how many words it has
    final: bool  # timed from all of its audio


# Times for a sentence's words: (word, start s) from the recognizer, in its order. Blocking.
Recognize = Callable[[np.ndarray], list[tuple[str, float]]]


def spoken_words(said: str) -> list[str]:
    """The words her voice was given, as the subtitles count them: no voice cues or control marks."""
    return _CONTROL.sub(" ", _CUE.sub(" ", str(said or ""))).split()


def words_from_tokens(tokens: list[str], starts: list[float]) -> list[tuple[str, float]]:
    """Recognizer tokens (a leading space starts a word) and their start times as words and theirs."""
    words: list[tuple[str, float]] = []
    for token, start in zip(tokens, starts, strict=False):
        if not words or token.startswith(" "):
            words.append((token.strip(), float(start)))
        else:
            words[-1] = (words[-1][0] + token, words[-1][1])
    return [(w, s) for w, s in words if w] if words else []


def _norm(word: str) -> str:
    return _NORM.sub("", word.lower())


def matched(spoken: list[str], heard: list[tuple[str, float]]) -> list[float | None]:
    """The time of each of ``spoken`` the recognizer heard as said; None for the rest."""
    times: list[float | None] = [None] * len(spoken)
    a = [_norm(w) for w in spoken]
    b = [_norm(w) for w, _ in heard]
    for block in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            if a[block.a + k]:
                times[block.a + k] = heard[block.b + k][1]
    return times


def timed_so_far(spoken: list[str], heard: list[tuple[str, float]], have_s: float, from_s: float = 0.0) -> list[float]:
    """Times for the first of ``spoken``, from what the recognizer heard of the sentence's first ``have_s`` seconds:
    up to the last word it heard as said that starts well inside them (the last one or two may be cut off
    mid-sound), with the words between heard ones spread between them. Never a guess past what was heard.
    ``from_s``: where ``heard`` starts in the sentence (a window of its audio), for words before the first heard."""
    hits = matched(spoken, heard)
    last = max((i for i, t in enumerate(hits) if t is not None and t < have_s - EARLY_GUARD_S), default=-1)
    if last < 0:
        return []
    return match(spoken, heard, have_s, from_s)[: last + 1]


def match(spoken: list[str], heard: list[tuple[str, float]], length_s: float, from_s: float = 0.0) -> list[float]:
    """When each of ``spoken`` starts, from what the recognizer ``heard``: matched words take their times; the rest
    are spread by length between the matched ones around them (from 0, up to the end of the audio)."""
    n = len(spoken)
    if not n:
        return []
    times = matched(spoken, heard)
    weights = [max(1, len(w)) for w in spoken]
    out: list[float] = []
    i = 0
    while i < n:
        if times[i] is not None:
            out.append(float(times[i]))  # type: ignore[arg-type]
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        lo = out[-1] if out else from_s
        lo_w = weights[i - 1] if out else 0  # the matched word before takes its own length first
        hi = float(times[j]) if j < n else max(lo, length_s * 0.97)  # type: ignore[arg-type]
        span = weights[i:j]
        total = lo_w + sum(span)
        acc = lo_w
        for w in span:
            out.append(lo + (hi - lo) * acc / total if total else lo)
            acc += w
        i = j
    return [round(t, 3) for t in out]


def resample(audio: np.ndarray, rate: int) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if rate == RATE or not audio.size:
        return audio
    n = int(audio.size * RATE / rate)
    return np.interp(np.arange(n) * (rate / RATE), np.arange(audio.size), audio).astype(np.float32)


def parakeet_recognizer(stt: Any) -> Recognize | None:
    """Times from her Parakeet (``ParakeetSTT``): its loaded sessions shared, nothing loaded twice. None if it isn't
    one. Until it has loaded, a sentence gets no times (and is paced)."""
    if not hasattr(stt, "_model"):
        return None
    cache: dict[str, Any] = {}

    def run(audio: np.ndarray) -> list[tuple[str, float]]:
        model = getattr(stt, "_model", None)
        if model is None:
            raise RuntimeError("her speech recognizer isn't loaded yet")
        if cache.get("model") is not model:
            cache["model"], cache["timed"] = model, model.with_timestamps()
        result = cache["timed"].recognize(audio, sample_rate=RATE)
        return words_from_tokens(list(result.tokens or []), list(result.timestamps or []))

    return run


class WordClock:
    """Times each sentence's words from its audio, and announces each word as the speaker reaches it."""

    def __init__(
        self,
        events: EventBus,
        sink: Any,
        recognize: Recognize,
        *,
        lock: asyncio.Lock | None = None,
        enabled: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        self.sink = sink
        self.recognize = recognize
        # Her recognizer's own lock: hearing you and timing her words never run at once (they share its threads), so
        # hearing you waits at most for one sentence's timing, and a sentence waits for you.
        self.lock = lock or asyncio.Lock()
        self.enabled = enabled  # her subtitles are on: nothing to time otherwise
        self.clock = clock
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="word-times")
        # Each sentence's word starts so far, by its place in the reply, with what was said (a stale entry, from a
        # reply that never got to play, never times another sentence at that place), how many words it has, and
        # whether they're all in (its whole audio timed) or only those of its audio so far.
        self._times: dict[int, Timed] = {}
        self._making: dict[int, tuple[str, list[np.ndarray], int]] = {}  # its audio as it comes in, until it's all in
        self._playing: tuple[int, str, float] | None = None  # the sentence playing, its words, where the audio stood
        self._task: asyncio.Task[None] | None = None
        self._early: asyncio.Task[None] | None = None
        self._jobs: set[asyncio.Task[None]] = set()
        self._reply = 0  # a new reply (or a cut) makes times still being worked out for the last one stale
        self.timed = 0  # sentences timed, for the log
        self.took_s = 0.0
        self._started_at = 0.0  # when the sentence playing started (clock), for how late its first word times came
        self._lateness: list[float] = []  # this reply's: how long each sentence played before its words had times
        self._unsubscribe = events.subscribe(
            self._on_event, SpeechSegmentMaking, SpeechSegmentAudio, SpeechSegmentStarted, SpeakingFinished, Interrupted
        )

    def close(self) -> None:
        self._unsubscribe()
        self._stop()
        for job in self._jobs:
            job.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ events

    def _on_event(self, event: Event) -> None:
        if isinstance(event, SpeechSegmentMaking):
            if event.said and event.sample_rate and self.enabled():
                if len(self._making) >= 16:
                    self._making.pop(next(iter(self._making)))
                self._making[event.index] = (str(event.said), event.dry, event.sample_rate)
                self._start_early()
        elif isinstance(event, SpeechSegmentAudio):
            self._making.pop(event.index, None)
            if event.audio is not None and event.said and self.enabled():
                job = asyncio.get_running_loop().create_task(self._time(event))
                self._jobs.add(job)
                job.add_done_callback(self._jobs.discard)
        elif isinstance(event, SpeechSegmentStarted):
            self._stop()
            # Where the audio stood as the sentence's first sample was reached (the speaker notes it at the mark: read
            # now, it'd be late by the rest of that block and by however late the event loop is).
            start = getattr(self.sink, "mark_played_seconds", None)
            self._playing = (event.index, str(event.text), float(start) if start is not None else self._played())
            self._started_at = self.clock()
            entry = self._times.get(event.index)
            if entry is not None and entry.said == str(event.text) and entry.times:
                self._lateness.append(0.0)
            self._run()
            self._start_early()
        else:  # she finished or was cut off: the reply's sentences are over
            self._stop()
            self._playing = None
            self._times.clear()
            self._making.clear()
            self._reply += 1
            for job in self._jobs:
                job.cancel()
            if self._lateness:
                late = sorted(x for x in self._lateness if x > 0)
                log.info(
                    "Word times this reply: %d sentence(s) timed as they started, %d later (median %.1f s in)",
                    len(self._lateness) - len(late),
                    len(late),
                    late[len(late) // 2] if late else 0.0,
                )
                self._lateness = []

    def _played(self) -> float:
        return float(getattr(self.sink, "played_seconds", 0.0) or 0.0)

    def _latency(self) -> float:
        try:
            return min(0.3, max(0.0, float(getattr(self.sink, "latency", 0.0) or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    def _queued(self) -> float:
        try:
            return float(self.sink.pending_seconds())
        except Exception:
            return 1.0

    # ------------------------------------------------------------------ timing a sentence

    async def _recognize(
        self, reply: int, audio: np.ndarray | list[np.ndarray], rate: int
    ) -> list[tuple[str, float]] | None:
        """What the recognizer heard of ``audio``, with times; None when her reply moved on or it failed."""
        loop = asyncio.get_running_loop()
        async with self.lock:
            if reply != self._reply:  # her reply ended (or was cut) while this waited
                return None
            started = self.clock()
            parts = audio if isinstance(audio, list) else [audio]  # joined off the event loop
            run = loop.run_in_executor(self._pool, lambda: self.recognize(resample(np.concatenate(parts), rate)))
            try:
                # Cancelled (she's cut off, the sentence moved on), the recognizer still runs to the end of this
                # pass: the lock is kept until it's done, so hearing you never runs beside it.
                heard = await asyncio.shield(run)
            except asyncio.CancelledError:
                await asyncio.wait([run])
                raise
            except Exception:
                log.debug("Couldn't time a sentence's words", exc_info=True)
                return None
            self.took_s += self.clock() - started
        return heard if reply == self._reply else None

    def _store(self, index: int, said: str, times: list[float], of: int, final: bool) -> None:
        entry = self._times.get(index)
        same = entry is not None and entry.said == said
        if same and entry.final and not final:  # type: ignore[union-attr]
            return  # all of it's in already
        if same and not final and len(entry.times) >= len(times):  # type: ignore[union-attr]
            return  # no further than before
        if len(self._times) >= 64:
            self._times.pop(next(iter(self._times)))
        self._times[index] = Timed(said, times, of, final)
        if self._playing is not None and self._playing[0] == index and self._playing[1] == said:
            if not (same and entry.times):  # type: ignore[union-attr]
                self._lateness.append(max(0.001, self.clock() - self._started_at))
            self._run()  # it's already playing: from where it is now

    async def _time(self, event: SpeechSegmentAudio) -> None:
        reply = self._reply
        spoken = spoken_words(event.said)
        audio = event.audio
        if not spoken or audio is None or not event.sample_rate or audio.size / event.sample_rate > MAX_AUDIO_S:
            return
        heard = await self._recognize(reply, audio, event.sample_rate)
        if heard is None:
            return
        self.timed += 1
        self._store(event.index, str(event.said), match(spoken, heard, event.seconds), len(spoken), True)

    def _start_early(self) -> None:
        """Times the playing sentence's words from its audio so far, while the rest still comes in."""
        if self._early is not None or self._playing is None:
            return
        index = self._playing[0]
        entry = self._times.get(index)
        if index not in self._making or (entry is not None and entry.final):
            return
        self._early = asyncio.get_running_loop().create_task(self._time_early(index, self._reply))

    async def _time_early(self, index: int, reply: int) -> None:
        done = 0.0  # seconds of its audio timed so far
        try:
            while reply == self._reply and index in self._making:
                said, dry, rate = self._making[index]
                if self._playing is None or self._playing[0] != index or self._playing[1] != said:
                    return
                have = sum(a.size for a in dry) / rate
                if have - done < EARLY_STEP_S or have < EARLY_MIN_S:
                    await asyncio.sleep(EARLY_LOOK_S)
                    continue
                # Only the audio from just before the last word already timed: each pass costs about the same, however
                # long the sentence (the whole of it each time was 8x the work for a 10 s one).
                spoken = spoken_words(said)
                entry = self._times.get(index)
                known = entry.times if entry is not None and entry.said == said and not entry.final else []
                anchor = max(0, len(known) - 1)  # the last word timed is heard again, to place the rest by
                from_s = max(0.0, known[anchor] - EARLY_BACK_S) if known else 0.0
                window = _tail(list(dry), int(from_s * rate))
                heard = await self._recognize(reply, window, rate)
                if heard is None or index not in self._making:
                    return
                done = have
                heard = [(w, t + from_s) for w, t in heard]
                times = timed_so_far(spoken[anchor:], heard, have, from_s)
                if times:
                    times = known[:anchor] + [max(t, known[anchor - 1] if anchor else 0.0) for t in times]
                    self._store(index, said, times, len(spoken), False)
        finally:
            if self._early is asyncio.current_task():
                self._early = None

    # ------------------------------------------------------------------ while it plays

    def _stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._early is not None:
            self._early.cancel()
            self._early = None

    def _run(self) -> None:
        if self._playing is None or self._task is not None:
            return
        index, said, start = self._playing
        entry = self._times.get(index)
        if entry is None or entry.said != said or not entry.times:
            return
        self._task = asyncio.get_running_loop().create_task(self._follow(index, said, start))

    async def _follow(self, index: int, text: str, start: float) -> None:
        said = -1
        last, still_since = -1.0, self.clock()
        try:
            while True:
                entry = self._times.get(index)
                if entry is None or entry.said != text or said >= entry.of - 1:
                    return
                times = entry.times
                played = self._played()
                now = self.clock()
                if played != last:
                    last, still_since = played, now
                # Stands still while the audio runs late; less what the device still holds, not yet heard. Once
                # nothing is left to hand it (a pause for a tool, the end), what it holds plays out all the same.
                behind = self._latency()
                if self._queued() <= 0:
                    behind = max(0.0, behind - (now - still_since))
                at = played - start - behind
                reached = said
                while reached + 1 < len(times) and times[reached + 1] <= at:
                    reached += 1
                if reached > said:
                    said = reached
                    self.events.emit(SpeechWordStarted(index=index, word=said, of=entry.of))
                # The next word's time, or, past the words timed so far, a look again soon for more.
                wait = times[said + 1] - at if said + 1 < len(times) else LOOK_S
                await asyncio.sleep(min(LOOK_S, max(0.005, wait)))
        except asyncio.CancelledError:
            pass
        finally:
            if self._task is asyncio.current_task():
                self._task = None
