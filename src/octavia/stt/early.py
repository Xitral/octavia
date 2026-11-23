"""The user's words, transcribed while they pause, so the end of the wait isn't spent on it.

The endpointer closes an utterance only after ``vad.end_silence_ms`` of silence (1.2 s in a typical setup), and then
the transcription starts. Both waits can overlap: ``vad.early_transcript_ms`` into a pause, what was said so far
(``Endpointer.speech_so_far``) goes to the STT. When only silence follows, that transcript *is* the utterance's, and
the turn starts with it at once (``take``).

It also tells whether what's been said already sounds finished (``finished``): a request like "what time is it"
gets its acknowledgment early (``conversation.acknowledge_early_ms``), one that trails off ("set a timer for, um")
gets a soft "mhm" (``core/backchannel.py``). The utterance itself still ends only after the full silence: a pause
that sounded finished is sometimes followed by more ("...in Tokyo").

One transcription at a time (the model isn't thread-safe, and ``SpeechToText`` holds a lock): a job is never
cancelled once started, since cancelling the task would let go of the lock while the worker thread still runs.
A job whose audio is out of date (the user talked again) just finishes and is ignored, and the next pause starts
a new one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from octavia.core.pauses import only_fillers, sounds_unfinished
from octavia.stt.base import SpeechToText, TranscriptionResult
from octavia.vad.endpointer import Endpointer

log = logging.getLogger(__name__)


@dataclass
class EarlyJob:
    """One transcription of the utterance so far."""

    task: asyncio.Task[TranscriptionResult]
    sound_frames: int  # the endpointer's count when the audio was taken: unchanged = only silence came after
    pause_ms: float  # how far into the pause it started
    samples: int = 0  # the audio's length: the utterance's must match for its transcript to be this one
    started: float = field(default_factory=time.perf_counter)
    cleaned: str | None = None  # the transcript as the turn would get it, once worked out (``EarlyTranscriber.text``)

    def result(self) -> TranscriptionResult | None:
        """The transcript, once it's done and didn't fail."""
        if not self.task.done() or self.task.cancelled() or self.task.exception() is not None:
            return None
        return self.task.result()


def finished(text: str) -> bool:
    """Whether ``text`` (cleaned of the wake phrase) sounds like a whole request: some words, not just "um", and not
    trailing off ("and", "the", "uh", a comma)."""
    return bool(text.strip()) and not only_fillers(text) and not sounds_unfinished(text)


class EarlyTranscriber:
    """Starts and keeps the early transcripts of the capture running (one ``Assistant`` capture at a time).

    ``frame`` is called for each captured frame, after the endpointer has seen it. ``clean`` turns raw STT text into
    what the turn would get (the user's vocabulary fixed, the wake phrase taken off); it's applied when checking
    whether the sentence sounds finished.
    """

    def __init__(
        self,
        stt: SpeechToText,
        endpointer: Endpointer,
        start_after_ms: float,
        clean: Callable[[str], str] = str.strip,
    ) -> None:
        self.stt = stt
        self.endpointer = endpointer
        self.start_after_ms = start_after_ms
        self.clean = clean
        self._job: EarlyJob | None = None
        self.started = 0  # jobs started for the capture running (for the latency log)

    @property
    def enabled(self) -> bool:
        return self.start_after_ms > 0

    def reset(self) -> None:
        """A new capture: the last one's transcript is nobody's now (a job still running finishes on its own)."""
        self._job = None
        self.started = 0

    def frame(self) -> None:
        """Start a transcription when the user has paused long enough and said something new since the last one."""
        if not self.enabled:
            return
        ep = self.endpointer
        pause = ep.pause_ms
        # Not before the silence kept at the end of an utterance (``tail_ms``) is all in: until then the audio grows
        # with each frame, and wouldn't be the utterance's.
        if pause < max(self.start_after_ms, ep.s.tail_ms):
            return
        job = self._job
        if job is not None and job.sound_frames == ep.sound_frames:
            return  # this pause has one already
        if self.stt.busy or (job is not None and not job.task.done()):
            return  # one at a time; this frame's pause is tried again on the next frame
        audio = ep.speech_so_far()
        if audio.size == 0:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.stt.transcribe(audio))
        except RuntimeError:
            return  # no event loop (a test driving the endpointer by hand)
        task.add_done_callback(_quiet_failure)
        self._job = EarlyJob(task, sound_frames=ep.sound_frames, pause_ms=pause, samples=audio.size)
        self.started += 1

    def current(self) -> EarlyJob | None:
        """The job for the audio as it is now: started in this pause, with only silence since (done or not)."""
        job = self._job
        if job is None or job.sound_frames != self.endpointer.sound_frames:
            return None
        return job

    def text(self) -> str | None:
        """The cleaned transcript of the utterance so far, once it's ready and while it's up to date."""
        job = self.current()
        result = job.result() if job is not None else None
        if job is None or result is None:
            return None
        if job.cleaned is None:
            job.cleaned = self.clean(result.text.strip())  # once: cleaning logs the vocabulary's corrections
        return job.cleaned

    def finished_text(self) -> str | None:
        """The cleaned transcript of the utterance so far when it's ready, up to date and sounds finished."""
        text = self.text()
        return text if text is not None and finished(text) else None

    def take(self, samples: int) -> EarlyJob | None:
        """The job for the utterance that just ended (``samples`` long), if its audio is the whole of it (await
        ``job.task`` shielded when it isn't done yet: it's further along than a new one would be). Clears it."""
        job = self.current()
        self._job = None
        return job if job is not None and job.samples == samples else None


def _quiet_failure(task: asyncio.Task[TranscriptionResult]) -> None:
    """A failed early transcript is only a lost head start: the utterance is transcribed again when it ends."""
    if not task.cancelled() and task.exception() is not None:
        log.debug("Early transcript failed", exc_info=task.exception())
