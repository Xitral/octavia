"""Memory interfaces.

Three deliberately separate concerns:

1. ``ConversationStore`` - session/turn history and metadata (what was said,
   when, whether it was interrupted). Used for continuity and debugging; it is
   NOT dumped wholesale into prompts.
2. ``LongTermMemory`` - durable facts about the user (preferences, projects,
   names, routines, decisions). A small, relevant slice is injected into the
   system prompt; the rest is reachable through the recall tool.
3. ``ToolStateStore`` - namespaced key/value state owned by tools and
   integrations (caches, tokens-to-be, last-used settings).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# Categories of the notes she keeps for her own work rather than about the user: a playbook for an app or a site
# (``octavia.playbooks``) is "playbook:app:powerpoint". They live with the facts (provenance, corrections kept as
# history, ``forget``) but stay out of the prompt's pick, plain recall, listing and counting.
APART = "playbook:"


@dataclass(frozen=True)
class MemoryItem:
    id: int
    text: str
    category: str
    pinned: bool
    created_at: float
    updated_at: float
    # Who it came from: "user" (they said it), "inferred" (her own conclusion), "observed" (seen on screen or in a
    # file), "assistant" (saved before this was told apart).
    source: str = "user"
    session_id: str | None = None  # the conversation it was saved in, and the user's turn there (turns.id)
    turn_id: int | None = None
    replaces: int | None = None  # the older version this one corrected
    superseded_by: int | None = None  # set on an old version: the fact that corrected it (kept as history)


@dataclass(frozen=True)
class TurnRecord:
    session_id: str
    role: str  # "user" | "assistant"
    text: str
    created_at: float
    interrupted: bool = False


@dataclass(frozen=True)
class SessionRecord:
    """One past conversation: when it ran, how many turns it had and its saved summary (None if it never ended)."""

    id: str
    started_at: float
    ended_at: float | None
    summary: str | None
    turns: int
    last_turn_at: float


class LongTermMemory(ABC):
    @abstractmethod
    async def add(
        self,
        text: str,
        category: str = "general",
        pinned: bool = False,
        source: str = "user",
        session_id: str | None = None,
    ) -> MemoryItem:
        """Save a fact (or refresh it when it's already saved). ``session_id`` is the conversation it came from."""

    @abstractmethod
    async def search(self, query: str, limit: int = 5, include_old: bool = False) -> list[MemoryItem]:
        """Current facts matching ``query``, best first; ``include_old`` adds the versions corrected since."""

    @abstractmethod
    async def list(self, limit: int = 50, category: str | None = None) -> list[MemoryItem]: ...

    @abstractmethod
    async def delete(self, item_id: int) -> bool: ...

    @abstractmethod
    async def for_prompt(self, limit: int) -> list[MemoryItem]:
        """The memories in her system prompt (pinned first, then recent)."""

    async def choose_for_prompt(self, limit: int) -> list[MemoryItem]:
        """Pick the memories for a new system prompt; ``for_prompt`` then answers with the same pick."""
        return await self.for_prompt(limit)

    async def count(self) -> int:
        """How many facts are saved (current versions only)."""
        return len(await self.list(limit=1_000_000))

    async def correct(
        self, item_id: int, text: str, session_id: str | None = None, source: str = "user"
    ) -> MemoryItem | None:
        """Replace a fact with a corrected one, keeping the old one as history; None when there's no such fact."""
        raise NotImplementedError

    async def history(self, item_id: int) -> list[MemoryItem]:
        """A fact and the older versions it corrected, newest first."""
        return []

    async def list_apart(self, prefix: str = APART, limit: int = 500) -> list[MemoryItem]:
        """The current notes kept apart from the facts (``APART``) whose category starts with ``prefix``, newest
        first."""
        return []

    async def trim_history(self, item_id: int, keep: int) -> int:
        """Delete all but the ``keep`` newest older versions of a fact; returns how many went."""
        return 0


class ConversationStore(ABC):
    @abstractmethod
    async def start_session(self, session_id: str, brain: str) -> None: ...

    @abstractmethod
    async def end_session(self, session_id: str, reason: str, summary: str | None = None) -> None: ...

    @abstractmethod
    async def set_brain_session(self, session_id: str, brain_session_id: str) -> None:
        """Remember the provider's own session id (e.g. a Claude session to resume)."""

    @abstractmethod
    async def add_turn(self, session_id: str, role: str, text: str, interrupted: bool = False) -> None: ...

    @abstractmethod
    async def recent_turns(self, session_id: str, limit: int = 20) -> list[TurnRecord]: ...

    async def latest_turns(self, limit: int = 20) -> list[TurnRecord]:
        """The last turns of the most recent session that has any, oldest first (for a recap after a restart)."""
        return []

    async def turns_since(self, since: float, limit: int = 100) -> list[TurnRecord]:
        """The last ``limit`` turns said at or after ``since`` across sessions, oldest first: a restart or a timeout
        starts a new session in the middle of what the user sees as one conversation."""
        return [t for t in await self.latest_turns(limit) if t.created_at >= since]

    async def turns_between(self, start: float, end: float, limit: int = 500) -> list[TurnRecord]:
        """The last ``limit`` turns said from ``start`` up to ``end``, across sessions, oldest first (a past day)."""
        return []

    async def next_turn_at(self, at: float, *, later: bool) -> float | None:
        """When the nearest turn after ``at`` (or before it) was said, across sessions; None when there's none."""
        return None

    async def search_turns(
        self, query: str, limit: int = 10, exclude_session: str | None = None, since: float | None = None
    ) -> list[TurnRecord]:
        """Turns of any past conversation that match ``query``, best match first (for "remember when we...")."""
        return []

    async def recent_sessions(self, limit: int = 10, since: float = 0.0) -> list[SessionRecord]:
        """The latest conversations that have any turns, started at or after ``since``, newest first."""
        return []


class ToolStateStore(ABC):
    @abstractmethod
    async def get(self, namespace: str, key: str, default: Any = None) -> Any: ...

    @abstractmethod
    async def set(self, namespace: str, key: str, value: Any) -> None: ...

    @abstractmethod
    async def delete(self, namespace: str, key: str) -> None: ...
