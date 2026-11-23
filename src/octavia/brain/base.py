"""Brain provider interface.

The rest of the assistant talks to "a brain" only through this interface, so
Claude can be swapped for another backend (OpenAI, a local model, ...) without
touching the audio pipeline or the tools.

Contract:

* ``respond(turn)`` streams ``BrainEvent``s for one user turn and always ends
  with exactly one ``ResponseDone`` (even on error or interruption).
* The provider runs the tool loop itself. Every tool call MUST go through the
  ``ToolExecutor`` it was constructed with, which is where validation,
  permissions and confirmation happen.
* ``interrupt()`` stops the in-flight response quickly and leaves the provider
  usable for the next turn.
* Conversation context persists across ``respond`` calls until ``reset()``.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from octavia.tools.base import ToolSpec
from octavia.tools.executor import ToolExecutor


@dataclass(frozen=True)
class TurnPicture:
    """A picture that goes with the user's words, and the note that says what it is."""

    data: str  # base64
    mime_type: str
    note: str

    @property
    def bytes(self) -> bytes:
        return base64.b64decode(self.data)


@dataclass(frozen=True)
class UserTurn:
    text: str
    timestamp: datetime = field(default_factory=lambda: datetime.now().astimezone())
    notes: tuple[str, ...] = ()  # system notes, e.g. "you were interrupted after: ..."
    source: str = "voice"  # "voice" | "text" | "discord" (texted from their phone) | "window" | "scheduled"
    picture: TurnPicture | None = (
        None  # what they circled on their screen; only a brain that can see gets a turn with one
    )

    def render(self) -> str:
        """The message text actually sent to the model."""
        stamp = self.timestamp.strftime("%A, %B %d, %Y, %I:%M %p %Z").replace(" 0", " ")
        header = f"[{stamp}"
        if self.source == "text":
            header += "; typed, not spoken"
        elif self.source == "discord":
            header += "; texted from their phone over Discord, not spoken"
        elif self.source == "window":
            header += "; typed in your chat window on the PC, not spoken; your reply is shown there"
        notes = (*self.notes, self.picture.note) if self.picture is not None else self.notes
        if notes:
            header += "; " + "; ".join(notes)
        return f"{header}]\n{self.text}"


# Said when a request used all its steps (``max_turns``) before it was done (the Claude brain adds it at the end).
STEP_LIMIT_PAUSE = (
    "That was a lot of steps in a row, so I've paused here. Say keep going and I'll pick up where I left off."
)


@dataclass(frozen=True)
class BrainEvent:
    pass


@dataclass(frozen=True)
class TextDelta(BrainEvent):
    text: str


@dataclass(frozen=True)
class ToolUseStarted(BrainEvent):
    """The model decided to call a tool. Useful to flush pending speech. Sent once per call, as soon as it's known."""

    call_id: str
    name: str
    # Not one of our tools: the provider runs it itself (e.g. Claude's WebSearch/WebFetch), so it never goes through
    # the ToolExecutor, and what it brings back is text from outside that can tell the model to do things.
    builtin: bool = False


@dataclass(frozen=True)
class ResponseDone(BrainEvent):
    text: str  # full assistant text for this turn
    interrupted: bool = False
    error: str | None = None  # for logs and the console
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    # How much the brain carried at the end of the turn: its last model call's whole input (fresh, cached and newly
    # cached tokens), when the provider says. ``usage`` adds up every call of the turn, so it can't tell this.
    context_tokens: int | None = None
    # ...and at its start: its first model call's whole input (the prompt, the tools and the conversation so far,
    # with this turn's request), when the provider says. Right after a fresh start that's what a fresh prompt carries.
    first_context_tokens: int | None = None
    model: str | None = None  # the model that wrote the reply (its id), when the provider says
    # A short sentence the assistant can say about the error ("I've hit the usage limit"),
    # so a voice-only user learns what's wrong. None = a generic apology is spoken.
    spoken_error: str | None = None
    # How long before the turn the brain had started on it (``BrainProvider.start_early``), when it had.
    head_start_s: float | None = None


SystemPromptFactory = Callable[[], Awaitable[str]]


class BrainProvider(ABC):
    name: str = "brain"
    # Set by the assistant while a turn runs: called whenever the brain hears anything at all of the turn (thinking,
    # a tool's arguments streaming in), including what never becomes a BrainEvent, so its watchdog knows it's alive.
    heartbeat: Callable[[], None] | None = None

    def __init__(self, tools: list[ToolSpec], executor: ToolExecutor, system_prompt: SystemPromptFactory) -> None:
        self.tools = tools
        self.executor = executor
        self.system_prompt = system_prompt

    async def start(self) -> None:
        """Connect / warm up. Called once before the first turn."""

    @abstractmethod
    def respond(self, turn: UserTurn) -> AsyncIterator[BrainEvent]:
        """Stream the response to one user turn."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop the current response as soon as possible."""

    async def abandon_turn(self) -> None:
        """The assistant gave up on the turn in flight (its watchdog found it stuck) and has already interrupted it:
        if it's still holding the brain, let go of it, so the next turn isn't stuck behind it."""

    async def start_early(self, turn: UserTurn) -> bool:
        """Begin on ``turn`` before it's certain to be the user's whole request (they're pausing, and it sounds
        finished): ``respond`` with the very same turn carries on from it, anything else drops it. Nothing it would do
        happens before then. False when the brain doesn't, or can't now; that's always safe."""
        return False

    def drop_early(self, why: str = "") -> None:
        """Give up on the turn ``start_early`` began (the user went on talking). Nothing when there's none."""

    @abstractmethod
    async def reset(self) -> None:
        """Forget the current conversation and start a fresh context."""

    @property
    def session_id(self) -> str | None:
        """Provider-side conversation id, if any (used to resume after restart)."""
        return None

    async def close(self) -> None:
        """Release resources."""
