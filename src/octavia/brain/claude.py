"""Claude brain via the Claude Agent SDK (subscription-backed Claude Code CLI).

Design notes (verified against claude-agent-sdk 0.2.159 / bundled CLI 2.1.281):

* One long-lived ``ClaudeSDKClient`` is owned by a single actor task; turns
  are queued to it. The SDK warns against sharing a client across tasks.
* Isolation: no built-in Claude Code tools except the ones listed in
  ``brain.claude.builtin_tools`` (default: WebSearch/WebFetch, which are
  read-only), none of your personal Claude Code settings, CLAUDE.md, skills,
  plugins or claude.ai connectors (``setting_sources=[]``,
  ``strict_mcp_config=True``), and no auto-memory.
* Our tools are exposed as an in-process MCP server. Every call goes through
  the ``ToolExecutor`` (validation, permission policy, confirmation).
* The system prompt is passed as a file because Windows caps command lines
  at ~32 KB, and the SDK reports that failure as a misleading "CLI not found".
* ``ANTHROPIC_API_KEY`` would silently override the Max subscription, so it is
  removed from this process's environment unless ``auth: api_key``.
* Interrupt: ``client.interrupt()``, then keep draining to the ResultMessage
  (the SDK requires it before the next query).
* If the CLI dies, the actor reconnects with ``resume=<session id>``; a turn
  that reached a dead CLI before any of it ran is retried once.
* A turn that hears nothing from the CLI for ``stall_timeout_s`` while no tool
  is running (a model request that never answers) ends with an error and a
  reconnect, instead of leaving her thinking forever.
* ``--system-prompt-snapshot off``: a resumed conversation (after a restart, e.g.
  to install a self-improvement) uses the freshly rendered system prompt, not the
  one recorded when the conversation began.
* ``tools_on_demand``: Claude Code's tool search (``ENABLE_TOOL_SEARCH=true``
  plus its ``ToolSearch`` tool) defers every MCP tool except the ones marked
  ``anthropic/alwaysLoad`` in their ``_meta``; a deferred tool is listed by name,
  and its ``anthropic/searchHint`` (here its group) helps the search find it. The SDK only writes
  ``maxResultSizeChars`` into ``_meta``, so ``_mcp_server`` adds ours.
* Early starts (``start_early``): a turn can be sent while the user is still
  pausing, so the model's first second or two overlaps the end-of-speech
  silence. Our tool calls in it wait until ``respond`` takes it (the same
  rendered turn); a drop interrupts it, and the next turn sent tells the model
  that the cut-off message was only the start of what the user said.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fnmatch
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octavia.brain.base import (
    STEP_LIMIT_PAUSE,
    BrainEvent,
    BrainProvider,
    ResponseDone,
    SystemPromptFactory,
    TextDelta,
    ToolUseStarted,
    TurnPicture,
    UserTurn,
)
from octavia.config import ClaudeConfig
from octavia.core import usage as claude_usage
from octavia.core.events import ErrorOccurred, EventBus, Notice, ToolCallFinished, ToolCallStarted, ToolsLoaded
from octavia.tools.base import ToolResult, ToolSpec
from octavia.tools.executor import ToolExecutor

log = logging.getLogger(__name__)

SERVER_KEY = "assistant"  # our tools appear to the model as mcp__assistant__<name>
TOOL_PREFIX = f"mcp__{SERVER_KEY}__"
# Claude Code's own tool that loads deferred tool schemas (``tools_on_demand``). It only reads our tool list, so it
# isn't one of the built-ins that bring in outside text, and no one needs to see or hear it.
TOOL_SEARCH = "ToolSearch"
# How long an early start waits to be taken by the turn before it's dropped: the user's silence ends within about a
# second of it, and the turn follows at once.
EARLY_TTL_S = 5.0
# Sent ahead of the next turn after an early start was dropped once it had reached the model: that message stays in
# the conversation, cut off, and the model must not take it for the request.
_CUT_NOTE = (
    "[Your previous message was what the user had said when they paused, sent before they had finished; it was cut "
    "off, and nothing of your answer to it was said or done. Don't act on it: answer what follows.]\n"
)
# The CLI echoes every tool result back as one JSON line, and a screenshot alone is up to ~4.7 MB of base64.
# The SDK's default 1 MB line limit made such a turn fail with CLIJSONDecodeError and drop the connection.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# The prompt cache warm-up (``ClaudeBrain._warm``): which prompts were read when, in the work folder; the model keeps
# a cached prompt an hour (her turns ask for the 1-hour cache), so one read within ~55 minutes is still warm.
WARM_FILE = "cache_warm.json"
WARM_AFTER_S = 55 * 60.0
WARM_JOB = "prompt cache warm-up"
WARM_PROMPT = "Reply with the one word: ready. Don't use any tools."

_ERRORS = {
    "authentication_failed": "Claude isn't signed in. Run `uv run octavia login` once.",
    "billing_error": "Claude reported a billing problem with the account.",
    "rate_limit": "Claude usage limit reached for now.",
    "invalid_request": "Claude rejected the request.",
    "server_error": "Claude's servers had an error.",
    "unknown": "Claude returned an unknown error.",
}
# What the assistant says out loud for errors that won't go away by retrying.
_SPOKEN = {
    "authentication_failed": "I'm not signed in to Claude yet. Run octavia login on the computer.",
    "billing_error": "Claude says there's a billing problem with the account.",
    "rate_limit": "I've hit the Claude usage limit for now. It resets in a few hours.",
    "connection": "I can't reach Claude right now. Check the internet connection.",
}
_API_KEY_AUTH_ERROR = "Claude rejected ANTHROPIC_API_KEY (brain.claude.auth is api_key). Check the key in .env."
_API_KEY_AUTH_SPOKEN = "Claude didn't accept the API key. Check the key in the dot env file."


_env_prepared = False


def cli_environment(config_dir: Path | None) -> dict[str, str]:
    """Environment overrides for the Claude Code CLI process."""
    env = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    if config_dir is not None:
        config_dir.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def bundled_cli_path() -> Path | None:
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if sys.platform == "win32" else "claude")
    return path if path.exists() else None


def prepare_process_environment(auth: str) -> None:
    """Process-wide setup that must happen before the first connect. Idempotent."""
    global _env_prepared
    if auth == "subscription":
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if os.environ.pop(key, None) is not None:
                log.warning(
                    "Ignoring %s so your Claude subscription is used (set brain.claude.auth: api_key to bill the API)",
                    key,
                )
    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    if _env_prepared:
        return
    _env_prepared = True
    if sys.platform == "win32":
        # The bundled claude.exe is a console program; without this a console
        # window pops up when the assistant runs under pythonw.exe.
        import anyio

        original = anyio.open_process

        async def open_process_no_window(command, **kwargs):  # noqa: ANN001, ANN202
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            return await original(command, **kwargs)

        anyio.open_process = open_process_no_window


@dataclass(eq=False)
class _Request:
    prompt: str
    picture: TurnPicture | None = None
    queue: asyncio.Queue[BrainEvent] = field(default_factory=asyncio.Queue)
    interrupted: bool = False  # also set when the consumer went away; a request not yet sent is skipped
    retried: bool = False
    early: bool = False  # sent by ``start_early``: our tool calls in it wait for ``decided``
    taken: bool = False  # an early one the turn took up (``respond``); else it may still be dropped
    decided: asyncio.Event = field(default_factory=asyncio.Event)  # an early one was taken up or dropped
    sent: bool = False  # it reached the CLI, so it's in the conversation
    created: float = field(default_factory=time.perf_counter)
    head_start_s: float | None = None  # an early one taken up: how long before the turn it began
    lookups: list[ToolsLoaded] = field(default_factory=list)  # an early one's tool lookups, told once it's taken

    def emit(self, event: BrainEvent) -> None:
        self.queue.put_nowait(event)


_RESET = object()
_STOP = object()


class ClaudeBrain(BrainProvider):
    name = "claude"
    reconnect_backoff_s = 2.0  # first retry delay after a failed connect; doubles up to 60 s

    def __init__(
        self,
        cfg: ClaudeConfig,
        tools: list[ToolSpec],
        executor: ToolExecutor,
        system_prompt: SystemPromptFactory,
        work_dir: Path,
        events: EventBus,
        resume_session_id: str | None = None,
        transport_factory: Callable[[], Any] | None = None,
        config_dir: Path | None = None,
        cli_path: str | None = None,
    ) -> None:
        """``transport_factory`` supplies an SDK ``Transport`` per connection (tests use a mock CLI).

        ``config_dir`` is a private Claude Code config directory (login, transcripts),
        keeping the assistant separate from your own Claude Code setup.
        """
        super().__init__(tools, executor, system_prompt)
        self.cfg = cfg
        self.work_dir = work_dir
        self.config_dir = config_dir
        self.cli_path = cli_path if cli_path is not None else cfg.cli_path
        self.events = events
        self._transport_factory = transport_factory
        self._requests: asyncio.Queue[Any] = asyncio.Queue()
        self._actor: asyncio.Task | None = None
        self._client = None
        self._current: _Request | None = None
        self._prompt_key: str | None = None  # the connected prompt and tools (``_cache_key``): is its cache warm?
        self._warming: asyncio.Task[None] | None = None
        self._open: set[_Request] = set()  # submitted by respond() and not finished
        self._retry: _Request | None = None
        self._ready = asyncio.Event()
        self._attempted = asyncio.Event()  # the first connect attempt finished, either way
        self._connect_error: str | None = None
        self._session_id: str | None = None
        self._resume_id = resume_session_id
        self._builtin_calls: dict[str, tuple[str, float]] = {}
        self._stall: asyncio.Timeout | None = None  # the turn in flight's stall timer
        self._tools_running = 0  # our tool calls in flight
        self._quiet_calls: set[str] = set()  # this turn's built-in tool calls (run inside the CLI) in flight
        self._early: _Request | None = None  # the early start waiting for its turn (``start_early``)
        self._cut_early = False  # an early start that reached the model was dropped: the next turn says so
        self._tasks: set[asyncio.Task] = set()  # strong refs to fire-and-forget interrupts

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        prepare_process_environment(self.cfg.auth)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._actor = asyncio.create_task(self._run_actor(), name="claude-brain")
        try:
            await asyncio.wait_for(self._attempted.wait(), self.cfg.connect_timeout_s)
        except TimeoutError:
            pass
        if not self._ready.is_set():
            log.warning("Claude is not connected yet (%s)", self._connect_error or "slow start")

    async def close(self) -> None:
        if self._warming is not None and not self._warming.done():
            self._warming.cancel()  # a warm-up is never worth holding up a shutdown
        if self._actor is None:
            return
        await self.interrupt()  # don't hold up shutdown for a reply nobody will hear
        await self._requests.put(_STOP)
        try:
            await asyncio.wait_for(asyncio.shield(self._actor), 10)
        except (TimeoutError, asyncio.CancelledError):
            self._actor.cancel()
        except Exception:
            log.debug("Brain actor ended with error", exc_info=True)
        self._actor = None
        for request in list(self._open):  # never leave a respond() waiting forever
            request.emit(ResponseDone(text="", interrupted=True))

    async def reset(self) -> None:
        """Start a fresh conversation (new session, freshly rendered system prompt)."""
        self.drop_early("starting afresh")  # it went to the conversation being left
        self._cut_early = False  # ...so the new one has no cut-off message to be told about
        await self._requests.put(_RESET)

    async def interrupt(self) -> None:
        self.drop_early("interrupted")
        for pending in self._open:
            pending.interrupted = True  # one still queued (connecting, reconnecting) is never sent
        request, client = self._current, self._client
        if request is None or client is None:
            return
        request.interrupted = True
        try:
            await asyncio.wait_for(client.interrupt(), 5)
        except Exception:
            log.warning("Claude interrupt failed", exc_info=True)

    async def abandon_turn(self) -> None:
        """A turn the interrupt didn't end (a tool that won't stop, a CLI that ignored it) is ended the way a stalled
        one is: its stall timer runs out now, and the actor reconnects into the same conversation."""
        stall = self._stall
        if self._current is None or stall is None or stall.expired():
            return
        log.warning("Giving up on a turn that wouldn't stop: reconnecting to Claude")
        stall.reschedule(asyncio.get_running_loop().time())

    # ----------------------------------------------------------------- turns
    async def start_early(self, turn: UserTurn) -> bool:
        """Send ``turn`` now, while the user is still pausing: only when Claude is connected and idle, so it never
        waits behind (or delays) anything. ``respond`` with the same rendered turn takes it up, with whatever it has
        already streamed; our tool calls wait until then, and run only if it's taken."""
        if turn.picture is not None:
            return False  # a picture sent early couldn't be unsent if the user went on to say otherwise
        busy = self._current is not None or not self._requests.empty() or self._open or self._retry is not None
        if self._early is not None or busy or not self._ready.is_set() or self._actor is None or self._actor.done():
            return False
        request = _Request(prompt=turn.render(), early=True)
        self._early = request
        self._open.add(request)  # close() and interrupt() reach it
        asyncio.get_running_loop().call_later(EARLY_TTL_S, self._expire_early, request)
        await self._requests.put(request)
        log.info("Started early on %r", turn.text[:80])
        return True

    def drop_early(self, why: str = "") -> None:
        request, self._early = self._early, None
        if request is None:
            return
        request.interrupted = True  # not voiced; never sent if it's still queued
        request.decided.set()  # a tool call waiting on it is refused
        self._open.discard(request)
        if request.sent:
            self._cut_early = True
        log.info("Dropped the early start (%s)", why or "no reason given")
        client = self._client
        if request is self._current and client is not None:
            task = asyncio.get_running_loop().create_task(self._interrupt_client(client), name="claude-drop-early")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _expire_early(self, request: _Request) -> None:
        if self._early is request:
            self.drop_early("its turn never came")

    @staticmethod
    async def _interrupt_client(client) -> None:  # noqa: ANN001
        try:
            await asyncio.wait_for(client.interrupt(), 5)
        except Exception:
            log.warning("Claude interrupt failed", exc_info=True)

    async def respond(self, turn: UserTurn) -> AsyncIterator[BrainEvent]:
        if self._actor is None or self._actor.done():
            self.drop_early("Claude isn't connected")
            yield ResponseDone(
                text="", error=self._connect_error or "Claude isn't connected.", spoken_error=_SPOKEN["connection"]
            )
            return
        prompt = turn.render()
        early = self._early
        if early is not None and early.prompt == prompt and not early.interrupted:
            self._early = None
            request = early
            request.taken = True
            request.head_start_s = time.perf_counter() - request.created
            request.decided.set()
            log.info("Answering from the early start, begun %.2f s before the turn", request.head_start_s)
            for lookup in request.lookups:  # for this turn's record (a dropped start's lookups count for none)
                self.events.emit(lookup)
            request.lookups.clear()
        else:
            if early is not None:
                self.drop_early("the turn wasn't the one started early")
            # Queue even while (re)connecting: the actor serves it once connected, or
            # fails it at once while Claude is known to be down.
            request = _Request(prompt=prompt, picture=turn.picture)
            self._open.add(request)
            await self._requests.put(request)
        try:
            while True:
                event = await request.queue.get()
                if isinstance(event, ResponseDone) and request.head_start_s is not None:
                    event = dataclasses.replace(event, head_start_s=round(request.head_start_s, 3))
                yield event
                if isinstance(event, ResponseDone):
                    return
        finally:
            self._open.discard(request)
            request.interrupted = True  # if the consumer gave up early, don't run or voice it

    # ----------------------------------------------------------------- actor
    async def _run_actor(self) -> None:
        backoff = self.reconnect_backoff_s
        while True:
            try:
                client = await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connect_error = _describe_connect_error(exc)
                log.error("Claude connection failed: %s", self._connect_error, exc_info=log.isEnabledFor(logging.DEBUG))
                self.events.emit(ErrorOccurred(source="BRAIN", message=self._connect_error))
                self._attempted.set()
                if self._resume_id:
                    # A missing or refused session would otherwise fail every reconnect forever.
                    log.warning("Couldn't resume Claude session %s; the next attempt starts a new one", self._resume_id)
                    self._resume_id = None
                if self._retry is not None:
                    self._retry.emit(
                        ResponseDone(text="", error=self._connect_error, spoken_error=_SPOKEN["connection"])
                    )
                    self._retry = None
                if await self._drain_while_down(backoff):
                    return
                backoff = min(backoff * 2, 60.0)
                continue

            backoff = self.reconnect_backoff_s
            self._connect_error = None
            self._client = client
            self._ready.set()
            self._attempted.set()
            if self._warm_due() and (self._warming is None or self._warming.done()):
                self._warming = asyncio.create_task(self._warm(), name="claude-cache-warm")
            try:
                while True:
                    if self._retry is not None:
                        item, self._retry = self._retry, None
                    else:
                        item = await self._requests.get()
                    if item is _STOP:
                        return
                    if item is _RESET:
                        self._resume_id = None
                        self._session_id = None
                        self._cut_early = False
                        break
                    if item.interrupted:  # interrupted or abandoned before it was sent
                        item.emit(ResponseDone(text="", interrupted=True))
                        continue
                    if not await self._serve(client, item):
                        # Reconnect into the same conversation (the init may not have arrived yet).
                        self._resume_id = self._session_id or self._resume_id
                        break
            finally:
                self._ready.clear()
                self._client = None
                await _disconnect(client)

    async def _drain_while_down(self, wait_s: float) -> bool:
        """While disconnected, fail queued turns quickly instead of hanging. True means stop."""
        deadline = time.monotonic() + wait_s
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                item = await asyncio.wait_for(self._requests.get(), remaining)
            except TimeoutError:
                break
            if item is _STOP:
                return True
            if item is _RESET:
                self._resume_id = None
                self._session_id = None
            elif isinstance(item, _Request):
                item.emit(ResponseDone(text="", error=self._connect_error, spoken_error=_SPOKEN["connection"]))
        return False

    async def _connect(self):
        from claude_agent_sdk import ClaudeSDKClient

        prompt_file = self.work_dir / "system_prompt.md"
        prompt = await self.system_prompt()
        prompt_file.write_text(prompt, encoding="utf-8")
        self._prompt_key = _cache_key(prompt, self.tools, self.cfg.model)
        options = self._options(prompt_file, resume=self._resume_id)
        transport = self._transport_factory() if self._transport_factory is not None else None
        client = ClaudeSDKClient(options=options, transport=transport)
        started = time.perf_counter()
        try:
            await asyncio.wait_for(client.connect(), self.cfg.connect_timeout_s)
        except BaseException:
            await _disconnect(client)
            raise
        log.info(
            "Claude connected in %.1fs (model=%s, resume=%s)",
            time.perf_counter() - started,
            self.cfg.model,
            self._resume_id,
        )
        return client

    def _options(self, prompt_file: Path, *, resume: str | None, inert: bool = False, max_turns: int | None = None):
        """The CLI's options. ``inert``: tools that only refuse (the cache warm-up must never run one), with the
        same names, descriptions and schemas, so the prompt the model reads (and its cache) is the same."""
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        builtins = [t for t in self.cfg.builtin_tools if t]
        env = cli_environment(self.config_dir)
        if self._on_demand:
            builtins.append(TOOL_SEARCH)
            env["ENABLE_TOOL_SEARCH"] = "true"  # defer whatever isn't always loaded, however small the total
        server = self._mcp_server(create_sdk_mcp_server, inert=inert)
        return ClaudeAgentOptions(
            model=self.cfg.model,
            fallback_model=self.cfg.fallback_model,
            system_prompt={"type": "file", "path": str(prompt_file)},
            tools=builtins,
            allowed_tools=[f"{TOOL_PREFIX}*", *builtins],
            permission_mode="dontAsk",
            mcp_servers={SERVER_KEY: server},
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            include_partial_messages=True,
            verbatim_prompts=True,
            max_turns=max_turns or self.cfg.max_turns,
            effort=self.cfg.effort,
            cwd=str(self.work_dir),
            resume=resume,
            # By default the CLI records the system prompt once per conversation and reuses the record on
            # resume. Off: a resumed conversation gets the current prompt (after an update, a new memory...).
            extra_args={"system-prompt-snapshot": "off"},
            env=env,
            cli_path=self.cli_path,
            max_buffer_size=MAX_MESSAGE_BYTES,
            stderr=lambda line: log.debug("claude-cli: %s", line.rstrip()),
        )

    @property
    def _on_demand(self) -> bool:
        return self.cfg.tools_on_demand and bool(self.tools)

    def _tool_meta(self, spec: ToolSpec) -> dict[str, Any]:
        """What Claude Code's tool search reads from a tool's ``_meta``: its group, and whether it's always loaded."""
        if not self._on_demand:
            return {}
        meta: dict[str, Any] = {}
        if spec.category:
            meta["anthropic/searchHint"] = spec.category.replace("_", " ")
        if any(fnmatch.fnmatchcase(spec.name, pattern) for pattern in self.cfg.always_loaded_tools):
            meta["anthropic/alwaysLoad"] = True
        return meta

    def _mcp_server(self, create_sdk_mcp_server: Callable[..., Any], *, inert: bool = False) -> Any:
        import claude_agent_sdk

        tools = [self._inert_tool(s) if inert else self._sdk_tool(s) for s in self.tools]
        metas = {s.name: self._tool_meta(s) for s in self.tools}
        if self._on_demand:
            names = [s.name for s in self.tools]
            unmatched = [p for p in self.cfg.always_loaded_tools if not fnmatch.filter(names, p)]
            # A typo here only shows as a slower first use of that tool, so say it: on her main brain, which carries
            # more tools than the list names. A brain of her side work (the nightly reflection, a background task)
            # has a few of its own, and most of the list isn't among them by design.
            if unmatched and len(names) > len(self.cfg.always_loaded_tools):
                log.warning("brain.claude.always_loaded_tools names no tool: %s", unmatched)
        build_meta = getattr(claude_agent_sdk, "_build_meta", None)
        if not any(metas.values()):
            return create_sdk_mcp_server(name=SERVER_KEY, version="1.0.0", tools=tools)
        if build_meta is None:  # the SDK changed: everything is deferred, which still works, one search slower
            log.warning("Couldn't mark tools as always loaded (claude_agent_sdk._build_meta is gone)")
            return create_sdk_mcp_server(name=SERVER_KEY, version="1.0.0", tools=tools)

        def with_ours(tool_def: Any) -> dict[str, Any] | None:
            return {**(build_meta(tool_def) or {}), **metas.get(tool_def.name, {})} or None

        # The SDK builds each tool's _meta while creating the server, so swapping its builder for that call is enough.
        claude_agent_sdk._build_meta = with_ours
        try:
            return create_sdk_mcp_server(name=SERVER_KEY, version="1.0.0", tools=tools)
        finally:
            claude_agent_sdk._build_meta = build_meta

    @staticmethod
    def _inert_tool(spec: ToolSpec):
        from claude_agent_sdk import SdkMcpTool

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "Not run: this was only a warm-up."}], "is_error": True}

        return SdkMcpTool(name=spec.name, description=spec.description, input_schema=spec.input_schema, handler=handler)

    # ----------------------------------------------------------------- warming the prompt cache
    def _warm_due(self) -> bool:
        """Whether the model's prompt cache is likely cold for the prompt just connected with: no turn or warm-up
        with this same prompt and tools (``_prompt_key``) within the cache's hour, by this run or an earlier one."""
        if not self.cfg.warm_cache or self._prompt_key is None or self._transport_factory is not None:
            return False
        last = _read_warm(self.work_dir / WARM_FILE).get(self._prompt_key)
        return not isinstance(last, (int, float)) or time.time() - last > WARM_AFTER_S

    def _warmed(self) -> None:
        """This prompt was just read by the model (a turn or a warm-up): its cache is warm for the next hour."""
        if self._prompt_key is None:
            return
        path = self.work_dir / WARM_FILE
        now = time.time()
        seen = {k: v for k, v in _read_warm(path).items() if isinstance(v, (int, float)) and now - v < 2 * WARM_AFTER_S}
        seen[self._prompt_key] = now
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(seen), encoding="utf-8")

    async def _warm(self) -> None:
        """One tiny request with the same system prompt and tools, so the user's next turn after a restart or a fresh
        start reads them from the model's cache (cold first turns took 3 to 4.4 s to the first word against about
        1.8 s; 2026-10-25 review). Never in a turn's way, within the usage budget, and its tools only refuse."""
        from claude_agent_sdk import ResultMessage, query

        if (why := claude_usage.check(WARM_JOB)) is not None:
            log.info("Not warming the prompt cache: %s", why)
            return
        if self._current is not None or not self._requests.empty():
            return  # a turn is already warming it
        started = time.perf_counter()
        try:
            options = self._options(self.work_dir / "system_prompt.md", resume=None, inert=True, max_turns=1)
            async with claude_usage.charged(WARM_JOB):
                async for message in query(prompt=WARM_PROMPT, options=options):
                    if isinstance(message, ResultMessage):
                        usage = message.usage or {}
                        log.info(
                            "Warmed the prompt cache in %.1fs (%s tokens written, %s read)",
                            time.perf_counter() - started,
                            usage.get("cache_creation_input_tokens"),
                            usage.get("cache_read_input_tokens"),
                        )
            self._warmed()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("Warming the prompt cache failed", exc_info=True)

    def _sdk_tool(self, spec: ToolSpec):
        from claude_agent_sdk import SdkMcpTool

        brain, executor = self, self.executor

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            brain._tools_running += 1
            brain._rearm()
            try:
                request = brain._current
                if request is not None and request.early and not request.taken:
                    await request.decided.wait()  # nothing runs on a guess: only once it's the user's turn
                    if not request.taken:
                        refused = [{"type": "text", "text": "Not run: the user was still talking."}]
                        return {"content": refused, "is_error": True}
                result = await executor.execute(spec.name, args)
            finally:
                brain._tools_running -= 1
                brain._rearm()
            content = [{"type": "text", "text": result.content}]
            if (image := _image_content(result)) is not None:
                content.append(image)
            return {"content": content, "is_error": not result.ok}

        return SdkMcpTool(name=spec.name, description=spec.description, input_schema=spec.input_schema, handler=handler)

    async def _serve(self, client, request: _Request) -> bool:  # noqa: ANN001
        """Run one turn. Returns False if the connection is broken."""
        from claude_agent_sdk import (
            AssistantMessage,
            CLIConnectionError,
            ProcessError,
            RateLimitEvent,
            ResultMessage,
            ServerToolUseBlock,
            StreamEvent,
            SystemMessage,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        self._current = request
        parts: list[str] = []
        model: str | None = None  # the model the CLI says wrote this turn's messages
        error: str | None = None
        spoken: str | None = None
        done: ResponseDone | None = None
        carried: int | None = None  # the whole input of this turn's latest model call (``ResponseDone.context_tokens``)
        carried_first: int | None = None  # ...and of its first (``ResponseDone.first_context_tokens``)
        began = False  # the model began working on this turn (so it must not be re-sent)
        announced: set[str] = set()  # ids of the tool calls this turn already sent a ToolUseStarted for

        def announce(call_id: str, name: str, server: bool) -> None:
            # A call shows up twice when streaming (its content_block_start, then the whole message) but only in
            # the message when the CLI fell back to a non-streaming request, so say it once, from whichever is first.
            if call_id in announced or name == TOOL_SEARCH:
                return
            if request.early and not request.taken and (server or not name.startswith(TOOL_PREFIX)):
                # One of Claude's own tools (a web search) runs inside the CLI, where nothing holds it until the
                # turn: the early start is given up before it runs, and the turn asks afresh.
                if self._early is request:
                    self.drop_early(f"it reached for {name} before the user had finished")
                return
            announced.add(call_id)
            builtin = server or not name.startswith(TOOL_PREFIX)  # after _short() the name no longer tells
            request.emit(ToolUseStarted(call_id=call_id, name=_short(name), builtin=builtin))

        # Where the wait before her first word goes (logged once per turn): the request reaching the model, its
        # thinking, its first text. Thinking is silence the user hears, so it's the first thing to look at.
        sent = time.perf_counter()
        marks: dict[str, float] = {}

        def mark(name: str) -> None:
            if name not in marks:
                marks[name] = time.perf_counter() - sent

        # A CLI that goes quiet mid-turn (a model request that never answers) would leave her thinking forever.
        stall = asyncio.timeout(None)
        self._stall, self._quiet_calls = stall, set()
        try:
            async with stall:
                self._rearm()
                prompt = request.prompt
                if self._cut_early:
                    self._cut_early = False
                    prompt = _CUT_NOTE + prompt
                request.sent = True  # from the moment it's written: a drop during the write still counts it
                await client.query(prompt if request.picture is None else _with_picture(prompt, request.picture))
                if request.interrupted:
                    # interrupt() raced ahead of the prompt; the CLI would have ignored it.
                    await client.interrupt()
                async for msg in client.receive_response():
                    began = began or isinstance(msg, (StreamEvent, AssistantMessage, UserMessage, ResultMessage))
                    if isinstance(msg, StreamEvent):
                        ev = msg.event
                        kind = ev.get("type")
                        if kind == "message_start":
                            mark("start")
                            if not msg.parent_tool_use_id:
                                carried = context_tokens((ev.get("message") or {}).get("usage")) or carried
                                carried_first = carried_first or carried
                        elif kind == "content_block_start":
                            mark(str(ev.get("content_block", {}).get("type", "block")))
                        if kind == "content_block_delta":
                            delta = ev.get("delta", {})
                            if delta.get("type") == "text_delta" and not request.interrupted:
                                text = delta.get("text", "")
                                if text:
                                    parts.append(text)
                                    request.emit(TextDelta(text))
                        elif kind == "content_block_start":
                            block = ev.get("content_block", {})
                            if block.get("type") == "text" and parts and not request.interrupted:
                                # A new text block after a tool call: keep sentences apart.
                                parts.append("\n")
                                request.emit(TextDelta("\n"))
                            elif block.get("type") in ("tool_use", "server_tool_use"):
                                server = block.get("type") == "server_tool_use"
                                announce(block.get("id", ""), block.get("name", ""), server)
                    elif isinstance(msg, AssistantMessage):
                        model = msg.model or model
                        if not msg.parent_tool_use_id:
                            carried = context_tokens(msg.usage) or carried
                            carried_first = carried_first or carried
                        if msg.error:
                            error, spoken = self._describe_error(msg.error)
                        for block in msg.content:
                            # Streaming or not, the CLI sends this message before it runs any tool call in it.
                            if isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                                announce(block.id, block.name, server=isinstance(block, ServerToolUseBlock))
                            if isinstance(block, ToolUseBlock) and block.name == TOOL_SEARCH:
                                query = str((block.input or {}).get("query", ""))
                                log.info("Loading tools on demand: %s", query)
                                lookup = ToolsLoaded(query=query, names=searched_names(query), early=request.early)
                                if request.early and not request.taken:
                                    request.lookups.append(lookup)
                                elif not request.interrupted:  # a cut-off turn's would land on the next one
                                    self.events.emit(lookup)
                            elif isinstance(block, ToolUseBlock) and not block.name.startswith(TOOL_PREFIX):
                                if request.early and not request.taken:
                                    continue  # dropped above: interrupted before it runs, and not announced
                                self._builtin_calls[block.id] = (block.name, time.perf_counter())
                                self._quiet_calls.add(block.id)
                                self.events.emit(
                                    ToolCallStarted(call_id=block.id, name=block.name, args=_short_args(block.input))
                                )
                    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                self._quiet_calls.discard(block.tool_use_id)
                            if isinstance(block, ToolResultBlock) and block.tool_use_id in self._builtin_calls:
                                name, started = self._builtin_calls.pop(block.tool_use_id)
                                self.events.emit(
                                    ToolCallFinished(
                                        call_id=block.tool_use_id,
                                        name=name,
                                        ok=not block.is_error,
                                        summary="error" if block.is_error else "ok",
                                        duration_s=time.perf_counter() - started,
                                    )
                                )
                    elif isinstance(msg, SystemMessage) and msg.subtype == "init":
                        self._session_id = msg.data.get("session_id") or self._session_id
                        self._check_isolation(msg.data)
                    elif isinstance(msg, RateLimitEvent):
                        reading = claude_usage.observe(msg.rate_limit_info)  # your limits, for her budget
                        status = getattr(msg.rate_limit_info, "status", None)
                        if reading is not None and reading.paid_past_limit:
                            pass  # past a limit, but extra usage pays for it: the turn goes on
                        elif status == "allowed_warning":
                            self.events.emit(
                                Notice(
                                    source="BRAIN",
                                    message="Heads up: close to the Claude usage limit",
                                    level=logging.WARNING,
                                )
                            )
                        elif status == "rejected":
                            error, spoken = self._describe_error("rate_limit")
                    elif isinstance(msg, ResultMessage):
                        self._session_id = msg.session_id or self._session_id
                        if not msg.is_error:
                            self._warmed()  # this prompt is in the model's cache for the next hour
                        interrupted = request.interrupted or msg.terminal_reason in (
                            "aborted_streaming",
                            "aborted_tools",
                        )
                        if msg.subtype == "error_max_turns" and not interrupted and error is None:
                            # Not a failure: a long task (working through a problem set, say) used its steps. Say so, and
                            # the next "keep going" carries on from here in the same conversation.
                            log.info("Stopped at the step limit (brain.claude.max_turns = %d)", self.cfg.max_turns)
                            paused = f" {STEP_LIMIT_PAUSE}" if parts else STEP_LIMIT_PAUSE
                            parts.append(paused)
                            request.emit(TextDelta(paused))
                        elif msg.is_error and not interrupted and error is None:
                            error = _describe_result(msg)
                        done = ResponseDone(
                            text="".join(parts).strip(),
                            interrupted=interrupted,
                            error=error,
                            session_id=self._session_id,
                            usage=msg.usage,
                            # The result's usage adds up all the turn's calls: it's what was carried only for one.
                            context_tokens=carried or (context_tokens(msg.usage) if msg.num_turns == 1 else None),
                            first_context_tokens=carried_first,
                            spoken_error=spoken if error else None,
                            model=model,
                        )
                    self._rearm()
            if marks:
                log.info(
                    "Brain timing: %s",
                    ", ".join(f"{name} {at:.2f}s" for name, at in sorted(marks.items(), key=lambda kv: kv[1])),
                )
            if done is None:
                # The message stream ended without a result: the CLI went away.
                raise CLIConnectionError("the Claude Code CLI stopped")
            request.emit(done)
            return True
        except TimeoutError:
            if not stall.expired():
                raise
            log.error("Claude stopped answering: nothing from the CLI for %.0f s mid-turn", self.cfg.stall_timeout_s)
            request.emit(
                ResponseDone(
                    text="".join(parts).strip(),
                    interrupted=request.interrupted,
                    error=None if request.interrupted else "Claude stopped answering, so I gave up on that turn.",
                    spoken_error=None if request.interrupted else "Claude stopped answering me. Try that again.",
                )
            )
            return False  # the CLI is stuck: reconnect into the same conversation
        except (CLIConnectionError, ProcessError) as exc:
            if not began and not request.retried and not request.interrupted:
                # It died between turns (e.g. exited after an error result): nothing of
                # this turn ran, so reconnect and send it again.
                log.warning("Claude connection was down (%s); reconnecting to retry the turn", exc)
                request.retried = True
                self._retry = request
                return False
            log.error("Claude connection lost: %s", exc)
            request.emit(
                ResponseDone(
                    text="".join(parts).strip(),
                    error=f"Lost the connection to Claude ({exc}).",
                    spoken_error="I lost the connection to Claude. Try that again.",
                )
            )
            return False
        except Exception as exc:
            log.exception("Claude turn failed")
            request.emit(ResponseDone(text="".join(parts).strip(), error=f"{type(exc).__name__}: {exc}"))
            return False  # state of the stream is unknown: reconnect to be safe
        finally:
            self._current = None
            self._stall = None

    def _rearm(self) -> None:
        """Restart the stall timer: stopped while a tool runs (the CLI is silent then, and every tool has its own
        time limit), else ``stall_timeout_s`` from now. Also the turn's sign of life for the assistant's watchdog."""
        if self.heartbeat is not None:
            try:
                self.heartbeat()
            except Exception:
                log.debug("Heartbeat failed", exc_info=True)
        stall = self._stall
        if stall is None or stall.expired():
            return
        busy = self._tools_running or self._quiet_calls
        stall.reschedule(None if busy else asyncio.get_running_loop().time() + self.cfg.stall_timeout_s)

    def _describe_error(self, kind: str) -> tuple[str, str | None]:
        """(message for logs/console, sentence to say out loud) for an SDK error kind."""
        if kind == "authentication_failed" and self.cfg.auth == "api_key":
            return _API_KEY_AUTH_ERROR, _API_KEY_AUTH_SPOKEN
        return _ERRORS.get(kind, f"Claude error: {kind}"), _SPOKEN.get(kind)

    def _check_isolation(self, data: dict[str, Any]) -> None:
        servers = data.get("mcp_servers") or []
        names = {s.get("name") if isinstance(s, dict) else str(s) for s in servers}
        unexpected = names - {SERVER_KEY}
        if unexpected:
            log.warning("Unexpected MCP servers attached to the brain: %s", sorted(unexpected))
        tools = data.get("tools") or []
        expected = {*self.cfg.builtin_tools, *([TOOL_SEARCH] if self._on_demand else [])}
        stray = [t for t in tools if not str(t).startswith(TOOL_PREFIX) and t not in expected]
        if stray:
            log.warning("Unexpected tools available to the brain: %s", stray)


def searched_names(query: str) -> tuple[str, ...]:
    """The tools a tool-search query names (``select:mcp__assistant__a,b``), short names; () for a keyword search."""
    if not query.startswith("select:"):
        return ()
    return tuple(_short(n.strip()) for n in query.removeprefix("select:").split(",") if n.strip())


def _short(name: str) -> str:
    return name[len(TOOL_PREFIX) :] if name.startswith(TOOL_PREFIX) else name


def context_tokens(usage: Any) -> int | None:
    """A model call's whole input from its usage: fresh, read from the cache and newly cached tokens (None when it
    doesn't say)."""
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    values = [usage.get(k) for k in keys]
    if not any(isinstance(v, int | float) for v in values):
        return None
    return int(sum(v for v in values if isinstance(v, int | float)))


async def _with_picture(prompt: str, picture: TurnPicture) -> AsyncIterator[dict[str, Any]]:
    """The user's message with a picture before their words, as the CLI takes it."""
    source = {"type": "base64", "media_type": picture.mime_type, "data": picture.data}
    content = [{"type": "image", "source": source}, {"type": "text", "text": prompt}]
    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}


def _image_content(result: ToolResult) -> dict[str, Any] | None:
    """The picture a tool attached for the model to look at, as MCP image content.

    ``ToolResult.content`` is text only, so a tool (e.g. a screenshot) attaches an image as
    ``data={"image": {"data": <base64>, "mime_type": "image/png"}}``.
    """
    image = (result.data or {}).get("image")
    if not isinstance(image, dict) or not isinstance(image.get("data"), str) or not image.get("mime_type"):
        return None
    return {"type": "image", "data": image["data"], "mimeType": str(image["mime_type"])}


def _short_args(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    return {k: (v if not isinstance(v, str) or len(v) < 80 else v[:77] + "...") for k, v in args.items()}


def _describe_result(msg: Any) -> str:
    if msg.subtype == "error_max_turns":
        return "That took too many steps, so I stopped."
    if msg.api_error_status:
        return f"Claude API error {msg.api_error_status}."
    errors = "; ".join(msg.errors or [])
    return f"Claude couldn't finish that ({msg.subtype}{': ' + errors if errors else ''})."


def _describe_connect_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "CLINotFoundError":
        return "Couldn't start the Claude Code CLI (not found, or the command line was too long)."
    if isinstance(exc, TimeoutError):
        return "Timed out starting the Claude Code CLI."
    return f"{name}: {exc}"


async def _disconnect(client) -> None:  # noqa: ANN001
    try:
        await asyncio.wait_for(client.disconnect(), 10)
    except Exception:  # not BaseException: a cancelled shutdown must stay cancelled
        log.debug("Claude disconnect error", exc_info=True)


def _cache_key(prompt: str, tools: Sequence[ToolSpec], model: str) -> str:
    """What the model's prompt cache is keyed by, as far as she controls it: the system prompt, the tools, the model."""
    digest = hashlib.sha256(model.encode())
    digest.update(prompt.encode("utf-8"))
    for spec in tools:
        digest.update(
            json.dumps([spec.name, spec.description, spec.input_schema], sort_keys=True, default=str).encode()
        )
    return digest.hexdigest()[:16]


def _read_warm(path: Path) -> dict[str, Any]:
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return seen if isinstance(seen, dict) else {}
