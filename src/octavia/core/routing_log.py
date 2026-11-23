"""One line per turn in ``data/routing_log.jsonl``: how it was routed, how fast it was heard, what it used.

Phase 1 of the multi-model plan measures before it changes anything: how many turns a rule answered (or could
have), how long each route takes from the end of your speech to the first sound, and what the brain's turns used.
A brain turn whose only tools were quick-command ones (``FAST_TOOLS``) is marked ``could_skip``: a rule could have
answered it. ``octavia routing`` summarizes the file (``report``).

Each line: ``ts``, ``trace`` (an id for the turn), ``source`` (voice, text, discord, scheduled), ``text`` (said),
``route`` (rule, shadow, preset, fast_chat, brain, dismissed), ``rule``, ``model``, ``tools``, ``ok``, ``first_audio_s`` (end
of speech, or the typed turn, to the first sound: an acknowledgment counts), ``reply_audio_s`` (to the reply
itself), ``duration_s``, ``first_word_s``, ``head_start_s`` (a brain turn started in the user's pause: how long
before the turn it began), ``usage`` (the brain's own token and cost figures), ``error``,
``interrupted``, ``could_skip``, ``tone`` (how the user sounded, from their voice: ``{"tone", "confidence", "cues"}``,
or null when it wasn't read in time or is off; ``octavia.emotion``). A brain turn that her quick voice passed on
(``brain/fast_chat.py``) also has ``fast_chat`` (why: handoff, timeout, error, empty, down) and ``fast_chat_s`` (how
long that took, which is included in its timings). A brain turn that looked up tools it hadn't loaded (Claude Code's
tool search, one more model round trip) has ``loaded``: the tools it named, or the words it searched for.

Written by the event loop only (events arrive there), appending a line at a time, so turns never interleave.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from octavia.core.events import (
    BrainFinished,
    BrainStarted,
    BrainTextDelta,
    Dismissed,
    Event,
    EventBus,
    FastChatFinished,
    FastChatStarted,
    FastPathHandled,
    Interrupted,
    OpenerChecked,
    PhrasePlaying,
    PresetReplied,
    SpeakingFinished,
    SpeakingStarted,
    SpeechEnded,
    ToneDetected,
    ToolCallFinished,
    ToolCallStarted,
    ToolsLoaded,
    Transcript,
)
from octavia.core.fast_path import FAST_TOOLS

log = logging.getLogger(__name__)

_FLUSH_S = 8.0  # a turn with no reply audio (no voice, typed chat) is written this long after it was routed
_SKIPPABLE = FAST_TOOLS | {"get_current_time"}
LOOKUP_STEP = "tool lookup"  # a lookup's step in ``steps``: the model's time to it, then the next step's from it


class RoutingLog:
    def __init__(
        self, events: EventBus, path: Path, model: str = "", clock: Any = time.monotonic, end_silence_s: float = 0.0
    ) -> None:
        self.path = path
        self.model = model
        self._clock = clock
        self._spoke_at: float | None = None  # when the user's last utterance ended
        # The silence the endpointer waits for before it calls the utterance over: it comes on top of every timing
        # here, which starts once that silence has passed.
        self.end_silence_s = end_silence_s
        self._heard_s: float | None = None  # an acknowledgment ("On it!") played this long after the speech ended
        self._stt_s: float | None = None  # how long the speech took to transcribe
        self._ending: dict[str, Any] = {}  # how the last utterance ended (``SpeechEnded``): its silence, early or not
        self._open: dict[str, Any] | None = None  # the turn being recorded
        self._started = 0.0  # when it began (end of speech, or the typed turn)
        self._flush_later: asyncio.TimerHandle | None = None
        self._shadow: str | None = None  # a rule that would have answered the brain turn about to start
        self._tone: dict[str, Any] | None = None  # how the user sounded, for the turn about to start
        self._passed: dict[str, Any] = {}  # the quick voice passed the turn about to start on to the brain
        # Her opener's verdict on a request (``OpenerChecked``) that came in before its brain turn's record opened.
        self._opener: tuple[str, dict[str, Any]] | None = None
        # Where a brain turn's time goes, step by step (``steps`` in the record): before each tool the model's own
        # time since its last step (``think_s``), the tool's (``run_s``) and a picture's size (``image_kb``); then
        # from the last tool's end to the reply's next words (``reply_after_tools_s``). A screen look took 9.4 s to
        # the first word and nobody knew which part (2026-10-25 review).
        self._steps: list[dict[str, Any]] = []
        self._step_of: dict[str, dict[str, Any]] = {}
        self._mark = 0.0  # the brain's last step: the turn starting, or a tool ending
        self._after_tools = False  # a tool ended and no words have come since
        events.subscribe(
            self._on_event,
            SpeechEnded,
            Transcript,
            BrainStarted,
            BrainFinished,
            BrainTextDelta,
            ToolCallStarted,
            ToolCallFinished,
            ToolsLoaded,
            PresetReplied,
            FastPathHandled,
            FastChatStarted,
            FastChatFinished,
            Dismissed,
            SpeakingStarted,
            PhrasePlaying,
            ToneDetected,
            OpenerChecked,
            SpeakingFinished,
            Interrupted,
        )

    # ------------------------------------------------------------------ events
    def _on_event(self, event: Event) -> None:
        try:
            self._handle(event)
        except Exception:
            log.warning("Routing log failed", exc_info=True)

    def _handle(self, event: Event) -> None:
        now = self._clock()
        match event:
            case Transcript(latency_s=latency):
                self._stt_s = latency
            case ToneDetected(tone=tone, confidence=confidence, cues=cues):
                self._tone = {"tone": tone, "confidence": confidence, "cues": list(cues)}
            case SpeechEnded():
                self._heard_s = self._stt_s = None
                self._tone = None
                self._passed = {}
                self.flush()
                self._spoke_at = now
                self._ending = {
                    k: v
                    for k, v in (
                        ("end_silence_s", event.silence_s),
                        ("sounded_finished", event.sounded_finished or None),
                        ("continued", event.continued or None),
                        ("turn_p", event.turn_end_p),
                        ("turn_pauses", [list(p) for p in event.turn_pauses] or None),
                    )
                    if v is not None
                }
                if event.acknowledged_after_s is not None and event.silence_s is not None:
                    # "On it!" played in the pause, before the speech was called over: before this clock starts.
                    self._heard_s = round(event.acknowledged_after_s - event.silence_s, 3)
            case FastPathHandled(shadow=True, rule=rule):
                self._shadow = rule  # the brain turn right after it is the one that answered
            case FastPathHandled(text=text, rule=rule, tool=tool, ok=ok, duration_s=duration):
                self._begin("voice" if self._spoke_at is not None else "text", text, "rule", rule=rule)
                assert self._open is not None
                self._open.update(tools=[tool] if tool else [], ok=ok, duration_s=round(duration, 3))
                if not ok:  # the brain takes it over: one line, the brain's, says how it went
                    self._open["route"] = "rule_failed"
                    self.flush()
                else:
                    self._flush_soon()
            case PresetReplied(text=text, rule=rule):
                self._begin("voice" if self._spoke_at is not None else "text", text, "preset", rule=rule)
                self._flush_soon()
            case Dismissed(text=text):
                self._begin("voice", text, "dismissed")
                self.flush()
            case FastChatStarted(text=text, source=source, first_word_s=first_word):
                self._passed = {}
                self._begin(source, text, "fast_chat", first_word_s=_round(first_word))
            case FastChatFinished(passed=None) as done:
                record = self._open
                if record is None or record["route"] != "fast_chat":
                    return
                record.update(
                    model=done.model,
                    interrupted=done.interrupted,
                    duration_s=_round(done.duration_s),
                    first_word_s=_round(done.first_word_s),
                    usage=_plain(done.usage),
                )
                self.flush()
            case FastChatFinished(passed=passed, duration_s=duration) if passed != "interrupted":
                self._passed = {"fast_chat": passed, "fast_chat_s": _round(duration)}  # for the brain turn next
            case BrainStarted(text=text, source=source):
                shadow, self._shadow = self._shadow, None
                passed, self._passed = self._passed, {}
                route = "shadow" if shadow else "brain"
                opener, self._opener = self._opener, None
                if opener is not None and opener[0] == text:
                    passed = {**passed, **opener[1]}
                self._begin(source, text, route, rule=shadow, model=self.model, **passed)
                self._steps, self._step_of, self._mark, self._after_tools = [], {}, now, False
            case OpenerChecked() as checked:
                fields = {
                    "opener": checked.said,
                    "opener_reason": checked.reason,
                    "opener_s": _round(checked.ready_s),
                    "opener_spoken": checked.spoken or None,
                    "opener_wrote": checked.wrote if not checked.said else None,
                }
                fields = {k: v for k, v in fields.items() if v is not None}
                record = self._open
                if record is not None and record["route"] in ("brain", "shadow") and record["text"] == checked.text:
                    record.update(fields)
                else:
                    self._opener = (checked.text, fields)
            case SpeakingFinished(underruns=underruns, underrun_s=underrun_s, stalled_s=stalled_s) if (
                self._open is not None
            ):
                self._open["underruns"] = self._open.get("underruns", 0) + underruns
                if underrun_s:
                    self._open["underrun_s"] = round(self._open.get("underrun_s", 0.0) + underrun_s, 3)
                if stalled_s:
                    self._open["stalled_s"] = round(self._open.get("stalled_s", 0.0) + stalled_s, 3)
            case Interrupted(after_s=after_s) if self._open is not None and after_s is not None:
                self._open.setdefault("barge_s", after_s)
            case ToolsLoaded() as lookup if self._open is not None and self._open["route"] in ("brain", "shadow"):
                # One made in the user's pause (``early``) is told when the turn takes it up: no model time here.
                self._lookup(lookup, 0.0 if lookup.early else round(now - self._mark, 3))
                self._mark, self._after_tools = now, True  # what comes next is timed from it
            case ToolCallStarted(name=name, call_id=call_id):
                if self._open is not None and self._open["route"] in ("brain", "shadow"):
                    self._open["tools"].append(name)
                    step = {"tool": name, "think_s": round(now - self._mark, 3)}
                    self._steps.append(step)
                    self._step_of[call_id] = step
                    self._after_tools = False
            case ToolCallFinished(call_id=call_id, duration_s=duration, data=data):
                step = self._step_of.pop(call_id, None)
                if step is not None:
                    step["run_s"] = round(duration, 3)
                    if (kb := _image_kb(data)) is not None:
                        step["image_kb"] = kb
                    self._mark, self._after_tools = now, True
            case BrainTextDelta() if self._after_tools and self._open is not None:
                self._after_tools = False
                self._open["reply_after_tools_s"] = round(now - self._mark, 3)
            case PhrasePlaying() if self._open is None and self._spoke_at is not None:
                # The acknowledgment plays the moment the speech is transcribed, before the turn's record opens.
                self._heard_s = round(now - self._spoke_at, 3)
            case PhrasePlaying() | SpeakingStarted():
                if self._open is not None:
                    if self._open.get("first_audio_s") is None:
                        self._open["first_audio_s"] = round(now - self._started, 3)
                    if isinstance(event, SpeakingStarted) and self._open.get("reply_audio_s") is None:
                        self._open["reply_audio_s"] = round(now - self._started, 3)
                        if self._open["route"] in ("rule", "preset"):
                            self.flush()
            case BrainFinished(
                error=error,
                interrupted=interrupted,
                duration_s=duration,
                first_word_s=first_word,
                usage=usage,
                model=model,
                head_start_s=head_start,
                handoff=handoff,
            ):
                record = self._open
                if record is None or record["route"] not in ("brain", "shadow"):
                    return
                tools = record["tools"]
                record.update(
                    ok=error is None,
                    error=error,
                    interrupted=interrupted,
                    duration_s=round(duration, 3) if duration is not None else None,
                    first_word_s=round(first_word, 3) if first_word is not None else None,
                    usage=_plain(usage),
                    model=model or record.get("model"),
                    could_skip=bool(tools) and len(tools) <= 2 and set(tools) <= _SKIPPABLE,
                )
                if head_start is not None:
                    record["head_start_s"] = round(head_start, 3)  # the brain began in the user's pause
                if handoff:  # her opening words began the reply: the brain's own first words were heard later
                    record["handoff_gap_s"] = handoff.get("gap_s")
                    if handoff.get("dropped"):
                        record["handoff_dropped"] = handoff["dropped"]
                    if handoff.get("corrected"):
                        record["handoff_corrected"] = True
                    if record.get("reply_audio_s") is not None:
                        record["opener_audio_s"] = record["reply_audio_s"]  # said once the silence was over
                    ago = handoff.get("answer_ago_s")
                    record["reply_audio_s"] = round(now - ago - self._started, 3) if ago is not None else None
                if self._steps:
                    record["steps"] = self._steps
                    log.info("Timing: steps %s", _spoken_steps(self._steps, record.get("reply_after_tools_s")))
                self._steps, self._step_of = [], {}
                self.flush()

    def _lookup(self, lookup: ToolsLoaded, think_s: float) -> None:
        assert self._open is not None
        # A keyword search keeps its words, marked so the report never takes them for a tool's name.
        self._open.setdefault("loaded", []).extend(lookup.names or [f"words: {lookup.query}"])
        self._steps.append({"tool": LOOKUP_STEP, "think_s": think_s, "run_s": 0.0})

    def _begin(self, source: str, text: str, route: str, **fields: Any) -> None:
        self.flush()
        now = self._clock()
        tone, self._tone = self._tone, None
        voice = source == "voice" and self._spoke_at is not None
        self._started = self._spoke_at if voice and self._spoke_at is not None else now
        self._spoke_at = None
        heard, stt = (self._heard_s, self._stt_s) if voice else (None, None)
        self._heard_s = self._stt_s = None
        ending, self._ending = self._ending, {}
        if voice:
            for key, value in ending.items():
                fields.setdefault(key, value)
            fields.setdefault("end_silence_s", self.end_silence_s)
            if stt is not None:
                fields.setdefault("stt_s", round(stt, 3))
            if heard is not None:
                fields.setdefault("first_audio_s", heard)
        self._open = {
            "ts": round(time.time(), 3),
            "trace": uuid.uuid4().hex[:12],
            "source": source,
            "text": text,
            "route": route,
            "rule": None,
            "model": None,
            "tools": [],
            "ok": True,
            "tone": tone,
            **fields,
        }

    def _flush_soon(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        if self._flush_later is not None:
            self._flush_later.cancel()
        self._flush_later = loop.call_later(_FLUSH_S, self.flush)

    def flush(self) -> None:
        """Write the turn being recorded, if any."""
        if self._flush_later is not None:
            self._flush_later.cancel()
            self._flush_later = None
        record, self._open = self._open, None
        if record is None:
            return
        silence, first = record.get("end_silence_s"), record.get("first_audio_s")
        if silence is not None and first is not None:
            record["from_last_word_s"] = round(float(silence) + float(first), 3)  # the wait the user actually had
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.warning("Couldn't write the routing log %s", self.path, exc_info=True)

    def close(self) -> None:
        self.flush()


def _image_kb(data: Any) -> int | None:
    """The size of a tool result's picture (``data["image"]``, base64), in KB, or None."""
    image = data.get("image") if isinstance(data, dict) else None
    encoded = image.get("data") if isinstance(image, dict) else None
    return round(len(encoded) * 3 / 4 / 1024) if isinstance(encoded, str) else None


def _spoken_steps(steps: list[dict[str, Any]], reply_after: float | None) -> str:
    """E.g. "model 1.20s, look_at_screen 0.41s (310 KB), model 6.05s to words"."""
    parts = []
    for step in steps:
        size = f" ({step['image_kb']} KB)" if "image_kb" in step else ""
        ran = "" if step["tool"] == LOOKUP_STEP else f" {step.get('run_s', 0):.2f}s"
        parts += [f"model {step['think_s']:.2f}s", f"{step['tool']}{ran}{size}"]
    if reply_after is not None:
        parts.append(f"model {reply_after:.2f}s to words")
    return ", ".join(parts)


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _plain(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------- the report


def read(path: Path, since: float = 0.0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and float(row.get("ts") or 0) >= since:
            rows.append(row)
    return rows


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _smart_turn_lines(user: list[dict[str, Any]]) -> list[str]:
    """What Smart Turn's shadow verdicts (``octavia.vad.smart_turn``) would have done if they ended your turns: at each
    confidence, the turns it would have ended in your last pause (0.2 s in instead of the full silence) and the turns
    it would have cut short, at a pause you went on talking after."""
    judged = [r for r in user if r.get("turn_p") is not None]
    if not judged:
        return []
    lines = [f"  Smart Turn (shadow, {len(judged)} turns): if it had ended your turns when at least this sure"]
    for threshold in (0.5, 0.7, 0.9):
        cut = sum(1 for r in judged if any(p >= threshold for p, _ in r.get("turn_pauses") or ()))
        early = sum(
            1
            for r in judged
            if r["turn_p"] >= threshold and not any(p >= threshold for p, _ in r.get("turn_pauses") or ())
        )
        lines.append(
            f"    {threshold:.1f}: ended in time {early} ({100 * early / len(judged):.0f}%), "
            f"cut you off {cut} ({100 * cut / len(judged):.0f}%)"
        )
    return lines


def _lookup_lines(brain: list[dict[str, Any]], top: int) -> list[str]:
    """How often the brain had to look a tool up first (one more model round trip each) and which tools: the ones
    looked up most are the candidates for ``brain.claude.always_loaded_tools``."""
    looked = [r for r in brain if r.get("loaded")]
    if not looked:
        return []
    names = Counter(name for r in looked for name in dict.fromkeys(r["loaded"]) if not name.startswith("words: "))
    words = [float(r["first_word_s"]) for r in looked if r.get("first_word_s") is not None]
    return [
        f"Brain turns that looked a tool up first: {len(looked)} ({100 * len(looked) / len(brain):.1f}%)"
        + (f", first word {statistics.median(words):.2f}s (median)" if words else "")
        + ". Looked up most: "
        + ", ".join(f"{name} {n}" for name, n in names.most_common(top))
    ]


def report(rows: list[dict[str, Any]], top: int = 10) -> str:
    """How the turns were routed and how fast each route was, as plain text."""
    user = [r for r in rows if r.get("source") in ("voice", "text", "discord", "window")]
    if not user:
        return "No turns logged yet."
    lines = [f"{len(user)} turns of yours ({sum(r.get('source') == 'voice' for r in user)} spoken)."]
    routes = Counter(r.get("route") for r in user)
    for route, n in routes.most_common():
        lines.append(f"  {route:<12} {n:>5}  {100 * n / len(user):5.1f}%")
    brain = [r for r in user if r.get("route") in ("brain", "shadow")]
    skippable = [r for r in brain if r.get("could_skip")]
    no_model = routes["rule"] + routes["preset"] + routes["dismissed"]
    lines.append(
        f"No model call: {no_model} ({100 * no_model / len(user):.1f}%). Brain turns a rule could have answered: "
        f"{len(skippable)} more ({100 * len(skippable) / len(user):.1f}%)."
    )
    waits = [float(r["end_silence_s"]) for r in user if r.get("end_silence_s")]
    wait = f" (add {statistics.median(waits):.1f}s: the silence that tells her you've finished)" if waits else ""
    lines.append(f"Time to first sound / to the reply itself, p50 and p95 (spoken turns){wait}:")
    waited = [float(r["from_last_word_s"]) for r in user if r.get("from_last_word_s") is not None]
    if waited:
        lines.append(
            f"  first sound from your last word: {_pct(waited, 0.5):.2f}s / {_pct(waited, 0.95):.2f}s  ({len(waited)} turns)"
        )
    finished = sum(1 for r in user if r.get("sounded_finished"))
    went_on = sum(1 for r in user if r.get("continued"))
    if finished or went_on:
        # ``continued``: they went on talking after a pause that sounded finished (``octavia.stt.early``): what ending
        # the utterance on such a pause would have cut off.
        lines.append(
            f"  sounded finished before the silence was up: {finished}, you went on talking after one: {went_on}"
        )
    lines += _smart_turn_lines(user)
    timings: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for r in user:
        if r.get("source") != "voice":
            continue
        first, reply = timings[r.get("route") or "?"]
        if r.get("first_audio_s") is not None:
            first.append(float(r["first_audio_s"]))
        if r.get("reply_audio_s") is not None:
            reply.append(float(r["reply_audio_s"]))
    for route, (first, reply) in sorted(timings.items()):
        parts = []
        if first:
            parts.append(f"first sound {_pct(first, 0.5):.2f}s / {_pct(first, 0.95):.2f}s")
        if reply:
            parts.append(f"reply {_pct(reply, 0.5):.2f}s / {_pct(reply, 0.95):.2f}s")
        if parts:
            lines.append(f"  {route:<12} " + ", ".join(parts) + f"  ({max(len(first), len(reply))} turns)")
    stt = [float(r["stt_s"]) for r in user if r.get("stt_s") is not None]
    words = [float(r["first_word_s"]) for r in brain if r.get("first_word_s") is not None]
    if stt or words:
        lines.append(
            "Of that: speech recognition "
            + (f"{statistics.median(stt):.2f}s" if stt else "?")
            + ", the brain's first word "
            + (f"{statistics.median(words):.2f}s" if words else "?")
            + " (medians)."
        )
    lines += _lookup_lines(brain, top)
    durations = [float(r["duration_s"]) for r in brain if r.get("duration_s") is not None]
    if durations:
        lines.append(f"Brain turns took {statistics.median(durations):.1f}s (median) start to finish.")
    chats = [r for r in user if r.get("route") == "fast_chat"]
    passed = Counter(r.get("fast_chat") for r in brain if r.get("fast_chat"))
    if chats or passed:
        quick = [float(r["first_word_s"]) for r in chats if r.get("first_word_s") is not None]
        lines.append(
            f"Quick voice (small talk): answered {len(chats)}"
            + (f", first word {statistics.median(quick):.2f}s (median)" if quick else "")
            + (
                f"; passed to the brain {sum(passed.values())} ("
                + ", ".join(f"{k} {n}" for k, n in passed.most_common())
                + ")"
                if passed
                else ""
            )
            + "."
        )
    rules = Counter(r.get("rule") for r in user if r.get("route") in ("rule", "shadow") and r.get("rule"))
    if rules:
        lines.append("Rules used: " + ", ".join(f"{rule} {n}" for rule, n in rules.most_common()))
    if skippable:
        lines.append("Brain turns a rule could have answered (candidates for new rules):")
        for r in skippable[-top:]:
            lines.append(f'  "{r.get("text", "")}" -> {", ".join(r.get("tools") or [])}')
    failed = [r for r in user if r.get("route") == "rule_failed"]
    if failed:
        lines.append(f"Rules whose tool failed (the brain took over): {len(failed)}")
    return "\n".join(lines)
