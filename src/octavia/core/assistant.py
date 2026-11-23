"""The conversation orchestrator.

Two cooperating asyncio tasks:

* the **audio loop** reads 32 ms microphone frames and routes each one by the
  current listening mode: wake detection, utterance capture (VAD +
  endpointer), or barge-in detection while the assistant thinks or speaks;
* the **conversation loop** waits for the wake phrase, captures and
  transcribes utterances, runs brain turns with streaming speech, and keeps
  listening for follow-ups until the conversation goes quiet.

Interruption is designed in from the start: a turn is a brain task plus a
speech stream, and either can be cancelled the moment barge-in fires.

``Assistant`` is put together from parts, each in its own module and all on the one object's state: ``hearing``
(the audio loop, capture, barge-in, transcripts), ``announcing`` (announcements, scheduled and texted turns),
``quick_turns`` (answers without the brain) and ``approvals`` (the tool executor's confirmer). This module keeps the
conversation and the brain's turns.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from octavia.audio.loudness import BangDetector
from octavia.audio.types import FRAME_MS
from octavia.audio.voice_volume import apply_gain
from octavia.brain.base import BrainProvider, ResponseDone, TextDelta, ToolUseStarted, TurnPicture, UserTurn
from octavia.config import READ_ONLY_BUILTINS
from octavia.core.announcing import AnnouncingMixin
from octavia.core.approvals import _MAX_QUESTIONS, _QUESTION_TIME_S, ApprovalsMixin
from octavia.core.approvals import _REASK_SILENT as _REASK_SILENT
from octavia.core.approvals import _REASK_UNCLEAR as _REASK_UNCLEAR
from octavia.core.backchannel import Backchannel
from octavia.core.claim_hold import ClaimHold, without_dropped
from octavia.core.claims import Act
from octavia.core.events import (
    AssistantState,
    BrainFinished,
    BrainStarted,
    BrainTextDelta,
    CameraShutter,
    Dismissed,
    ErrorOccurred,
    Event,
    EventBus,
    ListeningStarted,
    NoSpeech,
    Notice,
    ScreenshotTaken,
    SpeakingFinished,
    SpeakingStarted,
    StateChanged,
    ToolCallFinished,
    ToolCallStarted,
    WakeDetected,
)
from octavia.core.fast_path import spoken_model
from octavia.core.handoff import Handoff
from octavia.core.hearing import HearingMixin
from octavia.core.hearing import strip_wake_phrase as strip_wake_phrase
from octavia.core.opening import OpeningMixin
from octavia.core.panel_claims import kinds as claim_kinds
from octavia.core.panel_pointers import without_panel_pointers
from octavia.core.pauses import only_fillers, sounds_unfinished
from octavia.core.quick_turns import _CARRY_ON_NOTE, QuickTurnsMixin, paused_note
from octavia.core.quiet import asks_for_quiet, asks_for_talk
from octavia.core.said_once import without_retelling
from octavia.core.say_again import asks_to_say_again
from octavia.core.session import SessionManager
from octavia.core.speech import SpeechOutput, SpeechStream, resample_linear
from octavia.core.step_notes import NOTE_MAX_WORDS, about_her_tools, is_note_to_self
from octavia.core.stopping import stop_kind
from octavia.core.text import normalize_text
from octavia.core.turn_types import (
    _ASKS_WHO_ANSWERED,
    _ON_HOLD_NOTE,
    _ROUTED_SOURCES,
    _USER_SOURCES,
    ASIDE,
    Announcement,
    BackgroundTurn,
    ListenMode,
    TurnResult,
    WorkQuestion,
)
from octavia.core.watchdog import QUESTION_BACKSTOP_S, TurnWatchdog
from octavia.core.work_intent import WorkHold
from octavia.core.yes_or_no import parse_confirmation as parse_confirmation
from octavia.emotion.tone import ToneJob
from octavia.localbrain.delegation import asks_something
from octavia.stt.early import EarlyTranscriber
from octavia.study.at_screen import OWED_NOTE, AwayFromScreen, panel_owed, studying_note
from octavia.tools.asked import asks_to_look, asks_to_send, questions_in, refuses_look, says_yes
from octavia.tools.base import TurnInfo
from octavia.tools.permissions import READS_OUTSIDE, Confirmer, ConsoleConfirmer, Offer
from octavia.vad.endpointer import Endpointer, EndpointerSettings, EndpointEvent, EndpointKind
from octavia.vad.smart_turn import TurnShadow
from octavia.wake.base import WakeDetection
from octavia.wake.second_chance import SecondChance

if TYPE_CHECKING:
    from octavia.audio.earcons import Earcons
    from octavia.audio.source import AudioSource
    from octavia.brain.fast_chat import FastChat
    from octavia.brain.opener import Opener
    from octavia.config import AppConfig
    from octavia.core.acknowledge import Acknowledger
    from octavia.core.fast_path import FastPath
    from octavia.core.known_file_guard import KnownFileGuard
    from octavia.core.quick_replies import QuickReplies
    from octavia.core.unprompted import Gate, Line
    from octavia.emotion.tone import VoiceTone
    from octavia.gesture.watch import Circled, CircleMemory
    from octavia.stt.base import SpeechToText
    from octavia.stt.vocabulary import VocabularyFile
    from octavia.vad.base import VoiceActivityDetector
    from octavia.wake.base import WakeWordDetector

log = logging.getLogger(__name__)
_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*(?:\s+|$)")  # where a whole sentence ends

# Where a reply that isn't spoken is still shown to the user at the PC: the console and her chat window. (A text to
# their phone isn't here: a "yes" said at the PC isn't an answer to it.)
_SHOWN_HERE = frozenset({"text", "window"})
# Where the user is at the PC, so what they just circled there can be what they mean.
_CIRCLE_SOURCES = frozenset({"voice", "text", "window"})
# Their settings have her ask before she looks, so a circle can't send the picture by itself.
_CIRCLED_ASK_NOTE = (
    "the user just circled part of their screen with the mouse. When their words point at something on screen without "
    "naming it, however they put it, they mean what they circled: look_at_screen shows you where it is"
)


_DISMISSALS = {
    "stop", "cancel", "never mind", "nevermind", "forget it", "nothing", "that's all", "that's it",
    "that is all", "shut up", "be quiet", "quiet", "silence", "go away", "stop talking", "no thanks",
}  # fmt: skip
_INTERRUPTED_NOTE = "you were interrupted"
# Her opening words (``core/opening.py``) began the reply out loud: the brain carries on from those exact words.
_OPENED_NOTE = (
    'you have already begun your reply out loud with these exact words: "{words}". Carry straight on from them as one '
    "spoken reply: don't say them again, don't acknowledge or greet again, and don't contradict them; if they don't "
    "fit what you find, say so plainly"
)
_YES_WORDS = 4  # "yes", "yeah sure", "sure go ahead": an answer, not a new subject


def _just_yes(said: str, asked: str | None) -> bool:
    """Whether ``said`` is only a yes ("yeah", "sure, go ahead") to ``asked``, her last line, which asked something.
    Never a no: "don't click that" or "not that one" is the user stepping in, and the task must wait for it."""
    return (
        bool(asked)
        and asks_something(asked or "")
        and len(said.split()) <= _YES_WORDS
        and parse_confirmation(said) is True
    )


# An announcement is said outside any turn, so the brain never sees it unless it's passed along with the next one.
_QUIET_NOTE = (
    "the user asked you to work quietly: say nothing between your steps (it isn't spoken anyway), no narrating or "
    "describing the screen; when it's done, one short sentence with the result, or a question only if you're stuck"
)
_CALL_NOTE = (
    "the user is talking to you over a Discord voice call from their phone, away from the PC: they hear you but "
    "can't see the screen, so say what matters out loud and keep it short. send_to_phone texts them a file or a link"
)
_OTHER_CALL_NOTE = (
    "the user is on a call in {apps} with other people, so not everything they say is for you or about them. Don't "
    "save anything with remember from talk that may have been for the call, and don't take it as facts about the user"
)
_RECALL_TIMEOUT_S = 0.5  # the most the automatic memory lookup may delay a turn
# A turn that goes this long without anything audible says "Still on it." (``Assistant._still_working``), checked every
# few seconds, at most twice a turn.
STILL_WORKING_S = 30.0
STILL_WORKING_CHECK_S = 3.0
STILL_WORKING_TIMES = 2

# The most a turn waits for the notes its early start is still working out (``_EarlyTurn``): the memory lookup's own
# limit and the voice tone's, with room to spare. Past it, the turn works them out itself.
_EARLY_WAIT_S = 1.5
# An early start older than this is stale: its turn never came (the brain drops its side at about the same age).
_EARLY_STALE_S = 5.0
# The brain starts early in at most this many pauses of one request: each start the user talks on past is a Claude
# call thrown away, and someone thinking out loud can pause a dozen times.
_EARLY_PAUSES = 3
# How far into a pause the voice tone starts reading what the user said (``_read_tone_early``): soon enough to be done
# before the utterance closes, late enough not to start at every gap between two words.
_TONE_PAUSE_MS = 160.0
# Words about her seeing the screen that don't ask her to look ("don't look at my screen", "stop taking
# screenshots", "ask me first"): they end that standing yes, and she asks again before the next look.
_SCREEN_TALK = re.compile(
    r"\b(?:screens?|screen\s*(?:shots?|grabs?|caps?|captures?)|monitors?|displays?|peek\w*|snoop\w*|spy\w*)\b"
    r"|\b(?:don'?t|do\s+not|stop|quit|never|no\s+more)\b(?:\s+[\w']+){0,2}?\s+(?:look|looking|watch|watching)\b"
    r"|\bask\s+(?:me\s+)?(?:first|before)\b"
)
# A stop heard without the wake phrase while she works (``_listen_for_stop``) is short: anything longer is something
# else, and isn't transcribed.
_STOP_MAX_S = 3.0
# How long to keep listening after a transcript that trails off ("and...", "um"), and how many times one request may
# trail off ("and... um... so...") and still be waited for.
_UNFINISHED_WAIT_S = 2.0
_MAX_HOLDS = 3
_RELISTEN_MIN_S = 1.0  # after a sound with no words, the listen window goes on only if at least this much is left
# On a call in another app, she listens for a follow-up only when she asked something this recently (``_just_asked``).
_JUST_ASKED_S = 60.0
# Right after the wake detector fires, an utterance with no more speech than this after the moment it fired was said
# (nearly) all before it: the end of her name the detector caught, heard as words ("...tavia" as "save you").
_NAME_TAIL_LIVE_MS = 224.0  # 7 frames: the last of the name may still be sounding as it fires
_NAME_TAIL_MAX_WORDS = 2
# The watchdog stopped her last turn (``core/watchdog.py``): the brain is told, so it doesn't carry on as if it hadn't.
_STUCK_NOTE = (
    "your previous turn got stuck with nothing happening and was stopped before you finished; whatever step you were "
    "on may or may not have happened. Don't pick that request back up unless the user asks you to"
)
# ...after the brain had finished, while her voice was saying the answer.
_STUCK_SPEAKING_NOTE = "your previous answer was complete, but saying it out loud got stuck and was stopped part-way"
_STUCK_SAY_S = 15.0  # the most saying so may take: her voice may be what got stuck
_STEP_NOTE_WORDS = 12  # what she writes before a step and is no longer than this is a status line, not an answer


def texted_reply(steps: Sequence[str]) -> str:
    """Her answer to a text message from what she wrote between her steps (oldest first): the last thing she wrote,
    and anything earlier that's more than a short status line ("Checking your calendar.")."""
    written = [step for step in steps if step.strip()]
    kept = [step for step in written[:-1] if len(step.split()) > _STEP_NOTE_WORDS]
    if not written or len(kept) == len(written) - 1:
        return "".join(steps)  # nothing to leave out: what she wrote, as she wrote it
    return "\n\n".join(step.strip() for step in [*kept, written[-1]])


def _log_early_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning("Starting the brain early failed", exc_info=exc)


@dataclass(eq=False)
class _EarlyTurn:
    """A brain turn started in the user's pause (``_start_early``): what it was started on, and the notes it worked
    out once. The turn that follows reuses them, so it renders the same (and the brain takes the early start up) and
    the voice tone is concluded only once."""

    text: str
    acknowledged: str | None  # what she said in that pause ("On it!"), or None when nothing fitted ("Yes.")
    sound_frames: int  # the endpointer's count when it began: more speech since means the user went on
    tone: ToneJob | None
    begun: float  # time.monotonic()
    dropped: bool = False
    task: asyncio.Task[tuple[str | None, str | None, datetime]] | None = None  # (recalled, heard, timestamp)
    opened: bool = False  # ``acknowledged`` is her opening words (``core/opening.py``), not a cached phrase


class Assistant(HearingMixin, AnnouncingMixin, QuickTurnsMixin, ApprovalsMixin, OpeningMixin):
    def __init__(
        self,
        config: AppConfig,
        events: EventBus,
        brain: BrainProvider,
        speech: SpeechOutput | None,
        sessions: SessionManager,
        stt: SpeechToText | None = None,
        vad: VoiceActivityDetector | None = None,
        wake: WakeWordDetector | None = None,
        source: AudioSource | None = None,
        earcons: Earcons | None = None,
        text_confirmer: Confirmer | None = None,
        acknowledger: Acknowledger | None = None,
        quick_replies: QuickReplies | None = None,
        fast_path: FastPath | None = None,
        vocabulary: VocabularyFile | None = None,
        fast_chat: FastChat | None = None,
    ) -> None:
        self.config = config
        self.events = events
        self.brain = brain
        self.speech = speech
        self.sessions = sessions
        if sessions is not None:  # some tests build her without conversations
            sessions.may_roll = self.may_start_afresh  # a too-big context starts afresh only when nothing waits on it
        self.stt = stt
        self.vad = vad
        self.wake = wake
        self.source = source
        self.earcons = earcons
        self.text_confirmer = text_confirmer or ConsoleConfirmer()
        self.acknowledger = acknowledger
        self.quick_replies = quick_replies
        self.fast_path = fast_path  # quick commands that skip the brain (routing.fast_path)
        # Who gave her last answer, for "which model was that?": a model, a rule or a preset reply.
        self._answered_by: str | None = None
        if fast_path is not None:
            fast_path.answered_by = lambda: self._answered_by
            fast_path.work_paused = lambda: self._paused_work() is not None
        self.vocabulary = vocabulary
        self.fast_chat = fast_chat  # her quick voice for small talk (``fast_chat``); None = the brain answers it all
        # The latest lines of the conversation as (session, role, text), what the quick voice is shown to stay on topic.
        self._said: deque[tuple[str | None, str, str]] = deque(maxlen=32)
        self._last_line_at: float | None = None  # when the latest of them was recorded (``time.time()``)
        self._last_spoken: tuple[float, str] | None = None  # (when, what) she last said out loud: "say that again"

        v = config.vad
        self.endpointer = Endpointer(
            EndpointerSettings(
                threshold=v.threshold,
                neg_threshold=v.neg_threshold,
                min_speech_ms=v.min_speech_ms,
                end_silence_ms=v.end_silence_ms,
                pre_roll_ms=v.pre_roll_ms,
                max_utterance_s=v.max_utterance_s,
                no_speech_timeout_s=v.no_speech_timeout_s,
            )
        )
        # While she works, short utterances are heard without the wake phrase, in case they're a stop.
        self._stop_ear = Endpointer(
            EndpointerSettings(
                threshold=v.threshold,
                neg_threshold=v.neg_threshold,
                min_speech_ms=v.min_speech_ms,
                end_silence_ms=v.end_silence_ms,
                pre_roll_ms=v.pre_roll_ms,
                max_utterance_s=_STOP_MAX_S,
                no_speech_timeout_s=math.inf,
                short_utterance_extra_ms=0,
            )
        )
        self._stop_ear_on = False
        self._hearing: set[asyncio.Task[None]] = set()
        # Set by the app when screen tasks run beside the conversation: whether one is running or waiting, stopping
        # it (True: everything waiting too), and pausing it until the user asks to carry on; both return the line to
        # say about it, or None when there was nothing to stop or pause.
        self.work_running: Callable[[], bool] | None = None
        self.stop_work: Callable[[bool], str | None] | None = None
        self.pause_work: Callable[[], str | None] | None = None
        # The title of the screen task paused last, or None when none is: "continue" then carries on with it.
        self.work_paused: Callable[[], str | None] | None = None
        # Holding the running task off the screen while she answers what the user said (a ``Hold``, or None when
        # nothing runs or is paused), and after her answer letting it carry on or keeping it paused by the verdict on
        # their words (``ScreenTaskManager.hold`` and ``release``; ``_hold_work``). None: talk pauses it, as before.
        self.hold_work: Callable[[], Any] | None = None
        self.release_work: Callable[[Any, str | None], str] | None = None
        self._work_hold: WorkHold | None = None  # the hold waiting for her answer (``_settle_work_hold``)
        self._settling: list[WorkHold] = []  # holds settled after her answer whose judgments are still out
        self._releasing: set[asyncio.Task[None]] = set()
        # Set by the app when memory is on: what the user's words bring up from saved notes and past conversations,
        # as a note for their turn (``octavia.core.conversation_memory.AutoRecall``).
        self.recall: Callable[[str], Awaitable[str | None]] | None = None
        # Set by the app when the voice tone is on (``emotion``): how the user sounds, read from each utterance they
        # say to her while the end-of-speech silence is still being waited out (``_read_tone_early``).
        self.voice_tone: VoiceTone | None = None
        # Set by Futures (``futures.tuning.Tuner``): what she does differently on a turn, from her experiments on
        # herself or a habit she kept from one. Its notes join the turn's; what it chose rides on BrainStarted.
        self.tuner: Any = None
        self._tuning: tuple[
            tuple[str, str], ...
        ] = ()  # what the tuner chose for the turn whose notes were last written
        self._tone_wanted = False  # the capture running is one of the user's requests (not a yes or no)
        self._tone_job: ToneJob | None = None  # the reading of the capture running, from its latest pause
        self._tone_armed = True  # the user spoke again since the last reading began: their next pause starts one
        self._tone_pause_ms = min(_TONE_PAUSE_MS, v.end_silence_ms / 2)
        # What the user has said so far, transcribed while they pause (``stt/early.py``), and the pauses after which
        # it already sounded finished (by the endpointer's ``sound_frames``).
        self._early = (
            EarlyTranscriber(stt, self.endpointer, v.early_transcript_ms, clean=self._clean)
            if stt is not None
            else None
        )
        self._finished_pauses: set[int] = set()
        # The acknowledgment said in a pause of the capture running, before it ended (``_acknowledge_early``), and
        # how far into the pause (ms).
        self._early_said: tuple[str, float] | None = None
        # The brain turn begun in that pause, before the silence was over (``_start_early``), if any.
        self._early_turn: _EarlyTurn | None = None
        # Set by the app when her opening words are on or in shadow (``conversation.opener``, ``core/opening.py``):
        # what asks for them; the asking for the request in the pause (or turn) now; the wait for them where the
        # early acknowledgment would play.
        self.opener: Opener | None = None
        self._opening = None
        self._early_pending: asyncio.Task[None] | None = None
        self._opener_refused: tuple[str, int | None] | None = None  # the last request that didn't fit
        # Set by the app when backchannels are on (``conversation.backchannel``): a soft "mhm" in a pause to think.
        self.backchannel: Backchannel | None = None
        # Set by the app when the Smart Turn model is there: its verdict on each pause of a request, logged only.
        self.turn_shadow: TurnShadow | None = None
        self.state = AssistantState.STARTING
        self._mode = ListenMode.OFF
        self._wake_waiter: asyncio.Future[WakeDetection | None] | None = None
        self._announcements: asyncio.Queue[Announcement | BackgroundTurn | WorkQuestion] = asyncio.Queue()
        self._announcing = False
        self._capture_waiter: asyncio.Future[EndpointEvent] | None = None
        self._interrupt: asyncio.Event | None = None
        self._barge_kind: str | None = None
        self._barge_run_ms = 0.0
        self._played_s = 0.0  # total playback time, for echo-canceller warmup
        self._speaking_since: float | None = None
        self._stop_requested = False
        self._turn_first_audio: float | None = None
        self._audible_at = 0.0  # time.monotonic() she was last heard (``_still_working``)
        self.echo_cancellation_active = False  # set by the app when the AEC is running
        self.echo_reduction_db: Callable[[], float | None] | None = None  # set by the app: the AEC's measured ERLE
        self._echo_trusted = False  # see ``_echo_cancelled_well``
        self._recent: deque[np.ndarray] = deque(maxlen=max(1, int((v.pre_roll_ms + 1000) / FRAME_MS)))
        self._frames_heard = 0  # microphone frames so far, to tell which of ``_recent`` came after a capture ended
        self._ears_off = False  # muted from the tray: she hears nothing but the talk hotkey (``set_listening``)
        self._talk_pressed = False  # the talk hotkey opened what she's hearing now: it gets through a mute
        self._capture_ended_at = 0
        # The capture began as the wake detector fired: what it hears first may be only the end of her name
        # (``_only_name_tail``). How much speech its seed held (said before it began), and how much it heard since.
        self._name_tail_possible = False
        self._seed_speech_ms = 0.0
        self._live_speech_ms = 0.0
        self._seeding = False
        self._pending_notes: list[str] = []
        # Set by the app when circling is on (``octavia.gesture``): what the user last circled on their screen with the
        # mouse, and whether a circle may send its picture with their words (their settings let her look unasked).
        self.circles: CircleMemory | None = None
        self.circle_shows: Callable[[], bool] | None = None
        # Her reply, a piece at a time before it's spoken and whole before it's shown: what she says she did, held to
        # what her tools did (``ClaimWatch.vet``, claims.mode enforce). None: said as written.
        self.vet_claims: Callable[[str], str] | None = None
        # The course the user is studying right now, "" for none (set by the app): a turn at the screen says so.
        self.studying: Callable[[], str] | None = None
        # Whether one of her panels is on screen now (set by the app): a sentence about a panel that's up already
        # isn't held until a panel call (``panel_claims``).
        self.panels_up: Callable[[], bool] | None = None
        # Her calls in the few turns before this one (set by the app, ``ClaimWatch.earlier``): a claim about one of
        # them isn't held for a call in this turn (``claim_hold``).
        self.earlier_acts: Callable[[], list[Act]] | None = None
        # A sentence asking the user for a file she can read herself: stopped, and she goes and reads it.
        self.known_files: KnownFileGuard | None = None
        self._active_stream: SpeechStream | None = None
        self._turn_active = False
        self._turn_text: str | None = None  # what the user said or typed in the running turn (see ``requested``)
        self._turn_source: str | None = None  # where the running turn of theirs came from ("voice", "discord"...)
        self._remote_busy: str | None = None  # a remote turn (``run_from``) runs: its source, told with each state
        self._from_away: list[BackgroundTurn] = []  # turns texted from far away (``run_from``) not answered yet
        self._running_away: BackgroundTurn | None = None  # the one of them that runs now
        self._follow_up_open = False  # listening for a follow-up without her name (``_end_quiet_follow_up``)
        # Set by the app for each place a turn can be typed from far away (``_REMOTE_SOURCES``): who asks the user
        # there. A remote turn with none is never asked out loud at the PC: its risky calls are declined.
        self.remote_confirmers: dict[str, Confirmer] = {}
        # Told each line of the conversation as it's recorded: (role, text, source). The chat window follows them.
        self.line_listeners: list[Callable[[str, str, str], None]] = []
        # Set by the app with the Discord bridge: every announcement is handed to it too, with its cue, to text the
        # user when they're away (``discord_bridge.notify``).
        self.relay: Callable[[str, str | None], None] | None = None
        # Set by the app: the one judgment every line she says on her own passes first (``core/unprompted.py``):
        # wave-offs, what she already said, staleness. None: everything is said as it comes.
        self.unprompted: Gate | None = None
        self._gating: set[asyncio.Task[None]] = set()  # lines the gate is still judging
        # Told "shut up": until when her own lines wait (``_hush``), the timer that ends it, and the lines waiting.
        self._hushed_until: float | None = None
        self._hush_timer: asyncio.TimerHandle | None = None
        self._hushed_lines: list[tuple[Line, Callable[[], None], Callable[[str], None]]] = []
        # Windows where the user said she may submit without asking, for this conversation: {(session, place)}.
        self._standing: set[tuple[str | None, str]] = set()
        # The conversation where the user said so before she had anything to submit: it goes to the first window
        # she submits in.
        self._waived: str | None = None
        # The conversation where the user asked her to work quietly ("just work, talk less"): what she says between
        # her steps isn't spoken there, only what she says once she's done (see ``_quiet``).
        self._quiet_session: str | None = None
        self._away = AwayFromScreen()  # they said they're away from the screen ("I'm in the shower") until back
        self._turn_studying = False  # this turn is theirs while studying at the screen (a panel goes with it)
        self._panel_owed = False  # the last such turn explained or asked with nothing on a panel (``panel_owed``)
        self._offers: list[Offer] = []  # what announcements offered to do since the last turn, for the next one
        self._turn_offers: list[Offer] = []  # ...taken up by the running turn (see ``offered``)
        # What she said or showed the user since they last spoke (replies, announcements), oldest first: what their
        # next "yes" answers (``offered``). Moved to ``_answering`` when they speak, and to the turn that hears it.
        # Each with when, and whether it can be trusted as hers: not a reply of a turn that took in outside content
        # (a page can steer what she offers) or of one that wasn't the user's.
        self._put_to_user: list[tuple[float, str, bool]] = []
        self._answering: tuple[str, ...] = ()
        self._reply_trusted = True  # the reply being recorded was of the user's own turn, with nothing from outside
        self._turn_asked: tuple[str, ...] = ()
        # What carrying out the calls approved in this turn may also take (``asked.steps_after``, ``already_approved``).
        self._turn_steps: list[Callable[[str, Mapping[str, object]], bool]] = []
        self._tools_failed = (
            0  # tool calls that have failed so far: what she writes after one is always said (``said_once``)
        )
        self._turn_used_builtin = False  # the running turn used a tool the brain runs itself (see ``requested``)
        self._turn_read_outside = False  # ...or one of ours that read others' words (``READS_OUTSIDE``)
        self._turn_id = 0  # counts the user's turns, so tools can tell one task from the next (``turn_info``)
        # (turn, time.monotonic()) the user was last heard talking while she worked silently (``heard_talking``).
        self._heard_talking: tuple[int, float] | None = None
        self._turn_approved: set[str] = set()  # kinds of call the user said yes to in this turn (``already_approved``)
        # Terminals the user said yes to working in this turn: their everyday input stands across other tools.
        self._turn_terminals: set[str] = set()
        # The conversation where the user said yes to her looking at the screen (``_looks_ok``).
        self._looks_ok: str | None = None
        # One question at a time: calls made together wait for the one being asked, then take its answer
        # (``confirm``), so the same question is never stacked or repeated.
        self._confirm_lock = asyncio.Lock()
        self._turn_declined: set[tuple[str, str]] = set()  # (scope, description) the user said no to in this turn
        self._wake_blocked_until = 0.0
        self._asking = False  # a confirmation question is playing: talking over it answers it
        self._question_cut = False  # the user's answer stopped the question before it was finished
        self._heard_answers: list[str] = []  # what the user said to the last question, each try's words
        # A screen task's question still waiting for its answer while the user, having woken her over it, talks.
        self._open_question: WorkQuestion | None = None
        # Screen tasks' questions nobody answered yet, held open (``_hold_open``): a later yes or no answers them.
        self._held_questions: list[WorkQuestion] = []
        self._unanswered_why = "nothing was heard"  # why the last question got no answer (``NotAnswered``)
        self._mic_zeros = 0  # the mic's frames in a row that were exact zeros (``mic_silent_s``)
        self._worked_last: str | None = None  # the conversation whose last brain reply took a step (a tool call)
        self._frame_errors = 0
        # On a voice call from the user's phone (``bridge/discord_voice.py``): her frames come from the call, the
        # phone cancels its own echo (so she's heard as on headphones), and speech starts a request by itself.
        self.on_call = False
        self._call_run_ms = 0.0
        # The call apps on the microphone now ("Discord"), from ``addressee.situation.AudioWatch``; None: not known.
        self.other_call: Callable[[], list[str]] | None = None
        # The apps that played sound in the last few seconds ("Vivaldi"), from the same watch; None: not known.
        self.sound_playing: Callable[[], list[str]] | None = None
        # A bang in the room makes the avatar jump: listened for (the loudness only, on this PC) while waiting for the
        # wake word, when there's an avatar to jump.
        avatar = config.avatar
        self._bangs = BangDetector(FRAME_MS / 1000) if avatar.enabled and avatar.startle else None
        self._running = False
        # A second listen for the wake word, when the detector misses it (``wake/second_chance.py``); the request said
        # after the name in the same breath, already transcribed, waits here for ``_transcribe`` (event, text, latency).
        w = config.wake
        self._second_chance = (
            SecondChance(
                config, events, vad, stt, self._second_chance_wake, lambda: self._frames_heard,
                correct=lambda text: self.vocabulary.correct(text) if self.vocabulary is not None else text,
            )
            if w.second_chance and w.engine != "keyboard" and vad is not None and stt is not None
            else None
        )  # fmt: skip
        self._heard_already: tuple[EndpointEvent, str, float] | None = None
        events.subscribe(self._on_event, SpeakingStarted, SpeakingFinished)
        # The avatar presses her camera's shutter for a screenshot: then its sound waits for that beat (``camera``).
        self.camera: Callable[[], bool] | None = None
        self._shutter_due: asyncio.TimerHandle | None = None
        events.subscribe(self._on_screenshot, ScreenshotTaken)
        events.subscribe(self._on_shutter, CameraShutter)
        events.subscribe(self._on_tool_started, ToolCallStarted)
        events.subscribe(self._on_tool_finished, ToolCallFinished)

    # ------------------------------------------------------------------ state
    @property
    def voice_enabled(self) -> bool:
        return self.source is not None

    @property
    def second_chance(self) -> SecondChance | None:
        """The second listen for the wake word, when it's on (the addressee check takes what it overhears)."""
        return self._second_chance

    @property
    def _headphones(self) -> bool:
        """She can't hear herself: headphones at the PC, or a call (the phone cancels its own echo)."""
        return self.config.audio.headphones or self.on_call

    def _other_call(self) -> list[str]:
        """The apps the user is on a call in, other than with her (``other_call``); [] when none or not known."""
        if self.other_call is None or self.on_call or not self.config.conversation.name_on_calls:
            return []
        try:
            return list(self.other_call())
        except Exception:
            log.warning("Asking which call apps are on the microphone failed", exc_info=True)
            return []

    def _playing_elsewhere(self) -> list[str]:
        """The other apps that just played sound (a video, a song, a game: ``sound_playing``), whose speech the mic
        can pick up even through headphones; [] when none, not known, or ``conversation.name_while_playing`` is off."""
        if self.sound_playing is None or self.on_call or not self.config.conversation.name_while_playing:
            return []
        try:
            return list(self.sound_playing())
        except Exception:
            log.warning("Asking which apps are playing sound failed", exc_info=True)
            return []

    def _follow_up_window(self) -> float:
        """How long to listen for a follow-up without her name: none while the user is on a call in another app, or
        while another app plays sound (a video talking in the follow-up window was answered as the user, again and
        again), unless what she just said asked them something ("What did you want me to save?"): the answer is for
        her.

        None while a turn typed at the PC waits in line (an answer clicked on her panel as she asked): that's the
        user's answer already, and it would otherwise wait out the whole window unheard."""
        if self._typed_waiting():
            log.info("A turn typed at the PC is waiting: no follow-up listen")
            return 0.0
        if apps := self._other_call():
            if not self._just_asked():
                log.info("On a call in %s: no follow-up without my name", " and ".join(apps))
                return 0.0
            log.info("On a call in %s, but I just asked something: listening for the answer", " and ".join(apps))
        elif apps := self._playing_elsewhere():
            if not self._just_asked():
                log.info("%s is playing sound: no follow-up without my name", " and ".join(apps))
                return 0.0
            log.info("%s is playing sound, but I just asked something: listening for the answer", " and ".join(apps))
        return self.config.conversation.follow_up_window_s

    def _just_asked(self) -> bool:
        """Whether the last thing she said or showed the user, just now, put something to them (a question, an
        offer). Only the last thing: a question followed by more talk of hers has been moved past."""
        if not self._put_to_user:
            return False
        at, text, _ = self._put_to_user[-1]
        return time.monotonic() - at <= _JUST_ASKED_S and bool(questions_in([text]))

    def start_call(self) -> None:
        """The user called (``bridge/discord_voice.py``): from now on her ears are the call's."""
        self.on_call = True
        self._call_run_ms = 0.0
        if self.vad is not None and self._mode is ListenMode.WAKE:
            self.vad.reset()

    def end_call(self) -> None:
        """The call ended: back to the microphone and the wake word. What she was saying into it stops."""
        self.on_call = False
        if self.wake is not None:
            self.wake.reset()
        self._silence()

    @property
    def speech_enabled(self) -> bool:
        return self.speech is not None and self.speech.tts is not None

    def _set_state(self, new: AssistantState) -> None:
        old = self.state
        if new is old:
            return
        self.state = new
        self.events.emit(StateChanged(old=old, new=new, remote=self._remote_busy))

    async def _still_working(self) -> None:
        """Say "Still on it." when a turn has gone ``STILL_WORKING_S`` without anything audible (her words, a
        question), at most ``STILL_WORKING_TIMES`` times: on 2026-11-04 two turns went over two minutes in silence
        while she wrote code into a command, and the user typed "continue" again. Not while she asks or listens for
        an answer, and only with a cached phrase, so it plays at once beside whatever comes next."""
        said = 0
        self._audible_at = time.monotonic()
        while said < STILL_WORKING_TIMES:
            await asyncio.sleep(STILL_WORKING_CHECK_S)
            if not self._turn_active or self.speech is None or self.acknowledger is None:
                return
            if self.config.conversation.acknowledge != "speech" or not self.speech_enabled:
                return
            if (
                self._speaking_since is not None
                or self.state is AssistantState.CONFIRMING
                or getattr(self, "_asking", False)
            ):
                continue
            if time.monotonic() - self._audible_at < STILL_WORKING_S:
                continue
            choice = self.acknowledger.still_working()
            if choice is None:
                return
            self.speech.play_phrase(*choice)
            self._aside("assistant", choice[0])
            self._audible_at = time.monotonic()
            said += 1

    def _on_event(self, event: Event) -> None:
        if isinstance(event, (SpeakingStarted, SpeakingFinished)):
            self._audible_at = time.monotonic()  # the last time she was heard, for ``_still_working``
        if isinstance(event, SpeakingStarted):
            self._speaking_since = time.monotonic()
            if self._turn_active and self._turn_first_audio is None:
                self._turn_first_audio = time.perf_counter()
        elif isinstance(event, SpeakingFinished) and self._speaking_since is not None:
            self._played_s += time.monotonic() - self._speaking_since
            self._speaking_since = None
        if not self._turn_active:
            return
        if isinstance(event, SpeakingStarted) and self.state is AssistantState.THINKING:
            self._set_state(AssistantState.SPEAKING)
        elif isinstance(event, SpeakingFinished) and self.state is AssistantState.SPEAKING:
            self._set_state(AssistantState.THINKING)

    # ------------------------------------------------------------- audio loop

    # ------------------------------------------------------- the second listen

    # ------------------------------------------------------------- a spoken stop

    # ----------------------------------------------------------- hotkey hooks

    # ------------------------------------------------------ listening helpers

    # ------------------------------------------------------------ announcements

    def _prewarm_tts(self) -> None:
        """Open the TTS connection while the user is still talking (hides the handshake)."""
        if self.speech_enabled and self.speech is not None and self.speech.tts is not None:
            try:
                self.speech.tts.prewarm()
            except Exception:
                log.debug("TTS prewarm failed", exc_info=True)

    def _acknowledge(self, text: str) -> str | None:
        """Fill the silence while the brain thinks. Returns the phrase spoken, if any."""
        mode = self.config.conversation.acknowledge
        if mode == "none" or self.speech is None:
            return None
        if mode == "speech" and self.acknowledger is not None and self.speech_enabled:
            choice = self.acknowledger.choose(text)
            if choice is not None:
                phrase, audio = choice
                self.speech.play_phrase(phrase, audio)
                return phrase
            if self.acknowledger.ready:
                return None  # phrases are ready but this utterance doesn't call for one (small talk)
        from octavia.core.acknowledge import classify

        if classify(text) is not None:
            self._earcon("thinking")  # chime mode, or no voiced phrases ready yet
        return None

    def _say_searching(self, call: ToolUseStarted) -> bool:
        """Say "Looking it up online." when she starts a web search before any word of her answer: the search
        alone keeps the user waiting many seconds, which the acknowledgment ("Let me check.") doesn't cover. A cached
        phrase, so it plays at once, beside the reply rather than in it. Returns whether it was said."""
        if (
            not call.builtin
            or call.name not in READ_ONLY_BUILTINS
            or self.config.conversation.acknowledge != "speech"
            or self.acknowledger is None
            or self.speech is None
        ):
            return False
        choice = self.acknowledger.searching()
        if choice is None:
            return False
        self.speech.play_phrase(*choice)
        self._aside("assistant", choice[0])
        return True

    def _acknowledge_early(self, text: str, pause_ms: float) -> None:
        """Say "On it!" in the user's pause, once what they've said sounds finished and is for the brain, rather than
        after the rest of the end-of-speech silence, and start the brain on it then too (``_start_early``). With
        headphones only: on speakers the mic would hear it as them going on talking. Nothing else happens early: the
        request is acted on once the silence is over (with whatever they added in it), so a pause is never taken for
        the end.

        The brain starts early whether or not a phrase fits what they said: "Yes.", "Zero." or "Okay" gets no "On
        it!", and those are the answers that wait longest (a short utterance's silence is 0.4 s longer), 64 of 142
        of them without a head start in three weeks (2026-11-22)."""
        if not self._early_fits(text):
            return
        acknowledging = self.config.conversation.acknowledge == "speech" and self.acknowledger is not None
        choice = self.acknowledger.choose(text) if acknowledging and self.acknowledger is not None else None
        if self._open_early(text, choice, pause_ms):
            return  # her opening words may take its place (``core/opening.py``)
        if choice is not None:
            assert self.speech is not None
            self.speech.play_phrase(*choice)
            self._early_said = (choice[0], pause_ms)
        elif self._question_waiting() is not None:
            return  # a bare yes or no may be the answer to a screen task's question, which the brain never hears
        self._start_early(text, choice[0] if choice is not None else None)

    def _early_fits(self, text: str) -> bool:
        """Whether what the user has said so far, in a pause, may be acknowledged and started on early."""
        return bool(
            self._headphones
            and self.speech_enabled
            and self.speech is not None
            and self._for_the_brain(text)
            and not self._only_name_tail(text)  # not a request: no "On it!" for it
        )

    def _start_early_again(self, text: str) -> None:
        """A later pause of the same request that sounds finished, after "On it!" was said in an earlier one and the
        user went on talking: the brain starts on what they've said by now. The acknowledgment isn't said twice, and
        it's the one the turn will carry, so the start is told the same. (28 of 339 spoken turns in ten days lost
        their head start this way: started, dropped as they went on, and never started again; 2026-11-22.)"""
        said = self._early_said
        if said is None or self._early_turn is not None or not self._early_fits(text):
            return
        asked = self._opening
        if asked is not None and asked.stream is not None:
            return  # her opening words are being said: their start is theirs (``core/opening.py``)
        self._start_early(text, said[0])

    def _only_name_tail(self, text: str) -> bool:
        """Whether ``text``, heard in the capture the wake detector started, is only the end of her name.

        The detector fires a moment after the name and the capture is seeded with what it reckons came after it
        (``trailing_s``). When the name was drawn out, or the detector's timing was off, that seed holds the last
        syllables, and speech recognition turns them into words ("Octavia" -> "save you"), which went to the brain as
        the request while the user was still about to ask. Told apart by when it was said: it began before the
        detector fired (in the seed: a short word said after a pause starts after it), with hardly any speech since,
        and it's a word or two. Anything she acts on by itself (a stop, "never mind", a preset reply, a quick command)
        is still taken as said: "Octavia, stop" can be that quick.
        """
        if not self._name_tail_possible or self._live_speech_ms > _NAME_TAIL_LIVE_MS:
            return False
        if self._seed_speech_ms < self.config.vad.min_speech_ms:
            return False
        if len(text.split()) > _NAME_TAIL_MAX_WORDS:
            return False
        core = self._addressed(text)
        if stop_kind(core) is not None or normalize_text(text) in _DISMISSALS or core in _DISMISSALS:
            return False
        if self.quick_replies is not None and self.quick_replies.match(text) is not None:
            return False
        if asks_to_say_again(core) or says_yes(core) or normalize_text(core) in {"no", "nope", "nah"}:
            return False
        return not (self.fast_path is not None and self.fast_path.match(text) is not None)

    def _for_the_brain(self, text: str) -> bool:
        """Whether ``text`` would go to the brain: not a stop or dismissal, a preset reply, a quick command or small
        talk for the quick voice (those answer at once, with no acknowledgment)."""
        core = self._addressed(text)
        if stop_kind(core) is not None or normalize_text(text) in _DISMISSALS or core in _DISMISSALS:
            return False
        if self.quick_replies is not None and self.quick_replies.match(text) is not None:
            return False
        if asks_to_say_again(core) and self._to_say_again() is not None:
            return False
        if (
            self.fast_path is not None
            and self.config.routing.fast_path not in ("off", "shadow")
            and self.fast_path.match(text) is not None
        ):
            return False
        return not self._small_talk_fits(text, "voice")

    def _on_screenshot(self, _: ScreenshotTaken) -> None:
        """The shutter sound: now, or on the avatar's flash when she's taking the picture with her camera. Checked a
        moment later, once the avatar's bridge has heard of the screenshot too."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._earcon("shutter")
            return
        loop.call_soon(self._shutter_or_wait, loop)

    def _shutter_or_wait(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._shutter_due is not None:
            self._shutter_due.cancel()
            self._shutter_due = None
        camera = self.camera
        try:
            waits = camera is not None and camera()
        except Exception:
            log.debug("Couldn't tell whether the avatar takes the picture", exc_info=True)
            waits = False
        if not waits:
            self._earcon("shutter")
            return
        from octavia.avatar.bridge import SNAPSHOT_WAIT_S

        self._shutter_due = loop.call_later(SNAPSHOT_WAIT_S, self._on_shutter, None)  # her flash never came

    def _on_shutter(self, _: CameraShutter | None) -> None:
        if self._shutter_due is None:
            return  # a flash with no screenshot waiting on it (already played)
        self._shutter_due.cancel()
        self._shutter_due = None
        self._earcon("shutter")

    def _earcon(self, name: str) -> None:
        if self.earcons is not None and self.speech is not None and self.config.audio.earcons:
            sound = self.earcons.get(name)
            if sound is not None:
                self.speech.play(sound)

    def cue(self, name: str) -> None:
        """One of her sounds, from outside a turn (the mute hotkey); follows ``audio.earcons`` like the rest."""
        self._earcon(name)

    def chime(self, name: str) -> bool:
        """Play a sound the user asked for, such as a timer ringing (``ctx.extras["chime"]``).

        Unlike the earcons it plays even with them turned off: it's what the user wanted to hear. False when there's
        no such sound or nothing to play it on.
        """
        sound = self.earcons.get(name) if self.earcons is not None else None
        if sound is None or self.speech is None:
            return False
        self.speech.play(sound)
        return True

    # -------------------------------------------------------------- main loop
    async def run(self) -> None:
        """Run the voice loop until cancelled."""
        if self.source is None or self.wake is None or self.vad is None or self.stt is None:
            raise RuntimeError("Voice mode needs an audio source, wake detector, VAD and STT")
        self._running = True
        audio_task = asyncio.create_task(self._audio_loop(), name="audio-loop")
        convo_task = asyncio.create_task(self._conversation_loop(), name="conversation-loop")
        try:
            done, _ = await asyncio.wait({audio_task, convo_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raise failures
        finally:
            self._running = False
            hearing = list(self._hearing)
            for task in (audio_task, convo_task, *hearing):
                task.cancel()
            await asyncio.gather(audio_task, convo_task, *hearing, return_exceptions=True)
            self._set_state(AssistantState.STOPPED)

    async def _conversation_loop(self) -> None:
        while True:
            try:
                detection = await self._wait_for_wake()
                if detection is None:
                    item = self._announcements.get_nowait()
                    if isinstance(item, BackgroundTurn):
                        await self._run_background(item)
                    elif isinstance(item, WorkQuestion):
                        await self._ask_for_work(item)
                    else:
                        await self._deliver_announcement(item)
                    continue
                self.events.emit(
                    WakeDetected(phrase=detection.phrase, score=detection.score, second_chance=detection.second_chance)
                )
                self._prewarm_tts()
                # A chime when there's nothing to wait for; on a call, talking is what woke her, so it would cut in.
                if self.config.conversation.wake_ack == "chime" and not detection.heard and not self.on_call:
                    self._earcon("wake")
                await self._converse()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Conversation loop error")
                self.events.emit(ErrorOccurred(source="APP", message=f"{type(exc).__name__}: {exc}"))
                await asyncio.sleep(1.0)
            finally:
                self._wake_blocked_until = time.monotonic() + self.config.wake.cooldown_s

    async def _converse(self, follow_up: bool = False) -> None:
        """One conversation: utterances and replies until it goes quiet.

        The first capture was already started by the audio loop at the wake frame
        (seeded with anything said in the same breath), or by a barge-in. With
        ``follow_up`` (after an announcement) it starts with a follow-up window instead.
        """
        try:
            await self._conversing(follow_up)
        finally:
            self._settle_work_hold("")  # a screen task held for an answer that never came (it went quiet, a stop)

    async def _conversing(self, follow_up: bool) -> None:
        if follow_up:
            timeout, pre_roll = self._follow_up_window(), True
            if timeout <= 0:
                return
        else:
            timeout = self.config.vad.no_speech_timeout_s
            pre_roll = False  # right after the wake phrase: don't include it
        seed: list[np.ndarray] | None = None
        resumed = False  # listening on in the same window, after a sound that had no words in it
        while True:
            self._set_state(AssistantState.LISTENING)
            if not resumed:
                self.events.emit(ListeningStarted(follow_up=follow_up))
                window_ends = time.monotonic() + timeout
            resumed = False
            self._tone_wanted = True  # a request of theirs: its tone is read while they pause (``_read_tone_early``)
            self._follow_up_open = follow_up
            try:
                event = await self._capture(timeout, pre_roll=pre_roll, seed=seed)
            finally:
                self._tone_wanted = False
                self._follow_up_open = False
            seed = None
            if event.kind is EndpointKind.NO_SPEECH:
                if not self._stop_requested:
                    self.events.emit(NoSpeech(follow_up=follow_up))
                self._stop_requested = False
                return
            await self._settle_early_open()  # opening words waited for in the pause: said, or the "On it!" was
            early_said, self._early_said = self._early_said, None
            tone = self._take_tone(event)
            text = await self._transcribe(event)
            words = len(text.split())  # of the audio the tone was read from (not what _hear_the_rest adds)
            name_tail = bool(text) and self._only_name_tail(text)
            if not text or name_tail:
                self._drop_early("it wasn't for the brain after all")
                # No words: the end of her name the wake detector fired on, a cough, a click. Or words that are only
                # that end of her name ("save you"). That isn't the user done talking, so the window they were given
                # goes on (the rest of it) instead of closing on it.
                left = window_ends - time.monotonic()
                if self._stop_requested or left < _RELISTEN_MIN_S:
                    return
                if name_tail:
                    log.info("Heard only the end of my name (%r); still listening (%.1f s left)", text, left)
                else:
                    log.info("Heard a sound with no words in it; still listening (%.1f s left)", left)
                since = min(len(self._recent), self._frames_heard - self._capture_ended_at)
                seed = list(self._recent)[-since:] if since > 0 else []  # what came in while it was transcribed
                timeout, pre_roll, resumed = left, False, True
                continue
            text = await self._hear_the_rest(text)
            if not text:
                self._drop_early("it wasn't for the brain after all")
                return
            if tone is not None:
                tone.heard_at, tone.words = time.perf_counter(), words
            core = self._addressed(text)
            self._heard_from_user(core)
            kind = stop_kind(core)
            # A stop while a screen task works halts it ("stop everything": and clears its queue; "hold on": pauses
            # it), however it was put ("Octavia, stop"). Without one, "hold on" or "wait" goes to the brain as before.
            stopped = self._halt_work(kind, core) if kind is not None and self._work_running() else None
            if stopped is not None or normalize_text(text) in _DISMISSALS or core in _DISMISSALS:
                self._drop_early("it wasn't for the brain after all")
                self._learn_tone(tone)
                self.events.emit(Dismissed(text=text))
                self.events.emit(Notice(source="VOICE", message="Okay, standing by"))
                # The interruption is moot now; what was announced (e.g. a build is ready) still matters.
                self._pending_notes = [n for n in self._pending_notes if not n.startswith(_INTERRUPTED_NOTE)]
                if stopped is not None:
                    await self._say_stopped(stopped)
                return
            if (answered := self._answer_open_question(core or text)) is not None:
                self._drop_early("it wasn't for the brain after all")
                self._learn_tone(tone)
                await self._say_stopped(answered)
                return
            preset = self.quick_replies.match(text) if self.quick_replies is not None else None
            if preset is not None:
                self._drop_early("it wasn't for the brain after all")
                self._learn_tone(tone)
                await self._say_preset(preset)
                if preset.then == "end":
                    return
            elif (again := await self._say_again(text, core, "voice")) is not None:
                # "Say that again": her last reply, word for word, with no model (a screen task carries on meanwhile).
                self._drop_early("it wasn't for the brain after all")
                self._learn_tone(tone)
                result = again
                if result.barge_in == "stop":
                    return
                if result.barge_in:
                    follow_up = False
                    continue
            else:
                # Anything else said to her while a screen task works (a question, another subject, "keep going")
                # holds it off the screen while she answers, and what they said is judged against it meanwhile:
                # after her answer it carries on, unless they asked to stop, pause or change it (``_hold_work``). A
                # plain yes to something she just asked is its answer, not another subject: the task keeps going.
                just_yes = _just_yes(core or text, self._last_reply())
                if not just_yes:
                    self._hold_work(text)
                handled, failed = await self._fast_command(text)
                if handled:
                    self._drop_early("it wasn't for the brain after all")
                    self._learn_tone(tone)
                    self._settle_work_hold(handled)
                    window = self._follow_up_window()
                    if window <= 0:
                        return
                    await self._let_speech_tail_fade()
                    follow_up, timeout, pre_roll = True, window, True
                    continue
                # Small talk tries her quick voice first; when it passes, nothing was said and the brain answers.
                chat = None if failed else await self._small_talk(text, "voice", tone)
                if chat is not None:
                    self._drop_early("it wasn't for the brain after all")
                    result = chat
                else:
                    # Instantly, while the brain starts working; or already, in the user's pause. Her opening words
                    # (``core/opening.py``) take the acknowledgment's place when they're said.
                    opened: SpeechStream | None = None
                    if early_said is not None:
                        said = early_said[0]
                        if (asked := self._take_opening(text)) is not None:
                            if asked.stream is not None and asked.stream.usable:
                                opened = asked.stream
                            else:
                                self._log_when_done(asked)
                    else:
                        said, opened = await self._open_now(text)
                        if said is None:
                            if opened is not None:
                                opened.cancel()
                            opened = None
                            said = self._acknowledge(text)
                    if not just_yes:
                        self._hold_work(text, paused_too=True)  # "carry on with it" for a paused one
                    await self.sessions.touch()
                    result = await self._run_turn(
                        text, source="voice", acknowledged=said, notes=failed, tone=tone, opened=opened
                    )
                if result.barge_in == "stop":
                    return
                if result.barge_in:
                    # Barge-in already started capturing the new utterance; adopt it. A task held for this answer
                    # stays held for the next one.
                    follow_up = False
                    continue
                self._settle_work_hold(result.text or "")
                if result.error:
                    return
            window = self._follow_up_window()
            if window <= 0:
                return
            await self._let_speech_tail_fade()
            follow_up, timeout, pre_roll = True, window, True

    async def _hear_the_rest(self, text: str) -> str:
        """``text`` with whatever the user adds after it, when it trailed off ("turn off the lights and, um").

        The endpointer closes an utterance on silence alone, so a pause to think was answered as if it were the whole
        request. A transcript that sounds unfinished gets ``_UNFINISHED_WAIT_S`` more of listening; what's said in
        that time (and while it was being transcribed) is joined to it. A complete sentence is answered at once.
        Returns "" when the stop hotkey ends the wait.
        """
        waited = 0
        while waited < _MAX_HOLDS and sounds_unfinished(text):
            waited += 1
            log.info("Sounds unfinished, listening for the rest: %r", text)
            self._set_state(AssistantState.LISTENING)
            since = min(len(self._recent), self._frames_heard - self._capture_ended_at)
            seed = list(self._recent)[-since:] if since > 0 else []
            event = await self._capture(_UNFINISHED_WAIT_S, pre_roll=False, seed=seed)
            if self._stop_requested:
                self._stop_requested = False
                return ""
            if event.kind is not EndpointKind.UTTERANCE:
                break
            more = await self._transcribe(event)
            if not more:
                break
            text = more if only_fillers(text) else f"{text} {more}"
        return text

    async def _let_speech_tail_fade(self) -> None:
        """Don't open the mic for a follow-up while our own last words are still in the air."""
        if self.speech is None:
            return
        latency = float(getattr(self.speech.sink, "latency", 0.0) or 0.0)
        await asyncio.sleep(min(0.5, latency + (0.05 if self.echo_cancellation_active else 0.15)))

    # -------------------------------------------------------------- one turn
    async def handle_text(self, text: str) -> TurnResult:
        """Process typed input (chat mode)."""
        self._heard_from_user(self._addressed(text))
        preset = self.quick_replies.match(text) if self.quick_replies is not None else None
        if preset is not None:
            await self._say_preset(preset)
            return TurnResult(text=preset.text)
        if (again := await self._say_again(text, self._addressed(text), "text")) is not None:
            return again
        handled, failed = await self._fast_command(text)
        if handled is not None:
            return TurnResult(text=handled)
        if not failed and (chat := await self._small_talk(text, "text")) is not None:
            return chat
        await self.sessions.touch()
        return await self._run_turn(text, source="text", notes=failed)

    async def _record(self, role: str, text: str, interrupted: bool = False, source: str | None = None) -> None:
        """A line of the conversation: into the transcript, into what the quick voice is shown (``_said``), and to
        whoever follows the conversation (``line_listeners``)."""
        await self.sessions.record_turn(role, text, interrupted=interrupted)
        if role == "user":  # what she said before is what this answers; nothing she said before that
            self._answering, self._put_to_user = self._still_asked(), []
        if text:
            self._said.append((self.sessions.session_id, role, text))
            self._last_line_at = time.time()
            for listener in list(self.line_listeners):
                try:
                    listener(role, text, source or self._turn_source or "voice")
                except Exception:
                    log.warning("A conversation listener failed", exc_info=True)

    def _aside(self, role: str, text: str) -> None:
        """A line said out loud that isn't one of the conversation's turns (her approval questions and the user's
        answers to them, a quick "Got it.", a line a tool said while it worked, a screen task stopping): to whoever
        follows the conversation (``line_listeners``, source "aside"), though not into the transcript."""
        text = " ".join(str(text).split())
        if not text:
            return
        for listener in list(self.line_listeners):
            try:
                listener(role, text, ASIDE)
            except Exception:
                log.warning("A conversation listener failed", exc_info=True)

    async def _run_turn(
        self,
        text: str,
        source: str,
        acknowledged: str | None = None,
        notes: Sequence[str] = (),
        speak: bool = True,
        tone: ToneJob | None = None,
        opened: SpeechStream | None = None,
    ) -> TurnResult:
        """One turn of the brain's. ``opened``: the speech stream her opening words (``acknowledged``) are being said
        on (``core/opening.py``): her reply carries on from them on it, and the brain is told them."""
        if opened is not None and not (speak and self.speech_enabled and opened.usable):
            opened.cancel()
            opened = None
        notes = [*self._pending_notes, *notes]
        self._pending_notes.clear()
        early = await self._early_notes(text, source, acknowledged, tone, opened=opened is not None)
        if early is not None:
            recalled, heard, stamp = early
        else:
            recalled = await self._recalled(text) if source in _USER_SOURCES else None
            heard = await self._heard_tone(tone) if tone is not None and self.voice_tone is not None else None
            stamp = datetime.now().astimezone()
        quiet, notes = self._turn_notes(text, source, acknowledged, notes, recalled, heard, opened=opened is not None)
        tuning = self._tuning  # what the tuner chose for these very notes (an early start may write it again)
        circled, picture = self._circled(text, source, notes)
        turn = UserTurn(text=text, notes=tuple(notes), source=source, timestamp=stamp, picture=picture)
        if picture is not None and circled is not None and self.circles is not None:
            self.circles.used(circled)
        turn_id = self._turn_id + 1  # this turn's number, from here on (``_turn_id`` itself moves on below)
        started = time.perf_counter()
        first_token: float | None = None
        self._turn_first_audio = None
        await self._record("user", text, source=source)
        if acknowledged and opened is None:
            self._aside("assistant", acknowledged)

        self._set_state(AssistantState.THINKING)
        self.events.emit(BrainStarted(text=text, source=source, turn_id=turn_id, tuning=tuning))
        self._prewarm_tts()  # connect while the brain thinks (no-op if already warm)
        stream: Any = opened or (self.speech.begin() if self.speech_enabled and speak else None)
        if opened is None and stream is not None and (self.vet_claims is not None or self.known_files is not None):
            stream.vet = self._vetted
        self._active_stream = stream
        # Her opening words are playing on it: the brain's words go in through the handoff, which drops a first
        # sentence that says them again (``core/handoff.py``).
        handoff = Handoff(stream, acknowledged) if opened is not None and acknowledged else None
        if handoff is not None:
            stream = handoff
        self._interrupt = asyncio.Event()
        self._barge_kind = None
        self._turn_active = True
        self._enter_barge_mode()
        if self._running_away is not None and self._running_away.stop_wanted and source in _ROUTED_SOURCES:
            self._trigger_barge_in("stop", reason="you texted stop")  # it came while the turn was getting ready

        pieces: list[str] = []
        not_up: list[str] = []  # what she said instead of a claim never borne out
        called: list[str] = []  # her tools this turn, as the brain named them
        steps: list[list[str]] = [[]]  # what she wrote between her steps (tool calls), for ``texted_reply``
        held: list[str] = []  # working quietly: what she's said since her last step, spoken only if it's her last
        # Mid-task, what she's written since her last step, held back while it could still be a note to herself
        # (``is_note_to_self``); None once it's been passed on to her voice.
        # A turn's first words too: "Need show_visual schema." was said as a whole reply before her first step.
        pending: list[str] | None = []
        tool_names = self._tool_names()
        done_event: ResponseDone | None = None
        worked = False  # she took a step (a tool call) in this turn
        after_step = False  # no text yet since her last step
        searched = False  # she said she's searching the web

        watchdog = self._watchdog()
        still = asyncio.create_task(self._still_working()) if stream is not None and not quiet else None
        # A sentence saying she did something waits until a call of hers bears it out; "it's on the panel" until a
        # panel call does (``claim_hold``, ``panel_claims``).
        claim = (
            ClaimHold(
                stream.feed,
                claim_kinds(states=self._turn_studying, actions=self.vet_claims is not None),
                panels_up=self._panels_up,
                earlier=self._earlier_acts,
                flush=stream.flush,
            )
            if stream is not None
            else None
        )

        def on_tool(event: ToolCallFinished) -> None:
            if claim is not None:
                claim.finished(Act(event.name, event.ok, event.data, event.summary))

        stop_hearing_tools = self.events.subscribe(on_tool, ToolCallFinished)

        async def pump() -> None:
            nonlocal done_event, first_token, worked, after_step, searched, pending
            try:
                replies = without_panel_pointers(self.brain.respond(turn))  # no "it's on the panel" lines
                async for ev in without_retelling(replies, lambda: self._tools_failed):
                    watchdog.poke()
                    if isinstance(ev, TextDelta):
                        if first_token is None:
                            first_token = time.perf_counter()
                        if after_step and not quiet:
                            # A long step can outlast the warm TTS connection: reconnect while the sentence is written.
                            after_step = False
                            self._prewarm_tts()
                        pieces.append(ev.text)
                        steps[-1].append(ev.text)
                        self.events.emit(BrainTextDelta(text=ev.text))
                        if quiet:
                            held.append(ev.text)
                        elif stream is not None and pending is not None:
                            pending.append(ev.text)
                            line = "".join(pending)
                            whole = _SENTENCE_END.search(line)
                            if len(line.split()) > NOTE_MAX_WORDS:
                                claim.text(line)  # too long for a note: she's talking to the user
                                pending = None
                            elif not worked and whole and not about_her_tools(line[: whole.end()], tool_names):
                                # A turn's first words, as soon as a sentence is whole: only a line about her
                                # tools waits (to be dropped); after a step, notes wait as before.
                                claim.text(line)
                                pending = None
                        elif claim is not None:
                            claim.text(ev.text)
                    elif isinstance(ev, ToolUseStarted):
                        if first_token is None and not quiet and stream is not None and not searched:
                            searched = self._say_searching(ev)
                        worked = after_step = True
                        called.append(ev.name)
                        if steps[-1]:
                            steps.append([])
                        # A tool the brain runs itself (Claude's WebSearch/WebFetch, whatever brain.claude.builtin_tools
                        # adds) brings back text from outside, which can tell the model to do things: from here on, a
                        # call in this turn may be that text's idea rather than the user's.
                        if ev.builtin:
                            self._turn_used_builtin = True
                        elif ev.name in READS_OUTSIDE:  # mail, posts, the screen: others' words (``READS_OUTSIDE``)
                            self._turn_read_outside = True
                        if quiet:
                            held.clear()  # said on the way to a step: not spoken
                            if claim is not None:
                                claim.step()
                        elif stream is not None:
                            if pending and (line := "".join(pending)).strip():
                                first = not called[:-1]  # before this, her first step: only tool notes are dropped
                                if about_her_tools(line, tool_names) or (not first and is_note_to_self(line)):
                                    log.info("Not saying her note to herself: %r", line.strip())
                                else:
                                    claim.text(line)
                            pending = []
                            claim.step()
                            stream.flush()
                    elif isinstance(ev, ResponseDone):
                        done_event = ev
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Brain failed")
                done_event = ResponseDone(text="".join(pieces), error=f"{type(exc).__name__}: {exc}")
            stop_hearing_tools()
            if stream is not None and claim is not None:
                last = "".join(held or pending or ())
                if pending and not held and about_her_tools(last, tool_names):
                    log.info("Not saying her note to herself at the end: %r", last.strip())
                elif held or pending:
                    claim.text(last)  # what she said once she was done
                interrupted_now = self._interrupt.is_set()
                if not interrupted_now and (added := claim.finish()):
                    log.info("Claims not borne out (dropped: %r): said %r", claim.dropped, added)
                    not_up[:] = [added]
                    for note in claim.notes[:2]:
                        self.note(note)
                if done_event is not None and done_event.error and not pieces:
                    glitch = done_event.spoken_error or "Ugh, my brain just glitched. Try that again in a sec."
                    stream.say(glitch)
                    self._aside("assistant", glitch)
                stream.finish()
                await stream.wait()

        pump_task = asyncio.create_task(pump(), name="turn-pump")
        interrupt_task = asyncio.create_task(self._interrupt.wait(), name="turn-interrupt")
        # Set right before the try whose finally clears it, so it can never outlive the turn. The brain (and with
        # it every tool call of this turn) only starts running at the first await below.
        self._turn_text = text if source in _USER_SOURCES else None
        self._turn_source = source if self._turn_text is not None else None
        # An offer is for the very next turn: the user's answer to it, or nothing (any other turn uses it up). So is
        # what she said to them since they last spoke: their answer is about it.
        self._turn_offers, self._offers = (self._offers if self._turn_text is not None else []), []
        self._turn_asked, self._answering = (self._answering if self._turn_text is not None else ()), ()
        self._turn_steps = []
        if self._turn_text is not None and not self._remote() and asks_to_send(self._turn_text):
            self._waived = self.sessions.session_id
        if self._turn_text is not None and _SCREEN_TALK.search(self._turn_text.lower()) and not asks_to_look(text):
            self._looks_ok = None
        self._turn_used_builtin = False
        self._turn_read_outside = picture is not None  # a picture of the screen can show others' words, as a look can
        self._turn_approved = set()
        self._turn_terminals = set()
        self._turn_declined = set()
        self._turn_id = turn_id
        cut_off = False  # the user cut in before the brain was done
        stuck: str | None = None  # what the watchdog found the turn stuck on, when it called it off
        watchdog.start()
        self.brain.heartbeat = watchdog.poke  # whatever the brain hears of the turn, not only what it passes on
        stuck_task = asyncio.create_task(watchdog.expired(), name="turn-watchdog")
        try:
            await asyncio.wait({pump_task, interrupt_task, stuck_task}, return_when=asyncio.FIRST_COMPLETED)
            if stuck_task.done() and not pump_task.done() and not self._interrupt.is_set():
                stuck = self._stuck_line(watchdog.stuck_on())
                log.warning(
                    "Turn stuck for %.1fs (%s): stopping it", stuck_task.result(), watchdog.stuck_on() or "no tool"
                )
            cut_off = self._interrupt.is_set() and done_event is None
            interrupted = (self._interrupt.is_set() or stuck is not None) and not pump_task.done()
            if interrupted:
                if stream is not None:
                    stream.cancel()
                try:
                    await asyncio.wait_for(self.brain.interrupt(), 3.0)
                except Exception:
                    log.warning("Brain interrupt failed or timed out", exc_info=True)
                try:
                    await asyncio.wait_for(asyncio.shield(pump_task), 3.0)
                except TimeoutError:
                    pump_task.cancel()
                except asyncio.CancelledError:
                    pump_task.cancel()
                    raise  # we are being cancelled (shutdown): don't carry on
                except Exception:
                    log.exception("Turn failed after interruption")
                if stuck is not None:
                    try:  # the interrupt may not have reached whatever it was stuck on: the next turn mustn't be
                        await asyncio.wait_for(self.brain.abandon_turn(), 3.0)
                    except Exception:
                        log.warning("Couldn't give up on the stuck turn in the brain", exc_info=True)
            else:
                await pump_task
        except asyncio.CancelledError:
            pump_task.cancel()
            if stream is not None:
                stream.cancel()
            raise
        finally:
            interrupt_task.cancel()
            stuck_task.cancel()
            if still is not None:
                still.cancel()
            watchdog.stop()
            stop_hearing_tools()
            self.brain.heartbeat = None
            self._turn_active = False
            self._reply_trusted = self._turn_text is not None and not self._turn_used_builtin
            self._turn_text = None
            self._turn_source = None
            self._turn_offers = []
            self._turn_asked = ()
            self._turn_steps = []
            self._turn_used_builtin = False
            self._turn_read_outside = False
            self._turn_approved = set()
            self._turn_declined = set()
            self._active_stream = None
            if self._mode is ListenMode.BARGE:
                self._mode = ListenMode.OFF

        self._report_timing(started, first_token, acknowledged, done_event.head_start_s if done_event else None)
        full_text = done_event.text if done_event and done_event.text else "".join(pieces)
        if source in _ROUTED_SOURCES and pieces:
            # A text message is read all at once: the few words she said on the way to a step ("PT location, check
            # calendar.") are status, not part of the answer.
            full_text = texted_reply(["".join(step) for step in steps])
        if handoff is not None:
            full_text = f"{acknowledged} {handoff.trim(full_text)}".strip()  # her opening words began it
        if full_text:
            full_text = self._vetted(full_text)  # shown and kept as she said it
        if claim is not None and (claim.dropped or not_up):
            full_text = without_dropped(full_text, claim.dropped, not_up[0] if not_up else "")
        error = done_event.error if done_event else None
        outcome = stream.outcome() if stream is not None else None
        # A barge-in that lands after the reply fully finished interrupts nothing.
        interrupted = self._interrupt.is_set() and (outcome.interrupted if outcome else True)
        if interrupted:
            heard = outcome.spoken_text if outcome else ""
            if outcome and outcome.partial:
                heard = (heard + " " + outcome.partial + " [cut off]").strip()
            self._pending_notes.append(
                f'{_INTERRUPTED_NOTE}; the user heard only: "{heard}"'
                if heard
                else f"{_INTERRUPTED_NOTE} before speaking"
            )
            if worked and cut_off:
                self._pending_notes.append(_ON_HOLD_NOTE)
        if stuck is not None:
            error = error or "the turn got stuck and was stopped"
            self.events.emit(ErrorOccurred(source="APP", message=stuck))
            if done_event is None:
                self._pending_notes.append(f'{_STUCK_NOTE}; you told the user: "{stuck}"')
            else:  # her answer was complete; it was saying it that got stuck
                heard = outcome.spoken_text if outcome else ""
                self._pending_notes.append(
                    f'{_STUCK_SPEAKING_NOTE}; the user heard only: "{heard}"; you told them: "{stuck}"'
                )
            if speak and self.speech_enabled and self.speech is not None:
                self._set_state(AssistantState.SPEAKING)  # the thinking sound stops: she isn't thinking any more
                try:
                    await asyncio.wait_for(self.speech.say(stuck), _STUCK_SAY_S)
                except Exception:
                    log.warning("Couldn't say the turn got stuck", exc_info=True)
                    self.speech.stop()
            full_text = f"{full_text}\n\n{stuck}".strip()
        await self._record("assistant", full_text, interrupted=interrupted, source=source)
        shown = source in _SHOWN_HERE and not (speak and stream is not None)
        if shown and full_text and not interrupted and stuck is None and not error:
            self._put(
                full_text, self._reply_trusted
            )  # shown at the PC, not spoken (typed, her chat window): all of it reached them
        if speak and stream is not None:
            # What was said out loud, whole: a reply cut off, stuck or that failed is the brain's to say again.
            complete = not interrupted and stuck is None and error is None and outcome is not None
            self._spoke(outcome.replay_text if complete and not outcome.unspoken else None, self._reply_trusted)
        self._reply_trusted = True
        # What the user says next may be about what she just did ("you're still too quiet"): the brain hears it.
        self._worked_last = self.sessions.session_id if worked else None
        await self.sessions.record_brain_session()
        self.sessions.note_turn(  # big: afresh before the next
            done_event.context_tokens if done_event else None,
            first=done_event.first_context_tokens if done_event else None,
        )
        self.events.emit(
            BrainFinished(
                text=full_text,
                interrupted=interrupted,
                error=error,
                source=source,
                turn_id=turn_id,
                duration_s=time.perf_counter() - started,
                first_word_s=first_token - started if first_token is not None else None,
                usage=done_event.usage if done_event else None,
                model=done_event.model if done_event else None,
                head_start_s=done_event.head_start_s if done_event else None,
                opener=acknowledged if handoff is not None else None,
                handoff=handoff.report() if handoff is not None else None,
            )
        )
        if self._turn_studying and not interrupted and not error and stuck is None:
            if panel_owed(full_text, called):  # a panel call that failed she says so herself
                log.info("A study turn with nothing on a panel: the next one is told to put it up")
                self._panel_owed = True
        if full_text and not error:
            model = done_event.model if done_event else None
            self._answered_by = spoken_model(model) if model else "the brain (it didn't say which model)"

        if error and picture is not None and circled is not None and self.circles is not None:
            self.circles.give_back(circled)  # it never got to her: saying it again still means what they circled
        barge_in = self._barge_kind
        owed = self.known_files.owed() if self.known_files is not None else None
        if owed is not None and not interrupted and not error and stuck is None:
            # A turn of her own, right now and not in line: they're waiting on the very thing it fetches.
            more = await self._run_turn(owed.turn, source="scheduled", notes=[owed.note], speak=speak)
            return TurnResult(
                text=f"{full_text}\n\n{more.text}".strip(),
                interrupted=more.interrupted,
                barge_in=more.barge_in,
                error=more.error,
            )
        return TurnResult(text=full_text, interrupted=interrupted, barge_in=barge_in, error=error)

    def _vetted(self, text: str) -> str:
        """What she's about to say, past what holds her to it."""
        if self.known_files is not None:
            text = self.known_files.vet(text)
        return self.vet_claims(text) if self.vet_claims is not None else text

    def _turn_notes(
        self,
        text: str,
        source: str,
        acknowledged: str | None,
        notes: list[str],
        recalled: str | None,
        heard: str | None,
        keep: bool = True,
        opened: bool = False,
    ) -> tuple[bool, list[str]]:
        """Whether the turn works quietly, and its notes: ``notes`` (pending ones and the caller's) and the rest, in
        the order the brain is shown them. The same words and state give the same notes, so an early start
        (``_start_early``) renders exactly as its turn does."""
        notes = list(notes)
        if self._answered_by and _ASKS_WHO_ANSWERED.search(text):  # "which model answered?", however it's put
            notes.append(f"your previous answer came from {self._answered_by}")
        quiet = self._quiet(text if source in _USER_SOURCES else None, keep=keep)
        if quiet:
            notes.append(_QUIET_NOTE)
        if source == "voice" and self.on_call:
            notes.append(_CALL_NOTE)
        elif source == "voice" and (apps := self._other_call()):
            notes.append(_OTHER_CALL_NOTE.format(apps=" and ".join(apps)))
        away = self._away.hear(text if source in _USER_SOURCES else "", source, keep=keep)
        if course := self._studying_here(source):
            notes.append(studying_note(course, away=away))
        if course and not away and self._panel_owed:
            notes.append(OWED_NOTE)  # their next turn while studying: whatever it's about, put it up first
        if keep:
            self._turn_studying = bool(course) and not away
            if self._turn_studying:
                self._panel_owed = False  # told now (a turn of her own in between doesn't use it up)
        if acknowledged and opened:
            notes.append(_OPENED_NOTE.format(words=acknowledged))
        elif acknowledged:
            notes.append(f'you already said "{acknowledged}" out loud, so skip any acknowledgment and just answer')
        if (
            source in _USER_SOURCES
            and not any(n.startswith(_CARRY_ON_NOTE) for n in notes)
            and not self._work_running()
        ):
            if (paused := self._paused_work()) is not None:
                notes.append(paused_note(paused))
        if recalled:
            notes.append(recalled)
        if heard:
            notes.append(heard)
        if self.unprompted is not None and (waved := self.unprompted.turn_note()):
            notes.append(waved)  # subjects the user waved off: not brought up in a reply either
        self._tuning = ()
        if self.tuner is not None:
            try:
                tuned = self.tuner.for_turn(text, source)
            except Exception:
                log.warning("Couldn't tune the turn", exc_info=True)
            else:
                notes.extend(tuned.notes)
                self._tuning = tuned.tuning
        return quiet, notes

    def _earlier_acts(self) -> list[Act]:
        """Her calls in the few turns before this one (``ClaimWatch.earlier``): a claim about one isn't held."""
        try:
            return list(self.earlier_acts()) if self.earlier_acts is not None else []
        except Exception:
            return []

    def _tool_names(self) -> frozenset[str]:
        """Her tools' names, as she'd write them in a note to herself ("Need show_visual schema.")."""
        try:
            return frozenset(str(spec.name) for spec in getattr(self.brain, "tools", None) or ())
        except Exception:
            return frozenset()

    def _panels_up(self) -> bool:
        try:
            return bool(self.panels_up()) if self.panels_up is not None else False
        except Exception:
            return False

    def _studying_here(self, source: str) -> str:
        """The course the user is studying at the screen, where a panel can help them; "" anywhere else."""
        if self.studying is None or source not in _CIRCLE_SOURCES or (source == "voice" and self.on_call):
            return ""
        try:
            return self.studying()
        except Exception:
            log.warning("Couldn't tell what the user is studying", exc_info=True)
            return ""

    def _circled(self, text: str, source: str, notes: list[str]) -> tuple[Circled | None, TurnPicture | None]:
        """What the user just circled on their screen, if anything, for their turn saying ``text``: the circle, and its
        picture when it may go with their words. Where she has to ask before she looks, ``notes`` gets a note instead,
        and the circle stays for her look to name (``LookAtScreen``). Telling her not to look ends it."""
        if self.circles is None or source not in _CIRCLE_SOURCES or (source == "voice" and self.on_call):
            return None, None  # on a call they're away from the PC: whoever moved the mouse there, it wasn't them
        circled = self.circles.live()
        if circled is None:
            return None, None
        if refuses_look(text):
            self.circles.forget()
            return None, None
        if self.circle_shows is None or self.circle_shows():
            return circled, circled.picture
        notes.append(_CIRCLED_ASK_NOTE)
        return circled, None

    # ----------------------------------------------------------- starting early, in the user's pause
    def _start_early(self, text: str, acknowledged: str | None, opened: bool = False) -> None:
        """Start the brain on what the user has said, in their pause, once it sounds finished and is for the brain
        (right as "On it!" plays early, ``_acknowledge_early``): the model's first second or so then overlaps the rest
        of the end-of-speech silence. Nothing is said or done early: the brain runs none of its tools until the turn
        takes the start up (``BrainProvider.start_early``), and it's dropped if the user goes on talking."""
        if not self.config.conversation.early_start or self.brain is None:
            return
        self._drop_early("a newer pause", opening=False)
        if self._turn_active or self._work_running():
            return  # her turn or a screen task: the brain is busy, or the turn's notes would differ
        if len(self._finished_pauses) > _EARLY_PAUSES:
            return  # they keep talking on past pauses that sound finished: wait for the silence this time
        if self.circles is not None and self.circles.live() is not None:
            return  # a picture of their screen goes only with what they end up saying, and can't be taken back
        ep = self.endpointer
        job = self._tone_job
        tone = job if job is not None and job.sound_frames == ep.sound_frames else None
        entry = _EarlyTurn(text, acknowledged, ep.sound_frames, tone, time.monotonic(), opened=opened)
        self._early_turn = entry
        entry.task = asyncio.get_running_loop().create_task(self._begin_early(entry), name="early-start")
        entry.task.add_done_callback(_log_early_failure)

    async def _begin_early(self, entry: _EarlyTurn) -> tuple[str | None, str | None, datetime]:
        """The notes the turn will have (worked out now, while the silence runs), then the early start itself."""
        # As the turn does first: a conversation gone stale starts afresh now, so the start goes to the new one (and
        # its notes, such as quiet mode, are the new conversation's).
        await self.sessions.touch()
        stamp = datetime.now().astimezone()
        recalled = await self._recalled(entry.text)
        heard = None
        if entry.tone is not None and self.voice_tone is not None:
            entry.tone.heard_at, entry.tone.words = time.perf_counter(), len(entry.text.split())  # as _converse sets
            heard = await self._heard_tone(entry.tone)
        if entry.dropped:
            return recalled, heard, stamp  # the user went on talking, or it wasn't their turn after all
        _, notes = self._turn_notes(
            entry.text,
            "voice",
            entry.acknowledged,
            self._pending_notes,
            recalled,
            heard,
            keep=False,
            opened=entry.opened,
        )
        turn = UserTurn(text=entry.text, notes=tuple(notes), source="voice", timestamp=stamp)
        try:
            await self.brain.start_early(turn)
        except Exception:
            log.warning("Couldn't start the brain early", exc_info=True)
        return recalled, heard, stamp

    def _drop_early(self, why: str, opening: bool = True) -> None:
        """Give up on the early start, if there is one: the brain stops working on it. With ``opening``, the opening
        words asked for (or being said) in the same pause go too (``core/opening.py``)."""
        if opening:
            self._drop_opening(why)
        entry, self._early_turn = self._early_turn, None
        if entry is None:
            return
        entry.dropped = True
        try:
            self.brain.drop_early(why)
        except Exception:
            log.warning("Couldn't drop the early start", exc_info=True)

    async def _early_notes(
        self, text: str, source: str, acknowledged: str | None, tone: ToneJob | None, opened: bool = False
    ) -> tuple[str | None, str | None, datetime] | None:
        """The notes an early start worked out for this very turn (same words, same acknowledgment, same voice
        reading, not stale), so the turn reuses them; None when there's none for it (and any other is dropped)."""
        entry = self._early_turn
        if entry is None:
            return None
        if (
            source != "voice"
            or entry.text != text
            or entry.acknowledged != acknowledged
            or entry.opened != opened
            or entry.tone is not tone
            or time.monotonic() - entry.begun > _EARLY_STALE_S
            or entry.task is None
        ):
            self._drop_early("the turn isn't the one it started on")
            return None
        # The turn takes it from here (the start goes ahead if it hasn't yet): the brain matches it, or drops it.
        self._early_turn = None
        try:
            return await asyncio.wait_for(asyncio.shield(entry.task), _EARLY_WAIT_S)
        except Exception:
            log.warning("The early start's notes didn't come in time", exc_info=True)
            entry.dropped = True  # it mustn't start the brain after the turn has gone ahead without it
            try:
                self.brain.drop_early("its notes didn't come in time")
            except Exception:
                log.warning("Couldn't drop the early start", exc_info=True)
            return None

    def _watchdog(self) -> TurnWatchdog:
        """A watchdog for the turn about to run (``core/watchdog.py``), timing her tools by their own limits."""

        def tool_limit(name: str) -> float | None:
            registry = getattr(getattr(self.brain, "executor", None), "registry", None)
            limit = getattr(registry.get(name), "timeout_s", None) if registry is not None else None
            return float(limit) if isinstance(limit, int | float) else None

        def question_limit() -> float:
            # About the most the executor waits for an answer asked in the room (``timeout_s``); a text to the user's
            # phone may wait minutes, so it gets the backstop.
            if self._remote():
                return QUESTION_BACKSTOP_S
            return self.config.permissions.confirm_timeout_s + _QUESTION_TIME_S * _MAX_QUESTIONS

        return TurnWatchdog(self.events, self.config.conversation.turn_stall_s, tool_limit, question_limit)

    @staticmethod
    def _stuck_line(tool: str | None) -> str:
        """What she says when the watchdog stops a turn: what it was stuck on."""
        if tool is None:
            return (
                "Sorry, I got stuck on that and nothing was happening, so I stopped. Ask me again if you still want it."
            )
        what = tool.removeprefix("mcp__octavia__").replace("_", " ")
        return f"Sorry, I got stuck waiting on {what}, so I stopped. Ask me again if you still want it."

    def _quiet(self, text: str | None, keep: bool = True) -> bool:
        """Whether this turn works quietly: the user asked for it in this conversation ("just work, talk less") and
        hasn't asked to hear her again ("talk me through it"), or ``conversation.narrate_work`` is off. ``keep``
        False only tells: what the words ask for isn't kept (an early start, on words that may not be the turn)."""
        session = self.sessions.session_id
        quiet_session = self._quiet_session
        if text:
            if asks_for_quiet(text):
                quiet_session = session
            elif asks_for_talk(text):
                quiet_session = None
        if keep:
            self._quiet_session = quiet_session
        if not self.config.conversation.narrate_work:
            return not (text and asks_for_talk(text) and not asks_for_quiet(text))
        return quiet_session is not None and quiet_session == session

    async def _recalled(self, text: str) -> str | None:
        """What ``recall`` finds for the user's words, or None. It's a nicety: a slow or broken lookup must neither
        hold up the answer nor fail the turn."""
        if self.recall is None:
            return None
        try:
            return await asyncio.wait_for(self.recall(text), _RECALL_TIMEOUT_S)
        except Exception:
            log.warning("Couldn't look up memories for the turn", exc_info=True)
            return None

    def may_start_afresh(self) -> bool:
        """Nothing waits on the brain's context, so it may start afresh (``SessionManager.may_roll``): no turn is
        running (nor one from far away), no screen task runs beside the conversation, nothing she offered or asked
        waits for its answer, and no request of the user's is on hold from being cut off."""
        return not (
            self._turn_active
            or self._remote_busy
            or self._work_running()
            or self._offers
            or self._still_asked()
            or _ON_HOLD_NOTE in self._pending_notes
        )

    @property
    def started_early(self) -> bool:
        """A brain turn was started in the user's pause and waits for its turn (``_start_early``): it went to the
        brain's current context, so the background check mustn't start that afresh under it (the early start's own
        ``touch`` does, first, when it's due)."""
        return self._early_turn is not None

    # ----------------------------------------------------------- the running turn, for tools
    def turn_info(self) -> TurnInfo | None:
        """The user's turn that's running, as tools see it (``ctx.extras["turn"]``); None between their turns."""
        if self._turn_text is None:
            return None
        return TurnInfo(
            id=self._turn_id,
            interrupted=self._turn_interrupted(),
            outside_content=self._turn_used_builtin or self._turn_read_outside,
            remote=self._remote(),
        )

    def heard_talking(self) -> float | None:
        """When (``time.monotonic()``) the user was last heard talking while she worked silently in the running turn
        (``ctx.extras["heard_talking"]``), moving on with every frame while they go on; None when they haven't, or
        between turns. For a tool that goes on by itself, such as scrolling through a long chat: someone talking to
        her shouldn't have to grab the mouse to stop it."""
        heard = self._heard_talking
        if self._turn_text is None or heard is None or heard[0] != self._turn_id:
            return None
        return heard[1]

    async def say_during_turn(self, text: str) -> bool:
        """Say a line now, inside the running turn, and return once it has been heard (``ctx.extras["speak"]``).

        For a tool that talks while it works: pointing at one thing after another, a line for each. False when it
        couldn't be said: no voice, no user turn running, or the user cut the turn off.
        """
        text = " ".join(str(text).split())
        if not text or self._turn_text is None or self._turn_interrupted():
            return False
        if not self.speech_enabled or self.speech is None:
            return False
        stream = self._active_stream
        if stream is None or not stream.usable:
            return False
        before = len(stream.outcome().spoken)
        stream.say(text)
        self._aside("assistant", text)
        await stream.wait_idle()
        outcome = stream.outcome()
        return outcome.error is None and len(outcome.spoken) > before and not self._turn_interrupted()

    def play_during_turn(self, label: str, audio: np.ndarray, sample_rate: int) -> bool:
        """Queue a sound into the running turn's reply (``ctx.extras["play_in_reply"]``): it plays right after what
        she's said so far and before whatever she says next, so a falcon's call comes between her two sentences
        instead of over them. Returns at once; the reply goes on streaming behind it.

        False when it can't go in: no voice, no user turn running, or the user cut the turn off.
        """
        if self._turn_text is None or self._turn_interrupted():
            return False
        if not self.speech_enabled or self.speech is None:
            return False
        stream = self._active_stream
        if stream is None or not stream.usable:
            return False
        clip = resample_linear(
            np.asarray(audio, dtype=np.float32).reshape(-1), sample_rate, self.speech.sink.sample_rate
        )
        clip = apply_gain(clip, self.speech.volume, self.speech.sink.sample_rate)
        stream.play_clip(f"[{' '.join(str(label).split()) or 'a sound'}]", clip)
        return True

    # ----------------------------------------------------------- confirmation

    def _report_timing(
        self, started: float, first_token: float | None, acknowledged: str | None, head_start: float | None = None
    ) -> None:
        """Where the wait went: shown with --verbose and always written to the log."""
        parts = []
        if head_start is not None:
            parts.append(f"brain started {head_start:.2f}s early")
        if first_token is not None:
            parts.append(f"first word from the brain {first_token - started:.2f}s")
        if self._turn_first_audio is not None:
            parts.append(f"reply audible {self._turn_first_audio - started:.2f}s")
        if not parts:
            return
        message = "Timing: " + ", ".join(parts) + (f' (said "{acknowledged}" at once)' if acknowledged else "")
        log.info(message)
        self.events.emit(Notice(source="BRAIN", message=message, level=logging.DEBUG))

    def _turn_interrupted(self) -> bool:
        return self._interrupt is not None and self._interrupt.is_set()
