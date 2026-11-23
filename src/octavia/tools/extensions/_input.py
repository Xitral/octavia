"""Mouse and keyboard input for computer control: SendInput, the cursor, and noticing the user take over.

Everything goes through ``SendInput``, the documented way to make input: clicks, wheel notches, keys and cursor moves
reach apps exactly as if they came from the hardware, and ``KEYEVENTF_UNICODE`` types any character in any language
whatever the keyboard layout. An app that streams a remote screen passes on which key went down rather than text, and
Unicode never reaches it; for those, ``scan_strokes`` types each character as its key on the layout, by scan code
(``KEYEVENTF_SCANCODE``), one key at a time. Windows itself keeps it within bounds: nothing reaches the secure desktop
(UAC prompts, the lock screen) or apps running as administrator, and Ctrl+Alt+Delete can't be faked at all.

Positions are physical desktop pixels, the ones screenshots, UI Automation and the avatar use. A move goes to
absolute coordinates spread over the whole virtual desktop (``MOUSEEVENTF_VIRTUALDESK``) in a per-monitor DPI aware
thread context, and the cursor is read back to make sure it got there.

``Watch`` notices the user taking the mouse or keyboard back, without an input hook: a low-level hook would run in
this Python process, and while the GIL was busy elsewhere every mouse move on the whole system would wait for it.
It compares, before each action, where the cursor is with where she left it, and Windows' last-input time with the
one right after her own input.

Not loaded as a tool module (the name starts with ``_``); ``computer_control`` uses it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import re
import threading
import time
import unicodedata
import weakref
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from octavia.tools.extensions.screen_capture import Rect, per_monitor_dpi

log = logging.getLogger(__name__)

Button = Literal["left", "right", "middle"]
Direction = Literal["up", "down", "left", "right"]

MOVED_PX = 25.0  # the cursor this far from where she left it: the user moved the mouse
STILL_PX = 3.0  # closer than this while Windows saw newer input than hers: the user typed or clicked
LATE_MS = 50  # input stamped this long after hers went in isn't hers (Windows' tick moves in ~16 ms steps)
BLIP_GAP_MS = 2000  # a motionless input blip this soon after another forgiven one is taken as the user
WHEEL_DELTA = 120  # one notch of the mouse wheel
# How long a click or key press stays down: more than a game's tick (50 ms in Minecraft), and a double-click's two
# presses still fit well within Windows' shortest double-click time (200 ms).
TAP_S = 0.06
# Between the presses and the releases of keys typed by scan code: apps streaming a remote screen forward each one
# over the network, and a burst of them in one instant can arrive out of order or be dropped (~100 characters, 2 s).
KEY_PACE_S = 0.008
MARK = 0x4F435456  # dwExtraInfo on all her input ("OCTV"), so a debugging hook can tell it from the user's

# winuser.h
_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE, _KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0004, 0x0008
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
_MOUSEEVENTF_WHEEL, _MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
_MOUSEEVENTF_VIRTUALDESK, _MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
_BUTTONS = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}
_SM_SWAPBUTTON = 23
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79
_MAPVK_VK_TO_VSC, _MAPVK_VK_TO_CHAR, _MAPVK_VK_TO_VSC_EX = 0, 2, 4
_DEAD_KEY = 0x80000000  # MapVirtualKey's mark on a key's character when the key is a dead key
_GA_ROOT = 2

# --------------------------------------------------------------------------- keys

VK_RETURN = 0x0D
VK_LSHIFT = 0xA0

# The modifiers she may hold, as the left-hand keys: apps asking for "Ctrl" see those too.
MODIFIERS: dict[str, tuple[int, str]] = {
    "ctrl": (0xA2, "Ctrl"),
    "alt": (0xA4, "Alt"),
    "shift": (VK_LSHIFT, "Shift"),
    "win": (0x5B, "Win"),
}
_MODIFIER_ALIASES = {"control": "ctrl", "ctl": "ctrl", "windows": "win"}

# Every key she can press, by name: (virtual key, how it's said).
KEYS: dict[str, tuple[int, str]] = {
    "enter": (VK_RETURN, "Enter"),
    "tab": (0x09, "Tab"),
    "escape": (0x1B, "Escape"),
    "space": (0x20, "Space"),
    "backspace": (0x08, "Backspace"),
    "delete": (0x2E, "Delete"),
    "insert": (0x2D, "Insert"),
    "home": (0x24, "Home"),
    "end": (0x23, "End"),
    "page_up": (0x21, "Page Up"),
    "page_down": (0x22, "Page Down"),
    "up": (0x26, "Up"),
    "down": (0x28, "Down"),
    "left": (0x25, "Left"),
    "right": (0x27, "Right"),
    "context_menu": (0x5D, "the menu key"),
    "print_screen": (0x2C, "Print Screen"),
    "volume_up": (0xAF, "Volume Up"),
    "volume_down": (0xAE, "Volume Down"),
    "volume_mute": (0xAD, "Mute"),
    "play_pause": (0xB3, "Play/Pause"),
    "next_track": (0xB0, "Next Track"),
    "previous_track": (0xB1, "Previous Track"),
    "minus": (0xBD, "-"),
    "equals": (0xBB, "="),
    "plus": (0xBB, "+"),  # the = key: Ctrl+plus is what apps take for zooming in
    "comma": (0xBC, ","),
    "period": (0xBE, "."),
    "slash": (0xBF, "/"),
    "backslash": (0xDC, "\\"),
    "semicolon": (0xBA, ";"),
    "quote": (0xDE, "'"),
    "backquote": (0xC0, "`"),
    "left_bracket": (0xDB, "["),
    "right_bracket": (0xDD, "]"),
}
KEYS.update({c: (ord(c.upper()), c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"})
KEYS.update({f"f{n}": (0x6F + n, f"F{n}") for n in range(1, 25)})

_KEY_ALIASES = {
    "return": "enter",
    "esc": "escape",
    "del": "delete",
    "ins": "insert",
    "pageup": "page_up",
    "pgup": "page_up",
    "pagedown": "page_down",
    "pgdn": "page_down",
    "spacebar": "space",
    "apps": "context_menu",
    "printscreen": "print_screen",
    "prtsc": "print_screen",
    "mute": "volume_mute",
    "arrow_up": "up",
    "up_arrow": "up",
    "arrow_down": "down",
    "down_arrow": "down",
    "arrow_left": "left",
    "left_arrow": "left",
    "arrow_right": "right",
    "right_arrow": "right",
    "-": "minus",
    "=": "equals",
    "+": "plus",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "\\": "backslash",
    ";": "semicolon",
    "'": "quote",
    "`": "backquote",
    "[": "left_bracket",
    "]": "right_bracket",
}

# Keys Windows marks as extended: without the flag an arrow can arrive as a number-pad key (a digit, with Num Lock).
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D}
_EXTENDED |= {0xA3, 0xA5, 0x6F, 0x90, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}

KEY_HELP = (
    "Keys: a-z, 0-9, f1-f24, enter, tab, escape, space, backspace, delete, insert, home, end, page_up, page_down, "
    "up, down, left, right, context_menu, print_screen, volume_up, volume_down, volume_mute, play_pause, "
    "next_track, previous_track, and - = [ ] \\ ; ' , . / ` (or minus, equals, plus, left_bracket, right_bracket, "
    "backslash, semicolon, quote, comma, period, slash, backquote). Modifiers: ctrl, alt, shift, win, joined to "
    "one key with +, like ctrl+shift+t."
)


@dataclass(frozen=True)
class KeyCombo:
    """A key with the modifiers held for it, or one modifier on its own (``win`` opens the Start menu)."""

    modifiers: tuple[str, ...]  # canonical names, in the order ctrl, alt, shift, win
    key: str | None  # canonical name; None for a lone modifier

    @property
    def text(self) -> str:
        """The canonical form, e.g. "ctrl+shift+t"."""
        return "+".join((*self.modifiers, *((self.key,) if self.key else ())))

    @property
    def label(self) -> str:
        """How it's said, e.g. "Ctrl+Shift+T"."""
        names = [MODIFIERS[m][1] for m in self.modifiers]
        if self.key:
            names.append(KEYS[self.key][1])
        return "+".join(names)

    @property
    def vks(self) -> tuple[int, ...]:
        """The virtual keys to press, modifiers first."""
        return (*(MODIFIERS[m][0] for m in self.modifiers), *((KEYS[self.key][0],) if self.key else ()))


def _token(raw: str) -> str:
    """One part of a combination, in canonical spelling: "Page Down" and "page-down" are "page_down"."""
    token = raw.strip().casefold()
    if len(token) > 1:
        token = re.sub(r"[\s-]+", "_", token)
    return _MODIFIER_ALIASES.get(token) or _KEY_ALIASES.get(token) or token


def parse_keys(text: str) -> KeyCombo:
    """``"ctrl+shift+t"`` -> ``KeyCombo``. Raises ValueError, with the names she can use, for anything else."""
    raw = re.sub(r"\s*\+\s*", "+", " ".join(text.split()))
    if not raw:
        raise ValueError(f"Give a key to press, like 'enter' or 'ctrl+c'. {KEY_HELP}")
    if raw == "+" or raw.endswith("++"):  # the plus key itself: "+", "ctrl++"
        head = raw[:-1].rstrip("+")
        parts = [*(head.split("+") if head else ()), "+"]
    else:
        parts = raw.split("+")
    modifiers: set[str] = set()
    keys: list[str] = []
    for part in parts:
        if not part.strip():
            raise ValueError(f"'{text}' has an empty part. {KEY_HELP}")
        token = _token(part)
        if token in MODIFIERS:
            modifiers.add(token)
        elif token in KEYS:
            keys.append(token)
        else:
            raise ValueError(f"Unknown key '{part.strip()}'. {KEY_HELP}")
    if len(keys) > 1:
        raise ValueError(
            f"'{text}' has more than one key ({' and '.join(keys)}): press one key at a time, with its modifiers. "
            "For text, use type_text."
        )
    ordered = tuple(m for m in MODIFIERS if m in modifiers)
    if not keys and len(ordered) > 1:
        raise ValueError(f"'{text}' has only modifiers: add the key to press with them. {KEY_HELP}")
    if keys and keys[0] == "delete" and {"ctrl", "alt"} <= modifiers:
        raise ValueError("Windows doesn't let apps press Ctrl+Alt+Delete.")
    return KeyCombo(ordered, keys[0] if keys else None)


@dataclass(frozen=True)
class Stroke:
    """One key going down or up: a virtual key, one UTF-16 unit of text (typed whatever the keyboard layout), or a
    hardware scan code (``scan``; 0xE0 in the high byte for an extended key), as a physical key press makes it."""

    vk: int = 0
    unit: int = 0
    up: bool = False
    scan: int = 0


def combo_strokes(vks: Sequence[int]) -> list[Stroke]:
    """Keys down in order, then up in reverse, as one press of a shortcut."""
    return [*(Stroke(vk=vk) for vk in vks), *(Stroke(vk=vk, up=True) for vk in reversed(vks))]


def held_apart(strokes: Sequence[Stroke]) -> list[list[Stroke]]:
    """``strokes`` split wherever a key goes up right after going down, so it can stay down a moment there: games
    read the keyboard once a frame or tick, and a key that goes down and up in the same instant never shows. Typed
    characters stay together; apps read those as text, and holding each one would slow typing to a crawl."""
    batches: list[list[Stroke]] = [[]]
    for i, stroke in enumerate(strokes):
        before = strokes[i - 1] if i else None
        if stroke.vk and stroke.up and before is not None and before.vk and not before.up:
            batches.append([])
        batches[-1].append(stroke)
    return [batch for batch in batches if batch]


def clean_text(text: str) -> str:
    """Text as it will be typed: line breaks as "\\n". Raises ValueError for characters that aren't text.

    Control characters would arrive as control codes, and some of those are shortcuts in disguise (0x16 pastes in
    an edit box, 0x03 interrupts a console), so they're refused rather than typed; so is a tab, which moves the
    focus somewhere the risk check never looked (press_keys "tab" does that as a step of its own).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in text:
        if ch != "\n" and unicodedata.category(ch) == "Cc":
            name = "a tab" if ch == "\t" else f"the control character U+{ord(ch):04X}"
            raise ValueError(f"the text has {name}; type only text (press_keys moves between fields)")
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise ValueError("the text has a broken character (half of a surrogate pair)")
        if ch in _DIRECTION_OVERRIDES:
            raise ValueError("the text has an invisible direction override, which can make it read as something else")
    return text


# Invisible characters that reorder how text reads ("invoice‮fdp.exe" shows as "invoiceexe.pdf").
_DIRECTION_OVERRIDES = frozenset("‪‫‬‭‮⁦⁧⁨⁩")


def text_strokes(text: str, newline: Sequence[int] = (VK_RETURN,)) -> list[Stroke]:
    """Strokes that type ``text``: each character as its UTF-16 units (an emoji is two), each line break as the
    ``newline`` keys (Enter, or Shift+Enter where Enter would send)."""
    strokes: list[Stroke] = []
    for ch in text:
        if ch == "\n":
            strokes.extend(combo_strokes(newline))
            continue
        encoded = ch.encode("utf-16-le")
        for i in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[i : i + 2], "little")
            strokes.append(Stroke(unit=unit))
            strokes.append(Stroke(unit=unit, up=True))
    return strokes


# Scan codes of the keys typing by key presses needs on any layout (set 1, as keyboards send them).
SCAN_LSHIFT, SCAN_LCTRL, SCAN_RETURN = 0x2A, 0x1D, 0x1C

# Where a character is on the keyboard layout: (scan code, with Shift), or None when no key alone or with Shift types
# it (another script, an AltGr character, a dead key that would wait to join the next letter).
KeyFor = Callable[[str], "tuple[int, bool] | None"]


def scan_strokes(text: str, key_for: KeyFor, newline: Sequence[int] = (SCAN_RETURN,)) -> list[Stroke]:
    """Strokes that type ``text`` as key presses on the keyboard layout: each character's key by its scan code,
    Shift held around it when the layout needs it, each line break as the ``newline`` scan codes (Enter, or Shift
    and Enter where Enter would send). Apps that stream a remote screen (a cloud workspace in a web page, a remote
    desktop) pass on which key went down, not text, so ``text_strokes``' Unicode packets never reach them. A character
    no key types (``key_for`` None) still goes as Unicode, the only way it can."""
    strokes: list[Stroke] = []
    for ch in text:
        if ch == "\n":
            strokes.extend(Stroke(scan=s) for s in newline)
            strokes.extend(Stroke(scan=s, up=True) for s in reversed(newline))
            continue
        key = key_for(ch)
        if key is None:
            strokes.extend(text_strokes(ch))
            continue
        scan, shift = key
        presses = (SCAN_LSHIFT, scan) if shift else (scan,)
        strokes.extend(Stroke(scan=s) for s in presses)
        strokes.extend(Stroke(scan=s, up=True) for s in reversed(presses))
    return strokes


def paste_strokes(key_for: KeyFor) -> list[Stroke]:
    """Ctrl+V as key presses: the V where the layout has it, by scan code like the rest of a streamed app's input."""
    key = key_for("v")
    if key is None:
        return combo_strokes((MODIFIERS["ctrl"][0], KEYS["v"][0]))
    return [
        Stroke(scan=SCAN_LCTRL),
        Stroke(scan=key[0]),
        Stroke(scan=key[0], up=True),
        Stroke(scan=SCAN_LCTRL, up=True),
    ]


def chunks(text: str, size: int) -> list[str]:
    """``text`` in pieces of at most ``size`` characters (whole characters, so an emoji is never split)."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


# --------------------------------------------------------------------------- coordinates


def to_absolute(x: int, y: int, desktop: Rect) -> tuple[int, int]:
    """Physical desktop pixel -> SendInput's absolute coordinates (0..65535 across the virtual desktop).

    Windows maps back by truncating (``ax * width >> 16``); aiming at the pixel's centre keeps the rounding of 65536
    steps over a few thousand pixels from ever tipping it onto the neighbour. ``WindowsDesktop.move`` still reads
    the cursor back, in case a setup maps differently.
    """
    left, top, right, bottom = desktop
    width, height = max(right - left, 1), max(bottom - top, 1)
    ax = round((x - left + 0.5) * 65536 / width)
    ay = round((y - top + 0.5) * 65536 / height)
    return min(max(ax, 0), 65535), min(max(ay, 0), 65535)


# --------------------------------------------------------------------------- Windows


class InputFailed(OSError):
    """Windows didn't take the input (the PC is locked, or the pointer can't get there)."""


class Desktop(Protocol):
    """The mouse and keyboard, as the tools and the watch use them. Every method may block briefly."""

    def cursor(self) -> tuple[int, int]: ...

    def last_input(self) -> int: ...

    def tick(self) -> int: ...

    def held(self) -> str | None: ...

    def foreground(self) -> int: ...

    def owner_at(self, x: int, y: int) -> int: ...

    def move(self, x: int, y: int) -> tuple[int, int]: ...

    def click(self, x: int, y: int, button: Button, double: bool) -> None: ...

    def wheel(self, direction: Direction, notches: int) -> None: ...

    def keys(self, strokes: Sequence[Stroke]) -> int: ...

    def button(self, button: Button, down: bool) -> None: ...

    def nudge(self, dx: int, dy: int) -> None: ...


@functools.cache
def _api() -> Any:
    """Typed Win32 entry points on private ``WinDLL`` instances (as in screen_capture), built on first use because
    they only exist on Windows. The INPUT structures are the ones platform.windows already declares."""
    import ctypes
    from ctypes import POINTER, c_int
    from ctypes import wintypes as w
    from types import SimpleNamespace

    from octavia.platform.windows import INPUT, KEYBDINPUT, MOUSEINPUT

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")

    def sig(fn: Any, restype: Any, *argtypes: Any) -> None:
        fn.restype, fn.argtypes = restype, list(argtypes)

    sig(kernel32.GetTickCount, w.DWORD)
    sig(user32.SendInput, w.UINT, w.UINT, POINTER(INPUT), c_int)
    sig(user32.GetCursorPos, w.BOOL, POINTER(w.POINT))
    sig(user32.SetCursorPos, w.BOOL, c_int, c_int)
    sig(user32.GetLastInputInfo, w.BOOL, POINTER(LASTINPUTINFO))
    sig(user32.GetAsyncKeyState, w.SHORT, c_int)
    sig(user32.GetSystemMetrics, c_int, c_int)
    sig(user32.MapVirtualKeyW, w.UINT, w.UINT, w.UINT)
    sig(user32.GetForegroundWindow, w.HWND)
    sig(user32.WindowFromPoint, w.HWND, w.POINT)
    sig(user32.GetAncestor, w.HWND, w.HWND, w.UINT)
    sig(user32.GetWindowThreadProcessId, w.DWORD, w.HWND, POINTER(w.DWORD))
    sig(user32.GetKeyboardLayout, w.HKL, w.DWORD)
    sig(user32.VkKeyScanExW, w.SHORT, w.WCHAR, w.HKL)
    sig(user32.MapVirtualKeyExW, w.UINT, w.UINT, w.UINT, w.HKL)
    return SimpleNamespace(
        ctypes=ctypes,
        w=w,
        user32=user32,
        kernel32=kernel32,
        INPUT=INPUT,
        KEYBDINPUT=KEYBDINPUT,
        MOUSEINPUT=MOUSEINPUT,
        LASTINPUTINFO=LASTINPUTINFO,
    )


# Held keys and buttons that mean the user is at the keyboard or mouse right now (GetAsyncKeyState).
_HELD = (
    (0x01, "a mouse button"),
    (0x02, "a mouse button"),
    (0x04, "a mouse button"),
    (0x05, "a mouse button"),
    (0x06, "a mouse button"),
    (0x10, "Shift"),
    (0x11, "Ctrl"),
    (0x12, "Alt"),
    (0x5B, "the Windows key"),
    (0x5C, "the Windows key"),
)
_MOUSE_BUTTONS = frozenset({0x01, 0x02, 0x04, 0x05, 0x06})
# Every mouse button and key a press of which is the user at the desk (GetAsyncKeyState).
_PRESSABLE = (*sorted(_MOUSE_BUTTONS), *range(0x08, 0xFF))


class WindowsDesktop:
    """The real mouse and keyboard, through SendInput. Every method blocks briefly: call them off the event loop."""

    def cursor(self) -> tuple[int, int]:
        api = _api()
        with per_monitor_dpi():
            return self._cursor(api)

    def last_input(self) -> int:
        """Windows' tick (ms) of the last input from anyone, hers included."""
        api = _api()
        info = api.LASTINPUTINFO()
        info.cbSize = api.ctypes.sizeof(info)
        if not api.user32.GetLastInputInfo(api.ctypes.byref(info)):
            raise InputFailed("Windows couldn't say when the last input was")
        return int(info.dwTime)

    def tick(self) -> int:
        """Windows' tick (ms) now, on the same clock as ``last_input``."""
        return int(_api().kernel32.GetTickCount())

    def held(self) -> str | None:
        """A key or button the user is holding down right now, or None."""
        state = _api().user32.GetAsyncKeyState
        for vk, name in _HELD:
            if state(vk) & 0x8000:
                return name
        return None

    def pressed_since_asked(self) -> str | None:
        """A key or mouse button that went down since the last call, or None. Every call clears what it read.

        Windows keeps one "pressed since last asked" bit per key for everyone, so another app asking can clear it
        first: a press it reports is certain, but a press can go unreported (``Watch`` allows for that)."""
        state = _api().user32.GetAsyncKeyState
        found = None
        for vk in _PRESSABLE:
            if state(vk) & 1 and found is None:
                found = "a mouse button" if vk in _MOUSE_BUTTONS else "a key"
        return found

    def foreground(self) -> int:
        return _api().user32.GetForegroundWindow() or 0

    def foreground_pid(self) -> int:
        """The process of the window in front; 0 when there's none."""
        api = _api()
        hwnd = api.user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = api.w.DWORD()
        api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid))
        return int(pid.value)

    def bring_to_front(self, hwnd: int) -> bool:
        """Give ``hwnd`` the keyboard again (``platform.windows.focus_window``); whether it's in front now. No input
        is sent, so it's nothing the takeover watch hears."""
        from octavia.platform.windows import focus_window

        return focus_window(hwnd)

    def owner_at(self, x: int, y: int) -> int:
        """The process of the top-level window a click at (x, y) would reach; 0 when there's none."""
        api = _api()
        with per_monitor_dpi():
            hwnd = api.user32.GetAncestor(api.user32.WindowFromPoint(api.w.POINT(x, y)), _GA_ROOT)
            if not hwnd:
                return 0
            pid = api.w.DWORD()
            api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid))
            return int(pid.value)

    def move(self, x: int, y: int) -> tuple[int, int]:
        """Move the cursor to (x, y) and return where it really is now."""
        api = _api()
        with per_monitor_dpi():
            ax, ay = to_absolute(x, y, self._virtual_desktop(api))
            self._send(
                api, [self._mouse(api, ax, ay, _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK)]
            )
            deadline = time.monotonic() + 0.2  # Windows moves it a moment after SendInput returns
            while True:
                where = self._cursor(api)
                if max(abs(where[0] - x), abs(where[1] - y)) <= 1 or time.monotonic() > deadline:
                    break
                time.sleep(0.005)
            if max(abs(where[0] - x), abs(where[1] - y)) > 1:
                # Landed a pixel or more off (rounding on an odd desktop size, a monitor's scaling): put it there.
                api.user32.SetCursorPos(x, y)
                where = self._cursor(api)
            if max(abs(where[0] - x), abs(where[1] - y)) > 1:
                raise InputFailed(f"the pointer stopped at {where[0]}, {where[1]} instead of {x}, {y}")
            return where

    def click(self, x: int, y: int, button: Button, double: bool) -> None:
        """Press and release ``button`` at (x, y), twice for a double-click, holding each press ``TAP_S``: games
        read the mouse once a frame or tick and miss a press let go in the same instant (a Minecraft inventory slot
        lights up but nothing is picked up). The position goes with every press and release, so the click lands
        exactly there even if the cursor was nudged since the move."""
        api = _api()
        if button != "middle" and api.user32.GetSystemMetrics(_SM_SWAPBUTTON):
            # Buttons swapped for a left hand: SendInput names the physical buttons, and Windows swaps them back.
            button = "right" if button == "left" else "left"
        down, up = _BUTTONS[button]
        with per_monitor_dpi():
            ax, ay = to_absolute(x, y, self._virtual_desktop(api))
            at = _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
            presses = [[self._mouse(api, ax, ay, at | down)], [self._mouse(api, ax, ay, at | up)]]
            self._send_held(api, presses * (2 if double else 1))

    def wheel(self, direction: Direction, notches: int) -> None:
        """Turn the wheel where the cursor is: up and right away from the user, as the hardware reports it."""
        api = _api()
        flag = _MOUSEEVENTF_WHEEL if direction in ("up", "down") else _MOUSEEVENTF_HWHEEL
        delta = WHEEL_DELTA if direction in ("up", "right") else -WHEEL_DELTA
        self._send(api, [self._mouse(api, 0, 0, flag, delta & 0xFFFFFFFF) for _ in range(notches)])

    def button(self, button: Button, down: bool) -> None:
        """Press or let go of ``button`` where the pointer is, on its own: for holding it (mining in a game)."""
        api = _api()
        if button != "middle" and api.user32.GetSystemMetrics(_SM_SWAPBUTTON):
            button = "right" if button == "left" else "left"
        press, release = _BUTTONS[button]
        self._send(api, [self._mouse(api, 0, 0, press if down else release)])

    def nudge(self, dx: int, dy: int) -> None:
        """Move the mouse by (dx, dy) counts, as the hardware reports motion. Games that hold the cursor still turn
        the camera by this raw motion, where an absolute move would do nothing (the pointer's own speed and
        acceleration still apply to the cursor, not to what games read)."""
        api = _api()
        self._send(api, [self._mouse(api, dx, dy, _MOUSEEVENTF_MOVE)])

    def keys(self, strokes: Sequence[Stroke]) -> int:
        """Send key strokes, each key held down ``TAP_S`` before it goes up (``held_apart``) so games see it, and
        everything between in one SendInput call so the user's own typing can't land in the middle. Returns the tick
        right after they went in (see ``Watch.acted``)."""
        api = _api()
        if any(stroke.scan for stroke in strokes):
            # Typed by scan code for a streamed app: a character's keys going down together (Shift and the key), then
            # going up together, each ``KEY_PACE_S`` after the last.
            paced: list[list[Any]] = []
            for i, stroke in enumerate(strokes):
                if not paced or stroke.up != strokes[i - 1].up:
                    paced.append([])
                paced[-1].append(self._stroke(api, stroke))
            self._send_held(api, paced, KEY_PACE_S)
            return int(api.kernel32.GetTickCount())
        batches = []
        for strokes_together in held_apart(strokes):
            events = []
            for stroke in strokes_together:
                if stroke.vk:
                    flags = (_KEYEVENTF_EXTENDEDKEY if stroke.vk in _EXTENDED else 0) | (
                        _KEYEVENTF_KEYUP if stroke.up else 0
                    )
                    scan = api.user32.MapVirtualKeyW(stroke.vk, _MAPVK_VK_TO_VSC)
                    events.append(self._key(api, stroke.vk, scan, flags))
                else:
                    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if stroke.up else 0)
                    events.append(self._key(api, 0, stroke.unit, flags))
            batches.append(events)
        if batches:
            self._send_held(api, batches)
        return int(api.kernel32.GetTickCount())

    def key_for(self, ch: str) -> tuple[int, bool] | None:
        """Where ``ch`` is on the keyboard layout of the window in front: its key's scan code and whether Shift goes
        with it; None when no key types it alone or with Shift (``scan_strokes``)."""
        if len(ch) != 1 or ord(ch) > 0xFFFF:
            return None
        api = _api()
        u = api.user32
        layout = u.GetKeyboardLayout(u.GetWindowThreadProcessId(u.GetForegroundWindow(), None))
        found = u.VkKeyScanExW(ch, layout)
        if found == -1:
            return None
        vk, shifts = found & 0xFF, (found >> 8) & 0xFF
        if shifts & ~1:  # Ctrl or Alt too (AltGr): would arrive as a shortcut, not a character
            return None
        if u.MapVirtualKeyExW(vk, _MAPVK_VK_TO_CHAR, layout) & _DEAD_KEY:
            return None
        scan = u.MapVirtualKeyExW(vk, _MAPVK_VK_TO_VSC_EX, layout)
        return (scan, bool(shifts & 1)) if scan else None

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _stroke(api: Any, stroke: Stroke) -> Any:
        """One stroke as an INPUT: by scan code, as a Unicode unit, or as a virtual key."""
        up = _KEYEVENTF_KEYUP if stroke.up else 0
        if stroke.scan:
            extended = _KEYEVENTF_EXTENDEDKEY if stroke.scan >> 8 == 0xE0 else 0
            return WindowsDesktop._key(api, 0, stroke.scan & 0xFF, _KEYEVENTF_SCANCODE | extended | up)
        if stroke.unit:
            return WindowsDesktop._key(api, 0, stroke.unit, _KEYEVENTF_UNICODE | up)
        extended = _KEYEVENTF_EXTENDEDKEY if stroke.vk in _EXTENDED else 0
        return WindowsDesktop._key(
            api, stroke.vk, api.user32.MapVirtualKeyW(stroke.vk, _MAPVK_VK_TO_VSC), extended | up
        )

    @staticmethod
    def _cursor(api: Any) -> tuple[int, int]:
        point = api.w.POINT()
        if not api.user32.GetCursorPos(api.ctypes.byref(point)):
            raise InputFailed("Windows couldn't say where the pointer is (is the PC locked?)")
        return point.x, point.y

    @staticmethod
    def _virtual_desktop(api: Any) -> Rect:
        metric = api.user32.GetSystemMetrics
        left, top = metric(_SM_XVIRTUALSCREEN), metric(_SM_YVIRTUALSCREEN)
        return left, top, left + metric(_SM_CXVIRTUALSCREEN), top + metric(_SM_CYVIRTUALSCREEN)

    @staticmethod
    def _mouse(api: Any, dx: int, dy: int, flags: int, data: int = 0) -> Any:
        return api.INPUT(
            type=_INPUT_MOUSE,
            mi=api.MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=MARK),
        )

    @staticmethod
    def _key(api: Any, vk: int, scan: int, flags: int) -> Any:
        return api.INPUT(
            type=_INPUT_KEYBOARD,
            ki=api.KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=MARK),
        )

    @staticmethod
    def _send(api: Any, events: Sequence[Any]) -> None:
        WindowsDesktop._send_held(api, [events])

    @staticmethod
    def _send_held(api: Any, batches: Sequence[Sequence[Any]], gap_s: float = TAP_S) -> None:
        """Send each batch in one SendInput call, ``gap_s`` apart. If Windows stops taking them part of the way,
        whatever is still down gets let go."""
        ctypes = api.ctypes
        total = sum(len(batch) for batch in batches)
        done: list[Any] = []
        for i, batch in enumerate(batches):
            if i:
                time.sleep(gap_s)
            array = (api.INPUT * len(batch))(*batch)
            sent = api.user32.SendInput(len(batch), array, ctypes.sizeof(api.INPUT))
            done.extend(batch[:sent])
            if sent != len(batch):
                error = ctypes.get_last_error()
                WindowsDesktop._release(api, WindowsDesktop._still_down(done))
                raise InputFailed(
                    f"Windows took {len(done)} of {total} input events (error {error}; is the PC locked?)"
                )

    @staticmethod
    def _still_down(events: Sequence[Any]) -> list[Any]:
        """The presses in ``events`` that nothing later in them let go of, in the order they went down."""
        down: dict[tuple[int, int, int], Any] = {}
        for event in events:
            if event.type == _INPUT_KEYBOARD:
                key = (_INPUT_KEYBOARD, event.ki.wVk, event.ki.wScan)
                if event.ki.dwFlags & _KEYEVENTF_KEYUP:
                    down.pop(key, None)
                else:
                    down[key] = event
            elif event.type == _INPUT_MOUSE:
                for press, release in _BUTTONS.values():
                    if event.mi.dwFlags & press:
                        down[(_INPUT_MOUSE, press, 0)] = event
                    if event.mi.dwFlags & release:
                        down.pop((_INPUT_MOUSE, press, 0), None)
        return list(down.values())

    @staticmethod
    def _release(api: Any, events: Sequence[Any]) -> None:
        """Let go of every key and button ``events`` pressed: a Ctrl left down would turn the user's own typing into
        shortcuts."""
        ups = []
        for event in events:
            if event.type == _INPUT_KEYBOARD and not event.ki.dwFlags & _KEYEVENTF_KEYUP:
                ki = event.ki
                ups.append(WindowsDesktop._key(api, ki.wVk, ki.wScan, ki.dwFlags | _KEYEVENTF_KEYUP))
            elif event.type == _INPUT_MOUSE:
                for down, up in _BUTTONS.values():
                    if event.mi.dwFlags & down:
                        ups.append(WindowsDesktop._mouse(api, event.mi.dx, event.mi.dy, event.mi.dwFlags & ~down | up))
        if ups:
            array = (api.INPUT * len(ups))(*ups)
            api.user32.SendInput(len(ups), array, api.ctypes.sizeof(api.INPUT))


# --------------------------------------------------------------------------- the takeover watch


def ms_between(earlier: int, later: int) -> int:
    """Milliseconds from ``earlier`` to ``later`` on Windows' 32-bit tick, which wraps every 49.7 days; negative
    when ``later`` is in fact earlier."""
    delta = (later - earlier) % 2**32
    return delta if delta < 2**31 else delta - 2**32


@dataclass(frozen=True)
class _Baseline:
    cursor: tuple[int, int]  # where the cursor was after her last action
    tick: int  # Windows' last-input tick right after her last input
    sent: int | None = None  # Windows' tick right after her last keys went in; None when her last input wasn't keys


class Watch:
    """Notices the user taking the mouse or keyboard back during a turn; she then stops for the rest of it.

    Before each action (and between typed chunks) ``check`` compares the world with the baseline ``acted`` set after
    her own last input: the cursor more than ``MOVED_PX`` from where she left it means the user moved the mouse;
    Windows having seen newer input than hers while the cursor barely moved (under ``STILL_PX``) means they typed,
    clicked or scrolled; a movement in between is a nudge of the desk, accepted as the new baseline. A modifier
    or mouse button held down right now counts too: it's the user, and it would change what her keys and clicks
    do. The first check of a turn only takes the baseline: before her first action the user may well have been
    using the mouse. Answering one of the assistant's questions isn't taking over (``listen``). Once taken over,
    ``check`` answers False until the turn changes. Thread-safe.

    Her keys can move the pointer themselves: E opening or closing Minecraft's inventory makes the game let go of
    the mouse or take it back, and either way it snaps the pointer to the middle of its window. So a pointer jump
    with no input stamped later than ``LATE_MS`` after her keys went in is the app answering them, and becomes the
    new baseline; the user's own mouse movement goes on past that, so it's still noticed, as their typing is.

    Not every input is someone at the desk: a wireless mouse going to sleep or waking up sends an event with no
    motion, no button and no key. So input with the pointer still counts only if a key or button went down since
    her last input (``WindowsDesktop.pressed_since_asked``); a lone blip without one is forgiven. Another app can
    clear the "pressed" bit Windows keeps, so a second motionless blip within ``BLIP_GAP_MS`` of a forgiven one is
    the user regardless: their typing goes on, where a mouse waking sends one event. A desktop without
    ``pressed_since_asked`` can't tell, so every input counts there.

    Nobody can take back what she hasn't taken yet: until her own input has gone in this turn, each of her calls
    starts with a fresh baseline (``begin``). A click refused because the screen changed sends nothing, and the user
    moving the mouse while she looks again, before the next call, isn't taking over; within a call (while the
    avatar points, say) it still is, and so is everything once she has moved, clicked or typed.
    """

    def __init__(self, desktop: Desktop, moved_px: float = MOVED_PX, still_px: float = STILL_PX) -> None:
        self.desktop = desktop
        self.moved_px = moved_px
        self.still_px = still_px
        self.reason = ""  # why she stopped, for the log
        self._lock = threading.Lock()
        self._turn: int | None = None
        self._base: _Baseline | None = None
        self._taken = False
        self._blip: int | None = None  # Windows' tick of the last motionless blip forgiven this turn
        self._acted = False  # her own input went in this turn
        self._buses: weakref.WeakSet[Any] = weakref.WeakSet()  # event buses whose answers it hears

    def taken_over(self, turn_id: int) -> bool:
        with self._lock:
            self._for(turn_id)
            return self._taken

    def begin(self, turn_id: int) -> None:
        """At the start of each of her calls: before her own input has gone in this turn, the next check takes the
        baseline afresh (see the class)."""
        with self._lock:
            self._for(turn_id)
            if not self._acted and not self._taken:
                self._base = None

    def check(self, turn_id: int, own: Collection[int] = ()) -> bool:
        """Whether she may go on in this turn. May raise OSError when Windows can't say (the PC is locked). ``own``:
        her own windows' processes (her chat window): the user typing or clicking there is talking to her, answering
        her question, not taking the mouse and keyboard back (``_with_her``)."""
        with self._lock:
            self._for(turn_id)
            if self._taken:
                return False
            held = self.desktop.held()
            cursor, tick = self.desktop.cursor(), self.desktop.last_input()
            if (held or (self._base is not None and tick != self._base.tick)) and self._with_her(own):
                self._base = _Baseline(cursor, tick)
                self._pressed()  # their keys and clicks in her window are no business of the task's
                return True
            if held:
                return self._take(f"{held} is held down")
            base = self._base
            if base is None:
                self._base = _Baseline(cursor, tick)
                self._pressed()  # presses before her first action are no business of hers
                return True
            moved = math.dist(cursor, base.cursor)
            if moved > self.moved_px:
                if base.sent is None or ms_between(base.sent, tick) > LATE_MS:
                    return self._take(f"the mouse moved {moved:.0f} px")
                log.debug("The pointer jumped %.0f px as her keys went in: the app moved it, not the user", moved)
                self._base = _Baseline(cursor, tick, base.sent)
                return True
            if tick != base.tick:
                if moved < self.still_px:
                    if reason := self._not_a_blip(tick):
                        return self._take(reason)
                    log.info("Input with no motion, key or click (a wireless mouse waking?): not the user taking over")
                self._base = _Baseline(cursor, tick)  # a nudge of the mouse or a blip: from here on
            return True

    def acted(self, turn_id: int, cursor: tuple[int, int] | None = None, sent: int | None = None) -> None:
        """After her own input, once Windows has taken it in: the new baseline. ``cursor`` is where she put the
        pointer; None when she didn't move it (typing), which keeps the old spot. ``sent`` is Windows' tick right
        after her keys went in: input stamped clearly later came from the user while hers was being taken in."""
        with self._lock:
            self._for(turn_id)
            if cursor is None:
                cursor = self._base.cursor if self._base is not None else self.desktop.cursor()
            tick = self.desktop.last_input()
            self._base = _Baseline(cursor, tick, sent)
            self._acted = True
            self._pressed()  # her own keys and clicks
            if sent is not None and not self._taken and ms_between(sent, tick) > LATE_MS:
                self._take("there was input from the user while she typed")

    def rebase(self) -> None:
        """Take the mouse and keyboard as they are now for the baseline: the user just answered a question, and
        typing "y" or clicking into the chat window to answer isn't taking over. A takeover already seen stays."""
        with self._lock:
            if self._base is None or self._taken:
                return
            try:
                self._base = _Baseline(self.desktop.cursor(), self.desktop.last_input())
                self._pressed()
            except OSError:
                log.debug("Couldn't read the mouse and keyboard after a question", exc_info=True)

    def listen(self, events: Any, answered: type, unasked: Collection[str] = ()) -> None:
        """Rebase whenever ``events`` (an EventBus) reports an ``answered`` event (PermissionResolved), except one
        with a reason in ``unasked``: approved without a question, so nobody touched anything. Once per bus."""
        with self._lock:
            if events in self._buses:
                return
            self._buses.add(events)
        skip = frozenset(unasked)

        def on_answer(event: Any) -> None:
            if getattr(event, "reason", "") not in skip:
                self.rebase()

        events.subscribe(on_answer, answered)

    def _with_her(self, own: Collection[int]) -> bool:
        """Whether what the user is doing is in one of her own windows (``own``): the window in front is hers (they
        clicked into her chat window to answer her). The pointer merely passing over one of hers isn't: it may be on
        its way anywhere. Call with the lock held."""
        front = getattr(self.desktop, "foreground_pid", None)
        if not own or front is None:
            return False
        try:
            return front() in own
        except OSError:
            return False

    def _for(self, turn_id: int) -> None:
        """Start afresh for a new turn. Call with the lock held."""
        if self._turn != turn_id:
            self._turn, self._base, self._taken, self.reason = turn_id, None, False, ""
            self._blip, self._acted = None, False

    def _pressed(self) -> str | None:
        """The desktop's ``pressed_since_asked`` (which also clears it), or None when it can't tell."""
        pressed = getattr(self.desktop, "pressed_since_asked", None)
        return pressed() if pressed is not None else None

    def _not_a_blip(self, tick: int) -> str | None:
        """Why input at ``tick`` with the pointer still is the user, or None for a lone blip (see the class)."""
        if getattr(self.desktop, "pressed_since_asked", None) is None:
            return "there was input from the user"
        if pressed := self._pressed():
            return f"{pressed} was pressed"
        if self._blip is not None and ms_between(self._blip, tick) < BLIP_GAP_MS:
            return "there was input from the user"
        self._blip = tick
        return None

    def _take(self, reason: str) -> bool:
        self._taken, self.reason = True, reason
        log.info("The user took over the mouse and keyboard (%s): computer control stops for this turn", reason)
        return False


async def wait_for_quiet(desktop: Desktop, own: Collection[int], quiet_ms: int = 1200, limit_s: float = 20.0) -> None:
    """Before a screen task's next mouse or keyboard step: while the user is typing or clicking in one of her own
    windows (``own``: her chat window, answering her), wait until they pause, at most ``limit_s``, so her click or
    keys don't take the focus from under their typing (the rest of their "yes, carry on" landing in the task's page).
    At once when her window isn't in front, or Windows can't say."""
    front = getattr(desktop, "foreground_pid", None)
    if not own or front is None:
        return
    loop = asyncio.get_running_loop()
    until = loop.time() + limit_s
    try:
        while loop.time() < until:
            if await asyncio.to_thread(front) not in own:
                return
            idle = ms_between(desktop.last_input(), desktop.tick())
            if idle >= quiet_ms and desktop.held() is None:
                return
            await asyncio.sleep(0.1)
    except OSError:
        return
    log.info("The user kept typing in her window for %.0f s: going on with the task's step", limit_s)


DESKTOP = WindowsDesktop()
WATCH = Watch(DESKTOP)  # one for all the control tools: taking over stops every one of them
