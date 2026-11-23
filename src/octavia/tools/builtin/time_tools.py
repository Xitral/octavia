"""Date and time tools."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult


class CurrentTimeArgs(ToolArgs):
    timezone: str | None = Field(
        default=None,
        description="IANA zone, e.g. 'Asia/Tokyo'; omit for local.",
    )


class GetCurrentTime(Tool):
    name = "get_current_time"
    description = "Current date, time and weekday, here or in a time zone."
    Args = CurrentTimeArgs
    risk = RiskLevel.SAFE
    category = "time"

    async def run(self, args: CurrentTimeArgs, ctx: ToolContext) -> ToolResult:
        if args.timezone:
            try:
                now = datetime.now(ZoneInfo(args.timezone))
            except (ZoneInfoNotFoundError, ValueError):
                return ToolResult.error(
                    f"Unknown time zone '{args.timezone}'. Use an IANA name like 'America/New_York'."
                )
            where = args.timezone
        else:
            now = datetime.now().astimezone()
            where = "local"
        text = now.strftime("%A, %B %d, %Y, %I:%M %p").replace(" 0", " ")
        return ToolResult.success(
            f"{text} ({where}, UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]})",
            data={"iso": now.isoformat(), "timezone": where},
        )


TOOLS = [GetCurrentTime]
