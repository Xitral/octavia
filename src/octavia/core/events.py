"""Typed application events and a small asyncio event bus.

Everything user-visible (the console status lines today, a GUI/HUD later) is
driven by subscribing to these events. Pipeline code emits events; it never
prints status directly.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger(__name__)


class AssistantState(str, Enum):
    STARTING = "starting"
    IDLE = "idle"  # passive: only the wake detector is listening
    LISTENING = "listening"  # capturing an utterance (after wake or during follow-up)
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"  # brain is working (may be calling tools)
    SPEAKING = "speaking"
    CONFIRMING = "confirming"  # waiting for the user to approve a risky tool call
    STOPPED = "stopped"


@dataclass(frozen=True, kw_only=True)
class Event:
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True, kw_only=True)
class StateChanged(Event):
    old: AssistantState
    new: AssistantState
    # Where the running turn came from when that's far from the PC ("discord": texted from the user's phone), else
    # None. Such a turn is answered where it came from, so nothing at the PC should come forward for it.
    remote: str | None = None


@dataclass(frozen=True, kw_only=True)
class WakeListening(Event):
    phrase: str
    engine: str


@dataclass(frozen=True, kw_only=True)
class WakeDetected(Event):
    phrase: str
    score: float | None = None
    during_speech: bool = False  # True when used as a barge-in trigger
    second_chance: bool = False  # True when the wake detector missed it and speech recognition heard the name


@dataclass(frozen=True, kw_only=True)
class ListeningStarted(Event):
    follow_up: bool = False


@dataclass(frozen=True, kw_only=True)
class SpeechStarted(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class SpeechEnded(Event):
    duration_s: float
    # The silence that ended it (None: not measured). What was said already sounded finished in that last pause
    # (``octavia.stt.early``), and the acknowledgment played this far into it (seconds from the last word); the user
    # went on talking after an earlier pause that sounded finished (so an early end there would have cut them off).
    silence_s: float | None = None
    sounded_finished: bool = False
    acknowledged_after_s: float | None = None
    continued: bool = False
    # Smart Turn's shadow verdicts (``octavia.vad.smart_turn``): how sure it was that the user had finished, in the
    # pause that ended the utterance, and in each earlier pause they went on talking after (verdict, pause ms).
    turn_end_p: float | None = None
    turn_pauses: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, kw_only=True)
class NoSpeech(Event):
    follow_up: bool = False


@dataclass(frozen=True, kw_only=True)
class Transcript(Event):
    text: str
    latency_s: float


@dataclass(frozen=True, kw_only=True)
class ToneDetected(Event):
    """How the user sounded in what they just said (``octavia.emotion``), heard from their voice, not their words:
    ``tone`` ("neutral", "stressed", "cheerful"...), how sure it is (0-1) and what in their voice said so ("faster
    than usual"). Emitted before the turn's BrainStarted, for every reading that was ready in time (neutral too)."""

    tone: str
    confidence: float
    cues: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class BrainStarted(Event):
    text: str
    # Who the turn is for: "voice", "text" or "discord" (the user; "discord" texted from their phone), "scheduled" (a
    # scheduled job's instruction). Only the user's own turns tell anything about what the user wants.
    source: str = "voice"
    turn_id: int = 0  # the same number in the turn's BrainFinished
    # What she does differently this turn (``futures.tuning``): (what, variant) pairs, "experiment:<id>" for a variant
    # of one of her experiments on herself, "habit:<id>" for a habit she kept from one.
    tuning: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, kw_only=True)
class BrainTextDelta(Event):
    text: str


@dataclass(frozen=True, kw_only=True)
class BrainFinished(Event):
    text: str
    interrupted: bool = False
    error: str | None = None
    source: str = "voice"
    turn_id: int = 0
    duration_s: float | None = None  # from the brain starting to its last word
    first_word_s: float | None = None  # until its first word, when it said any
    usage: dict[str, Any] | None = None  # the provider's own usage figures (tokens), when it gives them
    model: str | None = None  # the model that wrote the reply (its id), when the brain says
    head_start_s: float | None = None  # how long before the turn the brain had started on it, in the user's pause
    # Her opening words when they began this reply (``brain/opener.py``), and how the brain carried on from them
    # (``core/handoff.py`` ``Handoff.report``: the silence between them, a first sentence dropped as a repeat).
    opener: str | None = None
    handoff: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class OpenerChecked(Event):
    """Her opener's verdict on a spoken request (``brain/opener.py``): ``said`` is the phrase she said (``spoken``) or
    would have said in shadow mode, ``reason`` "ok" or why there was none ("handoff", "claim", "new", "timeout"...),
    ``wrote`` what the model wrote, said or not; ``ready_s`` from asking to the verdict."""

    text: str  # what the user said
    said: str | None
    reason: str
    ready_s: float
    spoken: bool = False
    first_word_s: float | None = None
    model: str | None = None
    wrote: str | None = None


@dataclass(frozen=True, kw_only=True)
class ClaimCorrected(Event):
    """A sentence of hers that her tools contradicted, said honestly instead (``core.claims``)."""

    sentence: str
    turn_id: int = 0


@dataclass(frozen=True, kw_only=True)
class PresetReplied(Event):
    """Answered from a preset reply (personality/quick_replies.yaml); the brain was not called."""

    text: str
    rule: str


@dataclass(frozen=True, kw_only=True)
class FastPathHandled(Event):
    """A quick command matched a fast-path rule (``core/fast_path.py``). ``shadow``: it was only logged, and the
    brain answered as usual; ``ok`` False: its tool failed, and the brain got it with the failure as a note."""

    text: str
    rule: str
    tool: str | None = None
    ok: bool = True
    shadow: bool = False
    said: str = ""
    duration_s: float = 0.0


@dataclass(frozen=True, kw_only=True)
class FastChatStarted(Event):
    """Small talk is being answered by her quick voice (``brain/fast_chat.py``), not the brain: its reply has begun
    and isn't a handoff, so it's about to be spoken. The matching ``FastChatFinished`` follows once it's done."""

    text: str  # what the user said
    source: str = "voice"
    first_word_s: float | None = None  # from asking the quick voice to its first word


@dataclass(frozen=True, kw_only=True)
class FastChatFinished(Event):
    """The quick voice is done with a turn. ``passed`` set: it didn't answer ("handoff": it said the turn needs the
    brain, "timeout", "error", "empty", "down": not connected), nothing was said, and the brain answers as usual (no
    ``FastChatStarted`` came before it). Otherwise ``said`` is its reply."""

    text: str  # what the user said
    source: str = "voice"
    said: str = ""
    model: str | None = None  # the model id that wrote it, as Claude reports it
    passed: str | None = None
    interrupted: bool = False
    duration_s: float | None = None  # from asking the quick voice to its last word (or to giving up on it)
    first_word_s: float | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class ImprovementChanged(Event):
    """A self-improvement item changed status (noted, building, ready, installed...)."""

    improvement_id: int
    title: str
    status: str
    message: str = ""  # human-readable detail, e.g. "4 files changed, all 322 tests pass"


@dataclass(frozen=True, kw_only=True)
class ToolCallStarted(Event):
    call_id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class ToolCallFinished(Event):
    call_id: str
    name: str
    ok: bool
    summary: str
    duration_s: float
    data: Mapping[str, Any] | None = None  # the result's own structured part (``ToolResult.data``), if any


@dataclass(frozen=True, kw_only=True)
class ToolsLoaded(Event):
    """The brain looked up tools it hadn't loaded (Claude Code's tool search): one more model round trip, 1-2.5 s.
    ``names`` are the tools a ``select:`` query named; a keyword query has none."""

    query: str
    names: tuple[str, ...] = ()
    early: bool = False  # made in an early start, before the turn took it up: told when it does


@dataclass(frozen=True, kw_only=True)
class LoudNoise(Event):
    """A sudden bang in the room (a door slamming), heard while waiting for the wake word: the avatar jumps. Only the
    microphone's loudness is looked at, on this PC (``audio.loudness``)."""


@dataclass(frozen=True, kw_only=True)
class CameraShutter(Event):
    """The avatar's camera pressed its shutter just now (its flash), in the pose a ``ScreenshotTaken`` started: the
    shutter sound plays on that beat."""


@dataclass(frozen=True, kw_only=True)
class ScreenshotTaken(Event):
    """A screenshot is being taken right now, so the user gets a cue: a shutter sound, the avatar's camera pose."""

    area: str  # "screen", "window" or "all"


@dataclass(frozen=True, kw_only=True)
class PermissionRequested(Event):
    tool: str
    description: str
    detail: str = ""  # the exact call, for a screen (``Tool.call_detail``); never said


@dataclass(frozen=True, kw_only=True)
class PermissionResolved(Event):
    tool: str
    approved: bool
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class SpeakingStarted(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class SpeechSegmentStarted(Event):
    """A sentence (segment) of speech starts playing now: its first audio reached the speaker."""

    text: str  # as spoken, including any voice cues the TTS performs ("[excited] ...")
    index: int  # its position in the reply, from 0
    shown: str | None = None  # as her subtitles show it ([[shown|said]] marks, math and code: tts/shown.py)
    beats: tuple[str, ...] = ()  # the explainer steps it plays ({{step-id}} in her reply: visuals/explainer.py)


@dataclass(frozen=True, kw_only=True)
class SpeechSegmentAudio(Event):
    """All of a sentence's audio has been made (and queued to play): it lasts ``seconds``. Often before the sentence
    starts playing, sometimes while it plays. Her subtitles pace its words by it (docs/subtitles.md)."""

    index: int  # the sentence's position in the reply, as SpeechSegmentStarted's
    seconds: float
    said: str = ""  # the words her voice was given for it
    # Her voice for it before any effect, mono at ``sample_rate``, for timing its words (tts/word_times.py)
    audio: np.ndarray | None = field(default=None, repr=False, compare=False)
    sample_rate: int = 0


@dataclass(frozen=True, kw_only=True)
class SpeechSegmentMaking(Event):
    """A sentence's audio is being made: ``dry`` grows with her voice for it (before any effect, mono at
    ``sample_rate``) as the voice streams it in, so its words can be timed before all of it is in
    (tts/word_times.py). Read it only on the event loop."""

    index: int
    said: str
    dry: list[np.ndarray] = field(default_factory=list, repr=False, compare=False)
    sample_rate: int = 0


@dataclass(frozen=True, kw_only=True)
class SpeechWordStarted(Event):
    """She reaches word ``word`` (from 0) of the ``of`` words her voice was given for sentence ``index``, by her
    audio as the speaker plays it (tts/word_times.py). Her subtitles light that word (docs/subtitles.md)."""

    index: int
    word: int
    of: int


@dataclass(frozen=True, kw_only=True)
class PhrasePlaying(Event):
    """A short cached phrase ("On it!") starts playing now, outside any reply (``SpeechOutput.play_phrase``).

    Not a SpeakingStarted: the assistant is still thinking, and the reply that follows starts speaking as usual.
    """

    text: str
    seconds: float


@dataclass(frozen=True, kw_only=True)
class AnnouncementStarted(Event):
    """An announcement (something said unprompted: a timer going off, a reminder) starts being said now.

    ``cue`` is what kind it is, when whoever asked for it said: ``timer``, ``reminder`` or ``morning`` (the morning
    rundown). The avatar matches it with a prop (docs/avatar-life-plan.md). ``full`` is the whole of it when
    ``text``, what's said, is only part (a long task report): the chat window shows that.
    """

    text: str
    cue: str | None = None
    full: str | None = None


@dataclass(frozen=True, kw_only=True)
class TasksCleared(Event):
    """The user just ticked off the last of what was due today (or overdue) in TickTick: the avatar does a victory
    lap round the screen."""


@dataclass(frozen=True, kw_only=True)
class Dismissed(Event):
    """The user waved her off ("never mind", "stop") instead of asking anything: the conversation ends there. Right
    after something she said on her own, it's how that went down (Octavia Futures)."""

    text: str


@dataclass(frozen=True, kw_only=True)
class AppContextChanged(Event):
    """Another app came to the front and stayed there for a moment (futures/sources.py watches; names only, never
    window titles)."""

    app: str  # a speakable name ("Blender", "Visual Studio Code")
    process: str  # its program ("blender.exe")
    previous: str = ""  # the app that was in front before, if any
    previous_s: float = 0.0  # how long that one was in front


@dataclass(frozen=True, kw_only=True)
class TaskChanged(Event):
    """A to-do changed through one of her TickTick tools (``tools/extensions/ticktick.py``): the only place the id of
    a task she creates is known."""

    action: str  # "created", "updated", "completed", "deleted" or "moved"
    task_id: str = ""
    project_id: str = ""
    title: str = ""
    due: str = ""  # TickTick's own date string, when the call carried one
    all_day: bool = False
    priority: int | None = None


@dataclass(frozen=True, kw_only=True)
class TimerChanged(Event):
    """A timer was set, rang or was cancelled (``tools/extensions/timers.py``)."""

    action: str  # "created", "rang" or "cancelled"
    timer_id: int
    seconds: float
    label: str = ""
    alert: str = "speak"


@dataclass(frozen=True, kw_only=True)
class EmailArrived(Event):
    """New mail in the inbox, seen by the Gmail watcher (headers only, never a body)."""

    key: str  # stable for this message: the mailbox's validity and the message's uid
    sender: str  # the sender's name (or address when there's no name)
    subject: str = ""
    unread: bool = True
    sent_at: float | None = None  # epoch seconds, from its Date header


@dataclass(frozen=True, kw_only=True)
class SpeakingFinished(Event):
    interrupted: bool = False
    underruns: int = 0  # times the speaker's queue ran dry mid-sentence while this stream played (a heard gap)
    underrun_s: float = 0.0  # how long those gaps were, in all
    stalled_s: float = 0.0  # how long her event loop was stuck meanwhile (``core/loop_watch.py``)


@dataclass(frozen=True, kw_only=True)
class Interrupted(Event):
    reason: str
    # How long the user had been talking over her when her voice stopped (a voice barge-in), when it's known.
    after_s: float | None = None


@dataclass(frozen=True, kw_only=True)
class SaidAgain(Event):
    """The user asked her to say her last reply again ("sorry, what?"), and she did, with no model
    (``QuickTurns._say_again``): her own record of a reply that didn't land the first time."""

    text: str


@dataclass(frozen=True, kw_only=True)
class SessionStarted(Event):
    session_id: str


@dataclass(frozen=True, kw_only=True)
class SessionEnded(Event):
    session_id: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class Notice(Event):
    """A generic human-readable status line from a subsystem."""

    source: str  # e.g. "WAKE", "STT", "TTS", "APP"
    message: str
    level: int = logging.INFO


@dataclass(frozen=True, kw_only=True)
class ErrorOccurred(Event):
    source: str
    message: str
    fatal: bool = False


Handler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """Fan-out of events to subscribers.

    ``emit`` is synchronous and cheap: sync handlers run inline, async handlers
    are scheduled as tasks. A failing handler is logged and never breaks the
    emitter. Use ``emit_threadsafe`` from non-asyncio threads (audio callbacks).
    """

    def __init__(self) -> None:
        self._subs: list[tuple[tuple[type[Event], ...], Handler]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, handler: Handler, *types: type[Event]) -> Callable[[], None]:
        entry = (types or (Event,), handler)
        self._subs.append(entry)

        def unsubscribe() -> None:
            if entry in self._subs:
                self._subs.remove(entry)

        return unsubscribe

    def emit(self, event: Event) -> None:
        for types, handler in list(self._subs):
            if not isinstance(event, types):
                continue
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(result)
                    self._tasks.add(task)
                    task.add_done_callback(self._on_task_done)
            except Exception:
                log.exception("Event handler %r failed for %s", handler, type(event).__name__)

    def emit_threadsafe(self, event: Event) -> None:
        if self._loop is None:
            raise RuntimeError("EventBus.bind_loop() must be called before emit_threadsafe()")
        self._loop.call_soon_threadsafe(self.emit, event)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Async event handler failed", exc_info=task.exception())
