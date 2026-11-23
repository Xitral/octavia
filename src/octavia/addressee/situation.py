"""What's going on at the PC when something is overheard: the app in front, a call app on the microphone, and which
apps have been playing sound.

All of it is cheap and never reads inside a window: the foreground window's process, app name and title come from
``recall.capture.foreground`` (Win32 calls, no UI Automation, so a browser is never read). Audio comes from Windows'
per-app audio sessions (pycaw), polled every ``_POLL_S`` on one COM thread: an app counts as playing when its
session's peak meter went above ``_PLAYING_PEAK`` in the last ``_PLAYING_FOR_S``, and a call app counts as on a call
while it holds an active session on a microphone. A browser needs more: any tab can leave the mic open, so it also has
to have played sound (the other people) in the last ``_WEB_CALL_SOUND_S``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_POLL_S = 1.5
_PLAYING_FOR_S = 6.0  # sound this recent still counts: the user may have spoken over a quiet moment
_PLAYING_PEAK = 0.003  # a session's peak meter above this is sound, not a stream left open in silence
_WEB_CALL_SOUND_S = 60.0  # a browser on the mic that played sound this recently is a web call: the others can go quiet
_BROWSER = "a browser"
# Apps whose microphone use means a call (lowercase process names). A browser only with sound too (``_WEB_CALL_SOUND_S``).
CALL_APPS = {
    "discord.exe": "Discord", "discordptb.exe": "Discord", "discordcanary.exe": "Discord",
    "ms-teams.exe": "Teams", "teams.exe": "Teams", "zoom.exe": "Zoom", "slack.exe": "Slack",
    "skype.exe": "Skype", "telegram.exe": "Telegram", "whatsapp.exe": "WhatsApp", "signal.exe": "Signal",
    "steamwebhelper.exe": "Steam", "steam.exe": "Steam", "guilded.exe": "Guilded", "mumble.exe": "Mumble",
    "ts3client_win64.exe": "TeamSpeak", "teamspeak.exe": "TeamSpeak", "webex.exe": "Webex",
    "ciscocollabhost.exe": "Webex", "element.exe": "Element",
    "vivaldi.exe": _BROWSER, "chrome.exe": _BROWSER, "msedge.exe": _BROWSER, "firefox.exe": _BROWSER,
    "brave.exe": _BROWSER, "opera.exe": _BROWSER,
}  # fmt: skip
# Audio plumbing whose sessions carry everyone's sound (her voice too): never "an app playing".
_PLUMBING = {
    "audiodg.exe", "amdrsserv.exe", "voicemod.exe", "steelseriessonar.exe", "steelseriesgg.exe",
    "steelseriesengine.exe", "nvidia broadcast.exe", "nahimicsvc64.exe", "svchost.exe", "explorer.exe",
}  # fmt: skip


@dataclass(frozen=True)
class SessionReading:
    """One audio session at one moment."""

    pid: int
    process: str  # "Discord.exe"
    capture: bool  # on a microphone (else on speakers)
    active: bool
    peak: float


@dataclass
class Situation:
    """What the model is told about the moment, and (without the window title) what the log keeps."""

    app: str = ""  # the app in front ("Vivaldi"); "" when there's none
    title: str = ""  # its window title: told to the model, never logged
    fullscreen: bool = False
    call: list[str] = field(default_factory=list)  # call apps on the microphone ("Discord")
    playing: list[str] = field(default_factory=list)  # apps that have been playing sound ("Spotify")

    def logged(self) -> dict[str, Any]:
        out: dict[str, Any] = {"app": self.app}
        if self.fullscreen:
            out["fullscreen"] = True
        if self.call:
            out["call"] = self.call
        if self.playing:
            out["playing"] = self.playing
        return out


def _app_name(process: str) -> str:
    return process[:-4] if process.casefold().endswith(".exe") else process


class AudioWatch:
    """Polls the per-app audio sessions; ``call_apps`` and ``playing`` answer from the last readings."""

    def __init__(
        self,
        read: Callable[[], list[SessionReading]] | None = None,
        own_pids: Callable[[], Iterable[int]] = tuple,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._own_pids = own_pids
        self._clock = clock
        self._heard: dict[str, float] = {}  # app -> when it was last heard playing
        self._browser_heard: dict[str, float] = {}  # browser process -> when it last played sound
        self._calls: list[str] = []
        self._executor: ThreadPoolExecutor | None = None
        self._task: asyncio.Task[None] | None = None
        self._failures = 0
        self._users = 0  # the app's call check and the addressee shadow share one

    async def start(self) -> None:
        self._users += 1
        if self._task is not None:
            return
        if self._read is None:
            if sys.platform != "win32":
                return
            self._read = _windows_sessions
        self._executor = ThreadPoolExecutor(1, thread_name_prefix="octavia-addressee-audio", initializer=_init_com)
        self._task = asyncio.create_task(self._poll(), name="addressee-audio")

    async def close(self) -> None:
        self._users -= 1
        if self._users > 0:
            return
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    async def _poll(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                # Her own processes are listed there too: walking the process tree is too slow for the event loop.
                readings, own = await loop.run_in_executor(self._executor, self._read_all)
                self.take(readings, own)
                self._failures = 0
            except Exception as exc:
                self._failures += 1
                if self._failures in (1, 100):
                    log.warning("Couldn't read the apps' audio sessions: %r", exc)
            await asyncio.sleep(_POLL_S)

    def _read_all(self) -> tuple[list[SessionReading], set[int]]:
        """On the COM thread: the readings, and her own processes (whose sound is hers)."""
        assert self._read is not None
        try:
            own = set(self._own_pids())
        except Exception:
            own = set()
        return self._read(), own

    def take(self, readings: Iterable[SessionReading], own: set[int] | None = None) -> None:
        """One poll's readings."""
        now = self._clock()
        if own is None:
            own = set(self._own_pids())
        on_mic: list[str] = []
        for r in readings:
            if r.pid in own or not r.active:
                continue
            key = r.process.casefold()
            if r.capture:
                on_mic.append(key)
            elif key not in _PLUMBING and r.peak >= _PLAYING_PEAK:
                self._heard[_app_name(r.process)] = now
                if CALL_APPS.get(key) == _BROWSER:
                    self._browser_heard[key] = now
        calls: list[str] = []
        for key in on_mic:
            name = CALL_APPS.get(key)
            if name == _BROWSER and now - self._browser_heard.get(key, -_WEB_CALL_SOUND_S) >= _WEB_CALL_SOUND_S:
                continue  # a tab with the mic open, nothing more
            if name and name not in calls:
                calls.append(name)
        self._calls = calls

    def call_apps(self) -> list[str]:
        return list(self._calls)

    def playing(self) -> list[str]:
        now = self._clock()
        for app in [a for a, t in self._heard.items() if now - t > _PLAYING_FOR_S]:
            del self._heard[app]
        return sorted(self._heard)


def foreground() -> tuple[str, str, bool]:
    """The app in front, its window title and whether it covers the screen; ("", "", False) when none."""
    from octavia.recall import capture

    fg = capture.foreground()
    if fg is None:
        return "", "", False
    return fg.app or _app_name(fg.process), fg.title or "", bool(fg.fullscreen)


def _init_com() -> None:
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        log.debug("COM initialisation on the addressee audio thread failed", exc_info=True)


_process_names: dict[int, str] = {}


def _process(pid: int) -> str:
    name = _process_names.get(pid)
    if name is None:
        try:
            import psutil

            name = psutil.Process(pid).name()
        except Exception:
            name = ""
        if len(_process_names) > 512:
            _process_names.clear()
        _process_names[pid] = name
    return name


def _windows_sessions() -> list[SessionReading]:
    """Every app's audio session on every active speaker and microphone. On the COM thread, and nothing COM leaves it:
    on any failure the error is logged by its text alone and [] comes back, so no traceback carries a COM pointer to
    another thread, where releasing it would crash the process."""
    try:
        return _read_sessions()
    except Exception as exc:
        log.debug("Couldn't read the audio sessions: %r", exc)
        return []


def _read_sessions() -> list[SessionReading]:
    import comtypes
    from pycaw.constants import DEVICE_STATE, CLSID_MMDeviceEnumerator, EDataFlow
    from pycaw.pycaw import IAudioMeterInformation, IAudioSessionControl2, IAudioSessionManager2, IMMDeviceEnumerator

    enumerator = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    out: list[SessionReading] = []
    for flow in (EDataFlow.eRender.value, EDataFlow.eCapture.value):
        devices = enumerator.EnumAudioEndpoints(flow, DEVICE_STATE.ACTIVE.value)
        for i in range(devices.GetCount()):
            try:
                # QueryInterface, never ctypes.cast: a cast pointer is released once more than it was taken, and
                # the extra Release crashes the process wherever the garbage collector happens to run.
                manager = devices.Item(i).Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
                manager = manager.QueryInterface(IAudioSessionManager2)
                sessions = manager.GetSessionEnumerator()
                count = sessions.GetCount()
            except Exception as exc:
                log.debug("Skipped an audio device: %r", exc)
                continue
            for j in range(count):
                try:
                    control = sessions.GetSession(j)
                    pid = int(control.QueryInterface(IAudioSessionControl2).GetProcessId() or 0)
                    if not pid:
                        continue
                    active = int(control.GetState()) == 1  # AudioSessionStateActive
                    peak = float(control.QueryInterface(IAudioMeterInformation).GetPeakValue()) if active else 0.0
                except Exception as exc:
                    log.debug("Skipped an audio session: %r", exc)
                    continue
                out.append(SessionReading(pid, _process(pid), flow == EDataFlow.eCapture.value, active, peak))
    return out
