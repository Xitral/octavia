"""TickTick: the user's tasks, lists, habits, focus sessions and countdowns, through TickTick's own MCP server.

TickTick runs an official MCP server (https://mcp.ticktick.com). Rather than hand the brain that server directly
(its tools would then run inside Claude Code, outside the permission policy, and could delete tasks without a
question), each of its tools becomes one of ours, ``ticktick_<its name>``, and every call goes through the executor
like any other tool: reading and adding never ask, changing and deleting follow ``config.ticktick`` (see
``permission_rules``), and the avatar holds up her magnifying glass while she looks things up.

The server has over fifty tools, and every tool's description goes to Claude with every request. So the ones used
day to day (``CORE``: tasks, lists, habits, focus, countdowns) are tools of their own, and the rest (kanban
columns, comments, tags, list groups, bulk edits...) are reached through one, ``ticktick_more``, which names them
and gives a tool's arguments when asked.

Which tools the server offers is read from it (``tools/list``) and kept in ``data/ticktick/tools.json``, so a start
never waits on the network once that file exists; each start refreshes it in the background for the next one.

The server authenticates with a personal API token the user makes in TickTick (Settings, Account & Security, API
Token). Like the other keys it comes from the environment (``.env``: ``TICKTICK_API_TOKEN``), never from YAML, and
is never put in a tool result or a log line.

A call that created, changed, ticked off, deleted or moved to-dos is also told on the event bus, one ``TaskChanged``
per task, for Octavia Futures: the only place the id of a task she creates is known. Only a few fields are picked out
of the call and its answer (the id, list, title, due date, priority), never the answer's text.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from octavia import keys
from octavia.config import find_project_root
from octavia.core.events import TaskChanged
from octavia.core.jsonfile import read_json, write_json
from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult, _inline_schema

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.tools.permissions import Decision
    from octavia.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

TOKEN_ENV = "TICKTICK_API_TOKEN"
DEFAULT_URL = "https://mcp.ticktick.com"
PREFIX = "ticktick_"
# The brain sees our tools as "mcp__assistant__<name>", and Claude caps a tool name at 64 characters.
MAX_NAME = 48
ROOT = find_project_root()  # her folder (OCTAVIA_HOME, or this checkout), like every other data/ path
CATALOG = ROOT / "data" / "ticktick" / "tools.json"
CONNECT_TIMEOUT_S = 15.0
CALL_TIMEOUT_S = 30.0
IDLE_REOPEN_S = 240.0  # a session unused this long is opened afresh first: servers drop idle ones
LIST_TIMEOUT_S = 8.0  # a first start (no catalog yet) waits this long for the server at most
RESULT_CHARS = 12_000  # a long answer is cut: every character goes to Claude with each later turn
ABOUT = "about"  # the argument we add to every tool: what the call is about, in words, for the question
# The server's tools that are tools of their own; any others go through ticktick_more.
CORE = frozenset(
    {
        "list_projects",
        "get_project_with_undone_tasks",
        "list_undone_tasks_by_time_query",
        "list_undone_tasks_by_date",
        "list_completed_tasks_by_date",
        "filter_tasks",
        "search_task",
        "get_task_by_id",
        "create_task",
        "update_task",
        "complete_task",
        "delete_task",
        "move_task",
        "list_habits",
        "get_habit_checkins",
        "upsert_habit_checkins",
        "get_focuses_by_time",
        "list_countdowns",
    }
)
MORE = PREFIX + "more"
READER = "get_task_by_id"  # how an update is checked against what TickTick saved

SETUP_HINT = keys.hint(TOKEN_ENV)


def setup_hint(config: AppConfig | None = None) -> str:
    """How to connect TickTick, naming the .env file checked and why the token looks missing (``keys.missing``)."""
    return keys.missing(TOKEN_ENV, cfg=config)


Kind = Literal["read", "create", "change", "delete"]

# What a tool does, from its name: the first word that says so wins ("get_task_by_id" reads, "batch_delete_tasks"
# deletes). Deleting outranks everything, then changing, adding, reading.
_DELETE = re.compile(r"(?:^|_)(?:delete|remove|trash|purge|clear|archive|destroy|drop|erase|wipe)(?:_|$)")
_CHANGE = re.compile(
    r"(?:^|_)(?:update|edit|set|complete|uncomplete|reopen|undo|move|rename|change|mark|check|checkin|uncheck|"
    r"toggle|pin|unpin|assign|reorder|sort|convert|merge|restore|snooze|postpone|reschedule|start|stop|pause|"
    r"resume|finish|end|abandon|skip|modify|patch|put)(?:_|$)"
)
_CREATE = re.compile(r"(?:^|_)(?:create|add|new|insert|make|log|record|save|upload|duplicate|copy)(?:_|$)")
_READ = re.compile(
    r"(?:^|_)(?:get|list|search|find|query|filter|fetch|read|show|view|lookup|look|count|stats|statistics|summary|"
    r"summarize|overview|export|download|check_status)(?:_|$)"
)


def categorize(name: str, annotations: dict[str, Any] | None = None) -> Kind:
    """What calling this tool does to the user's TickTick, judged from its name and the server's own hints.

    The name decides first: a tool called delete-something deletes whatever its hints say. A tool the server says
    only reads, whose name doesn't say otherwise, reads. Anything unclear counts as a change (asked about, by
    default), never as a harmless read: a new tool can't slip past a question for lack of a familiar name.
    """
    lowered = name.lower()
    hints = annotations or {}
    if _DELETE.search(lowered):
        return "delete"
    if _CHANGE.search(lowered):
        return "change"
    if _CREATE.search(lowered):
        return "create"
    if _READ.search(lowered) or hints.get("readOnlyHint") is True:
        return "read" if hints.get("destructiveHint") is not True else "change"
    return "delete" if hints.get("destructiveHint") is True and hints.get("readOnlyHint") is not True else "change"


def local_name(remote: str) -> str:
    """``ticktick_<remote>``, lower case, only letters, digits and underscores, and short enough for Claude."""
    tidy = re.sub(r"[^a-z0-9_]+", "_", remote.lower()).strip("_") or "tool"
    name = PREFIX + tidy
    if len(name) > MAX_NAME:
        digest = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:6]
        name = name[: MAX_NAME - 7].rstrip("_") + "_" + digest
    return name


@dataclass(frozen=True)
class RemoteTool:
    """One tool of TickTick's server, as its ``tools/list`` describes it."""

    name: str
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> Kind:
        return categorize(self.name, self.annotations)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": self.annotations,
        }

    @classmethod
    def from_json(cls, data: Any) -> RemoteTool | None:
        if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"].strip():
            return None
        schema = data.get("inputSchema")
        annotations = data.get("annotations")
        return cls(
            name=data["name"].strip(),
            description=str(data.get("description") or ""),
            schema=schema if isinstance(schema, dict) else {},
            annotations=annotations if isinstance(annotations, dict) else {},
        )


# ---------------------------------------------------------------------------------------------------- the server


class TickTickError(RuntimeError):
    """TickTick's server couldn't be reached, refused the token, or the call failed on its side."""


@dataclass(frozen=True)
class Answer:
    """What a tool call returned: its text (the text blocks, or the structured content as JSON) and whether it
    worked."""

    text: str
    ok: bool = True


def _field(obj: Any, name: str, wire: str) -> Any:
    """An MCP object's field: the library's own (snake_case) name, or the wire name some versions use."""
    value = getattr(obj, name, None)
    return getattr(obj, wire, None) if value is None else value


def _answer(result: Any) -> Answer:
    parts = [getattr(block, "text", "") for block in getattr(result, "content", None) or ()]
    text = "\n".join(p for p in parts if p).strip()
    structured = _field(result, "structured_content", "structuredContent")
    if not text and structured is not None:
        text = json.dumps(structured, ensure_ascii=False, default=str)
    return Answer(text=text or "(no answer)", ok=not _field(result, "is_error", "isError"))


def _describe_failure(exc: BaseException) -> str:
    """The innermost error of an exception group (anyio wraps transport errors in several), in a few words."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc) or type(exc).__name__
    if "401" in text or "403" in text or "unauthori" in text.lower():
        return f"TickTick refused the token (check {TOKEN_ENV} in {keys.env_file() or '.env'})"
    return f"{type(exc).__name__}: {text[:200]}"


Connect = Callable[[str, dict[str, str]], Any]  # (url, headers) -> async context manager yielding a session


def _retrieved(future: asyncio.Future[Any]) -> None:
    """Mark a failed session's error as seen. When whoever waited for the session gave up first (a timeout, a
    cancelled call), the error lands with nobody waiting, and asyncio would log it as never retrieved; the failure
    is already handled by closing the session, and the next call opens a fresh one."""
    if not future.cancelled():
        future.exception()


@contextlib.asynccontextmanager
async def _mcp_session(url: str, headers: dict[str, str]):  # noqa: ANN202 (an async context manager)
    """An initialised MCP session to ``url`` over Streamable HTTP."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


class TickTickClient:
    """One session with TickTick's server, opened on first use and kept open; opened again (once) when a call
    finds the old one gone. The token is read from the environment each time a session opens, so a token added to
    ``.env`` later works without a restart."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        token: Callable[[], str | None] | None = None,
        connect: Connect | None = None,
    ) -> None:
        self.url = url
        self._token = token or (lambda: keys.value(TOKEN_ENV))
        self._connect = connect or _mcp_session
        self._session: Any = None
        self._holder: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self._last_used = 0.0

    @property
    def configured(self) -> bool:
        return bool((self._token() or "").strip())

    async def list_tools(self) -> list[RemoteTool]:
        result = await self._with_session(lambda s: s.list_tools(), retry=True)
        tools = []
        for tool in getattr(result, "tools", None) or ():
            annotations = getattr(tool, "annotations", None)
            data = {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", "") or "",
                "inputSchema": _field(tool, "input_schema", "inputSchema") or {},
                # By the wire names ("readOnlyHint"), which is how the server writes them and categorize reads them.
                "annotations": (
                    annotations.model_dump(by_alias=True, exclude_none=True) if annotations is not None else {}
                ),
            }
            if (remote := RemoteTool.from_json(data)) is not None:
                tools.append(remote)
        return tools

    async def call(self, name: str, arguments: dict[str, Any], *, retry: bool = False) -> Answer:
        """Call one of the server's tools. ``retry`` once on a fresh session when it fails: only for reads, since a
        change that failed on the way back may have gone through, and doing it again could add a task twice."""
        result = await self._with_session(lambda s: s.call_tool(name, arguments), retry=retry)
        return _answer(result)

    async def _with_session(self, use: Callable[[Any], Awaitable[Any]], *, retry: bool) -> Any:
        for attempt in (1, 2):
            session = await self._open()
            try:
                result = await asyncio.wait_for(use(session), CALL_TIMEOUT_S)
            except TimeoutError:
                await self.close()
                raise TickTickError("TickTick took too long to answer") from None
            except Exception as exc:
                await self.close()  # a dropped connection or an expired session: the next call starts afresh
                if attempt == 2 or not retry:
                    raise TickTickError(_describe_failure(exc)) from None
                log.debug("TickTick call failed; reconnecting", exc_info=True)
            else:
                self._last_used = time.monotonic()
                return result
        raise AssertionError("unreachable")

    async def _open(self) -> Any:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            alive = self._session is not None and self._holder is not None and not self._holder.done()
            if alive and time.monotonic() - self._last_used < IDLE_REOPEN_S:
                return self._session
            if alive:
                await self._close_now()  # idle a while: the server may have dropped it, so don't bet a change on it
            token = (self._token() or "").strip()
            if not token:
                raise TickTickError(setup_hint())
            ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            ready.add_done_callback(_retrieved)
            self._stop = asyncio.Event()
            # The transport's task groups must be entered and left by one task, so one task holds the session.
            self._holder = asyncio.create_task(self._hold(token, ready, self._stop), name="ticktick-session")
            try:
                self._session = await asyncio.wait_for(asyncio.shield(ready), CONNECT_TIMEOUT_S)
            except TimeoutError:
                await self._close_now()
                raise TickTickError("TickTick didn't answer in time") from None
            except Exception as exc:
                await self._close_now()
                raise TickTickError(_describe_failure(exc)) from None
            self._last_used = time.monotonic()
            return self._session

    async def _hold(self, token: str, ready: asyncio.Future[Any], stop: asyncio.Event) -> None:
        session = None
        try:
            async with self._connect(self.url, {"Authorization": f"Bearer {token}"}) as session:
                if not ready.done():
                    ready.set_result(session)
                await stop.wait()
        except BaseException as exc:  # noqa: BLE001 - handed to whoever waits for the session, never raised here
            if not ready.done():
                ready.set_exception(exc if isinstance(exc, Exception) else TickTickError("cancelled"))
            elif not isinstance(exc, asyncio.CancelledError):
                log.debug("TickTick session ended: %s", _describe_failure(exc))
        finally:
            if self._session is session:  # not a newer session opened since
                self._session = None

    async def close(self) -> None:
        if self._lock is None:
            await self._close_now()
            return
        async with self._lock:
            await self._close_now()

    async def _close_now(self) -> None:
        holder, self._holder = self._holder, None
        self._session = None
        if self._stop is not None:
            self._stop.set()
        if holder is not None and not holder.done():
            try:
                await asyncio.wait_for(asyncio.shield(holder), 5.0)
            except (TimeoutError, Exception):
                holder.cancel()
                with contextlib.suppress(BaseException):
                    await holder


_client: TickTickClient | None = None


def shared_client() -> TickTickClient:
    """The session every TickTick tool (and the reminders) shares."""
    global _client
    if _client is None:
        _client = TickTickClient()
    return _client


# ---------------------------------------------------------------------------------------------------- catalog


def read_catalog(path: Path = CATALOG) -> list[RemoteTool] | None:
    """The tools TickTick offered last time, or None when never read (or unreadable)."""
    data = read_json(path)
    if not isinstance(data, list):
        return None
    tools = [t for t in (RemoteTool.from_json(item) for item in data) if t is not None]
    return tools or None


def write_catalog(tools: Sequence[RemoteTool], path: Path = CATALOG) -> None:
    write_json(path, [t.to_json() for t in tools], indent=1)


def fetch_catalog(url: str = DEFAULT_URL, timeout_s: float = LIST_TIMEOUT_S) -> list[RemoteTool]:
    """The server's tools, now. Blocking: it runs its own event loop on a thread of its own, so it can be called
    while the app's loop is running (tool modules register synchronously). Raises TickTickError."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        async def listed() -> list[RemoteTool]:
            client = TickTickClient(url)
            try:
                return await client.list_tools()
            finally:
                await client.close()

        try:
            outcome["tools"] = asyncio.run(asyncio.wait_for(listed(), timeout_s))
        except TimeoutError:
            outcome["error"] = TickTickError("TickTick didn't answer in time")
        except Exception as exc:
            outcome["error"] = exc if isinstance(exc, TickTickError) else TickTickError(_describe_failure(exc))

    worker = threading.Thread(target=run, name="ticktick-catalog", daemon=True)
    worker.start()
    worker.join(timeout_s + 2)
    if "tools" in outcome:
        return outcome["tools"]
    raise outcome.get("error") or TickTickError("TickTick didn't answer in time")


def _refresh_later(url: str) -> None:
    """Read the server's tools again in the background, for the next start (this one keeps what it registered)."""

    def refresh() -> None:
        try:
            tools = fetch_catalog(url)
        except TickTickError as exc:
            log.info("Couldn't refresh the TickTick tools: %s", exc)
            return
        known = read_catalog()
        write_catalog(tools)
        if known is not None and {t.name for t in known} != {t.name for t in tools}:
            log.info("TickTick's tools changed; the new list is used after the next restart")

    threading.Thread(target=refresh, name="ticktick-refresh", daemon=True).start()


# ---------------------------------------------------------------------------------------------------- tools


class _Passthrough(BaseModel):
    """Arguments exactly as the model gives them: TickTick's server checks them against its own schema."""

    model_config = ConfigDict(extra="allow")


_VERBS = {"read": "look up", "create": "add", "change": "change", "delete": "delete"}


class TickTickTool(Tool):
    """One of TickTick's tools. Subclassed per tool by ``make_tool``."""

    remote: ClassVar[RemoteTool]
    kind: ClassVar[Kind]
    reader: ClassVar[RemoteTool | None] = None  # update_task's: get_task_by_id, to check what was saved
    Args = _Passthrough
    category = "tasks"
    timeout_s = CALL_TIMEOUT_S + CONNECT_TIMEOUT_S

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        schema = flat_schema(cls.remote.schema)
        properties = schema.setdefault("properties", {})
        if isinstance(properties, dict) and ABOUT not in properties:
            properties[ABOUT] = {
                "type": "string",
                "description": "Ours: what it's about, briefly ('the dentist task'), for the confirm question; no IDs.",
            }
        return schema

    async def describe_call(self, args: _Passthrough, ctx: ToolContext) -> str:
        about = str((args.model_extra or {}).get(ABOUT) or "").strip()
        words = self.remote.name.replace("-", "_").split("_")
        verb, thing = (words[0], " ".join(words[1:])) if len(words) > 1 else (_VERBS[self.kind], words[0])
        target = about[:80] if about else (f"a {thing}" if thing else "something")
        return f"{verb} {target} in TickTick"

    async def precheck(self, args: _Passthrough, ctx: ToolContext) -> str | None:
        return None if keys.is_set(TOKEN_ENV) else setup_hint(ctx.config)

    async def run(self, args: _Passthrough, ctx: ToolContext) -> ToolResult:
        arguments = {k: v for k, v in args.model_dump().items() if k != ABOUT}
        try:
            answer = await shared_client().call(self.remote.name, arguments, retry=self.kind == "read")
        except TickTickError as exc:
            return ToolResult.error(f"TickTick: {exc}")
        if self.reader is not None and answer.ok:
            from octavia.tools.extensions._ticktick_saved import check_update

            checked = await check_update(shared_client(), self.remote, self.reader, arguments, answer)
            if not checked.saved:
                return _result(checked.answer)
            arguments, answer = checked.arguments, checked.answer
        _tell(ctx, self.remote.name, arguments, answer)
        return _result(answer)


def _result(answer: Answer) -> ToolResult:
    text = answer.text
    if len(text) > RESULT_CHARS:
        text = (
            text[:RESULT_CHARS] + f"\n[cut: the answer went on for {len(answer.text) - RESULT_CHARS} more characters]"
        )
    return ToolResult.success(text) if answer.ok else ToolResult.error(f"TickTick: {text}")


def flat_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The server's argument schema with its ``$defs`` inlined and titles dropped (as for our own tools)."""
    try:
        return _inline_schema(copy.deepcopy(schema) if schema else {})
    except (KeyError, TypeError, RecursionError):  # a reference we can't follow: send it as it came
        flat = copy.deepcopy(schema) if schema else {}
        flat.setdefault("type", "object")
        flat.setdefault("properties", {})
        return flat


def make_tool(remote: RemoteTool, reader: RemoteTool | None = None) -> type[TickTickTool]:
    """Our tool for one of TickTick's; ``reader`` reads a task back after it (``_ticktick_saved``)."""
    kind = remote.kind
    description = remote.description.strip() or remote.name.replace("_", " ")
    risk = RiskLevel.SAFE if kind in ("read", "create") else RiskLevel.CONFIRM
    return type(
        f"TickTick_{local_name(remote.name)[len(PREFIX) :]}",
        (TickTickTool,),
        {
            "name": local_name(remote.name),
            "description": f"TickTick (the user's own to-do app): {description}",
            "remote": remote,
            "kind": kind,
            "risk": risk,
            "reader": reader,
            "__module__": __name__,
        },
    )


class TickTickSetup(Tool):
    """Stands in while TickTick isn't connected, so the brain can say how to connect it instead of guessing."""

    name = "ticktick_setup"
    description = (
        "TickTick (the user's to-do app) isn't connected yet, or couldn't be reached when the assistant started. "
        "Call this when the user asks about their TickTick tasks, lists, habits or focus time: it says why and how "
        "to fix it."
    )
    category = "tasks"

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        if not keys.is_set(TOKEN_ENV):
            return ToolResult.error(setup_hint(ctx.config))
        return ToolResult.error(
            "TickTick is set up but couldn't be reached when the assistant started, so its tools aren't loaded. "
            "Restarting the assistant once TickTick is reachable loads them."
        )


class MoreArgs(ToolArgs):
    tool: str = Field(description="A tool name from this tool's description.")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Its arguments, as its schema says.")
    describe: bool = Field(default=False, description="True to get its description and schema instead of calling it.")
    about: str = Field(
        default="",
        max_length=120,
        description="What it's about, briefly ('my Work board'), for the confirm question; no IDs.",
    )


class TickTickMore(Tool):
    """The server's less-used tools, through one: named in the description, each one's schema on request."""

    name = MORE
    Args = MoreArgs
    category = "tasks"
    timeout_s = CALL_TIMEOUT_S + CONNECT_TIMEOUT_S
    remotes: ClassVar[dict[str, RemoteTool]] = {}
    description: ClassVar[str] = ""

    def _remote(self, args: MoreArgs) -> RemoteTool | None:
        return self.remotes.get(args.tool.strip())

    def assess_risk(self, args: MoreArgs) -> RiskLevel:
        remote = self._remote(args)
        if args.describe or remote is None:
            return RiskLevel.SAFE
        return RiskLevel.SAFE if remote.kind in ("read", "create") else RiskLevel.CONFIRM

    async def inspect_risk(self, args: MoreArgs, ctx: ToolContext) -> RiskFinding | None:
        """A delete is flagged, so it's asked whatever the rule for changes says."""
        remote = self._remote(args)
        if remote is not None and not args.describe and remote.kind == "delete":
            return RiskFinding(RiskLevel.CONFIRM, scope="delete", reason="deletes it")
        return None

    async def describe_call(self, args: MoreArgs, ctx: ToolContext) -> str:
        words = args.tool.strip().replace("-", "_").split("_")
        verb, thing = (words[0], " ".join(words[1:])) if len(words) > 1 else ("use", words[0])
        target = args.about.strip()[:80] or (f"a {thing}" if thing else "something")
        return f"{verb} {target} in TickTick"

    async def precheck(self, args: MoreArgs, ctx: ToolContext) -> str | None:
        if self._remote(args) is None:
            return f"TickTick has no tool called {args.tool!r} here. The ones this reaches: {', '.join(self.remotes)}."
        return None if keys.is_set(TOKEN_ENV) else setup_hint(ctx.config)

    async def run(self, args: MoreArgs, ctx: ToolContext) -> ToolResult:
        remote = self._remote(args)
        if remote is None:
            return ToolResult.error(f"TickTick has no tool called {args.tool!r} here.")
        if args.describe:
            schema = json.dumps(flat_schema(remote.schema), ensure_ascii=False, separators=(",", ":"))
            return ToolResult.success(f"{remote.name}: {remote.description.strip()}\nArguments (JSON schema): {schema}")
        arguments = dict(args.arguments)
        try:
            answer = await shared_client().call(remote.name, arguments, retry=remote.kind == "read")
        except TickTickError as exc:
            return ToolResult.error(f"TickTick: {exc}")
        _tell(ctx, remote.name, arguments, answer)
        return _result(answer)


def make_more(remotes: Sequence[RemoteTool]) -> type[TickTickMore]:
    """``ticktick_more`` for these tools of the server's: each named with the first sentence of its description."""
    lines = []
    for remote in remotes:
        summary = (remote.description.strip().splitlines() or [""])[0]
        summary = summary.split(". ", 1)[0].rstrip(".")[:80]
        lines.append(f"{remote.name}: {summary}." if summary else f"{remote.name}.")
    description = (
        "More TickTick tools, for what the other ticktick_ tools don't cover: call with tool=<name> and "
        "arguments=<its arguments>; unsure of the arguments, first call with describe=true. "
        "The tools: " + " ".join(lines)
    )
    return type(
        "TickTick_more",
        (TickTickMore,),
        {"remotes": {r.name: r for r in remotes}, "description": description, "__module__": __name__},
    )


def tools_for(remotes: Sequence[RemoteTool]) -> list[type[Tool]]:
    """Our tools for the server's: one each for the ``CORE`` ones (first come first served when two tidy to the
    same name), and ``ticktick_more`` for the rest. ``update_task`` reads its task back with ``get_task_by_id``,
    when the server has it: TickTick has answered an update with the task as it was."""
    made: dict[str, type[Tool]] = {}
    rest = []
    reader = next((r for r in remotes if r.name == READER), None)
    for remote in remotes:
        if remote.name not in CORE:
            rest.append(remote)
            continue
        tool = make_tool(remote, reader if remote.name == "update_task" else None)
        made.setdefault(tool.name, tool)
    if rest:
        made.setdefault(MORE, make_more(rest))
    return list(made.values())


def register(registry: ToolRegistry, url: str = DEFAULT_URL) -> None:
    """Register TickTick's tools as ours: from the saved list (refreshed in the background for next time), or, the
    first time, straight from the server. Without a token, or with no list and no server, just ``ticktick_setup``."""
    if not keys.is_set(TOKEN_ENV):
        registry.register(TickTickSetup)
        return
    remotes = read_catalog()
    if remotes is not None:
        _refresh_later(url)
    else:
        try:
            remotes = fetch_catalog(url)
            write_catalog(remotes)
        except TickTickError as exc:
            log.warning("TickTick's tools couldn't be loaded: %s", exc)
            registry.register(TickTickSetup)
            return
    for tool in tools_for(remotes):
        try:
            registry.register(tool)
        except ValueError:  # the name is taken by one of our own tools
            log.warning("Skipped a TickTick tool: the name %s is taken", tool.name)


def permission_rules(config: AppConfig, tools: Sequence[Tool]) -> dict[str, Decision]:
    """The permission each TickTick tool gets from ``config.ticktick``: changing (complete, reschedule, move,
    edit) and deleting. Reading and adding never ask. A rule the user wrote for one tool in
    ``permissions.tools`` still wins (the policy applies these only where there's none)."""
    settings = config.ticktick
    rules: dict[str, Decision] = {}
    for tool in tools:
        if isinstance(tool, TickTickTool):
            if tool.kind == "change":
                rules[tool.name] = settings.changes
            elif tool.kind == "delete":
                rules[tool.name] = settings.deletes
        elif isinstance(tool, TickTickMore):
            # Its reads and changes follow the rule for changes; its deletes are flagged by inspect_risk, so they're
            # asked whatever this says.
            rules[tool.name] = settings.changes
    return rules


# ---------------------------------------------------------------------------------------------------- told on the bus

# What each of the server's tools does to to-dos when a call goes through; any other tool is never told.
TASK_ACTIONS = {
    "create_task": "created",
    "batch_add_tasks": "created",
    "update_task": "updated",
    "batch_update_tasks": "updated",
    "complete_task": "completed",
    "complete_tasks_in_project": "completed",
    "delete_task": "deleted",
    "move_task": "moved",
}
COMPLETED = 2  # a task's status once it's ticked off (0 is open, -1 abandoned)
_TICKTICK_ID = re.compile(r"\b[0-9a-f]{24}\b")  # TickTick's ids, when an answer is prose


def _pick(item: Mapping[str, Any], *names: str) -> Any:
    """The first of ``names`` that ``item`` has a value for (TickTick writes both camelCase and snake_case)."""
    for name in names:
        value = item.get(name)
        if value is not None and value != "":
            return value
    return None


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())[:120]


def _number(value: Any) -> int | None:
    """An integer the model or the server may have written as a number or a string ("5"); None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _change(action: str, *items: Mapping[str, Any], task_id: Any = None, project_id: Any = None) -> TaskChanged:
    """A ``TaskChanged`` from what's known of one task: ``items`` best first, and the ids given before any of them."""

    def pick(*names: str) -> Any:
        return next((value for item in items if (value := _pick(item, *names)) is not None), None)

    due = pick("dueDate", "due_date")
    return TaskChanged(
        action=action,
        task_id=str(task_id or pick("id", "task_id", "taskId") or ""),
        project_id=str(project_id or pick("projectId", "project_id") or ""),
        title=_words(pick("title")),
        due=due if isinstance(due, str) else "",
        all_day=pick("isAllDay", "is_all_day") is True,
        priority=_number(pick("priority")),
    )


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def task_changes(remote: str, arguments: Mapping[str, Any], answer: str) -> list[TaskChanged]:
    """The to-dos a call of TickTick's ``remote`` tool changed, once it went through: one ``TaskChanged`` each, with
    the ids from the arguments, or for new tasks from ``answer`` (a task, a list of them, or prose holding the id)."""
    action = TASK_ACTIONS.get(remote)
    if action is None:
        return []
    answered = [task for task in parse_tasks(answer) if task.get("id")]
    known = {str(task["id"]): task for task in answered}
    if remote == "create_task":
        task = arguments["task"] if isinstance(arguments.get("task"), Mapping) else arguments
        if answered:
            return [_change(action, answered[0], task)]
        found = _TICKTICK_ID.search(answer)
        return [_change(action, task, task_id=found.group(0) if found else "")]
    if remote == "batch_add_tasks":
        asked = _dicts(arguments.get("tasks"))
        if not answered:
            return [_change(action, task) for task in asked]  # added, though the answer didn't say under what ids
        by_title = {_words(task.get("title")): task for task in asked}
        return [_change(action, made, by_title.get(_words(made.get("title")), {})) for made in answered]
    if remote in ("update_task", "batch_update_tasks"):
        if remote == "update_task":
            task = arguments["task"] if isinstance(arguments.get("task"), Mapping) else {}
            asked = [(_pick(arguments, "task_id", "taskId") or _pick(task, "id"), task)]
        else:
            asked = [(_pick(task, "id", "task_id", "taskId"), task) for task in _dicts(arguments.get("tasks"))]
        changes = []
        for ident, task in asked:
            done = _number(_pick(task, "status")) == COMPLETED
            changes.append(_change("completed" if done else action, task, known.get(str(ident), {}), task_id=ident))
        return changes
    if remote == "move_task":
        return [
            _change(
                action,
                known.get(str(_pick(move, "taskId", "task_id")), {}),
                task_id=_pick(move, "taskId", "task_id"),
                project_id=_pick(move, "toProjectId", "to_project_id"),
            )
            for move in _dicts(arguments.get("moves"))
        ]
    project = _pick(arguments, "project_id", "projectId")
    if remote == "complete_tasks_in_project":
        idents = _pick(arguments, "task_ids", "taskIds")
        idents = [i for i in idents if isinstance(i, str | int) and str(i)] if isinstance(idents, list) else []
        return [_change(action, known.get(str(i), {}), task_id=i, project_id=project) for i in idents]
    ident = _pick(arguments, "task_id", "taskId")  # complete_task, delete_task
    return [_change(action, known.get(str(ident), {}), task_id=ident, project_id=project)]


def _tell(ctx: ToolContext, remote: str, arguments: Mapping[str, Any], answer: Answer) -> None:
    """Tell ``ctx.events`` about the to-dos a call that went through changed. Never in the tool's way: a failure
    here is only logged."""
    if not answer.ok or remote not in TASK_ACTIONS:
        return
    try:
        for change in task_changes(remote, arguments, answer.text):
            ctx.events.emit(change)
    except Exception:
        log.debug("Couldn't tell what %s changed in TickTick", remote, exc_info=True)


# ---------------------------------------------------------------------------------------------------- reminders

# What the reminders ask for: overdue tasks (due in the last week), today's, and what's due in the next day.
_DUE_QUERIES = ("last7day", "today", "next24hour")


def parse_tasks(text: str) -> list[dict[str, Any]]:
    """The tasks in one of the server's task lists: ``{"result": [...]}``, a bare list, or one task."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    if isinstance(data, dict):
        data = data.get("tasks", [data])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


async def due_tasks(client: TickTickClient | None = None) -> list[Any]:
    """Unfinished tasks with a date, overdue to a day ahead, for the reminders (``_ticktick_reminders``)."""
    from octavia.tools.extensions._ticktick_reminders import DueTask, local_due

    client = client or shared_client()
    found: dict[str, DueTask] = {}
    for query in _DUE_QUERIES:
        answer = await client.call("list_undone_tasks_by_time_query", {"query_command": query}, retry=True)
        if not answer.ok:
            raise TickTickError(answer.text[:200])
        for item in parse_tasks(answer.text):
            if item.get("status", 0) not in (0, None):
                continue  # done or abandoned
            all_day = bool(item.get("is_all_day", item.get("isAllDay", False)))
            due = local_due(item.get("due_date") or item.get("dueDate"), all_day)
            ident = str(item.get("id") or "")
            if due is None or not ident:
                continue
            title = " ".join(str(item.get("title") or "a task").split())[:120]
            found[ident] = DueTask(
                id=ident,
                title=title,
                due=due,
                all_day=all_day,
                priority=_number(item.get("priority")),
                repeats=bool(item.get("repeatFlag") or item.get("repeat_flag")),
            )
    return list(found.values())
