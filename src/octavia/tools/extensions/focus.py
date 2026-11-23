"""Focus mode: "let me focus for 45 minutes". For that long whatever media is playing is paused, Windows' Do Not
Disturb is turned on when it can be, and the spoken email and phone alerts hold off; when time's up the alerts and Do
Not Disturb go back to how they were and she says focus time is over (``ctx.extras["announce"]``, once nobody is
talking). It can be ended early, or asked how long is left.

The tray's "Snooze for an hour" is the same session without the media and Do Not Disturb parts (``snooze``). Asking
for focus during a snooze makes it a focus session (the music paused, Do Not Disturb on); the tray's snooze leaves
a focus session alone.

Like a timer it's a wait on the event loop and lives only while she runs. The alerts are held with the watchers'
``paused`` flag, not their remembered on/off setting, so a restart in the middle can't leave them off; what arrived
meanwhile is said once focus ends. Media isn't resumed at the end: the user may not want it back.

Windows has no documented way to switch Do Not Disturb. The shell keeps it in a WNF state
(``WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED``: 0 off, 1 priority only, 2 alarms only), which ntdll's
``NtQueryWnfStateData`` and ``NtUpdateWnfStateData`` read and write; it's read back after the write to tell whether it
took. When it was already on it's left alone, and at the end it's only put back while it's still what focus mode set,
so a change the user made meanwhile stands.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.builtin import system_tools
from octavia.tools.extensions import gmail, phone_notifications
from octavia.tools.extensions.timers import spoken_duration

log = logging.getLogger(__name__)

MIN_MINUTES = 1.0
MAX_MINUTES = 8 * 60.0
DND_OFF = 0
DND_ON = 1  # priority only: what Windows 11 calls Do Not Disturb
OVER_NOTE = "That was the focus mode the user started, ending on its own; it's over and gone."
CUE = "timer"  # the avatar holds up a ringing stopwatch, as for a timer
_WNF_QUIET_HOURS = 0x0D83063EA3BF1C75  # WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED


class DoNotDisturb(Protocol):
    """Blocking: call from a thread."""

    def get(self) -> int | None:
        """The current profile (``DND_OFF`` when off), or None when it can't be read."""

    def set(self, profile: int) -> bool:
        """Whether the write was taken."""


class WindowsDoNotDisturb:
    """Windows' Do Not Disturb, through the shell's WNF state."""

    def get(self) -> int | None:
        if sys.platform != "win32":
            return None
        try:
            ntdll = ctypes.WinDLL("ntdll")
            name = ctypes.c_uint64(_WNF_QUIET_HOURS)
            stamp = ctypes.c_ulong(0)
            value = ctypes.c_uint32(0)
            size = ctypes.c_ulong(ctypes.sizeof(value))
            status = ntdll.NtQueryWnfStateData(
                ctypes.byref(name), None, None, ctypes.byref(stamp), ctypes.byref(value), ctypes.byref(size)
            )
        except (OSError, AttributeError):
            log.debug("Couldn't read Do Not Disturb", exc_info=True)
            return None
        if status != 0:
            return None
        return int(value.value) if size.value else DND_OFF  # never set since sign-in: off

    def set(self, profile: int) -> bool:
        if sys.platform != "win32":
            return False
        try:
            ntdll = ctypes.WinDLL("ntdll")
            name = ctypes.c_uint64(_WNF_QUIET_HOURS)
            value = ctypes.c_uint32(profile)
            status = ntdll.NtUpdateWnfStateData(
                ctypes.byref(name), ctypes.byref(value), ctypes.sizeof(value), None, None, 0, 0
            )
        except (OSError, AttributeError):
            log.debug("Couldn't switch Do Not Disturb", exc_info=True)
            return False
        return status == 0


async def pause_playing_media() -> list[str] | None:
    """Pause every media session that's playing: the apps paused, or None when the media sessions can't be reached.
    Never the media keys: they toggle, so they could start music instead."""
    if sys.platform != "win32":
        return None

    async def pause() -> list[str]:
        manager = await system_tools._session_manager()
        paused: list[str] = []
        for session in system_tools._sessions(manager)[0]:
            if int(session.get_playback_info().playback_status) != system_tools._PLAYING:
                continue
            if await session.try_pause_async():
                paused.append(system_tools._source_name(session.source_app_user_model_id or ""))
        return paused

    try:
        return await system_tools._on_media_thread(pause)
    except Exception:
        log.info("Couldn't pause media for focus mode", exc_info=True)
        return None


class Alerts(Protocol):
    """A spoken-alert watcher (``gmail.InboxWatcher``, ``phone_notifications.PhoneWatcher``)."""

    paused: bool


def _alert_watchers() -> Sequence[Alerts]:
    return (gmail.WATCHER, phone_notifications.WATCHER)


DndState = Literal["on", "already", "unavailable"]
Announce = Callable[..., None]


@dataclass
class Session:
    seconds: float
    ends: float  # on the event loop's clock
    media: list[str] | None = None  # the apps paused; None when media couldn't be reached
    dnd: DndState = "unavailable"
    dnd_before: int | None = None  # the profile to put back, when focus mode turned Do Not Disturb on
    held: list[Alerts] = field(default_factory=list)  # the watchers it paused
    task: asyncio.Task[None] | None = None
    snooze: bool = False  # the tray's snooze: only the alerts and her remarks are held


class FocusKeeper:
    """The focus session, if one runs. Call on the event loop thread."""

    def __init__(
        self,
        dnd: DoNotDisturb | None = None,
        pause_media: Callable[[], Awaitable[list[str] | None]] = pause_playing_media,
        alerts: Callable[[], Sequence[Alerts]] = _alert_watchers,
    ) -> None:
        self.dnd: DoNotDisturb = dnd if dnd is not None else WindowsDoNotDisturb()
        self._pause_media = pause_media
        self._alerts = alerts
        self.session: Session | None = None

    def remaining(self) -> float:
        if self.session is None:
            return 0.0
        return max(0.0, self.session.ends - asyncio.get_running_loop().time())

    async def start(self, seconds: float, announce: Announce | None, *, snooze: bool = False) -> tuple[Session, bool]:
        """Start focusing for ``seconds``, or give the running session that long from now (what it paused stays
        paused, to be put back at its end). The session, and whether it was already running. A ``snooze`` (the
        tray's) only holds her alerts and remarks: music plays on and Windows is left alone."""
        loop = asyncio.get_running_loop()
        session = self.session
        upgrade = session is not None and session.snooze and not snooze  # focus asked for during a snooze
        already = session is not None and not upgrade
        if session is None:
            session = self.session = Session(seconds, loop.time() + seconds, snooze=snooze)
            for watcher in self._alerts():
                if not watcher.paused:
                    watcher.paused = True
                    session.held.append(watcher)
        else:
            session.seconds, session.ends = seconds, loop.time() + seconds
            if session.task is not None:
                session.task.cancel()
        if not already and not snooze:  # focus: the music and Do Not Disturb too
            session.snooze = False
            session.media = await self._pause_media()
            session.dnd, session.dnd_before = await self._dnd_on()
        session.task = loop.create_task(self._run_out(session, announce), name="focus-mode")
        return session, already

    async def end(self) -> Session | None:
        """Put the alerts and Do Not Disturb back as they were; the session that ended, or None when none ran."""
        session, self.session = self.session, None
        if session is None:
            return None
        if session.task is not None and session.task is not asyncio.current_task():
            session.task.cancel()
        for watcher in session.held:
            watcher.paused = False
        if session.dnd_before is not None:
            try:
                if await asyncio.to_thread(self.dnd.get) == DND_ON:  # else the user changed it meanwhile
                    await asyncio.to_thread(self.dnd.set, session.dnd_before)
            except Exception:
                log.warning("Couldn't put Do Not Disturb back", exc_info=True)
        return session

    async def _dnd_on(self) -> tuple[DndState, int | None]:
        try:
            before = await asyncio.to_thread(self.dnd.get)
            if before is None:
                return "unavailable", None
            if before != DND_OFF:
                return "already", None
            if await asyncio.to_thread(self.dnd.set, DND_ON) and await asyncio.to_thread(self.dnd.get) == DND_ON:
                return "on", before
        except Exception:
            log.warning("Couldn't turn on Do Not Disturb", exc_info=True)
        return "unavailable", None

    async def _run_out(self, session: Session, announce: Announce | None) -> None:
        await asyncio.sleep(max(0.0, session.ends - asyncio.get_running_loop().time()))
        if self.session is not session:
            return
        await self.end()
        if announce is None:
            return
        try:
            announce(over_text(session), OVER_NOTE, False, cue=CUE)
        except Exception:
            log.warning("Couldn't say focus time is over", exc_info=True)


KEEPER = FocusKeeper()


def over_text(session: Session) -> str:
    back = [what for what, put_back in (("alerts", session.held), ("Do Not Disturb", session.dnd == "on")) if put_back]
    if not back:
        return "Focus time is over."
    return f"Focus time is over. Your {' and '.join(back)} {'is' if back == ['Do Not Disturb'] else 'are'} back."


def _media_line(media: list[str] | None) -> str:
    if media is None:
        return "Couldn't reach the media player to pause it."
    if not media:
        return "Nothing was playing."
    return f"Paused {', '.join(media)}."


def _dnd_line(dnd: DndState) -> str:
    return {
        "on": "Windows' Do Not Disturb is on until it ends.",
        "already": "Do Not Disturb was already on, so it's left as it is.",
        "unavailable": "Couldn't turn on Windows' Do Not Disturb here.",
    }[dnd]


def _alerts_line(session: Session) -> str:
    return "Spoken email and phone alerts are paused until it ends." if session.held else ""


class FocusModeArgs(ToolArgs):
    action: Literal["start", "end", "status"] = Field(
        default="start",
        description="start (again to change time left), end, or status.",
    )
    minutes: float = Field(default=25, ge=MIN_MINUTES, le=MAX_MINUTES)


class FocusMode(Tool):
    name = "focus_mode"
    description = (
        "Focus session: pauses media, turns on Do Not Disturb if possible and holds your spoken email/phone "
        "alerts; restores them and announces the end when time's up."
    )
    Args = FocusModeArgs
    risk = RiskLevel.SAFE
    category = "time"

    def __init__(self, keeper: FocusKeeper | None = None) -> None:
        self.keeper = keeper if keeper is not None else KEEPER

    async def run(self, args: FocusModeArgs, ctx: ToolContext) -> ToolResult:
        keeper = self.keeper
        if args.action == "status":
            session = keeper.session
            if session is None:
                return ToolResult.success("Focus mode isn't on.", data={"on": False})
            left = keeper.remaining()
            lines = [f"Focus mode is on: {spoken_duration(left)} left of {spoken_duration(session.seconds)}."]
            lines += [_alerts_line(session), _dnd_line(session.dnd)]
            return ToolResult.success(" ".join(line for line in lines if line), data={"on": True, "left_s": left})
        if args.action == "end":
            left = keeper.remaining()
            session = await keeper.end()
            if session is None:
                return ToolResult.success("Focus mode wasn't on.", data={"on": False})
            back = over_text(session).removeprefix("Focus time is over.").strip()
            text = f"Focus mode ended with {spoken_duration(left)} to spare. {back}".strip()
            return ToolResult.success(text, data={"on": False})
        announce = ctx.extras.get("announce")
        session, already = await keeper.start(args.minutes * 60, announce if callable(announce) else None)
        length = spoken_duration(session.seconds)
        if already:
            return ToolResult.success(f"Focus mode was already on; it now ends in {length}.", data={"on": True})
        lines = [f"Focus mode is on for {length}.", _media_line(session.media), _dnd_line(session.dnd)]
        lines.append(_alerts_line(session))
        if callable(announce):
            lines.append("When time's up you'll say so on your own.")
        data = {"on": True, "seconds": session.seconds, "paused_media": session.media or [], "dnd": session.dnd}
        return ToolResult.success(" ".join(line for line in lines if line), data=data)


TOOLS = [FocusMode]
