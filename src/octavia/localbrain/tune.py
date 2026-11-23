"""Teaching the local model from her own turns (docs/local-brain.md, "Fine-tuning"): her Claude sessions on this PC
hold every turn of the user's she answered, exactly as Claude was shown it, with Claude's tool calls and reply. Those
become examples of what the local model should have done with the same turn, in the same request it gets when it
answers turns (``delegation.focused_prompt``, ``shadow.routed_tools``):

* **answer**: Claude answered in a few spoken words with no tool: that reply.
* **tool**: Claude's whole turn was one call of a tool the local model may call itself (``route_tools`` and the ones
  that only look): that call, then Claude's reply to its result.
* **delegate**: anything else (another tool, several steps, a long or written answer, a claim the router would refuse):
  ``DELEGATE: <why>``.

Every turn is labelled by what Claude did, also the ones the router never asks it about (``gate``: a note, too long,
a follow-up, a policy word): they teach it which kinds of turn are Claude's, and the router still sends them to
Claude. Only turns the user said themselves count (the routing log's source, or the turn's header), and a turn with
anything that looks like a secret (a key, a token, a password, a card number) is left out, with its conversation.
The newest ``held_share`` of the turns the router would ask it about are held out and never trained on, nor is any
turn with the same words: ``score`` replays them through the router's own local turn
(``router.RoutedBrain._local_turn``, its checks and confidence bar), so a score says what the user would have heard.

  uv run python -m octavia.localbrain.tune build [--root MAIN] [--out DIR]
  uv run python -m octavia.localbrain.tune eval --url http://127.0.0.1:8092 --label tuned [--lora 1.0] [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import random
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from octavia.core.turn_types import TEXTED_NOTE
from octavia.localbrain.delegation import (
    _CONTEXT_NOTES,
    CLAIMED,
    DELEGATE,
    READ_TOOLS,
    UNCHECKED,
    echoes_context,
    follows_up,
    policy,
    required_note,
)

TRANSCRIPTS = "data/claude-home/projects/*-data-brain/*.jsonl"
ROUTING_LOG = "data/routing_log.jsonl"
OUT = Path("E:/local-llm/finetune/data/route")
_USER_SOURCES = ("voice", "text", "discord", "window")
_MCP = "mcp__assistant__"
_MATCH_S = 300.0  # a routing-log line and a transcript turn this close, with the same words, are one turn
_REPLY_CHARS = 220  # longer than this, and it's an explanation: Claude's
_LINE_CHARS = 400  # each history line, as the router clips it
_HISTORY = 3  # exchanges shown, as ``local_brain.history_turns``
# What render() puts in the header for the source: not a note.
_SOURCE_NOTES = (
    "typed, not spoken",
    "texted from their phone over Discord, not spoken",
    "typed in your chat window on the PC, not spoken; your reply is shown there",
)
# Claude is told what the local model said since its last answer (``RoutedBrain._with_notes``): the local model isn't.
_CLAUDE_ONLY_NOTES = ("since your last answer, your local brain answered them itself",)
_SCHEDULED_NOTE = "this is your scheduled job"
# Her own checks of the brain ("Reply with just the word: pong"), and Claude's failures said as her reply.
_NOT_SAID = re.compile(r"^\s*reply with (?:just |only )?(?:the|one) (?:one )?word", re.I)
_FAILED = re.compile(r"hit your (?:session|usage|weekly) limit|API Error|credit balance is too low|rate limit", re.I)
# Notes that start like this are notes of their own, never a piece of the one before (the route log's ``note``s).
_NOTE_STARTS = (
    "you were interrupted",
    "you announced this on your",
    "screen task #",
    "background task #",
    "[your own pop-up panels",
    "the user is on a",
    "you were about to shut",
    "they sent a file",
    TEXTED_NOTE[:25],
)
# Looks like a secret: such a turn and its conversation never go into a training file.
SECRET = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|[A-Fa-f0-9]{32,}"
    # a long run of letters and digits mixed, like a key (not a path, a URL or a tool's name)
    r"|(?=[A-Za-z0-9+_-]{0,60}\d)(?=[A-Za-z0-9+_-]{0,60}[A-Za-z])[A-Za-z0-9+_-]{40,}={0,2})"
    r"|\b(?:password|passcode|passwd|pin code|api key|secret key|access token|recovery code)s?\b\W{0,3}(?:is|was|:|=)"
    r"|\b(?:password|passcode|passwd|pin)\s+(?!reset|manager|for|to|of|and|on)[^\s,.?!]{4,}"  # "password hunter2"
    r"|\b[sprk]k_(?:live|test)_[A-Za-z0-9]{12,}|\bbearer\s+[A-Za-z0-9._~+/-]{12,}"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"  # a link with a password in it
    r"|\b(?:verification|one[- ]time|2fa|security|login) code\W{0,3}(?:is\W*)?\d{4,8}\b"
    r"|\b(?:\d[ -]?){13,19}\b|\b\d{3}-\d{2}-\d{4}\b",
    re.I,
)
# Her tools grouped by what they need, for the why of a handover (the route log keeps it; it's never said).
_NEEDS = (
    ("canvas_", "needs Canvas"),
    ("ticktick_", "needs their to-dos"),
    ("gmail", "needs their email"),
    ("calendar", "needs their calendar"),
    ("spotify", "needs their music"),
    ("garmin", "needs their watch data"),
    ("web", "needs a web search"),
    ("screen", "needs the screen"),
    ("remember", "needs remembering"),
    ("memory", "needs remembering"),
    ("improvement", "about her updates"),
    ("objective", "about her objectives"),
    ("background_task", "needs a background task"),
    ("discord", "needs Discord"),
    ("futures", "needs her notes on them"),
)


@dataclass
class Turn:
    """One of the user's turns Claude answered, as Claude's session holds it."""

    ts: float
    session: str
    rendered: str  # exactly what the brain was sent (``UserTurn.render``)
    text: str = ""
    source: str = "voice"
    notes: list[str] = field(default_factory=list)
    steps: list[list[dict[str, Any]]] = field(default_factory=list)  # tool calls, step by step, before each result
    results: list[str] = field(default_factory=list)  # each tool result, in order
    reply: str = ""  # everything Claude said in the turn
    history: list[tuple[str, str]] = field(default_factory=list)  # (they said, she said) before it, oldest first


@dataclass
class Example:
    id: str
    ts: float
    source: str
    user: str  # the turn as the local model is sent it
    history: list[tuple[str, str]]
    gate: str | None  # why the router sends it to Claude without asking the local model
    label: str  # "answer", "tool" or "delegate"
    target: dict[str, Any]  # the assistant message it should give
    after: dict[str, Any] | None = None  # for "tool": the tool's result and Claude's reply to it
    claude_tools: list[str] = field(default_factory=list)
    reply: str = ""


# ---------------------------------------------------------------------------------------------------- reading
def _ts(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def split_header(rendered: str) -> tuple[str, list[str], str] | None:
    """A rendered turn's (source, notes, words), or None when it isn't one ("[stamp; notes]\\nwords")."""
    if not rendered.startswith("[") or "]\n" not in rendered:
        return None
    header, words = rendered[1:].split("]\n", 1)
    pieces = header.split("; ")[1:]  # the first is the time
    source = "voice"
    for name, note in zip(("text", "discord", "window"), _SOURCE_NOTES, strict=True):
        if header.find("; " + note) >= 0:
            source = name
            first = note.split("; ")
            for i in range(len(pieces) - len(first) + 1):
                if pieces[i : i + len(first)] == first:
                    del pieces[i : i + len(first)]
                    break
    notes: list[str] = []
    for piece in pieces:
        new = piece.casefold().startswith(_NOTE_STARTS) or piece.startswith(_CONTEXT_NOTES)
        # a context note with "; " in it ("work quietly: ...; when it's done, ...") comes apart here: put it back
        if notes and not new and notes[-1].startswith((*_CONTEXT_NOTES, TEXTED_NOTE[:25])):
            notes[-1] += "; " + piece
        else:
            notes.append(piece)
    notes = [n for n in notes if not n.startswith(_CLAUDE_ONLY_NOTES)]
    return source, notes, words.strip()


def read_session(lines: Iterable[str], session: str = "") -> list[Turn]:
    """The user's turns in one of Claude's session files, each with its steps, results, reply and what came before."""
    turns: list[Turn] = []
    current: Turn | None = None
    step: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("type")
        content = (entry.get("message") or {}).get("content")
        if kind == "user":
            blocks = content if isinstance(content, list) else []
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                if current is not None:
                    if step:
                        current.steps.append(step)
                        step = []
                    current.results += [_text(b.get("content")) for b in results]
                continue
            rendered = _text(content)
            parsed = split_header(rendered)
            if current is not None and step:
                current.steps.append(step)
            step = []
            if parsed is None:
                current = None  # her own warm-up or an instruction, not the user's turn
                continue
            source, notes, words = parsed
            current = Turn(_ts(entry["timestamp"]), session, rendered, words, source, notes)
            turns.append(current)
        elif kind == "assistant" and current is not None and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    step.append({"name": str(block.get("name", "")).removeprefix(_MCP), "args": block.get("input")})
                elif block.get("type") == "text" and block.get("text", "").strip():
                    current.reply = (current.reply + " " + block["text"].strip()).strip()
    if current is not None and step:
        current.steps.append(step)
    for i, turn in enumerate(turns):
        earlier = [(t.text, t.reply) for t in turns[max(0, i - _HISTORY) : i]]
        turn.history = [(" ".join(a.split())[:_LINE_CHARS], " ".join(b.split())[:_LINE_CHARS]) for a, b in earlier]
    return turns


def read_turns(root: Path) -> list[Turn]:
    turns: list[Turn] = []
    for path in sorted(glob.glob(str(root / TRANSCRIPTS))):
        with open(path, encoding="utf-8", errors="replace") as f:
            turns += read_session(f, Path(path).stem)
    return sorted(turns, key=lambda t: t.ts)


def sources(root: Path) -> dict[str, list[tuple[float, str]]]:
    """From the routing log: when each set of words was said, and by what (voice, discord, scheduled...)."""
    said: dict[str, list[tuple[float, str]]] = {}
    path = root / ROUTING_LOG
    if not path.exists():
        return said
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("route") == "brain" and isinstance(row.get("text"), str):
            said.setdefault(" ".join(row["text"].split()), []).append((float(row["ts"]), str(row.get("source"))))
    return said


def source_of(turn: Turn, said: dict[str, list[tuple[float, str]]]) -> str | None:
    """Who said it, from the routing log line with the same words nearest in time, or None when there isn't one."""
    near = [(abs(ts - turn.ts), source) for ts, source in said.get(" ".join(turn.text.split()), ())]
    near = [n for n in near if n[0] <= _MATCH_S]
    return min(near)[1] if near else None


# ---------------------------------------------------------------------------------------------------- labels
def gate(turn: Turn, core: Any, max_words: int) -> str | None:
    """Why the router sends this turn to Claude without asking the local model (``RoutedBrain.claudes``), or None.
    What it can't know from a transcript: whether a screen task was running (``busy``)."""
    if required_note(turn.notes):
        return "notes"
    said = core(turn.text)
    if not said:
        return "empty"
    if len(said.split()) > max_words:
        return "long"
    lines = [line for said_, reply in turn.history for line in (("user", said_), ("assistant", reply))]
    if (why := follows_up(said, lines)) is not None:
        return f"context: {why}"
    return policy(said)


def needs(tools: Sequence[str]) -> str:
    for part, why in _NEEDS:
        if any(part in t.casefold() for t in tools):
            return why
    return "needs her other tools" if tools else "needs real reasoning"


def label(turn: Turn, allowed: frozenset[str]) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """What the local model should have said, from what Claude did: (label, its message, the second step after a
    tool). Judged on what Claude did alone, also for a turn the router never asks it about: those teach it which
    kinds of turn are Claude's."""
    calls = [c for s in turn.steps for c in s]
    reply = " ".join(turn.reply.split())
    if len(calls) == 1 and calls[0]["name"] in allowed and isinstance(calls[0]["args"], dict) and turn.results:
        short = bool(reply) and len(reply) <= _REPLY_CHARS and not _WRITTEN.search(reply)
        if short or calls[0]["name"] not in READ_TOOLS:  # a look that took a long answer is still Claude's
            call = {"name": calls[0]["name"], "arguments": calls[0]["args"]}
            after = {"result": turn.results[0], "reply": reply} if short else None
            return "tool", {"content": "", "tool_calls": [call]}, after
    if (
        not calls
        and reply
        and len(reply) <= _REPLY_CHARS
        and not _WRITTEN.search(reply)
        and not CLAIMED.search(reply)
        and not UNCHECKED.search(reply)
        and not echoes_context(reply, turn.notes)
    ):
        return "answer", {"content": reply}, None
    return "delegate", {"content": f"{DELEGATE}: {needs([c['name'] for c in calls])}"}, None


_WRITTEN = re.compile(r"\*\*|^\s*(?:[-*•]|#{1,6}|\d+\.)\s|https?://|```", re.M)


def private(turn: Turn) -> bool:
    """Something in the turn, its conversation or Claude's answer looks like a secret."""
    blob = "\n".join([turn.rendered, turn.reply, *(a + "\n" + b for a, b in turn.history), *turn.results[:1]])
    blob += json.dumps([c for s in turn.steps for c in s], ensure_ascii=False)
    return bool(SECRET.search(blob))


def examples(
    turns: Iterable[Turn], said: dict[str, list[tuple[float, str]]], allowed: frozenset[str], core: Any, max_words: int
) -> tuple[list[Example], Counter[str]]:
    out: list[Example] = []
    dropped: Counter[str] = Counter()
    for turn in turns:
        # The routing log names the source, but misses some turns: then the turn's own header does (a scheduled
        # job's turn says so in its note).
        source = source_of(turn, said) or turn.source
        if any(note.startswith(_SCHEDULED_NOTE) for note in turn.notes):
            source = "scheduled"
        if source not in _USER_SOURCES:
            dropped[f"source {source}"] += 1
            continue
        if _NOT_SAID.search(turn.text) or _FAILED.search(turn.reply):
            dropped["her own test, or Claude failed"] += 1
            continue
        if private(turn):
            dropped["looks private"] += 1
            continue
        gated = gate(turn, core, max_words)
        kind, target, after = label(turn, allowed)
        out.append(
            Example(
                id=f"{turn.session[:8]}-{int(turn.ts)}",
                ts=turn.ts,
                source=source,
                user=_without_claude_notes(turn),
                history=turn.history,
                gate=gated,
                label=kind,
                target=target,
                after=after,
                claude_tools=[c["name"] for s in turn.steps for c in s],
                reply=" ".join(turn.reply.split())[: _REPLY_CHARS * 2],
            )
        )
    return out, dropped


def _without_claude_notes(turn: Turn) -> str:
    """The turn as the local model is sent it: without what only Claude is told."""
    header, words = turn.rendered[1:].split("]\n", 1)
    pieces = [p for p in header.split("; ") if not p.startswith(_CLAUDE_ONLY_NOTES)]
    return "[" + "; ".join(pieces) + "]\n" + words


def split(rows: list[Example], held_share: float, seed: int = 0) -> tuple[list[Example], list[Example]]:
    """(train, held out): the newest ``held_share`` of the turns the router asks it about are held out, and no
    training turn has the same words as one of them. Gated turns only ever train."""
    asked = sorted((r for r in rows if r.gate is None), key=lambda r: r.ts)
    held = asked[len(asked) - round(len(asked) * held_share) :]
    words = {_words(r.user) for r in held}
    held_ids = {r.id for r in held}
    train = [r for r in rows if r.id not in held_ids and _words(r.user) not in words]
    random.Random(seed).shuffle(train)
    return train, held


def _words(user: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", user.split("]\n", 1)[-1].casefold()))


# ---------------------------------------------------------------------------------------------------- setup
async def setup(root: Path) -> tuple[Any, str, list[dict[str, Any]], Any]:
    """Her config (``root``'s), the local model's prompt and tools exactly as the app builds them when it answers
    turns (``app._start_local_brain``), and its tool registry."""
    from octavia.app import create_tts
    from octavia.config import load_config, load_dotenv_file
    from octavia.localbrain.shadow import openai_tools, routed_prompt, routed_tools
    from octavia.tools.registry import build_registry

    load_dotenv_file(root)
    config = load_config(path=root / "config" / "local.yaml", root=root)
    registry = build_registry(config.tools.plugins, config.tools.enabled, config.tools.disabled)
    mine = routed_tools(registry.specs(), config.local_brain)
    try:
        voice_style = create_tts(config).style_hint()
    except Exception:  # no voice set up here: the prompt without its cues
        voice_style = None
    prompt = await routed_prompt(config, voice_style, [s.name for s in mine])
    return config, prompt, openai_tools(mine), registry


def build(root: Path, out: Path, held_share: float = 0.35) -> dict[str, Any]:
    from octavia.core.fast_path import FastPath

    config, prompt, tools, _ = asyncio.run(setup(root))
    allowed = frozenset({*config.local_brain.route_tools, *READ_TOOLS})
    core = FastPath(config.assistant.wake_phrase, config.assistant.name).core
    rows, dropped = examples(read_turns(root), sources(root), allowed, core, config.local_brain.route_max_words)
    train, held = split(rows, held_share)
    out.mkdir(parents=True, exist_ok=True)
    (out / "setup.json").write_text(json.dumps({"prompt": prompt, "tools": tools}, indent=1), encoding="utf-8")
    for name, part in (("train", train), ("held", held)):
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for row in part:
                f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
    summary = {
        "turns": len(rows),
        "dropped": dict(dropped),
        "train": dict(Counter(r.label for r in train)),
        "train_gated": sum(r.gate is not None for r in train),
        "held": dict(Counter(r.label for r in held)),
        "gates": dict(Counter(r.gate for r in rows).most_common()),
        "prompt_chars": len(prompt),
        "tools": [t["function"]["name"] for t in tools],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def load(path: Path) -> list[Example]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        data = json.loads(line)
        data["history"] = [tuple(pair) for pair in data["history"]]
        rows.append(Example(**data))
    return rows


# ---------------------------------------------------------------------------------------------------- scoring
@dataclass
class Outcome:
    id: str
    label: str
    decision: str  # "keep" (it answered), "delegate", or the router's fallback reason
    tool: str | None = None  # the tool it called first, when it kept the turn
    said: str = ""
    want_tool: str | None = None
    confidence: float | None = None
    seconds: float = 0.0

    @property
    def verdict(self) -> str:
        """right (did what Claude did: kept it the same way, or handed it over), safe (handed over a turn it could
        have kept), wrong (kept a turn that was Claude's, or did something else with it)."""
        if self.decision != "keep":
            return "right" if self.label == "delegate" else "safe"
        if self.label == "tool":
            return "right" if self.tool == self.want_tool else "wrong"
        if self.label == "answer":
            return "right" if self.tool is None or self.tool in READ_TOOLS else "wrong"
        return "wrong"


class _Executor:
    """Runs nothing: each call gets what the same tool returned to Claude in that turn, or a plain "Done."."""

    def __init__(self, registry: Any, results: dict[str, str]) -> None:
        self.registry = registry
        self.results = results

    async def execute(self, name: str, args: dict[str, Any], *, call_id: str = "") -> Any:
        from octavia.tools.base import ToolResult

        return ToolResult.success(self.results.get(name, "Done."))


class _Inner:
    name = "claude"
    tools: list[Any] = []
    system_prompt = ""
    session_id = None
    heartbeat = None

    def __init__(self, executor: _Executor) -> None:
        self.executor = executor


class _Rendered:
    """A turn that renders as it was really sent (its notes inside the header, as Claude saw them)."""

    def __init__(self, turn: Any, rendered: str) -> None:
        self._turn = turn
        self._rendered = rendered
        parsed = split_header(rendered)
        self.notes = tuple(parsed[1]) if parsed else ()

    def render(self) -> str:
        return self._rendered

    def __getattr__(self, name: str) -> Any:
        return getattr(self._turn, name)


async def score(
    rows: Sequence[Example],
    config: Any,
    prompt: str,
    tools: list[dict[str, Any]],
    registry: Any,
    url: str,
    lora: float | None = None,
) -> list[Outcome]:
    """Each turn through the router's own local turn (its checks, its confidence bar) against a llama-server at
    ``url``, tools answered from Claude's own results; with ``lora``, the server's first adapter at that scale."""
    import time

    import httpx

    from octavia.brain.base import TextDelta, ToolUseStarted, UserTurn
    from octavia.core.events import EventBus
    from octavia.core.fast_path import FastPath
    from octavia.localbrain.router import LocalFailed, RoutedBrain
    from octavia.localbrain.shadow import LocalShadow

    class Server:
        def __init__(self) -> None:
            self.url = url.rstrip("/")

        async def stop(self) -> None:
            pass

    async def the_prompt() -> str:
        return prompt

    class Transport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if lora is not None and request.method == "POST":
                body = json.loads(request.content) | {"lora": [{"id": 0, "scale": lora}]}
                request = httpx.Request(request.method, request.url, json=body)
            return await super().handle_async_request(request)

    cfg = config.local_brain.model_copy(update={"mode": "route", "answer_timeout_s": 60.0})
    core = FastPath(config.assistant.wake_phrase, config.assistant.name).core
    out: list[Outcome] = []
    for row in rows:
        results = {}
        if row.after is not None:
            results[row.target["tool_calls"][0]["name"]] = row.after["result"]
        shadow = LocalShadow(cfg, EventBus(), Path("unused.jsonl"), the_prompt, tools, Server(), game=lambda: False)
        brain = RoutedBrain(_Inner(_Executor(registry, results)), shadow, cfg, Path("unused.jsonl"), core=core)
        brain.transport = Transport()
        history = list(row.history)
        brain.conversation = lambda h=history: [line for a, b in h for line in (("user", a), ("assistant", b))]
        words = row.user.split("]\n", 1)[1]
        turn = _Rendered(UserTurn(text=words, source=row.source), row.user)
        want = row.target["tool_calls"][0]["name"] if row.label == "tool" else None
        outcome = Outcome(row.id, row.label, "keep", want_tool=want)
        started = time.perf_counter()
        try:
            async for event in brain._local_turn(turn, "route"):  # type: ignore[arg-type]
                if isinstance(event, ToolUseStarted) and outcome.tool is None:
                    outcome.tool = event.name
                elif isinstance(event, TextDelta):
                    outcome.said += event.text
        except LocalFailed as exc:
            outcome.decision = "delegate" if exc.reason == "delegated" else exc.reason
        outcome.confidence = brain._confidence
        outcome.seconds = round(time.perf_counter() - started, 3)
        out.append(outcome)
    return out


def summarize(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    verdicts = Counter(o.verdict for o in outcomes)
    n = len(outcomes) or 1
    kept = [o for o in outcomes if o.decision == "keep"]
    keepable = [o for o in outcomes if o.label != "delegate"]
    return {
        "n": len(outcomes),
        "agreement": round(verdicts["right"] / n, 3),
        "right": verdicts["right"],
        "safe_misses": verdicts["safe"],
        "wrong": verdicts["wrong"],
        "kept": len(kept),
        "kept_right": sum(o.verdict == "right" for o in kept),
        "coverage": round(sum(o.verdict == "right" for o in kept) / (len(keepable) or 1), 3),
        "decisions": dict(Counter(o.decision for o in outcomes)),
        "median_s": sorted(o.seconds for o in outcomes)[len(outcomes) // 2] if outcomes else None,
    }


def main(argv: Sequence[str] | None = None) -> None:
    from octavia.config import find_project_root

    ap = argparse.ArgumentParser(prog="python -m octavia.localbrain.tune")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="her turns -> train.jsonl, held.jsonl, setup.json")
    b.add_argument("--out", type=Path, default=OUT)
    b.add_argument("--held", type=float, default=0.35)
    e = sub.add_parser("eval", help="score a llama-server on the held-out turns")
    e.add_argument("--url", required=True)
    e.add_argument("--label", required=True)
    e.add_argument("--lora", type=float, default=None)
    e.add_argument("--out", type=Path, default=OUT)
    e.add_argument("--set", default="held")
    for parser in (b, e):
        parser.add_argument("--root", type=Path, default=None, help="her checkout (data/, config/local.yaml)")
    a = ap.parse_args(argv)
    root = a.root or find_project_root()
    if a.cmd == "build":
        print(json.dumps(build(root, a.out, a.held), indent=1))
        return
    config, prompt, tools, registry = asyncio.run(setup(root))
    saved = json.loads((a.out / "setup.json").read_text(encoding="utf-8"))
    if saved["prompt"] != prompt or saved["tools"] != tools:
        print("note: her prompt or tools changed since the dataset was built; scoring with the saved ones")
    rows = [r for r in load(a.out / f"{a.set}.jsonl") if r.gate is None]
    outcomes = asyncio.run(score(rows, config, saved["prompt"], saved["tools"], registry, a.url, a.lora))
    report = {
        "label": a.label,
        "url": a.url,
        "lora": a.lora,
        **summarize(outcomes),
        "outcomes": [asdict(o) | {"verdict": o.verdict} for o in outcomes],
    }
    (a.out / f"eval-{a.label}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "outcomes"}, indent=1))


if __name__ == "__main__":
    main()
