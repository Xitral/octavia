"""The latest picture she got (a satellite photo, a screenshot), so ``send_to_phone`` can send it to the user's phone
as a file when they ask ("send me that picture"): those pictures only ever exist in memory, so without this there'd
be nothing on the PC to send.

Only the newest is kept, in memory, and it lapses ``ttl_s`` after it was taken: a picture of the screen can show
private things that shouldn't linger. A timer drops it on time even when nothing asks again. Thread-safe.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

PICTURE_TTL_S = 600.0
_SUFFIXES = {b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg", b"GIF8": ".gif"}


@dataclass(frozen=True, eq=False)
class Picture:
    data: bytes
    name: str  # a file name for it, as the phone shows it: "Satellite - Portland.jpg"
    taken: float


def file_name(title: str, data: bytes) -> str:
    """``title`` as a file name Windows takes, with the suffix the picture's bytes call for."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title)
    stem = " ".join(stem.split()).strip(" .")[:100] or "Picture"
    suffix = next((s for head, s in _SUFFIXES.items() if data.startswith(head)), ".jpg")
    return stem + suffix


class PictureMemory:
    """The newest picture a tool got, for a few minutes."""

    def __init__(self, ttl_s: float = PICTURE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.now = clock
        self._lock = threading.Lock()
        self._picture: Picture | None = None
        self._timer: threading.Timer | None = None

    def remember(self, data: bytes, title: str) -> None:
        """Keep ``data`` (PNG or JPEG bytes) as the latest picture, named after ``title``."""
        with self._lock:
            self._picture = Picture(data, file_name(title, data), self.now())
            self._cancel_timer()
            timer = threading.Timer(self.ttl_s + 0.1, self.forget_expired)
            timer.daemon = True
            timer.start()
            self._timer = timer

    def latest(self) -> Picture | None:
        with self._lock:
            self._expire()
            return self._picture

    def forget_expired(self) -> None:
        with self._lock:
            self._expire()

    def clear(self) -> None:
        with self._lock:
            self._picture = None
            self._cancel_timer()

    def _expire(self) -> None:
        if self._picture is not None and self.now() - self._picture.taken > self.ttl_s:
            self._picture = None

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


PICTURES = PictureMemory()
