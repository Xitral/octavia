"""The rule fast path: quick commands run their tool directly, with no brain call.

"Set a timer for five minutes", "volume up", "next song", "what time is it" don't need a model to think about them.
A rule fires only when the whole utterance is the command (give or take "hey Octavia", "please", "can you"), so
"play something to study to and remind me in 20 minutes" still goes to the brain. Every call goes through the tool
executor as usual (its permission policy, audit log and events), and only tools that are safe on their own are
used. A command that fails isn't answered here: the brain gets it, with the failure as a note, so nothing fails
silently.

``routing.fast_path``: "on" runs the rules; "shadow" only logs which rule would have fired (``RoutingLog``); "off".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from octavia.core.text import clock, normalize_text
from octavia.tools.base import ToolResult

log = logging.getLogger(__name__)

# The tools the rules may call: each safe to run on the user's plain words (reversible, nothing sent or deleted).
FAST_TOOLS = frozenset(
    {"set_timer", "cancel_timer", "list_timers", "set_volume", "get_volume", "media_control", "switch_engines"}
)
_MAX_WORDS = 14  # anything longer is a real request
# Lines she says the same way every time: cached in her voice (``PhraseCache``), so they play instantly.
FIXED_LINES = ("Paused.", "Playing.", "Skipped.", "Going back.", "Timer cancelled.", "Muted.")

_NUMBERS = {
    word: i
    for i, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen".split()
    )
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMBER = (
    r"(?:\d+(?:\.\d+)?|a|an|one|"
    + "|".join(sorted([*_NUMBERS, *_TENS], key=len, reverse=True))
    + r")(?:[ -](?:one|two|three|four|five|six|seven|eight|nine))?"
)
_UNIT = r"(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)"
_AMOUNT = rf"(?:{_NUMBER}\s+{_UNIT}(?:\s+and\s+a\s+half)?|half\s+an?\s+(?:hour|minute)|an?\s+(?:hour|minute)\s+and\s+a\s+half)"
_DURATION = rf"(?P<duration>{_AMOUNT}(?:(?:\s+and|,)?\s+{_AMOUNT})*)"
_LABEL = r"(?:\s+(?:for|called|named)\s+(?:the\s+|my\s+)?(?P<label>[a-z][a-z' ]{0,30}?))?"

# What can wrap a command without changing it: "hey Octavia, ...", "can you ...", "... please", "... for me".
_LEAD = re.compile(
    r"^(?:(?:hey|ok|okay|yo|so|um|uh|and|please|now|quick|quickly|just)\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|i\s+(?:want|need)\s+(?:you\s+to\s+)?|"
    r"go\s+ahead\s+and\s+|let'?s\s+)?"
)
_TAIL = re.compile(r"(?:\s+(?:please|for\s+me|thanks|thank\s+you|now|real\s+quick|right\s+now))+$")


def _number(text: str) -> float:
    text = text.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if text in ("a", "an"):
        return 1.0
    total = 0
    for part in re.split(r"[ -]", text):
        total += _NUMBERS.get(part, _TENS.get(part, 0))
    return float(total)


def parse_duration(text: str) -> float | None:
    """Seconds in "5 minutes", "an hour and a half", "two hours and 30 minutes", "half an hour"; None if none."""
    text = normalize_text(text)
    if not text:
        return None
    seconds = 0.0
    found = False
    for m in re.finditer(
        rf"(?P<half>half\s+an?\s+(?P<hu>hour|minute))|(?P<n>{_NUMBER})\s+(?P<unit>{_UNIT})(?P<andhalf>\s+and\s+a\s+half)?|"
        rf"an?\s+(?P<u2>hour|minute)\s+and\s+a\s+half",
        text,
    ):
        found = True
        if m.group("half"):
            seconds += (3600 if m.group("hu") == "hour" else 60) / 2
        elif m.group("u2"):
            seconds += (3600 if m.group("u2") == "hour" else 60) * 1.5
        else:
            unit = m.group("unit")
            size = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
            seconds += _number(m.group("n")) * size + (size / 2 if m.group("andhalf") else 0)
    return seconds if found and seconds > 0 else None


@dataclass(frozen=True)
class Command:
    """A quick command: the rule that matched and the tool call it stands for (``tool`` None: answered here)."""

    rule: str
    tool: str | None
    args: dict[str, Any] = field(default_factory=dict)
    say: Callable[[ToolResult | None], str] = field(default=lambda _: "Done.", compare=False, repr=False)


def _media(action: str, said: str) -> Callable[[str], Command]:
    return lambda _: Command(f"media_{action}", "media_control", {"action": action}, lambda r: said)


def _volume_said(result: ToolResult | None) -> str:
    data = result.data if result is not None and isinstance(result.data, dict) else {}
    level = data.get("level")
    if data.get("muted") and not data.get("was_muted"):
        return "Muted."
    if data.get("was_muted") and not data.get("muted"):
        return f"Unmuted, volume's at {level} percent." if level is not None else "Unmuted."
    return f"Volume's at {level} percent." if level is not None else "Done."


def _timer(m: re.Match[str]) -> Command | None:
    seconds = parse_duration(m.group("duration"))
    if seconds is None or seconds > 24 * 3600:
        return None
    label = (m.groupdict().get("label") or "").strip()
    from octavia.tools.extensions.timers import spoken_duration

    said = f"{label.capitalize() + ' timer' if label else 'Timer'} set for {spoken_duration(seconds)}."
    return Command("set_timer", "set_timer", {"seconds": seconds, "label": label}, lambda r: said)


def _volume_to(m: re.Match[str]) -> Command | None:
    level = int(_number(m.group("level")))
    if not 0 <= level <= 100:
        return None
    return Command("volume_set", "set_volume", {"level": level}, _volume_said)


def _volume_change(change: str) -> Callable[[re.Match[str]], Command]:
    return lambda _: Command(f"volume_{change}", "set_volume", {"change": change}, _volume_said)


def _the_time(_: re.Match[str]) -> Command:
    return Command("time", None, {}, lambda r: f"It's {clock(datetime.now())}.")


def spoken_model(model: str) -> str:
    """A model id as it's said: "claude-opus-4-1-20250805" -> "Claude Opus 4.1", "claude-3-5-haiku-latest" ->
    "Claude 3.5 Haiku", "local:Qwen3.5-4B" -> "my local brain, Qwen3.5-4B"; anything else as it is."""
    if model.startswith("local:"):
        return f"my local brain, {model.removeprefix('local:')}"
    m = re.fullmatch(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?:-\d{6,}|-latest)?(?:\[[^\]]*\])?", model)
    if m:
        return f"Claude {m.group(1).capitalize()} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")
    m = re.fullmatch(r"claude-(\d+)(?:-(\d{1,2}))?-(opus|sonnet|haiku)(?:-\d{6,}|-latest)?", model)
    if m:
        return f"Claude {m.group(1)}" + (f".{m.group(2)}" if m.group(2) else "") + f" {m.group(3).capitalize()}"
    return model


def _which_model(_: re.Match[str]) -> Command:
    return Command("which_model", None, {}, lambda r: "")  # said by FastPath.match, which knows who answered


def _engines(rule: str, **args: object) -> Callable[[re.Match[str]], Command]:
    """Who answers or which voice speaks (``switch_engines``): what she says is the tool's own account of it."""
    return lambda _: Command(rule, "switch_engines", dict(args), lambda r: r.content if r is not None else "Done.")


def _cancel_timer(_: re.Match[str]) -> Command:
    return Command("cancel_timer", "cancel_timer", {}, lambda r: "Timer cancelled.")


_MUSIC = r"(?:\s+(?:the\s+)?(?:music|song|track|spotify|video|audio|playback|it|this))?"
_SONG = r"(?:\s+(?:to\s+the\s+)?(?:song|track|one))?"
_MEDIA = r"\s+(?:the\s+)?(?:music|song|track|spotify|video|audio|playback)"  # named: "resume the music"
_PICK = (
    r"(?:only\s+use|use\s+only|just\s+use|switch\s+(?:over\s+)?to|go\s+to|use)\s+"  # "only use ...", "switch to ..."
)
_NORMAL = "engines_normal"
# Her voice effects by the words for them ("use your radio voice", "robot voice", "talk like a robot").
_EFFECT_WORDS = {"radio": "radio", "console": "console", "computer": "console", "robot": "robot",
                 "robotic": "robot", "room": "room", "reverb": "room", "echo": "room", "echoey": "room",
                 "deeper": "deeper", "deep": "deeper"}  # fmt: skip
_EFFECT = "(?P<effect>" + "|".join(sorted(_EFFECT_WORDS, key=len, reverse=True)) + ")"
_HER_EFFECT = r"(?:(?:the\s+|your\s+|that\s+)?voice\s+|your\s+)(?:effects?|filters?)"


def _effect(m: re.Match[str]) -> Command:
    effect = _EFFECT_WORDS[m.group("effect")] if m.groupdict().get("effect") else "off"
    return Command(f"voice_effect_{effect}", "switch_engines", {"effect": effect},
                   lambda r: r.content if r is not None else "Done.")  # fmt: skip


RULES: tuple[tuple[str, re.Pattern[str], Callable[[re.Match[str]], Command | None]], ...] = tuple(
    (name, re.compile(pattern), build)
    for name, pattern, build in (
        # timers
        ("set_timer", rf"(?:set|start|make|put\s+on)\s+(?:a\s+|an\s+|the\s+|one\s+)?timer\s+(?:for\s+)?{_DURATION}{_LABEL}",
         _timer),
        ("set_timer", rf"(?:set|start|make|put\s+on)\s+(?:a\s+|an\s+)?{_DURATION}\s+timer{_LABEL}", _timer),
        ("set_timer", rf"timer\s+(?:for\s+)?{_DURATION}{_LABEL}", _timer),
        ("set_timer", rf"{_DURATION}\s+timer{_LABEL}", _timer),
        ("cancel_timer", r"(?:cancel|stop|kill|end|delete)\s+(?:the|my|that)\s+timer", _cancel_timer),
        # volume
        ("volume_set", rf"(?:set\s+|turn\s+|put\s+)?(?:the\s+)?volume\s+(?:to\s+|at\s+)?(?P<level>\d{1,3}|{_NUMBER})"
         r"(?:\s+percent|%)?", _volume_to),
        ("volume_up", r"(?:turn\s+(?:it|the\s+volume|the\s+music|the\s+sound)\s+up|(?:turn\s+)?(?:the\s+)?volume\s+up|"
         r"louder|make\s+it\s+louder|turn\s+up\s+the\s+volume|raise\s+the\s+volume)", _volume_change("up")),
        ("volume_down", r"(?:turn\s+(?:it|the\s+volume|the\s+music|the\s+sound)\s+down|(?:turn\s+)?(?:the\s+)?volume\s+down|"
         r"quieter|softer|make\s+it\s+quieter|turn\s+down\s+the\s+volume|lower\s+the\s+volume)", _volume_change("down")),
        ("volume_mute", r"mute(?:\s+(?:it|the\s+sound|the\s+volume|everything))?", _volume_change("mute")),
        ("volume_unmute", r"unmute(?:\s+(?:it|the\s+sound|the\s+volume|everything))?", _volume_change("unmute")),
        # playback
        ("media_pause", r"pause(?:\s+(?:the\s+)?(?:music|song|track|spotify|video|audio|playback|it|this))?|"
         r"stop\s+(?:the\s+)?(?:music|song|track|spotify|video|audio|playback)", _media("pause", "Paused.")),
        # A bare "continue", "resume" or "unpause" names no music: it may be a screen task or a question she asked, so
        # it's the brain's ("continue" once hit play while a DataCamp task waited). Only "play" means the music alone.
        ("media_play", rf"play{_MUSIC}(?:\s+again)?|(?:resume|unpause|continue){_MEDIA}(?:\s+again)?|"
         rf"keep\s+playing(?:{_MEDIA})?", _media("play", "Playing.")),
        ("media_next", rf"(?:next|skip)(?:\s+(?:this|the|that|it))?{_SONG}|(?:play\s+the\s+)?next\s+(?:song|track)",
         _media("next", "Skipped.")),
        ("media_previous", rf"(?:previous|last|go\s+back)(?:\s+a)?{_SONG}|play\s+the\s+(?:previous|last)\s+(?:song|track)|"
         r"back\s+one\s+(?:song|track)", _media("previous", "Going back.")),
        # who answered: "which model was that?", "what model just spoke to me?"
        ("which_model", r"(?:which|what)\s+(?:ai\s+|claude\s+)?model\s+(?:was\s+that|is\s+that|was\s+this|is\s+this|"
         r"(?:just\s+)?(?:spoke|talked|answered|replied|responded)(?:\s+to\s+me)?|said\s+that|are\s+you(?:\s+using)?|"
         r"did\s+(?:that|you\s+use)|answered\s+(?:that|me))|who\s+(?:just\s+)?answered\s+(?:that|me)", _which_model),
        # who answers and which voice speaks ("back to normal" only while one is picked: ``FastPath.match``)
        ("brain_claude", rf"{_PICK}(?:claude|your\s+cloud\s+brain)(?:\s+only)?|(?:claude|cloud\s+brain)\s+only",
         _engines("brain_claude", brain="claude")),
        # "Local brain" said on its own is the pick too (2026-10-26 it went to the local model, which said "ready")
        ("brain_local", rf"{_PICK}(?:your\s+|the\s+)?local\s+brain(?:\s+only)?|(?:the\s+)?local\s+brain(?:\s+(?:only|mode))?",
         _engines("brain_local", brain="local")),
        ("voice_fish", rf"{_PICK}(?:the\s+|your\s+)?fish(?:\s+audio)?(?:\s+voice)?(?:\s+only)?|"
         r"fish(?:\s+audio)?(?:\s+voice)?\s+only", _engines("voice_fish", voice="fish")),
        ("voice_local", rf"{_PICK}(?:your\s+|the\s+)?local\s+voice(?:\s+only)?|local\s+voice\s+only",
         _engines("voice_local", voice="local")),
        (_NORMAL, r"(?:go\s+|switch\s+|get\s+)?back\s+to\s+(?:normal|automatic|auto)(?:\s+mode)?",
         _engines(_NORMAL, normal=True)),
        # her voice effect: "use your radio voice", "robot voice", "talk like a robot", "turn off the voice effect"
        ("voice_effect", rf"(?:{_PICK}|(?:turn|put)\s+on\s+)(?:your\s+|the\s+|a\s+|that\s+)?{_EFFECT}\s+"
         r"(?:voice|effect|filter)(?:\s+effect)?(?:\s+(?:on|again))?", _effect),
        # said bare, only the presets' own names: "deep voice" alone may be about something else
        ("voice_effect", r"(?:the\s+|your\s+)?(?P<effect>radio|console|robot|deeper)\s+voice(?:\s+(?:on|again))?",
         _effect),
        ("voice_effect", rf"(?:talk|speak|sound)\s+like\s+(?:a\s+|an\s+)?{_EFFECT}", _effect),
        # (an effect or filter that's hers: "turn off the filter" alone may be a spreadsheet's)
        ("voice_effect", rf"(?:turn\s+off|remove|drop|stop|disable|lose|kill|get\s+rid\s+of|no\s+more)\s+{_HER_EFFECT}|"
         rf"turn\s+{_HER_EFFECT}\s+off", _effect),
        # the time
        ("time", r"what\s+time\s+is\s+it(?:\s+right\s+now)?|what'?s\s+the\s+time|what\s+is\s+the\s+time|"
         r"(?:tell\s+me\s+)?the\s+time|time\s+check", _the_time),
    )
)  # fmt: skip


# "Continue" with no music named: while a screen task is paused, it's the task the user means (she told them to say
# it), not the media; only without one is it media play.
_CARRY_ON = re.compile(
    r"(?:continue|carry\s+on|keep\s+going|go\s+on|resume|unpause|pick\s+(?:it|that)\s+(?:back\s+)?up)"
    r"(?:\s+(?:with\s+)?(?:it|that|this|the\s+task|where\s+you\s+left\s+off))?"
)


def carries_on(said: str) -> bool:
    """Whether ``said`` (a command's core, ``FastPath.core``) is a bare "continue", "carry on" or "keep going"."""
    return _CARRY_ON.fullmatch(said) is not None


def _model_line(answered_by: str | None) -> str:
    if not answered_by:
        return "I haven't answered anything yet this time."
    if answered_by.startswith("no model"):
        return "No model at all: that one was a quick command, answered by a rule."
    if answered_by.startswith("the brain"):
        return "Claude answered that, but it didn't say which model."
    return f"That was {answered_by}."


class FastPath:
    """Matches whole-utterance quick commands (``match``)."""

    def __init__(self, wake_phrase: str = "", assistant_name: str = "") -> None:
        names = {normalize_text(n) for n in (wake_phrase, assistant_name) if n and normalize_text(n)}
        self._names = sorted(names, key=len, reverse=True)
        # Who gave the last answer (the assistant sets it): a spoken model name, or a rule or preset reply.
        self.answered_by: Callable[[], str | None] = lambda: None
        # Whether a screen task is paused (the assistant sets it): then a bare "continue" is for it, not the media.
        self.work_paused: Callable[[], bool] = lambda: False
        self.phrases: Any = None  # the PhraseCache holding FIXED_LINES, when there's a voice
        # Whether her brain or voice is picked, not automatic (the app sets it): only then is "back to normal" about
        # them; otherwise it's the brain's, as before.
        self.engines_changed: Callable[[], bool] = lambda: False

    def audio(self, line: str) -> Any:
        """``line``'s cached audio (FIXED_LINES), or None: then it's synthesized as usual."""
        return self.phrases.get(line) if self.phrases is not None else None

    def core(self, text: str) -> str:
        """The utterance without what can wrap a command ("hey Octavia, ... please")."""
        said = normalize_text(text)
        for name in self._names:
            said = re.sub(rf"(?:^|\s){re.escape(name)}(?:\s|$)", " ", said)
        said = " ".join(said.split())
        said = _LEAD.sub("", said, count=1)
        return _TAIL.sub("", said).strip()

    def _who_answered(self, command: Command) -> Command:
        try:
            line = _model_line(self.answered_by())
        except Exception:
            line = _model_line(None)
        return Command(command.rule, None, {}, lambda r: line)

    def _engines_changed(self) -> bool:
        try:
            return self.engines_changed() is True
        except Exception:
            log.warning("Asking whether her brain or voice is picked failed", exc_info=True)
            return False

    def _work_paused(self) -> bool:
        try:
            return self.work_paused() is True
        except Exception:
            log.warning("Asking whether a screen task is paused failed", exc_info=True)
            return False

    def match(self, text: str) -> Command | None:
        said = self.core(text)
        if not said or len(said.split()) > _MAX_WORDS:
            return None
        if carries_on(said) and self._work_paused():
            return None
        for _name, pattern, build in RULES:
            m = pattern.fullmatch(said)
            if m is None:
                continue
            command = build(m)
            if command is not None and command.rule == "which_model":
                return self._who_answered(command)
            if command is not None and command.rule == _NORMAL and not self._engines_changed():
                return None
            if command is not None and (command.tool is None or command.tool in FAST_TOOLS):
                return command
        return None
