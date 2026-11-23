"""A window on the user's screen showing one picture that's only to be looked at (a satellite photo): no browser, no
title bar, just the picture and a line saying what it is.

It runs as its own process (``python -m octavia.platform.picture_window FILE TITLE CAPTION``): Tk wants the window's
whole life on one thread, and a window the user leaves open shouldn't hold the assistant up or close when she
restarts. Tk 8.6 reads PNG and GIF but not JPEG, so callers hand it a PNG. The viewer deletes the file once it's
read and prints ``ready`` once the window is up, so the caller knows the picture really is on the screen.

Starting Python and Tk takes a good part of a second, so a caller that knows a picture is coming (the satellite tool,
while it looks up the place and downloads the photo) starts a viewer ahead with ``warm_up``: ``python -m
octavia.platform.picture_window --wait`` gets Tk ready and waits for one JSON line on stdin naming the file, title and
caption. ``show_picture`` hands the picture to that waiting viewer, or starts one as before when there's none.

Tk only scales a picture by whole factors, so zoom goes from 1x to 4x: the mouse wheel, a double-click or +/- zoom
on the point under the pointer, dragging moves around, 0 shows the whole picture again. With no title bar, dragging
the caption line moves the window, and Escape, a right-click or the x at the end of the caption closes it. It still
has a taskbar button, so it can be found again behind other windows.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

READY = "ready"
MAX_ZOOM = 4
BACKGROUND = "#111111"
HINT = "Wheel or double-click to zoom, drag to look around, 0 for all of it, Esc to close"
READY_TIMEOUT_S = 15.0  # a viewer that isn't up by then is stopped, so the caller never waits on it for long
WAIT = "--wait"
IDLE_TIMEOUT_S = 60.0  # a viewer started ahead that's never handed a picture (the download failed) is stopped then
MODULE = "octavia.platform.picture_window"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def layout(size: int, zoom: int, view: int) -> tuple[int, int]:
    """Along one axis: how long the scrollable area is, and where the picture starts in it. The picture is centred
    while it's smaller than the view."""
    shown = size * zoom
    region = max(shown, view)
    return region, (region - shown) // 2


def zoomed_start(at: float, start: float, size: int, view: int, old: int, new: int) -> float:
    """Along one axis: where the view starts after zooming from ``old`` to ``new``, so the spot of the picture under
    the pointer (``at`` pixels into a view that starts at ``start``) stays under it, as far as the picture allows."""
    _, offset = layout(size, old, view)
    spot = min(max((start + at - offset) / old, 0.0), float(size))
    region, offset = layout(size, new, view)
    return min(max(offset + spot * new - at, 0.0), float(region - view))


class Viewer:
    """The picture on a canvas that zooms and pans."""

    def __init__(self, root: Any, picture: Any, caption: str, view: tuple[int, int]) -> None:
        import tkinter as tk

        self._root = root
        self._picture = picture
        self._shown = picture
        self._zoom = 1
        root.configure(background=BACKGROUND)
        canvas = tk.Canvas(
            root,
            width=view[0],
            height=view[1],
            background=BACKGROUND,
            highlightthickness=0,
            borderwidth=0,
            xscrollincrement=1,
            yscrollincrement=1,
            cursor="fleur",
        )
        canvas.pack(fill="both", expand=True)
        bar = tk.Frame(root, background=BACKGROUND, cursor="fleur")
        bar.pack(fill="x")
        close = tk.Label(bar, text="✕", background=BACKGROUND, foreground="#b8b8b8", padx=10, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _: root.destroy())
        text = f"{caption}    {HINT}" if caption else HINT
        label = tk.Label(bar, text=text, background=BACKGROUND, foreground="#b8b8b8", anchor="w", padx=8, pady=4)
        label.pack(side="left", fill="x", expand=True)
        for handle in (bar, label):  # no title bar to drag, so the caption line moves the window
            handle.bind("<ButtonPress-1>", self._grab)
            handle.bind("<B1-Motion>", self._move)
        self._canvas = canvas
        self._item = canvas.create_image(0, 0, anchor="nw", image=picture)
        canvas.bind("<Configure>", lambda _: self._place())
        canvas.bind("<ButtonPress-1>", lambda e: canvas.scan_mark(e.x, e.y))
        canvas.bind("<B1-Motion>", lambda e: canvas.scan_dragto(e.x, e.y, gain=1))
        canvas.bind("<Double-Button-1>", lambda e: self.zoom(self._zoom + 1, e.x, e.y))
        root.bind("<MouseWheel>", self._wheel)  # on the window: Tk on Windows may send it to the focus, not the canvas
        root.bind("<Escape>", lambda _: root.destroy())
        root.bind("<Button-3>", lambda _: root.destroy())
        root.bind("<ButtonPress>", lambda _: root.focus_force(), add="+")  # keys reach a frameless window once clicked
        self._grabbed = (0, 0)
        for key in ("<plus>", "<equal>", "<KP_Add>"):
            root.bind(key, lambda _: self.zoom(self._zoom + 1))
        for key in ("<minus>", "<KP_Subtract>"):
            root.bind(key, lambda _: self.zoom(self._zoom - 1))
        root.bind("<Key-0>", lambda _: self.zoom(1))
        self._place()

    def _grab(self, event: Any) -> None:
        self._grabbed = (event.x_root - self._root.winfo_x(), event.y_root - self._root.winfo_y())

    def _move(self, event: Any) -> None:
        self._root.geometry(f"+{event.x_root - self._grabbed[0]}+{event.y_root - self._grabbed[1]}")

    def _wheel(self, event: Any) -> None:
        x, y = event.x_root - self._canvas.winfo_rootx(), event.y_root - self._canvas.winfo_rooty()
        self.zoom(self._zoom + (1 if event.delta > 0 else -1), x, y)

    def _view(self) -> tuple[int, int]:
        return self._canvas.winfo_width(), self._canvas.winfo_height()

    def _place(self) -> tuple[int, int]:
        width, height = self._view()
        across, left = layout(self._picture.width(), self._zoom, width)
        down, top = layout(self._picture.height(), self._zoom, height)
        self._canvas.configure(scrollregion=(0, 0, across, down))
        self._canvas.coords(self._item, left, top)
        return across, down

    def zoom(self, zoom: int, x: float | None = None, y: float | None = None) -> None:
        """Zoom to a whole factor, keeping the spot at (x, y) in the view, or its centre, where it is."""
        zoom = max(1, min(MAX_ZOOM, zoom))
        if zoom == self._zoom:
            return
        width, height = self._view()
        x = width / 2 if x is None else x
        y = height / 2 if y is None else y
        start_x = zoomed_start(x, self._canvas.canvasx(0), self._picture.width(), width, self._zoom, zoom)
        start_y = zoomed_start(y, self._canvas.canvasy(0), self._picture.height(), height, self._zoom, zoom)
        self._shown = self._picture.zoom(zoom) if zoom > 1 else self._picture
        self._canvas.itemconfigure(self._item, image=self._shown)
        self._zoom = zoom
        across, down = self._place()
        self._canvas.xview_moveto(start_x / across)
        self._canvas.yview_moveto(start_y / down)


def _dpi_aware() -> None:
    """Shows the picture pixel for pixel instead of blurred by Windows' display scaling."""
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)


def _frameless(root: Any) -> None:
    """No title bar or border, but still a taskbar button (Windows hides one for a frameless window by default)."""
    root.overrideredirect(True)
    if sys.platform != "win32":
        return
    import ctypes

    gwl_exstyle, app_window, tool_window = -20, 0x00040000, 0x00000080
    with contextlib.suppress(AttributeError, OSError, ValueError):
        user32 = ctypes.windll.user32
        hwnd = int(root.wm_frame(), 16)
        style = user32.GetWindowLongW(hwnd, gwl_exstyle)
        user32.SetWindowLongW(hwnd, gwl_exstyle, (style & ~tool_window) | app_window)


def instructions(line: str) -> list[str] | None:
    """The file, title and caption a waiting viewer was sent (``_hand_over``); None for EOF or anything else."""
    try:
        sent = json.loads(line)
        return [str(sent["path"]), str(sent.get("title", "")), str(sent.get("caption", ""))]
    except (ValueError, KeyError, TypeError):
        return None


def main(argv: list[str]) -> int:
    _dpi_aware()
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    if argv[:1] == [WAIT]:
        sent = instructions(sys.stdin.readline())  # EOF when the assistant stops it or exits
        if sent is None:
            root.destroy()
            return 1
        argv = sent
    path, title, caption = (*argv, "", "")[:3]
    try:
        picture = tk.PhotoImage(master=root, file=path)
    except tk.TclError:
        return 1
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)
    root.title(title or "Picture")  # still the taskbar button's name
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    view = min(picture.width(), screen_w * 9 // 10), min(picture.height(), screen_h * 8 // 10)
    Viewer(root, picture, caption, view)
    root.update_idletasks()
    width, height = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{max((screen_w - width) // 2, 0)}+{max((screen_h - height) // 2 - 30, 0)}")
    _frameless(root)
    root.deiconify()
    root.attributes("-topmost", True)  # in front of the window the user is in, then an ordinary window again
    root.focus_force()
    root.after(500, lambda: root.attributes("-topmost", False))
    with contextlib.suppress(AttributeError, OSError, ValueError):
        print(READY, flush=True)
    root.mainloop()
    return 0


def _remove(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _stop(process: Any) -> None:
    with contextlib.suppress(OSError):
        process.kill()


class Spare:
    """One viewer started ahead (``--wait``), waiting for its picture: Python and Tk are up by the time it comes."""

    def __init__(self, popen: Callable[..., Any] = subprocess.Popen, idle_timeout_s: float = IDLE_TIMEOUT_S) -> None:
        self._popen = popen
        self._idle_timeout_s = idle_timeout_s
        self._lock = threading.Lock()
        self._process: Any = None
        self._timer: threading.Timer | None = None

    def start(self) -> None:
        """Start a waiting viewer, unless one is already waiting."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            try:
                process = self._popen(
                    [sys.executable, "-m", MODULE, WAIT],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=_NO_WINDOW,
                )
            except OSError as exc:
                log.warning("couldn't start a picture window ahead: %s", exc)
                return
            timer = threading.Timer(self._idle_timeout_s, self._expire, (process,))
            timer.daemon = True
            timer.start()
            self._process, self._timer = process, timer

    def take(self) -> Any:
        """The waiting viewer, now the caller's to hand a picture to; None if there's none still running."""
        with self._lock:
            process, self._process = self._process, None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if process is None or process.poll() is not None:
            return None
        return process

    def _expire(self, process: Any) -> None:
        with self._lock:
            if self._process is not process:  # taken meanwhile
                return
            self._process, self._timer = None, None
        _stop(process)


_SPARE = Spare()


def _hand_over(process: Any, path: str, title: str, caption: str) -> bool:
    """Send a waiting viewer its picture; False (and it's stopped) if it's gone."""
    line = json.dumps({"path": path, "title": title, "caption": caption}) + "\n"
    try:
        with process.stdin:
            process.stdin.write(line.encode("ascii"))
    except (OSError, ValueError) as exc:
        log.warning("the picture window started ahead had gone: %s", exc)
        _stop(process)
        return False
    return True


def launch(
    picture: bytes,
    title: str,
    caption: str,
    popen: Callable[..., Any] = subprocess.Popen,
    ready_timeout_s: float = READY_TIMEOUT_S,
    spare: Spare | None = _SPARE,
) -> bool:
    """Opens the window, in the viewer ``spare`` holds when one is waiting, else in a process of its own, and waits
    until it's up: False if it couldn't start or read the picture, or wasn't up within ``ready_timeout_s`` (then
    it's stopped)."""
    descriptor, path = tempfile.mkstemp(prefix="octavia-picture-", suffix=".png")
    with os.fdopen(descriptor, "wb") as file:
        file.write(picture)
    process = spare.take() if spare is not None else None
    if process is not None and not _hand_over(process, path, title, caption):
        process = None
    if process is None:
        try:
            process = popen(
                [sys.executable, "-m", MODULE, path, title, caption],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            log.warning("couldn't start the picture window: %s", exc)
            _remove(path)
            return False
    timer = threading.Timer(ready_timeout_s, _stop, (process,))  # a stuck viewer ends the wait below with EOF
    timer.start()
    try:
        with process.stdout:
            ready = process.stdout.readline().strip() == READY.encode()
    finally:
        timer.cancel()
    if not ready:
        log.warning("the picture window closed without showing the picture (exit code %s)", process.wait())
        _remove(path)
    return ready


async def show_picture(picture: bytes, title: str, caption: str = "") -> bool:
    """Puts a PNG picture on the user's screen in its own window; False if it couldn't be shown."""
    return await asyncio.to_thread(launch, picture, title, caption)


async def warm_up() -> None:
    """Start a viewer now for a picture ``show_picture`` will show soon, so the window comes up sooner."""
    await asyncio.to_thread(_SPARE.start)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
