"""The local model in shadow: after each of the user's turns it's asked the same thing, and only what it would have done
is written down, next to what she really did (docs/local-brain.md).

Phase 2 of the local brain. Phase 1 (docs/local-brain-phase1.md) measured Qwen3.5-4B on her eval set: about five
times faster to its first word than the brain, but wrong on about a quarter of the cases, and it doesn't know when.
So before it answers anything, this collects its choices on real turns, to find the kinds of request it gets right.

* It never answers, never runs a tool and never holds up a turn. A turn is queued once it's over (``BrainFinished``,
  or the quick voice's ``FastChatFinished``); one worker asks the model, one request at a time. Its tool calls are
  recorded, not run: only its first step is judged. A full queue drops the oldest turn.
* The prompt is hers without what changes from day to day (memories, the last conversation's recap, build status),
  built once at start, like the eval's: the model keeps it cached, and only the new turn is read each time.
* Games: while a full-screen app that isn't a reader (``recall.service.FULLSCREEN_TEXT_APPS``) is in front, turns are
  skipped; after ``game_unload_s`` the model is unloaded (its GPU memory is the game's again), and it loads again
  once no game has been in front for ``game_reload_s``.
* One line per turn in ``local_brain.log_file``: what was said, what she did (``brain``), what the local model would
  have done (``local``), whether the tools match (``agree``), or why it was skipped. ``octavia shadow`` summarizes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import statistics
import time
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

from octavia.config import LocalBrainConfig
from octavia.core.events import (
    BrainFinished,
    BrainStarted,
    Event,
    EventBus,
    FastChatFinished,
    FastChatStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from octavia.localbrain.delegation import delegated
from octavia.localbrain.server import SLOTS, LlamaServer, local_client

log = logging.getLogger(__name__)

USER_SOURCES = frozenset(
    {"voice", "text", "discord", "window"}
)  # a scheduled job's turn says nothing about what you want
_QUEUE = 4
_REQUEST_TIMEOUT_S = 60.0
_MAX_TOKENS = 300
_CLIP = 300
_GAME_POLL_S = 5.0  # a quick look at the window in front (a few ms, off the event loop)
_RETENTION_S = 30 * 86400  # older lines are dropped at start: it holds what you said
_RESTART_EVERY_S = 600.0
# What a reply read aloud must not have: the eval's own test (``evolve/evals._WRITTEN``).
_WRITTEN = re.compile(r"\*\*|^\s*(?:[-*•]|#{1,6}|\d+\.)\s|https?://|```", re.M)
_WEB = {
    "WebSearch": (
        "Search the web for current information (news, recent events, anything you don't know).",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    "WebFetch": (
        "Fetch a web page and answer a question about it.",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}, "prompt": {"type": "string"}},
            "required": ["url", "prompt"],
        },
    ),
}


def _loosen(schema: Any) -> Any:
    """Without array length bounds: llama.cpp can't compile the grammar for some of them (create_excel_workbook)."""
    if isinstance(schema, dict):
        return {k: _loosen(v) for k, v in schema.items() if k not in ("minItems", "maxItems")}
    if isinstance(schema, list):
        return [_loosen(v) for v in schema]
    return schema


def openai_tools(specs: Iterable[Any], builtin: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Her tools as the OpenAI-style list llama-server takes, plus the brain's built-in web tools that are on."""
    out = [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": _loosen(s.input_schema or {"type": "object", "properties": {}}),
            },
        }
        for s in specs
    ]
    for name in builtin:
        if name in _WEB:
            description, schema = _WEB[name]
            out.append(
                {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}
            )
    return out


def routed_tools(specs: Iterable[Any], config: LocalBrainConfig) -> list[Any]:
    """The tools the local model may ever call when it answers turns (``route_tools``, the ones that only look, and
    the ones it may use while Claude is down): only those are described to it, not her ~100 others."""
    from octavia.localbrain.delegation import ACT_TOOLS, READ_TOOLS

    allowed = {*config.route_tools, *READ_TOOLS, *ACT_TOOLS}
    return [s for s in specs if s.name in allowed]


async def routed_prompt(config: Any, voice_style: str | None, tools: Iterable[str]) -> str:
    """The local model's prompt when it answers turns ("route" or "fallback_only"): who she is, in her own
    personality, and a small focused job with examples (``delegation.focused_prompt``), not her whole prompt."""
    from octavia.brain.prompt import SystemPromptBuilder
    from octavia.localbrain.delegation import focused_prompt

    values = SystemPromptBuilder(config, None, voice_style=voice_style).persona_values()
    return focused_prompt(
        values["assistant_name"], values["user_name"], values["personality"], values["voice_style"], tools
    )


async def stable_prompt(config: Any, voice_style: str | None) -> str:
    """Her system prompt without what changes from day to day (memories, her about-me notes, the last conversation's
    recap, build and Futures status), as the eval builds it: it stays cached in the model between turns. It ends with
    when to hand a turn to Claude (``delegation``), the same in every mode, so the shadow measures what routing does."""
    from octavia.brain.prompt import SystemPromptBuilder
    from octavia.localbrain.delegation import instructions

    class Stable(SystemPromptBuilder):
        async def _memory_block(self) -> str:
            return "Known about the user: nothing yet."

        def _about_me_block(self) -> str:
            return ""

        async def _recap_block(self) -> str:
            return ""

    async def nothing() -> str:
        return ""

    builder = Stable(
        config,
        None,
        voice_style=voice_style,
        self_improvement=nothing if config.self_improvement.enabled else None,
        futures=nothing if config.futures.enabled else None,
    )
    return await builder.build() + instructions(config.local_brain.route_tools)


# Written by the local brain's fine-tune (scripts/local_brain_train.py) while it trains: the GPU is lent out, and the
# model is unloaded as for a game. Only while the process that wrote it is alive and before its "until": a training
# run that crashed never keeps her local brain away.
LENT_FILE = "data/local_brain_lent.json"


def gpu_lent(path: Path) -> bool:
    """The GPU is lent to a training run right now (``LENT_FILE``)."""
    import psutil

    try:
        lent = json.loads(path.read_text(encoding="utf-8"))
        pid, until = int(lent["pid"]), float(lent["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return time.time() < until and psutil.pid_exists(pid)


def game_in_front() -> bool:
    """A full-screen app that isn't one she reads (a browser, Word...) is in front: most likely a game."""
    from octavia.recall import capture
    from octavia.recall.service import FULLSCREEN_TEXT_APPS

    fg = capture.foreground()
    return bool(fg and fg.fullscreen and fg.process.casefold() not in FULLSCREEN_TEXT_APPS)


class LocalShadow:
    def __init__(
        self,
        config: LocalBrainConfig,
        events: EventBus,
        path: Path,
        prompt: Callable[[], Awaitable[str]],
        tools: list[dict[str, Any]],
        server: LlamaServer,
        *,
        private_args: dict[str, frozenset[str]] | None = None,
        game: Callable[[], bool] = game_in_front,
        clock: Callable[[], float] = time.time,
        lent_file: Path | None = None,
    ) -> None:
        self.config = config
        self.path = path
        self.server = server
        self.tools = tools
        self._prompt_factory = prompt
        self._prompt: str | None = None
        self._game = game
        self._lent_file = lent_file  # the GPU lent to a fine-tune: unloaded as for a game (``gpu_lent``)
        self._clock = clock
        self._private = private_args or {}  # per tool: arguments logged by length only (what she'd type)
        self._turns: dict[int, dict[str, Any]] = {}  # brain turns in progress, by turn_id
        self._fast: dict[str, Any] | None = None  # the quick voice's turn in progress
        self._reload: asyncio.Task[None] | None = None
        self._history: deque[tuple[str, str]] = deque(maxlen=max(config.history_turns, 1))
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(_QUEUE)
        self._tasks: list[asyncio.Task[None]] = []
        self._gaming_since: float | None = None
        self._clear_since: float | None = None
        self._unloaded_for_game = False
        self._restarted_at = 0.0  # when it was last started again after dying
        self.transport: httpx.AsyncBaseTransport | None = None  # tests answer for the server
        self._unsubscribe = events.subscribe(
            self._on_event,
            BrainStarted,
            ToolCallStarted,
            ToolCallFinished,
            BrainFinished,
            FastChatStarted,
            FastChatFinished,
        )

    # ------------------------------------------------------------------ life
    async def start(self) -> None:
        await asyncio.to_thread(self._prune)
        self._tasks = [
            asyncio.create_task(self._load(), name="local-brain-load"),
            asyncio.create_task(self._work(), name="local-brain-shadow"),
            asyncio.create_task(self._watch_games(), name="local-brain-games"),
        ]

    async def close(self) -> None:
        self._unsubscribe()
        tasks = [*self._tasks, *([self._reload] if self._reload is not None else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        await self.server.stop()

    async def _load(self) -> None:
        if await self._gaming():
            self._unloaded_for_game = True  # loaded once the game is over
            return
        self._restarted_at = time.monotonic()  # the game watch mustn't take a load in progress for a dead server
        try:
            await self.server.start(warm=await self._body("Hi."))
        except Exception:
            log.warning("Local brain: couldn't start", exc_info=True)

    # ------------------------------------------------------------------ events (the event loop: never blocks)
    def _on_event(self, event: Event) -> None:
        try:
            self._handle(event)
        except Exception:
            log.warning("Local brain shadow failed on %s", type(event).__name__, exc_info=True)

    def _handle(self, event: Event) -> None:
        match event:
            case BrainStarted(text=text, source=source, turn_id=turn_id) if source in USER_SOURCES:
                self._turns[turn_id] = {"text": text, "source": source, "route": "brain", "tools": [], "first": None}
                if len(self._turns) > 1:  # two turns at once (a text while you talk): tools can't be told apart
                    for turn in self._turns.values():
                        turn["overlapped"] = True
            case ToolCallStarted(name=name) if self._turns:
                self._turns[max(self._turns)]["tools"].append(name)
            case ToolCallFinished() if self._turns:
                turn = self._turns[max(self._turns)]
                if turn["first"] is None:
                    turn["first"] = list(turn["tools"])  # her first step: what she called before any result came back
            case BrainFinished(turn_id=turn_id) if turn_id in self._turns:
                turn = self._turns.pop(turn_id)
                tools, first = turn.pop("tools"), turn.pop("first")
                brain = {
                    "tools": tools,
                    "first_tools": tools if first is None else first,
                    "first_word_s": _round(event.first_word_s),
                    "model": event.model,
                    "reply": event.text[:_CLIP],
                }
                if event.error:
                    brain["error"] = event.error[:200]
                if event.interrupted:
                    brain["interrupted"] = True
                self._queue_turn(turn, brain, event.text)
            case FastChatStarted(text=text, source=source) if source in USER_SOURCES:
                self._fast = {"text": text, "source": source, "route": "fast_chat"}
            case FastChatFinished(passed=None) if self._fast is not None:
                turn, self._fast = self._fast, None
                brain = {"tools": [], "first_tools": [], "first_word_s": _round(event.first_word_s),
                         "model": event.model, "reply": event.said[:_CLIP]}  # fmt: skip
                self._queue_turn(turn, brain, event.said)

    def _queue_turn(self, turn: dict[str, Any], brain: dict[str, Any], reply: str) -> None:
        history = self.history()
        self._history.append((turn["text"][:_CLIP], reply[:_CLIP]))  # clipped: long replies would fill its context
        if self.config.mode != "shadow":
            return  # it answers turns itself (``router``): only the conversation is kept, for its next turn
        job = {**turn, "brain": brain, "history": history, "ts": round(self._clock(), 3)}
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                dropped = self._queue.get_nowait()  # the oldest goes: the newest says the most about now
                self._write({k: dropped[k] for k in _KEPT} | {"skipped": "busy"})
        self._queue.put_nowait(job)

    # ------------------------------------------------------------------ the worker
    async def _work(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._shadow(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Local brain shadow turn failed", exc_info=True)

    async def _shadow(self, job: dict[str, Any]) -> None:
        history = job.pop("history")
        record = {k: job[k] for k in _KEPT}
        if job.get("overlapped"):
            record["skipped"] = "overlapped"
        elif await self._gaming():
            record["skipped"] = "game"
        elif not (self.server.ready and self.server.running):
            record["skipped"] = "not_loaded"
        else:
            try:
                local = await self._ask(job["text"], history)
            except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                record["skipped"] = "error"
                record["error"] = f"{type(exc).__name__}: {exc}"[:200]
            else:
                record["local"] = local
                # Judged on her first step, as the local model's only step is: what she called before any result.
                record["agree"] = set(record["brain"]["first_tools"]) == {c["name"] for c in local["tools"]}
        self._write(record)

    def history(self) -> list[tuple[str, str]]:
        """The last ``history_turns`` exchanges, clipped, oldest first."""
        return list(self._history) if self.config.history_turns else []

    async def ready(self) -> bool:
        """Loaded and no game in front: it can be asked now."""
        return self.server.ready and self.server.running and not await self._gaming()

    async def body(self, text: str, history: list[tuple[str, str]]) -> dict[str, Any]:
        """A request for ``text``, with the same prompt and tools as every other, so the server's cache is used."""
        return await self._body(text, history)

    async def _body(self, text: str, history: list[tuple[str, str]] = ()) -> dict[str, Any]:  # type: ignore[assignment]
        if self._prompt is None:
            self._prompt = await self._prompt_factory()
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._prompt}]
        for said, reply in history:
            messages += [{"role": "user", "content": said}, {"role": "assistant", "content": reply or "Okay."}]
        messages.append({"role": "user", "content": text})
        return {
            "messages": messages,
            "tools": self.tools,
            "temperature": 0.2,
            "max_tokens": _MAX_TOKENS,
            "cache_prompt": True,
            "id_slot": SLOTS["shadow"],
            "chat_template_kwargs": {"enable_thinking": False},
        }

    async def _ask(self, text: str, history: list[tuple[str, str]]) -> dict[str, Any]:
        """One streamed reply: its first step only (tool calls are recorded, never run)."""
        body = await self._body(text, history) | {"stream": True}
        started = time.perf_counter()
        first: float | None = None
        parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        timings: dict[str, Any] = {}
        async with (
            asyncio.timeout(_REQUEST_TIMEOUT_S),
            local_client(timeout=_REQUEST_TIMEOUT_S, transport=self.transport) as client,
        ):
            async with client.stream("POST", f"{self.server.url}/v1/chat/completions", json=body) as response:
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}: {(await response.aread())[:200]!r}")
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line[6:])
                    timings = chunk.get("timings") or timings
                    for choice in chunk.get("choices") or ():
                        delta = choice.get("delta") or {}
                        content = delta.get("content") or ""
                        if content:
                            if first is None and content.strip():
                                first = time.perf_counter() - started
                            parts.append(content)
                        for call in delta.get("tool_calls") or ():
                            if first is None:
                                first = time.perf_counter() - started
                            slot = calls.setdefault(call.get("index", 0), {"name": "", "arguments": ""})
                            fn = call.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
        reply = "".join(parts).strip()
        return {
            "tools": [
                {"name": c["name"], "args": self._redact(c["name"], _args(c["arguments"]))}
                for _, c in sorted(calls.items())
            ],
            "reply": reply[:_CLIP],
            "written": bool(_WRITTEN.search(reply)),  # markdown in what would have been spoken
            "delegated": delegated(reply),  # it would have handed the turn to Claude
            "first_s": _round(first),
            "total_s": _round(time.perf_counter() - started),
            "prompt_ms": _round(timings.get("prompt_ms")),
            "decode_tps": _round(timings.get("predicted_per_second"), 1),
        }

    def _redact(self, name: str, args: Any) -> Any:
        """As the executor logs it: a private argument (text she'd type, maybe a password) by its length only."""
        private = self._private.get(name)
        if not private or not isinstance(args, dict):
            return args
        return {k: f"<{len(str(v))} chars>" if k in private else v for k, v in args.items()}

    def _prune(self) -> None:
        """Drop lines older than ``_RETENTION_S`` (once, at start)."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return
        cutoff = self._clock() - _RETENTION_S
        kept = [line for line in lines if _ts(line) >= cutoff]
        if len(kept) < len(lines):
            try:
                self.path.write_text("".join(kept), encoding="utf-8")
            except OSError:
                log.warning("Couldn't trim the local brain shadow log", exc_info=True)

    def _write(self, record: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("Couldn't write the local brain shadow log", exc_info=True)

    # ------------------------------------------------------------------ games
    async def gaming(self) -> bool:
        """A game is in front (the addressee check, sharing the model, skips its turns then too)."""
        return await self._gaming()

    async def _gaming(self) -> bool:
        if self._lent_file is not None:
            try:
                if await asyncio.to_thread(gpu_lent, self._lent_file):
                    return True
            except Exception:
                log.debug("Couldn't tell whether the GPU is lent", exc_info=True)
        try:
            return bool(await asyncio.to_thread(self._game))
        except Exception:
            log.debug("Couldn't tell what's in front", exc_info=True)
            return False

    async def _watch_games(self) -> None:
        while True:
            await asyncio.sleep(_GAME_POLL_S)
            try:
                await self._check_games()
            except Exception:  # the watch must outlive anything: it's what gives a game the GPU's memory back
                log.warning("Local brain: the game watch failed once", exc_info=True)

    async def _check_games(self) -> None:
        now = time.monotonic()
        if await self._gaming():
            self._clear_since = None
            if self._gaming_since is None:
                self._gaming_since = now
            unload = self.config.game_unload_s
            if unload and self.server.running and now - self._gaming_since >= unload:
                log.info("Local brain: a game is in front; unloading the model")
                self._unloaded_for_game = True
                if self._reload is not None:
                    self._reload.cancel()
                await self.server.stop()  # at once, even while it's still loading
            return
        self._gaming_since = None
        if self._clear_since is None:
            self._clear_since = now
        if self._reload is not None and not self._reload.done():
            return  # loading in the background; the watch keeps looking meanwhile
        if self._unloaded_for_game and now - self._clear_since >= self.config.game_reload_s:
            self._unloaded_for_game = False
            log.info("Local brain: no game for a while; loading the model again")
        elif self._unloaded_for_game or self.server.running or now - self._restarted_at < _RESTART_EVERY_S:
            return
        else:
            log.warning("Local brain: llama-server isn't running; starting it again")
        self._restarted_at = now  # it died or never came up: try again once in a while, never in a loop
        self._reload = asyncio.create_task(self._restart(), name="local-brain-reload")

    async def _restart(self) -> None:
        try:
            await self.server.stop()
            await self.server.start(warm=await self._body("Hi."))
        except Exception:
            log.warning("Local brain: couldn't start", exc_info=True)


_KEPT = ("ts", "source", "route", "text", "brain")


def _ts(line: str) -> float:
    try:
        return float(json.loads(line).get("ts", 0))
    except (ValueError, AttributeError):
        return 0.0


def _args(raw: str) -> Any:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_raw": raw[:_CLIP]}


def _round(value: Any, digits: int = 3) -> float | None:
    return round(float(value), digits) if isinstance(value, int | float) else None


# ---------------------------------------------------------------------------------------------------- the report


def read(path: Path, since: float = 0.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("ts", 0) >= since:
            out.append(row)
    return out


def _pct(values: list[float], q: float) -> str:
    values = sorted(v for v in values if v is not None)
    if not values:
        return "-"
    return f"{values[min(len(values) - 1, int(q * len(values)))]:.2f} s"


def report(rows: list[dict[str, Any]]) -> str:
    """How the local model's choices compare with hers, overall and by what she did."""
    if not rows:
        return "No shadowed turns yet (local_brain.log_file)."
    run = [r for r in rows if "local" in r]
    skipped = Counter(r.get("skipped") for r in rows if "skipped" in r)
    lines = [f"{len(rows)} turns, {len(run)} shadowed" + (f", skipped: {dict(skipped)}" if skipped else "")]
    if not run:
        return "\n".join(lines)
    agree = sum(bool(r.get("agree")) for r in run)
    lines.append(f"Same tools as her: {agree}/{len(run)} ({agree / len(run):.0%})")
    handed = sum(bool(r["local"].get("delegated")) for r in run)
    if handed:
        kept = [r for r in run if not r["local"].get("delegated")]
        same = sum(bool(r.get("agree")) for r in kept)
        lines.append(
            f"Would have handed to Claude: {handed}/{len(run)}; of the {len(kept)} it kept, same tools as her: {same}"
        )
    written = sum(bool(r["local"].get("written")) for r in run if not r["local"]["tools"])
    if written:
        lines.append(f"Markdown in a reply that would have been spoken: {written}")
    lines.append(
        "First word or tool: local p50 {} p90 {}; hers p50 {} p90 {}".format(
            _pct([r["local"].get("first_s") for r in run], 0.5),
            _pct([r["local"].get("first_s") for r in run], 0.9),
            _pct([r["brain"].get("first_word_s") for r in run], 0.5),
            _pct([r["brain"].get("first_word_s") for r in run], 0.9),
        )
    )
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in run:
        tools = r["brain"].get("first_tools", r["brain"]["tools"])
        key = "fast_chat" if r["route"] == "fast_chat" else (tools[0] if tools else "(no tool)")
        groups[key].append(r)
    lines.append("")
    lines.append("By what she did first (her first tool), most common first:")
    for key, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        same = sum(bool(r.get("agree")) for r in group)
        wrong = Counter(
            ",".join(c["name"] for c in r["local"]["tools"]) or "(no tool)" for r in group if not r.get("agree")
        ).most_common(2)
        note = "; local instead: " + ", ".join(f"{k} x{n}" for k, n in wrong) if wrong else ""
        lines.append(f"  {key:28} {same}/{len(group)} same{note}")
    times = [r["local"]["first_s"] for r in run if r["local"].get("first_s") is not None]
    if times:
        lines.append(f"\nLocal first word mean {statistics.mean(times):.2f} s over {len(times)} turns")
    return "\n".join(lines)
