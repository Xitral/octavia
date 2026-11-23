"""Cheap looks at a game while she plays: small pictures, and local checks on them that cost no Claude usage.

A full screenshot costs about 1,600 image tokens; a picture 480 pixels wide about 170. What an image costs Claude
depends only on its size in pixels (not on how it's compressed), so the pictures here are simply small. Several
taken while she holds keys go back as one strip (a tool result carries one picture), left to right, then top to
bottom.

The danger checks are deliberately simple, so they run on every picture without asking Claude: the middle of the
view turning mostly blue (she walked into water, or was flung into the sky) and a health bar losing a good part of
its colour (the filled part of a bar is a strong colour; the empty part is grey or dark).
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from octavia.core.text import spoken_list
from octavia.tools.extensions.screen_capture import Capture, Rect, attachment, prepare_image, scale_down

log = logging.getLogger(__name__)

FRAME_WIDTH = 480  # each picture of the strip
MAX_FRAMES = 6  # in one strip: more taken are thinned out evenly, keeping the first and the last
STRIP_COLUMNS = 3
GAP = 4  # dark pixels between the pictures of a strip
BLUE_SHARE = 0.6  # the middle of the view this blue is water or sky ...
BLUE_RISE = 0.3  # ... when it wasn't, by this much, at the start
HEALTH_DROP = 0.25  # the health bar lost this share of its colour since the start


def small_frame(pixels: np.ndarray, width: int = FRAME_WIDTH) -> np.ndarray:
    """``pixels`` at most ``width`` wide, same aspect ratio."""
    height, wide = pixels.shape[:2]
    if wide <= width:
        return pixels
    return scale_down(pixels, (width, max(1, round(height * width / wide))))


def blue_share(pixels: np.ndarray) -> float:
    """How much of the middle of the view is water or sky blue (the edges hold the game's buttons and bars)."""
    height, width = pixels.shape[:2]
    middle = pixels[height // 6 : height - height // 6, width // 6 : width - width // 6].astype(np.int16)
    if middle.size == 0:
        return 0.0
    r, g, b = middle[..., 0], middle[..., 1], middle[..., 2]
    return float(((b > r + 40) & (b >= g) & (b > 90)).mean())


def bar_fill(pixels: np.ndarray) -> float:
    """How full a bar looks: the share of its pixels in a strong colour, whatever the colour (a health bar turns
    from green to red as it empties, and the empty part is grey or dark)."""
    if pixels.size == 0:
        return 0.0
    p = pixels.astype(np.int16)
    high, low = p.max(axis=2), p.min(axis=2)
    return float(((high >= 80) & (high - low >= 0.35 * high)).mean())


def crop(pixels: np.ndarray, rect: Rect, part: Rect) -> np.ndarray:
    """The pixels of desktop rectangle ``part`` in a picture of ``rect`` at full size (empty when outside it)."""
    left, top = max(part[0], rect[0]) - rect[0], max(part[1], rect[1]) - rect[1]
    right, bottom = min(part[2], rect[2]) - rect[0], min(part[3], rect[3]) - rect[1]
    if right <= left or bottom <= top:
        return pixels[:0, :0]
    return pixels[top:bottom, left:right]


def pick(count: int, most: int = MAX_FRAMES) -> list[int]:
    """Which of ``count`` pictures go in a strip: all of them, or ``most`` spread evenly with the first and last."""
    if count <= most:
        return list(range(count))
    return sorted({round(i * (count - 1) / (most - 1)) for i in range(most)})


def strip(frames: list[np.ndarray]) -> np.ndarray:
    """Pictures side by side, ``STRIP_COLUMNS`` to a row, with a dark gap between them."""
    columns = min(len(frames), STRIP_COLUMNS)
    rows = math.ceil(len(frames) / columns)
    cell_h = max(f.shape[0] for f in frames)
    cell_w = max(f.shape[1] for f in frames)
    out = np.zeros((rows * cell_h + (rows - 1) * GAP, columns * cell_w + (columns - 1) * GAP, 3), np.uint8)
    for i, frame in enumerate(frames):
        y, x = (i // columns) * (cell_h + GAP), (i % columns) * (cell_w + GAP)
        out[y : y + frame.shape[0], x : x + frame.shape[1]] = frame
    return out


@dataclass(frozen=True)
class Look:
    """One picture taken while holding: small, with what the checks measured on it."""

    at: float  # seconds into the hold; negative: after letting go
    pixels: np.ndarray
    blue: float
    fill: float | None  # the health bar's colour share, when one was given


@dataclass
class Eyes:
    """Small pictures of the game every ``every_s`` seconds while she holds keys, and the danger check that makes
    her let go early.

    ``look`` is awaited from the hold's loop: cheap when no picture is due. The capture and the measuring run in a
    thread, so the event loop (and the release of the keys) never waits on them.
    """

    capture: Capture
    every_s: float = 1.0
    health: Rect | None = None  # the health bar, in desktop pixels
    stop_on_danger: bool = True
    looks: list[Look] = field(default_factory=list)
    danger: str | None = None  # why she let go early
    problem: str | None = None  # why she stopped watching (the screen couldn't be read)
    _next_at: float = 0.0

    async def look(self, elapsed: float) -> str | None:
        """Take a picture when one is due; the danger it shows (she lets go then), or None."""
        if self.problem is not None or self.danger is not None or elapsed < self._next_at:
            return None
        self._next_at = elapsed + self.every_s
        taken = await self._take(elapsed)
        if taken is None or not self.stop_on_danger:
            return None
        self.danger = self._danger(taken)
        return self.danger

    async def last_look(self) -> None:
        """One more picture after letting go: where it ended up."""
        if self.problem is None:
            await self._take(-1.0)

    async def _take(self, elapsed: float) -> Look | None:
        try:
            taken = await asyncio.to_thread(self._measure, elapsed)
        except Exception as exc:  # ScreenUnavailable, OSError from ctypes, anything GDI throws
            log.warning("Watching the game failed", exc_info=True)
            self.problem = f"I couldn't watch the screen ({exc or type(exc).__name__}), so I stopped taking pictures."
            return None
        self.looks.append(taken)
        return taken

    def _measure(self, elapsed: float) -> Look:
        pixels, _, *where = self.capture("window")
        rect: Rect = where[0] if where else (0, 0, pixels.shape[1], pixels.shape[0])
        small = small_frame(pixels)
        fill = bar_fill(crop(pixels, rect, self.health)) if self.health is not None else None
        return Look(elapsed, small, blue_share(small), fill)

    def _danger(self, now: Look) -> str | None:
        first = self.looks[0]
        if now is first:
            return None
        if now.blue >= BLUE_SHARE and now.blue - first.blue >= BLUE_RISE:
            return "The view suddenly turned mostly blue (water or sky?), so I let go."
        if now.fill is not None and first.fill is not None and first.fill >= 0.05:
            lost = 1 - now.fill / first.fill
            if lost >= HEALTH_DROP:
                return f"The health bar dropped by about {lost:.0%}, so I let go."
        return None

    def picture(self) -> tuple[dict[str, Any], int, int, list[float]] | None:
        """The strip as an attachment for the model, its size, and when each of its pictures was taken."""
        if not self.looks:
            return None
        chosen = [self.looks[i] for i in pick(len(self.looks))]
        data, width, height = prepare_image(strip([look.pixels for look in chosen]))
        image = attachment(data)
        return image, width, height, [look.at for look in chosen]


def times_said(times: list[float]) -> str:
    """ "0, 1 and 2 seconds in, and after letting go"."""
    during = [f"{t:.1f}".rstrip("0").rstrip(".") for t in times if t >= 0]
    parts = []
    if during:
        listed = spoken_list(during)
        parts.append(f"{listed} seconds in")
    if any(t < 0 for t in times):
        parts.append("after letting go")
    return ", and ".join(parts)
