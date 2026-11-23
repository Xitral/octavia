"""A small window on the user's screen asking for an account's email and password, so a password is typed, never said
aloud or seen by the language model.

It runs as its own process (``python -m octavia.platform.sign_in_window SERVICE``), as the picture window does: Tk wants
the window's whole life on one thread. It prints one JSON line, ``{"email": ..., "password": ...}``, when the user
presses Connect, and nothing when they cancel or close it. The caller reads that line from a pipe and never logs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from typing import Any

log = logging.getLogger(__name__)

MODULE = "octavia.platform.sign_in_window"
WAIT_S = 300.0  # the window closes itself if the user leaves it this long
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def main(argv: list[str]) -> int:
    import tkinter as tk

    service = argv[0] if argv else "your account"
    root = tk.Tk()
    root.title(f"Connect {service}")
    root.resizable(False, False)
    frame = tk.Frame(root, padx=16, pady=14)
    frame.pack()
    tk.Label(frame, text=f"Sign in to {service}. Your password goes only to {service} and isn't kept.").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
    )
    tk.Label(frame, text="Email").grid(row=1, column=0, sticky="w")
    email = tk.Entry(frame, width=36)
    email.grid(row=1, column=1, pady=3)
    tk.Label(frame, text="Password").grid(row=2, column=0, sticky="w")
    password = tk.Entry(frame, width=36, show="•")
    password.grid(row=2, column=1, pady=3)

    def connect(_: Any = None) -> None:
        if email.get().strip() and password.get():
            print(json.dumps({"email": email.get().strip(), "password": password.get()}), flush=True)
            root.destroy()

    buttons = tk.Frame(frame)
    buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
    tk.Button(buttons, text="Cancel", width=10, command=root.destroy).pack(side="right", padx=(6, 0))
    tk.Button(buttons, text="Connect", width=10, command=connect).pack(side="right")
    root.bind("<Return>", connect)
    root.bind("<Escape>", lambda _: root.destroy())
    root.after(int(WAIT_S * 1000), root.destroy)
    root.attributes("-topmost", True)
    root.focus_force()
    email.focus_set()
    root.mainloop()
    return 0


async def ask_sign_in(service: str, wait_s: float = WAIT_S + 10) -> tuple[str, str] | None:
    """Show the window and wait: (email, password), or None when the user cancelled or it couldn't open."""
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            MODULE,
            service,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        log.warning("couldn't open the sign-in window: %s", exc)
        return None
    try:
        line = await asyncio.wait_for(process.stdout.readline(), wait_s) if process.stdout else b""
    except TimeoutError:
        line = b""
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
        await process.wait()
    try:
        given = json.loads(line)
        return str(given["email"]), str(given["password"])
    except (ValueError, KeyError, TypeError):
        return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
