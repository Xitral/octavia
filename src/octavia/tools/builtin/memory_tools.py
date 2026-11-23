"""Long-term memory tools: remember, recall, correct_memory, forget.

Each saved fact records where it came from (``source``: the user said it, she inferred it, or she saw it) and the
conversation it was saved in. A correction doesn't overwrite: the old version stays as history, out of the prompt
and out of plain recall, and ``recall`` with ``history`` shows it. ``forget`` deletes a fact with its history.
"""

from __future__ import annotations

import re
import time
from typing import Literal

from pydantic import Field

from octavia.core.conversation_memory import keywords
from octavia.memory.base import MemoryItem
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

Category = Literal["preference", "person", "project", "routine", "decision", "fact", "general"]
Basis = Literal["user", "inferred", "observed"]

# A light guard against storing obvious secrets. The system prompt tells the
# model not to, this catches the careless cases.
_SECRET_PATTERNS = [
    re.compile(r"\b(password|passcode|pin|api[ _-]?key|secret|token)s?\s*(is|was|:|=)\s*\S+", re.I),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card-number-like digit runs
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-like
    re.compile(r"\b(sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9]{10,}"),
]
# Saved notes a new one shares this many keywords with are shown back, in case it updates one of them.
_SIMILAR_WORDS = 2
_SIMILAR_SHOWN = 3


def _looks_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _session(ctx: ToolContext) -> str | None:
    """The conversation running now (``ctx.extras["session"]``), for a fact's provenance."""
    getter = ctx.extras.get("session")
    try:
        return getter() if callable(getter) else None
    except Exception:
        return None


def _basis(ctx: ToolContext, basis: str) -> str:
    """How she knows it. Not "the user said it" in a turn that took in outside text (a web page, a mail): that text
    may be what's talking (``TurnInfo.outside_content``)."""
    getter = ctx.extras.get("turn")
    try:
        turn = getter() if callable(getter) else None
    except Exception:
        turn = None
    return "observed" if basis == "user" and turn is not None and turn.outside_content else basis


def describe(item: MemoryItem) -> str:
    """One fact as she reads it back: its id, category, when it was saved and how she knows it."""
    day = time.strftime("%b %d %Y", time.localtime(item.created_at))
    how = {"inferred": ", your inference", "observed": ", seen"}.get(item.source, "")
    old = f", replaced by #{item.superseded_by}" if item.superseded_by is not None else ""
    return f"#{item.id} [{item.category}] ({day}{how}{old}) {item.text}"


class RememberArgs(ToolArgs):
    text: str = Field(
        min_length=3,
        max_length=200,
        description="One short self-contained sentence, e.g. 'The user's sister is Ana.'",
    )
    category: Category = Field(default="general")
    pinned: bool = Field(default=False, description="Core fact, always in context.")
    basis: Basis = Field(
        default="user",
        description="user: they told you; inferred: your own conclusion; observed: seen on screen or in a file.",
    )


class Remember(Tool):
    name = "remember"
    description = (
        "Save a durable fact about the user. If it changes a saved one, use correct_memory instead. "
        "Never store passwords, keys or financial/ID numbers."
    )
    Args = RememberArgs
    risk = RiskLevel.SAFE
    category = "memory"

    async def run(self, args: RememberArgs, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult.error("Long-term memory is disabled.")
        if _looks_secret(args.text):
            return ToolResult.error("That looks like a secret (password, key or account number), so it was not saved.")
        item = await ctx.memory.add(
            args.text,
            category=args.category,
            pinned=args.pinned,
            source=_basis(ctx, args.basis),
            session_id=_session(ctx),
        )
        message = f"Saved memory #{item.id}."
        similar = await _similar(ctx, item)
        if similar:
            message += (
                " Similar saved notes:\n"
                + "\n".join(describe(m) for m in similar)
                + f"\nIf #{item.id} changes one of these, call correct_memory on that one with the new text."
            )
        return ToolResult.success(message, data={"id": item.id, "similar": [m.id for m in similar]})


async def _similar(ctx: ToolContext, item: MemoryItem) -> list[MemoryItem]:
    """Current notes sharing at least two keywords with ``item``: it may be an update of one of them."""
    assert ctx.memory is not None
    words = keywords(item.text)
    if len(words) < _SIMILAR_WORDS:
        return []
    found = await ctx.memory.search(" ".join(words), limit=_SIMILAR_SHOWN + 3)
    out = []
    for m in found:
        theirs = set(keywords(m.text))
        if m.id != item.id and sum(1 for w in words if w in theirs) >= _SIMILAR_WORDS:
            out.append(m)
    return out[:_SIMILAR_SHOWN]


class RecallArgs(ToolArgs):
    query: str = Field(description="Keywords.")
    limit: int = Field(default=5, ge=1, le=20)
    history: bool = Field(default=False, description="Also show what corrected notes used to say.")


class Recall(Tool):
    name = "recall"
    description = "Search long-term memory about the user; returns ids, dates and how each was learned."
    Args = RecallArgs
    risk = RiskLevel.SAFE
    category = "memory"

    async def run(self, args: RecallArgs, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult.error("Long-term memory is disabled.")
        items = await ctx.memory.search(args.query, limit=args.limit, include_old=args.history)
        if not items:
            return ToolResult.success("No matching memories.")
        lines = [describe(m) for m in items]
        if args.history:
            shown = {m.id for m in items}
            for m in items:
                for old in (await ctx.memory.history(m.id))[1:]:
                    if old.id not in shown:
                        lines.append(f"  earlier: {describe(old)}")
                        shown.add(old.id)
        return ToolResult.success("\n".join(lines), data={"ids": [m.id for m in items]})


class CorrectMemoryArgs(ToolArgs):
    memory_id: int = Field(description="The # of the saved note that's wrong or out of date.")
    text: str = Field(min_length=3, max_length=200, description="The corrected note, one self-contained sentence.")
    basis: Basis = Field(default="user", description="How you know the correction (as for remember).")


class CorrectMemory(Tool):
    name = "correct_memory"
    description = (
        "Fix or update a saved note about the user (they corrected you, or it changed). "
        "The old version is kept as history; use forget only when they want it gone."
    )
    Args = CorrectMemoryArgs
    risk = RiskLevel.SAFE  # nothing is lost: the old version stays, and recall with history shows it
    category = "memory"

    async def run(self, args: CorrectMemoryArgs, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult.error("Long-term memory is disabled.")
        if _looks_secret(args.text):
            return ToolResult.error("That looks like a secret (password, key or account number), so it was not saved.")
        try:
            new = await ctx.memory.correct(
                args.memory_id, args.text, session_id=_session(ctx), source=_basis(ctx, args.basis)
            )
        except NotImplementedError:
            return ToolResult.error("This memory store can't correct notes; use remember and forget.")
        if new is None:
            return ToolResult.error(f"No memory with id {args.memory_id}.")
        if new.id == args.memory_id:
            return ToolResult.success(f"Memory #{new.id} already says that.", data={"id": new.id})
        return ToolResult.success(
            f"Corrected: memory #{new.id} now says {new.text!r} (#{args.memory_id} kept as history).",
            data={"id": new.id, "replaced": args.memory_id},
        )


class ForgetArgs(ToolArgs):
    memory_id: int


class Forget(Tool):
    name = "forget"
    description = "Delete a long-term memory by id, with its earlier versions. To fix one, use correct_memory."
    Args = ForgetArgs
    risk = RiskLevel.CONFIRM
    category = "memory"

    async def describe_call(self, args: ForgetArgs, ctx: ToolContext) -> str:
        getter = getattr(ctx.memory, "get", None)
        item = await getter(args.memory_id) if getter else None
        return f'forget "{item.text}"' if item else f"forget memory number {args.memory_id}"

    async def run(self, args: ForgetArgs, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult.error("Long-term memory is disabled.")
        versions = len(await ctx.memory.history(args.memory_id))
        if await ctx.memory.delete(args.memory_id):
            earlier = f" and its {versions - 1} earlier version{'s' if versions > 2 else ''}" if versions > 1 else ""
            return ToolResult.success(f"Deleted memory #{args.memory_id}{earlier}.")
        return ToolResult.error(f"No memory with id {args.memory_id}.")


TOOLS = [Remember, Recall, CorrectMemory, Forget]
