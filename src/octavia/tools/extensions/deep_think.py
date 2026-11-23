"""Tools to think a hard question through at a high effort, beside the conversation (see ``_deep_think``).

Starting one is SAFE, like a background task: the thinking brain only reads (the web and read-only tools) and
writes nothing but its own result file.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._background_tasks import has_result, saved_heading
from octavia.tools.extensions._deep_think import ThinkManager

MAX_RESULT_CHARS = 20000
_UNAVAILABLE = "Deep thinking isn't available right now (it starts with the assistant); answer directly."


class ThinkArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=80, description="A short name for the question.")
    question: str = Field(
        min_length=1,
        max_length=6000,
        description=(
            "The question with everything needed to answer it (it can't ask and doesn't see this conversation): "
            "what the user wants decided or worked out, the facts, numbers, constraints and options they gave, "
            "and what they already tried or ruled out."
        ),
    )


class SecondOpinionArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=80, description="A short name for what's being weighed.")
    perspective: str = Field(
        min_length=1,
        max_length=400,
        description=(
            "Who should argue against it, written for this very moment and the real worry: 'someone who thinks this "
            "plan ignores Thursday's lab', 'an engineer who has seen this kind of migration fail'. Not a generic role."
        ),
    )
    question: str = Field(
        min_length=1,
        max_length=6000,
        description="The position or plan to attack and what hangs on it. It doesn't see this conversation.",
    )
    context: str = Field(
        default="", max_length=6000, description="The facts, numbers and constraints it needs, and what was ruled out."
    )


class ThoughtRef(ToolArgs):
    thought: str = Field(default="", description="Number or name; empty = the latest.")


def _manager(ctx: ToolContext) -> ThinkManager | None:
    manager = ctx.extras.get("thoughts")
    return manager if isinstance(manager, ThinkManager) else None


class ThinkHard(Tool):
    name = "think_hard"
    description = (
        "Think a hard question through carefully, at a much higher effort, while you keep talking; the answer is "
        "said when it's ready (usually one to a few minutes). Only for questions where careful reasoning beats a "
        "quick reply: a decision with real trade-offs, a plan, debugging something tricky, maths or logic, or when "
        "they ask you to think hard. Never for facts, lookups, small talk, or anything a tool answers."
    )
    Args = ThinkArgs
    risk = RiskLevel.SAFE
    category = "deep_think"

    async def run(self, args: ThinkArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        asked = manager.asked()  # their own words, never the model's say-so, make it theirs
        if why := await manager.allowed(asked):
            return ToolResult.error(why)
        try:
            task = manager.think(args.title, args.question, asked)
        except RuntimeError:
            return ToolResult.error(_UNAVAILABLE)
        return ToolResult.success(
            f"Thinking it through (deep think #{task.id}). Tell the user in one short sentence, in your own words, "
            "that you're taking a moment to think it over properly and will tell them when you have it. Don't "
            "answer the question yourself now.",
            data={"id": task.id},
        )


class SecondOpinion(Tool):
    name = "second_opinion"
    description = (
        "Have a separate, high-effort mind argue against something before it stands, from a perspective you write "
        "for this moment; it comes back to you to weigh while you keep talking. For costly moments only: a plan for "
        "days of work, a decision they're about to act on that's hard to undo, a build you're about to install, or "
        "when you're about to agree with them on something you have doubts about, or they ask for one. Never for "
        "everyday questions."
    )
    Args = SecondOpinionArgs
    risk = RiskLevel.SAFE
    category = "deep_think"

    async def run(self, args: SecondOpinionArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        asked = manager.asked_for_critic()  # their own words, never the model's say-so, make it theirs
        if why := await manager.allowed_critic(asked):
            return ToolResult.error(why)
        try:
            task = manager.second_opinion(args.title, args.perspective, args.question, args.context, asked)
        except RuntimeError:
            return ToolResult.error(_UNAVAILABLE)
        return ToolResult.success(
            f"Getting a second opinion (deep think #{task.id}). It comes back to you as a turn of your own to weigh. "
            "Say in a short line that you want to check this against someone arguing the other side, then carry on.",
            data={"id": task.id},
        )


class GetThought(Tool):
    name = "get_thought"
    description = "A deep think's full reasoning, or how it's going. Give the gist unless they want all of it."
    Args = ThoughtRef
    risk = RiskLevel.SAFE
    category = "deep_think"

    async def run(self, args: ThoughtRef, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        tasks = manager.tasks
        task = manager.find(args.thought) if args.thought.strip() else (tasks[-1] if tasks else None)
        if task is None:
            return ToolResult.error("There's no deep think like that." if tasks else "No deep thinks yet.")
        if not has_result(task):
            return ToolResult.success(f"No answer yet. {task.progress(datetime.now())}")
        try:
            text = Path(task.file).read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult.error(
                f"The reasoning for “{task.title}” was saved to {task.file}, but I can't read it: {exc}"
            )
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n\n[… the rest is in the file]"
        return ToolResult.success(f"{saved_heading(task)}\n\n{text}", data={"id": task.id, "file": task.file})


class CancelThought(Tool):
    name = "cancel_thought"
    description = "Stop the deep think under way (when they say never mind, or it's no longer needed)."
    risk = RiskLevel.SAFE
    category = "deep_think"
    timeout_s = 30.0  # its brain closes before it returns

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        task = next((task for task in manager.tasks if task.active), None)
        if task is None or not await manager.cancel(task):
            return ToolResult.success("No deep think is under way.")
        return ToolResult.success(f"Stopped thinking about “{task.title}”.")


TOOLS = [ThinkHard, SecondOpinion, GetThought, CancelThought]
