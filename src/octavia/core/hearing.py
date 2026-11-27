"""The assistant's ears (mixed into ``Assistant``): the audio loop routes each 32 ms microphone frame by the
listening mode (wake detection, utterance capture, or barge-in while she thinks or speaks), reads the voice tone in
the user's pauses, gives the wake word a second listen, hears a "stop" while she works, and turns captured speech
into a transcript."""

from __future__ import annotations

import asyncio
import logging
import math
import time

import numpy as np

from octavia.audio.types import FRAME_MS
from octavia.core.events import (
    AssistantState,
    Dismissed,
    ErrorOccurred,
    Interrupted,
    LoudNoise,
    SpeechEnded,
    SpeechStarted,
    ToneDetected,
    Transcript,
    WakeDetected,
    WakeListening,
)
from octavia.core.pauses import sounds_unfinished
from octavia.core.stopping import StopKind, addressed, holds, stop_kind
from octavia.core.text import normalize_text
from octavia.core.turn_types import ListenMode, _resolve
from octavia.core.work_intent import JUDGE_S, WorkHold, combined, judge_intent, speaks_of_pause
from octavia.emotion.tone import ToneJob, tone_note
from octavia.vad.endpointer import EndpointEvent, EndpointKind
from octavia.wake.base import WakeDetection
from octavia.wake.second_chance import SecondChanceWake

log = logging.getLogger("octavia.core.assistant")  # part of the assistant: its log lines read as before


_HALLUCINATIONS = {"thanks for watching", "thank you for watching", "subtitles by the amara.org community", "you"}

# On speakers, voice barge-in (automatic mode) waits until the echo canceller is measured removing at least this much
# of her own voice from the mic: a converged AEC3 manages 20-30 dB, a struggling one under 10, which the VAD then hears.
_MIN_ECHO_REDUCTION_DB = 15.0

# A pause this long after words that sound finished is noted (``SpeechEnded.sounded_finished``) when the early
# acknowledgment is off: how often such a pause is followed by more speech is what an early end would cost.
_FINISHED_PAUSE_MS = 600

# On a call (no wake word): this much speech in a row starts a request, which then takes in the last _CALL_SEED_S.
_CALL_SPEECH_MS = 64
_CALL_SEED_S = 0.5

# A listen ends only on frames (the endpointer counts its timeout in them), so with none coming in (the microphone
# vanished: Voicemod closed, a headset unplugged) it would wait forever, and with it everything waiting in line behind
# the conversation: a text from the phone, an announcement. When no frame has come for the listen's own timeout plus
# this much, it ends as "no speech" (``_await_capture``), checked every _CAPTURE_CHECK_S.
_DEAD_MIC_GRACE_S = 5.0
_CAPTURE_CHECK_S = 0.5


def strip_wake_phrase(text: str, wake_phrase: str) -> str:
    """Remove a leading wake phrase ("Hey computer, what's the time?")."""
    words = text.split()
    wake_words = normalize_text(wake_phrase).split()
    n = len(wake_words)
    if n and len(words) >= n and normalize_text(" ".join(words[:n])) == " ".join(wake_words):
        return " ".join(words[n:]).lstrip(" ,.!?")
    return text


class HearingMixin:
    """Part of ``Assistant`` (``octavia.core.assistant``), on its state."""

    async def _audio_loop(self) -> None:
        assert self.source is not None
        async for frame in self.source.frames():
            self._mic_zeros = self._mic_zeros + 1 if not frame.any() else 0  # see ``mic_silent_s``
            if self._ears_off and not self._talk_pressed:  # muted from the tray (``set_listening``)
                if self._mode is ListenMode.WAKE:
                    continue  # what's heard while she waits for her name is neither kept nor looked at
                frame = np.zeros_like(frame)  # a listen she opened herself (after an announcement) hears silence
            self._recent.append(frame)
            self._frames_heard += 1
            try:
                self._on_frame(frame)
            except Exception:
                self._frame_errors += 1
                if self._frame_errors <= 3 or self._frame_errors % 500 == 0:
                    log.exception("Audio frame processing failed (%d errors)", self._frame_errors)

    def _on_frame(self, frame: np.ndarray) -> None:
        mode = self._mode
        if mode is ListenMode.WAKE and self.on_call and not self.config.discord_bridge.call_wake_word:
            self._call_frame(frame)
        elif mode is ListenMode.WAKE:
            assert self.wake is not None
            if self._bangs is not None and self._bangs.process(frame):
                self.events.emit(LoudNoise())
            detection = self.wake.process(frame)
            if detection is not None and self._wake_needs_her_name():
                detection = None  # the second listen below transcribes the stretch: her name must start it
            if detection is not None and time.monotonic() >= self._wake_blocked_until:
                # Capture from this exact frame on, seeded with what was said right after
                # the phrase (the detector fires a moment late). _converse adopts it.
                self._start_capture(
                    self.config.vad.no_speech_timeout_s, pre_roll=False, seed=self._tail(detection.trailing_s)
                )
                self._name_tail_possible = True  # what it hears first may be only the end of her name
                _resolve(self._wake_waiter, detection)
            else:
                self._listen_for_stop(frame, self._work_running())
                self._second_chance_frame(frame, kws=detection is not None)
        elif mode is ListenMode.CAPTURE:
            self._capture_frame(frame)
        elif mode is ListenMode.BARGE:
            self._barge_frame(frame)

    def _wake_needs_her_name(self) -> bool:
        """Whether a wake detector hit should wait for speech recognition to hear her name: while another app plays
        sound. A video's speech reached the headset mic and fired the detector on a line with no name in it, and she
        answered the video. The second listen hears the name far better, at ~0.2 s more. Without one (turned off, or
        a screen task has the VAD), the detector wakes her as before."""
        if self._second_chance is None or self._stop_ear_on or not (apps := self._playing_elsewhere()):
            return False
        log.info("The wake detector fired while %s played sound: waiting to hear my name", " and ".join(apps))
        return True

    def mic_silent_s(self) -> float:
        """How long the microphone has sent nothing but exact zeros, by its frames: no room is that quiet, so it's
        muted (a headset's mute button), at zero gain, or the wrong device, and the user's answer can't be heard
        however loudly they give it. 0 when it sends sound."""
        return self._mic_zeros * FRAME_MS / 1000.0

    def _call_frame(self, frame: np.ndarray) -> None:
        """Waiting on a call: only the caller's voice reaches her, so speaking is what wakes her. A little speech
        in a row (not a click) starts a request, seeded with the moment before so its first word isn't clipped."""
        if self.vad is None or time.monotonic() < self._wake_blocked_until:
            return
        if self.vad.probability(frame) < self.config.vad.threshold:
            self._call_run_ms = 0.0
            return
        self._call_run_ms += FRAME_MS
        if self._call_run_ms < _CALL_SPEECH_MS:
            return
        self._call_run_ms = 0.0
        self._start_capture(self.config.vad.no_speech_timeout_s, pre_roll=False, seed=self._tail(_CALL_SEED_S))
        _resolve(self._wake_waiter, WakeDetection(phrase="(call)"))

    def _capture_frame(self, frame: np.ndarray) -> None:
        assert self.vad is not None
        prob = self.vad.probability(frame)
        if prob >= self.config.vad.threshold:
            if self._seeding:
                self._seed_speech_ms += FRAME_MS
            else:
                self._live_speech_ms += FRAME_MS
        event = self.endpointer.process(frame, prob)
        if event is None:
            if self._tone_wanted:
                if self.voice_tone is not None:
                    self._read_tone_early()
                self._listen_in_pause()
                if self.turn_shadow is not None:
                    self.turn_shadow.frame()
            return
        if event.kind is EndpointKind.SPEECH_STARTED:
            self.events.emit(SpeechStarted())
            return
        self._end_capture(event)

    def _end_capture(self, event: EndpointEvent) -> None:
        self._mode = ListenMode.OFF
        self._capture_ended_at = self._frames_heard
        if self._asking and event.kind is EndpointKind.UTTERANCE:
            self._cut_question()  # answered before she'd finished asking: no need to hear the rest
        _resolve(self._capture_waiter, event)

    def _listen_in_pause(self) -> None:
        """While the user pauses in one of their requests: transcribe what they've said so far (``stt/early.py``),
        acknowledge it early when it already sounds finished and is for the brain, and say a soft "mhm" when it sounds
        like they're thinking (``core/backchannel.py``). The utterance itself ends as before, after the full silence."""
        early = self._early
        if early is None or not early.enabled:
            return
        early.frame()
        ep = self.endpointer
        started = self._early_turn
        if started is not None and started.sound_frames != ep.sound_frames:
            self._drop_early("you went on talking")  # what the brain began on was only the start
        asked = self._opening
        if asked is not None and asked.sound_frames is not None and asked.sound_frames != ep.sound_frames:
            self._drop_opening("you went on talking")  # opening words for only the start of it
        pause = ep.pause_ms
        if pause <= 0:
            return
        if self._opener_mode() != "off" and (finished := early.finished_text()) is not None:
            self._ask_opener(finished, ep.sound_frames)  # as soon as it sounds finished: about half a second to answer
        after = self.config.conversation.acknowledge_early_ms or _FINISHED_PAUSE_MS
        if pause >= after and ep.sound_frames not in self._finished_pauses:
            text = early.finished_text()
            if text is not None:
                self._finished_pauses.add(ep.sound_frames)
                if self.config.conversation.acknowledge_early_ms:
                    if self._early_said is None:
                        self._acknowledge_early(text, pause)
                    else:
                        self._start_early_again(text)  # "On it!" was said in an earlier pause: only the brain
        self._backchannel(pause)

    def _backchannel(self, pause: float) -> None:
        """A soft "mhm" when the pause sounds like thinking. With headphones only: on speakers the mic would hear it
        as the user going on talking, and hold the utterance open."""
        bc = self.backchannel
        if bc is None or self._early is None or not self._headphones or not self.speech_enabled:
            return
        text = self._early.text()
        if text is None:
            return
        sound_frames = self.endpointer.sound_frames
        unfinished = bool(text) and sounds_unfinished(text)
        if not bc.due(pause, sound_frames, unfinished, end_ms=self.endpointer.needed_silence_ms):
            return
        choice = bc.choose(sound_frames)
        if choice is not None and self.speech is not None:
            log.info("Backchannel %r: %r sounds unfinished", choice[0], text)
            self.speech.play_phrase(*choice)

    def _ending(self, event: EndpointEvent) -> dict[str, object]:
        """How the capture that just ended ended, for ``SpeechEnded`` (and so the routing log)."""
        ep = self.endpointer
        said = self._early_said
        turn_end_p, turn_pauses = self.turn_shadow.verdicts() if self.turn_shadow is not None else (None, ())
        return {
            "turn_end_p": turn_end_p if not event.truncated else None,
            "turn_pauses": turn_pauses,
            "silence_s": round(ep.silence_ms / 1000, 3) if not event.truncated else None,
            "sounded_finished": ep.sound_frames in self._finished_pauses,
            "acknowledged_after_s": round(said[1] / 1000, 3) if said is not None else None,
            # A pause before the last one sounded finished, and the user went on talking.
            "continued": any(frames != ep.sound_frames for frames in self._finished_pauses),
        }

    def _read_tone_early(self) -> None:
        """Start reading the voice tone as soon as the user pauses, on what they've said so far.

        The endpointer only closes an utterance after ``vad.end_silence_ms`` of silence (0.7-0.9 s), and that wait is
        free time: a reading started ``_tone_pause_ms`` into a pause is done before the utterance even ends, so it
        never competes with the transcription for the CPU. When the user goes on talking, their next pause starts a
        new one (a reading still waiting for the worker is dropped); when they don't, ``_take_tone`` keeps this one.
        """
        ep = self.endpointer
        pause = ep.pause_ms
        if pause <= 0:
            self._tone_armed = True
            return
        if not self._tone_armed or pause < self._tone_pause_ms:
            return
        self._tone_armed = False
        if self._tone_job is not None and self._tone_job.sound_frames == ep.sound_frames:
            return  # nothing but silence since the last one began
        assert self.voice_tone is not None
        try:
            future = self.voice_tone.start(ep.speech_so_far())
        except Exception:
            log.warning("Couldn't start reading the voice tone", exc_info=True)
            return
        if future is not None:
            self._tone_job = ToneJob(future, sound_frames=ep.sound_frames)

    def _take_tone(self, event: EndpointEvent) -> ToneJob | None:
        """The voice tone's reading of the utterance that just ended: the one begun in its last pause when only
        silence came after it, otherwise one started now (it then runs beside the transcription)."""
        job, self._tone_job = self._tone_job, None
        if self.voice_tone is None or event.audio is None:
            return None
        if job is not None and job.sound_frames == self.endpointer.sound_frames:
            return job
        if job is not None:
            job.future.cancel()
        try:
            future = self.voice_tone.start(event.audio)
        except Exception:
            log.warning("Couldn't start reading the voice tone", exc_info=True)
            return None
        return ToneJob(future, sound_frames=self.endpointer.sound_frames) if future is not None else None

    def _learn_tone(self, job: ToneJob | None) -> None:
        """An utterance answered without the brain (a preset, a quick command, a dismissal) still teaches the voice
        tone how the user usually sounds, once its reading is done; nothing waits for it."""
        if job is not None and self.voice_tone is not None and job.concluded is None:
            self.voice_tone.learn_later(job.future, job.words)

    async def _heard_tone(self, job: ToneJob) -> str | None:
        """The note on how the user sounds, when the reading is ready in time and has something to say.

        It waits at most ``emotion.max_wait_ms`` after the transcript was ready (usually not at all: the reading
        began during the end-of-speech silence); a later one is skipped for this turn, and the baseline still learns
        from it. Every reading that's used is announced (``ToneDetected``), neutral too; only a confident, non-neutral
        one becomes a note.
        """
        assert self.voice_tone is not None
        if job.concluded is not None:
            return job.concluded[0]  # an early start in the pause already used it
        job.concluded = (None,)
        cfg = self.voice_tone.config  # the app gives it config.emotion
        future = job.future
        if not future.done():
            heard_at = job.heard_at if job.heard_at is not None else time.perf_counter()
            left = heard_at + cfg.max_wait_ms / 1000 - time.perf_counter()
            if left > 0:
                await asyncio.wait([asyncio.wrap_future(future)], timeout=left)
        if not future.done():
            log.debug(
                "Voice tone not ready %d ms after the transcript (begun %.0f ms ago): skipped for this turn",
                cfg.max_wait_ms,
                1000 * (time.perf_counter() - job.started),
            )
            self.voice_tone.learn_later(future, job.words)
            return None
        if future.cancelled():
            return None
        reading = future.result()
        tone = self.voice_tone.conclude(reading, job.words)
        log.info(
            "Voice tone: %s %.2f%s (read %.1f s of audio in %.0f ms)",
            tone.tone,
            tone.confidence,
            f" ({', '.join(tone.cues)})" if tone.cues else "",
            reading.audio_s,
            1000 * reading.took_s,
        )
        self.events.emit(ToneDetected(tone=tone.tone, confidence=tone.confidence, cues=tone.cues))
        note = tone_note(tone, cfg.min_confidence) if cfg.note else None
        job.concluded = (note,)
        return note

    def _barge_frame(self, frame: np.ndarray) -> None:
        cfg = self.config.barge_in
        if cfg.wake_phrase and self.wake is not None:
            detection = self.wake.process(frame)
            if detection is not None:
                self._trigger_barge_in("wake", trailing_s=detection.trailing_s)
                return
        if self.state is not AssistantState.SPEAKING:
            # Working on her steps, silent: a "stop" or "hold on" is heard without the wake phrase. (While she
            # speaks, talking over her or the wake phrase interrupts, and the stop is heard in what follows.)
            self._listen_for_stop(frame, self._turn_active and not self._asking)
            return
        if not self._voice_barge_in_ready() or self.vad is None:
            return
        headphones = self._headphones
        threshold = self.config.vad.threshold if headphones else cfg.vad_threshold
        needed_ms = min(cfg.min_speech_ms, 160) if headphones else cfg.min_speech_ms
        if self.vad.probability(frame) >= threshold:
            self._barge_run_ms += FRAME_MS
            if self._barge_run_ms >= needed_ms:
                if self._asking:
                    self._capture_early_answer()
                else:
                    self._trigger_barge_in("voice")
        else:
            self._barge_run_ms = 0.0

    def _voice_barge_in_ready(self) -> bool:
        """Voice barge-in only while audio plays and (on speakers) once echo cancellation has converged.

        On speakers in automatic mode (``barge_in.voice`` unset) it also waits until the echo canceller is measured
        removing her voice well, so the user can just talk over her without her own voice cutting her off.
        """
        cfg = self.config.barge_in
        headphones = self._headphones
        if cfg.voice is False or self.state is not AssistantState.SPEAKING:
            return False
        if not self._asking and self._other_call():  # on a call: talking is mostly to the people on it
            return False
        if headphones:
            return True
        if not self.echo_cancellation_active:
            return False  # speakers without AEC: the mic hears our own voice
        if self._playback_seconds() < cfg.aec_warmup_s:
            return False
        return cfg.voice is True or self._echo_cancelled_well()

    def _echo_cancelled_well(self) -> bool:
        """Whether the echo canceller removes enough of her voice for any speech left in the mic to be the user's.

        The measurement sinks as soon as the user talks over her (their voice is in the mic but not in her output, so
        it looks like echo left behind), which would block the very interruption it guards. It is therefore read only
        between bursts of speech and held while one is being timed.
        """
        if self._barge_run_ms == 0:
            reading = self.echo_reduction_db() if self.echo_reduction_db is not None else None
            trusted = reading is not None and reading >= _MIN_ECHO_REDUCTION_DB
            if trusted != self._echo_trusted:
                log.debug("Voice barge-in on speakers %s (echo reduced by %s dB)", "on" if trusted else "off", reading)
            self._echo_trusted = trusted
        return self._echo_trusted

    def _playback_seconds(self) -> float:
        """Seconds of our own audio the echo canceller has heard (silent tool waits don't count)."""
        played = getattr(self.speech.sink, "played_seconds", None) if self.speech is not None else None
        if played is not None:
            return float(played)
        total = self._played_s
        if self._speaking_since is not None:
            total += time.monotonic() - self._speaking_since
        return total

    def _capture_early_answer(self) -> None:
        """The user started answering while the confirmation question was still playing.

        People say "yes" as soon as they know what's asked. Treated as a barge-in, it cut the turn off and the
        tool call was declined; instead it is captured (with the frames that tripped it) as the answer, which
        ``confirm`` adopts once the question has been said.
        """
        seed = self._tail((self._barge_run_ms + self.config.vad.pre_roll_ms) / 1000)
        self._start_capture(self.config.conversation.confirm_listen_s, pre_roll=True, seed=seed)

    def _cut_question(self) -> None:
        """Stop saying the confirmation question: the user's answer is in. Only the question (and anything queued
        with it) stops; the turn's speech stream goes on for what she says next."""
        self._question_cut = True
        stream = self._active_stream
        if stream is not None and stream.usable:
            stream.cut_short()
        elif self.speech is not None:
            self.speech.stop()

    def _second_chance_frame(self, frame: np.ndarray, kws: bool) -> None:
        """Feed a wake-listening frame to the second listen, and run the transcription it asks for off the loop.

        While a screen task works the stop ear has the VAD (its state follows one stream of frames), so the second
        listen waits until the task is done."""
        if self._second_chance is None or self._stop_ear_on:
            return
        hearing = self._second_chance.process(frame, kws=kws)
        if hearing is not None:
            task = asyncio.get_running_loop().create_task(hearing, name="second-chance")
            self._hearing.add(task)
            task.add_done_callback(self._hearing.discard)

    def _second_chance_wake(self, heard: SecondChanceWake) -> bool:
        """Her name, heard by speech recognition where the wake detector missed it: wake up as if it had fired.

        Only while she's still just waiting for it (not speaking, in a turn or a question, or in the cooldown). With
        words after the name, they are the user's request, handed to the conversation as already captured and
        transcribed (no chime, nothing to wait for); for the name alone she starts listening, as after the wake word.
        Returns whether it was taken."""
        waiter = self._wake_waiter
        if (
            self._ears_off
            or self._mode is not ListenMode.WAKE
            or waiter is None
            or waiter.done()
            or self._turn_active
            or self._announcing
            or self._asking
            or self._speaking_since is not None
            or time.monotonic() < self._wake_blocked_until
        ):
            return False
        self._capture_ended_at = heard.ended_at  # what's been said since is the start of anything more
        if heard.rest:
            text = self._clean(heard.rest)
            if text:
                if self._tone_job is not None:  # a reading of an older capture: nobody takes it now
                    self._tone_job.future.cancel()
                    self._tone_job = None
                event = EndpointEvent(EndpointKind.UTTERANCE, audio=heard.audio)
                future: asyncio.Future[EndpointEvent] = asyncio.get_running_loop().create_future()
                future.set_result(event)
                _resolve(self._capture_waiter, EndpointEvent(EndpointKind.NO_SPEECH))
                self._capture_waiter = future  # ``_converse`` adopts it as its first capture
                self._heard_already = (event, text, heard.latency_s)
                self._mode = ListenMode.OFF
                _resolve(
                    waiter, WakeDetection(phrase=self.config.assistant.wake_phrase, second_chance=True, heard=text)
                )
                return True
        since = min(len(self._recent), self._frames_heard - heard.ended_at)
        seed = list(self._recent)[-since:] if since > 0 else []
        self._start_capture(self.config.vad.no_speech_timeout_s, pre_roll=False, seed=seed)
        _resolve(waiter, WakeDetection(phrase=self.config.assistant.wake_phrase, second_chance=True))
        return True

    def _work_running(self) -> bool:
        """A screen task is running or waiting beside the conversation (``work_running``)."""
        try:
            return self.work_running is not None and self.work_running() is True
        except Exception:
            log.warning("Asking whether a screen task runs failed", exc_info=True)
            return False

    def _paused_work(self) -> str | None:
        """The title of the screen task paused last (``work_paused``), or None when none is paused."""
        try:
            return self.work_paused() if self.work_paused is not None else None
        except Exception:
            log.warning("Asking whether a screen task is paused failed", exc_info=True)
            return None

    def _stop_running_work(self, everything: bool = False) -> str | None:
        """Stop the running screen task (``everything``: and the waiting ones): the line saying what stopped, or
        None when nothing did."""
        if self.stop_work is None:
            return None
        try:
            return self.stop_work(everything)
        except Exception:
            log.exception("Stopping the screen task failed")
            return None

    def _pause_running_work(self) -> str | None:
        """Pause the running screen task until the user asks to carry on (``pause_work``): the line saying so, or
        None when nothing was paused."""
        if self.pause_work is None:
            return None
        try:
            return self.pause_work()
        except Exception:
            log.exception("Pausing the screen task failed")
            return None

    def _hold_work(self, said: str, paused_too: bool = False) -> None:
        """The user said something to her while a screen task works (``said``, not a stop or a plain yes): it stops
        touching the screen while she answers (``hold_work``) and what they said is judged against it at once, by her
        quick voice (``work_intent``); once she's answered (``_settle_work_hold``) it carries on unless they asked to
        stop, pause or change it. ``paused_too``: with none running, a paused one is judged too (only "carry on with
        it" then counts), which the brain's turns ask for and quick replies don't (the quick voice is theirs then).
        Without ``hold_work`` (an older wiring), the task is paused as before, until they ask to carry on."""
        if self.hold_work is None:
            if not paused_too and self._work_running():
                self._pause_running_work()
            return
        if not self._work_running() and not (paused_too and self._paused_work() is not None):
            return
        try:
            hold = self.hold_work()
        except Exception:
            log.exception("Holding the screen task failed")
            return
        if hold is None:
            return
        record = self._work_hold
        if record is not None and record.hold.task is not hold.task:
            self._settle_work_hold("")  # another task's: it's settled on its own
            record = None
        if record is None:
            record = self._work_hold = WorkHold(hold)
            for earlier in self._settling:
                if earlier.hold.task is hold.task and not earlier.superseded:
                    # Said again while what came before is still being judged: it's all settled after this answer.
                    earlier.superseded = True
                    record.hold, record.said, record.judgments = earlier.hold, [*earlier.said], [*earlier.judgments]
        elif any(said == earlier for earlier in record.said):
            return  # judged already, in this same turn
        record.said.append(said)
        task = hold.task
        record.judgments.append(
            asyncio.get_running_loop().create_task(
                judge_intent(self.fast_chat, task.title, task.instruction, said, paused=hold.was_paused),
                name="screen-task-intent",
            )
        )

    def _settle_work_hold(self, reply: str) -> None:
        """She's answered what the user said while a screen task was held (``_hold_work``): once what they said is
        judged, it carries on or stays paused (``release_work``), in the background so nothing waits for it. ``reply``
        is what she said back: when the task stays paused and that didn't say so, she does."""
        record, self._work_hold = self._work_hold, None
        if record is None:
            return
        self._settling.append(record)
        releasing = asyncio.get_running_loop().create_task(
            self._release_work_hold(record, reply), name="screen-task-release"
        )
        self._releasing.add(releasing)
        releasing.add_done_callback(self._releasing.discard)

    async def _release_work_hold(self, record: WorkHold, reply: str) -> None:
        try:
            verdicts: list[str | None] = []
            for judgment in record.judgments:
                try:
                    verdicts.append(await asyncio.wait_for(asyncio.shield(judgment), JUDGE_S + 1.0))
                except (TimeoutError, Exception):
                    judgment.cancel()
                    verdicts.append(None)
        finally:
            self._settling.remove(record)
        if record.superseded:
            return  # they spoke again meanwhile: the hold for that answer settles it all
        verdict = combined(verdicts)
        try:
            outcome = self.release_work(record.hold, verdict) if self.release_work is not None else "moot"
        except Exception:
            log.exception("Letting the screen task carry on failed")
            outcome = "moot"
        log.info("Screen task #%s after her answer: %s (%s)", record.hold.task.id, outcome, verdict or "no verdict")
        if outcome == "kept" and not speaks_of_pause(reply):
            line = f"“{record.hold.task.title}” stays paused until you tell me to carry on."
            self.announce(line, listen=False, kind="reply")  # about what they just said: never held

    def _halt_work(self, kind: StopKind, core: str) -> str | None:
        """A spoken stop reaching the screen task: "hold on" or "wait" pauses it, to pick up again when asked; a stop
        drops it ("stop everything": and what waits). The line to say about it, or None when nothing halted."""
        if kind == "stop" and holds(core) and (line := self._pause_running_work()) is not None:
            return line
        return self._stop_running_work(kind == "all")

    def _listen_for_stop(self, frame: np.ndarray, active: bool) -> None:
        """While she works (a screen task beside the conversation, or her own silent steps in a turn), each short
        utterance is transcribed in case it's a stop: the wake phrase isn't needed to make her stop. Nothing else
        said is acted on or kept; only when the user was last heard talking in her turn is noted (``heard_talking``)."""
        if not active or self.vad is None or self.stt is None:
            self._stop_ear_on = False
            return
        if not self._stop_ear_on:
            self._stop_ear.reset(pre_roll=False)
            self._stop_ear_on = True
        event = self._stop_ear.process(frame, self.vad.probability(frame))
        if self._stop_ear.speaking and self._turn_text is not None:
            self._heard_talking = (self._turn_id, time.monotonic())
        if event is None or event.kind is EndpointKind.SPEECH_STARTED:
            return
        self._stop_ear.reset()
        if event.kind is EndpointKind.UTTERANCE and event.audio is not None and not event.truncated:
            hearing = asyncio.get_running_loop().create_task(self._hear_stop(event.audio), name="hear-stop")
            self._hearing.add(hearing)
            hearing.add_done_callback(self._hearing.discard)

    async def _hear_stop(self, audio: np.ndarray) -> None:
        assert self.stt is not None
        try:
            result = await self.stt.transcribe(audio)
        except Exception:
            log.debug("Transcribing a possible stop failed", exc_info=True)
            return
        text = self._clean(result.text.strip())
        core = self._addressed(text)
        kind = stop_kind(core)
        if kind is None:
            return
        log.info("Heard %r while working: stopping", text)
        self.events.emit(Transcript(text=text, latency_s=result.latency_s))
        self.events.emit(Dismissed(text=text))
        if self._turn_active or self._announcing:
            self._trigger_barge_in("stop", reason="you said stop")
        line = self._halt_work(kind, core)
        if line:
            self.announce(line, listen=False, kind="reply")  # once the turn it cut off has wound down

    def _addressed(self, text: str) -> str:
        assistant = self.config.assistant
        return addressed(text, assistant.name, assistant.wake_phrase)

    def _trigger_barge_in(self, kind: str, trailing_s: float = 0.0, reason: str | None = None) -> None:
        """Interrupt the current turn. kind: "wake" | "voice" | "hotkey" (then listen) or "stop"."""
        if self._interrupt is None:
            return
        if self._interrupt.is_set():
            if kind == "stop" and self._barge_kind != "stop":
                # Stop pressed while an earlier barge-in is still being torn down: drop
                # the capture it started and go back to waiting for the wake phrase.
                self._barge_kind = "stop"
                self._mode = ListenMode.OFF
                self._stop_requested = True
                _resolve(self._capture_waiter, EndpointEvent(EndpointKind.NO_SPEECH))
            return
        self._barge_kind = kind
        # Talking over her voice (not her name said while she works quietly: that's addressing her, and the exact
        # stops cover it): what it says may be a stop however it's heard (``core/cut_off.py``).
        if kind == "voice" and getattr(self, "_speaking_since", None) is not None:
            began = getattr(self, "_began_talking_at", None)
            into_s = time.monotonic() - began if began is not None else None
            self._cut_in = (time.monotonic(), into_s, bool(getattr(self, "_saying_unasked", False)))
        if kind != "stop":
            # Start capturing *now*, synchronously, so no words are lost while the
            # brain and speech are being torn down. For voice barge-in the frames that
            # tripped it (plus pre-roll) seed the capture; after a wake phrase, only
            # the audio said after it.
            if kind == "voice":
                seed = self._tail((self._barge_run_ms + self.config.vad.pre_roll_ms) / 1000)
            else:
                seed = self._tail(trailing_s)
            self._start_capture(self.config.vad.no_speech_timeout_s, pre_roll=kind == "voice", seed=seed)
            self._name_tail_possible = kind == "wake"
        else:
            self._mode = ListenMode.OFF
        self._silence()
        self._interrupt.set()
        if kind == "wake":
            self.events.emit(WakeDetected(phrase=self.config.assistant.wake_phrase, during_speech=True))
        reasons = {
            "wake": "you said the wake phrase",
            "voice": "you started talking",
            "hotkey": "talk hotkey",
            "stop": "stop hotkey",
        }
        # How long they'd been talking over her when her voice went quiet: the speech that tripped a voice barge-in,
        # and what the speaker's own buffer still played out after she was told to stop (cleared audio never plays,
        # but what the device already holds does). Until 2026-11-25 only the first part was logged, so every
        # interruption read 0.16 s, the threshold, whatever the speaker.
        after_s = round(self._barge_run_ms / 1000 + self._speaker_lag_s(), 3) if kind == "voice" else None
        self.events.emit(Interrupted(reason=reason or reasons.get(kind, kind), after_s=after_s))

    def _speaker_lag_s(self) -> float:
        """What her speaker still plays after its queue is cleared: the output stream's own latency (0 when it
        doesn't say)."""
        sink = getattr(self.speech, "sink", None) if self.speech is not None else None
        try:
            return max(0.0, float(getattr(sink, "latency", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _silence(self) -> None:
        """Stop her voice on the very frame the user cut in, not once the turn gets round to tearing itself down.

        Talking over someone who keeps going for another word or two feels like not being heard. What was said up to
        here is still what ``outcome()`` reports as heard, so the next turn knows where she was cut off.
        """
        if self._active_stream is not None:
            self._active_stream.cancel()
        if (self._turn_active or self._announcing) and self.speech is not None:
            self.speech.stop()  # an announcement, or a question asked outside the turn's stream

    @property
    def listening(self) -> bool:
        """Whether she's listening for her name (False: muted from the tray)."""
        return not self._ears_off

    def set_listening(self, on: bool) -> None:
        """The tray's Mute listening. While muted she hears nothing: as she waits for her name the mic's frames go
        nowhere (no wake phrase, no overheard speech, no stop word, no caller's voice on a Discord call), and a listen
        she opens herself (the follow-up after an announcement) hears silence. Only the talk hotkey gets through: the
        conversation it starts is heard as usual, until she's back to waiting for her name. Call on the event loop
        thread."""
        if on == (not self._ears_off):
            return
        self._ears_off = not on
        # Start afresh either way: nothing from before the mute is used as the start of a later request, and no
        # half-heard name (or second listen already transcribing) wakes her just after it.
        if self._mode is ListenMode.WAKE:
            if self.wake is not None:
                self.wake.reset()
            if self.vad is not None and not self._stop_ear_on:
                self.vad.reset()
            if self._second_chance is not None:
                self._second_chance.reset()
        if not on:
            self._recent.clear()

    def request_talk(self) -> None:
        """Talk hotkey: wake up now, or interrupt and listen. Call on the event loop thread."""
        self._talk_pressed = True  # heard even while muted, until she's back to waiting for her name
        if self._mode is ListenMode.WAKE and self._wake_waiter is not None:
            self._mode = ListenMode.OFF
            _resolve(self._wake_waiter, WakeDetection(phrase="(hotkey)"))
        elif self._turn_active or self._announcing:
            self._trigger_barge_in("hotkey")

    def request_stop(self) -> None:
        """Stop hotkey: stop talking and go back to waiting for the wake phrase. A screen task working beside the
        conversation stops too: the hotkey is the user's way to stop her hands at once."""
        if line := self._stop_running_work():
            self.announce(line, listen=False, kind="reply")
        if (self._turn_active or self._announcing) and self.state is not AssistantState.CONFIRMING:
            self._trigger_barge_in("stop")
        elif self._capture_waiter is not None:
            # Ends the capture as "no speech"; confirm() then declines and stops the turn.
            self._stop_requested = True
            _resolve(self._capture_waiter, EndpointEvent(EndpointKind.NO_SPEECH))
        elif self.speech is not None:
            self.speech.stop()

    def _end_quiet_follow_up(self) -> None:
        """Close the follow-up listen when the user answered at the PC instead (a choice clicked on her panel) and
        hasn't started talking: the typed turn waits for the conversation to end, so it went unanswered for the
        whole window. A listen they've started talking into is left to finish."""
        if not self._follow_up_open or self._mode is not ListenMode.CAPTURE:
            return
        if self.endpointer.speaking or self._live_speech_ms > 0:
            return
        log.info("Answered at the PC: closing the follow-up listen")
        _resolve(self._capture_waiter, EndpointEvent(EndpointKind.NO_SPEECH))

    def _tail(self, seconds: float) -> list[np.ndarray]:
        """The most recent ``seconds`` of microphone frames."""
        n = min(len(self._recent), math.ceil(max(0.0, seconds) * 1000 / FRAME_MS))
        return list(self._recent)[-n:] if n else []

    def _enter_barge_mode(self) -> None:
        # Voice barge-in seeds its capture from recent audio; it must not replay the
        # utterance (or confirmation answer) that was just captured and transcribed.
        self._recent.clear()
        if not (self.voice_enabled and self.config.barge_in.enabled):
            self._mode = ListenMode.OFF
            return
        if self.wake is not None:
            self.wake.reset()
        if self.vad is not None:
            self.vad.reset()
        self._barge_run_ms = 0.0
        self._stop_ear_on = False
        self._mode = ListenMode.BARGE

    async def _wait_for_wake(self) -> WakeDetection | None:
        """The wake phrase (or talk hotkey), or None when an announcement is waiting to be said."""
        assert self.wake is not None
        # A barge-in capture nobody adopted (the conversation ended on an error) is stale, and so is a
        # stop that ended the last conversation: it must not cut off a later, unanswered confirmation.
        _resolve(self._capture_waiter, EndpointEvent(EndpointKind.NO_SPEECH))
        self._capture_waiter = None
        self._stop_requested = False
        self._talk_pressed = False
        if not self._announcements.empty():
            return None
        self.wake.reset()
        if self._bangs is not None:
            self._bangs.reset()  # the room may have changed since it last listened
        if self.vad is not None:
            self.vad.reset()
        self._stop_ear_on = False
        self._wake_waiter = asyncio.get_running_loop().create_future()
        if self.state is not AssistantState.IDLE:
            self._set_state(AssistantState.IDLE)
            self.events.emit(WakeListening(phrase=self.config.assistant.wake_phrase, engine=self.wake.name))
        self._mode = ListenMode.WAKE
        try:
            return await self._wake_waiter
        finally:
            self._wake_waiter = None

    def _start_capture(
        self, timeout_s: float, pre_roll: bool = True, seed: list[np.ndarray] | None = None
    ) -> asyncio.Future[EndpointEvent]:
        """Begin capturing an utterance immediately (synchronous, frame-exact)."""
        assert self.vad is not None
        # Only one capture at a time: a superseded one resolves as "no speech".
        _resolve(self._capture_waiter, EndpointEvent(EndpointKind.NO_SPEECH))
        self.vad.reset()
        self._stop_ear_on = False
        self.endpointer.reset(no_speech_timeout_s=timeout_s, pre_roll=pre_roll)
        if self._early is not None:
            self._early.reset()
        if self.backchannel is not None:
            self.backchannel.reset()
        if self.turn_shadow is not None:
            self.turn_shadow.reset()
        self._finished_pauses = set()
        self._early_said = None
        if self._tone_job is not None:  # a reading of the capture before: nobody takes it now
            self._tone_job.future.cancel()
            self._tone_job = None
        self._tone_armed = True
        future: asyncio.Future[EndpointEvent] = asyncio.get_running_loop().create_future()
        self._capture_waiter = future
        self._mode = ListenMode.CAPTURE
        # Speech in the seed was said before the capture began; only what's heard from now on is "live".
        self._name_tail_possible, self._seed_speech_ms, self._live_speech_ms = False, 0.0, 0.0
        self._seeding = True
        try:
            for frame in seed or ():
                if self._mode is not ListenMode.CAPTURE:
                    break
                self._capture_frame(frame)
        finally:
            self._seeding = False
        return future

    async def _capture(
        self, timeout_s: float, pre_roll: bool = True, seed: list[np.ndarray] | None = None
    ) -> EndpointEvent:
        # Adopt a capture that barge-in already started, otherwise start one. It may
        # already have ended if the user finished talking while the turn was torn down.
        future = self._capture_waiter
        if future is None:
            future = self._start_capture(timeout_s, pre_roll=pre_roll, seed=seed)
        try:
            event = await self._await_capture(future, timeout_s)
            if event.kind is EndpointKind.UTTERANCE:
                self.events.emit(SpeechEnded(duration_s=event.duration_s, **self._ending(event)))  # type: ignore[arg-type]
            return event
        finally:
            if self._capture_waiter is future:
                if self._mode is ListenMode.CAPTURE:
                    self._mode = ListenMode.OFF
                self._capture_waiter = None

    async def _await_capture(self, future: asyncio.Future[EndpointEvent], timeout_s: float) -> EndpointEvent:
        """The capture's end, or "no speech" once no frame has come in for ``timeout_s`` plus ``_DEAD_MIC_GRACE_S``:
        the microphone is gone, and nothing would ever end the capture (see ``_DEAD_MIC_GRACE_S``)."""
        limit = max(timeout_s, 0.0) + _DEAD_MIC_GRACE_S
        heard, quiet_since = self._frames_heard, time.monotonic()
        try:
            while not future.done():
                await asyncio.wait({future}, timeout=_CAPTURE_CHECK_S)
                if future.done():
                    break
                if self._frames_heard != heard:
                    heard, quiet_since = self._frames_heard, time.monotonic()
                elif time.monotonic() - quiet_since >= limit:
                    log.warning("No sound from the microphone for %.0f s while listening: ending the listen", limit)
                    _resolve(future, EndpointEvent(EndpointKind.NO_SPEECH))
        except asyncio.CancelledError:
            future.cancel()  # as awaiting it would: the capture is given up with the turn
            raise
        return future.result()

    async def _transcribe(self, event: EndpointEvent) -> str:
        assert self.stt is not None and event.audio is not None
        self._set_state(AssistantState.TRANSCRIBING)
        heard, self._heard_already = self._heard_already, None
        if heard is not None and heard[0] is event:  # the second listen transcribed it already
            self.events.emit(Transcript(text=heard[1], latency_s=heard[2]))
            return heard[1]
        if (early := await self._early_transcript(event)) is not None:
            text, waited = early
            if text:
                self.events.emit(Transcript(text=text, latency_s=waited))
            return text
        try:
            result = await self.stt.transcribe(event.audio)
        except Exception as exc:
            log.exception("Transcription failed")
            self.events.emit(ErrorOccurred(source="STT", message=str(exc)))
            self._earcon("error")
            return ""
        text = self._clean(result.text.strip())
        if text:
            self.events.emit(Transcript(text=text, latency_s=result.latency_s))
        return text

    async def _early_transcript(self, event: EndpointEvent) -> tuple[str, float] | None:
        """The transcript taken in the pause that ended ``event`` (``stt/early.py``), cleaned, and how long it was
        waited for here; None when there isn't one for this exact audio (then it's transcribed now)."""
        assert event.audio is not None
        job = self._early.take(event.audio.size) if self._early is not None else None
        if job is None:
            return None
        started = time.perf_counter()
        try:
            # Shielded: cancelling this turn mustn't cancel the task, which would let go of the model's lock while
            # its worker thread still runs.
            result = await asyncio.shield(job.task)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None  # it failed: transcribed again
        if job.cleaned is None:
            job.cleaned = self._clean(result.text.strip())
        return job.cleaned, time.perf_counter() - started

    def _clean(self, text: str) -> str:
        """A transcript with the user's words fixed, the wake phrase taken off, and "" for the STT's hallucinations."""
        if self.vocabulary is not None:
            # Before the wake phrase is stripped, so a misheard assistant name in it is fixed first.
            corrected = self.vocabulary.correct(text)
            if corrected != text:
                log.info("Speech vocabulary: %r -> %r", text, corrected)
                text = corrected
        text = strip_wake_phrase(text, self.config.assistant.wake_phrase)
        if normalize_text(text) in _HALLUCINATIONS:
            # Said, so an empty answer to her question can be told apart from one heard as "you" (a clipped "yes"?)
            log.info("Heard %r and dropped it as a likely mishearing", text)
            return ""
        return text
