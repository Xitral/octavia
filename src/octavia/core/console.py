"""Terminal status display driven entirely by events.

Prints concise lines like::

    [COMPUTER] Starting...
    [WAKE] Listening for "hey computer"
    [STT] "What's the weather today?"

A GUI/HUD can subscribe to the same ``EventBus`` instead of (or alongside) this.
"""

from __future__ import annotations

import logging
import os
import sys
import textwrap
from typing import TextIO

from octavia.core.events import (
    BrainFinished,
    BrainStarted,
    ErrorOccurred,
    Event,
    EventBus,
    FastChatFinished,
    Interrupted,
    ListeningStarted,
    NoSpeech,
    Notice,
    PermissionRequested,
    PermissionResolved,
    PresetReplied,
    SessionEnded,
    SpeakingStarted,
    ToolCallFinished,
    ToolCallStarted,
    Transcript,
    WakeDetected,
    WakeListening,
)

_COLORS = {
    "APP": "96",  # bright cyan
    "WAKE": "35",
    "VOICE": "36",
    "STT": "33",
    "BRAIN": "34",
    "TOOL": "32",
    "TTS": "95",
    "CONFIRM": "93",
    "MEM": "90",
    "EVOLVE": "92",  # bright green: self-improvement
    "FUTURES": "94",  # bright blue: Octavia Futures
    "ERROR": "91",
    "WARN": "93",
}


def _enable_windows_ansi(stream: TextIO) -> bool:
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return True
    except Exception:
        return False


class StatusConsole:
    def __init__(
        self,
        assistant_name: str,
        verbosity: str = "normal",
        stream: TextIO | None = None,
        show_replies: bool = True,
    ) -> None:
        self.app_tag = assistant_name.upper()
        self.verbosity = verbosity
        self.stream = stream or sys.stdout
        self.show_replies = show_replies
        self.color = _enable_windows_ansi(self.stream)

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(self.handle)

    def line(self, tag: str, message: str) -> None:
        if self.verbosity == "quiet" and tag not in {"ERROR", "WARN", self.app_tag}:
            return
        color_key = "APP" if tag == self.app_tag else tag
        label = f"[{tag}]"
        if self.color and color_key in _COLORS:
            label = f"\x1b[{_COLORS[color_key]}m{label}\x1b[0m"
        try:
            self.stream.write(f"{label} {message}\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass  # no console attached (pythonw) - the log file still has everything

    def handle(self, event: Event) -> None:
        match event:
            case Notice(source=source, message=message, level=level):
                if level >= logging.WARNING:
                    self.line("WARN", message)
                elif level >= logging.INFO or self.verbosity == "verbose":
                    self.line(self.app_tag if source == "APP" else source, message)
            case WakeListening(phrase=phrase):
                self.line("WAKE", f'Listening for "{phrase}"')
            case WakeDetected(second_chance=True):
                self.line("WAKE", "Detected (on a second listen)")
            case WakeDetected(during_speech=during):
                self.line("WAKE", "Detected (interrupting)" if during else "Detected")
            case ListeningStarted(follow_up=follow_up):
                self.line("VOICE", "Listening for a follow-up..." if follow_up else "Listening...")
            case NoSpeech(follow_up=follow_up):
                if not follow_up or self.verbosity == "verbose":
                    self.line("VOICE", "Didn't hear anything")
            case Transcript(text=text, latency_s=latency):
                extra = f"  ({latency:.2f}s)" if self.verbosity == "verbose" else ""
                self.line("STT", f'"{text}"{extra}')
            case BrainStarted():
                self.line("BRAIN", "Thinking...")
            case ToolCallStarted(name=name, args=args):
                rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
                self.line("TOOL", f"{name}({rendered})")
            case ToolCallFinished(name=name, ok=ok, summary=summary, duration_s=duration):
                if not ok:
                    self.line("TOOL", f"{name} failed: {summary}")
                elif self.verbosity == "verbose":
                    self.line("TOOL", f"{name} ok ({duration:.2f}s): {summary}")
            case PermissionRequested(description=description, detail=detail):
                self.line("CONFIRM", f"Needs approval: {description}" + (f" ({detail})" if detail else ""))
            case PermissionResolved(approved=approved, reason=reason):
                if approved:
                    # "approved" is the plain yes; anything else (the user asked for it) is worth showing.
                    self.line("CONFIRM", "Approved" if reason == "approved" else f"Approved ({reason})")
                else:
                    self.line("CONFIRM", f"Not approved ({reason})")
            case SpeakingStarted():
                self.line("TTS", "Speaking...")
            case Interrupted(reason=reason):
                self.line("VOICE", f"Interrupted ({reason})")
            case BrainFinished(text=text, error=error):
                if error:
                    self.line("ERROR", f"Brain: {error}")
                elif text and self.show_replies:
                    wrapped = textwrap.fill(" ".join(text.split()), width=100, subsequent_indent="    ")
                    self.line(self.app_tag, wrapped)
            case PresetReplied(text=text):
                if self.show_replies:
                    self.line(self.app_tag, f"{text}  (preset reply, brain not used)")
            case FastChatFinished(said=said, passed=None) if said and self.show_replies:
                wrapped = textwrap.fill(" ".join(said.split()), width=100, subsequent_indent="    ")
                self.line(self.app_tag, f"{wrapped}  (quick voice)")
            case FastChatFinished(passed=passed) if passed and self.verbosity == "verbose":
                self.line("BRAIN", f"Quick voice passed ({passed}): thinking...")
            case SessionEnded(reason=reason):
                if self.verbosity == "verbose":
                    self.line(self.app_tag, f"Conversation session ended ({reason})")
            case ErrorOccurred(source=source, message=message, fatal=fatal):
                self.line("ERROR", f"{source}: {message}" + (" (fatal)" if fatal else ""))
            case _:
                pass
