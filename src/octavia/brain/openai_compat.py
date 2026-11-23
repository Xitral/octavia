"""A brain on any OpenAI-compatible chat API (``brain.provider: openai_compatible``).

One backend for every server that speaks OpenAI's ``/chat/completions`` with tools: OpenAI itself, Google Gemini
(its OpenAI endpoint), xAI Grok, Mistral, DeepSeek, Groq, OpenRouter, and the local servers (Ollama, LM Studio,
llama.cpp's llama-server, vLLM). Set ``brain.openai_compatible``'s base URL, the name of the environment variable
holding the key ("" for a local server with none) and the model's name.

It keeps the ``BrainProvider`` contract (``brain/base.py``) and runs the tool loop itself, every call through her
``ToolExecutor``, like the Claude brain. What only Claude has is switched off rather than faked:

* no Claude Code built-ins (WebSearch, WebFetch): only her own tools;
* no prompt cache warm-up and no session to resume after a restart (the server's own prefix caching, where it has
  one, works by itself: her prompt and tools are sent first and unchanged);
* tools on demand is her own ``load_tools`` tool instead of Claude Code's ToolSearch: her ~250 tools are more than
  most of these APIs take in one request (OpenAI: 128), so ``always_loaded_tools`` go out in full and the rest are
  listed by name until a request loads them;
* her quick voice and opening words (Haiku) stay off: ``app.create_fast_chat``/``create_opener`` need a Claude brain.

The conversation is kept here as the message list sent each request; when the server says it's grown past
``context_limit_tokens`` the oldest exchanges are dropped, whole, and pictures from earlier turns are replaced by a
short note.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from octavia.brain.base import (
    STEP_LIMIT_PAUSE,
    BrainEvent,
    BrainProvider,
    ResponseDone,
    SystemPromptFactory,
    TextDelta,
    ToolUseStarted,
    UserTurn,
)
from octavia.tools.base import ToolResult, ToolSpec
from octavia.tools.executor import ToolExecutor

if TYPE_CHECKING:
    from octavia.config import OpenAICompatibleConfig

log = logging.getLogger(__name__)

PROVIDER = "openai_compatible"
LOAD_TOOLS = "load_tools"  # her own tool search: loads tools that are only listed by name
_DROPPED_PICTURE = "[a picture was here; it's no longer kept]"
_INTERRUPTED_RESULT = "Not run: the user interrupted."
# Tools listed in load_tools' description: the first words of each one's description.
_HINT_CHARS = 70

_SPOKEN = {
    "auth": "My model's server didn't accept the API key. Check the key in the dot env file.",
    "rate_limit": "My model's server says I've hit its rate limit. Try again in a bit.",
    "not_found": "My model's server doesn't know the model I'm set to use. Check the model name in my settings.",
    "connection": "I can't reach my model's server right now.",
    "server": "My model's server had an error. Try that again.",
    "stalled": "My model stopped answering me. Try that again.",
    "rejected": "My model's server rejected that request.",
    "too_long": "Our conversation got too long for my model, so I've let go of the oldest part. Try that again.",
}
# A 400 saying the request is past the model's context (each server words it its own way).
_TOO_LONG = re.compile(
    r"context.{0,40}(length|window|limit|too long|exceed)|too many tokens|maximum.{0,30}tokens", re.I | re.S
)


class ModelError(Exception):
    """A request that failed: ``kind`` picks what's said (``_SPOKEN``), ``retry`` whether trying again could help."""

    def __init__(self, kind: str, detail: str, retry: bool = False) -> None:
        super().__init__(detail)
        self.kind = kind
        self.retry = retry


def api_key(cfg: OpenAICompatibleConfig) -> str | None:
    """The key from the environment variable ``api_key_env`` names; None when it names none (a local server)."""
    if not cfg.api_key_env:
        return None
    return os.environ.get(cfg.api_key_env) or None


def headers(cfg: OpenAICompatibleConfig) -> dict[str, str]:
    out = {"Content-Type": "application/json", **cfg.extra_headers}
    if (key := api_key(cfg)) is not None:
        out["Authorization"] = f"Bearer {key}"
    return out


def chat_url(cfg: OpenAICompatibleConfig) -> str:
    return cfg.base_url.rstrip("/") + "/chat/completions"


def function_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Her tools as OpenAI function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.input_schema or {"type": "object", "properties": {}},
            },
        }
        for s in specs
    ]


def _hint(spec: ToolSpec) -> str:
    first = re.split(r"(?<=[.!?])\s", " ".join(spec.description.split()), maxsplit=1)[0]
    return first if len(first) <= _HINT_CHARS else first[: _HINT_CHARS - 3].rstrip() + "..."


def load_tools_spec(listed: list[ToolSpec]) -> dict[str, Any]:
    """The ``load_tools`` tool: every tool not sent in full, by name and the first words of what it does."""
    lines = "\n".join(f"- {s.name}: {_hint(s)}" for s in listed)
    return {
        "type": "function",
        "function": {
            "name": LOAD_TOOLS,
            "description": (
                "Load more of your tools. Only some are sent in full; these others are listed by name. When a "
                "request needs one, call this with its name (several at once is fine), then call the tool itself in "
                "your next step. Loaded tools stay loaded for the rest of the conversation.\n" + lines
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}, "description": "tool names to load"}
                },
                "required": ["names"],
            },
        },
    }


def image_part(mime_type: str, data: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}}


def _result_image(result: ToolResult) -> dict[str, Any] | None:
    """The picture a tool attached for the model to look at (``data={"image": {...}}``, as for the Claude brain)."""
    image = (result.data or {}).get("image")
    if not isinstance(image, dict) or not isinstance(image.get("data"), str) or not image.get("mime_type"):
        return None
    return image_part(str(image["mime_type"]), image["data"])


class ThinkFilter:
    """Drops ``<think>...</think>`` from streamed text (models served without a reasoning parser put it there)."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._inside = False
        self._held = ""

    def feed(self, text: str) -> str:
        out: list[str] = []
        data = self._held + text
        self._held = ""
        while data:
            tag = self.CLOSE if self._inside else self.OPEN
            at = data.find(tag)
            if at >= 0:
                if not self._inside:
                    out.append(data[:at])
                data = data[at + len(tag) :]
                self._inside = not self._inside
                continue
            # A tag may be cut between two pieces: hold back what could be its start.
            keep = next((n for n in range(min(len(tag) - 1, len(data)), 0, -1) if tag.startswith(data[-n:])), 0)
            if not self._inside:
                out.append(data[: len(data) - keep])
            self._held = data[len(data) - keep :]
            break
        return "".join(out)

    def flush(self) -> str:
        held, self._held = self._held, ""
        return "" if self._inside else held


class _Step:
    """What one streamed model call said: its words, its tool calls, its usage."""

    def __init__(self) -> None:
        self.text = ""
        self.calls: dict[int, dict[str, Any]] = {}  # by index: id, name, arguments (as streamed)
        self.usage: dict[str, Any] | None = None
        self.model: str | None = None
        self.finish: str | None = None
        self.reasoning = ""
        self._slots: dict[Any, int] = {}  # the server's index -> our call number
        self._last: int | None = None

    def ordered(self) -> list[dict[str, Any]]:
        return [self.calls[i] for i in sorted(self.calls)]

    def add_call(self, delta: dict[str, Any]) -> None:
        """One streamed piece of a tool call. The server's index says which call it belongs to; a new id on an index
        already used is a call of its own (some servers number every call 0); a piece with no index goes with the
        call before it unless it brings a new id."""
        index, call_id = delta.get("index"), delta.get("id") or ""
        number = self._slots.get(index) if index is not None else self._last
        if number is not None and call_id and self.calls[number]["id"] and self.calls[number]["id"] != call_id:
            number = None
        if number is None:
            number = len(self.calls)
            self.calls[number] = {"id": call_id or f"call_{uuid.uuid4().hex[:12]}", "name": "", "arguments": "",
                                  "extra": {}}  # fmt: skip
            if index is not None:
                self._slots[index] = number
        self._last = number
        slot = self.calls[number]
        fn = delta.get("function") or {}
        if fn.get("name"):
            slot["name"] = slot["name"] or fn["name"]
        if (args := fn.get("arguments")) is not None and args != "":
            slot["arguments"] += args if isinstance(args, str) else json.dumps(args)
        for key, value in delta.items():
            if key not in ("index", "id", "type", "function"):
                slot["extra"][key] = value

    def clear(self) -> None:
        self.__init__()  # type: ignore[misc]


class OpenAICompatibleBrain(BrainProvider):
    name = PROVIDER

    def __init__(
        self,
        cfg: OpenAICompatibleConfig,
        tools: list[ToolSpec],
        executor: ToolExecutor,
        system_prompt: SystemPromptFactory,
        *,
        always_loaded: list[str] | None = None,
        max_turns: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(tools, executor, system_prompt)
        self.cfg = cfg
        self.max_turns = max_turns or cfg.max_turns
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._messages: list[dict[str, Any]] = []
        self._turn_starts: list[int] = []  # where each user turn begins in _messages
        self._prompt: str | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._running: asyncio.Task[Any] | None = None
        patterns = cfg.always_loaded_tools if cfg.always_loaded_tools is not None else (always_loaded or [])
        self._by_name = {s.name: s for s in tools}
        # Tools sent in full, in a fixed order (sent first and unchanged, so a server's prefix cache keeps them), and
        # the rest, listed by name in load_tools until a request loads them.
        on_demand = cfg.tools_on_demand and len(tools) > cfg.max_tools
        full = [s.name for s in tools if not on_demand or any(fnmatch.fnmatchcase(s.name, p) for p in patterns)]
        room = cfg.max_tools - (1 if len(full) < len(tools) or len(full) > cfg.max_tools else 0)
        if len(full) > room:
            log.warning("%d tools sent in full is more than brain.openai_compatible.max_tools (%d): the rest are "
                        "listed for load_tools", len(full), cfg.max_tools)  # fmt: skip
            full = full[:room]
        self._first = (list(full), [s.name for s in tools if s.name not in set(full)])  # what reset() goes back to
        self._loaded: list[str] = full
        self._listed: list[ToolSpec] = [s for s in tools if s.name not in set(full)]
        self._loaded_later: list[str] = []  # loaded by load_tools in this conversation, oldest first

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not self.cfg.model:
            log.warning("brain.openai_compatible.model is empty: set the model's name")
        if self.cfg.api_key_env and api_key(self.cfg) is None:
            log.warning("%s isn't set: requests will likely be refused", self.cfg.api_key_env)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(self.cfg.request_timeout_s, connect=self.cfg.connect_timeout_s)
            self._client = httpx.AsyncClient(timeout=timeout, transport=self._transport)
        return self._client

    async def close(self) -> None:
        await self.interrupt()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def reset(self) -> None:
        async with self._lock:
            self._messages.clear()
            self._turn_starts.clear()
            self._prompt = None
            self._loaded = list(self._first[0])
            self._listed = [self._by_name[n] for n in self._first[1]]
            self._loaded_later.clear()

    async def interrupt(self) -> None:
        self._stop.set()
        if self._running is not None and not self._running.done():
            self._running.cancel()

    async def abandon_turn(self) -> None:
        await self.interrupt()

    # ------------------------------------------------------------------ tools
    def tool_list(self) -> list[dict[str, Any]]:
        tools = function_tools([self._by_name[n] for n in self._loaded])
        if self._listed:
            tools.append(load_tools_spec(self._listed))
        return tools

    def _load(self, names: Any) -> ToolResult:
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return ToolResult.error("names must be a list of tool names.")
        listed = {s.name for s in self._listed}
        found = [n for n in dict.fromkeys(names) if n in listed]
        already = [n for n in names if n in self._loaded]
        unknown = [n for n in names if n not in listed and n not in self._loaded]
        room = self.cfg.max_tools - len(self._loaded) - 1
        if len(found) > room:
            # The oldest tools loaded on demand make room (the always-loaded ones stay).
            drop = [n for n in self._loaded_later if n not in found][: len(found) - room]
            for n in drop:
                self._loaded.remove(n)
                self._loaded_later.remove(n)
                self._listed.append(self._by_name[n])
        room = max(0, self.cfg.max_tools - len(self._loaded) - 1)
        found, no_room = found[:room], found[room:]
        for n in found:
            self._loaded.append(n)
            self._loaded_later.append(n)
        self._listed = [s for s in self._listed if s.name not in found]
        parts = []
        if found:
            parts.append(f"Loaded: {', '.join(found)}. Call them in your next step.")
        if already:
            parts.append(f"Already loaded: {', '.join(already)}.")
        if no_room:
            parts.append(f"No room for: {', '.join(no_room)} (load fewer at once).")
        if unknown:
            parts.append(f"No such tool: {', '.join(unknown)}.")
        return (ToolResult.success if found or already else ToolResult.error)(" ".join(parts) or "Nothing to load.")

    # ------------------------------------------------------------------ a turn
    def respond(self, turn: UserTurn) -> AsyncIterator[BrainEvent]:
        return self._respond(turn)

    async def _respond(self, turn: UserTurn) -> AsyncIterator[BrainEvent]:
        async with self._lock:
            self._stop.clear()
            pieces: list[str] = []
            usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "iterations": []}
            first_ctx = last_ctx = None
            model: str | None = None
            error: str | None = None
            spoken: str | None = None
            try:
                if self._prompt is None:
                    self._prompt = await self.system_prompt()
                self._forget_old_pictures()
                self._turn_starts.append(len(self._messages))
                self._messages.append({"role": "user", "content": self._user_content(turn)})
                steps = 0
                shortened = False
                while True:
                    step = _Step()
                    try:
                        async for text in self._stream(step):
                            pieces.append(text)
                            yield TextDelta(text)
                    except ModelError as exc:
                        # Past the model's context: let go of the oldest exchanges and ask once more.
                        if exc.kind != "too_long" or shortened or not self._trim(force=True):
                            raise
                        shortened = True
                        continue
                    model = step.model or model
                    if step.usage:
                        p = int(step.usage.get("prompt_tokens") or 0)
                        c = int(step.usage.get("completion_tokens") or 0)
                        usage["input_tokens"] += p
                        usage["output_tokens"] += c
                        usage["iterations"].append({"input_tokens": p, "output_tokens": c})
                        first_ctx = p if first_ctx is None else first_ctx
                        last_ctx = p
                    # Cut off mid-call, its arguments may be half there: no call is kept from an interrupted step,
                    # and none without a name.
                    calls = [] if self._stop.is_set() else [c for c in step.ordered() if c["name"]]
                    assistant: dict[str, Any] = {"role": "assistant", "content": step.text or None}
                    if step.reasoning and calls:
                        assistant["reasoning_content"] = step.reasoning  # thinking models want it back in a tool turn
                    if calls:
                        assistant["tool_calls"] = [_history_call(c) for c in calls]
                    if step.text or calls:
                        self._messages.append(assistant)
                    if not calls or self._stop.is_set():
                        break
                    if steps >= self.max_turns:
                        self._answer_unrun(calls, "Not run: the step limit was reached.")
                        paused = f" {STEP_LIMIT_PAUSE}" if pieces else STEP_LIMIT_PAUSE
                        pieces.append(paused)
                        yield TextDelta(paused)
                        break
                    steps += 1
                    async for event in self._run_tools(calls):
                        yield event
                    if self._stop.is_set():
                        break
            except ModelError as exc:
                error, spoken = f"{self.cfg.model or 'model'}: {exc}"[:300], _SPOKEN.get(exc.kind)
                log.warning("Brain request failed (%s): %s", exc.kind, exc)
                if exc.kind == "too_long":
                    self._trim(force=True)
                self._drop_unanswered_user()
            except Exception as exc:  # anything else still ends the turn with its ResponseDone
                log.exception("Brain turn failed")
                error, spoken = f"{type(exc).__name__}: {exc}"[:300], _SPOKEN["server"]
                self._drop_unanswered_user()
            finally:
                self._close_open_calls()
                self._running = None
            # The server's count when it gives one, else about four characters a token.
            if (last_ctx if last_ctx is not None else self._estimate()) > self.cfg.context_limit_tokens:
                self._trim()
            yield ResponseDone(
                text="".join(pieces).strip(),
                interrupted=self._stop.is_set(),
                error=error,
                spoken_error=spoken,
                usage=usage if usage["iterations"] else None,
                context_tokens=last_ctx,
                first_context_tokens=first_ctx,
                model=model or self.cfg.model or None,
            )

    def _user_content(self, turn: UserTurn) -> str | list[dict[str, Any]]:
        text = turn.render()
        if turn.picture is None or not self.cfg.vision:
            return text
        return [image_part(turn.picture.mime_type, turn.picture.data), {"type": "text", "text": text}]

    async def _run_tools(self, calls: list[dict[str, Any]]) -> AsyncIterator[BrainEvent]:
        pictures: list[tuple[str, dict[str, Any]]] = []
        for i, call in enumerate(calls):
            if self._stop.is_set():
                self._answer_unrun(calls[i:], _INTERRUPTED_RESULT)
                return
            if call["name"] != LOAD_TOOLS:
                yield ToolUseStarted(call_id=call["id"], name=call["name"])
            if self.heartbeat is not None:
                self.heartbeat()
            try:
                args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                result = ToolResult.error(f"Your arguments weren't valid JSON ({exc.msg}). Try again.")
            else:
                if not isinstance(args, dict):
                    result = ToolResult.error("Arguments must be a JSON object.")
                elif call["name"] == LOAD_TOOLS:
                    result = self._load(args.get("names"))
                elif call["name"] not in self._loaded:
                    known = call["name"] in self._by_name
                    result = ToolResult.error(
                        f"{call['name']} isn't loaded: call {LOAD_TOOLS} with its name first." if known
                        else f"There's no tool named {call['name']}."
                    )  # fmt: skip
                else:
                    ran = await self._until_stopped(
                        asyncio.ensure_future(self.executor.execute(call["name"], args, call_id=call["id"]))
                    )
                    if ran is None:
                        self._answer_unrun(calls[i:], _INTERRUPTED_RESULT)
                        return
                    result = ran
            content = result.content if result.ok else f"Error: {result.content}"
            self._messages.append({"role": "tool", "tool_call_id": call["id"], "content": content or "(no output)"})
            if self.cfg.vision and (image := _result_image(result)) is not None:
                pictures.append((call["name"], image))
        if pictures:
            names = ", ".join(dict.fromkeys(n for n, _ in pictures))
            note = {"type": "text", "text": f"[The picture{'s' if len(pictures) > 1 else ''} from {names}.]"}
            self._messages.append({"role": "user", "content": [note, *(p for _, p in pictures)]})

    def _answer_unrun(self, calls: list[dict[str, Any]], why: str) -> None:
        """Every tool call the model made needs an answer, or the next request is refused."""
        for call in calls:
            self._messages.append({"role": "tool", "tool_call_id": call["id"], "content": why})

    def _close_open_calls(self) -> None:
        answered = {m.get("tool_call_id") for m in self._messages if m.get("role") == "tool"}
        for m in self._messages[self._turn_starts[-1] if self._turn_starts else 0 :]:
            for c in m.get("tool_calls") or ():
                if c["id"] not in answered:
                    self._messages.append({"role": "tool", "tool_call_id": c["id"], "content": _INTERRUPTED_RESULT})

    def _drop_unanswered_user(self) -> None:
        """A turn that got no reply at all leaves nothing behind: the next one mustn't read as two in a row."""
        if self._turn_starts and self._turn_starts[-1] == len(self._messages) - 1:
            self._messages.pop()
            self._turn_starts.pop()

    # ------------------------------------------------------------------ the request
    def _body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": self._prompt or ""}, *self._messages],
            "stream": True,
        }
        if tools := self.tool_list():
            body["tools"] = tools
        if self.cfg.stream_usage:
            body["stream_options"] = {"include_usage": True}
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        if self.cfg.max_tokens is not None:
            body["max_tokens"] = self.cfg.max_tokens
        if self.cfg.reasoning_effort is not None:
            body["reasoning_effort"] = self.cfg.reasoning_effort
        return body

    async def _next(self, queue: asyncio.Queue[tuple[str, Any]]) -> tuple[str, Any]:
        """The next thing the stream said; ("stopped", None) once interrupted; ``ModelError`` when it went silent."""
        get = asyncio.ensure_future(queue.get())
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            done, _ = await asyncio.wait({get, stop}, timeout=self.cfg.stall_timeout_s,
                                         return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        finally:
            stop.cancel()
            if not get.done():
                get.cancel()
        if get in done:
            return get.result()
        if stop in done:
            return "stopped", None
        raise ModelError("stalled", f"nothing for {self.cfg.stall_timeout_s:g} s")

    async def _stream(self, step: _Step) -> AsyncIterator[str]:
        """One model call, streamed: yields its words as they come (``step`` gets the rest). One retry when it failed
        before saying anything and trying again could help."""
        for attempt in range(2):
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
            reader = self._running = asyncio.create_task(self._read(step, queue))
            said = False
            try:
                while True:
                    kind, value = await self._next(queue)
                    if kind == "progress":
                        continue  # still streaming (a tool call's arguments, its thinking): not stalled
                    if kind == "text":
                        said = True
                        yield value
                    elif kind == "error":
                        if attempt == 0 and value.retry and not said and not step.calls and not self._stop.is_set():
                            log.info("Brain request failed (%s), trying again", value)
                            break
                        raise value
                    else:  # done, or stopped
                        return
            finally:
                if not reader.done():
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await reader
            step.clear()  # the retry starts over
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 1.0)
            if self._stop.is_set():
                return

    async def _until_stopped(self, task: asyncio.Future[ToolResult]) -> ToolResult | None:
        """The tool's result, or None when the turn was interrupted first: the tool then finishes on its own (a click
        half done mustn't be cut off), and the turn lets go of the brain."""
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if task.done():
            return task.result()
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # no "never retrieved" warning
        return None

    async def _read(self, step: _Step, queue: asyncio.Queue[tuple[str, Any]]) -> None:
        think = ThinkFilter()
        try:
            async with self._http().stream(
                "POST", chat_url(self.cfg), json=self._body(), headers=headers(self.cfg)
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    raise _http_error(r.status_code, body)
                async for line in r.aiter_lines():
                    if self.heartbeat is not None:
                        self.heartbeat()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk, list) and chunk and isinstance(chunk[0], dict):
                        chunk = chunk[0]  # some servers wrap an error in a list
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk and not chunk.get("choices"):
                        raise _stream_error(chunk["error"])
                    if text := think.feed(_take(chunk, step)):
                        step.text += text
                        queue.put_nowait(("text", text))
                    else:
                        queue.put_nowait(("progress", None))
            if text := think.flush():
                step.text += text
                queue.put_nowait(("text", text))
            queue.put_nowait(("done", None))
        except ModelError as exc:
            queue.put_nowait(("error", exc))
        except httpx.TimeoutException as exc:
            queue.put_nowait(("error", ModelError("stalled", f"timed out: {type(exc).__name__}", retry=True)))
        except httpx.HTTPError as exc:
            queue.put_nowait(("error", ModelError("connection", f"{type(exc).__name__}: {exc}"[:300], retry=True)))
        except Exception as exc:  # a reply it can't read: said now, not after the stall timeout
            log.warning("Unreadable reply from the model's server", exc_info=True)
            queue.put_nowait(("error", ModelError("server", f"{type(exc).__name__}: {exc}"[:300])))

    # ------------------------------------------------------------------ keeping the context small
    def _forget_old_pictures(self) -> None:
        """Pictures from earlier turns are replaced by a note: each would be sent again with every request."""
        for m in self._messages:
            content = m.get("content")
            if isinstance(content, list) and any(p.get("type") == "image_url" for p in content):
                kept = [p for p in content if p.get("type") != "image_url"]
                m["content"] = [*kept, {"type": "text", "text": _DROPPED_PICTURE}]

    def _fixed_tokens(self) -> int:
        """About how many tokens her prompt and the tools sent carry (in every request)."""
        return (len(self._prompt or "") + len(json.dumps(self.tool_list(), ensure_ascii=False))) // 4

    def _estimate(self) -> int:
        """About how many tokens a request carries (prompt, tools, conversation; a picture as a short note)."""
        return self._fixed_tokens() + sum(_size(m) for m in self._messages) // 4

    def _trim(self, force: bool = False) -> bool:
        """Drop the oldest exchanges, whole, until the request is about half the limit (the last one always stays;
        ``force``: past the model's own limit, so at least half of the earlier ones go). True when any was dropped."""
        if len(self._turn_starts) < 2:
            return False
        limit = self.cfg.context_limit_tokens
        target = max(limit // 2 - self._fixed_tokens(), limit // 8)
        size = sum(_size(m) for m in self._messages) // 4
        dropped, at_least = 0, max(1, (len(self._turn_starts) - 1) // 2) if force else 0
        while len(self._turn_starts) > 1 and (size > target or dropped < at_least):
            cut = self._turn_starts[1]
            size -= sum(_size(m) for m in self._messages[:cut]) // 4
            del self._messages[:cut]
            self._turn_starts = [s - cut for s in self._turn_starts[1:]]
            dropped += 1
        if dropped:
            log.info("Brain context trimmed: %d exchanges dropped, %d kept", dropped, len(self._turn_starts))
        return bool(dropped)


def _size(message: dict[str, Any]) -> int:
    """A message's length in characters, a picture counted as a short note (servers count one far smaller than
    its base64)."""
    content = message.get("content")
    if isinstance(content, list):
        text = sum(len(p.get("text") or "") for p in content if isinstance(p, dict))
        pictures = sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
        return text + 3000 * pictures
    return len(json.dumps(message, ensure_ascii=False))


def _history_call(call: dict[str, Any]) -> dict[str, Any]:
    """A tool call as it's kept and sent back. Arguments that don't parse (cut off at the reply's length) are kept as
    {} so the server can read its own history (the tool's answer says what was wrong); whatever else the server put
    on the call (Gemini's thought signature) goes back with it."""
    raw = call["arguments"] or "{}"
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        raw = "{}"
    return {**call["extra"], "id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": raw}}


def _stream_error(error: Any) -> ModelError:
    text = str(error)[:300]
    return ModelError("too_long" if _TOO_LONG.search(text) else "server", text)


def create(
    config: Any,
    tools: list[ToolSpec],
    executor: ToolExecutor,
    system_prompt: SystemPromptFactory,
    *,
    max_turns: int | None = None,
) -> OpenAICompatibleBrain:
    """The brain ``config`` (an ``AppConfig``) sets up: its tools always loaded are brain.claude's unless it names
    its own. ``max_turns`` raises the step limit (a background task's)."""
    cfg = config.brain.openai_compatible
    return OpenAICompatibleBrain(
        cfg,
        tools,
        executor,
        system_prompt,
        always_loaded=config.brain.claude.always_loaded_tools,
        max_turns=max(cfg.max_turns, max_turns or 0),
    )


def _take(chunk: dict[str, Any], step: _Step) -> str:
    """Fold one streamed chunk into ``step``; returns its new words."""
    step.model = chunk.get("model") or step.model
    if chunk.get("usage"):
        step.usage = chunk["usage"]
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    step.finish = choice.get("finish_reason") or step.finish
    delta = choice.get("delta") or {}
    if isinstance(reasoning := delta.get("reasoning_content"), str):
        step.reasoning += reasoning
    for call in delta.get("tool_calls") or ():
        if isinstance(call, dict):
            step.add_call(call)
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _http_error(status: int, body: str) -> ModelError:
    detail = f"HTTP {status}: {body.strip()[:300]}"
    if status in (401, 403):
        return ModelError("auth", detail)
    if status == 404:
        return ModelError("not_found", detail)
    if status == 429:
        return ModelError("rate_limit", detail, retry=True)
    if status >= 500:
        return ModelError("server", detail, retry=True)
    if _TOO_LONG.search(body):
        return ModelError("too_long", detail)
    return ModelError("rejected", detail)


async def ask(cfg: OpenAICompatibleConfig, prompt: str, system: str, model: str | None = None) -> str:
    """One reply, no tools, nothing else in its context: the eval's judge when the brain isn't Claude."""
    body = {
        "model": model or cfg.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }
    timeout = httpx.Timeout(cfg.request_timeout_s, connect=cfg.connect_timeout_s)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(chat_url(cfg), json=body, headers=headers(cfg))
    if r.status_code != 200:
        raise _http_error(r.status_code, r.text)
    content = r.json()["choices"][0]["message"].get("content") or ""
    if "<think>" not in content:
        return content
    think = ThinkFilter()
    return think.feed(content) + think.flush()
