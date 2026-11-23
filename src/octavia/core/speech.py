"""Streaming, interruptible speech output.

``SpeechOutput.begin()`` returns a ``SpeechStream``. Text deltas from the
brain are fed in, split into sentence segments by the chunker, synthesized
in order and played as audio arrives, so speech starts after the first
sentence rather than after the whole reply.

A streaming voice (Fish Audio) delivers a sentence at little more than the
speed it's spoken, so waiting for one sentence's last audio before asking for
the next leaves the next one's time-to-first-audio as a silence between them.
The next segment's synthesis therefore starts as soon as the current one is
producing audio, and its audio waits in memory until its turn. When nothing is
playing, a segment's first audio is also held until a small cushion has
arrived, so a chunk arriving a moment late doesn't cut into a word.

Playback markers after each segment record what the user has actually heard,
so on interruption we can tell the brain where it was cut off.

A voice effect (``audio/voice_effects.py``, ``SpeechOutput.effect``) is one chain for the whole stream, read as
it starts: her voice goes through it after it's made and before her volume, chunk by chunk with nothing held back,
and what still rings (a room's echo) runs on into her next sentence when that's already waiting, and is otherwise
played out, faded, right after the sentence (before a pause for a tool, a sound, a prepared phrase, or the end).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import numpy as np

from octavia.audio import voice_effects
from octavia.audio.sink import AudioSink
from octavia.audio.types import pcm16_to_float32
from octavia.audio.voice_volume import VoiceGain
from octavia.core import loop_watch
from octavia.core.claims import could_claim
from octavia.core.events import (
    ErrorOccurred,
    EventBus,
    PhrasePlaying,
    SpeakingFinished,
    SpeakingStarted,
    SpeechSegmentAudio,
    SpeechSegmentMaking,
    SpeechSegmentStarted,
)
from octavia.tts.base import TTSProvider
from octavia.tts.chunker import SentenceChunker
from octavia.tts.shown import Spoken, as_mark

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.!?…][\"')\]”’]*\s*$")

# Audio gathered before a segment starts from silence: enough to ride out a chunk arriving late, short enough
# not to be heard as a delay.
START_CUSHION_S = 0.15
# Pieces of text this long or longer run dry mid-sentence whenever Fish is slower than real time: its audio for a
# piece comes in growing chunks, the last ones seconds apart (measured 2026-10-25: 7 pauses at 220, 2 at 40).
LONG_PIECE_CHARS = 140


def piece_size_note(min_chunk_chars: int) -> str | None:
    """What to tell the user when ``tts.min_chunk_chars`` is long enough to make her pause mid-reply."""
    if min_chunk_chars < LONG_PIECE_CHARS:
        return None
    return (
        f"tts.min_chunk_chars is {min_chunk_chars}: pieces that long make her pause mid-reply when Fish is slow. "
        "Around 40 keeps her voice smooth."
    )


@dataclass
class SpeechOutcome:
    spoken: list[str] = field(default_factory=list)  # segments fully played
    partial: str | None = None  # segment that was playing when interrupted
    unspoken: list[str] = field(default_factory=list)
    interrupted: bool = False
    error: str | None = None

    @property
    def spoken_text(self) -> str:
        return " ".join(self.spoken)

    @property
    def replay_text(self) -> str:
        """What was heard, as text that says and shows it the same way again ("say that again"): a segment shown
        differently from how it was said goes back in as a [[shown|said]] mark."""
        return " ".join(as_mark(getattr(s, "shown", None) or "", str(s)) for s in self.spoken)


def _nothing() -> None:
    """A speaker mark that only marks a place (the end of a ring-out)."""


def resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cheap fallback resampler; providers should prefer emitting the sink rate."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


class _Synthesis:
    """One segment's audio, fetched in the background so it can start before the segment ahead of it has ended.

    A failure is kept and raised where the audio is read, so it surfaces in the segment's turn, as before.
    """

    def __init__(self, tts: TTSProvider, text: str) -> None:
        self.sample_rate = tts.format.sample_rate
        # How fast the audio came: seconds of it after the first piece, over the time from the first piece to the
        # last (the first piece arrives at the clock's start, so counting it would flatter a slow voice).
        self.audio_s = 0.0
        self.delivery_s = 0.0
        self._received = False
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._error: Exception | None = None
        self._task = asyncio.create_task(self._fetch(tts, text), name="speech-synthesis")

    async def _fetch(self, tts: TTSProvider, text: str) -> None:
        loop = asyncio.get_running_loop()
        first_at = 0.0
        try:
            # aclosing: a cancelled segment must release its TTS connection right away.
            async with contextlib.aclosing(tts.synthesize(text)) as stream:
                async for pcm in stream:
                    if not self._received:
                        self._received, first_at = True, loop.time()
                    else:
                        self.audio_s += len(pcm) / 2 / self.sample_rate
                        self.delivery_s = loop.time() - first_at
                    self._chunks.put_nowait(pcm)
        except Exception as exc:
            self._error = exc
        finally:
            self._chunks.put_nowait(None)

    async def chunks(self) -> AsyncIterator[bytes]:
        while (pcm := await self._chunks.get()) is not None:
            yield pcm
        if self._error is not None:
            raise self._error

    def cancel(self) -> None:
        self._task.cancel()


class SpeechStream:
    def __init__(self, output: SpeechOutput) -> None:
        self._out = output
        self._chunker = SentenceChunker(
            min_chars=output.min_chunk_chars,
            first_min_words=output.first_min_words,
            first_max_chars=output.first_max_chars,
            allowed_tags=getattr(output.tts, "allowed_tags", frozenset()),
            pause_tags=getattr(output.tts, "pause_tags", frozenset()),
            inline_tags=getattr(output.tts, "inline_tags", frozenset()),
            avoid_start=output.last_start,
        )
        # (text, audio already rendered at the sink's rate if any, whether it's her voice); None ends the stream
        self._queue: asyncio.Queue[tuple[str, np.ndarray | None, bool] | None] = asyncio.Queue()
        self._segments: list[str] = []
        self._played = 0  # number of segments fully heard
        self._submitted = 0  # segments whose audio is fully enqueued
        self._audible = 0  # segments with at least some audio enqueued
        self._closed = False
        self._cancelled = False
        self._error: str | None = None
        self._started = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._done = asyncio.Event()
        self._generation = 0  # which worker is current (``cut_short`` starts a new one)
        self._worker = asyncio.create_task(self._run(), name="speech-stream")
        # Each piece of her reply before it's spoken: what she says she did, held to what her tools did
        # (``ClaimWatch.vet``); it may say a sentence otherwise.
        self.vet: Callable[[str], str] | None = None
        self._held: str | None = None  # a piece that claims something, until its sentence is whole
        # Whole sentences that claim something, until she says more or the reply ends: a step she takes right after
        # one ("I've recorded it." before the call that records it) is judged with its result, not before it ran.
        self._waiting: list[str] = []
        # When each segment's first audio reached the speaker and when it had all been heard (``time.perf_counter``,
        # by index): the silence between two of them is a gap the user heard (``core/handoff.py``).
        self.started_at: dict[int, float] = {}
        self.played_at: dict[int, float] = {}
        self.underruns = 0  # times the speaker ran dry mid-sentence while this stream played
        self.underrun_s = 0.0  # ...and for how long, in all
        self.stalled_s = 0.0  # how long the event loop was stuck while it played (``core/loop_watch.py``)

    # -- feeding -------------------------------------------------------------
    def feed(self, delta: str) -> None:
        if self._closed or self._cancelled:
            return
        self._emit(self._chunker.feed(delta))

    def _emit(self, segments: list[str]) -> None:
        """Pushes ``segments``, each vetted. A piece cut at a clause that says she did something waits for the rest
        of its sentence, so it's judged whole ("...whether I sent it, or should I leave it?" is a question); a whole
        sentence that does waits for what comes after it (``_waiting``)."""
        for seg in segments:
            if self._held is not None:
                held, self._held = self._held, None
                seg = Spoken(
                    f"{held} {seg}",
                    f"{getattr(held, 'shown', held)} {getattr(seg, 'shown', seg)}",
                    (*getattr(held, "beats", ()), *getattr(seg, "beats", ())),
                )
            if self.vet is not None and could_claim(str(seg)):
                if _SENTENCE_END.search(seg):
                    self._waiting.append(seg)
                else:
                    self._held = seg
                continue
            self._release_waiting()
            self._push(self._vetted(seg))

    def _release(self) -> None:
        if self._held is not None:
            held, self._held = self._held, None
            self._waiting.append(held)

    def _release_waiting(self) -> None:
        """The claims waiting for what she said next, vetted now: with any step she took since in their judging."""
        waiting, self._waiting = self._waiting, []
        for seg in waiting:
            self._push(self._vetted(seg))

    def _vetted(self, segment: str) -> str:
        if self.vet is None:
            return segment
        try:
            said = self.vet(str(segment))
            if said == str(segment):
                return segment
            shown = getattr(segment, "shown", None)
            beats = getattr(segment, "beats", ())
            return Spoken(said, self.vet(shown) if shown and shown != str(segment) else said, beats)
        except Exception:
            log.warning("Vetting what she's about to say failed", exc_info=True)
            return segment

    def say(self, text: str) -> None:
        """Speak a complete piece of text right after what's already queued."""
        self.flush()  # ends a list of sources the reply was on, so this is heard
        self.feed(text)
        self.flush()

    def say_prepared(self, text: str, audio: np.ndarray) -> None:
        """Speak ``text`` from audio that is already rendered at the sink's rate (a cached phrase)."""
        if self._closed or self._cancelled:
            return
        self.flush()
        self._release_waiting()
        self._push(text, audio)

    def play_clip(self, label: str, audio: np.ndarray) -> None:
        """Play a sound (at the sink's rate) right after what's already queued and before what's fed next: between
        two sentences, not over them. It counts as a segment (``label`` shows in the outcome, so an interruption
        note says it was heard) but isn't announced with ``SpeechSegmentStarted``, as it isn't her voice."""
        if self._closed or self._cancelled:
            return
        self.flush()
        self._release_waiting()
        self._push(label, audio, voiced=False)

    def flush(self) -> None:
        """Everything fed so far goes out, except a claim waiting for what comes next: a flush before her step (a
        tool call) mustn't let "I've saved it" be said before the step that saves it has run."""
        if self._closed or self._cancelled:
            return
        self._emit(self._chunker.flush())
        self._release()

    def finish(self) -> None:
        if self._closed:
            return
        self.flush()
        if not self._cancelled:
            self._release_waiting()
        if self._chunker.start_word is not None:  # the next reply doesn't open with the same "Oh,"
            self._out.last_start = self._chunker.start_word
        self._closed = True
        self._queue.put_nowait(None)

    def _push(self, segment: str, audio: np.ndarray | None = None, voiced: bool = True) -> None:
        self._segments.append(segment)
        self._idle.clear()
        self._queue.put_nowait((segment, audio, voiced))

    # -- control -------------------------------------------------------------
    def cancel(self) -> None:
        if self._cancelled or self._done.is_set():
            return
        self._cancelled = True
        self._out.sink.clear()
        self._worker.cancel()

    def cut_short(self) -> None:
        """Stop what's playing and drop what's queued, but keep the stream for what's fed next: a question the user
        answered before she'd finished asking it. What was cut counts as heard (the user answered it)."""
        if not self.usable:
            return
        self._generation += 1
        self._worker.cancel()
        self._out.sink.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._closed:
            self._queue.put_nowait(None)
        self._played = self._submitted = self._audible = len(self._segments)
        self._idle.set()
        self._worker = asyncio.create_task(self._run(start=len(self._segments)), name="speech-stream")

    @property
    def started(self) -> bool:
        return self._started.is_set()

    @property
    def pushed(self) -> int:
        """How many segments have gone out to be spoken so far (what's still in the chunker isn't one yet)."""
        return len(self._segments)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def usable(self) -> bool:
        """False once the stream was cancelled or its worker ended (e.g. after a TTS error)."""
        return not self._cancelled and not self._done.is_set()

    async def wait_started(self) -> None:
        await self._started.wait()

    async def wait_idle(self) -> None:
        """Wait until everything fed so far (after a flush) has been heard."""
        waiter = asyncio.create_task(self._idle.wait())
        done_waiter = asyncio.create_task(self._done.wait())
        try:
            await asyncio.wait({waiter, done_waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            done_waiter.cancel()

    async def wait(self) -> SpeechOutcome:
        await self._done.wait()
        return self.outcome()

    def outcome(self) -> SpeechOutcome:
        spoken = self._segments[: self._played]
        partial = None
        rest = self._segments[self._played :]
        if self._cancelled and self._started.is_set() and rest and self._audible > self._played:
            partial, rest = rest[0], rest[1:]
        return SpeechOutcome(
            spoken=spoken, partial=partial, unspoken=rest, interrupted=self._cancelled, error=self._error
        )

    # -- worker --------------------------------------------------------------
    async def _run(self, start: int = 0) -> None:
        out = self._out
        index = start
        generation = self._generation
        underruns = getattr(out.sink, "underruns", None)
        dry_s = getattr(out.sink, "underrun_s", None)
        device_underruns = getattr(out.sink, "device_underruns", None)
        watch = loop_watch.current
        stalls = watch.snapshot() if watch is not None else None
        fetched: list[_Synthesis] = []  # this stream's syntheses, for how fast the voice delivered
        effect_name = out.effect  # her voice effect, one chain for the whole stream
        effect = voice_effects.make(effect_name, out.sink.sample_rate)
        ringing = False  # her voice went through ``effect`` since its tail was last played out
        tail_queued = False

        def ring_out() -> None:
            """What ``effect`` still holds (a room's echo, faded), played now: her last words ring out."""
            nonlocal ringing, tail_queued
            if effect is None or not ringing:
                return
            ringing = False
            limiter = VoiceGain(out.sink.sample_rate)
            tail = effect.flush()
            tail = np.concatenate([limiter.process(tail, out.volume, True), limiter.flush()])
            if tail.size:
                out.sink.enqueue(tail)
                # The silence after the ring-out is a pause between sentences: a mark where it ends keeps the speaker
                # from counting it as running dry mid-sentence.
                out.sink.mark(_nothing)
                tail_queued = True

        held: tuple[str, np.ndarray | None, bool] | None = None  # the next item, taken off the queue early...
        # ...and its synthesis, started once the current segment's audio is all in (not while it still streams: two
        # at once share Fish's speed, and the current one's big later piece, about 3 s into it, then came late, a
        # gap mid-sentence in about 1 reply in 3; measured 2026-11-22 on her real replies)
        ahead: _Synthesis | None = None
        ended = False  # the end of the stream was taken off the queue early
        synthesis: _Synthesis | None = None

        def look_ahead() -> None:
            nonlocal held, ahead, ended
            if held is not None or ended or self._queue.empty():
                return
            item = self._queue.get_nowait()
            if item is None:
                ended = True
                return
            held = item
            if item[1] is None and out.tts is not None:
                ahead = _Synthesis(out.tts, item[0])
                fetched.append(ahead)

        try:
            while True:
                if held is not None:
                    item, held = held, None
                elif ended:
                    item = None
                else:
                    item = await self._queue.get()
                if item is None:
                    break
                segment, prepared, voiced = item
                if prepared is not None:
                    ring_out()
                    if not self._started.is_set():
                        out.sink.mark(self._on_first_audio)
                    if voiced:
                        out.sink.mark(lambda i=index, s=segment: self._on_segment_started(i, s))
                    out.sink.enqueue(prepared)
                    self._audible = index + 1
                    if voiced:
                        self._audio_made(index, segment, [prepared])
                else:
                    if out.tts is None:
                        raise RuntimeError("no TTS provider configured")
                    if ahead is None:
                        fetched.append(ahead := _Synthesis(out.tts, segment))
                    synthesis, ahead = ahead, None
                    if out.effect != effect_name:  # switched mid-reply ("use your radio voice" as a tool): from here
                        ring_out()
                        effect_name, effect = out.effect, voice_effects.make(out.effect, out.sink.sample_rate)
                    ringing = effect is not None
                    await self._synthesize(segment, index, synthesis, effect)
                    look_ahead()
                self._submitted = index + 1
                out.sink.mark(lambda i=index: self._on_segment_played(i))
                index += 1
                if held is None:  # nothing to say next yet (a tool runs, or the end): her words ring out now, after
                    ring_out()  # the sentence counts as heard, and not over the start of one said after the pause
            ring_out()
            # Wait for the tail of the audio to be heard. Bounded, as a backstop: if the
            # output device dies, the marks never fire and the turn must not hang.
            if self._submitted > self._played or tail_queued:
                drained = asyncio.Event()
                out.sink.mark(drained.set)
                try:
                    await asyncio.wait_for(drained.wait(), out.sink.pending_seconds() + 5.0)
                except TimeoutError:
                    raise RuntimeError("the speaker stopped playing") from None
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            log.exception("Speech synthesis failed")
            out.events.emit(ErrorOccurred(source="TTS", message=self._error))
            out.sink.clear()
        finally:
            for unfinished in (synthesis, ahead):
                if unfinished is not None:
                    unfinished.cancel()  # releases the TTS connection of a segment that was cut off
            try:
                self._log_gaps(underruns, device_underruns, fetched, watch, stalls, dry_s)
            except Exception:  # a log line must never keep the turn from ending
                log.debug("Couldn't log speech gaps", exc_info=True)
            # A worker that was cut short leaves the stream to the one that replaced it.
            if generation == self._generation:
                self._idle.set()
                self._done.set()
                if self._started.is_set():
                    out.events.emit(
                        SpeakingFinished(
                            interrupted=self._cancelled,
                            underruns=self.underruns,
                            underrun_s=round(self.underrun_s, 3),
                            stalled_s=round(self.stalled_s, 3),
                        )
                    )

    async def _synthesize(
        self,
        segment: str,
        index: int,
        synthesis: _Synthesis,
        effect: voice_effects.Chain | None = None,
    ) -> None:
        out = self._out
        first_chunk = True
        cushion = int(START_CUSHION_S * out.sink.sample_rate) if out.sink.pending_seconds() < START_CUSHION_S else 0
        held: list[np.ndarray] = []
        limiter = VoiceGain(out.sink.sample_rate)  # one per sentence: it looks ahead across the joins between chunks
        samples = 0
        dry: list[np.ndarray] = []  # her voice before any effect, to time its words (tts/word_times.py)
        # ...as it comes in: the first sentence of a reply starts playing long before all of it is in
        out.events.emit(SpeechSegmentMaking(index=index, said=str(segment), dry=dry, sample_rate=out.sink.sample_rate))

        def play(audio: np.ndarray) -> None:
            nonlocal first_chunk, samples
            samples += audio.size
            if first_chunk:
                if not self._started.is_set():
                    out.sink.mark(self._on_first_audio)
                out.sink.mark(lambda i=index, s=segment: self._on_segment_started(i, s))
            first_chunk = False
            out.sink.enqueue(audio)
            self._audible = index + 1

        async with contextlib.aclosing(synthesis.chunks()) as audio_chunks:
            async for pcm in audio_chunks:
                audio = pcm16_to_float32(pcm)
                if synthesis.sample_rate != out.sink.sample_rate:
                    audio = resample_linear(audio, synthesis.sample_rate, out.sink.sample_rate)
                dry.append(audio)
                if effect is not None:
                    audio = effect.process(audio)  # before her volume: its limiter catches a peak the effect made
                audio = limiter.process(audio, out.volume, effect is not None)
                if not audio.size:  # all held back by the limiter until the next chunk
                    continue
                if cushion:
                    held.append(audio)
                    if sum(a.size for a in held) < cushion:
                        continue
                    audio, held, cushion = np.concatenate(held), [], 0
                play(audio)
        if (tail := limiter.flush()).size:
            held.append(tail)
        if held:
            play(np.concatenate(held))
        self._audio_made(index, segment, dry, samples)

    def _audio_made(self, index: int, segment: str, dry: list[np.ndarray], samples: int | None = None) -> None:
        """All of a sentence's audio is queued: how long it lasts and her voice for it before any effect, for her
        subtitles' words (``SpeechSegmentAudio``)."""
        rate = self._out.sink.sample_rate
        voice = np.concatenate(dry) if dry else None
        samples = samples if samples is not None else (voice.size if voice is not None else 0)
        if samples > 0 and not self._cancelled:
            said = str(segment)  # what her voice was given (a Spoken segment is its said form)
            self._out.events.emit(
                SpeechSegmentAudio(index=index, seconds=samples / rate, said=said, audio=voice, sample_rate=rate)
            )

    def _log_gaps(
        self,
        underruns: int | None,
        device_underruns: int | None,
        fetched: list[_Synthesis],
        watch: loop_watch.LoopWatch | None = None,
        stalls: tuple[int, float] | None = None,
        dry_s: float | None = None,
    ) -> None:
        """Say how often the speaker ran out while this stream played, with what points at the cause.

        ``underruns``: the queue was empty mid-sentence. Usually the voice's audio hadn't arrived (the delivery
        rate shows whether it was slow), though a busy event loop not passing on audio that had arrived looks the
        same. ``device_underruns``: the speaker asked and this process answered too late; counted over the whole
        stream, silences included, so not every one was heard. The event loop stuck (``core/loop_watch.py``): Fish's
        audio waited unread meanwhile.
        """
        sink = self._out.sink
        late = getattr(sink, "underruns", 0) - underruns if underruns is not None else 0
        self.underruns += max(0, late)  # each worker counts from its own start (``cut_short`` starts another)
        # A gap still open when the stream ends is counted once the audio comes back: the next stream's, a little late.
        silent = max(0.0, getattr(sink, "underrun_s", 0.0) - dry_s) if isinstance(dry_s, float) else 0.0
        self.underrun_s += silent
        missed = getattr(sink, "device_underruns", 0) - device_underruns if device_underruns is not None else 0
        parts = []
        if late > 0:
            # Long enough pieces to time: a word or two comes in one go.
            timed = [s for s in fetched if s.delivery_s > 0.2]
            rate = ""
            if timed:  # none timed: the line still goes out (it once divided by zero and logged nothing)
                speed = sum(s.audio_s for s in timed) / sum(s.delivery_s for s in timed)
                rate = f" (the voice delivered at {speed:.1f}x real time)"
            heard = f", {silent:.2f} s of silence in all" if silent else ""
            parts.append(f"the queue ran dry {late} time(s) mid-sentence{heard}{rate}")
        if missed > 0:
            parts.append(f"the speaker got {missed} block(s) late from this process")
        if watch is not None and stalls is not None:
            count, stuck = watch.snapshot()
            count, stuck = count - stalls[0], stuck - stalls[1]
            self.stalled_s += stuck
            if count > 0 and parts:
                parts.append(f"the event loop was stuck {count} time(s), {stuck * 1000:.0f} ms in all")
        if parts:
            log.info("While this stream played, %s", " and ".join(parts))

    def _on_first_audio(self) -> None:
        if not self._started.is_set() and not self._cancelled:
            self._started.set()
            self._out.events.emit(SpeakingStarted())

    def _on_segment_started(self, index: int, segment: str) -> None:
        if not self._cancelled:
            self.started_at.setdefault(index, time.perf_counter())
            shown = getattr(segment, "shown", None)  # a Spoken segment from the chunker knows how it's shown
            beats = tuple(getattr(segment, "beats", ()))  # explainer steps it plays as it starts
            self._out.events.emit(SpeechSegmentStarted(text=segment, index=index, shown=shown, beats=beats))

    def _on_segment_played(self, index: int) -> None:
        if self._cancelled:
            return
        self.played_at.setdefault(index, time.perf_counter())
        self._played = max(self._played, index + 1)
        if self._played >= len(self._segments) and self._queue.empty():
            self._idle.set()


class SpeechOutput:
    def __init__(
        self,
        tts: TTSProvider | None,
        sink: AudioSink,
        events: EventBus,
        min_chunk_chars: int = 40,
        volume: float = 1.0,
        first_min_words: int = 2,
        first_max_chars: int | None = 100,
    ) -> None:
        self.tts = tts
        self.sink = sink
        self.events = events
        self.min_chunk_chars = min_chunk_chars
        # A one-word opener is spoken with the next sentence so there's no gap after it.
        self.first_min_words = first_min_words
        # A longer opener is cut at a comma once it gets this long, so she starts talking sooner.
        self.first_max_chars = first_max_chars
        self.volume = volume  # read for every chunk, so a change (the voice_volume tool) is heard right away
        self.effect = voice_effects.OFF  # her voice effect (``tts.voice_effect``): read as each reply starts
        self.current: SpeechStream | None = None
        self.last_start: str | None = None  # the interjection her last reply opened with ("oh"; "" for none)

    def begin(self) -> SpeechStream:
        if self.current is not None and not self.current._done.is_set():
            self.current.cancel()
        self.current = SpeechStream(self)
        return self.current

    async def say(self, text: str) -> SpeechOutcome:
        stream = self.begin()
        stream.say(text)
        stream.finish()
        return await stream.wait()

    async def say_prepared(self, text: str, audio: np.ndarray) -> SpeechOutcome:
        """Like ``say``, with audio that's already rendered (no TTS wait); stoppable the same way."""
        stream = self.begin()
        stream.say_prepared(text, audio)
        stream.finish()
        return await stream.wait()

    def stop(self) -> None:
        if self.current is not None:
            self.current.cancel()

    def play(self, audio: np.ndarray) -> None:
        """Play a short sound (earcon) at the sink's sample rate, right away: over speech, not after it."""
        self.sink.overlay(audio)

    def loop(self, audio: np.ndarray | None) -> None:
        """Keep a sound looping under everything (fading in), or fade it out with None."""
        self.sink.set_loop(audio)

    def play_phrase(self, text: str, audio: np.ndarray) -> None:
        """Play a short spoken phrase (an acknowledgment) right away, like ``play``, and say when it starts
        (``PhrasePlaying``: the avatar moves its lips to it)."""
        seconds = audio.size / self.sink.sample_rate
        self.sink.mark(lambda: self.events.emit(PhrasePlaying(text=text, seconds=seconds)))
        self.sink.enqueue(audio)
