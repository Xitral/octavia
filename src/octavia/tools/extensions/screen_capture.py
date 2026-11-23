"""Screen capture: look at the user's screen when they ask, e.g. to find a setting or spot a prompt they're missing.

A screenshot is taken only inside a call of this tool, never in the background. It is sized to exactly what the
model sees (``fit_for_claude``), so a pixel position the model names in it maps straight back onto the screen, and
goes back to the model as an image attached to the tool result. This module writes nothing to disk: the
full-resolution picture is kept only in memory, for a few minutes (``SCREEN``), so ``zoom_screen`` can show part of
it in more detail and ``point_at_screen`` can turn the model's pixel positions into places on the desktop; then
it's dropped. The picture the model saw is kept in memory too (``_pictures``), for ``send_to_phone`` to send when the
user asks for it on their phone. What goes to Claude is another matter: the Claude Code CLI keeps every tool result, pictures
included, in its conversation transcript under data/claude-home, so the tool texts make no promise about storage.
The tool is CONFIRM because the picture leaves the computer (it goes to Claude) and a screen can show anything,
but when the user's own words just asked for it (``octavia.tools.asked``), that request is the yes.

The capture uses GDI (``BitBlt``) through ctypes. Pictures go to the model as JPEG, not PNG: the CLI sends every
picture still in the conversation again with each request, so bytes add up over a screen task (her own transcripts:
about 0.4 s more wait per MB of pictures in the conversation, whatever their count), and a JPEG of a screen is about
a third of the PNG's size. The model sees it at the same size, so positions it names still map 1:1.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import io
import logging
import math
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from PIL import Image
from pydantic import Field

from octavia.core.events import ScreenshotTaken
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._pictures import PICTURES, PictureMemory

if TYPE_CHECKING:
    from octavia.tools.extensions._uia import Match

log = logging.getLogger(__name__)

Area = Literal["screen", "window", "all"]
Rect = tuple[int, int, int, int]  # left, top, right, bottom in physical pixels

# Claude's standard-tier image limits: no side over MAX_SIDE, and at most MAX_TOKENS patches of PATCH x PATCH pixels.
# A bigger image is scaled down by the API, and then the positions the model names no longer match ours.
MAX_SIDE = 1568
MAX_TOKENS = 1568
PATCH = 28
MAX_IMAGE_BYTES = 3_500_000  # stays well under the API's 5 MB per image once base64-encoded
IMAGE_MIME = "image/jpeg"
# High enough that small text stays crisp; no chroma subsampling, so coloured text (red errors, blue links) keeps
# its edges. A 1456x819 screen comes out around 140 KB, against 330-380 KB as PNG.
JPEG_QUALITY = 85
MIN_SIDE = 16  # a window smaller than this (or off screen) isn't worth a picture of its own
SCREEN_TTL_S = 300.0  # how long what was seen stays usable for zooming and pointing; then the pixels are dropped

_WHAT = {"screen": "the user's screen", "window": "the active window", "all": "all of the user's screens"}
_ASK = {"screen": "your screen", "window": "the active window", "all": "all your screens"}

# Win32 constants (winuser.h, wingdi.h, dwmapi.h)
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79
_MONITOR_DEFAULTTOPRIMARY = 1
_MONITOR_DEFAULTTONEAREST = 2
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_SRCCOPY, _CAPTUREBLT = 0x00CC0020, 0x40000000
_BI_RGB, _DIB_RGB_COLORS = 0, 0
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


class ScreenUnavailable(RuntimeError):
    """The screen can't be read right now (locked PC, secure desktop, GDI failure)."""


# --------------------------------------------------------------------------- image helpers


def clip_rect(rect: Rect, bounds: Rect) -> Rect | None:
    """``rect`` cut to ``bounds``; None when too little of it is left (minimised or off-screen windows)."""
    left, top = max(rect[0], bounds[0]), max(rect[1], bounds[1])
    right, bottom = min(rect[2], bounds[2]), min(rect[3], bounds[3])
    if right - left < MIN_SIDE or bottom - top < MIN_SIDE:
        return None
    return left, top, right, bottom


def choose_rect(area: Area, window: Rect | None, monitor: Rect, desktop: Rect) -> tuple[Rect, Area]:
    """What to capture for ``area`` and which area that really is: a window that can't be seen falls back to its
    monitor, so the user still gets an answer."""
    if area == "all":
        return desktop, "all"
    if area == "window" and window is not None and (rect := clip_rect(window, desktop)) is not None:
        return rect, "window"
    return clip_rect(monitor, desktop) or desktop, "screen"


def fit_for_claude(width: int, height: int, max_side: int = MAX_SIDE, max_tokens: int = MAX_TOKENS) -> tuple[int, int]:
    """The (width, height) Claude sees a ``width`` x ``height`` image at: send exactly this and the positions the
    model names are pixels of our image, with nothing to rescale.

    Anthropic's reference resize for the standard tier (models with a bigger budget take such an image as it is
    too): the largest size with the same aspect ratio whose sides, rounded up to whole 28-pixel patches, stay within
    ``max_side`` and whose patch count stays within ``max_tokens``. A 1920x1080 screen comes out 1456x819: the patch
    budget, not the side limit, decides for most screens. The short side rounds half to even, as the API does. An
    image that already fits comes back unchanged: never enlarged.
    """
    if width < 1 or height < 1:
        raise ValueError(f"no image size is {width}x{height}")

    def fits(w: int, h: int) -> bool:
        across, down = math.ceil(w / PATCH), math.ceil(h / PATCH)
        return across * PATCH <= max_side and down * PATCH <= max_side and across * down <= max_tokens

    if fits(width, height):
        return width, height
    if height > width:
        tall_h, tall_w = fit_for_claude(height, width, max_side, max_tokens)
        return tall_w, tall_h
    aspect = width / height
    lo, hi = 1, width  # search the long side: lo always fits, hi never does
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(mid, max(round(mid / aspect), 1)):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / aspect), 1)


def _box_average(pixels: np.ndarray, factor: int, axis: int) -> np.ndarray:
    """Every ``factor`` pixels along ``axis`` averaged into one (a remainder at the end is dropped), as float32.

    Summed as strided slices rather than a mean over a reshaped axis of length ``factor``: numpy reduces such a
    short axis slowly, and this runs on the full-resolution capture, before every look."""
    usable = pixels.shape[axis] - pixels.shape[axis] % factor

    def every(offset: int) -> np.ndarray:
        index: list[slice] = [slice(None)] * pixels.ndim
        index[axis] = slice(offset, usable, factor)
        return pixels[tuple(index)]

    total = every(0).astype(np.float32)
    for offset in range(1, factor):
        total += every(offset)
    total *= np.float32(1 / factor)
    return total


def _shrink_axis(pixels: np.ndarray, size: int, axis: int) -> np.ndarray:
    """Area-average ``pixels`` down to ``size`` along ``axis``.

    Every source pixel counts toward the result (unlike nearest-neighbour sampling), which keeps small text
    legible. A whole-pixel box average first keeps the float work small on 4K and multi-monitor captures; what's
    left shrinks by less than half, so each output pixel overlaps at most a few source pixels, and it is their
    overlap-weighted sum: a handful of gathers, much quicker than a running sum over the whole picture. It runs
    while the user waits for her answer, on every look.
    """
    n = pixels.shape[axis]
    if size >= n:
        return pixels.astype(np.float32)
    factor = n // size
    if factor >= 2:
        pixels = _box_average(pixels, factor, axis)
        n = pixels.shape[axis]
    if size >= n:
        return pixels.astype(np.float32, copy=False)
    ratio = n / size
    edges = np.arange(size + 1) * ratio
    first = np.floor(edges[:-1]).astype(np.intp)
    shape = [1] * pixels.ndim
    shape[axis] = size

    def share(k: int) -> np.ndarray:
        """What the k-th source pixel from each output pixel's left edge adds to it (nothing past its right edge)."""
        index = first + k
        overlap = np.minimum(edges[1:], index + 1) - np.maximum(edges[:-1], index)
        weight = (np.clip(overlap, 0, None) / ratio).astype(np.float32).reshape(shape)
        return np.take(pixels, np.minimum(index, n - 1), axis=axis) * weight

    out = share(0)
    for k in range(1, math.ceil(ratio) + 1):
        out += share(k)
    return out


def scale_down(pixels: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """RGB pixels resized to ``size`` (width, height) by area averaging. Never enlarges: a side that is already
    small enough stays as it is, and pixels that need no change come back untouched."""
    height, width = pixels.shape[:2]
    new_width, new_height = min(size[0], width), min(size[1], height)
    if (new_width, new_height) == (width, height):
        return pixels
    out = _shrink_axis(pixels, max(1, new_width), 1)
    out = _shrink_axis(out, max(1, new_height), 0)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def encode_image(pixels: np.ndarray) -> bytes:
    """RGB pixels as the JPEG the model is sent (``IMAGE_MIME``)."""
    out = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(pixels, dtype=np.uint8)).save(out, "JPEG", quality=JPEG_QUALITY, subsampling=0)
    return out.getvalue()


def attachment(data: bytes) -> dict[str, str]:
    """An encoded picture as a tool result's ``data["image"]``, which the brain hands the model."""
    return {"data": base64.b64encode(data).decode("ascii"), "mime_type": IMAGE_MIME}


def prepare_image(pixels: np.ndarray) -> tuple[bytes, int, int]:
    """Encoded bytes (``encode_image``), width and height of a picture at a size Claude takes as it is, small enough
    to send."""
    height, width = pixels.shape[:2]
    size = fit_for_claude(width, height)
    while True:
        small = scale_down(pixels, size)
        data = encode_image(small)
        if len(data) <= MAX_IMAGE_BYTES or max(size) <= 400:
            return data, small.shape[1], small.shape[0]
        # Too big to send (a very busy screen): a quarter off the long side, still a size the model sees 1:1.
        size = fit_for_claude(width, height, max_side=int(max(size) * 0.75))


# --------------------------------------------------------------------------- what was seen


def _to_screen(rect: Rect, width: int, height: int, x: float, y: float) -> tuple[int, int]:
    """Image pixel (x, y) of a ``width`` x ``height`` picture of ``rect`` -> the desktop pixel under its centre.

    Floored rather than rounded: the pixel that contains the centre, so a picture at full size maps 1:1.
    """
    left, top, right, bottom = rect
    sx = math.floor(left + (x + 0.5) * (right - left) / width)
    sy = math.floor(top + (y + 0.5) * (bottom - top) / height)
    return min(max(sx, left), right - 1), min(max(sy, top), bottom - 1)


def _box_to_screen(rect: Rect, width: int, height: int, box: tuple[float, float, float, float]) -> Rect:
    """A box in image pixels (edges, in any corner order) -> the desktop rectangle it covers, inside ``rect``."""
    left, top, right, bottom = rect
    x1, x2 = sorted((box[0], box[2]))
    y1, y2 = sorted((box[1], box[3]))
    kx, ky = (right - left) / width, (bottom - top) / height

    def fit(value: float, low: int, high: int) -> int:
        return min(max(round(value), low), high)

    return (
        fit(left + x1 * kx, left, right),
        fit(top + y1 * ky, top, bottom),
        fit(left + x2 * kx, left, right),
        fit(top + y2 * ky, top, bottom),
    )


@dataclass(frozen=True, eq=False)  # eq=False: comparing the pixel arrays makes no sense
class Screenshot:
    """A screenshot the model was shown, kept (in memory only) to zoom into and to point at things in it."""

    pixels: np.ndarray  # full-resolution RGB, for zooming in later
    rect: Rect  # the physical desktop pixels actually captured
    width: int  # the image the model saw
    height: int
    area: Area
    taken: float  # time.monotonic() (or the memory's clock) when it was captured

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """A pixel of the image the model saw -> physical desktop pixels (its centre)."""
        return _to_screen(self.rect, self.width, self.height, x, y)

    def box_to_screen(self, box: tuple[float, float, float, float]) -> Rect:
        """``[x1, y1, x2, y2]`` in the image the model saw -> the physical desktop rectangle it covers."""
        return _box_to_screen(self.rect, self.width, self.height, box)


def _from_screen(shot: Screenshot, x: int, y: int) -> tuple[int, int]:
    """A physical desktop pixel -> the pixel of the image the model saw of ``shot``, inside it."""
    left, top, right, bottom = shot.rect
    px = round((x - left) * shot.width / (right - left))
    py = round((y - top) * shot.height / (bottom - top))
    return min(max(px, 0), shot.width), min(max(py, 0), shot.height)


@dataclass(frozen=True)
class Zoom:
    """A zoomed-in part of the latest screenshot, as the model saw it (no pixels of its own: they're the shot's)."""

    source: Rect  # physical desktop pixels of the crop
    width: int  # the image the model saw
    height: int
    taken: float  # when its pixels were captured, i.e. the screenshot's time: it's exactly as old

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        return _to_screen(self.source, self.width, self.height, x, y)

    def box_to_screen(self, box: tuple[float, float, float, float]) -> Rect:
        return _box_to_screen(self.source, self.width, self.height, box)


class ScreenMemory:
    """The latest screenshot, zoom and UI Automation matches, so later tool calls can zoom into or point at what the
    model saw.

    In memory only, and every entry lapses ``ttl_s`` after its picture was taken: an old picture no longer matches
    the screen, and a screen can show private things that shouldn't linger. A timer drops the pixels on time even
    when no tool asks again; reads check the age themselves too. Thread-safe.
    """

    def __init__(self, ttl_s: float = SCREEN_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.now = clock
        self._lock = threading.Lock()
        self._shot: Screenshot | None = None
        self._zoom: Zoom | None = None
        self._matches: tuple[Match, ...] = ()
        self._matches_at = 0.0
        self._timer: threading.Timer | None = None

    def remember_shot(self, shot: Screenshot) -> None:
        """Keep a new screenshot. The zoom into the previous one goes with it."""
        with self._lock:
            self._shot, self._zoom = shot, None
            self._cancel_timer()
            timer = threading.Timer(max(0.0, shot.taken + self.ttl_s - self.now()) + 0.1, self.forget_expired)
            timer.daemon = True
            timer.start()
            self._timer = timer

    def remember_zoom(self, zoom: Zoom) -> None:
        with self._lock:
            self._zoom = zoom

    def remember_matches(self, matches: Iterable[Match]) -> None:
        """Keep the matches of the latest search (numbered from 1 in this order); an empty list forgets older ones."""
        with self._lock:
            self._matches, self._matches_at = tuple(matches), self.now()

    def shot(self) -> Screenshot | None:
        with self._lock:
            self._expire()
            return self._shot

    def zoom(self) -> Zoom | None:
        with self._lock:
            self._expire()
            return self._zoom

    def matches(self) -> list[Match]:
        with self._lock:
            self._expire()
            return list(self._matches)

    def looked_at(self) -> float:
        """When the screen was last looked at (the latest screenshot, or find_on_screen search, found or not), on
        this memory's clock; 0 before any. A zoom is only a closer look at the screenshot's old pixels."""
        with self._lock:
            self._expire()
            return max(self._shot.taken if self._shot is not None else 0.0, self._matches_at)

    def forget_expired(self) -> None:
        with self._lock:
            self._expire()

    def clear(self) -> None:
        with self._lock:
            self._shot, self._zoom, self._matches = None, None, ()
            self._cancel_timer()

    def _expire(self) -> None:
        """Drop whatever is too old. Call with the lock held."""
        now = self.now()
        if self._shot is not None and now - self._shot.taken > self.ttl_s:
            self._shot = None  # the only reference to the pixels
        if self._zoom is not None and now - self._zoom.taken > self.ttl_s:
            self._zoom = None
        if self._matches and now - self._matches_at > self.ttl_s:
            self._matches = ()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


SCREEN = ScreenMemory()


def pointing_avatar(ctx: ToolContext) -> Any | None:
    """The avatar, when it's running and able to point at things on the screen; otherwise None."""
    avatar = ctx.extras.get("avatar")
    if avatar is None:
        return None
    try:
        return avatar if avatar.can_point else None
    except Exception:  # a bridge in a bad state can't point either
        log.debug("Asking the avatar whether it can point failed", exc_info=True)
        return None


def can_mark(ctx: ToolContext) -> bool:
    """Whether she can draw marks on the screen now (mark_screen, ``octavia.visuals.marks``)."""
    try:
        return ctx.extras.get("marks") is not None and ctx.config.visuals.marks is True
    except Exception:  # a config without the setting
        return False


# --------------------------------------------------------------------------- GDI capture


@functools.cache
def _gdi() -> Any:
    """Typed Win32 entry points, built on first use because they only exist on Windows.

    Private ``WinDLL`` instances, so setting ``argtypes`` here can't change how other modules call the same
    functions, and 64-bit handles aren't truncated to the default ``int`` return type.
    """
    import ctypes
    from ctypes import POINTER, Structure, c_int, c_long, c_void_p
    from ctypes import wintypes as w
    from types import SimpleNamespace

    class BITMAPINFOHEADER(Structure):
        _fields_ = [
            ("biSize", w.DWORD),
            ("biWidth", w.LONG),
            ("biHeight", w.LONG),
            ("biPlanes", w.WORD),
            ("biBitCount", w.WORD),
            ("biCompression", w.DWORD),
            ("biSizeImage", w.DWORD),
            ("biXPelsPerMeter", w.LONG),
            ("biYPelsPerMeter", w.LONG),
            ("biClrUsed", w.DWORD),
            ("biClrImportant", w.DWORD),
        ]

    class BITMAPINFO(Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 1)]

    class MONITORINFO(Structure):
        _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, POINTER(w.RECT), w.LPARAM)

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")

    def sig(fn: Any, restype: Any, *argtypes: Any) -> None:
        fn.restype, fn.argtypes = restype, list(argtypes)

    sig(user32.GetSystemMetrics, c_int, c_int)
    sig(user32.GetForegroundWindow, w.HWND)
    sig(user32.IsIconic, w.BOOL, w.HWND)
    sig(user32.GetWindowRect, w.BOOL, w.HWND, POINTER(w.RECT))
    sig(user32.MonitorFromWindow, w.HMONITOR, w.HWND, w.DWORD)
    sig(user32.GetMonitorInfoW, w.BOOL, w.HMONITOR, POINTER(MONITORINFO))
    sig(user32.EnumDisplayMonitors, w.BOOL, w.HDC, POINTER(w.RECT), MONITORENUMPROC, w.LPARAM)
    sig(user32.GetDC, w.HDC, w.HWND)
    sig(user32.ReleaseDC, c_int, w.HWND, w.HDC)
    sig(gdi32.CreateCompatibleDC, w.HDC, w.HDC)
    sig(gdi32.CreateCompatibleBitmap, w.HBITMAP, w.HDC, c_int, c_int)
    sig(gdi32.SelectObject, w.HGDIOBJ, w.HDC, w.HGDIOBJ)
    sig(gdi32.BitBlt, w.BOOL, w.HDC, c_int, c_int, c_int, c_int, w.HDC, c_int, c_int, w.DWORD)
    sig(gdi32.GetDIBits, c_int, w.HDC, w.HBITMAP, w.UINT, w.UINT, c_void_p, POINTER(BITMAPINFO), w.UINT)
    sig(gdi32.DeleteObject, w.BOOL, w.HGDIOBJ)
    sig(gdi32.DeleteDC, w.BOOL, w.HDC)
    sig(dwmapi.DwmGetWindowAttribute, c_long, w.HWND, w.DWORD, c_void_p, w.DWORD)
    set_dpi = getattr(user32, "SetThreadDpiAwarenessContext", None)  # Windows 10 1607 and later
    if set_dpi is not None:
        sig(set_dpi, c_void_p, c_void_p)

    return SimpleNamespace(
        ctypes=ctypes,
        RECT=w.RECT,
        BITMAPINFO=BITMAPINFO,
        BITMAPINFOHEADER=BITMAPINFOHEADER,
        MONITORINFO=MONITORINFO,
        MONITORENUMPROC=MONITORENUMPROC,
        user32=user32,
        gdi32=gdi32,
        dwmapi=dwmapi,
        set_dpi=set_dpi,
    )


@contextlib.contextmanager
def per_monitor_dpi() -> Iterator[None]:
    """Make the calling thread per-monitor DPI aware for the block (Windows only).

    Otherwise Windows reports scaled coordinates to this thread: a 150 % display would come out cropped, and
    rectangles wouldn't be the physical pixels everything here works in.
    """
    api = _gdi()
    previous = api.set_dpi(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) if api.set_dpi is not None else None
    try:
        yield
    finally:
        if previous:
            api.set_dpi(previous)


def _rect(r: Any) -> Rect:
    return r.left, r.top, r.right, r.bottom


def monitor_rects() -> list[Rect]:
    """Every monitor's rectangle in physical desktop pixels; [] where that can't be known (not Windows). Blocking,
    but quick."""
    if sys.platform != "win32":
        return []
    api = _gdi()
    found: list[Rect] = []

    def collect(monitor: Any, dc: Any, rect: Any, data: Any) -> bool:
        found.append(_rect(rect.contents))
        return True

    with per_monitor_dpi():
        api.user32.EnumDisplayMonitors(None, None, api.MONITORENUMPROC(collect), 0)
    return found


def _window_rect(api: Any, hwnd: Any) -> Rect | None:
    """The visible frame of a window (without the invisible resize borders), None when minimised."""
    if not hwnd or api.user32.IsIconic(hwnd):
        return None
    r = api.RECT()
    size = api.ctypes.sizeof(r)
    if api.dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, api.ctypes.byref(r), size) == 0:
        return _rect(r)
    return _rect(r) if api.user32.GetWindowRect(hwnd, api.ctypes.byref(r)) else None


def _grab(api: Any, rect: Rect) -> np.ndarray:
    """RGB pixels of a rectangle of the desktop."""
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    ctypes, user32, gdi32 = api.ctypes, api.user32, api.gdi32
    screen = user32.GetDC(None)
    if not screen:
        raise ScreenUnavailable("the screen can't be read right now")
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    try:
        if not memory or not bitmap:
            raise ScreenUnavailable("there wasn't enough memory for the picture")
        previous = gdi32.SelectObject(memory, bitmap)
        copied = gdi32.BitBlt(memory, 0, 0, width, height, screen, left, top, _SRCCOPY | _CAPTUREBLT)
        gdi32.SelectObject(memory, previous)  # GetDIBits needs the bitmap out of the DC
        if not copied:
            raise ScreenUnavailable("the screen can't be read right now (is the PC locked?)")
        info = api.BITMAPINFO()
        header = info.bmiHeader
        header.biSize = ctypes.sizeof(api.BITMAPINFOHEADER)
        header.biWidth, header.biHeight = width, -height  # negative: rows top to bottom
        header.biPlanes, header.biBitCount, header.biCompression = 1, 32, _BI_RGB
        pixels = np.empty((height, width, 4), np.uint8)
        lines = gdi32.GetDIBits(
            memory, bitmap, 0, height, pixels.ctypes.data_as(ctypes.c_void_p), ctypes.byref(info), _DIB_RGB_COLORS
        )
        if lines != height:
            raise ScreenUnavailable("the picture couldn't be read back")
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
    return pixels[:, :, 2::-1].copy()  # BGRA to RGB


def foreground_frame() -> tuple[int, Rect, Rect] | None:
    """The window in front, its visible frame and the monitor it's on, in physical desktop pixels; None when there's
    no window in front, it's minimised, or this isn't Windows. Quick: the avatar polls it."""
    if sys.platform != "win32":
        return None
    api = _gdi()
    u = api.user32
    with per_monitor_dpi():
        hwnd = u.GetForegroundWindow()
        frame = _window_rect(api, hwnd)
        if not hwnd or frame is None:
            return None
        info = api.MONITORINFO()
        info.cbSize = api.ctypes.sizeof(info)
        found = u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST), api.ctypes.byref(info))
        monitor = _rect(info.rcMonitor) if found else frame
    return int(hwnd), frame, monitor


def capture_rect(rect: Rect) -> np.ndarray:
    """RGB pixels of ``rect`` (physical desktop pixels), cut to the desktop. Blocking. Raises ScreenUnavailable when
    nothing of it is on the desktop or the screen can't be read."""
    api = _gdi()
    with per_monitor_dpi():
        u = api.user32
        x, y = u.GetSystemMetrics(_SM_XVIRTUALSCREEN), u.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        desktop = (x, y, x + u.GetSystemMetrics(_SM_CXVIRTUALSCREEN), y + u.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
        clipped = clip_rect(rect, desktop)
        if clipped is None:
            raise ScreenUnavailable("that part of the screen is off the desktop")
        return _grab(api, clipped)


def capture_screen(area: Area) -> tuple[np.ndarray, Area, Rect]:
    """Screenshot of ``area`` as RGB pixels, the area really captured and where it is on the desktop. Blocking."""
    api = _gdi()
    with per_monitor_dpi():
        u = api.user32
        x, y = u.GetSystemMetrics(_SM_XVIRTUALSCREEN), u.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        desktop = (x, y, x + u.GetSystemMetrics(_SM_CXVIRTUALSCREEN), y + u.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
        hwnd = u.GetForegroundWindow()
        info = api.MONITORINFO()
        info.cbSize = api.ctypes.sizeof(info)
        monitor = desktop
        if u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTOPRIMARY), api.ctypes.byref(info)):
            monitor = _rect(info.rcMonitor)
        window = _window_rect(api, hwnd) if area == "window" else None
        rect, captured = choose_rect(area, window, monitor, desktop)
        return _grab(api, rect), captured, rect


# --------------------------------------------------------------------------- tool


class LookAtScreenArgs(ToolArgs):
    area: Area = Field(
        default="screen",
        description=(
            "'screen': the monitor the user is working on (where you last clicked), full detail. 'window': only "
            "the active window, sharper for small text. 'all': every monitor, less detail: to find something on "
            "another monitor, then click there and go back to 'screen'."
        ),
    )


# A capture gives the pixels, the area really captured and (optionally, e.g. in tests) the desktop rectangle they
# show; without one, the picture is taken to start at the desktop's origin.
Capture = Callable[[Area], tuple[np.ndarray, Area] | tuple[np.ndarray, Area, Rect]]


class LookAtScreen(Tool):
    name = "look_at_screen"
    description = (
        "Take a screenshot and look at it: to answer about the user's screen (find a setting, spot an error, read "
        "text they point at), or whenever a task you're doing needs to see it (where to click next, whether a step "
        "worked). Don't look when nothing needs it. The system asks the user first when their settings say so. "
        "Positions are pixels of the image you get, x from its left edge, y from its top."
    )
    Args = LookAtScreenArgs
    risk = RiskLevel.CONFIRM
    category = "system"
    timeout_s = 20.0

    def __init__(
        self, capture: Capture | None = None, memory: ScreenMemory | None = None, pictures: PictureMemory | None = None
    ) -> None:
        self._capture = capture
        self.memory = memory if memory is not None else SCREEN
        self._pictures = pictures if pictures is not None else PICTURES

    async def describe_call(self, args: LookAtScreenArgs, ctx: ToolContext) -> str:
        return f"take a screenshot of {_ASK[args.area]} and look at it"

    async def precheck(self, args: LookAtScreenArgs, ctx: ToolContext) -> str | None:
        if self._capture is None and sys.platform != "win32":
            return "Screenshots only work on Windows."
        return None

    async def run(self, args: LookAtScreenArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        # Emitted just before the capture, never awaited: the cue plays alongside the screenshot without delaying
        # it, and the avatar's window is kept out of screenshots (avatar.hide_from_capture), flash included.
        ctx.events.emit(ScreenshotTaken(area=args.area))
        try:
            pixels, captured, *where = await asyncio.to_thread(self._capture or capture_screen, args.area)
        except ScreenUnavailable as exc:
            return ToolResult.error(f"I couldn't take a screenshot: {exc}.")
        except Exception as exc:  # OSError from ctypes, or anything else GDI throws
            log.warning("Screen capture failed", exc_info=True)
            return ToolResult.error(f"I couldn't take a screenshot ({type(exc).__name__}).")
        taken = self.memory.now()
        rect: Rect = where[0] if where else (0, 0, pixels.shape[1], pixels.shape[0])
        data, width, height = await asyncio.to_thread(prepare_image, pixels)
        self.memory.remember_shot(Screenshot(pixels, rect, width, height, captured, taken))
        self._pictures.remember(data, time.strftime("Screenshot %Y-%m-%d %H%M%S"))
        note = ""
        if captured != args.area:
            note = " The active window couldn't be captured on its own (it may be minimised), so this is the screen."
        pointing = ""
        if pointing_avatar(ctx) is not None:
            pointing = (
                " To show the user where something is, call point_at_screen with its pixel position in this image."
                " Call zoom_screen first when the thing is small, crowded, or among similar-looking things (like one"
                " apple among oranges). To draw a ring, arrow or label there instead, use mark_screen."
            )
        elif can_mark(ctx):
            pointing = (
                " To show the user where something is, draw it on their screen with mark_screen at its pixel position"
                " in this image (zoom_screen first when it's small or crowded)."
            )
        note += self._circled_note(ctx, Screenshot(pixels, rect, width, height, captured, taken))
        content = (
            f"Screenshot of {_WHAT[captured]} attached ({width}x{height} pixels).{note} If the user asked about "
            "the screen, answer from it briefly and in plain speech, and say where things are ('top right', 'the blue "
            "Save button'); in the middle of a task, carry on with it without describing the screen. It shows only "
            "what's in view: to go through a whole document or page, scroll and look again to its end (or read its "
            "file with read_document); don't say you've read all of it before you have."
            f"{pointing} Never read out passwords, keys, tokens, one-time codes, card numbers or other secrets you "
            "can see: just say one is on screen."
        )
        return ToolResult.success(
            content, data={"image": attachment(data), "area": captured, "width": width, "height": height}
        )

    @staticmethod
    def _circled_note(ctx: ToolContext, shot: Screenshot) -> str:
        """Where in this picture the user has just circled, when they did it after their words went to the brain (a
        circle they made first came with the turn)."""
        memory = ctx.extras.get("circles")
        circled = memory.live() if memory is not None else None
        rect = circled.rect if circled is not None else None
        if rect is None or clip_rect(rect, shot.rect) is None:
            return ""
        memory.forget()  # she's told now: it's in the conversation
        (left, top), (right, bottom) = (_from_screen(shot, rect[0], rect[1]), _from_screen(shot, rect[2], rect[3]))
        return (
            f" The user just circled the part of the screen from about ({left}, {top}) to ({right}, {bottom}) in this "
            "picture with their mouse: when their words point at something without naming it, that's what they mean."
        )


TOOLS = [LookAtScreen]
