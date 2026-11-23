"""Browser tools: open a URL, open a web search."""

from __future__ import annotations

import asyncio
import webbrowser
from urllib.parse import quote_plus, urlsplit

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
}


def validate_web_url(url: str) -> str:
    """Return a normalized http(s) URL or raise ValueError."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http and https URLs can be opened")
    if not parts.hostname or "." not in parts.hostname and parts.hostname != "localhost":
        raise ValueError("that doesn't look like a web address")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not allowed")
    if any(ch in url for ch in "\r\n\t\x00"):
        raise ValueError("URL contains control characters")
    return url


async def _open_in_browser(url: str) -> bool:
    return await asyncio.to_thread(webbrowser.open, url, 2)


class OpenUrlArgs(ToolArgs):
    url: str = Field(description="E.g. 'weather.gov'.")


class OpenUrl(Tool):
    name = "open_url"
    description = "Open an http(s) page in the browser."
    Args = OpenUrlArgs
    risk = RiskLevel.SAFE
    category = "web"

    async def run(self, args: OpenUrlArgs, ctx: ToolContext) -> ToolResult:
        try:
            url = validate_web_url(args.url)
        except ValueError as exc:
            return ToolResult.error(f"Can't open that: {exc}.")
        if not await _open_in_browser(url):
            return ToolResult.error("No web browser responded.")
        return ToolResult.success(f"Opened {urlsplit(url).hostname} in the browser.")


class WebSearchArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=300)
    engine: str | None = Field(default=None, description="google, bing, duckduckgo or youtube.")


class OpenWebSearch(Tool):
    name = "open_web_search"
    description = "Show web or YouTube search results in the browser."
    Args = WebSearchArgs
    risk = RiskLevel.SAFE
    category = "web"

    async def run(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        engine = (args.engine or ctx.config.tools.search_engine).lower()
        template = SEARCH_ENGINES.get(engine)
        if template is None:
            return ToolResult.error(f"Unknown search engine '{engine}'. Options: {', '.join(SEARCH_ENGINES)}.")
        if not await _open_in_browser(template.format(q=quote_plus(args.query))):
            return ToolResult.error("No web browser responded.")
        return ToolResult.success(f"Opened {engine} results for '{args.query}'.")


TOOLS = [OpenUrl, OpenWebSearch]
