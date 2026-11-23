"""One guarded Claude Code session that hands back a structured answer: the planner, a code step, a reviewer.

Built like self-improvement's builder (``evolve/builder.py``): only the tools it's given exist for it, a PreToolUse
hook (``guard``) checks every call, ``--restricted`` keeps file access in its folder, none of the user's own Claude
Code settings, memory, skills or MCP servers apply, prompts are sent verbatim, and it runs on the brain's own login
and subscription. Its usage reports go to the usage budget like every other Claude call of hers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from octavia.brain.claude import cli_environment, prepare_process_environment
from octavia.core import usage as claude_usage
from octavia.evolve.builder import _failure_reason, _sdk_client, _stop, _structured
from octavia.objectives.guard import STRUCTURED_OUTPUT_TOOL, Check, hook

log = logging.getLogger(__name__)

ClientFactory = Callable[[Any], Any]  # options -> an object with the ClaudeSDKClient methods (tests pass fakes)
Progress = Callable[[str], None]


@dataclass(frozen=True)
class SessionSettings:
    model: str
    effort: str | None
    config_dir: Path | None  # the brain's Claude Code login
    cli_path: str | None
    auth: str = "subscription"


@dataclass
class SessionResult:
    ok: bool
    data: dict[str, Any] | None = None  # the structured answer
    text: str = ""  # its final text, when there was no structured answer
    error: str | None = None  # why it didn't end well, in words for the user
    turns: int = 0
    seconds: float = 0.0
    tool_calls: int = 0
    blocked_calls: int = 0


async def run_session(
    settings: SessionSettings,
    *,
    cwd: Path,
    prompt: str,
    system: str,
    tools: tuple[str, ...],
    check: Check,
    schema: dict[str, Any],
    max_turns: int,
    timeout_s: float,
    env: dict[str, str] | None = None,
    progress: Progress | None = None,
    client_factory: ClientFactory | None = None,
) -> SessionResult:
    """Run one session to its answer. Cancelling the caller stops it (interrupt, then disconnect)."""
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    started = time.monotonic()
    counts = {"calls": 0, "blocked": 0}

    def on_call(name: str, tool_input: Any, decision: Any) -> None:
        counts["calls"] += 1
        if not decision.allowed:
            counts["blocked"] += 1
            log.info("Objective session call blocked: %s: %s", name, decision.reason)
        elif progress is not None:
            progress(_describe(name, tool_input, cwd))

    options = ClaudeAgentOptions(
        tools=list(tools),
        allowed_tools=[*tools, STRUCTURED_OUTPUT_TOOL],
        permission_mode="dontAsk",
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[hook(check, on_call)])]},
        system_prompt={"type": "preset", "preset": "claude_code", "append": system},
        setting_sources=[],
        skills=[],
        strict_mcp_config=True,
        mcp_servers={},
        model=settings.model,
        effort=settings.effort,
        max_turns=max_turns,
        cwd=str(cwd),
        env=cli_environment(settings.config_dir) | {"GIT_PAGER": "cat", "PAGER": "cat"} | (env or {}),
        extra_args={"restricted": None},
        output_format={"type": "json_schema", "schema": schema},
        include_partial_messages=False,
        verbatim_prompts=True,
        cli_path=settings.cli_path,
        stderr=lambda line: log.debug("objective-cli: %s", line.rstrip()),
    )
    client: Any = None
    timer = asyncio.timeout(timeout_s)
    try:
        prepare_process_environment(settings.auth)
        client = (client_factory or _sdk_client)(options)
        async with timer:
            await client.connect()
            await client.query(prompt)
            result = await _receive(client)
    except asyncio.CancelledError:
        if client is not None:
            await asyncio.shield(_stop(client.interrupt))
        raise
    except Exception as exc:
        if isinstance(exc, TimeoutError) and timer.expired():
            if client is not None:
                await _stop(client.interrupt)
            result = SessionResult(ok=False, error=f"it took longer than {round(timeout_s / 60)} minutes")
        else:
            log.warning("An objective session failed", exc_info=True)
            result = SessionResult(ok=False, error=f"the session hit an error ({type(exc).__name__})")
    finally:
        if client is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(_stop(client.disconnect))
    result.seconds = time.monotonic() - started
    result.tool_calls, result.blocked_calls = counts["calls"], counts["blocked"]
    return result


async def _receive(client: Any) -> SessionResult:
    from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage

    api_error: str | None = None
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            api_error = msg.error or api_error
        elif isinstance(msg, RateLimitEvent):
            claude_usage.observe(msg.rate_limit_info)
            if getattr(msg.rate_limit_info, "status", None) == "rejected":
                api_error = "rate_limit"
        elif isinstance(msg, ResultMessage):
            ok = not msg.is_error and msg.subtype == "success"
            text = msg.result.strip() if isinstance(msg.result, str) else ""
            return SessionResult(
                ok=ok,
                data=_structured(msg.structured_output) if ok else None,
                text=text,
                error=None if ok else _failure_reason(msg, api_error),
                turns=msg.num_turns or 0,
            )
    return SessionResult(ok=False, error="the session ended without a result")


def _describe(name: str, tool_input: Any, cwd: Path) -> str:
    args = tool_input if isinstance(tool_input, dict) else {}
    verbs = {"Read": "reading", "Edit": "editing", "Write": "writing"}
    if name in verbs:
        path = str(args.get("file_path") or "a file")
        with contextlib.suppress(ValueError):
            path = Path(path).relative_to(cwd).as_posix()
        return f"{verbs[name]} {path[-80:]}"
    if name == "Bash":
        return f"running {' '.join(str(args.get('command') or '').split())[:80]}"
    if name in ("Grep", "Glob"):
        return "searching the code"
    if name == "WebSearch":
        return "searching the web"
    if name == "WebFetch":
        return "reading a web page"
    if name == "TodoWrite":
        return "planning its next steps"
    return f"using {name}"
