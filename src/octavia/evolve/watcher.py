"""Turns exceptions in the assistant's own code into improvement notes.

A logging handler, so nothing else has to know self-improvement exists: whatever the
code already logs with a traceback (``log.exception``, asyncio's "Task exception was
never retrieved", the uncaught-exception hook) reaches it. It keeps only what a code
change could plausibly fix: errors raised in the package's own code, not network,
audio-device or cancellation noise, and never errors that pass through the
self-improvement machinery itself, so a failing build can't feed on its own failures.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import os
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)

# Outages, cancellations and device trouble: real, but not something a code change fixes.
DEFAULT_IGNORED: frozenset[str] = frozenset(
    {
        "ConnectionError",
        "TimeoutError",
        "CancelledError",
        "KeyboardInterrupt",
        "CLIConnectionError",
        "ProcessError",
        "ConnectionClosed",
        "ConnectionClosedError",
        "InvalidStatus",
        "HTTPError",
        "TransportError",
        "TTSError",
        "PortAudioError",
        "gaierror",
    }
)

MAX_TITLE = 120
MAX_EVIDENCE = 4000
_TRACEBACK_LINES = 60
_MAX_MESSAGE = 1000
_MAX_CAUSES = 10
_OWN, _PACKAGE, _ELSEWHERE = "own", "package", ""


def _package_dir(package: str) -> Path | None:
    try:
        module = importlib.import_module(package)
    except ImportError:
        return None
    file = getattr(module, "__file__", None)
    return Path(file).parent if file else None


class ErrorWatcher(logging.Handler):
    """Turns exceptions logged by the assistant's own code into improvement notes.

    Calls ``on_error(fingerprint, title, evidence)``: on ``loop`` (thread-safe) when one is given and running,
    otherwise directly. The fingerprint is the exception type plus the innermost function of the package it went
    through, not the line number, so the same bug keeps its identity while the code around it is edited.
    ``package_dir`` and ``clock`` exist for tests.
    """

    def __init__(
        self,
        on_error: Callable[[str, str, str], None],
        loop: asyncio.AbstractEventLoop | None = None,
        package: str = "octavia",
        ignore_types: Iterable[str] = DEFAULT_IGNORED,
        max_per_minute: int = 10,
        *,
        package_dir: Path | str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self.on_error = on_error
        self.loop = loop
        self.package = package
        self.package_dir = Path(package_dir) if package_dir is not None else _package_dir(package)
        self.ignore_types = frozenset(ignore_types)
        self.max_per_minute = max_per_minute
        self._clock = clock
        self._root = os.path.normcase(os.path.abspath(self.package_dir)) if self.package_dir else ""
        self._own = os.path.join(self._root, "evolve")
        self._places: dict[str, str] = {}
        self._recent: deque[float] = deque()
        self._rate_lock = threading.Lock()
        self._busy = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._busy, "active", False):  # on_error logged something: don't note our own reaction
            return
        self._busy.active = True
        try:
            self._emit(record)
        except Exception:
            pass  # a logging handler must never break the code that logged
        finally:
            self._busy.active = False

    def _emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR or not self._root or not isinstance(record.exc_info, tuple):
            return
        if record.name == f"{self.package}.evolve" or record.name.startswith(f"{self.package}.evolve."):
            return
        exc_type, exc, tb = record.exc_info
        if exc_type is None or tb is None or self._ignored(exc_type, exc):
            return
        where = self._innermost(tb)
        if where is None:
            return
        filename, function = where
        rel = f"{self.package}/{os.path.abspath(filename)[len(self._root) + 1 :].replace(os.sep, '/')}"
        name = exc_type.__name__
        fingerprint = hashlib.sha1(f"{name}|{rel}:{function}".encode(), usedforsecurity=False).hexdigest()[:16]
        title = f"{name} in {rel}:{function}"[:MAX_TITLE]
        if not self._allow():
            return
        self._deliver(fingerprint, title, _evidence(record, exc_type, exc, tb))

    def _ignored(self, exc_type: type[BaseException], exc: BaseException | None) -> bool:
        """An ignored type anywhere in the MRO, or wrapped (``raise ... from``) around one: a network error
        re-raised as a RuntimeError is still a network error."""
        for _ in range(_MAX_CAUSES):
            if any(klass.__name__ in self.ignore_types for klass in exc_type.__mro__):
                return True
            cause = getattr(exc, "__cause__", None)
            if cause is None:
                return False
            exc_type, exc = type(cause), cause
        return False

    def _innermost(self, tb: TracebackType) -> tuple[str, str] | None:
        """(filename, qualified function) of the innermost frame in the package, or None when there is none or
        the traceback passes through the self-improvement package."""
        found = None
        for frame, _ in traceback.walk_tb(tb):
            code = frame.f_code
            place = self._place(code.co_filename)
            if place == _OWN:
                return None
            if place == _PACKAGE:
                found = (code.co_filename, code.co_qualname)
        return found

    def _place(self, filename: str) -> str:
        place = self._places.get(filename)
        if place is None:
            place = _ELSEWHERE
            if filename and not filename.startswith("<"):  # "<string>", "<frozen ...>"
                path = os.path.normcase(os.path.abspath(filename))
                if path.startswith(self._own + os.sep):
                    place = _OWN
                elif path.startswith(self._root + os.sep):
                    place = _PACKAGE
            if len(self._places) < 4096:
                self._places[filename] = place
        return place

    def _allow(self) -> bool:
        """At most ``max_per_minute`` notes a minute: an error in a hot loop mustn't flood the journal."""
        now = self._clock()
        with self._rate_lock:
            while self._recent and now - self._recent[0] >= 60.0:
                self._recent.popleft()
            if len(self._recent) >= self.max_per_minute:
                return False
            self._recent.append(now)
            return True

    def _deliver(self, fingerprint: str, title: str, evidence: str) -> None:
        loop = self.loop
        if loop is not None and loop.is_running() and not loop.is_closed():
            loop.call_soon_threadsafe(self._call, fingerprint, title, evidence)
        else:
            self._call(fingerprint, title, evidence)

    def _call(self, fingerprint: str, title: str, evidence: str) -> None:
        try:
            self.on_error(fingerprint, title, evidence)
        except Exception:
            log.warning("Couldn't record an error for self-improvement", exc_info=True)


def _evidence(
    record: logging.LogRecord, exc_type: type[BaseException], exc: BaseException | None, tb: TracebackType
) -> str:
    """The log message, then the end of the traceback (where the error is), within MAX_EVIDENCE."""
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    message = message.strip()[:_MAX_MESSAGE]
    lines = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip().splitlines()
    trace = "\n".join(lines[-_TRACEBACK_LINES:])
    room = MAX_EVIDENCE - len(message) - 1
    return f"{message}\n{trace[-room:] if len(trace) > room else trace}"
