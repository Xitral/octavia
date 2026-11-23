"""System tools: status (CPU, memory, disk, uptime, network, top processes), media control and volume.

Top processes come from one ``NtQuerySystemInformation`` snapshot pair
instead of psutil's per-process calls, which take seconds for a normal user
on Windows. COM work runs on dedicated single threads with a known apartment:
pycaw needs COM initialised on its thread, and WinRT async calls must not run
on a single-threaded apartment that nobody pumps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, TypeVar

from pydantic import Field, model_validator

from octavia.platform import windows as win
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

T = TypeVar("T")

SAMPLE_S = 0.5  # one short window measures CPU, network and per-process CPU together
TOP_N = 5
VOLUME_STEP = 10

_PLAYING, _PAUSED, _STOPPED = 4, 5, 3  # GlobalSystemMediaTransportControlsSessionPlaybackStatus

_executors: dict[str, ThreadPoolExecutor] = {}
_executors_lock = threading.Lock()


def _executor(name: str, initializer: Callable[[], None]) -> ThreadPoolExecutor:
    with _executors_lock:
        pool = _executors.get(name)
        if pool is None:
            pool = ThreadPoolExecutor(1, thread_name_prefix=f"octavia-{name}", initializer=initializer)
            _executors[name] = pool
        return pool


def _init_sta() -> None:
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        log.debug("COM initialisation on the audio thread failed", exc_info=True)


def _init_mta() -> None:
    try:
        from winrt.runtime import ApartmentType, init_apartment

        init_apartment(ApartmentType.MULTI_THREADED)
    except Exception:
        log.debug("WinRT apartment initialisation failed", exc_info=True)


async def _on_audio_thread(fn: Callable[[], T]) -> T:
    return await asyncio.get_running_loop().run_in_executor(_executor("audio", _init_sta), fn)


async def _on_media_thread(coro_fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return await asyncio.get_running_loop().run_in_executor(
        _executor("media", _init_mta), lambda: asyncio.run(coro_fn())
    )


# --------------------------------------------------------------------------- status


def _snapshot() -> dict[int, tuple[str, int, int]]:
    """``{pid: (name, cpu_time_100ns, memory_bytes)}``: one syscall on Windows, psutil elsewhere."""
    if win.IS_WINDOWS:
        return win.process_snapshot()
    import psutil

    out: dict[int, tuple[str, int, int]] = {}
    for p in psutil.process_iter(["name", "cpu_times", "memory_info"]):
        info = p.info
        if info.get("cpu_times") is None or info.get("memory_info") is None:
            continue
        cpu = info["cpu_times"]
        out[p.pid] = (info.get("name") or "", int((cpu.user + cpu.system) * 1e7), info["memory_info"].rss)
    return out


def _top_processes(
    before: dict[int, tuple[str, int, int]], after: dict[int, tuple[str, int, int]], elapsed_s: float
) -> dict[str, list[dict[str, Any]]]:
    """Group by image name ("chrome" x 24 counts once) and rank by CPU and by memory."""
    ncpu = os.cpu_count() or 1
    wall = max(elapsed_s * 1e7 * ncpu, 1.0)
    grouped: dict[str, dict[str, Any]] = {}
    for pid, (name, cpu_time, mem) in after.items():
        if pid == 0 or not name or name.lower() in {"system idle process", "idle"}:
            continue
        label = name[:-4] if name.lower().endswith(".exe") else name
        g = grouped.setdefault(label.lower(), {"name": label, "cpu_percent": 0.0, "memory_bytes": 0, "count": 0})
        prev = before.get(pid)
        if prev is not None and prev[0] == name:
            g["cpu_percent"] += max(0.0, 100.0 * (cpu_time - prev[1]) / wall)
        g["memory_bytes"] += mem
        g["count"] += 1
    rows = list(grouped.values())
    for r in rows:
        r["cpu_percent"] = round(r["cpu_percent"], 1)
    by_cpu = [r for r in sorted(rows, key=lambda r: -r["cpu_percent"]) if r["cpu_percent"] >= 0.1][:TOP_N]
    by_mem = sorted(rows, key=lambda r: -r["memory_bytes"])[:TOP_N]
    return {"top_cpu": by_cpu, "top_memory": by_mem}


def collect_system_status(include_processes: bool = False) -> dict[str, Any]:
    """Blocking: samples for ``SAMPLE_S`` seconds. Run it in a worker thread."""
    import psutil

    before = None
    if include_processes:
        try:
            before = _snapshot()
        except Exception:
            log.warning("Process snapshot failed", exc_info=True)
    net_before = psutil.net_io_counters()
    started = time.monotonic()
    cpu = psutil.cpu_percent(interval=SAMPLE_S)
    elapsed = time.monotonic() - started
    net_after = psutil.net_io_counters()

    vm = psutil.virtual_memory()
    drive = os.environ.get("SystemDrive", "C:") + os.sep if win.IS_WINDOWS else "/"
    disk = psutil.disk_usage(drive)
    status: dict[str, Any] = {
        "cpu_percent": round(cpu, 1),
        "cpu_logical": psutil.cpu_count(),
        "cpu_physical": psutil.cpu_count(logical=False),
        "memory_used_bytes": vm.total - vm.available,
        "memory_total_bytes": vm.total,
        "memory_percent": round(vm.percent, 1),
        "disk": drive.rstrip("\\/") or "/",
        "disk_free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "disk_percent": round(disk.percent, 1),
        "uptime_s": int(time.time() - psutil.boot_time()),
        "battery": None,
        "net_down_bps": None,
        "net_up_bps": None,
    }
    if net_before is not None and net_after is not None and elapsed > 0:
        status["net_down_bps"] = round(8 * (net_after.bytes_recv - net_before.bytes_recv) / elapsed)
        status["net_up_bps"] = round(8 * (net_after.bytes_sent - net_before.bytes_sent) / elapsed)
    try:
        battery = psutil.sensors_battery()
    except Exception:
        battery = None
    if battery is not None:
        secs = battery.secsleft if isinstance(battery.secsleft, int) and battery.secsleft > 0 else None
        status["battery"] = {"percent": round(battery.percent), "plugged_in": battery.power_plugged, "secs_left": secs}
    if before is not None:
        try:
            after = _snapshot()
            status.update(_top_processes(before, after, time.monotonic() - started))
        except Exception:
            log.warning("Process snapshot failed", exc_info=True)
    return status


def _gb(n: float) -> str:
    gb = n / 1024**3
    if gb >= 1000:
        return f"{gb / 1024:.1f} TB"
    return f"{gb:.1f} GB" if gb < 100 else f"{gb:.0f} GB"


def _rate(bps: int) -> str:
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps / 1000:.0f} kbps"


def _duration(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [(days, "day"), (hours, "hour"), (minutes, "minute")]
    shown = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts if n][:2]
    return ", ".join(shown) or "less than a minute"


def format_status(s: dict[str, Any]) -> str:
    lines = [
        f"CPU {s['cpu_percent']:.0f}% ({s['cpu_logical']} threads).",
        f"Memory {s['memory_percent']:.0f}% used: {_gb(s['memory_used_bytes'])} of {_gb(s['memory_total_bytes'])}.",
        f"Drive {s['disk']} {_gb(s['disk_free_bytes'])} free of {_gb(s['disk_total_bytes'])} "
        f"({s['disk_percent']:.0f}% full).",
        f"Up {_duration(s['uptime_s'])}.",
    ]
    if s.get("net_down_bps") is not None:
        lines.append(f"Network {_rate(s['net_down_bps'])} down, {_rate(s['net_up_bps'])} up.")
    battery = s.get("battery")
    if battery:
        charge = "charging" if battery["plugged_in"] else "on battery"
        left = ""
        if battery.get("secs_left") and not battery["plugged_in"]:
            left = f", about {_duration(battery['secs_left'])} left"
        lines.append(f"Battery {battery['percent']}%, {charge}{left}.")
    if "top_cpu" in s:
        cpu = _process_list(s["top_cpu"], lambda r: f"{r['cpu_percent']:.0f}%")
        lines.append(f"Top CPU: {cpu or 'nothing busy'}.")
        lines.append(f"Top memory: {_process_list(s['top_memory'], lambda r: _gb(r['memory_bytes']))}.")
    return " ".join(lines)


def _process_list(rows: list[dict[str, Any]], value: Callable[[dict[str, Any]], str]) -> str:
    return ", ".join(f"{r['name']} {value(r)}" + (f" ({r['count']} processes)" if r["count"] > 1 else "") for r in rows)


class SystemStatusArgs(ToolArgs):
    include_processes: bool = Field(
        default=False, description="Also top processes by CPU/memory ('why is my PC slow')."
    )


class GetSystemStatus(Tool):
    name = "get_system_status"
    description = "CPU, memory, disk space, uptime, network speed and battery."
    Args = SystemStatusArgs
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: SystemStatusArgs, ctx: ToolContext) -> ToolResult:
        status = await asyncio.to_thread(collect_system_status, args.include_processes)
        return ToolResult.success(format_status(status), data=status)


# --------------------------------------------------------------------------- media

MediaAction = Literal["play", "pause", "play_pause", "next", "previous", "stop", "now_playing"]

APP_SESSION_WAIT_S = 5.0  # an app that was just opened (Spotify) takes a few seconds to register its media session
_APP_SESSION_POLL_S = 0.5

_MEDIA_KEYS = {
    "play": win.VK_MEDIA_PLAY_PAUSE,
    "pause": win.VK_MEDIA_PLAY_PAUSE,
    "play_pause": win.VK_MEDIA_PLAY_PAUSE,
    "next": win.VK_MEDIA_NEXT_TRACK,
    "previous": win.VK_MEDIA_PREV_TRACK,
    "stop": win.VK_MEDIA_STOP,
}
_MEDIA_LABELS = {
    "play": "play",
    "pause": "pause",
    "play_pause": "play/pause",
    "next": "next track",
    "previous": "previous track",
    "stop": "stop",
}


class MediaUnavailable(RuntimeError):
    """The Windows media-session API can't be used here (package missing, API failure)."""


def _source_name(app_id: str) -> str:
    """ "Spotify.exe" -> "Spotify"; "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify" -> "Spotify"."""
    name = app_id.rsplit("!", 1)[-1] if "!" in app_id else app_id
    name = name.rsplit("\\", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name or "the media app"


def _squash(text: str) -> str:
    return "".join(text.lower().split())


async def _session_manager() -> Any:
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

        return await Manager.request_async()
    except Exception as exc:  # ModuleNotFoundError when winrt-Windows.Foundation is missing
        raise MediaUnavailable(str(exc)) from exc


def _sessions(manager: Any) -> tuple[list[Any], bool]:
    """The media sessions, and whether that's all of them.

    Listing sessions returns a WinRT vector, which needs the winrt-Windows.Foundation.Collections projection; when it
    isn't installed the call raises ModuleNotFoundError. The current session (the app that played last) needs no
    collection, so it's still visible, and that covers the usual "pause Spotify" while Spotify is the one playing.
    """
    try:
        return list(manager.get_sessions()), True
    except ImportError as exc:
        log.debug("Can't list media sessions (%s); only the current one is visible", exc)
        current = manager.get_current_session()
        return ([current] if current is not None else []), False


def _find_session(manager: Any, app: str) -> Any:
    """The media session whose app id names ``app`` ("spotify" matches "Spotify.exe"), or None."""
    needle = _squash(app)
    for session in _sessions(manager)[0]:
        if needle and needle in _squash(session.source_app_user_model_id or ""):
            return session
    return None


async def _wait_for_session(manager: Any, app: str) -> Any:
    deadline = time.monotonic() + APP_SESSION_WAIT_S
    while (session := _find_session(manager, app)) is None and time.monotonic() < deadline:
        await asyncio.sleep(_APP_SESSION_POLL_S)
    return session


async def _media_session(action: str) -> ToolResult:
    """Talk to the current media session (the app that owns the media overlay)."""
    manager = await _session_manager()
    return await _control_session(manager.get_current_session(), action)


async def _app_media_session(action: str, app: str) -> ToolResult:
    """Talk to one named app's media session, so a browser that played last isn't the one resumed."""
    manager = await _session_manager()
    session = await _wait_for_session(manager, app)
    if session is None:
        sessions, complete = _sessions(manager)
        others = sorted({_source_name(s.source_app_user_model_id or "") for s in sessions})
        if not complete:
            last = f" (the last one was {others[0]})" if others else ""
            return ToolResult.error(
                f"I can only reach the app that played media last{last} on this system, and it isn't {app}. "
                f"Start playback in {app} once, then I can control it.",
                data={"app": app, "sessions": others, "method": "current_session_only"},
            )
        known = f" Apps with media right now: {', '.join(others)}." if others else ""
        return ToolResult.error(
            f"{app} has no media session, so I can't control it. If it isn't open, open it first.{known}",
            data={"app": app, "sessions": others},
        )
    return await _control_session(session, action)


async def _control_session(session: Any, action: str) -> ToolResult:
    if session is None:
        if action == "now_playing":
            return ToolResult.success("Nothing is playing right now.", data={"playing": False})
        return ToolResult.error("No app is playing media right now, so there's nothing to control.")
    source = _source_name(session.source_app_user_model_id or "")
    status = int(session.get_playback_info().playback_status)

    if action == "now_playing":
        props = await session.try_get_media_properties_async()
        title, artist, album = ((value or "").strip() for value in (props.title, props.artist, props.album_title))
        state = {_PLAYING: "Playing", _PAUSED: "Paused", _STOPPED: "Stopped"}.get(status, "Open")
        what = f"{title} by {artist}" if title and artist else title or "something untitled"
        data = {"playing": status == _PLAYING, "title": title, "artist": artist, "album": album, "app": source}
        return ToolResult.success(f"{state} in {source}: {what}.", data=data)

    if action == "play" and status == _PLAYING:
        return ToolResult.success(f"{source} is already playing.", data={"app": source, "playing": True})
    if action == "pause" and status != _PLAYING:
        return ToolResult.success(f"{source} isn't playing.", data={"app": source, "playing": False})
    calls = {
        "play": session.try_play_async,
        "pause": session.try_pause_async,
        "play_pause": session.try_toggle_play_pause_async,
        "next": session.try_skip_next_async,
        "previous": session.try_skip_previous_async,
        "stop": session.try_stop_async,
    }
    if not await calls[action]():
        return ToolResult.error(f"{source} didn't accept {_MEDIA_LABELS[action]}.")
    if action in ("play", "pause", "play_pause"):
        playing = action == "play" or (action == "play_pause" and status != _PLAYING)
        return ToolResult.success(
            f"{'Resumed' if playing else 'Paused'} {source}.", data={"app": source, "playing": playing}
        )
    done = {"next": "Skipped to the next track", "previous": "Went back a track", "stop": "Stopped playback"}[action]
    return ToolResult.success(f"{done} in {source}.", data={"app": source})


async def app_now_playing(app: str) -> dict[str, Any] | None:
    """``{"playing", "title", "artist"}`` for one app's media session, or None when it has none (yet).

    Doesn't wait for the session to appear. Raises ``MediaUnavailable`` when the media-session API can't be used.
    """

    async def read() -> dict[str, Any] | None:
        session = _find_session(await _session_manager(), app)
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        return {
            "playing": int(session.get_playback_info().playback_status) == _PLAYING,
            "title": (props.title or "").strip(),
            "artist": (props.artist or "").strip(),
        }

    try:
        return await _on_media_thread(read)
    except ImportError as exc:
        raise MediaUnavailable(str(exc)) from exc


class MediaArgs(ToolArgs):
    action: MediaAction = Field()
    app: str | None = Field(
        default=None,
        max_length=60,
        description=(
            "Target app, e.g. 'Spotify'. Set it when the user names or just opened one; otherwise the last-played "
            "app (often a browser) is controlled."
        ),
    )


class MediaControl(Tool):
    name = "media_control"
    description = (
        "Control or query media playback (Spotify, browser video...). For a specific song/album/playlist use "
        "play_on_spotify; for Spotify shuffle/volume, set_spotify_playback."
    )
    Args = MediaArgs
    risk = RiskLevel.SAFE
    category = "media"
    timeout_s = 30.0  # Spotify's Web API may have to start the app before it has a device to play on

    async def run(self, args: MediaArgs, ctx: ToolContext) -> ToolResult:
        if args.app and "spotify" in _squash(args.app):
            # Spotify's Web API reaches its playback even when another app owns the media overlay.
            from octavia.tools.extensions.spotify_player import control_spotify

            return await control_spotify(args.action, ctx, lambda: self._control_locally(args))
        return await self._control_locally(args)

    async def _control_locally(self, args: MediaArgs) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.error("Media control only works on Windows.")
        app = args.app
        try:
            if app:
                return await _on_media_thread(lambda: _app_media_session(args.action, app))
            return await _on_media_thread(lambda: _media_session(args.action))
        except (MediaUnavailable, ImportError) as exc:  # a winrt projection the call needed isn't installed
            log.info("Media session API unavailable (%s); falling back to media keys", exc)
        if args.action == "now_playing":
            return ToolResult.error(
                "I can't read what's playing on this system (the Windows media session API is unavailable)."
            )
        if app:
            return ToolResult.error(
                f"I can't aim media controls at {app} on this system (the Windows media session API is "
                "unavailable); the media keys only reach whichever app played last."
            )
        await asyncio.to_thread(win.tap_key, _MEDIA_KEYS[args.action])
        return ToolResult.success(
            f"Pressed the {_MEDIA_LABELS[args.action]} media key.", data={"method": "media_key", "action": args.action}
        )


# --------------------------------------------------------------------------- volume


def _default_speakers() -> Any:
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers()


def _volume_state(level: int | None = None, change: str | None = None) -> dict[str, Any]:
    """Read (and optionally change) the default output device's master volume. Runs on the audio thread."""
    speakers = _default_speakers()
    endpoint = speakers.EndpointVolume
    before = round(endpoint.GetMasterVolumeLevelScalar() * 100)
    muted = bool(endpoint.GetMute())
    target = before
    if level is not None:
        target = level
    elif change == "up":
        target = min(100, before + VOLUME_STEP)
    elif change == "down":
        target = max(0, before - VOLUME_STEP)
    if target != before:
        endpoint.SetMasterVolumeLevelScalar(target / 100, None)
    if change == "mute":
        endpoint.SetMute(1, None)
    elif change == "unmute" or (muted and target > 0 and (level is not None or change == "up")):
        endpoint.SetMute(0, None)
    state = {
        "level": round(endpoint.GetMasterVolumeLevelScalar() * 100),
        "muted": bool(endpoint.GetMute()),
        "previous_level": before,
        "was_muted": muted,
        "device": getattr(speakers, "FriendlyName", None),
    }
    del endpoint, speakers  # release COM pointers on this thread
    return state


class GetVolume(Tool):
    name = "get_volume"
    description = "System volume (0-100) and mute state."
    risk = RiskLevel.SAFE
    category = "media"
    timeout_s = 10.0

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.error("Volume control only works on Windows.")
        state = await _on_audio_thread(_volume_state)
        device = f" on {state['device']}" if state.get("device") else ""
        muted = ", muted" if state["muted"] else ""
        return ToolResult.success(f"Volume is {state['level']}%{muted}{device}.", data=state)


class SetVolumeArgs(ToolArgs):
    level: int | None = Field(default=None, ge=0, le=100)
    change: Literal["up", "down", "mute", "unmute"] | None = Field(
        default=None, description=f"Steps are {VOLUME_STEP} points."
    )

    @model_validator(mode="after")
    def _check(self) -> SetVolumeArgs:
        if self.level is None and self.change is None:
            raise ValueError("give a level (0-100) or a change (up, down, mute, unmute)")
        if self.level is not None and self.change in ("up", "down"):
            raise ValueError("give either a level or up/down, not both")
        return self


class SetVolume(Tool):
    name = "set_volume"
    description = "Set system volume: a level, up/down a step, or mute/unmute."
    Args = SetVolumeArgs
    risk = RiskLevel.SAFE
    category = "media"
    timeout_s = 10.0

    async def run(self, args: SetVolumeArgs, ctx: ToolContext) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.error("Volume control only works on Windows.")
        state = await _on_audio_thread(lambda: _volume_state(args.level, args.change))
        level = state["level"]
        if args.change == "mute":
            text = "It was already muted." if state["was_muted"] else f"Muted (volume stays at {level}% for later)."
        elif state["muted"]:
            text = f"Volume set to {level}%, but sound is still muted."
        elif state["was_muted"]:
            text = f"Unmuted; volume is {level}%."
        elif args.change == "unmute":
            text = f"Sound wasn't muted; volume is {level}%."
        elif args.change in ("up", "down") and level == state["previous_level"]:
            text = f"Volume is already at {level}%."
        elif args.change in ("up", "down"):
            text = f"Volume {args.change} to {level}%."
        else:
            text = f"Volume set to {level}%."
        return ToolResult.success(text, data=state)


TOOLS = [GetSystemStatus, MediaControl, GetVolume, SetVolume]
