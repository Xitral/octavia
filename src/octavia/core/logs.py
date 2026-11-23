"""Logging setup.

The console shows concise status lines (``StatusConsole``); the detailed log
goes to a rotating file. Under ``pythonw.exe`` (background/autostart) there is
no console: stdout/stderr are ``None``, so they're redirected to a file before
any library tries to write to them, and hard crashes are captured with
faulthandler.
"""

from __future__ import annotations

import atexit
import contextlib
import faulthandler
import logging
import os
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

_crash_file: TextIO | None = None
_crash_log: Path | None = None

_NOISY = ("websockets", "httpx", "httpcore", "asyncio", "mcp", "claude_agent_sdk", "comtypes", "urllib3", "livekit")


def _drop_if_empty(crash_log: Path) -> None:
    """At a normal exit: a crash log nothing was written to isn't kept (it's opened up front, since a hard crash can't
    open a file, so otherwise every command leaves an empty one behind)."""
    global _crash_file
    try:
        faulthandler.disable()
        if _crash_file is not None:
            _crash_file.close()
            _crash_file = None
        if crash_log.stat().st_size == 0:
            crash_log.unlink()
    except OSError:
        pass


def exit_now(code: int, *, exit: Callable[[int], object] = os._exit) -> None:
    """End the process with ``code`` once everything of hers has shut down, skipping the exit hooks native libraries
    registered: PortAudio's terminate (``sounddevice``) and livekit's FFI dispose crashed the process on its way out
    (Windows access violation 0xC0000005, 2026-10-27 20:10, a restart from the tray), so the supervisor saw a crash
    instead of the exit code it was told. The log is flushed and closed first, and an empty crash log dropped, as a
    normal exit would; the OS frees the audio devices and threads."""
    with contextlib.suppress(Exception):
        logging.shutdown()
    if _crash_log is not None:
        _drop_if_empty(_crash_log)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            if stream is not None:
                stream.flush()
    exit(code)


def headless() -> bool:
    return sys.stdout is None or sys.stderr is None


def setup_logging(log_file: Path, level: str = "INFO", console: bool = False) -> None:
    """Configure root logging. ``console=True`` also echoes warnings to stderr."""
    global _crash_file, _crash_log
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if headless():
        stdio = open(log_file.with_suffix(".stdio.log"), "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        if sys.stdout is None:
            sys.stdout = stdio
        if sys.stderr is None:
            sys.stderr = stdio

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    if console and not headless():
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.WARNING if level.upper() != "DEBUG" else logging.DEBUG)
        stream.setFormatter(logging.Formatter("  %(levelname)s %(name)s: %(message)s"))
        # log.error(..., extra={"console": False}) = already shown by the status console.
        stream.addFilter(lambda record: getattr(record, "console", True))
        root.addHandler(stream)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    if _crash_file is None:
        crash_log = _crash_log = log_file.with_suffix(".crash.log")
        _crash_file = open(crash_log, "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=_crash_file, all_threads=True)
        atexit.register(_drop_if_empty, crash_log)

    def excepthook(exc_type, exc, tb) -> None:  # noqa: ANN001
        logging.getLogger("uncaught").critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        if sys.stderr is not None:
            sys.__excepthook__(exc_type, exc, tb)  # also show it, never swallow a crash

    sys.excepthook = excepthook
