"""Win32 platform services, all through ctypes (no hooks, no admin).

* ``SingleInstance``: a named mutex, so two copies never fight over the mic.
* ``disable_power_throttling``: opt out of EcoQoS, which slows windowless
  background processes and would starve wake-word detection.
* ``parse_hotkey`` and ``HotkeyService``: global shortcuts via
  ``RegisterHotKey`` on a hidden top-level window that also receives
  ``WM_QUERYENDSESSION``/``WM_ENDSESSION`` for a clean exit on logoff or
  shutdown. Console control handlers are not delivered once user32 is loaded,
  and message-only windows miss those broadcasts, hence a real hidden window.
* Window enumeration, focus and polite close (``WM_CLOSE``), a one-syscall
  process snapshot and media-key and shortcut taps, used by the app and system tools.
* A window's process and class, and how long the user has been idle
  (``GetLastInputInfo``: no hook), for the avatar watching the window in front;
  whether Ctrl is held (``GetAsyncKeyState``), for the pointer resting on her.

Every foreign function gets explicit ``argtypes``/``restype`` on private
``WinDLL`` instances, so 64-bit handles are never truncated and other modules
using ``ctypes.windll`` are unaffected. The module imports on any platform;
the functions raise ``OSError`` (or report failure) off Windows.
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
ERROR_ALREADY_EXISTS = 183
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

WM_DESTROY, WM_CLOSE = 0x0002, 0x0010
WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016
WM_HOTKEY = 0x0312
WM_APP_STOP = 0x8000 + 1
ENDSESSION_LOGOFF = 0x80000000

VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB2, 0xB3
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12  # VK_MENU is Alt

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW, _WS_EX_APPWINDOW, _WS_EX_NOACTIVATE = 0x80, 0x40000, 0x08000000
_GW_OWNER = 4
_DWMWA_CLOAKED = 14
_SW_RESTORE = 9

if IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi")
    _ntdll = ctypes.WinDLL("ntdll")

    LRESULT = ctypes.c_ssize_t
    ULONG_PTR = ctypes.c_size_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", wintypes.ULONG),
            ("NumberOfThreads", wintypes.ULONG),
            ("WorkingSetPrivateSize", ctypes.c_longlong),
            ("HardFaultCount", wintypes.ULONG),
            ("NumberOfThreadsHighWatermark", wintypes.ULONG),
            ("CycleTime", ctypes.c_ulonglong),
            ("CreateTime", ctypes.c_longlong),
            ("UserTime", ctypes.c_longlong),
            ("KernelTime", ctypes.c_longlong),
            ("ImageName", UNICODE_STRING),
            ("BasePriority", ctypes.c_long),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
            ("HandleCount", wintypes.ULONG),
            ("SessionId", wintypes.ULONG),
            ("UniqueProcessKey", ctypes.c_size_t),
            ("PeakVirtualSize", ctypes.c_size_t),
            ("VirtualSize", ctypes.c_size_t),
            ("PageFaultCount", wintypes.ULONG),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivatePageCount", ctypes.c_size_t),
        ]

    def _sig(fn, argtypes: list, restype) -> None:
        fn.argtypes = argtypes
        fn.restype = restype

    _sig(_kernel32.CreateMutexW, [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE)
    _sig(_kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
    _sig(_kernel32.GetCurrentProcess, [], wintypes.HANDLE)
    _sig(_kernel32.GetCurrentThreadId, [], wintypes.DWORD)
    _sig(
        _kernel32.SetProcessInformation, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL
    )
    _sig(_kernel32.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HMODULE)
    _sig(_kernel32.GetTickCount, [], wintypes.DWORD)

    _sig(_user32.RegisterClassExW, [ctypes.POINTER(WNDCLASSEXW)], wintypes.ATOM)
    _sig(_user32.UnregisterClassW, [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL)
    _sig(
        _user32.CreateWindowExW,
        [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ],
        wintypes.HWND,
    )  # fmt: skip
    _sig(_user32.DestroyWindow, [wintypes.HWND], wintypes.BOOL)
    _sig(_user32.DefWindowProcW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], LRESULT)
    _sig(
        _user32.GetMessageW, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], wintypes.BOOL
    )
    _sig(_user32.TranslateMessage, [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL)
    _sig(_user32.DispatchMessageW, [ctypes.POINTER(wintypes.MSG)], LRESULT)
    _sig(_user32.PostMessageW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
    _sig(_user32.PostQuitMessage, [ctypes.c_int], None)
    _sig(_user32.RegisterHotKey, [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL)
    _sig(_user32.UnregisterHotKey, [wintypes.HWND, ctypes.c_int], wintypes.BOOL)

    _sig(_user32.EnumWindows, [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
    _sig(_user32.EnumChildWindows, [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
    _sig(_user32.IsWindow, [wintypes.HWND], wintypes.BOOL)
    _sig(_user32.IsWindowVisible, [wintypes.HWND], wintypes.BOOL)
    _sig(_user32.IsIconic, [wintypes.HWND], wintypes.BOOL)
    _sig(_user32.GetWindowTextLengthW, [wintypes.HWND], ctypes.c_int)
    _sig(_user32.GetWindowTextW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
    _sig(_user32.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
    _sig(_user32.GetClassNameW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
    _sig(_user32.GetLastInputInfo, [ctypes.POINTER(LASTINPUTINFO)], wintypes.BOOL)
    _sig(_user32.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short)
    _sig(_user32.GetWindowLongPtrW, [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t)
    _sig(_user32.GetWindow, [wintypes.HWND, wintypes.UINT], wintypes.HWND)
    _sig(_user32.GetForegroundWindow, [], wintypes.HWND)
    _sig(_user32.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL)
    _sig(_user32.BringWindowToTop, [wintypes.HWND], wintypes.BOOL)
    _sig(_user32.ShowWindow, [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    _sig(_user32.AttachThreadInput, [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)
    _sig(_user32.SendInput, [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wintypes.UINT)
    _sig(_dwmapi.DwmGetWindowAttribute, [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], ctypes.c_long)
    _sig(
        _ntdll.NtQuerySystemInformation,
        [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)],
        ctypes.c_ulong,
    )


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise OSError("this feature needs Windows")


# --------------------------------------------------------------------------- single instance


class SingleInstance:
    """Named-mutex guard. Keep the object alive for the whole process lifetime.

    The OS releases the mutex when the process dies, even on a crash, so a
    stale lock can never block the next start. The ``Local\\`` namespace makes
    it per logon session.
    """

    def __init__(self, name: str = "OctaviaVoiceAssistant") -> None:
        self.name = name if "\\" in name else "Local\\" + name
        self.handle: int | None = None
        self.already_running = False
        if not IS_WINDOWS:
            log.debug("SingleInstance is a no-op off Windows")
            return
        handle = _kernel32.CreateMutexW(None, False, self.name)
        err = ctypes.get_last_error()  # must be read straight after the call
        if not handle:
            raise ctypes.WinError(err)
        self.handle = handle
        self.already_running = err == ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- power throttling


def disable_power_throttling() -> bool:
    """Opt this process out of EcoQoS execution-speed and timer throttling.

    Returns False (and logs why) instead of raising, since this is only an
    optimisation and older Windows builds lack the information class.
    """
    if not IS_WINDOWS:
        return False
    state = PROCESS_POWER_THROTTLING_STATE(1, 0x1 | 0x4, 0)  # EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION, both off
    ok = _kernel32.SetProcessInformation(
        _kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state)
    )  # 4 = ProcessPowerThrottling
    if not ok:
        log.info("Could not disable power throttling (Windows error %d)", ctypes.get_last_error())
        return False
    return True


# --------------------------------------------------------------------------- hotkeys

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "ctl": MOD_CONTROL,
    "alt": MOD_ALT, "menu": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}  # fmt: skip

_NAMED_KEYS = {
    "space": 0x20, "spacebar": 0x20,
    "backspace": 0x08, "back": 0x08,
    "enter": 0x0D, "return": 0x0D,
    "escape": 0x1B, "esc": 0x1B,
    "tab": 0x09,
    "pause": 0x13, "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    ";": 0xBA, "semicolon": 0xBA,
    "=": 0xBB, "equals": 0xBB, "equal": 0xBB, "plus": 0xBB, "+": 0xBB,
    ",": 0xBC, "comma": 0xBC,
    "-": 0xBD, "minus": 0xBD, "dash": 0xBD, "hyphen": 0xBD,
    ".": 0xBE, "period": 0xBE, "dot": 0xBE,
    "/": 0xBF, "slash": 0xBF,
    "`": 0xC0, "backquote": 0xC0, "backtick": 0xC0, "grave": 0xC0, "tilde": 0xC0,
    "[": 0xDB, "bracketleft": 0xDB, "lbracket": 0xDB,
    "\\": 0xDC, "backslash": 0xDC,
    "]": 0xDD, "bracketright": 0xDD, "rbracket": 0xDD,
    "'": 0xDE, "quote": 0xDE, "apostrophe": 0xDE,
}  # fmt: skip

_KEY_HELP = (
    "letters, digits, f1-f24, space, backspace, enter, escape, tab, arrows or punctuation like ; = , - . / [ ] '"
)


def _key_vk(token: str) -> int | None:
    if len(token) == 1 and ("a" <= token <= "z" or "0" <= token <= "9"):
        return ord(token.upper())
    if token.startswith("f") and token[1:].isdigit() and 1 <= int(token[1:]) <= 24:
        return 0x70 + int(token[1:]) - 1  # VK_F1 = 0x70 ... VK_F24 = 0x87
    return _NAMED_KEYS.get(token)


def parse_hotkey(combo: str) -> tuple[int, int]:
    """Parse ``"ctrl+alt+space"`` into ``(modifiers | MOD_NOREPEAT, virtual_key)``.

    Raises ValueError with a message meant for the user's config file.
    """
    text = " ".join(combo.lower().split())
    if not text:
        raise ValueError("hotkey is empty; use something like 'ctrl+alt+space'")
    raw = text.replace(" ", "")
    if raw.endswith("++"):  # "ctrl++" means ctrl + the plus key
        tokens = [*raw[:-2].split("+"), "plus"]
    else:
        tokens = raw.split("+")
    if any(not t for t in tokens):
        raise ValueError(f"hotkey {combo!r} has an empty part; write keys joined by '+', like 'ctrl+alt+space'")

    mods = 0
    keys: list[tuple[str, int]] = []
    for token in tokens:
        if token in _MODIFIERS:
            mods |= _MODIFIERS[token]
            continue
        vk = _key_vk(token)
        if vk is None:
            raise ValueError(f"unknown key {token!r} in hotkey {combo!r}; use {_KEY_HELP}")
        keys.append((token, vk))
    if not keys:
        raise ValueError(f"hotkey {combo!r} has no key besides modifiers; add one, like 'ctrl+alt+space'")
    if len(keys) > 1:
        names = " and ".join(t for t, _ in keys)
        raise ValueError(f"hotkey {combo!r} has more than one key ({names}); use modifiers plus exactly one key")
    token, vk = keys[0]
    is_function_key = 0x70 <= vk <= 0x87
    if not mods and not is_function_key:
        raise ValueError(
            f"hotkey {combo!r} needs a modifier (ctrl, alt, shift or win) so it doesn't steal normal typing"
        )
    if mods == MOD_SHIFT and not is_function_key:
        raise ValueError(f"hotkey {combo!r} would block typing '{token}' with shift; add ctrl, alt or win")
    return mods | MOD_NOREPEAT, vk


_class_counter = itertools.count(1)


class HotkeyService:
    """Global hotkeys plus logoff/shutdown notification on a hidden window thread.

    ``on_hotkey(name)`` runs on the window thread, so it should only hand the
    event to asyncio (``loop.call_soon_threadsafe``). ``on_session_end(reason)``
    ("logoff" or "shutdown") is called synchronously from ``WM_ENDSESSION`` and
    may block for a few seconds to finish cleanup: Windows can end the process
    as soon as the handler returns.
    """

    def __init__(
        self,
        bindings: dict[str, str],
        on_hotkey: Callable[[str], None],
        on_session_end: Callable[[str], None],
    ) -> None:
        self.bindings = dict(bindings)
        self.on_hotkey = on_hotkey
        self.on_session_end = on_session_end
        self.registered: dict[str, str] = {}
        self._ids: dict[int, str] = {}
        self._keys: dict[int, tuple[int, int]] = {}
        self._reg_errors: dict[int, int] = {}
        self._hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._fatal: str | None = None
        self._in_session_end = False
        self._class_name = f"OctaviaHotkeyWindow-{os.getpid()}-{next(_class_counter)}"
        self._wndproc = WNDPROC(self._proc) if IS_WINDOWS else None  # must outlive the window

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 5.0) -> dict[str, str]:
        """Create the window and register hotkeys. Returns ``{binding: problem}``; empty means all good."""
        if self._thread is not None:
            raise RuntimeError("HotkeyService already started")
        errors: dict[str, str] = {}
        seen: dict[tuple[int, int], str] = {}
        for name, combo in self.bindings.items():
            if not combo or not combo.strip():
                continue  # "" disables a binding
            try:
                key = parse_hotkey(combo)
            except ValueError as exc:
                errors[name] = str(exc)
                continue
            if key in seen:
                errors[name] = f"same shortcut as '{seen[key]}'"
                continue
            seen[key] = name
            hk_id = len(self._ids) + 1
            self._ids[hk_id] = name
            self._keys[hk_id] = key
        if not IS_WINDOWS:
            errors.update({name: "global hotkeys need Windows" for name in self._ids.values()})
            return errors

        self._thread = threading.Thread(target=self._run, name="octavia-hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            errors.update({name: "hotkey window did not start" for name in self._ids.values()})
            return errors
        if self._fatal:
            errors.update({name: self._fatal for name in self._ids.values()})
            return errors
        for hk_id, name in self._ids.items():
            code = self._reg_errors.get(hk_id)
            if code is None:
                self.registered[name] = self.bindings[name]
            elif code == ERROR_HOTKEY_ALREADY_REGISTERED:
                errors[name] = "already used by another app"
            else:
                errors[name] = f"Windows refused it (error {code})"
        return errors

    def stop(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_APP_STOP, 0, 0)
        # Joining from the window thread, or while it waits inside on_session_end, would deadlock.
        if thread is not threading.current_thread() and not self._in_session_end:
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Hotkey window thread did not stop within %.1fs", timeout)
        self._thread = None
        self.registered.clear()

    def _proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_HOTKEY:
                name = self._ids.get(int(wparam))
                if name is not None:
                    self.on_hotkey(name)
                return 0
            if msg == WM_QUERYENDSESSION:
                return 1  # allow it; cleanup happens in WM_ENDSESSION
            if msg == WM_ENDSESSION:
                if wparam:
                    reason = "logoff" if int(lparam) & ENDSESSION_LOGOFF else "shutdown"
                    self._in_session_end = True
                    try:
                        self.on_session_end(reason)
                    finally:
                        self._in_session_end = False
                return 0
            if msg == WM_APP_STOP:
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                for hk_id in self._keys:
                    _user32.UnregisterHotKey(hwnd, hk_id)
                _user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("Hotkey window callback failed (message 0x%04X)", msg)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        hinst = _kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(
            cbSize=ctypes.sizeof(WNDCLASSEXW),
            lpfnWndProc=self._wndproc,
            hInstance=hinst,
            lpszClassName=self._class_name,
        )
        if not _user32.RegisterClassExW(ctypes.byref(wc)):
            self._fatal = f"could not register the hotkey window class (error {ctypes.get_last_error()})"
            self._ready.set()
            return
        try:
            # Top-level (parent None), never shown. HWND_MESSAGE would miss the session-end broadcast.
            hwnd = _user32.CreateWindowExW(0, self._class_name, "Octavia", 0, 0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                self._fatal = f"could not create the hotkey window (error {ctypes.get_last_error()})"
                self._ready.set()
                return
            self._hwnd = hwnd
            for hk_id, (mods, vk) in self._keys.items():
                if not _user32.RegisterHotKey(hwnd, hk_id, mods, vk):
                    self._reg_errors[hk_id] = ctypes.get_last_error()
            self._ready.set()
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._hwnd = None
            _user32.UnregisterClassW(self._class_name, hinst)


# --------------------------------------------------------------------------- windows


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process: str  # image name, e.g. "Spotify.exe"
    exe: str | None  # full path when readable
    minimized: bool


def _window_title(hwnd: int) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD()
    hr = _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return hr == 0 and cloaked.value != 0


def _is_alt_tab_window(hwnd: int) -> bool:
    """The windows Alt-Tab would show: visible, not cloaked, not tool/owned popups, titled."""
    if not _user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
        return False
    ex = _user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    if ex & _WS_EX_TOOLWINDOW:
        return False
    if _user32.GetWindow(hwnd, _GW_OWNER) and not ex & _WS_EX_APPWINDOW:
        return False
    if ex & _WS_EX_NOACTIVATE and not ex & _WS_EX_APPWINDOW:
        return False
    return bool(_window_title(hwnd))


def _process_identity(pid: int) -> tuple[str, str | None]:
    import psutil

    try:
        proc = psutil.Process(pid)
        name = proc.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "?", None
    try:
        exe = proc.exe() or None
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        exe = None
    return name, exe


def _uwp_real_pid(hwnd: int, frame_pid: int) -> int:
    """Store apps live in ApplicationFrameHost.exe; the app itself owns a child CoreWindow."""
    found = [frame_pid]

    @WNDENUMPROC
    def callback(child: int, _lparam: int) -> bool:
        pid = _window_pid(child)
        if pid != frame_pid:
            found[0] = pid
            return False
        return True

    _user32.EnumChildWindows(hwnd, callback, 0)
    return found[0]


def list_windows() -> list[WindowInfo]:
    """Alt-Tab windows in Z order (most recently active first)."""
    _require_windows()
    hwnds: list[int] = []

    @WNDENUMPROC
    def callback(hwnd: int, _lparam: int) -> bool:
        if _is_alt_tab_window(hwnd):
            hwnds.append(hwnd)
        return True

    _user32.EnumWindows(callback, 0)
    out: list[WindowInfo] = []
    for hwnd in hwnds:
        pid = _window_pid(hwnd)
        name, exe = _process_identity(pid)
        if name.lower() == "applicationframehost.exe":
            real = _uwp_real_pid(hwnd, pid)
            if real != pid:
                pid = real
                name, exe = _process_identity(pid)
        out.append(WindowInfo(hwnd, _window_title(hwnd), pid, name, exe, bool(_user32.IsIconic(hwnd))))
    return out


def foreground_window() -> int:
    _require_windows()
    return _user32.GetForegroundWindow() or 0


def window_pid(hwnd: int) -> int:
    """The process a window belongs to (0 when it's gone)."""
    _require_windows()
    return _window_pid(hwnd)


def window_class(hwnd: int) -> str:
    """A window's class name: "Shell_TrayWnd" for the taskbar, say ("" when it's gone)."""
    _require_windows()
    buf = ctypes.create_unicode_buffer(256)
    n = _user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value if n > 0 else ""


def idle_seconds() -> float:
    """How long since the user last pressed a key, clicked or moved the mouse, anywhere (0 when Windows can't say)."""
    _require_windows()
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0  # both wrap after 49.7 days


def ctrl_held() -> bool:
    """Whether a Ctrl key is down right now, whichever window has the keyboard (the avatar's never does)."""
    _require_windows()
    return bool(_user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)


def focus_window(hwnd: int) -> bool:
    """Restore and bring a window to the front; returns whether it really is in front now.

    Windows only lets the foreground process hand focus away, so a plain
    SetForegroundWindow from a background assistant is often refused (the
    taskbar button flashes instead). Attaching to the foreground thread's
    input state is the documented-behaviour workaround; no keystrokes are sent.
    """
    _require_windows()
    if not _user32.IsWindow(hwnd):
        return False
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
    if _user32.SetForegroundWindow(hwnd) and _is_foreground(hwnd):
        return True
    current = _user32.GetForegroundWindow()
    fg_thread = _user32.GetWindowThreadProcessId(current, None) if current else 0
    me = _kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and _user32.AttachThreadInput(me, fg_thread, True))
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(me, fg_thread, False)
    return _is_foreground(hwnd)


def hand_back_focus(hwnd: int) -> bool:
    """Give the keyboard back to the window that had it before she took it (a panel the user typed into): only when
    it still exists and isn't minimized, so a window they closed or put away never comes back."""
    _require_windows()
    if not _user32.IsWindow(hwnd) or _user32.IsIconic(hwnd):
        return False
    return focus_window(hwnd)


def _is_foreground(hwnd: int) -> bool:
    for _ in range(3):
        if _user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.03)
    return False


def close_window(hwnd: int) -> bool:
    """Ask a window to close, exactly like clicking X. The app may prompt to save; never terminates it.

    False means the request wasn't delivered; ``last_error()`` then says why
    (5 = the window belongs to an app running as administrator, 1400 = it's already gone).
    """
    _require_windows()
    return bool(_user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


ERROR_INVALID_WINDOW_HANDLE = 1400


def last_error() -> int:
    """The Windows error from the last failed user32 call on this thread."""
    return ctypes.get_last_error()


def window_is_open(hwnd: int) -> bool:
    """Still exists and is shown (apps that close to the tray just hide their window)."""
    _require_windows()
    return bool(_user32.IsWindow(hwnd) and _user32.IsWindowVisible(hwnd) and not _is_cloaked(hwnd))


@lru_cache(maxsize=256)
def exe_description(path: str) -> str | None:
    """The FileDescription from an executable's version resource ("Visual Studio Code" for Code.exe)."""
    if not IS_WINDOWS or not path:
        return None
    try:
        import win32api

        langs = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")
        for lang, codepage in langs or [(0x0409, 0x04B0)]:
            value = win32api.GetFileVersionInfo(path, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription")
            if value and value.strip():
                return value.strip()
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- processes


def process_snapshot() -> dict[int, tuple[str, int, int]]:
    """``{pid: (image_name, cpu_time_100ns, working_set_bytes)}`` from one NtQuerySystemInformation call.

    What Task Manager does: about 20 ms for 450 processes, where psutil needs
    seconds because it opens each process separately.
    """
    _require_windows()
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        needed = wintypes.ULONG()
        status = _ntdll.NtQuerySystemInformation(5, buf, size, ctypes.byref(needed))  # SystemProcessInformation
        if status == 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            size = max(size * 2, needed.value + 65536)
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation failed (NTSTATUS 0x{status:08X})")
        break
    out: dict[int, tuple[str, int, int]] = {}
    base, offset = ctypes.addressof(buf), 0
    while True:
        spi = SYSTEM_PROCESS_INFORMATION.from_address(base + offset)
        pid = spi.UniqueProcessId or 0
        if spi.ImageName.Buffer:
            name = ctypes.wstring_at(spi.ImageName.Buffer, spi.ImageName.Length // 2)
        else:
            name = "System Idle Process" if pid == 0 else ""
        out[pid] = (name, spi.UserTime + spi.KernelTime, spi.WorkingSetSize)
        if spi.NextEntryOffset == 0:
            return out
        offset += spi.NextEntryOffset


# --------------------------------------------------------------------------- media keys


def tap_key(vk: int) -> None:
    """Press and release a (media/volume) virtual key via SendInput."""
    _require_windows()
    flags = 0x0001  # KEYEVENTF_EXTENDEDKEY
    events = (INPUT * 2)(
        INPUT(type=1, ki=KEYBDINPUT(wVk=vk, dwFlags=flags)),
        INPUT(type=1, ki=KEYBDINPUT(wVk=vk, dwFlags=flags | 0x0002)),  # KEYEVENTF_KEYUP
    )
    if _user32.SendInput(2, events, ctypes.sizeof(INPUT)) != 2:
        raise ctypes.WinError(ctypes.get_last_error())


def tap_combo(vks: tuple[int, ...]) -> None:
    """Press a shortcut such as Alt+Shift+S in the foreground window: keys down in order, then up in reverse.

    One SendInput call, so the user's own typing can't land between the presses.
    """
    _require_windows()
    downs = [INPUT(type=1, ki=KEYBDINPUT(wVk=vk)) for vk in vks]
    ups = [INPUT(type=1, ki=KEYBDINPUT(wVk=vk, dwFlags=0x0002)) for vk in reversed(vks)]  # KEYEVENTF_KEYUP
    events = (INPUT * (2 * len(vks)))(*downs, *ups)
    if _user32.SendInput(len(events), events, ctypes.sizeof(INPUT)) != len(events):
        raise ctypes.WinError(ctypes.get_last_error())
