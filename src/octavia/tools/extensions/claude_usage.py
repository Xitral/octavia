"""How full the user's Claude limits are, and what her own background work used and skipped (core/usage.py).

For "how's my Claude usage?", "how much of my limit is left?" and "what did you skip last night?". It only reads.
"""

from __future__ import annotations

import asyncio

from octavia.tools.base import NoArgs, RiskLevel, Tool, ToolContext, ToolResult


class ClaudeUsage(Tool):
    name = "claude_usage"
    description = (
        "How full the user's Claude subscription limits are (the 5-hour window and the week, and when each resets), "
        "how much of it your own background work (night shift, builds you start yourself, your reflection) used "
        "today against its daily share, and what you skipped to save it. For 'how's my Claude usage?'."
    )
    Args = NoArgs
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        from octavia.core import usage

        budget = usage.budget()
        if budget is None:
            return ToolResult.error("Usage tracking isn't running here.")
        return ToolResult.success(await asyncio.to_thread(budget.describe))


TOOLS = [ClaudeUsage]
