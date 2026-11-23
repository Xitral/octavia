"""The faint ring that says "I saw that" when the user circles something: hazy blue, gone in about a second.

A small native window of its own, not one of her Electron apps: it has to show within a few milliseconds of the loop
closing, whether or not the avatar or the marks app is running, and fading it only changes one number, so it costs
nothing however large the ring is. It can't be clicked or focused, stays on top, and stays out of screenshots.
"""

from __future__ import annotations

import functools
import logging
import math
import queue
import sys
import threading
import time
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

Rect = tuple[int, int, int, int]

GLOW_PX = 30  # room round the ring for its haze
RING_GAP_PX = 10  # the ring sits a little outside where the pointer went
MAX_SIDE_PX = 1800
FADE_IN_S = 0.11
HOLD_S = 0.22
FADE_OUT_S = 0.85
FRAME_S = 1 / 60
# Her panels' colours (avatar/renderer/panel): --glow for the line, --line for the haze round it.
LINE_RGB = np.array((168, 214, 255), dtype=np.float32)
HAZE_RGB = np.array((76, 156, 245), dtype=np.float32)
_LINE_ALPHA, _LINE_PX = 0.8, 1.4
_HAZE_ALPHA, _HAZE_PX = 0.3, 11.0
_LEVELS = 14.0  # the haze comes in steps with a dither between them, as the PS2 drew it
_BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float32) / 16.0

_WS_POPUP = 0x80000000
_WS_EX_TOPMOST, _WS_EX_TRANSPARENT, _WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
_WS_EX_LAYERED, _WS_EX_NOACTIVATE = 0x80000, 0x8000000
_SW_SHOWNOACTIVATE = 4
_ULW_ALPHA, _AC_SRC_OVER, _AC_SRC_ALPHA = 2, 0, 1
_WDA_EXCLUDEFROMCAPTURE = 0x11
_PM_REMOVE = 1
_CLASS_NAME = "OctaviaCircleRing"
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


def ring_box(rect: Rect) -> Rect:
    """The window for a ring round ``rect``: with room for the gap and the haze, and never absurdly large."""
    left, top, right, bottom = rect
    margin = RING_GAP_PX + GLOW_PX
    width, height = min(right - left, MAX_SIDE_PX), min(bottom - top, MAX_SIDE_PX)
    mid_x, mid_y = (left + right) // 2, (top + bottom) // 2
    return (
        mid_x - width // 2 - margin,
        mid_y - height // 2 - margin,
        mid_x + width // 2 + margin,
        mid_y + height // 2 + margin,
    )


def draw_ring(width: int, height: int) -> np.ndarray:
    """A ring filling a ``width`` x ``height`` window (glow margin included), as premultiplied BGRA rows."""
    half_w, half_h = max(width / 2 - GLOW_PX, 2.0), max(height / 2 - GLOW_PX, 2.0)
    xs = (np.arange(width, dtype=np.float32) + 0.5 - width / 2) / half_w
    ys = (np.arange(height, dtype=np.float32) + 0.5 - height / 2) / half_h
    grid_x, grid_y = np.meshgrid(xs, ys)
    f = np.sqrt(grid_x * grid_x + grid_y * grid_y) + 1e-6
    # Distance from the ellipse in pixels, to first order: an oval's line is as thick all the way round.
    slope = np.sqrt((grid_x / half_w) ** 2 + (grid_y / half_h) ** 2) / f
    distance = np.abs(f - 1.0) / np.maximum(slope, 1e-6)
    line = _LINE_ALPHA * np.exp(-((distance / _LINE_PX) ** 2))
    haze = _HAZE_ALPHA * np.exp(-((distance / _HAZE_PX) ** 2))
    tiles = np.tile(_BAYER, (height // 4 + 1, width // 4 + 1))[:height, :width]
    haze = np.floor(haze * _LEVELS + tiles) / _LEVELS
    haze[1::2] *= 0.8  # scanlines
    alpha = np.maximum(line, haze)
    share = (line / np.maximum(line + haze, 1e-6))[..., None]
    rgb = LINE_RGB * share + HAZE_RGB * (1 - share)
    out = np.empty((height, width, 4), np.uint8)
    premultiplied = rgb * alpha[..., None]
    out[..., 0] = premultiplied[..., 2]
    out[..., 1] = premultiplied[..., 1]
    out[..., 2] = premultiplied[..., 0]
    out[..., 3] = alpha * 255
    return out


def fade_at(elapsed: float) -> float | None:
    """How much of the ring shows ``elapsed`` seconds in (0 to 1, eased both ways), or None once it's gone."""
    if elapsed < FADE_IN_S:
        return math.sin(elapsed / FADE_IN_S * math.pi / 2)
    if elapsed < FADE_IN_S + HOLD_S:
        return 1.0
    out = (elapsed - FADE_IN_S - HOLD_S) / FADE_OUT_S
    return None if out >= 1.0 - 1e-9 else math.cos(out * math.pi / 2) ** 2


@functools.cache
def _win() -> Any:
    """Typed Win32 entry points, private to this module, built on first use (they only exist on Windows)."""
    import ctypes
    from ctypes import POINTER, Structure, c_int, c_void_p, wintypes
    from types import SimpleNamespace

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lresult = ctypes.c_ssize_t
    wndproc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc),
            ("cbClsExtra", c_int),
            ("cbWndExtra", c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", c_void_p),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class BLENDFUNCTION(Structure):
        _fields_ = [
            ("BlendOp", ctypes.c_byte),
            ("BlendFlags", ctypes.c_byte),
            ("SourceConstantAlpha", ctypes.c_ubyte),
            ("AlphaFormat", ctypes.c_byte),
        ]

    class BITMAPINFOHEADER(Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    def sig(fn: Any, restype: Any, *argtypes: Any) -> None:
        fn.restype, fn.argtypes = restype, list(argtypes)

    sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
    sig(user32.DefWindowProcW, lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    sig(user32.RegisterClassExW, wintypes.ATOM, POINTER(WNDCLASSEXW))
    sig(
        user32.CreateWindowExW,
        wintypes.HWND,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        c_int,
        c_int,
        c_int,
        c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        c_void_p,
    )
    sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    sig(user32.ShowWindow, wintypes.BOOL, wintypes.HWND, c_int)
    sig(user32.SetWindowDisplayAffinity, wintypes.BOOL, wintypes.HWND, wintypes.DWORD)
    sig(
        user32.UpdateLayeredWindow,
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.HDC,
        POINTER(wintypes.POINT),
        POINTER(wintypes.SIZE),
        wintypes.HDC,
        POINTER(wintypes.POINT),
        wintypes.COLORREF,
        POINTER(BLENDFUNCTION),
        wintypes.DWORD,
    )
    sig(
        user32.PeekMessageW,
        wintypes.BOOL,
        POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    )
    sig(user32.DispatchMessageW, lresult, POINTER(wintypes.MSG))
    sig(user32.GetDC, wintypes.HDC, wintypes.HWND)
    sig(user32.ReleaseDC, c_int, wintypes.HWND, wintypes.HDC)
    sig(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
    sig(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
    sig(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
    sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
    sig(
        gdi32.CreateDIBSection,
        wintypes.HBITMAP,
        wintypes.HDC,
        POINTER(BITMAPINFOHEADER),
        wintypes.UINT,
        POINTER(c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    )
    set_dpi = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if set_dpi is not None:
        sig(set_dpi, c_void_p, c_void_p)
    return SimpleNamespace(
        ctypes=ctypes,
        w=wintypes,
        user32=user32,
        gdi32=gdi32,
        kernel32=kernel32,
        WNDCLASSEXW=WNDCLASSEXW,
        WNDPROC=wndproc,
        BLENDFUNCTION=BLENDFUNCTION,
        BITMAPINFOHEADER=BITMAPINFOHEADER,
        set_dpi=set_dpi,
    )


@functools.cache
def _window_class() -> str:
    """Registers the ring's window class once. It runs nothing of ours: Windows' default handler does it all."""
    api = _win()
    cls = api.WNDCLASSEXW()
    cls.cbSize = api.ctypes.sizeof(cls)
    cls.lpfnWndProc = api.ctypes.cast(api.user32.DefWindowProcW, api.WNDPROC)
    cls.hInstance = api.kernel32.GetModuleHandleW(None)
    cls.lpszClassName = _CLASS_NAME
    if not api.user32.RegisterClassExW(api.ctypes.byref(cls)):
        raise OSError(api.ctypes.get_last_error(), "RegisterClassExW failed")
    return _CLASS_NAME


class _Layered:
    """One ring's window, on the screen until ``close``."""

    def __init__(self, box: Rect, *, hide_from_capture: bool) -> None:
        api = self.api = _win()
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        pixels = draw_ring(width, height)
        self.hwnd = api.user32.CreateWindowExW(
            _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
            _window_class(),
            "Octavia ring",
            _WS_POPUP,
            left,
            top,
            width,
            height,
            None,
            None,
            api.kernel32.GetModuleHandleW(None),
            None,
        )
        if not self.hwnd:
            raise OSError(api.ctypes.get_last_error(), "CreateWindowExW failed")
        if hide_from_capture:
            api.user32.SetWindowDisplayAffinity(self.hwnd, _WDA_EXCLUDEFROMCAPTURE)
        header = api.BITMAPINFOHEADER()
        header.biSize = api.ctypes.sizeof(header)
        header.biWidth, header.biHeight, header.biPlanes, header.biBitCount = width, -height, 1, 32
        bits = api.ctypes.c_void_p()
        screen = api.user32.GetDC(None)
        memory = api.gdi32.CreateCompatibleDC(screen)
        bitmap = api.gdi32.CreateDIBSection(screen, api.ctypes.byref(header), 0, api.ctypes.byref(bits), None, 0)
        try:
            if not bitmap or not bits:
                raise OSError(api.ctypes.get_last_error(), "CreateDIBSection failed")
            api.ctypes.memmove(bits, pixels.ctypes.data, pixels.size)
            previous = api.gdi32.SelectObject(memory, bitmap)
            at, size, origin = api.w.POINT(left, top), api.w.SIZE(width, height), api.w.POINT(0, 0)
            blend = self._blend(0.0)
            shown = api.user32.UpdateLayeredWindow(
                self.hwnd,
                screen,
                api.ctypes.byref(at),
                api.ctypes.byref(size),
                memory,
                api.ctypes.byref(origin),
                0,
                api.ctypes.byref(blend),
                _ULW_ALPHA,
            )
            api.gdi32.SelectObject(memory, previous)
            if not shown:
                raise OSError(api.ctypes.get_last_error(), "UpdateLayeredWindow failed")
        except Exception:
            self.close()
            raise
        finally:
            if bitmap:
                api.gdi32.DeleteObject(bitmap)
            api.gdi32.DeleteDC(memory)
            api.user32.ReleaseDC(None, screen)
        api.user32.ShowWindow(self.hwnd, _SW_SHOWNOACTIVATE)

    def _blend(self, amount: float) -> Any:
        blend = self.api.BLENDFUNCTION()
        blend.BlendOp, blend.AlphaFormat = _AC_SRC_OVER, _AC_SRC_ALPHA
        blend.SourceConstantAlpha = max(0, min(255, round(amount * 255)))
        return blend

    def set_fade(self, amount: float) -> bool:
        api = self.api
        blend = self._blend(amount)
        return bool(
            api.user32.UpdateLayeredWindow(
                self.hwnd, None, None, None, None, None, 0, api.ctypes.byref(blend), _ULW_ALPHA
            )
        )

    def pump(self) -> None:
        api = self.api
        message = api.w.MSG()
        while api.user32.PeekMessageW(api.ctypes.byref(message), None, 0, 0, _PM_REMOVE):
            api.user32.DispatchMessageW(api.ctypes.byref(message))

    def close(self) -> None:
        if self.hwnd:
            self.api.user32.DestroyWindow(self.hwnd)
            self.hwnd = None


class CircleRing:
    """Shows rings, one at a time, from a thread of its own (started on the first). A new one replaces one still
    fading. Nothing here can fail a caller: a ring that doesn't come up is only logged."""

    def __init__(self, *, hide_from_capture: bool = True) -> None:
        self.hide_from_capture = hide_from_capture
        self._queue: queue.SimpleQueue[Rect | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def show(self, rect: Rect) -> None:
        """A ring round ``rect`` (physical desktop pixels)."""
        if sys.platform != "win32":
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="circle-ring", daemon=True)
                self._thread.start()
        self._queue.put(rect)

    def close(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=2.0)

    def _run(self) -> None:
        api = _win()
        if api.set_dpi is not None:
            api.set_dpi(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)  # all of this is in physical pixels
        item = self._queue.get()
        while item is not None:
            item = self._play(item)

    def _play(self, rect: Rect) -> Rect | None:
        """Plays one ring; returns what to do next (the next ring, or None to stop)."""
        try:
            window = _Layered(ring_box(rect), hide_from_capture=self.hide_from_capture)
        except Exception:
            log.warning("The circle ring couldn't be shown", exc_info=True)
            return self._queue.get()
        began = time.perf_counter()
        try:
            while True:
                fade = fade_at(time.perf_counter() - began)
                if fade is None:
                    break
                window.set_fade(fade)
                window.pump()
                try:
                    return self._queue.get(timeout=FRAME_S)
                except queue.Empty:
                    pass
        finally:
            window.close()
        return self._queue.get()
