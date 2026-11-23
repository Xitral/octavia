"""Permission policy for tool calls.

This is the single place where "may the assistant do this?" is decided:

* each tool declares a risk level (and may escalate it per call),
* the policy maps risk -> decision (allow / auto / confirm / deny),
  configurable per risk level and per tool name in ``config.permissions``,
* a ``Confirmer`` asks the human when the decision is CONFIRM. The voice
  loop, the text chat and tests each provide their own confirmer.
* the user's own words can be the yes: when what they just said or typed
  plainly asks for the call ("look at my screen"), the confirmer reports it as
  ``requested`` and they aren't asked again. Only for the few tools listed in
  ``octavia.tools.asked``, and never for a restricted call.
* AUTO is "go ahead while the user is working with the assistant": no question
  inside one of their own turns (``Confirmer.unattended``), unless the turn has
  taken in outside content such as a web page, or text other people wrote that a
  tool read (``READS_OUTSIDE``: mail, chats, Canvas posts, documents, task
  results); otherwise it asks like CONFIRM. After such a read, the user's own
  words can still be the yes (``requested``, her quick voice's judgement): only
  they can ask for a call, and a read can't put words in their mouth.
* a yes to her own offer is the approval: a "yes" means what she asked. When an
  announcement offered one exact call ("Want me to start it again?"), or her own
  question since the user last spoke asks for the call in the words
  ``octavia.tools.asked`` accepts ("Want me to install them?"), and the user's whole
  answer is a plain yes, that call isn't asked again (``Confirmer.offered``). Every
  offer made since the last turn counts, each for one call, and only in the next turn.
* one yes covers what carrying the call out takes: rebuilding an update whose
  approved install found it clashing (``octavia.tools.asked.steps_after``).
* one yes covers calls of the same kind that follow straight on: once the user
  approved a call, the next calls of the same tool, for the same kind of thing
  (``ConfirmationRequest.scope``), in the same turn go ahead until anything else
  runs in between (``Confirmer.already_approved``).
None of these skip the question for a restricted call.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from octavia.tools.base import RiskLevel

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from octavia.config import PermissionsConfig
    from octavia.tools.base import Tool

log = logging.getLogger(__name__)


class Decision(str, Enum):
    ALLOW = "allow"
    # No question while the user is right there, in one of their own turns that took in nothing from outside;
    # otherwise it's asked, like CONFIRM.
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str
    per_tool: bool = False  # from a per-tool rule in config.permissions.tools, not the tool's risk level


class PermissionPolicy:
    def __init__(self, config: PermissionsConfig, defaults: Mapping[str, Decision] | None = None) -> None:
        """``defaults``: per-tool rules that come from another part of the config (``ticktick.changes`` for
        TickTick's tools). They decide the tool's calls that need a yes by their risk, never its safe ones (a read
        through a tool that can also change things just runs); a rule the user wrote in ``permissions.tools`` wins
        over them for every call."""
        self._by_risk = {
            RiskLevel.SAFE: config.safe,
            RiskLevel.CONFIRM: config.confirm,
            RiskLevel.RESTRICTED: config.restricted,
        }
        self._by_tool = dict(config.tools)
        self._defaults = dict(defaults or {})

    def decide(self, tool: Tool, risk: RiskLevel) -> PolicyDecision:
        override = self._by_tool.get(tool.name)
        if override is None and risk is not RiskLevel.SAFE:
            override = self._defaults.get(tool.name)
        if override is not None:
            if override is Decision.ALLOW and risk is RiskLevel.RESTRICTED:
                log.warning("Restricted tool %s is allowed by an explicit per-tool override", tool.name)
            if override is Decision.AUTO and risk is RiskLevel.RESTRICTED:
                # "auto" means going ahead unasked, which a restricted call never does: it's asked every time.
                return PolicyDecision(
                    Decision.CONFIRM, f"per-tool rule for {tool.name} (restricted: always asked)", per_tool=True
                )
            return PolicyDecision(override, f"per-tool rule for {tool.name}", per_tool=True)
        return PolicyDecision(self._by_risk[risk], f"default rule for {risk.value} tools")

    def rule_for(self, name: str) -> tuple[Decision | None, str]:
        """The per-tool rule for ``name`` and where it's from: "permissions.tools" for the user's own, "elsewhere"
        for one that comes from another config section (``defaults``), or (None, "") when the tool has none."""
        if name in self._by_tool:
            return self._by_tool[name], "permissions.tools"
        if name in self._defaults:
            return self._defaults[name], "elsewhere"
        return None, ""

    def without_rule(self, name: str) -> PermissionPolicy:
        """This policy without the user's own per-tool rule for ``name``: what taking that line out of
        ``permissions.tools`` would do (``ToolExecutor.preview``)."""
        other = copy.copy(self)
        other._by_tool = {k: v for k, v in self._by_tool.items() if k != name}
        return other


@dataclass(frozen=True)
class Offer:
    """A call an announcement offered to make ("Want me to start it again?"): the tool, and the arguments the call
    must have (the others may be anything). A plain yes in the next turn approves exactly that call."""

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)

    def covers(self, tool: str, args: Mapping[str, Any]) -> bool:
        return tool == self.tool and all(args.get(k) == v for k, v in self.args.items())


@dataclass
class ConfirmationRequest:
    tool_name: str
    description: str  # human-readable, e.g. "close Spotify"
    risk: RiskLevel
    args: dict[str, Any] = field(default_factory=dict)
    # Tells whether some words of the user's ask for this call (``octavia.tools.asked``); None when no words can.
    # It's how to judge the request, not part of it: left out of comparisons and the repr.
    requested_by: Callable[[str], bool] | None = field(default=None, compare=False, repr=False)
    # What kind of call it is, for "one yes covers the rest of the task": the tool, and what its own look at the
    # world said it would do ("click:send"). Calls with the same scope share a yes within one turn.
    scope: str = ""
    # Tells whether the user's words say yes to an offer of this call (``octavia.tools.asked``); None when the tool
    # can't be offered. Like ``requested_by``, how to judge it, not part of it.
    accepts_offer: Callable[[str], bool] | None = field(default=None, compare=False, repr=False)
    # Where the call would act (``RiskFinding.place``): the window a standing approval to submit there is for.
    place: str = ""
    window: str = ""  # that window's title (``RiskFinding.window``)
    # This call in particular is asked about: the tool's own look at the world found it risky (sending, deleting,
    # typing into a terminal), or the user's own rules say to ask (for the tool, or for tools of a lower risk than its
    # own). False when it's asked only because its tool asks about every call of its kind (``Tool.risk``).
    flagged: bool = False
    # The exact call, only for a screen (``Tool.call_detail``: the command line itself). Never said out loud, where the
    # plain ``description`` is all that's said. "" when the description already says it all.
    detail: str = field(default="", compare=False)


def terminal_of(scope: str) -> str:
    """The terminal a request's ``scope`` stands for ("type_text:terminal:WindowsTerminal.exe#1234" is
    "terminal:WindowsTerminal.exe#1234"), or "" for any other kind of call. A yes to one input there covers that
    terminal's everyday input for the rest of the task or turn (``computer_control.standing_terminal``)."""
    _, _, finding = scope.partition(":")
    return finding if finding.startswith("terminal:") else ""


class NotAnswered(Exception):
    """Raised by ``Confirmer.confirm`` when the question got no answer: nothing was heard (the mic sending silence),
    the reply couldn't be read as a yes or a no, or the place it was asked in closed or gave up waiting. Never a no:
    the call isn't made, and the model is told it wasn't answered, not that it was declined. ``reason`` says why, in
    words for the model ("nothing was heard")."""

    def __init__(self, reason: str = "nobody answered") -> None:
        super().__init__(reason)
        self.reason = reason


class Confirmer(ABC):
    """Asks the human to approve a tool call. Must return False when unsure; raises ``NotAnswered`` when nobody
    answered (silence, an unclear reply), which is never a no."""

    @abstractmethod
    async def confirm(self, request: ConfirmationRequest) -> bool: ...

    def requested(self, request: ConfirmationRequest) -> bool:
        """Whether the user already asked for this call in their own words, which makes asking again pointless.

        The executor checks this before ``confirm`` when words can approve the call (``request.requested_by`` is
        set): True approves it without a question. Only a confirmer that knows what the user just said can tell
        (the assistant, during a turn); by default nothing counts.
        """
        return False

    def unattended(self, request: ConfirmationRequest) -> bool:
        """Whether a call the policy says may go ahead on its own (AUTO) can do so now, without a question.

        True only while the user is working with the assistant: inside one of their own turns that they haven't
        cut off and that hasn't taken in outside content (a web page could try to steer what happens next). Only
        a confirmer that knows about turns can tell; by default it's always asked.
        """
        return False

    def offered(self, request: ConfirmationRequest) -> bool:
        """Whether the user just said yes to an offer of exactly this call (``Offer``), which makes asking again
        pointless. Only a confirmer that knows what was announced and answered can tell; by default nothing counts."""
        return False

    def already_approved(self, request: ConfirmationRequest) -> bool:
        """Whether the user already said yes to this kind of call (``request.scope``) earlier in the same task,
        which makes asking again pointless. By default nothing counts."""
        return False

    def timeout_s(self, request: ConfirmationRequest) -> float | None:
        """How long the executor waits for ``confirm`` before taking it as a no, when this confirmer asks somewhere
        slower than the room (a text to the user's phone waits minutes for its answer). None, the default: the
        executor's own limit (``permissions.confirm_timeout_s``)."""
        return None


class DenyAllConfirmer(Confirmer):
    """Used when nobody is available to ask (background jobs, tests)."""

    async def confirm(self, request: ConfirmationRequest) -> bool:
        return False


class StaticConfirmer(Confirmer):
    """Always answers the same way. Useful for tests."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.requests: list[ConfirmationRequest] = []

    async def confirm(self, request: ConfirmationRequest) -> bool:
        self.requests.append(request)
        return self.answer


class LineReader:
    """The one thread that reads stdin, shared by the chat loop and ConsoleConfirmer.

    A blocking readline in a worker thread can't be cancelled, so if a confirmation
    timed out with its own reader, that orphaned thread would swallow the user's next
    chat message. With a single reader, a cancelled wait just leaves the line queued.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        queue = self._queue

        def pump() -> None:
            while True:
                line = sys.stdin.readline() if sys.stdin is not None else ""
                loop.call_soon_threadsafe(queue.put_nowait, line if line else None)
                if not line:
                    return

        self._thread = threading.Thread(target=pump, name="stdin", daemon=True)
        self._thread.start()

    async def readline(self, prompt: str = "") -> str | None:
        """Next line without its newline, or None at end of input."""
        assert self._queue is not None, "call start() first"
        if prompt:
            sys.stdout.write(prompt)
            sys.stdout.flush()
        line = await self._queue.get()
        return None if line is None else line.rstrip("\r\n")


class ConsoleConfirmer(Confirmer):
    """Asks on the terminal. Used by text chat mode."""

    def __init__(self, reader: LineReader | None = None) -> None:
        self.reader = reader

    async def confirm(self, request: ConfirmationRequest) -> bool:
        shown = f"\n          {request.detail}" if request.detail else ""
        question = f"\n[CONFIRM] Allow: {request.description}?{shown} [y/N] "
        if self.reader is not None:
            answer = await self.reader.readline(question) or ""
        else:

            def ask() -> str:
                sys.stdout.write(question)
                sys.stdout.flush()
                return sys.stdin.readline()

            answer = await asyncio.to_thread(ask)
        return answer.strip().lower() in {"y", "yes"}


# Tools whose result is text other people wrote, or anything outside the user's own words: mail, chats, Canvas posts,
# documents, the screen, the clipboard, logs, a task's or an agent's report of a web page. Such text can tell the model
# to do things. Once one runs in a turn, an AUTO call later in it is asked (``Confirmer.unattended``), unless it is
# itself one of these reads, and tools that check ``TurnInfo.outside_content`` treat the turn as having read outside
# content; Octavia Futures counts the same reads (``futures.ingest``). The user's own words can still be the yes
# (``requested``, her quick voice's judgement): a read can't put words in their mouth. Mouse and keyboard steps are
# also checked by what they'd do (``computer_control``), whatever made the model want them. A list here, not a
# tool's own attribute, so no tool (or a build that adds one) can take itself off it.
READS_OUTSIDE = frozenset(
    {
        # mail, messages, notifications
        "gmail_recent",
        "search_gmail",
        "read_google_chat",
        "discord_read_messages",
        "phone_notifications",
        "open_phone_attachment",
        "get_calendar_events",
        # coursework others wrote
        "canvas_assignment",
        "canvas_course_material",
        "canvas_announcements",
        "canvas_due",
        # documents and saved pages
        "read_document",
        "read_reference",
        "word_outline",
        "excel_outline",
        "powerpoint_outline",
        # the user's money: merchants' names and the memos others wrote on payments
        "finance_look",
        "finance_import",
        "finance_refresh",
        # the screen and the clipboard
        "look_at_screen",
        "read_page",
        "zoom_screen",
        "find_on_screen",
        "see_screen_change",
        "wait_for_screen_change",
        "game_look",
        "screen_history",
        "read_clipboard",
        "clipboard_history",
        # what agents and services bring back from the web or a server, and her own record of it
        "get_background_task_result",
        "list_background_tasks",
        "whats_running",
        "check_web_task",
        "list_screen_tasks",
        "objective_status",
        "overnight_work",
        "my_picture_of_you",
        "scheduled_job_history",
        "read_past_conversation",
        "search_past_conversations",
        "list_references",
        "vercel_build_logs",
        "supabase_query",
        "supabase_logs",
        # pictures and pages from the web on her panels: their titles, and the words in a picture
        "show_image",
        "show_page",
    }
)
