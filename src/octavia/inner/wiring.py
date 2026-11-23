"""How the app (and ``octavia inner``) put her inner life together: the journal, where the night's material comes
from, what's off-limits, and the hooks into the conversation and the proactive gate."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from octavia.inner import digest
from octavia.inner.service import BrainFactory, InnerLife
from octavia.inner.store import InnerStore

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.memory.base import ConversationStore, LongTermMemory

log = logging.getLogger(__name__)

THOUGHT_PREFIX = "thought_"  # the kinds of remark the proactive gate takes from her journal


def off_limits_fn(config: AppConfig, futures: Any | None) -> Callable[[], Awaitable[frozenset[str]]]:
    """The domains she may not think about: the config's list, plus whatever the user told Futures."""
    floor = frozenset(d.lower() for d in config.futures.off_limits)

    async def off_limits() -> frozenset[str]:
        if futures is None:
            return floor
        said = await futures.call(lambda: futures.controls.off_limits)
        return floor | frozenset(said)

    return off_limits


def persona_fn(config: AppConfig) -> Callable[[], tuple[str, str, str]]:
    def persona() -> tuple[str, str, str]:
        from octavia.brain.prompt import SystemPromptBuilder

        values = SystemPromptBuilder(config).persona_values()
        return values["assistant_name"], values["user_name"], values["personality"]

    return persona


def futures_report_fn(futures: Any | None) -> digest.FuturesReport | None:
    if futures is None:
        return None
    from octavia.futures.reports import report

    async def read(topic: str) -> str:
        return str(await futures.call(report, futures, topic, digest.FUTURES_LIMIT))

    return read


def create_inner_life(
    config: AppConfig,
    *,
    conversations: ConversationStore | None,
    memory: LongTermMemory | None,
    futures: Any | None,
    brain_factory: BrainFactory | None = None,
    idle_s: Callable[[], float | None] | None = None,
) -> InnerLife:
    """The service, on its journal. Without a ``brain_factory`` it only reads and edits the journal."""
    from octavia.tools.extensions.about_me import read_notes

    settings = config.inner_life
    store = InnerStore(config.resolve_path(settings.database))
    sources = digest.Sources(
        conversations=conversations,
        memory=memory,
        memory_items=config.memory.max_prompt_items,
        about_me=lambda: read_notes(config),
        futures=futures_report_fn(futures),
    )
    return InnerLife(
        store,
        settings,
        sources=sources,
        persona=persona_fn(config),
        brain_factory=brain_factory,
        off_limits=off_limits_fn(config, futures),
        idle_s=idle_s,
    )


def with_turn_notes(
    recall: Callable[[str], Awaitable[str | None]] | None,
    inner: InnerLife,
    session: Callable[[], str | None],
    quiet: Callable[[str], bool],
    budget_s: float = 0.4,
) -> Callable[[str], Awaitable[str | None]]:
    """The conversation's per-turn lookup (``Assistant.recall``) with her thoughts added: what automatic recall found,
    then the thought on her mind (``InnerLife.prepare_note``). The two run side by side, each within ``budget_s``
    (the assistant gives the whole lookup half a second), so either being slow or failing leaves the other; a
    thought that came too late counts as never offered."""

    async def recalled(text: str) -> str | None:
        if recall is None:
            return None
        try:
            return await asyncio.wait_for(recall(text), budget_s)
        except Exception:
            log.warning("Couldn't look up memories for the turn", exc_info=True)
            return None

    async def thought(text: str) -> str | None:
        try:
            note, commit = await asyncio.wait_for(
                asyncio.to_thread(inner.prepare_note, text, session(), quiet=quiet(text)), budget_s
            )
        except Exception:
            log.warning("Couldn't look up her thoughts for the turn", exc_info=True)
            return None
        if note:
            task = asyncio.create_task(asyncio.to_thread(commit))
            _pending.add(task)
            task.add_done_callback(_pending.discard)
        return note

    async def notes(text: str) -> str | None:
        found = await asyncio.gather(recalled(text), thought(text))
        return "; ".join(n for n in found if n) or None

    return notes


_pending: set[asyncio.Task[None]] = set()  # bookkeeping still being written


def connect_proactive(futures: Any, inner: InnerLife) -> bool:
    """Hand her open thoughts to the proactive gate, which decides whether and when one is said (Proactive Octavia's
    ``FuturesService.add_source``). False when this Futures has no such hook yet."""
    add_source = getattr(futures, "add_source", None)
    if add_source is None:
        return False
    try:
        from octavia.futures.engine import RemarkSource  # type: ignore[attr-defined]
    except ImportError:
        return False

    def thought_id(key: str) -> str:
        return key.split(":", 1)[1]

    add_source(
        RemarkSource(
            prefix=THOUGHT_PREFIX,
            candidates=inner.candidates,
            shared=lambda key: inner.mark_shared(thought_id(key)),
            declined=lambda key: inner.mark_dismissed(thought_id(key)),
        )
    )
    return True
