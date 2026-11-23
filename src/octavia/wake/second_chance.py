"""A second listen for the wake word: what the wake detector missed, speech recognition catches.

The keyword spotter (sherpa-onnx KWS) is tuned to stay quiet on ordinary talk, so it also lets a real "Octavia" slip
by now and then, and the user is left waiting for a chirp that never comes. Speech recognition (Parakeet) is far
better at telling the name apart, and only takes ~0.1-0.2 s per utterance, so while she waits for the wake word each
stretch of speech the voice activity detector hears is collected (with pre-roll, ended by ``_END_SILENCE_MS`` of
quiet) and, when the detector didn't fire during it, transcribed in a worker thread. A transcript that *starts* with
her name (or one of its spellings, after a "hey" or "okay") wakes her, and what follows the name is the user's
request: "Octavia, what's the weather" is answered in one breath, with no chirp to wait for. A stretch may also start
with something said to someone else, when the user turned to her without a pause long enough to end it: her name
right after a sentence ("Good game, guys. Octavia, ...") wakes her the same way, and so does her name set off at the
very end ("Hold on, Octavia"), with nothing after it to answer yet. The name said somewhere else ("I told Octavia
yesterday...") is ordinary talk and is left alone.

Guards: never while she speaks or within ``_AFTER_SPEECH_S`` of it (her own voice saying her name must not wake her,
echo cancellation or not), never outside wake listening (the assistant checks that again before acting), at most one
transcription running and one started per ``_MIN_GAP_S`` of audio, and only for stretches between ``_MIN_SPEECH_MS``
and ``wake.second_chance_max_s`` long. The audio stays in memory and is dropped after; what was heard is never
logged unless it woke her, only counted (``WakeCounts``); the addressee check (``octavia.addressee``), when on, is
handed what didn't start with her name and keeps it only as its own rules say.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from rapidfuzz import fuzz

from octavia.audio.types import FRAME_MS, SAMPLE_RATE
from octavia.core.events import SpeakingFinished, SpeakingStarted, WakeDetected
from octavia.core.pauses import only_fillers
from octavia.vad.endpointer import Endpointer, EndpointerSettings, EndpointKind

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.core.events import Event, EventBus
    from octavia.stt.base import SpeechToText
    from octavia.vad.base import VoiceActivityDetector

log = logging.getLogger(__name__)

_END_SILENCE_MS = 600  # the quiet that ends a stretch of speech (shorter than a request's: only the name is needed)
_MIN_SPEECH_MS = 300  # less voiced audio than this isn't a name (a cough, a click, "mm")
_AFTER_SPEECH_S = 1.0  # her own voice, and its echo in the room, are given this long to die away
_MIN_GAP_S = 1.5  # at most one transcription started per this much audio, so a chatty room can't hog the CPU
_SUMMARY_S = 60.0  # the counts go to the log at most this often
_FUZZY_SCORE = 85.0  # rapidfuzz ratio of the name as heard to a spelling of it: "octavio" vs "octavia" is 86
# Said before the name, they don't count as the name being in the middle: "hey Octavia", "okay so, Octavia".
_GREETINGS = frozenset({"hey", "hi", "hello", "okay", "ok", "so", "yo", "oh", "um", "uh", "and", "well"})
_MAX_GREETINGS = 2
_WORD = re.compile(r"[^\W_]+")
_SENTENCE_END = (".", "?", "!")


def _key(text: str) -> str:
    """Lowercase letters and digits only: "Octavia," and "octavia" are the same word."""
    return "".join(_WORD.findall(text.lower()))


def name_spellings(config: AppConfig) -> list[list[str]]:
    """Each way her name may be written, as its words without a greeting: "hey computer" -> ["computer"]."""
    phrases = [config.assistant.wake_phrase, config.assistant.name, *config.wake.sherpa_kws.extra_spellings]
    out: list[list[str]] = []
    for phrase in phrases:
        words = [k for k in (_key(w) for w in phrase.split()) if k]
        while len(words) > 1 and words[0] in _GREETINGS:
            words.pop(0)
        if words and words not in out:
            out.append(words)
    return out


def _name_at(
    keys: Sequence[str], at: int, spellings: Sequence[Sequence[str]], greetings: int = _MAX_GREETINGS
) -> tuple[float, int] | None:
    """Her name starting at word ``at`` (after up to ``greetings`` greetings): its score and the index just past it,
    or None. Heard as one of ``spellings`` or close to it (one word more or less, for "octa via")."""
    best: tuple[float, int] | None = None
    for start in range(at, min(at + greetings, len(keys)) + 1):
        if start > at and keys[start - 1] not in _GREETINGS:
            break
        for words in spellings:
            target = "".join(words)
            for n in {len(words), len(words) + 1, max(1, len(words) - 1)}:
                if start + n > len(keys):
                    continue
                score = fuzz.ratio("".join(keys[start : start + n]), target)
                if score >= _FUZZY_SCORE and (best is None or score > best[0]):
                    best = (score, start + n)
    return best


def after_name(text: str, spellings: Sequence[Sequence[str]]) -> str | None:
    """What was said after her name when ``text`` calls her ("" for the name alone), or None when it doesn't.

    It calls her when it starts with her name (after up to ``_MAX_GREETINGS`` greetings: "hey", "okay so"); when her
    name starts a sentence after one said to someone else and is set off ("Thanks, guys. Octavia, what's the time?",
    not "Right. Octavia doesn't know"); or when her
    name, set off by a comma, is the last thing said ("Hold on, Octavia"): then nothing's asked yet (""). Anywhere
    else it's just talk ("I told Octavia yesterday").
    """
    tokens = [t for t in text.split() if _key(t)]
    keys = [_key(t) for t in tokens]
    for at in range(len(tokens)):
        before = tokens[at - 1].rstrip("\"')") if at else ""
        if at and not before.endswith(_SENTENCE_END + (",",)):
            continue
        comma = at and not before.endswith(_SENTENCE_END)
        # After a comma, the name itself must follow ("Sam, Bob, and Octavia" is a list, not a call).
        found = _name_at(keys, at, spellings, greetings=0 if comma else _MAX_GREETINGS)
        if found is None:
            continue
        if comma:  # only as the last thing said
            if found[1] == len(tokens):
                return ""
            continue
        if at and found[1] < len(tokens) and not tokens[found[1] - 1].endswith((",", ":", *_SENTENCE_END)):
            continue  # a sentence about her ("Octavia is kind of dumb"), not to her ("Octavia, ...")
        rest = " ".join(tokens[found[1] :]).lstrip(" ,.!?;:-")
        return "" if not _key(rest) or only_fillers(rest) else rest
    return None


def names_her(text: str, spellings: Sequence[Sequence[str]]) -> bool:
    """Whether her name is anywhere in ``text`` (as ``after_name`` would hear it at the start)."""
    keys = [_key(t) for t in text.split() if _key(t)]
    return any(_name_at(keys, at, spellings) is not None for at in range(len(keys)))


@dataclass(frozen=True)
class SecondChanceWake:
    """Her name, heard on the second listen."""

    audio: np.ndarray  # the whole stretch of speech, name and all
    rest: str  # what the user said after the name ("" when they only said it)
    ended_at: int  # the microphone frame the stretch ended on (what came after it was heard since)
    latency_s: float = 0.0  # how long the transcription took


@dataclass
class WakeCounts:
    detector: int = 0  # woken by the wake detector
    second: int = 0  # woken on the second listen: the detector missed these
    ignored: int = 0  # stretches of speech transcribed that didn't call her
    named: int = 0  # ...of them, ones with her name somewhere in them (the user talking about her, or a call missed)
    skipped: int = 0  # stretches not transcribed (too short, too long, too soon, while she spoke, CPU busy)


class SecondChance:
    """Collects speech heard while she waits for the wake word and finds her name in it (see the module docstring).

    The assistant feeds it each wake-listening frame the detector didn't wake on (``process``); a stretch to
    transcribe comes back as a coroutine to run, which calls ``wake`` when her name starts it. ``wake`` says
    whether it was taken (she may have started something else meanwhile).
    """

    def __init__(
        self,
        config: AppConfig,
        events: EventBus,
        vad: VoiceActivityDetector,
        stt: SpeechToText,
        wake: Callable[[SecondChanceWake], bool],
        frames_heard: Callable[[], int],
        correct: Callable[[str], str] | None = None,
    ) -> None:
        self.vad = vad
        self.stt = stt
        self._wake = wake
        self._frames_heard = frames_heard
        self._correct = correct  # the user's speech vocabulary (improvement #72), for a misheard name
        self._spellings = name_spellings(config)
        v = config.vad
        self._ear = Endpointer(
            EndpointerSettings(
                threshold=v.threshold,
                neg_threshold=v.neg_threshold,
                min_speech_ms=_MIN_SPEECH_MS,
                end_silence_ms=_END_SILENCE_MS,
                pre_roll_ms=v.pre_roll_ms,
                max_utterance_s=config.wake.second_chance_max_s,
                no_speech_timeout_s=math.inf,
                short_utterance_extra_ms=0,
            )
        )
        self._neg_threshold = v.neg_threshold
        self._last_frame = -2  # the frame last fed in: a gap means wake listening began again
        self._kws_fired = False  # the wake detector fired during this stretch: it's not a miss
        self._draining = False  # a stretch ran too long: wait for quiet before collecting again
        self._quiet_ms = 0.0
        self._started_s = 0.0  # audio time the stretch being collected began
        self._busy = False  # a transcription of ours is running
        self._last_try_s = -math.inf  # audio time the last transcription started
        self._speaking = False
        self._spoke_until_s = -math.inf  # audio time she last stopped speaking
        self.counts = WakeCounts()
        # What was heard without her name at the start, for the addressee check (``octavia.addressee``): the text and
        # how long the stretch was. Called on the event loop; it must not block. Nothing else keeps those words.
        self.overheard: Callable[[str, float], None] | None = None
        self._summarized = time.monotonic()
        self._summarized_counts = WakeCounts()
        events.subscribe(self._on_event, SpeakingStarted, SpeakingFinished, WakeDetected)

    def _now_s(self) -> float:
        return self._frames_heard() * FRAME_MS / 1000

    def _on_event(self, event: Event) -> None:
        if isinstance(event, SpeakingStarted):
            self._speaking = True
        elif isinstance(event, SpeakingFinished):
            self._speaking = False
            self._spoke_until_s = self._now_s()
        elif isinstance(event, WakeDetected) and not event.during_speech and event.phrase != "(hotkey)":
            if event.second_chance:
                self.counts.second += 1
                log.debug("Wake word heard on the second listen (the wake detector missed it)")
            else:
                self.counts.detector += 1
                log.debug("Wake word heard by the wake detector")
            self._summarize()

    def _summarize(self) -> None:
        """How often the wake detector misses, in the log at most once a minute (only counts, never words)."""
        now = time.monotonic()
        if now - self._summarized < _SUMMARY_S or self.counts == self._summarized_counts:
            return
        self._summarized = now
        c = self.counts
        self._summarized_counts = WakeCounts(c.detector, c.second, c.ignored, c.named, c.skipped)
        log.info(
            "Wake word so far: %d by the wake detector, %d only on the second listen (%d other utterances ignored, "
            "%d of them with my name in them; %d not transcribed)",
            *(c.detector, c.second, c.ignored, c.named, c.skipped),
        )

    def reset(self) -> None:
        self._ear.reset(pre_roll=False)
        self._kws_fired = False
        self._draining = False
        self._quiet_ms = 0.0

    def process(self, frame: np.ndarray, kws: bool = False) -> Coroutine[Any, Any, None] | None:
        """One wake-listening frame (``kws``: the wake detector fired on it but couldn't wake her, e.g. in the
        cooldown). Returns the transcription to run when a stretch of speech worth a second listen just ended."""
        frame_no = self._frames_heard()
        if frame_no != self._last_frame + 1:
            self.reset()  # wake listening began again: what was collected before belongs to another moment
        self._last_frame = frame_no
        self._kws_fired |= kws
        prob = self.vad.probability(frame)
        if self._draining:
            # The rest of a stretch too long to be someone calling her: skipped until they pause.
            self._quiet_ms = self._quiet_ms + FRAME_MS if prob < self._neg_threshold else 0.0
            if self._quiet_ms >= _END_SILENCE_MS:
                self.reset()
            return None
        event = self._ear.process(frame, prob)
        if event is None:
            return None
        if event.kind is EndpointKind.SPEECH_STARTED:  # confirmed once _MIN_SPEECH_MS of it was voiced
            self._started_s = self._now_s() - _MIN_SPEECH_MS / 1000
            return None
        kws_fired = self._kws_fired
        self.reset()
        if event.kind is not EndpointKind.UTTERANCE or event.audio is None:
            return None
        if event.truncated:
            self._draining = True
            self.counts.skipped += 1
            return None
        if kws_fired:
            return None  # the detector heard it: nothing missed
        now = self._now_s()
        if self._speaking or self._started_s < self._spoke_until_s + _AFTER_SPEECH_S:
            self.counts.skipped += 1  # her own voice, or too close to it to tell
            return None
        if self._busy or self.stt.busy or now - self._last_try_s < _MIN_GAP_S:
            self.counts.skipped += 1
            return None
        self._busy = True
        self._last_try_s = now
        return self._hear(event.audio, frame_no)

    async def _hear(self, audio: np.ndarray, ended_at: int) -> None:
        try:
            result = await self.stt.transcribe(audio)
        except Exception:
            log.debug("Transcribing for the second listen failed", exc_info=True)
            return
        finally:
            self._busy = False
        text = result.text.strip()
        if self._correct is not None:
            try:
                text = self._correct(text)
            except Exception:
                log.debug("Speech vocabulary failed on the second listen", exc_info=True)
        rest = after_name(text, self._spellings)
        if rest is None:
            self.counts.ignored += 1
            if names_her(text, self._spellings):
                self.counts.named += 1
            if self.overheard is not None and text:
                try:
                    self.overheard(text, audio.size / SAMPLE_RATE)
                except Exception:
                    log.warning("The addressee check failed on what was overheard", exc_info=True)
            return
        if not self._wake(SecondChanceWake(audio=audio, rest=rest, ended_at=ended_at, latency_s=result.latency_s)):
            self.counts.skipped += 1
