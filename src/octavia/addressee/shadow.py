"""Was that for her? In shadow: what the user says near the PC without her name is judged by the local model, and only
written down (docs/addressee.md).

The first step toward talking to her without the wake word. Nothing here answers, wakes her or holds anything up:

* **What it hears:** only what her second listen for the wake word (``wake/second_chance.py``) already transcribes
  while she waits for her name, and finds doesn't start with it. No other transcription, no audio kept, nothing while
  she speaks, listens or works on a turn.
* **What decides:** the local model (``octavia.localbrain``, its own slot of the same llama-server, so the turn shadow
  keeps its cached prompt), asked for ``{"for_her": true|false, "why": ...}`` with that answer begun for it, so its
  first token is the verdict. The score is the model's own odds of "true" over "false" there (its logprobs); she
  would have answered at ``threshold``.
  It's told the words, when she last spoke and what was said then, the app in front (and its title), call apps on
  the microphone and apps playing sound (``situation.py``).
* **What's kept:** one line per thing overheard in ``addressee.log_file``, written ``repeat_window_s`` (+20 s) after it was
  heard. The words (and the model's reason) are kept only when she would have answered, or when the user said it
  again with her name soon after (``repeated``: it was for her, so a miss when she wouldn't have). Everything else is
  kept as counts and context only: the app in front by name, never its title. Lines older than 30 days go at start.
  ``octavia addressee forget`` deletes the file.
* **Games:** like the turn shadow: nothing is asked while a full-screen game is in front, or while the model is
  unloaded for one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import statistics
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from rapidfuzz import fuzz

from octavia.addressee.situation import AudioWatch, Situation, foreground
from octavia.config import AddresseeConfig
from octavia.core.events import (
    BrainFinished,
    BrainStarted,
    Event,
    EventBus,
    FastChatFinished,
    FastChatStarted,
    NoSpeech,
    SpeakingFinished,
    Transcript,
    WakeDetected,
)
from octavia.core.pauses import only_fillers
from octavia.core.text import normalize_text
from octavia.localbrain.server import SLOTS
from octavia.wake.second_chance import after_name

log = logging.getLogger(__name__)

USER_SOURCES = frozenset({"voice", "text", "discord", "window"})
_QUEUE = 4
_REQUEST_TIMEOUT_S = 20.0
_MAX_TOKENS = 60
_CLIP = 200
_RETENTION_S = 30 * 86400
_FLUSH_S = 5.0
_RECENT_S = 300.0  # her last exchange is shown to the model when it's this recent
_WOKE_FOR_S = 20.0  # the request after a wake word comes this soon after it
_SAME = 80.0  # rapidfuzz ratio of the whole thing said: said again with her name
THRESHOLDS = (0.5, 0.7, 0.9)

# Her answer is begun for the model, so its very first token is the verdict ("true" or "false"): about 0.04 s once the
# prompt is cached, against ~0.14 s for a JSON grammar that spends eight tokens on '{"for_her":' first.
_PREFIX = '{"for_her":'
_WHY = re.compile(r'"why"\s*:\s*"((?:[^"\\]|\\.)*)')


def system_prompt(name: str) -> str:
    return f"""You decide whether something the user said out loud was meant for {name}, their voice assistant on this \
PC, when they didn't say her name first. {name} hears everything said near the PC: the user talking to her, to friends \
on a call, to people in the room, to themselves, reading aloud, singing, and voices from videos or games playing.

Say it's for her only when the words read as a request, question or reply directed at an assistant: a command \
("turn the volume down", "open Spotify"), a question she could answer ("what's the weather tomorrow", "how long is \
left on the timer"), or a direct follow-up to what she just said. Say it isn't when it reads as talk to other people \
("dude, push left", "I'll be there in five"), thinking out loud ("where did I put that", "ugh, come on"), reaction to \
a game or video, words that sound like they come from a video or song, or a fragment. Her name later in a sentence \
("what time is it, {name}") counts as talking to her; her name in talk about her ("{name} told me") doesn't.

The situation matters: on a call with friends, most speech is for them; with a video or music playing, words may come \
from it; within a few seconds after she spoke, a reply to her is likely. When unsure, it isn't for her: answering \
someone who wasn't talking to her is worse than missing a request, which the user can repeat with her name.

Answer with JSON: {{"for_her": true or false, "why": "a few words"}}."""


def describe(situation: Situation, since_her_s: float | None, last: tuple[str, str] | None, name: str) -> str:
    """The situation, as the model is told it."""
    lines = []
    if since_her_s is None:
        lines.append(f"{name} hasn't spoken since she started.")
    elif last is not None and since_her_s <= _RECENT_S:
        said, reply = last
        lines.append(
            f"{name} last spoke {since_her_s:.0f} s ago. The user had said: {json.dumps(said)} "
            f"She replied: {json.dumps(reply)}"
        )
    else:
        lines.append(f"{name} last spoke {since_her_s / 60:.0f} minutes ago.")
    if situation.app:
        title = f" ({json.dumps(situation.title[:80])})" if situation.title else ""
        lines.append(f"In front: {situation.app}{title}" + (", full screen" if situation.fullscreen else ""))
    if situation.call:
        lines.append("On a voice call in: " + ", ".join(situation.call))
    else:
        lines.append("No voice call app is using the microphone.")
    if situation.playing:
        lines.append("Playing sound: " + ", ".join(situation.playing))
    return "\n".join(lines)


@dataclass
class _Pending:
    """A judged line, held until ``repeat_window_s`` (+20 s) has passed (said again with her name meanwhile?)."""

    due: float  # monotonic
    record: dict[str, Any]
    text: str
    why: str = ""
    heard: float = 0.0  # monotonic, when it was overheard


@dataclass
class _Job:
    text: str
    audio_s: float
    ts: float
    heard: float  # monotonic
    since_her_s: float | None
    last: tuple[str, str] | None
    extra: dict[str, Any] = field(default_factory=dict)


class AddresseeShadow:
    def __init__(
        self,
        config: AddresseeConfig,
        events: EventBus,
        path: Path,
        server: Any,  # LlamaServer: ``ready``, ``running``, ``url``
        *,
        name: str,
        spellings: Sequence[Sequence[str]] = (),
        gaming: Callable[[], Awaitable[bool]] | None = None,
        audio: AudioWatch | None = None,
        front: Callable[[], tuple[str, str, bool]] = foreground,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.path = path
        self.server = server
        self.name = name
        self._spellings = [list(s) for s in spellings]
        self._gaming = gaming
        self.audio = audio if audio is not None else AudioWatch()
        self._front = front
        self._clock = clock
        self._mono = monotonic
        self._system = system_prompt(name)
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(_QUEUE)
        self._pending: deque[_Pending] = deque()
        self._tasks: list[asyncio.Task[None]] = []
        self._spoke_at: float | None = None  # monotonic: her voice last stopped
        self._said: str | None = None  # the user's last request of hers (voice, typed or texted)
        self._last: tuple[str, str] | None = None  # (what the user said, her reply) of the last finished turn
        self._woke_at: float | None = None  # a wake word, whose request (the next transcript) is still to come
        self.transport: httpx.AsyncBaseTransport | None = None  # tests answer for the server
        self._client: httpx.AsyncClient | None = None
        self._unsubscribe = events.subscribe(
            self._on_event,
            BrainStarted,
            BrainFinished,
            FastChatStarted,
            FastChatFinished,
            SpeakingFinished,
            WakeDetected,
            Transcript,
            NoSpeech,
        )

    # ------------------------------------------------------------------ life
    async def start(self) -> None:
        await asyncio.to_thread(self._prune)
        await self.audio.start()
        self._tasks = [
            asyncio.create_task(self._work(), name="addressee-shadow"),
            asyncio.create_task(self._flush_due(), name="addressee-flush"),
        ]

    async def close(self) -> None:
        self._unsubscribe()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        await self.audio.close()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._flush(everything=True)
        while not self._queue.empty():  # not judged before she stopped: counted all the same
            self._write(self._base(self._queue.get_nowait()) | {"skipped": "closing"})

    # ------------------------------------------------------------------ the event loop (never blocks)
    def overheard(self, text: str, audio_s: float) -> None:
        """Something said while she waited for her name, not starting with it (``SecondChance.overheard``)."""
        text = text.strip()
        if not text or only_fillers(text) or not normalize_text(text):
            return
        now = self._mono()
        since = None if self._spoke_at is None else round(now - self._spoke_at, 1)
        job = _Job(text, round(audio_s, 2), round(self._clock(), 3), now, since, self._last)
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                dropped = self._queue.get_nowait()  # the oldest goes
                self._write(self._base(dropped) | {"skipped": "busy"})
        self._queue.put_nowait(job)

    def _on_event(self, event: Event) -> None:
        try:
            self._handle(event)
        except Exception:
            log.warning("The addressee check failed on %s", type(event).__name__, exc_info=True)

    def _handle(self, event: Event) -> None:
        match event:
            case BrainStarted(text=text, source=source) | FastChatStarted(text=text, source=source) if (
                source in USER_SOURCES
            ):
                self._said = text
            case BrainFinished(text=reply, source=source) if source in USER_SOURCES and self._said is not None:
                self._last = (self._said[:_CLIP], reply[:_CLIP])
            case FastChatFinished(passed=None, said=reply, source=source) if (
                source in USER_SOURCES and self._said is not None
            ):
                self._last = (self._said[:_CLIP], reply[:_CLIP])
            case SpeakingFinished():
                self._spoke_at = self._mono()
            case WakeDetected(during_speech=False, phrase=phrase) if phrase not in ("(hotkey)", "(call)"):
                self._woke_at = self._mono()
            case WakeDetected() | NoSpeech():  # a hotkey or barge-in, or a wake word nothing followed: not one
                self._woke_at = None
            case Transcript(text=text) if self._woke_at is not None:
                woke, self._woke_at = self._woke_at, None
                if self._mono() - woke <= _WOKE_FOR_S:
                    self._said_again(text, woke)

    def _said_again(self, request: str, woke: float) -> None:
        """The request after a wake word: the one thing overheard shortly before that says the same was for her.

        "The same" is close as a whole, not a few shared words: "yeah" or "what" said before "what time is it" isn't
        that request (and its words would otherwise be kept)."""
        best: tuple[float, _Pending] | None = None
        for item in self._pending:
            if woke - item.heard > self.config.repeat_window_s or item.record.get("repeated"):
                continue
            score = same_request(item.text, request)
            if score is not None and (best is None or score > best[0]):
                best = (score, item)
        if best is not None:
            best[1].record["repeated"] = True
            log.info("Addressee: said again with her name (%s)", "caught" if best[1].record["for_her"] else "missed")

    # ------------------------------------------------------------------ the worker
    async def _work(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._judge(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("The addressee check failed", exc_info=True)

    def _base(self, job: _Job) -> dict[str, Any]:
        return {"ts": job.ts, "id": _short_id(job.ts), "words": len(job.text.split()), "audio_s": job.audio_s}

    async def _judge(self, job: _Job) -> None:
        record = self._base(job)
        if self._gaming is not None and await self._gaming():
            self._write(record | {"skipped": "game"})
            return
        if not (self.server.ready and self.server.running):
            self._write(record | {"skipped": "not_loaded"})
            return
        situation = await self._situation()
        record["since_her_s"] = job.since_her_s
        record["situation"] = situation.logged()
        if self._spellings:
            record["name_said"] = _name_anywhere(job.text, self._spellings)
        try:
            p, why, decide_s, total_s = await self._ask(job, situation)
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            # The kind only: a message could hold the model's words, and they can repeat what was said.
            self._write(record | {"skipped": "error", "error": type(exc).__name__})
            return
        record |= {"p": round(p, 3), "for_her": p >= self.config.threshold, "decide_s": decide_s, "total_s": total_s}
        if record["for_her"]:
            log.info("Addressee: she'd have answered line %s (%.2f)", record["id"], p)  # its words: in its own file
        # Kept until a request that comes after a wake word late in the window can still be matched to it.
        due = job.heard + self.config.repeat_window_s + _WOKE_FOR_S
        self._pending.append(_Pending(due, record, job.text, without_title(why, situation.title), job.heard))

    async def _situation(self) -> Situation:
        situation = Situation(call=self.audio.call_apps(), playing=self.audio.playing())
        try:
            situation.app, situation.title, situation.fullscreen = await asyncio.to_thread(self._front)
        except Exception:
            log.debug("Couldn't tell what's in front", exc_info=True)
        return situation

    def body(self, job: _Job, situation: Situation) -> dict[str, Any]:
        said = describe(situation, job.since_her_s, job.last, self.name)
        return {
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": f"{said}\n\nThe user said: {json.dumps(job.text)}"},
                {"role": "assistant", "content": _PREFIX},  # continued by the model, from the verdict on
            ],
            "temperature": 0,
            "max_tokens": _MAX_TOKENS,
            "cache_prompt": True,
            "id_slot": SLOTS["addressee"],
            "logprobs": True,
            "top_logprobs": 5,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    async def _ask(self, job: _Job, situation: Situation) -> tuple[float, str, float | None, float]:
        """The model's odds that it was for her, its reason, and how long it took to decide and to finish."""
        body = self.body(job, situation)
        if self._client is None:
            # One client for good: making one builds an SSL context, ~0.3 s on Windows, which was most of a verdict.
            self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S, transport=self.transport)
        client = self._client
        started = time.perf_counter()
        decided: float | None = None
        p: float | None = None
        parts: list[str] = []
        async with asyncio.timeout(_REQUEST_TIMEOUT_S):
            async with client.stream("POST", f"{self.server.url}/v1/chat/completions", json=body) as response:
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}: {(await response.aread())[:200]!r}")
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line[6:])
                    for choice in chunk.get("choices") or ():
                        parts.append((choice.get("delta") or {}).get("content") or "")
                        if p is None:
                            for token in (choice.get("logprobs") or {}).get("content") or ():
                                p = _odds(token)
                                if p is not None:
                                    decided = time.perf_counter() - started
                                    break
        total = time.perf_counter() - started
        answer = _PREFIX + "".join(parts)
        if p is None:  # no logprobs came back: the answer itself, as certain
            verdict = answer[len(_PREFIX) :].lstrip()
            if not verdict.startswith(("true", "false")):
                raise ValueError(f"no answer: {answer[:100]!r}")
            p = 1.0 if verdict.startswith("true") else 0.0
        why = ""
        if found := _WHY.search(answer):
            try:
                why = json.loads(f'"{found.group(1)}"')
            except json.JSONDecodeError:
                why = found.group(1)
        return p, why[:_CLIP], _round(decided), round(total, 3)

    # ------------------------------------------------------------------ the log
    async def _flush_due(self) -> None:
        while True:
            await asyncio.sleep(_FLUSH_S)
            self._flush()

    def _flush(self, everything: bool = False) -> None:
        now = self._mono()
        while self._pending and (everything or self._pending[0].due <= now):
            item = self._pending.popleft()
            record = item.record
            if record.get("for_her") or record.get("repeated"):  # the words only where they're needed
                record["text"] = item.text
                if item.why:
                    record["why"] = item.why
            self._write(record)

    def _prune(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return
        cutoff = self._clock() - _RETENTION_S
        kept = [line for line in lines if _ts(line) >= cutoff]
        if len(kept) < len(lines):
            try:
                _rewrite(self.path, kept)
            except OSError:
                log.warning("Couldn't trim the addressee log", exc_info=True)

    def _write(self, record: dict[str, Any]) -> None:
        _append(self.path, record)


def _odds(token: dict[str, Any]) -> float | None:
    """At the "true"/"false" token: the model's odds of "true" over "false" (None for any other token)."""
    if str(token.get("token", "")).strip() not in ("true", "false"):
        return None
    probs: Counter[str] = Counter()
    for alt in token.get("top_logprobs") or ():
        key = str(alt.get("token", "")).strip()
        if key in ("true", "false"):
            with contextlib.suppress(TypeError, ValueError, OverflowError):
                probs[key] += math.exp(float(alt["logprob"]))
    total = probs["true"] + probs["false"]
    if total <= 0:
        return 1.0 if str(token["token"]).strip() == "true" else 0.0
    return probs["true"] / total


def same_request(overheard: str, request: str) -> float | None:
    """How alike two things said are as a whole (0-100), or None when they aren't the same request: close in length
    (at least 0.6 of each other's words, 2 words or more unless identical) and close word for word."""
    a, b = normalize_text(overheard).split(), normalize_text(request).split()
    if not a or not b:
        return None
    if a == b:
        return 100.0
    if min(len(a), len(b)) < 2 or min(len(a), len(b)) / max(len(a), len(b)) < 0.6:
        return None
    score = max(fuzz.ratio(" ".join(a), " ".join(b)), fuzz.token_sort_ratio(" ".join(a), " ".join(b)))
    return score if score >= _SAME else None


def without_title(why: str, title: str) -> str:
    """The model's reason with the window's title taken out: titles are never kept (a show, a document's name)."""
    parts = {title, *re.split(r"\s+[-|–—]\s+", title)} if title else set()
    for part in sorted((p.strip() for p in parts), key=len, reverse=True):
        if len(part) >= 4:
            why = re.sub(re.escape(part), "the window", why, flags=re.I)
    return why


def _name_anywhere(text: str, spellings: Sequence[Sequence[str]]) -> bool:
    words = text.split()
    return any(after_name(" ".join(words[i:]), spellings) is not None for i in range(len(words)))


def _short_id(ts: float) -> str:
    return format(int(ts * 1000), "x")[-8:]  # unique for 50 days, longer than lines are kept


def _ts(line: str) -> float:
    try:
        return float(json.loads(line).get("ts", 0))
    except (ValueError, AttributeError, TypeError):
        return 0.0


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _append(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("Couldn't write the addressee log", exc_info=True)


def _rewrite(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, path)


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


def mark(path: Path, line_id: str, for_her: bool, clock: Callable[[], float] = time.time) -> bool:
    """The user's own word on a line: it was (or wasn't) for her. False when there's no such line."""
    if not any(r.get("id") == line_id and "mark" not in r for r in read(path)):
        return False
    _append(path, {"ts": round(clock(), 3), "mark": line_id, "for_her": for_her})
    return True


def forget(path: Path) -> bool:
    """Delete everything it kept. False when there was nothing."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _context(row: dict[str, Any]) -> list[str]:
    s = row.get("situation") or {}
    out = []
    if s.get("call"):
        out.append("on a call")
    if s.get("playing"):
        out.append("sound playing")
    since = row.get("since_her_s")
    if since is not None and since <= 120:
        out.append("she'd just spoken")
    return out


def report(rows: list[dict[str, Any]], threshold: float = 0.5, show: bool = False) -> str:
    """How often she'd have answered without her name, in what situations, and how that matches what the user did."""
    marks = {r["mark"]: bool(r.get("for_her")) for r in rows if "mark" in r}
    lines_ = [r for r in rows if "mark" not in r]
    if not lines_:
        return "Nothing overheard yet (addressee.log_file)."
    judged = [r for r in lines_ if "p" in r]
    skipped = Counter(r["skipped"] for r in lines_ if "skipped" in r)
    days = max(1.0, (max(r["ts"] for r in lines_) - min(r["ts"] for r in lines_)) / 86400)
    out = [
        f"Overheard without her name: {len(lines_)} lines, {len(judged)} judged"
        + (f" (not judged: {', '.join(f'{k} {n}' for k, n in skipped.items())})" if skipped else "")
    ]
    if not judged:
        return "\n".join(out)
    answered = [r for r in judged if r["p"] >= threshold]
    out.append(
        f"She'd have answered {len(answered)} ({len(answered) / days:.1f} a day) at {threshold:.0%} sure or more"
    )
    out.append(
        "  at other settings: "
        + ", ".join(f"{t:.0%}: {sum(r['p'] >= t for r in judged)}" for t in THRESHOLDS if t != threshold)
    )
    for label in ("on a call", "sound playing", "she'd just spoken"):
        group = [r for r in judged if label in _context(r)]
        if group:
            n = sum(r["p"] >= threshold for r in group)
            out.append(f"  {label}: {n} of {len(group)} lines")
    repeated = [r for r in judged if r.get("repeated")]
    if repeated:
        caught = sum(r["p"] >= threshold for r in repeated)
        out.append(
            f"Said again with her name: {len(repeated)} times; she'd have caught {caught} and missed "
            f"{len(repeated) - caught}"
        )
    marked = [r for r in judged if r.get("id") in marks]
    if marked:
        wrong = sum(r["p"] >= threshold and not marks[r["id"]] for r in marked)
        yes_marked = sum(r["p"] >= threshold for r in marked)
        missed = sum(r["p"] < threshold and marks[r["id"]] for r in marked)
        out.append(
            f"Your marks: {len(marked)} lines; she'd have answered {yes_marked} of them, {wrong} not for her "
            f"(false triggers), and missed {missed} that were"
        )
    times = [r["decide_s"] for r in judged if r.get("decide_s") is not None]
    if times:
        out.append(f"Time to decide: median {statistics.median(times):.2f} s, slowest {max(times):.2f} s")
    if show:
        kept = [r for r in judged if "text" in r]
        if kept:
            out.append("")
            out.append("Lines she'd have answered, and ones you said again with her name (id, how sure, what):")
            for r in kept:
                verdict = {True: " [you: for her]", False: " [you: not for her]"}.get(marks.get(r["id"]), "")
                when = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
                tags = _context(r) + (["said again"] if r.get("repeated") else [])
                app = (r.get("situation") or {}).get("app")
                where = ", ".join(([app] if app else []) + tags)
                out.append(f"  {r['id']}  {when}  {r['p']:.2f}  {r['text']!r}{verdict}")
                if r.get("why") or where:
                    out.append(f"          {r.get('why', '')}" + (f" ({where})" if where else ""))
            out.append("Mark one with `octavia addressee mark ID yes` (it was for her) or `... no`.")
    return "\n".join(out)
