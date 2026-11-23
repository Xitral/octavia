"""Ducking: other apps play quieter while she speaks.

Windows keeps a volume for each app on each output device (its audio session, what the Volume Mixer shows). When her
speech starts, every other app that is playing is faded down to ``level`` of its own volume; when it ends, each fades
back up to exactly where it was. Only the other apps' sessions change, so her own voice and cues stay as loud as ever,
and an app whose volume the user moved in the meantime is left where they put it.

Sessions are looked for on every output device, not just the default one. With SteelSeries Sonar the default device
is one of Sonar's virtual channels (Chat, where her voice goes) while music and browsers play into Media and games into
Gaming, so the default device alone finds almost nothing. Sonar's own mixer (and other audio routers, like Voicemod)
hold sessions too, on the real headphones; those carry everything, her voice included, so they're never touched
(``ROUTERS``).

Start and end come from ``SpeakingStarted`` and ``SpeakingFinished``, which every reply, announcement and question
sends, and which end the same way after an interruption or an error. Going back up waits a moment (``hold_s``) so the
music doesn't swell between a question and the reply right after it. What is turned down is also written to a file
until it's put back, so a crash in mid-sentence is repaired at the next start.

Whether it's on, how far down, how fast and which apps are left alone live in ``data/ducking.json`` (the
``audio_ducking`` tool changes them); they're read at each duck, so a change applies the next time she speaks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from octavia.core.events import Event, EventBus, SpeakingFinished, SpeakingStarted
from octavia.core.jsonfile import read_json, write_json

if TYPE_CHECKING:
    from octavia.config import AppConfig

log = logging.getLogger(__name__)

T = TypeVar("T")

SETTINGS_FILE = "data/ducking.json"
STATE_FILE = "data/cache/ducked.json"
DEFAULT_LEVEL = 0.25
FADE_DOWN_S = 0.3
FADE_UP_S = 0.8
HOLD_S = 0.8
WATCH_S = 0.25  # how often, while she speaks, to look for apps that have just started playing
STEP_S = 0.03  # one volume change per step of a fade: smooth to the ear, a few dozen COM calls a second
# Windows hands back the volume it was given, but as a float32: anything this close is still what she set.
TOLERANCE = 0.01
_ACTIVE = 1  # AudioSessionStateActive
# Apps that carry other apps' sound (Sonar's mixer sends everything, her voice included, to the headphones) or record
# it: turning them down would turn her down too. In ``app_name`` form.
ROUTERS = frozenset(
    {
        "audiodg",
        "steelseriessonar",
        "steelseriesgg",
        "steelseriesggez",
        "steelseriesengine",
        "voicemod",
        "voicemeeter",
        "voicemeeterpro",
        "voicemeeter8",
        "voicemeeter8x64",
        "amdrsserv",  # AMD's recorder
        "obs64",
    }
)


def app_name(name: str) -> str:
    """How an app is matched: its process name, lower-case, without ``.exe``."""
    name = name.strip().lower()
    return name.removesuffix(".exe")


@dataclass
class DuckSettings:
    enabled: bool = True
    level: float = DEFAULT_LEVEL  # the share of its own volume another app keeps while she speaks
    fade_down_s: float = FADE_DOWN_S
    fade_up_s: float = FADE_UP_S
    skip: list[str] = field(default_factory=list)  # apps the user wants left alone (``app_name`` form)

    @classmethod
    def load(cls, path: Path) -> DuckSettings:
        data = read_json(path, "the ducking settings")
        if not isinstance(data, dict):
            return cls()
        enabled = data.get("enabled", True)
        skip = data.get("skip", [])
        return cls(
            enabled=enabled if isinstance(enabled, bool) else True,
            level=_number(data.get("level"), DEFAULT_LEVEL, 1.0),
            fade_down_s=_number(data.get("fade_down_s"), FADE_DOWN_S, 5.0),
            fade_up_s=_number(data.get("fade_up_s"), FADE_UP_S, 5.0),
            skip=[app_name(s) for s in skip if isinstance(s, str) and s.strip()] if isinstance(skip, list) else [],
        )

    def save(self, path: Path) -> None:
        write_json(
            path,
            {
                "enabled": self.enabled,
                "level": round(self.level, 3),
                "fade_down_s": round(self.fade_down_s, 3),
                "fade_up_s": round(self.fade_up_s, 3),
                "skip": sorted(set(self.skip)),
            },
            indent=2,
        )

    def skips(self, name: str) -> bool:
        key = app_name(name)
        return key in ROUTERS or key in self.skip


def _number(value: Any, default: float, top: float) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return default
    return min(top, max(0.0, float(value)))


@dataclass(frozen=True)
class AppAudio:
    key: str  # the audio session's instance identifier: one app can have several (one per device, or more)
    pid: int
    name: str
    volume: float


class AudioSessions(Protocol):
    """The apps' own volumes. Called from one thread only (COM)."""

    def playing(self) -> list[AppAudio]: ...

    def volume(self, key: str) -> float | None: ...

    def set_volume(self, key: str, volume: float) -> bool: ...


@dataclass
class _Ducked:
    before: float  # its volume before she spoke: where it goes back to
    target: float  # where it sits while she speaks
    last: float  # what she last set it to (anything else means the user moved it)
    start: float = 0.0  # where the current fade began


class AudioDucker:
    def __init__(
        self,
        events: EventBus,
        sessions: AudioSessions,
        settings_path: Path,
        state_path: Path,
        *,
        executor: Executor | None = None,
        own_pids: Callable[[], set[int]] | None = None,
        hold_s: float = HOLD_S,
        step_s: float = STEP_S,
        watch_s: float = WATCH_S,
    ) -> None:
        self.sessions = sessions
        self.settings_path = settings_path
        self.state_path = state_path
        self.hold_s = hold_s
        self.step_s = step_s
        self.watch_s = watch_s
        # One thread: every touch of the sessions and of ``_ducked`` happens there, in order, even when a fade that
        # was taken over (she spoke again while the music came back up) is cancelled between its steps.
        self._executor = executor or ThreadPoolExecutor(1, thread_name_prefix="octavia-ducking")
        self._own_pids = own_pids or _own_pids
        self._ducked: dict[str, _Ducked] = {}
        self._released: set[str] = set()  # moved by the user while turned down: theirs until everything is back up
        self._watch: asyncio.Task[None] | None = None
        self._fade: asyncio.Task[None] | None = None
        self._restore_due: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubscribe = events.subscribe(self._on_event, SpeakingStarted, SpeakingFinished)

    @property
    def ducked(self) -> dict[str, tuple[float, float]]:
        """Each app turned down: (its volume before, where it sits while she speaks)."""
        return {key: (d.before, d.target) for key, d in self._ducked.items()}

    async def start(self) -> None:
        """Put back what the last run turned down and never restored (it crashed while she was speaking)."""
        with contextlib.suppress(Exception):
            await self._call(self.sessions.playing)  # the first look at the devices is the slow one: not mid-sentence
        saved = read_json(self.state_path, "the apps ducking turned down")
        if not isinstance(saved, dict):
            return
        left: dict[str, tuple[float, float]] = {}
        for key, pair in saved.items():
            with contextlib.suppress(TypeError, ValueError):
                before, target = pair
                left[str(key)] = (float(before), float(target))
        try:
            await self._call(self._repair, left)
        except Exception:
            log.warning("Couldn't put back the volumes a crash left turned down", exc_info=True)

    async def close(self) -> None:
        self._unsubscribe()
        self._cancel_restore()
        self._stop_watching()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._fade_to(None, 0.0)
        if isinstance(self._executor, ThreadPoolExecutor):
            self._executor.shutdown(wait=False)

    def _on_event(self, event: Event) -> None:
        self._cancel_restore()
        if isinstance(event, SpeakingStarted):
            self._spawn(self.duck())
            if self._watch is None or self._watch.done():
                self._watch = asyncio.get_running_loop().create_task(self._watch_newcomers())
        else:
            self._stop_watching()
            loop = asyncio.get_running_loop()
            self._restore_due = loop.call_later(self.hold_s, lambda: self._spawn(self.restore()))

    def _cancel_restore(self) -> None:
        if self._restore_due is not None:
            self._restore_due.cancel()
            self._restore_due = None

    def _stop_watching(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None

    async def _watch_newcomers(self) -> None:
        """While she speaks: an app that starts playing after she began (a game's sound, a video in a new tab, music
        resumed) is faded down too, and put back with the rest."""
        me = asyncio.current_task()
        # Stopping cancels this task, but a cancel landing while it waits on a fade is swallowed there (the fade is
        # shielded), so it also checks that it's still the watcher before each look and each fade.
        while self._watch is me:
            await asyncio.sleep(self.watch_s)
            if self._watch is not me:
                return
            settings = DuckSettings.load(self.settings_path)
            if not settings.enabled or settings.level >= 1.0:
                continue
            try:
                found = await self._call(self._newcomers, settings)
            except Exception:
                log.debug("Couldn't look for apps that started playing", exc_info=True)
                continue
            if found and self._watch is me:
                await self._fade_to(settings, settings.fade_down_s)

    def _spawn(self, coro: Any) -> None:
        self._restore_due = None
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def duck(self) -> None:
        """Fade every other app that's playing down to the set level of its volume (again keeps it there)."""
        settings = DuckSettings.load(self.settings_path)
        if settings.enabled and settings.level < 1.0:
            await self._fade_to(settings, settings.fade_down_s)

    async def restore(self) -> None:
        """Fade each app turned down back to its volume, unless the user has moved it since. Always queued: a duck
        still looking for apps when this comes (a slow device scan) is undone too, once it has them."""
        await self._fade_to(None, DuckSettings.load(self.settings_path).fade_up_s)

    async def _fade_to(self, down: DuckSettings | None, seconds: float) -> None:
        """Fade down (with these settings) or back up (``down`` None), taking over from any fade still going: that
        one stops where it got to and this one goes on from there. A fade that is itself taken over just ends."""
        previous = self._fade
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.get_running_loop().create_task(self._run_fade(down, seconds, previous))
        self._fade = task
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    async def _run_fade(self, down: DuckSettings | None, seconds: float, after: asyncio.Task[None] | None) -> None:
        if after is not None and not after.done():
            await asyncio.wait([after])  # its step in hand finishes first (and this one's own cancel still lands)
        up = down is None
        try:
            if down is not None:
                await self._call(self._take, down)
            steps = max(1, round(seconds / self.step_s)) if seconds > 0 else 1
            if not await self._call(self._begin):
                if up:
                    await self._call(self._finish)  # nothing turned down, but what the user took back is free again
                return
            for i in range(1, steps + 1):
                if i > 1:
                    await asyncio.sleep(seconds / steps)
                await self._call(self._step, up, i / steps)
            if up:
                await self._call(self._finish)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Couldn't %s the other apps' volume", "put back" if up else "turn down", exc_info=True)

    async def _call(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    # Everything below runs on the ducking thread.

    def _take(self, settings: DuckSettings) -> None:
        own = self._own_pids()
        changed = False
        for app in self.sessions.playing():
            if app.pid in own or settings.skips(app.name) or app.key in self._released:
                continue
            ducked = self._ducked.get(app.key)
            if ducked is not None:
                target = ducked.before * settings.level  # the level may have changed since
                changed = changed or target != ducked.target
                ducked.target = target
            elif app.volume > 0:
                self._ducked[app.key] = _Ducked(app.volume, app.volume * settings.level, app.volume)
                changed = True
        if changed:  # the file's targets are what a crash repair goes by
            self._save_state()

    def _newcomers(self, settings: DuckSettings) -> bool:
        """Whether an app is playing that ``_take`` would turn down and hasn't yet."""
        own = self._own_pids()
        return any(
            app.key not in self._ducked
            and app.key not in self._released
            and app.volume > 0
            and app.pid not in own
            and not settings.skips(app.name)
            for app in self.sessions.playing()
        )

    def _begin(self) -> bool:
        for d in self._ducked.values():
            d.start = d.last
        return bool(self._ducked)

    def _step(self, up: bool, progress: float) -> None:
        dropped = False
        for key, d in list(self._ducked.items()):
            try:
                now = self.sessions.volume(key)
                if now is None:
                    continue  # gone for now (Windows keeps its volume): kept, to put back if it comes back
                if abs(now - d.last) > TOLERANCE:
                    del self._ducked[key]  # the user moved it: theirs now
                    self._released.add(key)
                    dropped = True
                    continue
                goal = d.before if up else d.target
                value = goal if progress >= 1 else _between(d.start, goal, progress)
                if value != d.last and self.sessions.set_volume(key, value):
                    d.last = value
            except Exception:
                log.debug("Couldn't change an app's volume", exc_info=True)  # kept: tried again next step
        if dropped:
            self._save_state()

    def _finish(self) -> None:
        """Forget the apps that are back where they were. One that couldn't be reached stays (and stays in the file,
        for the next restore or the next start)."""
        for key, d in list(self._ducked.items()):
            if d.last == d.before:
                del self._ducked[key]
        self._released.clear()
        self._save_state()

    def _repair(self, left: dict[str, tuple[float, float]]) -> None:
        for key, (before, target) in left.items():
            try:
                now = self.sessions.volume(key)
                # Anywhere between where it was going and where it was is hers: a crash can stop a fade halfway.
                if now is not None and min(before, target) - TOLERANCE <= now <= max(before, target) + TOLERANCE:
                    self.sessions.set_volume(key, before)
            except Exception:
                log.debug("Couldn't put an app's volume back", exc_info=True)
        self._save_state()

    def _save_state(self) -> None:
        try:
            if self._ducked:
                write_json(self.state_path, {k: [d.before, d.target] for k, d in self._ducked.items()})
            else:
                self.state_path.unlink(missing_ok=True)
        except OSError:
            log.debug("Couldn't note which apps are turned down", exc_info=True)


def _between(start: float, goal: float, progress: float) -> float:
    """``progress`` of the way from ``start`` to ``goal``, eased and even to the ear: loudness follows the ratio of
    volumes, so the fade is geometric (straight in decibels) when both ends are above silence."""
    eased = progress * progress * (3 - 2 * progress)
    if start > 1e-3 and goal > 1e-3:
        return start * (goal / start) ** eased
    return start + (goal - start) * eased


def _own_pids(roots: Iterable[int] = ()) -> set[int]:
    """This process, ``roots`` (the avatar app) and all their children: their sound is hers, never turned down."""
    pids = {os.getpid(), *roots}
    try:
        import psutil
    except ImportError:
        return pids
    for pid in list(pids):
        try:
            pids.update(child.pid for child in psutil.Process(pid).children(recursive=True))
        except Exception:
            log.debug("Couldn't list the children of process %d", pid, exc_info=True)
    return pids


class WindowsAudioSessions:
    """The per-app volumes on every active output device, through pycaw. Call it from one COM thread only."""

    def __init__(self) -> None:
        self._controls: dict[str, Any] = {}
        self._names: dict[int, str] = {}

    def playing(self) -> list[AppAudio]:
        return self._scan()

    def volume(self, key: str) -> float | None:
        control = self._control(key)
        return None if control is None else float(control.GetMasterVolume())

    def set_volume(self, key: str, volume: float) -> bool:
        control = self._control(key)
        if control is None:
            return False
        control.SetMasterVolume(min(1.0, max(0.0, volume)), None)
        return True

    def _control(self, key: str) -> Any:
        if key not in self._controls:
            self._scan()  # the app may have opened a new session since (or this is the next start)
        return self._controls.get(key)

    def _scan(self) -> list[AppAudio]:
        """Every session on every active output device; the playing ones come back. Each device has its own
        session for an app, so the same app can show up more than once."""
        import comtypes
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import AudioUtilities, IAudioSessionControl2, IAudioSessionManager2, ISimpleAudioVolume

        controls: dict[str, Any] = {}
        found: list[AppAudio] = []
        devices = AudioUtilities.GetDeviceEnumerator().EnumAudioEndpoints(
            EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value
        )
        for d in range(devices.GetCount()):
            try:
                manager = devices.Item(d).Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
                sessions = manager.QueryInterface(IAudioSessionManager2).GetSessionEnumerator()
                count = sessions.GetCount()
            except Exception:
                log.debug("Skipped an output device", exc_info=True)
                continue
            for i in range(count):
                try:
                    control = sessions.GetSession(i).QueryInterface(IAudioSessionControl2)
                    pid = int(control.GetProcessId() or 0)
                    if not pid:
                        continue  # system sounds
                    key = str(control.GetSessionInstanceIdentifier() or f"pid:{pid}")
                    volume = control.QueryInterface(ISimpleAudioVolume)
                    controls[key] = volume
                    if int(control.GetState()) == _ACTIVE:
                        found.append(AppAudio(key, pid, self._name(pid), float(volume.GetMasterVolume())))
                except Exception:
                    log.debug("Skipped an audio session", exc_info=True)
        self._controls = controls
        return found

    def _name(self, pid: int) -> str:
        if pid not in self._names:
            try:
                import psutil

                self._names[pid] = psutil.Process(pid).name()
            except Exception:
                return str(pid)
        return self._names[pid]


def _init_com() -> None:
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        log.debug("COM initialisation on the ducking thread failed", exc_info=True)


def create_ducker(
    config: AppConfig, events: EventBus, also_own: Callable[[], Iterable[int]] = tuple
) -> AudioDucker | None:
    """The ducker for this computer, or None where there are no per-app volumes (not Windows). ``also_own``: other
    processes whose sound is hers (the avatar app), asked at each duck since they come and go."""
    if sys.platform != "win32":
        return None
    executor = ThreadPoolExecutor(1, thread_name_prefix="octavia-ducking", initializer=_init_com)
    return AudioDucker(
        events,
        WindowsAudioSessions(),
        config.resolve_path(SETTINGS_FILE),
        config.resolve_path(STATE_FILE),
        executor=executor,
        own_pids=lambda: _own_pids(also_own()),
    )
