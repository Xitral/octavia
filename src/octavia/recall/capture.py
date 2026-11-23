"""Reading what's on screen, on Windows: which window is in front, and the text in it.

``foreground`` is quick (a few Win32 calls) and is what the service polls. ``read_window`` asks UI Automation, the
interface screen readers use, for the visible text of one window: the text of its documents (a web page, a Word
document, a PDF) through their text pattern, limited to what's scrolled into view, and the names of its visible text,
links, list and tree items, tabs and headers. Password fields are never read, and neither is what's typed into any
other text box, except a browser's address bar, for the page's address. No picture is taken.

When an app describes nothing to UI Automation (some Electron apps, PDFs drawn as images, slides shown full
screen), ``ocr_window`` can read the window with Windows' own offline OCR (Windows.Media.Ocr), if its Python
projection is installed: the screenshot lives in memory only for that, and is dropped as soon as the words are read.
OCR reads everything shown, typed text included (password fields show dots), so it's never used on browsers, whose
private windows and banks only UI Automation can tell; the service keeps it to a short list of apps.

All UI Automation work runs on one thread of its own, with COM initialised on it, at background priority, so it never
waits behind the screen tools' thread and never competes with a game for the CPU.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from octavia.recall import privacy

log = logging.getLogger(__name__)

# UIAutomationClient.h
_NAME, _CONTROL_TYPE, _RECT, _IS_PASSWORD, _IS_OFFSCREEN, _VALUE = 30005, 30003, 30001, 30019, 30022, 30045
_AUTOMATION_ID = 30011
_TEXT_PATTERN = 10014
_TYPE_BUTTON, _TYPE_EDIT, _TYPE_LINK, _TYPE_LIST_ITEM, _TYPE_MENU_ITEM = 50000, 50004, 50005, 50007, 50011
_TYPE_TEXT, _TYPE_TAB_ITEM, _TYPE_TREE_ITEM, _TYPE_DATA_ITEM, _TYPE_DOCUMENT = 50020, 50019, 50024, 50029, 50030
_TYPE_HEADER, _TYPE_HEADER_ITEM = 50034, 50035
_TEXT_TYPES = (_TYPE_TEXT, _TYPE_LINK, _TYPE_LIST_ITEM, _TYPE_TREE_ITEM, _TYPE_DATA_ITEM, _TYPE_TAB_ITEM)
_TEXT_TYPES += (_TYPE_HEADER, _TYPE_HEADER_ITEM)
_MARKER_TYPES = (_TYPE_BUTTON, _TYPE_MENU_ITEM, _TYPE_TEXT)
_TREE_SCOPE_SUBTREE = 7
_ELEMENT_MODE_NONE = 0
_CONNECTION_TIMEOUT_MS = 800
_TRANSACTION_TIMEOUT_MS = 1500

TOOLBAR_PX = 180  # a browser's address bar and private badge are this close to the top of its window
MAX_ELEMENTS = 2500
MAX_BROWSER_ELEMENTS = 30_000  # checked for the private badge (it comes early, in the toolbar); a bound on the CPU
MAX_DOCUMENTS = 3
MAX_CHARS = 40_000  # of all the text read from one window in one look

# winbase.h
_THREAD_MODE_BACKGROUND_BEGIN = 0x00010000


@dataclass(frozen=True)
class Foreground:
    hwnd: int
    pid: int
    process: str  # image name, "chrome.exe"
    app: str  # what the app calls itself, "Google Chrome"
    title: str
    frame: tuple[int, int, int, int] | None
    fullscreen: bool
    chromium: bool = False  # built on Chromium (a browser, or an Electron app like Discord): see ``_uia.is_chromium``


@dataclass
class WindowText:
    lines: list[str] = field(default_factory=list)
    url: str = ""
    private: bool = False  # a browser's private-window badge was seen
    complete: bool = False  # every element was checked (for the private badge, the address bar)
    elements: int = 0
    seconds: float = 0.0


# --------------------------------------------------------------------------- foreground (any thread)


@functools.cache
def _user32() -> Any:
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

    user32.GetLastInputInfo.restype = w.BOOL
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetTickCount.restype = w.DWORD
    return user32, kernel32, LASTINPUTINFO


def idle_seconds() -> float:
    """Seconds since the last key press or mouse move anywhere; 0 when it can't be told."""
    if sys.platform != "win32":
        return 0.0
    try:
        user32, kernel32, info_type = _user32()
        info = info_type()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return max(0, (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


# (pid, image name) -> what the app calls itself. The image name is read fresh every time, since Windows reuses a
# dead process's pid: a cached name could let a password manager pass for the app that had its pid before.
_names: dict[tuple[int, str], str] = {}
_names_lock = threading.Lock()


def _process_and_app(api: Any, hwnd: int, pid: int) -> tuple[int, str, str]:
    from octavia.tools.extensions import _uia

    process = _uia._process_name(pid)
    if process.casefold() == "applicationframehost.exe":  # a Store app: the real one is a child window's
        real = _uia._child_pid(api, hwnd, pid)
        if real != pid:
            pid, process = real, _uia._process_name(real)
    with _names_lock:
        app = _names.get((pid, process))
    if app is None:
        app = _uia._app_name(pid) or process.removesuffix(".exe")
        with _names_lock:
            if len(_names) > 500:
                _names.clear()
            _names[(pid, process)] = app
    return pid, process, app


def foreground() -> Foreground | None:
    """The window in front, or None (none, the desktop, a locked PC, or not Windows). Quick."""
    if sys.platform != "win32":
        return None
    from octavia.tools.extensions import _uia
    from octavia.tools.extensions.screen_capture import foreground_frame

    api = _uia._win()
    hwnd = int(api.user32.GetForegroundWindow() or 0)
    if not hwnd:
        return None
    pid = _uia._pid(api, hwnd)
    pid, process, app = _process_and_app(api, hwnd, pid)
    title = _uia._window_text(api, hwnd)
    frame = monitor = None
    try:
        found = foreground_frame()
        if found is not None and found[0] == hwnd:
            frame, monitor = found[1], found[2]
    except Exception:
        log.debug("Couldn't read the foreground window's frame", exc_info=True)
    fullscreen = bool(frame and monitor and _covers(frame, monitor))
    return Foreground(hwnd, pid, process, app, title, frame, fullscreen, _uia.is_chromium(api, hwnd))


def _covers(frame: tuple[int, int, int, int], monitor: tuple[int, int, int, int]) -> bool:
    return frame[0] <= monitor[0] and frame[1] <= monitor[1] and frame[2] >= monitor[2] and frame[3] >= monitor[3]


# --------------------------------------------------------------------------- the capture thread


_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_local = threading.local()


def _init_thread() -> None:
    """COM for UI Automation, and background priority: lower CPU, disk and memory priority than anything the user
    runs, so a game or a video call always goes first."""
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        log.debug("COM initialisation on the screen history thread failed", exc_info=True)
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _THREAD_MODE_BACKGROUND_BEGIN)
    except Exception:
        log.debug("Couldn't lower the screen history thread's priority", exc_info=True)


def thread() -> ThreadPoolExecutor:
    """The one thread all of screen history's UI Automation (and OCR) runs on."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(1, thread_name_prefix="octavia-recall-capture", initializer=_init_thread)
        return _pool


def _client() -> Any:
    """This thread's own IUIAutomation object (COM objects stay on the thread that made them)."""
    client = getattr(_local, "client", None)
    if client is not None:
        return client
    import comtypes.client

    from octavia.tools.extensions import _uia

    uia = _uia._module()
    try:
        client = comtypes.client.CreateObject(uia.CUIAutomation8._reg_clsid_, interface=uia.IUIAutomation2)
        client.ConnectionTimeout = _CONNECTION_TIMEOUT_MS
        client.TransactionTimeout = _TRANSACTION_TIMEOUT_MS
    except Exception:
        client = comtypes.client.CreateObject(uia.CUIAutomation._reg_clsid_, interface=uia.IUIAutomation)
    _local.client = client
    return client


def read_window(hwnd: int, process: str, shallow: bool = False, budget_s: float = 1.5) -> WindowText:
    """The visible text of window ``hwnd`` (see the module's docstring). Blocking; on ``thread()`` only.

    ``shallow`` for apps drawn by Chromium (``_uia.is_chromium``): only the labels of text elements, as one cached
    search like ``find_on_screen``'s, with no values and no document text through the text pattern. The full read
    froze a test Vivaldi for good in 2 of 10 runs; this part alone never did."""
    started = time.perf_counter()
    found = WindowText()
    from octavia.tools.extensions import _uia

    client = _client()
    uia = _uia._module()
    root = client.ElementFromHandle(hwnd)
    browser = privacy.norm_app(process) in privacy.BROWSERS
    try:
        top = root.CurrentBoundingRectangle.top
    except Exception:
        top = 0

    cache = client.CreateCacheRequest()
    for prop in (
        (_NAME, _CONTROL_TYPE, _RECT, _IS_PASSWORD)
        if shallow
        else (
            _NAME,
            _CONTROL_TYPE,
            _RECT,
            _IS_PASSWORD,
            _VALUE,
            _AUTOMATION_ID,
        )
    ):
        cache.AddProperty(prop)
    cache.AutomationElementMode = _ELEMENT_MODE_NONE
    types = _TEXT_TYPES + (_TYPE_EDIT,) + (_MARKER_TYPES if browser else ())
    kinds = client.CreateOrConditionFromArray([client.CreatePropertyCondition(_CONTROL_TYPE, t) for t in types])
    condition = client.CreateAndCondition(client.CreatePropertyCondition(_IS_OFFSCREEN, False), kinds)
    elements = root.FindAllBuildCache(_TREE_SCOPE_SUBTREE, condition, cache)
    total_elements = elements.Length if elements else 0
    # Cached: going through them costs no calls into the app, so a browser's are all checked for its private badge,
    # however long finding them took. Other apps' are read up to MAX_ELEMENTS.
    count = min(total_elements, MAX_BROWSER_ELEMENTS if browser else MAX_ELEMENTS)
    found.elements = count
    names: list[str] = []
    for index in range(count):
        element = elements.GetElement(index)
        try:
            if element.GetCachedPropertyValue(_IS_PASSWORD):
                continue
            kind = element.CachedControlType
            name = element.CachedName or ""
            near_top = element.CachedBoundingRectangle.top - top < TOOLBAR_PX
        except Exception:
            continue
        if browser and near_top and kind in _MARKER_TYPES and privacy.is_private_marker(name):
            found.private = True
            return found  # nothing more is read from a private window
        if kind == _TYPE_EDIT:
            # What's typed in a text box isn't kept; a browser's address bar is read for the page's address.
            if browser and near_top and not found.url and not shallow:  # values aren't read in a shallow read
                value = element.GetCachedPropertyValue(_VALUE)
                if isinstance(value, str):
                    found.url = privacy.clean_url(value)
            continue
        if kind in _TEXT_TYPES and name and len(names) < MAX_ELEMENTS:
            names.append(name)
    found.complete = count == total_elements
    if shallow:
        found.lines = names
        found.seconds = time.perf_counter() - started
        return found

    documents: list[str] = []
    started = time.perf_counter()  # the budget is for the documents' text, the slow part
    try:
        docs = root.FindAll(
            _TREE_SCOPE_SUBTREE,
            client.CreateAndCondition(
                client.CreatePropertyCondition(_CONTROL_TYPE, _TYPE_DOCUMENT),
                client.CreatePropertyCondition(_IS_OFFSCREEN, False),
            ),
        )
        for index in range(min(docs.Length if docs else 0, MAX_DOCUMENTS)):
            if time.perf_counter() - started > budget_s:
                break
            element = docs.GetElement(index)
            if element.CurrentIsPassword:
                continue
            text = _visible_text(element, uia)
            if text:
                documents.append(text)
    except Exception:
        log.debug("Reading the documents of window %s failed", hwnd, exc_info=True)

    total = 0
    lines: list[str] = []
    for chunk in [*documents, *names]:
        for line in chunk.splitlines():
            total += len(line)
            if total > MAX_CHARS:
                break
            lines.append(line)
    found.lines = lines
    found.seconds = time.perf_counter() - started
    return found


READ_ALL_CHARS = 15_000  # of one window in one read_page call: about 4k tokens of her context


def read_all(hwnd: int, process: str, chromium: bool, max_chars: int = READ_ALL_CHARS, start: int = 0) -> WindowText:
    """All the text of window ``hwnd``, what's scrolled out of view included (``read_page``), from character ``start``
    on: the names of its text elements wherever they are, in the app's own order, as cached searches (the read that
    never froze a browser, see ``read_window``). In a browser only the page itself is read (its document), never the
    tabs, side panels or toolbars, and only once its toolbar has been checked for the private-window badge and its
    address read (``url``) for the caller's site rules; nothing at all from a private window. In an app that isn't a
    browser or drawn by Chromium, its documents' whole text too (a PDF reader, a Word document). Password fields and
    what's typed in text boxes are never read. ``complete`` is False when there's more past what was returned.
    Blocking; on ``thread()`` only."""
    started = time.perf_counter()
    found = WindowText()
    from octavia.tools.extensions import _uia

    client = _client()
    uia = _uia._module()
    root = client.ElementFromHandle(hwnd)
    browser = privacy.norm_app(process) in privacy.BROWSERS
    try:
        top = root.CurrentBoundingRectangle.top
    except Exception:
        top = 0

    def cached(element: Any, types: tuple[int, ...], props: tuple[int, ...]) -> Any:
        cache = client.CreateCacheRequest()
        for prop in props:
            cache.AddProperty(prop)
        cache.AutomationElementMode = _ELEMENT_MODE_NONE
        kinds = client.CreateOrConditionFromArray([client.CreatePropertyCondition(_CONTROL_TYPE, t) for t in types])
        return element.FindAllBuildCache(_TREE_SCOPE_SUBTREE, kinds, cache)

    scopes = [root]
    if browser:
        # The toolbar first, all of it: the private badge and the address are in the band at the window's top.
        bar = cached(root, _MARKER_TYPES + (_TYPE_EDIT,), (_NAME, _CONTROL_TYPE, _RECT, _VALUE))
        for index in range(min(bar.Length if bar else 0, MAX_BROWSER_ELEMENTS)):
            element = bar.GetElement(index)
            try:
                kind, name = element.CachedControlType, element.CachedName or ""
                dy = element.CachedBoundingRectangle.top - top
            except Exception:
                continue
            if not 0 <= dy < TOOLBAR_PX:
                continue
            if kind in _MARKER_TYPES and privacy.is_private_marker(name):
                found.private = True
                return found
            if kind == _TYPE_EDIT and not found.url:
                value = element.GetCachedPropertyValue(_VALUE)
                if isinstance(value, str):
                    found.url = privacy.clean_url(value)
        docs = root.FindAll(_TREE_SCOPE_SUBTREE, client.CreatePropertyCondition(_CONTROL_TYPE, _TYPE_DOCUMENT))
        scopes = [docs.GetElement(i) for i in range(min(docs.Length if docs else 0, MAX_DOCUMENTS))]
    lines: list[str] = []
    total = 0
    for scope in scopes:
        elements = cached(scope, _TEXT_TYPES + (_TYPE_EDIT,), (_NAME, _CONTROL_TYPE, _IS_PASSWORD))
        count = min(elements.Length if elements else 0, MAX_BROWSER_ELEMENTS)
        found.elements += count
        for index in range(count):
            element = elements.GetElement(index)
            try:
                if element.GetCachedPropertyValue(_IS_PASSWORD) or element.CachedControlType == _TYPE_EDIT:
                    continue  # a password, or what's typed in a text box
                name = " ".join((element.CachedName or "").split())
            except Exception:
                continue
            if name and not (lines and lines[-1] == name):  # not a link's words again, right inside its list item
                lines.append(name)
                total += len(name) + 1
    if not browser and not chromium:
        try:
            docs = root.FindAll(_TREE_SCOPE_SUBTREE, client.CreatePropertyCondition(_CONTROL_TYPE, _TYPE_DOCUMENT))
            for index in range(min(docs.Length if docs else 0, MAX_DOCUMENTS)):
                element = docs.GetElement(index)
                if element.CurrentIsPassword or element.CurrentFrameworkId in ("Chrome", "Gecko"):
                    continue  # a web view inside the app: its text pattern is the read that froze a browser
                pattern = element.GetCurrentPattern(_TEXT_PATTERN)
                if not pattern:
                    continue
                text = pattern.QueryInterface(uia.IUIAutomationTextPattern).DocumentRange.GetText(start + max_chars + 1)
                lines.extend(line for line in (text or "").splitlines() if line.strip())
        except Exception:
            log.debug("Reading the documents of window %s failed", hwnd, exc_info=True)
    text = "\n".join(privacy.clean_lines(lines))
    found.lines = text[start : start + max_chars].splitlines()
    found.complete = len(text) <= start + max_chars
    found.seconds = time.perf_counter() - started
    return found


def _visible_text(element: Any, uia: Any) -> str:
    """The text of a document that's scrolled into view (all of it when the app can't say what's visible)."""
    try:
        pattern = element.GetCurrentPattern(_TEXT_PATTERN)
        if not pattern:
            return ""
        text_pattern = pattern.QueryInterface(uia.IUIAutomationTextPattern)
    except Exception:
        return ""
    parts: list[str] = []
    try:
        ranges = text_pattern.GetVisibleRanges()
        for index in range(ranges.Length if ranges else 0):
            parts.append(ranges.GetElement(index).GetText(MAX_CHARS))
    except Exception:
        parts = []
    if not any(p.strip() for p in parts):
        try:
            parts = [text_pattern.DocumentRange.GetText(MAX_CHARS)]
        except Exception:
            return ""
    return "\n".join(parts)


# --------------------------------------------------------------------------- OCR (optional)


@functools.cache
def ocr_available() -> bool:
    """Whether Windows' offline OCR can be used from Python here (its winrt projection is installed)."""
    if sys.platform != "win32":
        return False
    try:
        import winrt.windows.graphics.imaging  # noqa: F401
        import winrt.windows.media.ocr  # noqa: F401
        import winrt.windows.storage.streams  # noqa: F401
    except Exception:
        return False
    return True


MAX_OCR_SIDE = 4000  # Windows' OCR refuses bigger pictures


def ocr_window(hwnd: int) -> list[str]:
    """The lines Windows' OCR reads in window ``hwnd``. Only that window's own pixels are read (PrintWindow), never
    whatever floats over it, such as a password manager's pop-up, and a window that keeps itself out of screenshots
    comes back blank. The picture is in memory only and is dropped before this returns. Blocking; on ``thread()``
    only. [] when OCR isn't available or the window can't be read."""
    if not ocr_available():
        return []
    import asyncio

    from octavia.tools.extensions.screen_capture import per_monitor_dpi

    with per_monitor_dpi():
        grabbed = _print_window(hwnd)
    if grabbed is None:
        return []
    data, width, height = grabbed
    try:
        return asyncio.run(_ocr(data, width, height))
    finally:
        del data


def _print_window(hwnd: int) -> tuple[bytes, int, int] | None:
    """Window ``hwnd``'s own pixels as top-down BGRA bytes, or None when there are none (minimised, too big, all
    black)."""
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32")
    gdi32 = ctypes.WinDLL("gdi32")
    user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user32.GetWindowDC.argtypes = [w.HWND]
    user32.GetWindowDC.restype = w.HDC
    user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
    user32.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
    gdi32.CreateCompatibleDC.argtypes = [w.HDC]
    gdi32.CreateCompatibleDC.restype = w.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = w.HBITMAP
    gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
    gdi32.SelectObject.restype = w.HGDIOBJ
    gdi32.GetBitmapBits.argtypes = [w.HBITMAP, ctypes.c_long, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [w.HDC]
    if user32.IsIconic(hwnd):
        return None
    rect = w.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 32 or height < 32 or width > MAX_OCR_SIDE or height > MAX_OCR_SIDE:
        return None
    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        return None
    memory = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    try:
        if not memory or not bitmap:
            return None
        previous = gdi32.SelectObject(memory, bitmap)
        printed = user32.PrintWindow(hwnd, memory, _PW_RENDERFULLCONTENT)
        gdi32.SelectObject(memory, previous)
        if not printed:
            return None
        buffer = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetBitmapBits(bitmap, len(buffer), buffer) != len(buffer):
            return None
        data = buffer.raw
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, window_dc)
    import numpy as np

    pixels = np.frombuffer(data, np.uint8).reshape(height, width, 4).copy()
    del data, buffer
    if not pixels[:, :, :3].any():
        return None  # a window that keeps itself out of screenshots
    pixels[:, :, 3] = 255  # GDI leaves alpha at 0, which the OCR would read as transparent
    return pixels.tobytes(), width, height


_PW_RENDERFULLCONTENT = 2  # also the content of windows drawn with DirectComposition (browsers, Electron)


async def _ocr(data: bytes, width: int, height: int) -> list[str]:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        return []
    writer = DataWriter()
    writer.write_bytes(data)
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, width, height)
    result = await engine.recognize_async(bitmap)
    return [line.text for line in result.lines]
