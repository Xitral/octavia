"""From the assistant's own events to Futures events: the one place that knows both vocabularies.

The assistant already tells its event bus everything that happens in it (``core/events.py``): turns starting and
finishing, every tool call, approvals given or refused, builds, to-dos changed through her TickTick tools, timers,
new mail, the app in front. ``Normalizer.normalize`` turns each of those into zero or more ``FuturesEvent``s,
deterministically; no model is asked what happened.

It keeps a little state to do that: which tool call is which (a call's start carries its arguments, its end its
result), which turn and conversation is running (so an action belongs to its request), and when her last reply
ended (so "no, that's wrong" right after it counts as a correction of it).

What it never keeps: what a tool returned (only whether it worked and a coarse failure class), window titles,
what she typed for the user, or the body of an email. The words of the user's requests are kept only when
``futures.keep_request_words`` is on, and even then ``privacy.redact`` cuts anything that looks secret.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from octavia.core.acknowledge import classify
from octavia.core.events import (
    AnnouncementStarted,
    AppContextChanged,
    BrainFinished,
    BrainStarted,
    ClaimCorrected,
    Dismissed,
    EmailArrived,
    Event,
    FastChatStarted,
    ImprovementChanged,
    Notice,
    PermissionResolved,
    PresetReplied,
    SaidAgain,
    SessionEnded,
    SessionStarted,
    TaskChanged,
    TasksCleared,
    TimerChanged,
    ToolCallFinished,
    ToolCallStarted,
)
from octavia.core.text import normalize_text
from octavia.futures.events import FuturesEvent
from octavia.futures.offers import classify_answer
from octavia.futures.privacy import redact
from octavia.futures.schema import ref
from octavia.tools.permissions import READS_OUTSIDE

log = logging.getLogger(__name__)

USER_SOURCES = frozenset(
    {"voice", "text", "discord", "window"}
)  # the user's own turns: said, typed, texted, or typed in her window
# Tools whose result is someone else's text (mail, messages, the screen, a task's report): after one, what the brain
# does next may be that text's idea (``remind_me_when`` asks first then). The permissions' own list, so the two agree.
OUTSIDE_TOOLS = READS_OUTSIDE
CORRECTION_WINDOW_S = 90.0  # "no, that's wrong" this soon after her reply is about that reply
FEEDBACK_WINDOW_S = 60.0  # a "thanks" or "never mind" this soon after she spoke up on her own is about that
TEXTED_ANSWER_WINDOW_S = 9 * 60.0  # a texted answer to something she said on her own (within the 10 to answer)
FUTURES_CUE = "futures"  # the announcement cue of what Futures says, so the reaction to it can be told apart

# The start of a correction: "no, the other one", "that's not what I asked", "I said ten minutes".
_CORRECTION = re.compile(
    r"^(?:no+|nope|nah|wrong|incorrect|not that|not what i|thats? not|that is not|that isnt|thats wrong|"
    r"you got it wrong|you misunderstood|i said|i meant|i asked|actually|no no|stop that|that was wrong|"
    r"thats the wrong|wrong one)\b"
)

# How a tool's failure reads, from the start of its (at most 160-character) summary. Only these coarse classes are
# kept, never the text: it can hold labels from the screen or window titles.
_FAILURES: tuple[tuple[str, str], ...] = (
    ("cancelled", "cancelled"),
    ("i can only do that while you're talking", "not_in_turn"),
    ("you took over", "taken_over"),
    ("you interrupted", "interrupted"),
    ("the screen changed", "screen_changed"),
    ("the keyboard focus moved", "focus_moved"),
    ("not installed", "not_installed"),
    ("no app", "not_found"),
    ("no window", "not_found"),
    ("couldn't find", "not_found"),
    ("i couldn't find", "not_found"),
    ("ambiguous", "ambiguous"),
    ("which one", "ambiguous"),
    ("refused", "refused"),
    ("not allowed", "denied"),
)
_TIMED_OUT = re.compile(r"timed out after")
_CRASHED = re.compile(r"^'[\w.]+' failed: ")
_OPENED = re.compile(r"^Opened (.+?)\.?$")
_CLEARED_BEFORE = 366 * 86400.0  # a due date before 1971 is TickTick's way of clearing one
_PATCH = re.compile(r"^#(\d+): Running the final checks")
_PASSED = re.compile(r"(\d+) passed")
_FAILED = re.compile(r"(\d+) failed")
# Why a build failed, from its journal message: the first match wins, so a check that timed out is a timeout and not
# a test failure. Only the class is used for grouping, so a failed build is never counted with an "unknown" cause.
_BUILD_FAILURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"timed out|took longer than"), "timeout"),
    (re.compile(r"interrupted|stopped during the build|didn't finish"), "interrupted"),
    (re.compile(r"ran out of steps"), "out_of_steps"),
    (re.compile(r"spending limit|usage budget|usage limit|billing"), "budget"),
    (re.compile(r"claude api error|claude's servers|isn't signed in|rejected the request|overloaded"), "api_error"),
    (re.compile(r"conflict"), "merge_conflict"),
    (re.compile(r"branch is gone|branch missing"), "branch_missing"),
    (re.compile(r"coding session|went wrong|crashed|^\w+(?:error|exception)\b"), "crash"),
    (re.compile(r"check|test|pytest|ruff|lint|format"), "checks_failed"),
)

# ImprovementChanged status -> the Futures event it means (None: not an event of its own).
_BUILD = {
    "building": "build.started",
    "ready": "build.test_passed",
    "installed": "build.installed",
    "rolled_back": "build.rolled_back",
    "failed": "build.failure",
    "no_change": "build.failure",
    "rejected": "build.failure",
}


def looks_like_correction(text: str) -> bool:
    """Does this utterance start by correcting what was just said or done? Deterministic, and deliberately narrow:
    a new request that happens to start with "no" (rare) is the price of never calling a model for this."""
    return bool(_CORRECTION.match(normalize_text(text).replace("'", "")))


# A plain answer to a question she asked ("Want me to add it?" "No, that's all"): not a correction of anything.
_PLAIN_ANSWER = re.compile(
    r"^(?:no+|nope|nah|not now|no thanks?|no thank you)(?: (?:thanks?|thank you|thats all|thats it|im good|"
    r"im fine|all good|not now|not today|not yet|maybe later|later|its fine|never mind|nevermind))*$"
)
_EXPLICIT_CORRECTION = re.compile(r"\b(?:i said|i meant|i asked|not what|wrong|the other|not that|instead)\b")


def is_plain_answer(text: str) -> bool:
    """ "No", "nope, that's all", "no thanks": an answer, with nothing in it that says she got something wrong."""
    words = normalize_text(text).replace("'", "")
    return bool(_PLAIN_ANSWER.match(words)) and not _EXPLICIT_CORRECTION.search(words)


def failure_class(summary: str) -> str:
    """The coarse kind of failure a tool's summary describes."""
    text = summary.strip().lower()
    if _TIMED_OUT.search(text):
        return "timed_out"
    if _CRASHED.match(summary.strip()):
        return "crashed"
    for start, kind in _FAILURES:
        if start in text[:80]:
            return kind
    return "error"


def due_timestamp(value: str, all_day: bool) -> float | None:
    """A TickTick date string as epoch seconds (for an all-day to-do, the start of its day, like the reminders')."""
    from octavia.tools.extensions._ticktick_reminders import local_due

    moment = local_due(value, all_day)
    return moment.timestamp() if moment is not None else None


@dataclass
class _Call:
    name: str
    args: Mapping[str, Any]
    turn: str | None
    session: str | None
    app: str | None


@dataclass
class _Reply:
    event_id: str
    ts: float
    turn: str
    session: str | None
    asked: bool = False  # it ended with a question: a plain "no" right after answers it


@dataclass
class Normalizer:
    """Turns bus events into Futures events. ``categories`` maps tool names to their ``Tool.category``;
    ``context`` gives the conditions of a moment (hour, weekday...; ``sources.time_context``)."""

    context: Callable[[float], dict[str, Any]]
    categories: Mapping[str, str] = field(default_factory=dict)
    keep_request_words: bool = True
    model: str = ""  # the conversation brain's model, as configured
    boot: int = 0  # this process's start (timer ids restart at 1 in every process)

    session: str | None = None
    app: str | None = None  # the app in front, once the context watch has said
    _turn: str | None = None
    _turn_source: str | None = None
    _calls: dict[str, _Call] = field(default_factory=dict)
    _failed_in_turn: set[str] = field(default_factory=set)
    _last_reply: _Reply | None = None
    _last_futures_said: float | None = None
    outside_turn: str | None = None  # the turn in which a tool brought in someone else's text
    # The words of the request her last reply answered, only to tell a repeat of it (``selfscore.reaction``): kept in
    # memory, never written anywhere.
    _last_request: str = ""
    _tuning: tuple[tuple[str, str], ...] = ()  # what she did differently on the running turn (``futures.tuning``)

    def normalize(self, event: Event) -> list[FuturesEvent]:
        """The Futures events ``event`` means (usually none or one)."""
        handler = _HANDLERS.get(type(event))
        return handler(self, event) if handler is not None else []

    # ------------------------------------------------------------------ helpers
    def _ctx(self, ts: float, *, conversation: bool = False, **extra: Any) -> dict[str, Any]:
        context = dict(self.context(ts))
        if self.app:
            context["app"] = self.app
        if conversation:
            if self.session:
                context["session"] = self.session
            if self._turn:
                context["turn"] = self._turn
        context.update({k: v for k, v in extra.items() if v is not None})
        return context

    def _about_recent_remark(self, ts: float) -> bool:
        return self._last_futures_said is not None and ts - self._last_futures_said <= FEEDBACK_WINDOW_S

    def _about_recent_reply(self, ts: float) -> bool:
        return self._last_reply is not None and ts - self._last_reply.ts <= FEEDBACK_WINDOW_S

    def _reaction(self, ts: float, text: str = "", *, how: str | None = None) -> list[FuturesEvent]:
        """How the user took her last reply, when what they said or did came right after it (``selfscore``): a
        ``user.reaction`` about that reply's turn, or nothing. ``how`` when it's already known (a "say that again")."""
        reply = self._last_reply
        if reply is None or ts - reply.ts > CORRECTION_WINDOW_S:
            return []
        if how is None:
            from octavia.futures.selfscore import reaction

            how = reaction(text, request=self._last_request, asked=reply.asked)
        if how is None:
            return []
        return [
            FuturesEvent(
                type="user.reaction",
                source="assistant",
                ts=ts,
                caused_by=reply.event_id,
                data={"how": how, "about_turn": reply.turn},
                context=self._ctx(ts, conversation=True),
            )
        ]

    # ------------------------------------------------------------------ the conversation
    def _session_started(self, event: SessionStarted) -> list[FuturesEvent]:
        self.session = event.session_id
        return []

    def _session_ended(self, event: SessionEnded) -> list[FuturesEvent]:
        if self.session == event.session_id:
            self.session = None
        return []

    def _brain_started(self, event: BrainStarted) -> list[FuturesEvent]:
        self._turn = f"{self.boot}-{event.turn_id}"
        self._turn_source = event.source
        self._failed_in_turn.clear()
        if event.source not in USER_SOURCES:
            return [
                FuturesEvent(
                    type="routine.started",
                    source="assistant",
                    ts=event.ts,
                    subject="routine:scheduled",
                    context=self._ctx(event.ts, turn=self._turn),
                )
            ]
        out: list[FuturesEvent] = self._reaction(event.ts, event.text)  # before the request settles the reply
        self._last_request = event.text
        self._tuning = tuple(event.tuning)
        words = redact(event.text, 200) if self.keep_request_words else ""
        reply = self._last_reply
        if (
            reply is not None
            and event.ts - reply.ts <= CORRECTION_WINDOW_S
            and looks_like_correction(event.text)
            and not (reply.asked and is_plain_answer(event.text))
        ):
            out.append(
                FuturesEvent(
                    type="user.correction",
                    source="assistant",
                    ts=event.ts,
                    caused_by=reply.event_id,
                    data={"text": words, "about_turn": reply.turn},
                    context=self._ctx(event.ts, conversation=True),
                )
            )
        out.append(
            FuturesEvent(
                type="user.request",
                source="assistant",
                ts=event.ts,
                data={"text": words, "source": event.source, "kind": classify(event.text) or "other"},
                context=self._ctx(event.ts, conversation=True),
            )
        )
        out.extend(self._answer(event.ts, event.text, texted=event.source == "discord"))
        return out

    def _fast_chat(self, event: FastChatStarted) -> list[FuturesEvent]:
        """Her quick voice took the turn (a short "not now" can be small talk to it): only an answer to what she just
        offered is of interest here; the turn itself is the brain's to record."""
        if event.source not in USER_SOURCES:
            return []
        return [*self._reaction(event.ts, event.text), *self._answer(event.ts, event.text)]

    def _answer(self, ts: float, text: str, *, texted: bool = False) -> list[FuturesEvent]:
        """A short "yes", "not now" or "no" right after she spoke up on her own: how that went
        (``offers.classify_answer``). The service decides whether that remark asked anything. ``texted``: from the
        user's phone, where what she says is relayed, and an answer can take longer than in the room."""
        window = TEXTED_ANSWER_WINDOW_S if texted else FEEDBACK_WINDOW_S
        recent = self._last_futures_said is not None and ts - self._last_futures_said <= window
        answer = classify_answer(text) if recent else None
        if answer is None:
            return []
        verdict = "positive" if answer == "yes" else "negative"
        return [
            FuturesEvent(
                type="user.feedback",
                source="assistant",
                ts=ts,
                data={"verdict": verdict, "how": answer, "about": "recommendation"},
                context=self._ctx(ts, conversation=True),
            )
        ]

    def _brain_finished(self, event: BrainFinished) -> list[FuturesEvent]:
        turn = f"{self.boot}-{event.turn_id}"
        outcome = "interrupted" if event.interrupted else ("failure" if event.error else "success")
        usage = event.usage or {}
        if event.source not in USER_SOURCES:
            return [
                FuturesEvent(
                    type="routine.completed",
                    source="assistant",
                    ts=event.ts,
                    subject="routine:scheduled",
                    outcome=outcome,
                    duration_s=event.duration_s,
                    data={"error": bool(event.error)},
                    context=self._ctx(event.ts, turn=turn),
                )
            ]
        reply = FuturesEvent(
            type="assistant.reply",
            source="assistant",
            ts=event.ts,
            id=uuid.uuid4().hex,
            subject=ref("model", self.model) if self.model else None,
            outcome=outcome,
            duration_s=event.duration_s,
            data={
                "source": event.source,
                "interrupted": event.interrupted,
                "error": (event.error or "")[:80],
                "first_word_s": event.first_word_s,
                "tokens_in": _int(usage.get("input_tokens")),
                "tokens_out": _int(usage.get("output_tokens")),
                "model": self.model,
                "turn": turn,
                "tuning": [list(pair) for pair in self._tuning],
            },
            context=self._ctx(event.ts, conversation=True, turn=turn),
        )
        self._last_reply = _Reply(reply.id, event.ts, turn, self.session, asked=event.text.rstrip().endswith("?"))
        return [reply]

    def _claim_corrected(self, event: ClaimCorrected) -> list[FuturesEvent]:
        turn = f"{self.boot}-{event.turn_id}"
        return [
            FuturesEvent(
                type="assistant.claim_corrected",
                source="assistant",
                ts=event.ts,
                data={"sentence": redact(event.sentence, 200), "turn": turn},
                context=self._ctx(event.ts, turn=turn),
            )
        ]

    def _preset(self, event: PresetReplied) -> list[FuturesEvent]:
        if event.rule != "thanks":
            return []
        return [*self._reaction(event.ts, how="thanked"), *self._feedback(event.ts, "positive", "thanks")]

    def _said_again(self, event: SaidAgain) -> list[FuturesEvent]:
        return self._reaction(event.ts, how="asked_again")

    def _dismissed(self, event: Dismissed) -> list[FuturesEvent]:
        if self._about_recent_remark(event.ts) and classify_answer(event.text or "") == "not_now":
            return self._answer(event.ts, event.text)  # "not now" is a later, not a turn-down
        return self._feedback(event.ts, "negative", "dismissed")

    def _feedback(self, ts: float, verdict: str, how: str) -> list[FuturesEvent]:
        if self._about_recent_remark(ts):
            about = "recommendation"
        elif self._about_recent_reply(ts):
            about = "reply"
        else:
            return []  # a "thanks" out of nowhere says nothing about anything she did
        caused = self._last_reply.event_id if about == "reply" and self._last_reply else None
        return [
            FuturesEvent(
                type="user.feedback",
                source="assistant",
                ts=ts,
                caused_by=caused,
                data={"verdict": verdict, "how": how, "about": about},
                context=self._ctx(ts, conversation=True),
            )
        ]

    def _announcement(self, event: AnnouncementStarted) -> list[FuturesEvent]:
        if event.cue == FUTURES_CUE:
            self._last_futures_said = event.ts
        return []

    # ------------------------------------------------------------------ tools
    def read_outside(self, turn_id: int) -> bool:
        """Did the turn ``turn_id`` run a tool that brought in someone else's text?"""
        return self.outside_turn == f"{self.boot}-{turn_id}"

    def _tool_started(self, event: ToolCallStarted) -> list[FuturesEvent]:
        if event.name in OUTSIDE_TOOLS and self._turn:
            self.outside_turn = self._turn
        self._calls[event.call_id] = _Call(event.name, dict(event.args), self._turn, self.session, self.app)
        if len(self._calls) > 256:  # calls that never finished (a crash mid-call): don't grow forever
            self._calls.pop(next(iter(self._calls)))
        return []

    def _tool_finished(self, event: ToolCallFinished) -> list[FuturesEvent]:
        call = self._calls.pop(event.call_id, None)
        args = call.args if call else {}
        name = event.name
        failure = None if event.ok else failure_class(event.summary)
        retry = name in self._failed_in_turn
        if not event.ok:
            self._failed_in_turn.add(name)
        context = self._ctx(event.ts, conversation=True)
        if call is not None:
            context.update({k: v for k, v in (("turn", call.turn), ("session", call.session)) if v})
            if call.app:
                context["app"] = call.app
        category = self.categories.get(name, "general")
        out = [
            FuturesEvent(
                type="computer.action_performed" if event.ok else "computer.action_failed",
                source="tools",
                ts=event.ts,
                subject=ref("skill", name),
                action=name,
                outcome="success" if event.ok else "failure",
                duration_s=event.duration_s,
                data={
                    "tool": name,
                    "category": category,
                    "failure": failure,
                    "app": context.get("app"),
                    "retry": retry,
                },
                context=context,
            )
        ]
        if not event.ok:
            return out
        if name == "open_application":
            opened = _OPENED.match(event.summary.strip())
            app = opened.group(1) if opened else str(args.get("name", "")).strip()
            if app:
                out.append(self._app_event("computer.application_opened", app, event.ts, context))
        elif name == "close_application" and str(args.get("name", "")).strip():
            out.append(self._app_event("computer.application_closed", str(args["name"]).strip(), event.ts, context))
        elif name == "remember":
            out.append(
                FuturesEvent(
                    type="memory.saved",
                    source="tools",
                    ts=event.ts,
                    data={"category": str(args.get("category", "general"))},
                    context=context,
                )
            )
        elif name == "forget":
            out.append(FuturesEvent(type="memory.forgotten", source="tools", ts=event.ts, context=context))
        return out

    @staticmethod
    def _app_event(kind: str, app: str, ts: float, context: dict[str, Any]) -> FuturesEvent:
        return FuturesEvent(
            type=kind,
            source="tools",
            ts=ts,
            subject=ref("application", app),
            data={"app": app, "by": "assistant"},
            context=context,
        )

    def _permission(self, event: PermissionResolved) -> list[FuturesEvent]:
        if event.approved or event.reason not in ("declined", "timed out"):
            return []
        return [
            FuturesEvent(
                type="user.feedback",
                source="assistant",
                ts=event.ts,
                subject=ref("skill", event.tool),
                data={"verdict": "negative", "how": "declined", "about": "action", "tool": event.tool},
                context=self._ctx(event.ts, conversation=True),
            )
        ]

    # ------------------------------------------------------------------ the rest of her world
    def _task(self, event: TaskChanged) -> list[FuturesEvent]:
        if not event.task_id:
            return []
        kind = {
            "created": "task.created",
            "updated": "task.updated",
            "moved": "task.updated",
            "completed": "task.completed",
            "deleted": "task.deleted",
        }.get(event.action)
        if kind is None:
            return []
        data: dict[str, Any] = {"by": "assistant"}
        if event.title:
            data["title"] = redact(event.title, 120)
        if event.due:
            due = due_timestamp(event.due, event.all_day)
            # TickTick clears a date by setting it to 1970-01-01: that's "no due date", not one long past.
            data["due_ts"] = due if due is not None and due > _CLEARED_BEFORE else None
            data["all_day"] = event.all_day
        if event.project_id:
            data["project_id"] = event.project_id
        if event.priority is not None:
            data["priority"] = event.priority
        if kind == "task.completed":
            data["completed_ts"] = event.ts
        if event.action == "moved":
            data["moved"] = True
        return [
            FuturesEvent(
                type=kind,
                source="ticktick",
                ts=event.ts,
                subject=ref("task", event.task_id),
                data=data,
                context=self._ctx(event.ts, conversation=True),
            )
        ]

    def _tasks_cleared(self, event: TasksCleared) -> list[FuturesEvent]:
        return [FuturesEvent(type="task.cleared", source="ticktick", ts=event.ts, context=self._ctx(event.ts))]

    def _timer(self, event: TimerChanged) -> list[FuturesEvent]:
        kind = {"created": "timer.created", "rang": "timer.completed", "cancelled": "timer.cancelled"}.get(event.action)
        if kind is None:
            return []
        return [
            FuturesEvent(
                type=kind,
                source="timers",
                ts=event.ts,
                subject=ref("timer", f"{self.boot}-{event.timer_id}"),
                data={"label": redact(event.label, 60), "seconds": event.seconds, "alert": event.alert},
                context=self._ctx(event.ts, conversation=kind == "timer.created"),
            )
        ]

    def _email(self, event: EmailArrived) -> list[FuturesEvent]:
        sender = " ".join(event.sender.split())[:80]
        if not sender:
            return []
        return [
            FuturesEvent(
                type="gmail.message_received",
                source="gmail",
                ts=event.ts,
                dedup_key=f"mail:{event.key}",
                subject=ref("email", event.key),
                entities=(ref("person", sender),),
                data={"sender": sender, "unread": event.unread},
                transient={"subject": event.subject},
                context=self._ctx(event.ts),
            )
        ]

    def _context(self, event: AppContextChanged) -> list[FuturesEvent]:
        self.app = event.app
        return [
            FuturesEvent(
                type="computer.context_changed",
                source="computer",
                ts=event.ts,
                subject=ref("application", event.app),
                data={
                    "app": event.app,
                    "process": event.process,
                    "previous": event.previous,
                    "previous_s": round(event.previous_s, 1),
                },
                context=self._ctx(event.ts),
            )
        ]

    def _improvement(self, event: ImprovementChanged) -> list[FuturesEvent]:
        kind = _BUILD.get(event.status)
        if kind is None or (event.status == "rejected" and "dropped" in event.message.lower()):
            return []  # the user dropping it isn't a build failing
        data: dict[str, Any] = {"title": redact(event.title, 120), "status": event.status, "message": event.message}
        if event.status == "failed" and _checks_failed(event.message):
            kind = "build.test_failed"
        if event.status in ("no_change", "rejected"):
            data["reason"] = event.status
        if event.status in ("failed", "rejected"):
            data["failure"] = build_failure(event.status, event.message)
        passed, failed = _PASSED.search(event.message), _FAILED.search(event.message)
        if passed:
            data["tests_passed"] = int(passed.group(1))
        if failed:
            data["tests_failed"] = int(failed.group(1))
        return [
            FuturesEvent(
                type=kind,
                source="evolve",
                ts=event.ts,
                subject=ref("improvement", event.improvement_id),
                data=data,
                context=self._ctx(event.ts),
            )
        ]

    def _notice(self, event: Notice) -> list[FuturesEvent]:
        if event.source != "EVOLVE":
            return []
        found = _PATCH.match(event.message)
        if not found:
            return []
        return [
            FuturesEvent(
                type="build.patch_created",
                source="evolve",
                ts=event.ts,
                subject=ref("improvement", found.group(1)),
                context=self._ctx(event.ts),
            )
        ]


def build_failure(status: str, message: str) -> str:
    """A failed build's cause in a word ("timeout", "crash", "checks_failed"...). One refused by the gate is
    "rejected"; one whose message matches nothing is "gave_up": the builder's own reason why it couldn't."""
    text = " ".join(message.lower().split())
    if status == "rejected":
        return "rejected"
    for pattern, kind in _BUILD_FAILURES:
        if pattern.search(text):
            return kind
    return "gave_up" if text else "no_reason"


def _checks_failed(message: str) -> bool:
    text = message.lower()
    return "check" in text or "test" in text or "pytest" in text or "ruff" in text


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_HANDLERS: dict[type[Event], Callable[[Normalizer, Any], list[FuturesEvent]]] = {
    SessionStarted: Normalizer._session_started,
    SessionEnded: Normalizer._session_ended,
    BrainStarted: Normalizer._brain_started,
    BrainFinished: Normalizer._brain_finished,
    ClaimCorrected: Normalizer._claim_corrected,
    FastChatStarted: Normalizer._fast_chat,
    PresetReplied: Normalizer._preset,
    SaidAgain: Normalizer._said_again,
    Dismissed: Normalizer._dismissed,
    AnnouncementStarted: Normalizer._announcement,
    ToolCallStarted: Normalizer._tool_started,
    ToolCallFinished: Normalizer._tool_finished,
    PermissionResolved: Normalizer._permission,
    TaskChanged: Normalizer._task,
    TasksCleared: Normalizer._tasks_cleared,
    TimerChanged: Normalizer._timer,
    EmailArrived: Normalizer._email,
    AppContextChanged: Normalizer._context,
    ImprovementChanged: Normalizer._improvement,
    Notice: Normalizer._notice,
}

# The bus event types the service subscribes to: exactly the ones above, so a BrainTextDelta never costs a thing.
BUS_TYPES: tuple[type[Event], ...] = tuple(_HANDLERS)
