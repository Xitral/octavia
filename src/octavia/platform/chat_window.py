"""Her chat window's plain form, for when her own on Electron (``avatar/chat``) can't start: what was said and done
today, and a box to type to her when talking out loud won't do.

It runs as its own process (``python -m octavia.platform.chat_window NAME``), like the picture window: Tk wants the
window's whole life on one thread. The assistant (``core/chat.py``) talks to it in JSON lines:

to the window (stdin)                                 from the window (stdout)
``{"type": "line", "who": ..., "text": ..., "at": ...}``  ``{"type": "say", "text": ...}``: typed and sent
  who: "you", "her" or "did" (something she did)      ``{"type": "answer", "id": ..., "yes": ...}``: a question
``{"type": "status", "text": ...}``: what she's doing     ``{"type": "speak", "on": ...}``: say replies out loud
``{"type": "ask", "id": ..., "text": ...}``: a yes/no      ``{"type": "closed"}``
``{"type": "show"}``: come to the front
``{"type": "draft", "text": ...}``: put the start of a message in the box and come to the front

Its stdin closing (she stopped) closes it. Enter sends, Shift+Enter starts a new line, Escape closes it.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
import time
from typing import Any

MODULE = "octavia.platform.chat_window"
POLL_MS = 50

# Colours: a dark window, her lines in her blue.
BACKGROUND = "#17181c"
PANEL = "#202227"
TEXT = "#e7e8ea"
MUTED = "#8b8f98"
YOU = "#dfe8f3"
HER = "#7fc4ff"
BUTTON = "#2c6fd6"


def stamp(at: float) -> str:
    """9:41 PM"""
    return time.strftime("%I:%M %p", time.localtime(at)).lstrip("0")


def parse(line: str) -> dict[str, Any] | None:
    """One message to the window; None for anything that isn't one."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) and isinstance(message.get("type"), str) else None


class ChatWindow:
    """The window: the conversation, a status line, a question bar when she asks, and the box to type in."""

    def __init__(self, root: Any, name: str, speak: bool, send: Any) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        self.root = root
        self.name = name
        self._send = send
        self._asking: int | None = None
        self._last_day = ""
        base = tkfont.nametofont("TkDefaultFont").actual()["family"]
        self._fonts = {
            "body": (base, 11),
            "who": (base, 10, "bold"),
            "small": (base, 9),
            "did": (base, 9, "italic"),
        }
        root.title(name)
        root.configure(background=BACKGROUND)
        scale = root.winfo_fpixels("1i") / 96  # Windows' display scaling (the process is DPI aware)
        root.geometry(f"{int(460 * scale)}x{int(620 * scale)}")
        root.minsize(int(320 * scale), int(360 * scale))

        self.log = tk.Text(
            root,
            wrap="word",
            background=BACKGROUND,
            foreground=TEXT,
            borderwidth=0,
            highlightthickness=0,
            padx=14,
            pady=10,
            font=self._fonts["body"],
            cursor="arrow",
            spacing1=2,
            spacing3=2,
        )
        bar = tk.Scrollbar(root, command=self.log.yview)
        self.log.configure(yscrollcommand=bar.set, state="disabled")
        self.log.tag_configure("you", foreground=YOU, font=self._fonts["who"], spacing1=10)
        self.log.tag_configure("her", foreground=HER, font=self._fonts["who"], spacing1=10)
        self.log.tag_configure("time", foreground=MUTED, font=self._fonts["small"])
        self.log.tag_configure("did", foreground=MUTED, font=self._fonts["did"], lmargin1=10, lmargin2=10)
        self.log.tag_configure("day", foreground=MUTED, font=self._fonts["small"], justify="center", spacing1=12)
        self.log.bind("<1>", lambda _: self.log.focus_set())  # lets text be selected and copied

        self.status = tk.Label(root, text="", anchor="w", background=PANEL, foreground=MUTED, padx=12,
                               font=self._fonts["small"])  # fmt: skip

        self.question = tk.Frame(root, background=PANEL)
        self.question_text = tk.Label(self.question, text="", wraplength=int(420 * scale), justify="left", anchor="w",
                                      background=PANEL, foreground=TEXT, font=self._fonts["body"])  # fmt: skip
        self.question_text.pack(side="top", fill="x", padx=12, pady=(10, 6))
        buttons = tk.Frame(self.question, background=PANEL)
        buttons.pack(side="top", fill="x", padx=12, pady=(0, 10))
        for label, yes in (("Yes", True), ("No", False)):
            tk.Button(buttons, text=label, width=8, relief="flat", background=BUTTON if yes else "#3a3d45",
                      foreground="white", activebackground=BUTTON, command=lambda yes=yes: self.answer(yes),
                      ).pack(side="left", padx=(0, 8))  # fmt: skip

        bottom = tk.Frame(root, background=PANEL)
        self.entry = tk.Text(bottom, height=3, wrap="word", background="#2a2c32", foreground=TEXT,
                             insertbackground=TEXT, borderwidth=0, highlightthickness=0, padx=10, pady=8,
                             font=self._fonts["body"])  # fmt: skip
        side = tk.Frame(bottom, background=PANEL)
        side.pack(side="right", fill="y", padx=(0, 10), pady=10)  # before the box, so the box can't crowd it out
        self.entry.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=10)
        tk.Button(side, text="Send", width=7, relief="flat", background=BUTTON, foreground="white",
                  activebackground=BUTTON, command=self.submit).pack(side="top")  # fmt: skip
        self.speak = tk.BooleanVar(value=speak)
        tk.Checkbutton(side, text="Say it", variable=self.speak, command=self._speak_changed, background=PANEL,
                       foreground=MUTED, selectcolor=PANEL, activebackground=PANEL, activeforeground=TEXT,
                       font=self._fonts["small"]).pack(side="top", pady=(8, 0))  # fmt: skip

        bottom.pack(side="bottom", fill="x")
        self.status.pack(side="bottom", fill="x")
        bar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        self.entry.bind("<Return>", self._enter)
        root.bind("<Escape>", lambda _: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.entry.focus_set()

    # ------------------------------------------------------------ from the assistant

    def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "line":
            self.add(str(message.get("who", "")), str(message.get("text", "")), float(message.get("at") or time.time()))
        elif kind == "status":
            self.status.configure(text=str(message.get("text", "")))
        elif kind == "ask":
            self.ask(int(message.get("id", 0)), str(message.get("text", "")))
        elif kind == "unask":
            if self._asking == message.get("id"):
                self._unask()
        elif kind == "show":
            self.show()
        elif kind == "draft":
            self.draft(str(message.get("text", "")))

    def add(self, who: str, text: str, at: float) -> None:
        if not text.strip():
            return
        at_bottom = self.log.yview()[1] > 0.98
        self.log.configure(state="normal")
        day = time.strftime("%A, %B %d", time.localtime(at)).replace(" 0", " ")
        if day != self._last_day:
            self.log.insert("end", f"{day}\n", "day")
            self._last_day = day
        if who == "did":
            self.log.insert("end", f"{text}\n", "did")
        else:
            self.log.insert("end", "You" if who == "you" else self.name, who if who in ("you", "her") else "her")
            self.log.insert("end", f"  {stamp(at)}\n", "time")
            self.log.insert("end", f"{text.strip()}\n")
        self.log.configure(state="disabled")
        if at_bottom:
            self.log.see("end")

    def draft(self, text: str) -> None:
        """The start of a message, put in the box for the user to finish, in place of whatever was there: two drafts
        run together would carry only the second's file (``ChatLink._from_draft`` matches the start)."""
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", text)
        self.entry.mark_set("insert", "end-1c")
        self.show()

    def ask(self, ask_id: int, text: str) -> None:
        self._asking = ask_id
        self.question_text.configure(text=text)
        self.question.pack(side="bottom", fill="x", before=self.status)
        self.show()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)  # in front of the window the user is in, then an ordinary window
        self.root.after(400, lambda: self.root.attributes("-topmost", False))
        self.entry.focus_force()

    # ------------------------------------------------------------ from the user

    def _enter(self, event: Any) -> str | None:
        if event.state & 0x0001:  # Shift: a new line
            return None
        self.submit()
        return "break"

    def submit(self) -> None:
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        self.entry.delete("1.0", "end")
        self._send({"type": "say", "text": text})

    def answer(self, yes: bool) -> None:
        if self._asking is None:
            return
        self._send({"type": "answer", "id": self._asking, "yes": yes})
        self._unask()

    def _unask(self) -> None:
        self._asking = None
        self.question.pack_forget()

    def _speak_changed(self) -> None:
        self._send({"type": "speak", "on": bool(self.speak.get())})

    def close(self) -> None:
        self._send({"type": "closed"})  # a question up stays unanswered (``NotAnswered``): closing it is no "no"
        self.root.destroy()


def _read(lines: queue.Queue[str | None]) -> None:
    for line in sys.stdin:
        lines.put(line)
    lines.put(None)  # EOF: she stopped


def main(argv: list[str]) -> int:
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    import tkinter as tk

    name = argv[0] if argv else "Octavia"
    speak = argv[1:2] == ["speak"]
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    lock = threading.Lock()

    def send(message: dict[str, Any]) -> None:
        with lock, contextlib.suppress(OSError, ValueError):
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    root = tk.Tk()
    window = ChatWindow(root, name, speak, send)
    lines: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_read, args=(lines,), name="chat-window-stdin", daemon=True).start()

    def pump() -> None:
        try:
            while True:
                line = lines.get_nowait()
                if line is None:
                    root.destroy()
                    return
                message = parse(line)
                if message is not None:
                    window.handle(message)
        except queue.Empty:
            pass
        root.after(POLL_MS, pump)

    root.after(POLL_MS, pump)
    window.show()
    send({"type": "ready"})
    root.mainloop()
    sys.stdout.flush()
    os._exit(0)  # not a normal exit: the stdin thread is still blocked reading, which can abort interpreter shutdown


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
