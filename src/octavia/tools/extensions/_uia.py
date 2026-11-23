"""Windows UI Automation: find labelled things on the screen (buttons, links, menu items, tabs, text) by their text.

Apps describe their controls to screen readers through UI Automation, each with its exact rectangle, so asking it
for a label is quicker and more precise than a screenshot, and no picture leaves the computer. Pictures, games,
videos and canvases have no such labels; for those the model looks at a screenshot instead.

comtypes (installed with pycaw) builds its wrapper for UIAutomationCore.dll on first use, which takes a moment, so
that and all the COM work run on one dedicated thread with COM initialised on it, never on the event loop. Each
search makes that thread per-monitor DPI aware, so every rectangle is in physical desktop pixels, the same ones
screenshots and the avatar use. This module only reads: nothing here clicks or types.

The avatar's own windows (the processes in ``ignore_pids``) are left out: they're never searched, and a match
they happen to float over isn't "behind another window" unless a real window is in front of it too.

``inspect_point`` and ``inspect_focus`` describe what a click at a spot would land on, or what typing would go into
(a ``Target``), for the computer-control risk check: the element, a few of its ancestors, the dialog or form around
it, and its top-level window and process. The window part comes from Windows itself, so it's known even for apps
that describe nothing to UI Automation (games, canvases) or when UI Automation is too slow.

Apps that stop answering: a UI Automation call into a hung app can block for good, so a window whose app doesn't
handle a message within ``ANSWER_MS`` isn't read at all (``_answers``), every call has a time limit (``run``), and a
call stuck past ``STUCK_S`` is left behind on its own thread so the ones after it still run (``_uia_thread``).

Chromium (browsers, and Electron apps like Discord, see ``is_chromium``): screen history's deep read of a window
(every text element with its value, then each page's whole text through the text pattern) froze a test Vivaldi for
good in 2 of 10 runs (2026-10-09), its UI thread spinning a core, once on the very first read after it opened.
Killing the reading process didn't free it, so no time limit on this side can undo it. The reads here (searching by
label, the element at a spot, the focus with its ancestors and value) never froze it in 27 runs, on fresh launches
and on loaded pages, and neither did either half of the deep read alone. Keep reads of Chromium windows to those:
no page text through the text pattern combined with a sweep of values.

Not loaded as a tool module (the name starts with ``_``); ``screen_pointing`` and ``computer_control`` use it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import queue
import re
import sys
import threading
import time
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import Executor, Future
from dataclasses import dataclass, replace
from typing import Any

from octavia.tools.extensions.screen_capture import Rect, monitor_rects, per_monitor_dpi

log = logging.getLogger(__name__)

MAX_MATCHES = 8  # what find_on_screen shows (and numbers for point_at_screen)
SEARCH_BUDGET_S = 3.0  # the foreground window is always searched; the others while time is left
MAX_WINDOWS = 40
MAX_PER_WINDOW = 200  # a label as short as "ok" can match thousands of things in a busy app
NAME_CHARS = 60
TITLE_CHARS = 50
INSPECT_TIMEOUT_S = 4.0  # then the risk check judges by the window alone
FIND_GRACE_S = 4.0  # past its budget, a search is held up by an app that doesn't answer: given up on
STUCK_S = 15.0  # a UI Automation call running this long is left behind on its thread (``_uia_thread``); above
# every caller's own time limit, so a call that's merely slow is never counted
MAX_LEFT_BEHIND = 4
ANSWER_MS = 500  # a window whose app doesn't handle a message this quickly isn't read through UI Automation
MAX_ANCESTORS = 30  # how far up from an element to look for the dialog or form around it
CHAIN = 4  # the element and its nearest ancestors: a click on an icon means the button it's in
CONTAINER_ITEMS = 80  # texts and buttons read from the dialog, form or menu around an element
CONTAINER_TEXT_CHARS = 300  # of each of those
CONTEXT_CHARS = 1200  # of them all together
WINDOW_TITLE_CHARS = 200  # a browser's title ends with the site's name ("... - Gmail - Google Chrome")

# UIAutomationClient.h
_UIA_BOUNDING_RECTANGLE = 30001
_UIA_PROCESS_ID = 30002
_UIA_CONTROL_TYPE = 30003
_UIA_LOCALIZED_CONTROL_TYPE = 30004
_UIA_NAME = 30005
_UIA_AUTOMATION_ID = 30011
_UIA_CLASS_NAME = 30012
_UIA_IS_PASSWORD = 30019
_UIA_ITEM_TYPE = 30021
_UIA_IS_OFFSCREEN = 30022
_UIA_FRAMEWORK_ID = 30024
_UIA_IS_VALUE_PATTERN_AVAILABLE = 30043
_UIA_VALUE_VALUE = 30045
_UIA_VALUE_IS_READ_ONLY = 30046
_UIA_IS_DIALOG = 30174  # Windows 10 1809 and later
_UIA_IS_EXPAND_COLLAPSE_PATTERN_AVAILABLE = 30028
_UIA_IS_SELECTION_ITEM_PATTERN_AVAILABLE = 30036
_UIA_IS_TOGGLE_PATTERN_AVAILABLE = 30041
_UIA_EXPAND_COLLAPSE_STATE = 30070
_UIA_SELECTION_ITEM_IS_SELECTED = 30079
_UIA_TOGGLE_STATE = 30086
_TYPE_BUTTON, _TYPE_LINK, _TYPE_MENU_ITEM, _TYPE_TEXT, _TYPE_SPLIT_BUTTON = 50000, 50005, 50011, 50020, 50031
_TREE_SCOPE_CHILDREN, _TREE_SCOPE_DESCENDANTS = 2, 4
_TREE_SCOPE_SUBTREE = 7  # the window itself and everything in it
_IGNORE_CASE, _MATCH_SUBSTRING = 1, 2  # PropertyConditionFlags (substring matching: Windows 10 1809 and later)
_ELEMENT_MODE_NONE = 0  # cached properties only, no live reference back into the app: cheaper, and read-only
_CONNECTION_TIMEOUT_MS = 1000  # so a hung app costs a second or two, not the whole search
_TRANSACTION_TIMEOUT_MS = 2500

# winuser.h, dwmapi.h
_GA_ROOT = 2
_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x20
_WS_EX_LAYERED = 0x80000
_CLICK_THROUGH = _WS_EX_LAYERED | _WS_EX_TRANSPARENT  # what Electron's setIgnoreMouseEvents(true) sets
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_CHROMIUM_CLASS = "Chrome_WidgetWin_"
_WM_NULL = 0
_SMTO_ABORTIFHUNG = 0x2
_ERROR_TIMEOUT = 1460  # SendMessageTimeout's error for a window that didn't answer (or is hung)

CONTROL_TYPES = {
    50000: "button",
    50001: "calendar",
    50002: "check box",
    50003: "drop-down",
    50004: "text box",
    50005: "link",
    50006: "image",
    50007: "list item",
    50008: "list",
    50009: "menu",
    50010: "menu bar",
    50011: "menu item",
    50012: "progress bar",
    50013: "radio button",
    50014: "scroll bar",
    50015: "slider",
    50016: "spinner",
    50017: "status bar",
    50018: "tab list",
    50019: "tab",
    50020: "text",
    50021: "toolbar",
    50022: "tooltip",
    50023: "tree",
    50024: "tree item",
    50025: "item",
    50026: "group",
    50027: "handle",
    50028: "grid",
    50029: "grid item",
    50030: "document",
    50031: "split button",
    50032: "window",
    50033: "pane",
    50034: "header",
    50035: "header item",
    50036: "table",
    50037: "title bar",
    50038: "separator",
    50039: "zoom view",
    50040: "app bar",
}
# Things a user means when they ask for a label come first: controls, then plain text and pictures, then containers.
_CONTROLS = {
    "button",
    "check box",
    "drop-down",
    "text box",
    "link",
    "list item",
    "menu item",
    "radio button",
    "slider",
    "spinner",
    "tab",
    "tree item",
    "grid item",
    "split button",
    "header item",
}
_CONTENT = {"text", "image", "item", "header", "tooltip", "calendar"}
# The HTML classes of web code editors' text surfaces (a browser gives an element's class attribute as its class
# name). CodeMirror 6 (Snowflake's worksheets, Jupyter) types into a contenteditable that browsers may not report as
# the keyboard focus: that's the page itself, though typing goes into the editor clicked.
_CODE_EDITOR = re.compile(r"(?<![\w-])(?:cm-editor|cm-content|cm-line|CodeMirror|monaco-editor|ace_editor)(?![\w])")


class UIAUnavailable(RuntimeError):
    """UI Automation can't be used here (not Windows, or its COM classes failed to load)."""


@dataclass(frozen=True)
class Match:
    """Something on screen whose label contains the searched text."""

    name: str  # its label, tidied and trimmed
    kind: str  # its control type in plain words ("button", "menu item")
    window: str  # the title of the top-level window it's in ("" when that has none)
    rect: Rect  # physical desktop pixels
    foreground: bool = False  # in the window the user is working in
    covered: bool = False  # its middle is hidden behind another window
    where: str = ""  # in words: "at the top right of the left monitor"
    order: tuple[int, int] = (0, 0)  # (window in front-to-back order, position in that window), for ranking

    @property
    def centre(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


@dataclass(frozen=True)
class Window:
    """A top-level window that can be seen, as the covered test needs it."""

    hwnd: int
    frame: Rect  # its visible frame, physical desktop pixels
    pid: int  # the process it belongs to
    click_through: bool = False  # layered and transparent: the mouse (and WindowFromPoint) goes through it


@dataclass(frozen=True)
class Target:
    """What a click at a spot would land on, or what typing would go into, as the computer-control risk check needs
    it. The window part comes from Windows and is there whenever a window is; the element part only when the app
    describes itself to UI Automation (``described``)."""

    # The element
    name: str = ""  # its label, tidied
    kind: str = ""  # its control type in plain words; "" when UI Automation couldn't describe it
    automation_id: str = ""
    rect: Rect | None = None  # physical desktop pixels
    pid: int = 0  # its process: the app itself, also for Store apps (whose frame window another process owns)
    process: str = ""  # that process's image name, e.g. "Discord.exe"
    password: bool = False  # a password box
    editable: bool = False  # a place to type text: a text box or an editable document
    web_page: bool = False  # inside web content (a document), in a browser or an app built on one
    page_root: bool = False  # a web page's document itself: typing there can trigger the site's own shortcuts
    code_editor: bool = False  # in a web page's code editor (CodeMirror, Monaco, Ace), by its HTML classes
    item_type: str = ""  # what kind of file an item is, as File Explorer says it ("Application", "Text Document")
    chain: tuple[tuple[str, str], ...] = ()  # (kind, name) of the element and its nearest ancestors, nearest first
    caption: bool = False  # a button of the window's own title bar (close, minimise, maximise)
    # The dialog, form or menu it's in: "dialog", "form", "menu", or "" when it isn't in one
    container: str = ""
    container_text: str = ""  # that one's title and texts
    actions: tuple[tuple[str, str], ...] = ()  # (kind, name) of its buttons, links and menu items
    # The top-level window
    hwnd: int = 0
    window: str = ""  # its title
    window_class: str = ""
    window_process: str = ""
    secondary: bool = False  # a dialog, or a window another one owns, rather than an app's main window
    app: str = ""  # what the app calls itself (its file description), e.g. "Google Chrome"
    # UI Automation didn't answer in time, so this is the window alone and what's in it is unknown (not an app that
    # merely describes nothing, like a game): the risk check asks rather than guessing it's harmless.
    timed_out: bool = False
    # What a click on it may have switched: "on", "off" or "mixed" (a check box or toggle), "expanded" or "collapsed"
    # (a drop-down, a tree item), "selected" (a tab, a list's option), as far as the app says; "" when it says none
    state: str = ""

    @property
    def described(self) -> bool:
        return bool(self.kind)

    def same_thing(self, other: Target) -> bool:
        """Whether ``other`` is (still) the same thing: the same label, control type and app, or, when neither was
        described, the same window and process. Web pages re-render a link's label cut short ("Decision…") or in
        full between two looks, so a label that's the other one cut short counts as the same."""
        if self.described or other.described:
            return (self.kind, self.pid) == (other.kind, other.pid) and _same_label(self, other)
        return (self.hwnd, self.pid) == (other.hwnd, other.pid)


_ELLIPSIS = ("…", "...")
_MIN_STEM = 3


def _same_label(a: Target, b: Target) -> bool:
    """Whether two labels name the same thing: equal, one cut short with an ellipsis where the other goes on, or
    one the start of the other on an element in the same place."""
    if a.name == b.name:
        return True
    x, y = " ".join(a.name.split()).casefold(), " ".join(b.name.split()).casefold()
    if x == y:
        return True
    short, full = (x, y) if len(x) <= len(y) else (y, x)
    stem = short
    for mark in _ELLIPSIS:
        if stem.endswith(mark):
            stem = stem[: -len(mark)].rstrip()
            break
    if len(stem) < _MIN_STEM or not full.startswith(stem):
        return False
    if stem != short:
        return True
    return a.rect is not None and b.rect is not None and _same_spot(a.rect, b.rect)


# --------------------------------------------------------------------------- pure helpers


def control_type_name(type_id: int) -> str:
    return CONTROL_TYPES.get(type_id, "item")


def tidy(text: str | None, limit: int) -> str:
    """One line of at most ``limit`` characters: labels can hold newlines, runs of spaces or a whole paragraph."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _kind_rank(kind: str) -> int:
    return 0 if kind in _CONTROLS else 1 if kind in _CONTENT else 2


def _same_spot(a: Rect, b: Rect) -> bool:
    """Whether two rectangles are mostly the same place (one inside the other, or nearly equal)."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return False
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return width * height >= 0.6 * smaller


def rank(matches: Sequence[Match], text: str) -> list[Match]:
    """Best first, without repeats.

    Visible before hidden behind another window; the exact label (ignoring case) before labels that merely contain
    the text; the window the user is working in before the others, which follow front to back; controls before
    plain text before containers; shorter labels first; then in the app's own order. An app often reports one
    thing several times (a link and the text inside it, a tab and its group) with the same label in the same
    place: only the best of those is kept.
    """
    wanted = " ".join(text.split()).casefold()

    def key(m: Match) -> tuple:
        exact = m.name.casefold() == wanted
        return (m.covered, not exact, not m.foreground, m.order[0], _kind_rank(m.kind), len(m.name), m.order[1])

    kept: list[Match] = []
    for match in sorted(matches, key=key):
        label = match.name.casefold()
        if any(k.name.casefold() == label and _same_spot(k.rect, match.rect) for k in kept):
            continue
        kept.append(match)
    return kept


def monitor_names(monitors: Sequence[Rect]) -> list[str]:
    """What to call each monitor, in the given order: "the screen" when there's one, else by where it sits
    ("the left monitor", "the right monitor"; "the top monitor" for a stack; "monitor 4 from the left" past three)."""
    if len(monitors) <= 1:
        return ["the screen"] * len(monitors)
    xs = [(m[0] + m[2]) / 2 for m in monitors]
    ys = [(m[1] + m[3]) / 2 for m in monitors]
    side_by_side = max(xs) - min(xs) >= max(ys) - min(ys)
    along = xs if side_by_side else ys
    order = sorted(range(len(monitors)), key=lambda i: along[i])
    words = {2: ("left", "right"), 3: ("left", "middle", "right")}.get(len(monitors))
    if words and not side_by_side:
        words = ("top", "bottom") if len(monitors) == 2 else ("top", "middle", "bottom")
    names = [""] * len(monitors)
    for position, index in enumerate(order):
        if words:
            names[index] = f"the {words[position]} monitor"
        else:
            names[index] = f"monitor {position + 1} from the {'left' if side_by_side else 'top'}"
    return names


def is_covered(
    point: tuple[int, int],
    hwnd: int,
    hit: int | None,
    stack: Sequence[Window],
    ignore_pids: Collection[int],
    pid_of: Callable[[int], int],
) -> bool:
    """Whether ``point`` of window ``hwnd`` is hidden behind another window.

    ``hit`` is the top-level window under the point (``WindowFromPoint``'s root), ``stack`` every window that can
    be seen, front to back. When the hit is one of the avatar's windows (a process in ``ignore_pids``), look past
    her: covered only if a window in front of ``hwnd`` holds the point, not counting her own windows or ones the
    mouse goes through. Her window stops being click-through while the mouse is on her, and then Windows reports
    it for any point inside its rectangle, however much of it is empty.
    """
    if hit == hwnd:
        return False
    if not hit or pid_of(hit) not in ignore_pids:
        return True
    x, y = point
    for window in stack:
        if window.hwnd == hwnd:
            return False
        if window.pid in ignore_pids or window.click_through:
            continue
        left, top, right, bottom = window.frame
        if left <= x < right and top <= y < bottom:
            return True
    return False


def describe_where(point: tuple[int, int], monitors: Sequence[Rect]) -> str:
    """Where a desktop point is, in a few spoken words: "at the top right of the left monitor", "in the middle of
    the screen". "" when the monitors aren't known."""
    if not monitors:
        return ""
    x, y = point

    def distance(m: Rect) -> float:
        dx = max(m[0] - x, 0, x - (m[2] - 1))
        dy = max(m[1] - y, 0, y - (m[3] - 1))
        return dx * dx + dy * dy

    index = min(range(len(monitors)), key=lambda i: distance(monitors[i]))  # the one it's on, else the nearest
    left, top, right, bottom = monitors[index]
    across = (x - left) / max(right - left, 1)
    down = (y - top) / max(bottom - top, 1)
    horizontal = "left" if across < 1 / 3 else "right" if across >= 2 / 3 else ""
    vertical = "top" if down < 1 / 3 else "bottom" if down >= 2 / 3 else ""
    name = monitor_names(monitors)[index]
    if vertical and horizontal:
        return f"at the {vertical} {horizontal} of {name}"
    if vertical:
        return f"at the {vertical} of {name}"
    if horizontal:
        return f"on the {horizontal} side of {name}"
    return f"in the middle of {name}"


# --------------------------------------------------------------------------- UI Automation (Windows)


@functools.cache
def _win() -> Any:
    """Typed Win32 entry points (private ``WinDLL`` instances, as in screen_capture)."""
    import ctypes
    from ctypes import POINTER, c_long, c_void_p
    from ctypes import wintypes as w
    from types import SimpleNamespace

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def sig(fn: Any, restype: Any, *argtypes: Any) -> None:
        fn.restype, fn.argtypes = restype, list(argtypes)

    sig(user32.GetForegroundWindow, w.HWND)
    sig(user32.GetAncestor, w.HWND, w.HWND, w.UINT)
    sig(user32.EnumWindows, w.BOOL, enum_proc, w.LPARAM)
    sig(user32.IsWindowVisible, w.BOOL, w.HWND)
    sig(user32.IsIconic, w.BOOL, w.HWND)
    sig(user32.GetWindowRect, w.BOOL, w.HWND, POINTER(w.RECT))
    sig(user32.WindowFromPoint, w.HWND, w.POINT)
    sig(user32.GetWindowTextW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
    sig(user32.GetClassNameW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
    sig(user32.GetWindow, w.HWND, w.HWND, w.UINT)
    sig(user32.EnumChildWindows, w.BOOL, w.HWND, enum_proc, w.LPARAM)
    sig(user32.GetWindowThreadProcessId, w.DWORD, w.HWND, POINTER(w.DWORD))
    sig(user32.IsHungAppWindow, w.BOOL, w.HWND)
    sig(
        user32.SendMessageTimeoutW,
        w.LPARAM,
        w.HWND,
        w.UINT,
        w.WPARAM,
        w.LPARAM,
        w.UINT,
        w.UINT,
        POINTER(ctypes.c_size_t),
    )
    # 64-bit Windows exports GetWindowLongPtrW; on 32-bit it's a macro for GetWindowLongW.
    get_long = getattr(user32, "GetWindowLongPtrW", None)
    if get_long is not None:
        sig(get_long, ctypes.c_ssize_t, w.HWND, ctypes.c_int)
    else:
        get_long = user32.GetWindowLongW
        sig(get_long, c_long, w.HWND, ctypes.c_int)
    sig(dwmapi.DwmGetWindowAttribute, c_long, w.HWND, w.DWORD, c_void_p, w.DWORD)
    return SimpleNamespace(
        ctypes=ctypes, w=w, user32=user32, dwmapi=dwmapi, enum_proc=enum_proc, get_window_long=get_long
    )


@functools.cache
def _module() -> Any:
    """comtypes' wrapper for UIAutomationCore.dll, generated on the first call (slow once, then cached on disk and
    here). Only ever called on the UI Automation thread."""
    import comtypes.client

    return comtypes.client.GetModule("UIAutomationCore.dll")


_local = threading.local()  # each UI Automation thread's own IUIAutomation object (COM objects stay on their thread)


def _automation() -> Any:
    client = getattr(_local, "client", None)
    if client is not None:
        return client
    try:
        import comtypes.client

        uia = _module()
        try:
            # The Windows 8+ class lets a search give up on an app that has stopped responding.
            client = comtypes.client.CreateObject(uia.CUIAutomation8._reg_clsid_, interface=uia.IUIAutomation2)
            client.ConnectionTimeout = _CONNECTION_TIMEOUT_MS
            client.TransactionTimeout = _TRANSACTION_TIMEOUT_MS
        except Exception:
            log.debug("CUIAutomation8 unavailable; using CUIAutomation", exc_info=True)
            client = comtypes.client.CreateObject(uia.CUIAutomation._reg_clsid_, interface=uia.IUIAutomation)
    except Exception as exc:
        raise UIAUnavailable(f"UI Automation couldn't be loaded ({type(exc).__name__})") from exc
    _local.client = client
    return client


def _frame(api: Any, hwnd: int) -> Rect | None:
    r = api.w.RECT()
    size = api.ctypes.sizeof(r)
    if api.dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, api.ctypes.byref(r), size) == 0:
        return r.left, r.top, r.right, r.bottom
    if api.user32.GetWindowRect(hwnd, api.ctypes.byref(r)):
        return r.left, r.top, r.right, r.bottom
    return None


def _cloaked(api: Any, hwnd: int) -> bool:
    """Windows can be "visible" yet not drawn: apps on another virtual desktop, suspended Store apps."""
    value = api.w.DWORD()
    result = api.dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, api.ctypes.byref(value), 4)
    return result == 0 and value.value != 0


def _answers(api: Any, hwnd: int, timeout_ms: int = ANSWER_MS) -> bool:
    """Whether the app of window ``hwnd`` is handling its messages. A UI Automation call into one that isn't can
    block for a long time (for good, in the worst case), so such a window is left alone. True when it can't be
    told, or when the message can't be sent for another reason (an app running as administrator refuses it)."""
    try:
        if api.user32.IsHungAppWindow(hwnd):
            return False
        result = api.ctypes.c_size_t()
        if api.user32.SendMessageTimeoutW(
            hwnd, _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG, timeout_ms, api.ctypes.byref(result)
        ):
            return True
        return api.ctypes.get_last_error() != _ERROR_TIMEOUT
    except Exception:
        return True


def _overlaps(rect: Rect, monitors: Sequence[Rect]) -> bool:
    return any(rect[0] < m[2] and m[0] < rect[2] and rect[1] < m[3] and m[1] < rect[3] for m in monitors)


def _pid(api: Any, hwnd: int) -> int:
    pid = api.w.DWORD()
    api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid))
    return pid.value


def _top_level_windows(api: Any, monitors: Sequence[Rect]) -> list[Window]:
    """Windows that can be seen, front to back (the order Windows lists them in)."""
    found: list[Window] = []

    def collect(hwnd: int, data: Any) -> bool:
        if hwnd and api.user32.IsWindowVisible(hwnd) and not api.user32.IsIconic(hwnd) and not _cloaked(api, hwnd):
            frame = _frame(api, hwnd)
            if frame and frame[2] - frame[0] >= 16 and frame[3] - frame[1] >= 16 and _overlaps(frame, monitors):
                style = api.get_window_long(hwnd, _GWL_EXSTYLE)
                found.append(Window(hwnd, frame, _pid(api, hwnd), style & _CLICK_THROUGH == _CLICK_THROUGH))
        return True

    api.user32.EnumWindows(api.enum_proc(collect), 0)
    return found


def _title(api: Any, hwnd: int, element: Any) -> str:
    try:
        name = element.CurrentName  # UI Automation names the taskbar "Taskbar", which has no window title
    except Exception:
        name = None
    if not name:
        buffer = api.ctypes.create_unicode_buffer(256)
        api.user32.GetWindowTextW(hwnd, buffer, 256)
        name = buffer.value
    return tidy(name, TITLE_CHARS)


def search(text: str, ignore_pids: Collection[int] = frozenset(), budget_s: float = SEARCH_BUDGET_S) -> list[Match]:
    """Everything visible whose label contains ``text`` (ignoring case), unranked, outside the windows of the
    processes in ``ignore_pids``. Blocking; only on the UI Automation thread (``find``)."""
    api = _win()
    with per_monitor_dpi():
        client = _automation()  # the first call also generates the wrapper
        started = time.monotonic()
        monitors = monitor_rects()
        condition = client.CreateAndCondition(
            client.CreatePropertyConditionEx(_UIA_NAME, text, _IGNORE_CASE | _MATCH_SUBSTRING),
            client.CreatePropertyCondition(_UIA_IS_OFFSCREEN, False),
        )
        cache = client.CreateCacheRequest()
        for prop in (_UIA_NAME, _UIA_CONTROL_TYPE, _UIA_BOUNDING_RECTANGLE):
            cache.AddProperty(prop)
        cache.AutomationElementMode = _ELEMENT_MODE_NONE
        foreground = api.user32.GetForegroundWindow() or 0
        if foreground and _pid(api, foreground) in ignore_pids:
            foreground = 0  # her own window (it shouldn't take the focus): search the others front to back
        # The true front-to-back order, for the covered test: before the foreground moves up and the list is cut.
        stack = _top_level_windows(api, monitors)
        windows = [w.hwnd for w in stack if w.pid not in ignore_pids]
        if foreground:
            windows = [foreground, *(h for h in windows if h != foreground)]
        found: list[Match] = []
        for order, hwnd in enumerate(windows[:MAX_WINDOWS]):
            if order and time.monotonic() - started > budget_s:
                log.debug("UI Automation search for %r stopped after %d windows (time's up)", text, order)
                break
            if not _answers(api, hwnd):
                log.info("Not searching window %s: its app isn't responding", hwnd)
                continue
            try:
                root = client.ElementFromHandle(hwnd)
                title = _title(api, hwnd, root)
                elements = root.FindAllBuildCache(_TREE_SCOPE_SUBTREE, condition, cache)
                count = min(elements.Length, MAX_PER_WINDOW) if elements else 0
                for index in range(count):
                    element = elements.GetElement(index)
                    r = element.CachedBoundingRectangle
                    rect = (r.left, r.top, r.right, r.bottom)
                    if rect[2] - rect[0] < 1 or rect[3] - rect[1] < 1:
                        continue  # no size: nothing to point at
                    match = Match(
                        name=tidy(element.CachedName, NAME_CHARS),
                        kind=control_type_name(element.CachedControlType),
                        window=title,
                        rect=rect,
                        foreground=hwnd == foreground,
                        order=(order, index),
                    )
                    x, y = match.centre
                    if not match.name or not _overlaps((x, y, x + 1, y + 1), monitors):
                        continue  # scrolled out of sight without saying so
                    hit = api.user32.GetAncestor(api.user32.WindowFromPoint(api.w.POINT(x, y)), _GA_ROOT)
                    covered = is_covered((x, y), hwnd, hit, stack, ignore_pids, lambda h: _pid(api, h))
                    found.append(replace(match, covered=covered, where=describe_where((x, y), monitors)))
            except Exception:  # the window closed meanwhile, or its app doesn't answer
                log.debug("UI Automation search in window %s failed", hwnd, exc_info=True)
        log.debug("UI Automation search for %r: %d found in %.2fs", text, len(found), time.monotonic() - started)
        return found


# --------------------------------------------------------------------------- what a click or typing would reach


def _cached(element: Any, prop: int) -> Any:
    """A cached property of ``element``, or None when the app (or this Windows) doesn't have it."""
    try:
        return element.GetCachedPropertyValue(prop)
    except Exception:
        return None


def _cached_text(element: Any, prop: int) -> str:
    """A cached text property, tidied; "" when it's missing or not text."""
    value = _cached(element, prop)
    return tidy(value, NAME_CHARS) if isinstance(value, str) else ""


def _class_name(api: Any, hwnd: int) -> str:
    buffer = api.ctypes.create_unicode_buffer(256)
    api.user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def is_chromium(api: Any, hwnd: int) -> bool:
    """Whether top-level window ``hwnd`` is drawn by Chromium: Chrome, Edge, Vivaldi, Brave, Opera, and Electron apps
    (Discord, Slack, VS Code, Spotify). Deep reads of these (a page's text through the text pattern, every text box's
    value) can freeze them for good: see the module's docstring. Apps built on WebView2 (new Teams and Outlook,
    WhatsApp) have a window of their own around Chromium's, so a child window of Chromium's class counts too."""
    if _class_name(api, hwnd).startswith(_CHROMIUM_CLASS):
        return True
    found = [False]

    def check(child: int, data: Any) -> bool:
        if _class_name(api, child).startswith(_CHROMIUM_CLASS):
            found[0] = True
            return False
        return True

    api.user32.EnumChildWindows(hwnd, api.enum_proc(check), 0)
    return found[0]


def _window_text(api: Any, hwnd: int) -> str:
    buffer = api.ctypes.create_unicode_buffer(512)
    api.user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def _process_name(pid: int) -> str:
    """The image name of process ``pid`` ("Discord.exe"); "" when it can't be read."""
    if pid <= 0:
        return ""
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:
        return ""


def _app_name(pid: int) -> str:
    """What an app calls itself: its executable's file description ("Google Chrome"); "" when unreadable."""
    if pid <= 0:
        return ""
    try:
        import psutil

        from octavia.platform.windows import exe_description

        return exe_description(psutil.Process(pid).exe()) or ""
    except Exception:
        return ""


def _child_pid(api: Any, hwnd: int, pid: int) -> int:
    """The app inside a Store app's frame window: ApplicationFrameHost.exe owns the frame, the app a child in it."""
    found = [pid]

    def check(child: int, data: Any) -> bool:
        other = _pid(api, child)
        if other != pid:
            found[0] = other
            return False
        return True

    api.user32.EnumChildWindows(hwnd, api.enum_proc(check), 0)
    return found[0]


def window_target(api: Any, hwnd: int) -> Target:
    """The window part of a ``Target``: title, class and process of top-level window ``hwnd``, and whether it's a
    dialog or a window another one owns. Blocking, but quick; needs no UI Automation."""
    if not hwnd:
        return Target()
    pid = _pid(api, hwnd)
    process = _process_name(pid)
    if process.casefold() == "applicationframehost.exe":
        real = _child_pid(api, hwnd, pid)
        if real != pid:
            pid, process = real, _process_name(real)
    window_class = _class_name(api, hwnd)
    owned = bool(api.user32.GetWindow(hwnd, _GW_OWNER))
    return Target(
        pid=pid,
        process=process,
        hwnd=hwnd,
        window=tidy(_window_text(api, hwnd), WINDOW_TITLE_CHARS),
        window_class=window_class,
        window_process=process,
        secondary=owned or window_class == "#32770" or window_class.startswith("MsiDialog"),
        app=_app_name(pid),
    )


def _window_at(api: Any, x: int, y: int, ignore_pids: Collection[int]) -> int:
    """The top-level window a click at (x, y) reaches, looking past the avatar's: her window answers for all of its
    rectangle while the mouse is on her, and then the first real window under the spot is the one that counts."""
    hit = api.user32.GetAncestor(api.user32.WindowFromPoint(api.w.POINT(x, y)), _GA_ROOT) or 0
    if not hit or _pid(api, hit) not in ignore_pids:
        return hit
    for window in _top_level_windows(api, monitor_rects()):
        if window.pid in ignore_pids or window.click_through:
            continue
        left, top, right, bottom = window.frame
        if left <= x < right and top <= y < bottom:
            return window.hwnd
    return 0


def _inspect_cache(client: Any) -> Any:
    """One request for every property the risk check reads, so each element costs one call into its app."""
    cache = client.CreateCacheRequest()
    for prop in (
        _UIA_NAME,
        _UIA_CONTROL_TYPE,
        _UIA_BOUNDING_RECTANGLE,
        _UIA_PROCESS_ID,
        _UIA_LOCALIZED_CONTROL_TYPE,
        _UIA_AUTOMATION_ID,
        _UIA_CLASS_NAME,
        _UIA_IS_PASSWORD,
        _UIA_FRAMEWORK_ID,
        _UIA_IS_VALUE_PATTERN_AVAILABLE,
        _UIA_VALUE_IS_READ_ONLY,
        _UIA_ITEM_TYPE,
        _UIA_IS_DIALOG,
        _UIA_IS_TOGGLE_PATTERN_AVAILABLE,
        _UIA_TOGGLE_STATE,
        _UIA_IS_EXPAND_COLLAPSE_PATTERN_AVAILABLE,
        _UIA_EXPAND_COLLAPSE_STATE,
        _UIA_IS_SELECTION_ITEM_PATTERN_AVAILABLE,
        _UIA_SELECTION_ITEM_IS_SELECTED,
    ):
        try:
            cache.AddProperty(prop)
        except Exception:  # IsDialog is newer than some Windows 10 builds
            log.debug("UI Automation has no property %d here", prop)
    return cache


def _element_under(client: Any, api: Any, x: int, y: int, hwnd: int, ignore_pids: Collection[int], cache: Any) -> Any:
    """The element at (x, y), looking past the avatar's windows (see ``_window_at``); None when there's none."""
    element = client.ElementFromPointBuildCache(api.w.POINT(x, y), cache)
    if element and element.CachedProcessId not in ignore_pids:
        return element
    if not hwnd:
        return None
    # Her window answered for the spot: the smallest thing there in the window under her, level by level.
    node = client.ElementFromHandleBuildCache(hwnd, cache)
    everything = client.CreateTrueCondition()
    for _ in range(MAX_ANCESTORS):
        children = node.FindAllBuildCache(_TREE_SCOPE_CHILDREN, everything, cache)
        best, best_area = None, 0
        for index in range(min(children.Length, MAX_PER_WINDOW) if children else 0):
            child = children.GetElement(index)
            r = child.CachedBoundingRectangle
            area = (r.right - r.left) * (r.bottom - r.top)
            if r.left <= x < r.right and r.top <= y < r.bottom and (best is None or area < best_area):
                best, best_area = child, area
        if best is None:
            break
        node = best
    return node


_TOGGLED = {0: "off", 1: "on", 2: "mixed"}
_EXPANDED = {0: "collapsed", 1: "expanded", 2: "expanded"}  # 3, a leaf, can't be either


def _state(element: Any) -> str:
    """``Target.state`` from ``element``'s cached properties: only those of the patterns it says it has, since the
    others come back as UI Automation's "not supported" value."""
    parts = []
    if _cached(element, _UIA_IS_TOGGLE_PATTERN_AVAILABLE) is True:
        parts.append(_TOGGLED.get(_cached(element, _UIA_TOGGLE_STATE), ""))
    if _cached(element, _UIA_IS_EXPAND_COLLAPSE_PATTERN_AVAILABLE) is True:
        parts.append(_EXPANDED.get(_cached(element, _UIA_EXPAND_COLLAPSE_STATE), ""))
    if _cached(element, _UIA_IS_SELECTION_ITEM_PATTERN_AVAILABLE) is True:
        parts.append("selected" if _cached(element, _UIA_SELECTION_ITEM_IS_SELECTED) is True else "")
    return ", ".join(p for p in parts if p)


def _container_kind(element: Any) -> str:
    """ "dialog", "form" or "menu" when ``element`` is one of those, else ""."""
    local = (element.CachedLocalizedControlType or "").casefold()
    kind = control_type_name(element.CachedControlType)
    window_class = element.CachedClassName or ""
    if _cached(element, _UIA_IS_DIALOG) or local in ("dialog", "alert dialog", "alertdialog"):
        return "dialog"
    if kind == "window" and (window_class == "#32770" or window_class.startswith("MsiDialog")):
        return "dialog"
    if local == "form":
        return "form"
    return "menu" if kind == "menu" else ""


def _container_facts(client: Any, container: Any, cache: Any) -> tuple[str, tuple[tuple[str, str], ...]]:
    """The title and texts of a dialog, form or menu, and (kind, name) of its buttons, links and menu items."""
    kinds = [
        client.CreatePropertyCondition(_UIA_CONTROL_TYPE, kind)
        for kind in (_TYPE_TEXT, _TYPE_BUTTON, _TYPE_SPLIT_BUTTON, _TYPE_MENU_ITEM, _TYPE_LINK)
    ]
    found = container.FindAllBuildCache(
        _TREE_SCOPE_DESCENDANTS, functools.reduce(client.CreateOrCondition, kinds), cache
    )
    texts = [tidy(container.CachedName, CONTAINER_TEXT_CHARS)]
    actions: list[tuple[str, str]] = []
    for index in range(min(found.Length, CONTAINER_ITEMS) if found else 0):
        element = found.GetElement(index)
        if element.CachedControlType == _TYPE_TEXT:
            texts.append(tidy(element.CachedName, CONTAINER_TEXT_CHARS))
        elif label := tidy(element.CachedName, NAME_CHARS):
            actions.append((control_type_name(element.CachedControlType), label))
    return tidy(" ".join(t for t in texts if t), CONTEXT_CHARS), tuple(dict.fromkeys(actions))


def _describe(client: Any, element: Any, window: Target, frame: Rect | None, cache: Any) -> Target:
    """``window`` with the element part filled in from ``element`` (fetched with ``cache``) and its ancestors."""
    name = tidy(element.CachedName, NAME_CHARS)
    kind = control_type_name(element.CachedControlType)
    r = element.CachedBoundingRectangle
    rect = (r.left, r.top, r.right, r.bottom) if r.right > r.left and r.bottom > r.top else None
    automation_id = element.CachedAutomationId or ""
    pid = int(element.CachedProcessId or 0) or window.pid
    read_only = bool(_cached(element, _UIA_IS_VALUE_PATTERN_AVAILABLE)) and bool(
        _cached(element, _UIA_VALUE_IS_READ_ONLY)
    )
    # A browser's (or a web-based app's) page is a document too, but typing there goes to the site's shortcuts.
    page_root = kind == "document" and (
        automation_id == "RootWebArea" or _cached(element, _UIA_FRAMEWORK_ID) in ("Chrome", "Gecko")
    )
    caption = False
    if kind == "button" and rect is not None and frame is not None:  # apps that draw their own title bar
        caption = frame[2] - rect[2] <= 8 and rect[1] - frame[1] <= 12 and rect[2] - rect[0] <= 80
    chain = [(kind, name)]
    item_type = _cached_text(element, _UIA_ITEM_TYPE)  # File Explorer's item, or the name inside one
    web_page = kind == "document"
    code_editor = bool(_CODE_EDITOR.search(element.CachedClassName or ""))
    # What a click switched shows on what it works: a web page's check box, not the text inside it that's hit.
    state = _state(element)
    container, container_element, top = "", None, None
    walker = client.ControlViewWalker
    node = element
    for depth in range(MAX_ANCESTORS):
        try:
            parent = walker.GetParentElementBuildCache(node, cache)
        except Exception:
            break
        if not parent or (parent.CachedClassName or "") == "#32769":  # the desktop: node is the top-level window
            top = node
            break
        parent_kind = control_type_name(parent.CachedControlType)
        if depth == 0 and not state and kind in ("text", "image"):
            state = _state(parent)
        if len(chain) < CHAIN:
            chain.append((parent_kind, tidy(parent.CachedName, NAME_CHARS)))
            item_type = item_type or _cached_text(parent, _UIA_ITEM_TYPE)
        web_page = web_page or parent_kind == "document"
        code_editor = code_editor or bool(_CODE_EDITOR.search(parent.CachedClassName or ""))
        caption = caption or (depth < 2 and parent_kind == "title bar")
        if container_element is None and (found := _container_kind(parent)):
            container, container_element = found, parent
        node = parent
    if container_element is None and window.secondary and top is not None:
        container, container_element = "dialog", top  # a message box, or an installer's owned wizard window
    container_text, actions = "", ()
    if container_element is not None:
        try:
            container_text, actions = _container_facts(client, container_element, cache)
        except Exception:
            log.debug("Reading the %s around %r failed", container, name, exc_info=True)
    return replace(
        window,
        name=name,
        kind=kind,
        automation_id=automation_id,
        rect=rect,
        pid=pid,
        process=window.process if pid == window.pid else _process_name(pid),
        password=bool(element.CachedIsPassword),
        editable=kind in ("text box", "document") and not page_root and not read_only,
        web_page=web_page,
        page_root=page_root,
        code_editor=code_editor and web_page,
        item_type=item_type,
        chain=tuple(chain),
        caption=caption,
        container=container,
        container_text=container_text,
        actions=actions,
        state=state,
    )


def describe_point(x: int, y: int, ignore_pids: Collection[int] = frozenset(), use_uia: bool = True) -> Target:
    """What a click at desktop pixel (x, y) would land on, looking past the windows of ``ignore_pids``. Blocking;
    with ``use_uia`` only on the UI Automation thread (``inspect_point``)."""
    api = _win()
    with per_monitor_dpi():
        hwnd = _window_at(api, x, y, ignore_pids)
        window = window_target(api, hwnd)
        if not use_uia:
            return window
        if hwnd and not _answers(api, hwnd):
            return replace(window, timed_out=True)  # the app is hung: what's there can't be checked
        try:
            client = _automation()
            cache = _inspect_cache(client)
            element = _element_under(client, api, x, y, hwnd, ignore_pids, cache)
            if not element:
                return window
            return _describe(client, element, window, _frame(api, hwnd) if hwnd else None, cache)
        except Exception:  # no UI Automation, the app not answering, the element gone meanwhile
            log.debug("UI Automation couldn't describe the spot %d, %d", x, y, exc_info=True)
            return window


def describe_focus(ignore_pids: Collection[int] = frozenset(), use_uia: bool = True) -> Target:
    """What has the keyboard focus: what typing would go into. Blocking; with ``use_uia`` only on the UI Automation
    thread (``inspect_focus``)."""
    api = _win()
    with per_monitor_dpi():
        hwnd = api.user32.GetForegroundWindow() or 0
        if hwnd and _pid(api, hwnd) in ignore_pids:
            hwnd = 0  # her window never takes the focus; should it ever, there's nothing of the user's to type into
        window = window_target(api, hwnd)
        if not use_uia or not hwnd:
            return window
        if not _answers(api, hwnd):
            return replace(window, timed_out=True)
        try:
            client = _automation()
            cache = _inspect_cache(client)
            element = client.GetFocusedElementBuildCache(cache)
            if not element or element.CachedProcessId in ignore_pids:
                return window
            return _describe(client, element, window, _frame(api, hwnd), cache)
        except Exception:
            log.debug("UI Automation couldn't describe the focus", exc_info=True)
            return window


def focus_identity(ignore_pids: Collection[int] = frozenset()) -> tuple[int, ...] | None:
    """The element with the keyboard focus, as UI Automation's runtime id: the same for as long as that element
    lives, whatever is typed into it. None when it can't be told. Blocking; on the UI Automation thread."""
    try:
        element = _automation().GetFocusedElement()
        if not element or element.CurrentProcessId in ignore_pids:
            return None
        return tuple(int(part) for part in element.GetRuntimeId())
    except Exception:
        log.debug("UI Automation couldn't say what has the focus", exc_info=True)
        return None


def focus_value(ignore_pids: Collection[int] = frozenset()) -> str | None:
    """The text in the box that has the keyboard focus (its value, as the app tells UI Automation), to tell whether
        what she typed left it when she pressed Enter. None when it can't be told: no box, a password box (never read),
        an app that isn't answering or doesn't say. Blocking; on the UI Automation thread. Chromium's own read of the focus
        and its value is one of the reads that never froze it (the module's docstring); Discord's message box says its
        text (an empty one "﻿
    ")."""
    api = _win()
    hwnd = api.user32.GetForegroundWindow() or 0
    if not hwnd or _pid(api, hwnd) in ignore_pids or not _answers(api, hwnd):
        return None
    try:
        element = _automation().GetFocusedElement()
        if not element or element.CurrentProcessId in ignore_pids or element.CurrentIsPassword:
            return None
        if not element.GetCurrentPropertyValue(_UIA_IS_VALUE_PATTERN_AVAILABLE):
            return None
        value = element.GetCurrentPropertyValue(_UIA_VALUE_VALUE)
        return value if isinstance(value, str) else None
    except Exception:
        log.debug("UI Automation couldn't say what's in the focused box", exc_info=True)
        return None


async def read_focus_value(ignore_pids: Collection[int] = frozenset(), timeout_s: float = 1.0) -> str | None:
    """``focus_value`` on the UI Automation thread; None when that isn't quick."""
    if sys.platform != "win32":
        return None
    try:
        return await run(focus_value, frozenset(ignore_pids), timeout_s=timeout_s)
    except TimeoutError:
        return None


async def focus_now(ignore_pids: Collection[int] = frozenset(), timeout_s: float = 1.0) -> tuple[int, ...] | None:
    """``focus_identity`` on the UI Automation thread; None when that isn't quick (it's asked between typed chunks)."""
    if sys.platform != "win32":
        return None
    try:
        return await run(focus_identity, frozenset(ignore_pids), timeout_s=timeout_s)
    except TimeoutError:
        return None


async def inspect_point(
    x: int, y: int, ignore_pids: Collection[int] = frozenset(), timeout_s: float = INSPECT_TIMEOUT_S
) -> Target:
    """``describe_point`` on the UI Automation thread; by the window alone when UI Automation takes too long."""
    return await _inspect(describe_point, (x, y, frozenset(ignore_pids)), timeout_s)


async def inspect_focus(ignore_pids: Collection[int] = frozenset(), timeout_s: float = INSPECT_TIMEOUT_S) -> Target:
    """``describe_focus`` on the UI Automation thread; by the window alone when UI Automation takes too long."""
    return await _inspect(describe_focus, (frozenset(ignore_pids),), timeout_s)


async def _inspect(describe: Callable[..., Target], args: tuple, timeout_s: float) -> Target:
    if sys.platform != "win32":
        return Target()
    try:
        return await run(describe, *args, timeout_s=timeout_s)
    except TimeoutError:
        # A hung app (or a long search ahead in the queue): the window part needs no UI Automation.
        log.warning("UI Automation took over %.0fs; judging by the window alone", timeout_s)
        return replace(await asyncio.to_thread(describe, *args, False), timed_out=True)


def _init_com() -> None:
    try:
        import comtypes

        comtypes.CoInitialize()  # one apartment for the thread's whole life, like the other COM threads here
    except Exception:
        log.debug("COM initialisation on the UI Automation thread failed", exc_info=True)


_Job = tuple[Future, Callable[..., Any], tuple, dict]


class _ComThread(Executor):
    """One daemon thread with COM initialised on it, running calls one at a time in the order they came.

    A daemon, unlike a ``ThreadPoolExecutor``'s worker, so a call stuck inside a hung app never holds up her exit.
    ``stuck_for`` says how long the current call has been running, so a stuck one can be left behind
    (``_uia_thread``)."""

    def __init__(self, name: str) -> None:
        self._jobs: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._closed = False
        self._busy_since: float | None = None
        self.thread = threading.Thread(target=self._work, name=name, daemon=True)
        self.thread.start()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        if self._closed:
            raise RuntimeError("this UI Automation thread was left behind")
        future: Future = Future()
        self._jobs.put((future, fn, args, kwargs))
        return future

    def _work(self) -> None:
        _init_com()
        while (job := self._jobs.get()) is not None:
            future, fn, args, kwargs = job
            if not future.set_running_or_notify_cancel():
                continue
            self._busy_since = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                self._busy_since = None
        _came_back(self)

    def stuck_for(self) -> float:
        since = self._busy_since
        return 0.0 if since is None else time.monotonic() - since

    def leave_behind(self) -> list[_Job]:
        """Take no more work and hand back what was waiting, for the thread that replaces this one. The stuck call
        finishes (or never does) on its own."""
        self._closed = True
        waiting: list[_Job] = []
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job is not None:
                waiting.append(job)
        self._jobs.put(None)  # ends the thread if the stuck call ever returns
        return waiting

    def take(self, jobs: Sequence[_Job]) -> None:
        for job in jobs:
            self._jobs.put(job)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        for future, *_ in self.leave_behind():
            future.cancel()


_pool: _ComThread | None = None
_pool_lock = threading.Lock()
_left_behind = 0  # threads given up on and still stuck in some app


def _came_back(thread: _ComThread) -> None:
    """A left-behind thread's stuck call returned after all, and the thread has ended: it no longer counts."""
    global _left_behind
    with _pool_lock:
        if thread is not _pool and _left_behind > 0:
            _left_behind -= 1


def _uia_thread() -> _ComThread:
    """The thread UI Automation work runs on, with COM initialised on it.

    A call into an app that has stopped answering can block for good, and everything queued behind it would wait
    for ever too. So when a call has been running for ``STUCK_S``, new work (and whatever was waiting) goes to a
    fresh thread and the stuck one is left behind, up to ``MAX_LEFT_BEHIND`` of them; past that, every UI
    Automation call waits behind the last one and times out, as UI Automation is plainly unusable then."""
    global _pool, _left_behind
    with _pool_lock:
        if _pool is not None and _pool.stuck_for() > STUCK_S and _left_behind < MAX_LEFT_BEHIND:
            _left_behind += 1
            log.warning("A UI Automation call has been stuck for %.0fs; leaving its thread behind", _pool.stuck_for())
            waiting = _pool.leave_behind()
            _pool = _ComThread(f"octavia-uia-{_left_behind}")
            _pool.take(waiting)
        if _pool is None:
            _pool = _ComThread("octavia-uia")
        return _pool


async def run(fn: Callable[..., Any], *args: Any, timeout_s: float) -> Any:
    """``fn(*args)`` on the UI Automation thread, given up on after ``timeout_s`` (``TimeoutError``). The call itself
    can't be stopped; if it stays stuck, later ones move to a fresh thread (``_uia_thread``)."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(_uia_thread(), fn, *args), timeout_s)


async def find(text: str, ignore_pids: Collection[int] = frozenset(), budget_s: float | None = None) -> list[Match]:
    """``search`` on the UI Automation thread, off the event loop. Raises ``UIAUnavailable``, and ``TimeoutError``
    when an app holds the search up well past its budget. ``budget_s`` 0 searches the foreground window only; None
    keeps the usual budget."""
    if sys.platform != "win32":
        raise UIAUnavailable("UI Automation only exists on Windows")
    if budget_s is None:
        return await run(search, text, frozenset(ignore_pids), timeout_s=SEARCH_BUDGET_S + FIND_GRACE_S)
    return await run(search, text, frozenset(ignore_pids), budget_s, timeout_s=budget_s + FIND_GRACE_S)


# --------------------------------------------------------------------------- a browser tab by its name

_TYPE_TAB_ITEM = 50019
_SELECTION_ITEM_PATTERN = 10010
_LEGACY_PATTERN = 10018


def _pick(element: Any) -> bool:
    """Bring a tab to the front the way its app lets: selecting it, or its default action (a click)."""
    module = _module()
    for pattern_id, interface, act in (
        (_SELECTION_ITEM_PATTERN, "IUIAutomationSelectionItemPattern", "Select"),
        (_LEGACY_PATTERN, "IUIAutomationLegacyIAccessiblePattern", "DoDefaultAction"),
    ):
        try:
            pattern = element.GetCurrentPattern(pattern_id)
            if not pattern:
                continue
            getattr(pattern.QueryInterface(getattr(module, interface)), act)()
            return True
        except Exception:
            log.debug("Picking a tab with pattern %s failed", pattern_id, exc_info=True)
    return False


def select_tab(text: str, ignore_pids: Collection[int] = frozenset()) -> tuple[int, str] | None:
    """Find a tab whose name contains ``text`` (ignoring case) in the open windows, front to back, and bring it to
    the front of its window: "Overleaf" and "ChatGPT" are tabs in the browser, not windows, so looking up a window by
    that name found nothing (2026-11-04). Returns (its window, the tab's name), or None. Blocking; only on the UI
    Automation thread (``find_tab``)."""
    api = _win()
    with per_monitor_dpi():
        client = _automation()
        started = time.monotonic()
        condition = client.CreateAndCondition(
            client.CreatePropertyCondition(_UIA_CONTROL_TYPE, _TYPE_TAB_ITEM),
            client.CreatePropertyConditionEx(_UIA_NAME, text, _IGNORE_CASE | _MATCH_SUBSTRING),
        )
        for order, window in enumerate(_top_level_windows(api, monitor_rects())[:MAX_WINDOWS]):
            if window.pid in ignore_pids:
                continue
            if order and time.monotonic() - started > SEARCH_BUDGET_S:
                break
            if not _answers(api, window.hwnd):
                continue
            try:
                found = client.ElementFromHandle(window.hwnd).FindFirst(_TREE_SCOPE_SUBTREE, condition)
                if not found:
                    continue
                name = tidy(found.CurrentName, NAME_CHARS)
                if _pick(found):
                    return window.hwnd, name
            except Exception:
                log.debug("Looking for a tab in window %s failed", window.hwnd, exc_info=True)
        return None


async def find_tab(text: str, ignore_pids: Collection[int] = frozenset()) -> tuple[int, str] | None:
    """``select_tab`` on the UI Automation thread; None when there's no such tab or UI Automation can't be used."""
    if sys.platform != "win32" or len(text.strip()) < 3:
        return None
    try:
        return await run(select_tab, text.strip(), frozenset(ignore_pids), timeout_s=SEARCH_BUDGET_S + FIND_GRACE_S)
    except Exception:
        log.info("Couldn't look for a tab named %r", text, exc_info=True)
        return None
