"""Her icon in the notification area (the hidden icons by the clock), and its right-click menu.

``TrayMenu`` is what the menu holds and what each choice does, on the event loop, through what the app hands it:

* Shut down: the same way out as asking her to (``App.shut_down_now``), with a goodbye said first. She's off until
  she's started again or the next login (autostart); the computer stays on.
* Restart: as asking her to (``App.restart_now``), into any update installed and not running yet. Greyed out when
  she can't restart herself (no supervisor).
* Mute listening: while she waits for her name, what the mic hears goes nowhere (``Assistant.set_listening``). The
  talk hotkey still works. Kept across restarts, so an update never turns her ears back on by itself. The mute hotkey
  (``hotkeys.mute``) does the same.
* Snooze for an hour: her alerts and her own remarks hold off for an hour (a focus session without pausing music or
  touching Do Not Disturb); timers and reminders the user set still go off. Chosen again, it ends.
* Microphone and Speaker: the devices she can use; picking one keeps it in ``data/settings.json``
  (``octavia.user_settings``) and restarts her onto it.
* Start with Windows: whether she starts at login (``platform/autostart.py``).
* Updates: the improvements she built that are ready, to install (all, or one), and the last one installed, to undo.
  Both are what asking her does (``ImprovementManager.install`` and ``undo_last``), restart included; what she'd
  say about it is said.
* Chat: her chat window (``core/chat.py``): today's conversation, and a box to type to her. The default choice on a
  double-click.
* Brain and Voice: who answers (automatic, Claude only, local brain only) and which voice speaks (automatic, local
  voice only, Fish Audio only), switched live and kept (``core/engines.py``); the tip names a pick that isn't automatic.
* Voice effect: off, or a preset on her voice (radio, console, robot, room, deeper; ``audio/voice_effects.py``), heard
  from her next reply and kept.
* Bring back a panel: her pop-up panels that have closed (``PanelHost.bring_back``), when there are any.
* Settings: her settings window (``platform/settings_window.py``); changes apply while she runs (``core/live_settings.py``).
* Open logs: the logs folder in Explorer.
* Forget the last...: 15 minutes, 30 minutes, an hour or a day of everything she kept, gone after one confirm
  (``App.forget_recent``, ``core/forget.py``); she restarts into a new conversation. Greyed out when she can't
  restart herself.

The icon is her emblem, a white raven perched in a halo, and the halo shows what she's doing at a glance: blue while
she waits for her name, cyan while she hears you, amber while she thinks, green while she speaks, grey (the raven too,
with a slash) while muted, and a purple dot while she works on something in the background (a build). Hovering says
the same in words. The halo moves while she's at it: a slow breath while she hears you or waits for your answer, an
arc swirling round while she thinks, and brightening and rippling with her voice while she speaks; the raven stays
still. It's all still while she waits for her name (muted or not), and then its timer is off and it costs nothing.
The frames are drawn once as she starts; playing them only hands the taskbar another handle, 12 a second.

Shutting down or restarting while she's building or installing asks first, in a Windows dialog: either would cut
that off. A click is otherwise the yes.

``TrayIcon`` is the Windows part: a hidden window on its own thread holding the icon (``Shell_NotifyIcon``), the menu
built fresh each time it opens (left or right click), and the icon drawn here, so there's no image file. It comes back
by itself when Explorer restarts (``TaskbarCreated``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import functools
import json
import logging
import os
import random
import struct
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

import numpy as np

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
T = TypeVar("T")

GOODBYES = (
    "Okay, shutting down. See you soon!",
    "Heading out. Bye for now!",
    "Signing off. Catch you later!",
    "Shutting down. Later!",
)
SHUT_DOWN, RESTART, MUTE, LOGS = "shut_down", "restart", "mute", "logs"
SNOOZE, AUTOSTART, CHAT, SETTINGS = "snooze", "autostart", "chat", "settings"
PANEL = "panel"  # bringing a panel back: "panel:<id>"
FORGET = "forget"  # forgetting the last stretch of time: "forget:<minutes>"
PANELS_LISTED = 10
INSTALL_ALL = "install:all"  # one update's key is "install:<id>", undoing the last one "undo:<id>"
MIC, SPEAKER = "input", "output"  # a device choice's key is "input:<name>" or "output:<name>"; no name = the default
BRAIN, VOICE = "brain", "voice"  # who answers / which voice speaks: "brain:<brain.answers>", "voice:<tts.provider>"
EFFECT = "effect"  # her voice effect: "effect:<tts.voice_effect>"
SNOOZE_S = 3600.0
RELEASE = "release-update"  # a public install's update from the signed feed (``hive/updater.py``)
CHECK_RELEASE = "release-check"  # look at the feed now (the watch only looks now and then)

# What each of her states looks like, and says when hovered.
_STATE_KIND = {
    "listening": "hearing",
    "transcribing": "hearing",
    "thinking": "thinking",
    "confirming": "thinking",
    "speaking": "speaking",
}
_STATE_WORDS = {
    "listening": "listening to you",
    "transcribing": "listening to you",
    "thinking": "thinking",
    "confirming": "waiting for your answer",
    "speaking": "speaking",
}
# How the icon moves in each state: none while she waits for her name or is muted, so then it costs nothing.
_STATE_MOTION = {
    "listening": "pulse",
    "transcribing": "pulse",
    "confirming": "pulse",
    "thinking": "swirl",
    "speaking": "wave",
}
_COLOURS = {  # (dark, bright): the halo's lower right and upper left
    "idle": ((20, 90, 210), (170, 235, 255)),
    "hearing": ((0, 150, 185), (215, 255, 255)),
    "thinking": ((195, 110, 10), (255, 228, 150)),
    "speaking": ((20, 145, 70), (175, 255, 195)),
    "muted": ((85, 85, 92), (200, 200, 205)),
    "off": ((70, 70, 76), (150, 150, 156)),
}


@dataclass(frozen=True)
class MenuItem:
    key: str | None  # what ``TrayMenu.choose`` gets; None for a line with nothing to choose (a title, a separator)
    label: str = ""  # "" with no key: a separator
    enabled: bool = True
    checked: bool = False
    default: bool = False  # shown in bold: the main choice
    children: tuple[MenuItem, ...] = ()  # a submenu (its own key is None)


class Snoozer(Protocol):
    """Holds her alerts and remarks for a while (the app wraps focus mode)."""

    def until(self) -> float | None:  # when it ends (``time.time``), or None when it isn't on
        ...

    def focusing(self) -> bool:  # it's the user's focus mode ("let me focus"), not a snooze: left alone
        ...

    async def start(self, seconds: float) -> None: ...

    async def end(self) -> None: ...


@dataclass(frozen=True)
class UpdateState:
    """Her updates as the menu shows them."""

    ready: tuple[tuple[int, str], ...] = ()  # (id, title) of each one built and ready to install, oldest first
    last: tuple[int, str] | None = None  # the last one installed, which can be undone
    changing: bool = False  # an install or undo is under way
    new_code: bool = False  # code committed outside her waits for her to be idle to restart into (``new_code.py``)


class Updates(Protocol):
    """Her improvements (the app wraps ``ImprovementManager``). ``install`` and ``undo`` return what to say, and
    raise ``EvolveError`` with why not."""

    async def look(self) -> UpdateState: ...

    async def install(self, ids: list[int]) -> str: ...

    async def undo(self, expected: int) -> str: ...


class Autostart(Protocol):
    """``octavia.platform.autostart``: both block, so they run off the loop."""

    def is_on(self) -> bool: ...

    def switch(self, on: bool) -> str | None: ...


class TrayMenu:
    """The menu's items and what they do. ``choose`` may be called from any thread; the work runs on ``loop``."""

    def __init__(
        self,
        *,
        name: str,
        loop: asyncio.AbstractEventLoop,
        shut_down: Callable[[], Awaitable[bool]],
        restart: Callable[[], Awaitable[str]],
        ready: Callable[[], bool],
        can_restart: Callable[[], bool],
        busy: Callable[[], Awaitable[str | None]],
        say: Callable[[str], None],
        listening: Callable[[], bool],
        set_listening: Callable[[bool], None],
        logs: Path,
        state_file: Path,
        ask: Callable[[str, str], bool] | None = None,
        show: Callable[[str, str], None] | None = None,
        open_folder: Callable[[Path], None] | None = None,
        background: Callable[[], Awaitable[str | None]] | None = None,
        snoozer: Snoozer | None = None,
        autostart: Autostart | None = None,
        devices: Callable[[str], tuple[list[str], str | None]] | None = None,
        save_device: Callable[[str, str | None], None] | None = None,
        chosen_device: Callable[[str], bool] | None = None,
        updates: Updates | None = None,
        chat: Callable[[], None] | None = None,
        settings: Callable[[], None] | None = None,
        panels: Any = None,
        engines: Any = None,
        mute_hotkey: str = "",
        chat_hotkey: str = "",
        forget: Callable[[int], Awaitable[str]] | None = None,
        release: Any = None,
    ) -> None:
        self.name = name
        self._forget = forget
        # A public install's update (``hive/updater.py``): ``available`` (a Release or None) and ``start()``, which
        # launches the updater and returns an error message or None.
        self._release = release
        self.loop = loop
        self._shut_down = shut_down
        self._restart = restart
        self._ready = ready  # she's up and running: before that, there's nothing to shut down or restart yet
        self._can_restart = can_restart
        self._busy = busy  # what a shutdown or restart now would cut off ("building X"), or None
        self._say = say
        self._listening = listening
        self._set_listening = set_listening
        self.logs = logs
        self.state_file = state_file
        self._panels = panels
        self._ask = ask or ask_yes_no
        self._show = show or show_message
        self._open_folder = open_folder or _open_folder
        self.leaving: str | None = None  # "shutting down" or "restarting", once chosen: nothing more to choose
        self._deciding = False  # a shutdown or restart is being chosen (its question may be open): not another
        self.on_change: Callable[[], None] | None = None  # set by the icon: draw it again (from any thread)
        self._tasks: set[asyncio.Task] = set()
        self._background = background  # what she's working on in the background ("building X"), or None
        self._snoozer = snoozer
        self._autostart = autostart
        self._devices = devices  # (names, the one in use) for MIC or SPEAKER; called on the icon's thread
        self._save_device = save_device
        self._chosen_device = chosen_device  # whether one was picked for MIC or SPEAKER (not Windows' default)
        self._updates = updates
        self.update_state: UpdateState | None = None  # None until it's been looked at
        self._mute_hotkey = mute_hotkey
        self._chat = chat
        self._settings = settings
        self._engines = engines  # who answers and which voice speaks (``core/engines.Engines``)
        self._chat_hotkey = chat_hotkey
        self.state = "idle"  # an ``AssistantState`` value
        self.working: str | None = None  # the last ``background`` answer
        self.autostart_on: bool | None = None  # None until it's been looked up
        self._drawn: tuple[str, tuple[str, bool], str | None] | None = None  # the tip, icon and motion last drawn

    # ---------------------------------------------------------------- what's shown

    @property
    def listening(self) -> bool:
        try:
            return bool(self._listening())
        except Exception:
            return True

    def kind(self) -> tuple[str, bool]:
        """The icon: what she's doing (a key of ``_COLOURS``), and whether she's busy in the background."""
        if self.leaving:
            return "off", False
        kind = _STATE_KIND.get(self.state) or ("idle" if self.listening else "muted")
        return kind, bool(self.working)

    def motion(self) -> str | None:
        """How the icon moves now (a value of ``_STATE_MOTION``), or None: still."""
        return None if self.leaving else _STATE_MOTION.get(self.state)

    def snoozed_until(self) -> float | None:
        """When the snooze ends, or None. A focus session counts too (it holds the same things)."""
        if self._snoozer is None:
            return None
        try:
            return self._snoozer.until()
        except Exception:
            return None

    def _focusing(self) -> bool:
        try:
            return self._snoozer is not None and bool(self._snoozer.focusing())
        except Exception:
            return False

    def tip(self) -> str:
        if self.leaving:
            return f"{self.name}: {self.leaving}"
        parts = []
        if self.state in _STATE_WORDS:
            parts.append(_STATE_WORDS[self.state])
        elif not self.listening:
            parts.append("not listening")
        if self.working:
            parts.append(self.working)
        if self.update_state is not None and self.update_state.new_code:
            parts.append("new code waiting")
        until = self.snoozed_until()
        if until is not None:
            parts.append(f"{'focus mode' if self._focusing() else 'snoozed'} until {_clock(until)}")
        if self._engines is not None:
            try:
                picked = self._engines.summary(effect=False)
            except Exception:
                picked = ""
            if picked:
                parts.append(picked.lower())
        from octavia import edition

        name = self.name.upper() if edition.PUBLIC else self.name  # OCTAVIA, as the public build writes it
        return f"{name}: {', '.join(parts)}" if parts else name

    def set_state(self, state: str) -> None:
        """Her state changed (``StateChanged``, on the loop): the icon follows."""
        self.state = state
        self._changed(only_if_different=True)

    async def refresh(self) -> None:
        """Look again at what changes without telling the menu (a build starting, a snooze running out)."""
        if self._background is not None:
            try:
                self.working = await self._background()
            except Exception:
                log.debug("Couldn't tell what she's working on", exc_info=True)
        if self._autostart is not None and self.autostart_on is None:
            await self.check_autostart()
        await self.check_updates()
        self._changed(only_if_different=True)

    async def check_updates(self) -> None:
        if self._updates is None:
            return
        try:
            self.update_state = await self._updates.look()
        except Exception:
            log.debug("Couldn't look at her updates", exc_info=True)

    async def check_autostart(self) -> None:
        if self._autostart is None:
            return
        try:
            self.autostart_on = await _off_loop(self._autostart.is_on)
        except Exception:
            log.warning("Couldn't tell whether she starts with Windows", exc_info=True)

    def items(self) -> list[MenuItem]:
        try:
            ready = bool(self._ready())
            can_restart = ready and bool(self._can_restart())
        except Exception:
            ready = can_restart = False
        going = self.leaving is None
        free = going and not self._deciding
        mute = "Mute listening" + (f"\t{_keys(self._mute_hotkey)}" if self._mute_hotkey else "")
        items = [MenuItem(None, self.tip(), enabled=False), MenuItem(None)]
        available = getattr(self._release, "available", None)
        if available is not None:
            version = str(available.version).split("+")[0]
            items += [MenuItem(RELEASE, f"Update {self.name} to {version}", enabled=free and ready), MenuItem(None)]
        elif self._release is not None and hasattr(self._release, "check"):
            items += [MenuItem(CHECK_RELEASE, "Check for updates", enabled=going), MenuItem(None)]
        if self._chat is not None:
            chat = "Chat" + (f"\t{_keys(self._chat_hotkey)}" if self._chat_hotkey else "")
            items += [MenuItem(CHAT, chat, enabled=going and ready), MenuItem(None)]
        items.append(MenuItem(MUTE, mute, enabled=going, checked=not self.listening))
        if self._snoozer is not None:
            until = self.snoozed_until()
            if until is None:
                items.append(MenuItem(SNOOZE, "Snooze for an hour", enabled=going and ready))
            elif self._focusing():  # the user's own focus session: ended by asking her, not here
                items.append(MenuItem(None, f"Focus mode until {_clock(until)}", enabled=False, checked=True))
            else:
                items.append(MenuItem(SNOOZE, f"Snoozed until {_clock(until)}", enabled=going, checked=True))
        if self._devices is not None and self._save_device is not None:
            free_now = free and ready
            items += [self._device_menu(MIC, "Microphone", free_now), self._device_menu(SPEAKER, "Speaker", free_now)]
        if self._engines is not None:
            items += self._engine_menus(going and ready)
        if self._autostart is not None:
            on = self.autostart_on
            items.append(MenuItem(AUTOSTART, "Start with Windows", enabled=on is not None and going, checked=bool(on)))
        if self._updates is not None:
            items.append(self._updates_menu(free and ready))
        if self._panels is not None:
            items += self._panels_menu(going and ready)
        if self._settings is not None:
            items.append(MenuItem(SETTINGS, "Settings...", enabled=going))
        if self._forget is not None:
            items.append(self._forget_menu(free and can_restart))
        return items + [
            MenuItem(LOGS, "Open logs"),
            MenuItem(RESTART, "Restart", enabled=free and can_restart),
            MenuItem(None),
            MenuItem(SHUT_DOWN, "Shut down", enabled=free and ready, default=True),
        ]

    def _forget_menu(self, enabled: bool) -> MenuItem:
        from octavia.core.forget import CHOICES

        children = tuple(
            MenuItem(f"{FORGET}:{minutes}", words[0].upper() + words[1:], enabled=enabled)
            for minutes, words in CHOICES.items()
        )
        return MenuItem(None, "Forget the last", children=children)

    def _panels_menu(self, enabled: bool) -> list[MenuItem]:
        """Her closed panels to bring back; nothing in the menu when there are none."""
        rows = [row for row in self._panels.listing() if not row["up"]][:PANELS_LISTED]
        if not rows:
            return []
        children = tuple(
            # (an & marks a menu shortcut to Windows; a title is hers, and may hold one)
            MenuItem(f"{PANEL}:{row['id']}", str(row["title"]).replace("&", "&&")[:60], enabled=enabled)
            for row in rows
        )
        return [MenuItem(None, "Bring back a panel", children=children)]

    def _updates_menu(self, enabled: bool) -> MenuItem:
        state = self.update_state
        if state is None:
            return MenuItem(None, "Updates", enabled=False)
        enabled = enabled and not state.changing
        children: list[MenuItem] = []
        if state.changing:
            children += [MenuItem(None, "Installing or undoing one now...", enabled=False), MenuItem(None)]
        if state.new_code:  # she'd restart into it once idle; this does it now (the tray's Restart)
            children += [MenuItem(RESTART, "Restart into the new code now", enabled=enabled), MenuItem(None)]
        if len(state.ready) > 1:
            children.append(MenuItem(INSTALL_ALL, f"Install all {len(state.ready)}", enabled=enabled, default=True))
        for improvement_id, title in state.ready:
            label = f"Install #{improvement_id}: {_short(title)}"
            children.append(MenuItem(f"install:{improvement_id}", label, enabled=enabled))
        if not state.ready:
            children.append(MenuItem(None, "Nothing new to install", enabled=False))
        children.append(MenuItem(None))
        if state.last is not None:
            improvement_id, title = state.last
            children.append(
                MenuItem(f"undo:{improvement_id}", f"Undo #{improvement_id}: {_short(title)}", enabled=enabled)
            )
        else:
            children.append(MenuItem(None, "Nothing to undo", enabled=False))
        label = f"Updates ({len(state.ready)} ready)" if state.ready else "Updates"
        return MenuItem(None, label, children=tuple(children))

    def _engine_menus(self, enabled: bool) -> list[MenuItem]:
        """Who answers (Brain), which voice speaks (Voice) and her Voice effect, the one on checked; a pick that can't
        apply greyed."""
        from octavia.audio.voice_effects import NAMES as EFFECT_NAMES
        from octavia.core.engines import BRAIN_KEY, BRAIN_NAMES, VOICE_NAMES

        engines = self._engines
        try:
            brain, voice = engines.brain_now(), engines.voice_now()
            menus = [MenuItem(None, "Brain", children=tuple(
                MenuItem(f"{BRAIN}:{value}", name, checked=value == brain,
                         enabled=enabled and engines.why_not(BRAIN_KEY, value) is None)
                for value, name in BRAIN_NAMES.items()
            ))]  # fmt: skip
            if engines.tts is not None:
                menus.append(MenuItem(None, "Voice", children=tuple(
                    MenuItem(f"{VOICE}:{value}", name, enabled=enabled, checked=value == voice)
                    for value, name in VOICE_NAMES.items()
                )))  # fmt: skip
                effect = engines.effect_now()
                menus.append(MenuItem(None, "Voice effect", children=tuple(
                    MenuItem(f"{EFFECT}:{value}", name, enabled=enabled, checked=value == effect)
                    for value, name in EFFECT_NAMES.items()
                )))  # fmt: skip
        except Exception:
            log.warning("Couldn't show her brain and voice picks", exc_info=True)
            return []
        return menus

    async def switch_engine(self, key: str) -> None:
        """A Brain, Voice or Voice effect pick: kept, applied, and what really happened said (it may wait for a
        restart)."""
        kind, _, value = key.partition(":")
        if kind == BRAIN:
            said = await self._engines.switch(brain=value)
        elif kind == EFFECT:
            said = await self._engines.switch(effect=value)
        else:
            said = await self._engines.switch(voice=value)
        log.info("Tray: %s", said)
        self._say(said)
        self._changed(only_if_different=True)

    def _device_menu(self, kind: str, label: str, enabled: bool) -> MenuItem:
        try:
            names, current = self._devices(kind)  # type: ignore[misc]
            chosen = self._chosen_device(kind) if self._chosen_device is not None else current is not None
        except Exception:
            log.warning("Couldn't list the %s devices", kind, exc_info=True)
            return MenuItem(None, label, enabled=False)
        children = [MenuItem(f"{kind}:", "Windows default", enabled=enabled, checked=not chosen)]
        if names:
            children.append(MenuItem(None))
        for name in names:
            shown = name.replace("&", "&&")  # a lone & would underline the next letter
            children.append(MenuItem(f"{kind}:{name}", shown, enabled=enabled, checked=chosen and name == current))
        return MenuItem(None, label, children=tuple(children))

    # ---------------------------------------------------------------- choosing

    def choose(self, key: str) -> None:
        """A menu choice, from the icon's thread (or any): handled on the event loop."""
        self.loop.call_soon_threadsafe(self._start, key)

    def _start(self, key: str) -> None:
        task = self.loop.create_task(self.handle(key), name=f"tray-{key}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle(self, key: str) -> None:
        try:
            if key == MUTE:
                self.toggle_listening()
            elif key == CHAT:
                if self._chat is not None:
                    self._chat()
            elif key == SETTINGS:
                if self._settings is not None:
                    self._settings()
            elif key == SNOOZE:
                await self.toggle_snooze()
            elif key == AUTOSTART:
                await self.toggle_autostart()
            elif key.startswith(f"{PANEL}:"):
                if self._panels is not None:
                    await self._bring_back(key.partition(":")[2])
            elif key.startswith(("install:", "undo:")):
                await self.change_updates(key)
            elif key.startswith((f"{BRAIN}:", f"{VOICE}:", f"{EFFECT}:")):
                await self.switch_engine(key)
            elif key.startswith((f"{MIC}:", f"{SPEAKER}:")):
                kind, _, name = key.partition(":")
                await self.choose_device(kind, name or None)
            elif key == LOGS:
                await _off_loop(self._open_folder, self.logs)
            elif key == RESTART:
                await self.restart()
            elif key.startswith(f"{FORGET}:"):
                await self.forget(int(key.partition(":")[2]))
            elif key == SHUT_DOWN:
                await self.shut_down()
            elif key == RELEASE:
                await self.update_release()
            elif key == CHECK_RELEASE:
                await self.check_release()
        except Exception:
            log.exception("The tray's %r failed", key)

    async def _bring_back(self, panel: str) -> None:
        """A closed panel, up again. When it can't be, she says why: nothing else on screen would."""
        try:
            await self._panels.bring_back(panel)
        except Exception as exc:
            log.warning("Bringing panel %s back from the tray failed: %s", panel, exc)
            self._say(f"I couldn't bring that panel back. {exc}")

    async def shut_down(self) -> None:
        if not await self._go_ahead("Shut down", "shutting down"):
            return
        self._leave("shutting down")
        log.info("Shutting down: chosen in the tray")
        self._say(random.choice(GOODBYES))  # said before she goes: the shutdown waits for it
        if not await self._shut_down():  # she couldn't: back as she was
            self._leave(None)
            await _off_loop(self._show, self.name, "I couldn't shut down just now. Try again in a moment.")

    async def check_release(self) -> None:
        """Looks at the feed now. A newer release: asked once whether to update now (the menu offers it either
        way). None: says this is the latest, or why it couldn't look."""
        if self._release is None or not hasattr(self._release, "check"):
            return
        available, error = await self._release.check()
        self._changed()
        if available is not None:
            version = str(available.version).split("+")[0]
            if await _off_loop(self._ask, self.name, f"Version {version} is out. Update now? It takes about a "
                               "minute, and everything you've told me stays."):  # fmt: skip
                await self.update_release()
            return
        text = f"I couldn't reach the update feed just now ({error})." if error else "I'm on the latest version."
        await _off_loop(self._show, self.name, text)

    async def update_release(self) -> None:
        """The click starts the update: the updater takes over and she shuts down, then starts on the new version."""
        if self._release is None or getattr(self._release, "available", None) is None:
            return
        if not await self._go_ahead("Update", "updating"):
            return
        self._leave("updating")
        error = await self._release.start()
        if error:
            self._leave(None)
            await _off_loop(self._show, self.name, f"The update couldn't start: {error}")
            return
        log.info("Updating: chosen in the tray")
        self._say("Updating now. I'll be back in a minute.")
        if not await self._shut_down():
            self._leave(None)

    async def restart(self) -> bool:
        """Whether she's restarting (it wasn't declined or refused)."""
        if not await self._go_ahead("Restart", "restarting"):
            return False
        from octavia.evolve.models import EvolveError

        self._leave("restarting")
        log.info("Restarting: chosen in the tray")
        try:
            said = await self._restart()
        except EvolveError as exc:
            self._leave(None)
            await _off_loop(self._show, self.name, str(exc))
            return False
        self._say(said)  # "Restarting now; I'll be right back.": the restart waits for it
        return True

    async def forget(self, minutes: int) -> bool:
        """Forget the last ``minutes``, after one confirm (it deletes): whether she's doing it."""
        from octavia.core.forget import label
        from octavia.evolve.models import EvolveError

        if self._forget is None or self.leaving or self._deciding:
            return False
        self._deciding = True
        try:
            try:
                busy = await self._busy()
            except Exception:
                busy = None
            what = label(minutes)
            question = (
                f"Forget {what}? Everything {self.name} kept from then is deleted (a copy goes to the "
                f"octavia-backups folder in your user folder), and she restarts into a new conversation."
            )
            if busy:
                question += f"\n\nShe's {busy}: restarting cuts that off."
            yes = await _off_loop(self._ask, self.name, question) and self.leaving is None
        finally:
            self._deciding = False
        if not yes:
            return False
        self._leave("restarting")
        log.info("Forgetting %s: chosen in the tray", label(minutes))
        try:
            said = await self._forget(minutes)
        except Exception as exc:  # whatever went wrong, the menu works again and nothing is left to run
            self._leave(None)
            if not isinstance(exc, EvolveError):
                log.warning("Forgetting from the tray failed", exc_info=True)
            await _off_loop(
                self._show, self.name, str(exc) if isinstance(exc, EvolveError) else "I couldn't forget that just now."
            )
            return False
        self._say(said)
        return True

    def toggle_listening(self) -> None:
        on = not self.listening
        self._set_listening(on)
        self._save(muted=not on)
        log.info("Listening %s from the tray", "back on" if on else "muted")
        self._changed()

    async def toggle_snooze(self) -> None:
        if self._snoozer is None:
            return
        if self._focusing():
            return
        if self.snoozed_until() is not None:
            await self._snoozer.end()
            log.info("Snooze ended from the tray")
        else:
            await self._snoozer.start(SNOOZE_S)
            log.info("Snoozed for an hour from the tray")
        self._changed()

    async def toggle_autostart(self) -> None:
        if self._autostart is None or self.autostart_on is None:
            return
        on = not self.autostart_on
        error = await _off_loop(self._autostart.switch, on)
        await self.check_autostart()
        if error:
            what = "on" if on else "off"
            await _off_loop(self._show, self.name, f"I couldn't turn starting with Windows {what}: {error}")
        else:
            log.info("Starting with Windows turned %s from the tray", "on" if on else "off")
        self._changed()

    async def change_updates(self, key: str) -> None:
        """Install one update or every ready one, or undo the last: the click is the yes, as it is for asking her."""
        from octavia.evolve.models import EvolveError

        state = self.update_state
        if self._updates is None or state is None or state.changing or self.leaving or self._deciding:
            return
        action, _, which = key.partition(":")
        self.update_state = replace(state, changing=True)  # until it's looked at again: no second click meanwhile
        said: str | None = None
        try:
            if action == "undo":
                log.info("Undoing update #%s: chosen in the tray", which)
                said = await self._updates.undo(int(which))
            else:
                ids = [i for i, _ in state.ready] if which == "all" else [int(which)]
                log.info("Installing update(s) %s: chosen in the tray", ids)
                said = await self._updates.install(ids)
        except EvolveError as exc:
            await _off_loop(self._show, self.name, str(exc))
        if said:
            self._say(said)  # first: the restart it asked for waits for her to be quiet
        await self.check_updates()
        self._changed(only_if_different=True)

    async def choose_device(self, kind: str, name: str | None) -> None:
        """Keep the choice, then restart onto it (the audio is opened as she starts). Picking what she's running
        with keeps that (taking back a pick still waiting for a restart) without restarting."""
        if self._save_device is None or self.leaving or self._deciding:
            return
        what = "microphone" if kind == MIC else "speaker"
        try:
            _, current = await _off_loop(self._devices, kind)  # type: ignore[arg-type]
            chosen = self._chosen_device(kind) if self._chosen_device is not None else current is not None
        except Exception:
            current, chosen = None, True
        running = (name is None and not chosen) or (name is not None and chosen and name == current)
        try:
            self._save_device(kind, name)
        except Exception as exc:
            log.warning("Couldn't keep the %s choice", what, exc_info=True)
            await _off_loop(self._show, self.name, f"I couldn't keep that {what}: {exc}")
            return
        log.info("The %s is now %s (chosen in the tray)", what, name or "Windows' default")
        if running:
            return
        try:
            can_restart = bool(self._ready()) and bool(self._can_restart())
        except Exception:
            can_restart = False
        if not (can_restart and await self.restart()):
            await _off_loop(self._show, self.name, f"Got it. I'll use that {what} from the next time I start.")

    def restore(self) -> None:
        """At start: muted again if she was muted when she last stopped."""
        if self._load().get("muted"):
            self._set_listening(False)
            log.info("Listening is muted (from the tray, before she last stopped)")
            self._changed()

    async def _go_ahead(self, action: str, doing: str) -> bool:
        """The click is the yes, unless it would cut off something she's in the middle of: then it's asked. One
        shutdown or restart at a time: while one is chosen (its question open), another click does nothing."""
        if self.leaving or self._deciding:
            return False
        self._deciding = True
        try:
            try:
                busy = await self._busy()
            except Exception:
                log.warning("Couldn't tell whether she's busy", exc_info=True)
                busy = None
            if not busy:
                return self.leaving is None
            question = f"{self.name} is {busy}. {doing.capitalize()} now cuts that off.\n\n{action} anyway?"
            return await _off_loop(self._ask, self.name, question) and self.leaving is None
        finally:
            self._deciding = False

    def _leave(self, doing: str | None) -> None:
        self.leaving = doing
        self._changed()

    def _changed(self, only_if_different: bool = False) -> None:
        drawn = (self.tip(), self.kind(), self.motion())
        if only_if_different and drawn == self._drawn:
            return
        self._drawn = drawn
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.warning("Couldn't redraw the tray icon", exc_info=True)

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, **values: object) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self._load() | values), encoding="utf-8")
        except OSError:
            log.warning("Couldn't save the tray's state to %s", self.state_file, exc_info=True)


async def _off_loop(fn: Callable[..., T], *args: object) -> T:
    """``fn(*args)`` on a thread of its own, awaited. A daemon thread, not the loop's executor: a dialog left open
    must never hold up her exit (``asyncio.run`` waits for the executor's threads)."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[T] = loop.create_future()

    def settle(result: T | None, error: BaseException | None) -> None:
        if done.done():
            return
        if error is not None:
            done.set_exception(error)
        else:
            done.set_result(result)  # type: ignore[arg-type]

    def run() -> None:
        try:
            result, error = fn(*args), None
        except BaseException as exc:
            result, error = None, exc
        with contextlib.suppress(RuntimeError):  # the loop closed meanwhile: nobody waits any more
            loop.call_soon_threadsafe(settle, result, error)

    threading.Thread(target=run, name="octavia-tray-dialog", daemon=True).start()
    return await done


def ask_yes_no(title: str, text: str) -> bool:
    """A Windows Yes/No dialog in front of everything. Blocks: run it in a thread."""
    import win32api
    import win32con

    flags = win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SETFOREGROUND | win32con.MB_TOPMOST
    return win32api.MessageBox(0, text, title, flags) == win32con.IDYES


def show_message(title: str, text: str) -> None:
    """A Windows message box in front of everything. Blocks: run it in a thread."""
    import win32api
    import win32con

    win32api.MessageBox(0, text, title, win32con.MB_OK | win32con.MB_ICONINFORMATION | win32con.MB_SETFOREGROUND)


def _clock(at: float) -> str:
    """9:45 PM"""
    return time.strftime("%I:%M %p", time.localtime(at)).lstrip("0")


def _short(title: str, limit: int = 60) -> str:
    title = " ".join(title.split()).replace("&", "&&")  # a lone & would underline the next letter
    return title if len(title) <= limit else title[: limit - 3].rstrip() + "..."


def _keys(combo: str) -> str:
    """ctrl+alt+m -> Ctrl+Alt+M"""
    return "+".join(part.strip().capitalize() for part in combo.split("+") if part.strip())


def _open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.startfile(path)  # type: ignore[attr-defined]  # Windows only, as is the tray


# ------------------------------------------------------------------------------------------------------ the icon


# Her emblem, in units of the halo's outer radius from its centre (y down): a white raven perched on the halo's
# rim, its tail sweeping out over the halo at the lower left, two four-point stars at its back.
_RAVEN = (
    (0.03, -0.50), (0.17, -0.57), (0.30, -0.54), (0.38, -0.46), (0.52, -0.42), (0.63, -0.34), (0.68, -0.23),
    (0.52, -0.27), (0.38, -0.24), (0.37, -0.17), (0.44, -0.10), (0.49, 0.03), (0.48, 0.17), (0.40, 0.33),
    (0.26, 0.45), (0.23, 0.56), (0.22, 0.81), (0.16, 0.81), (0.15, 0.49), (0.08, 0.51), (0.06, 0.81),
    (0.00, 0.81), (-0.01, 0.49), (-0.11, 0.45), (-0.44, 0.75), (-0.62, 0.81), (-0.67, 0.67), (-0.35, 0.37),
    (-0.20, 0.17), (-0.04, -0.05), (-0.02, -0.21), (-0.07, -0.36),
)  # fmt: skip
_STARS = ((-0.36, -0.41, 0.19, 20), (-0.49, -0.12, 0.095, 24))  # (x, y, radius, the smallest icon it's drawn on)
_HALO = 0.94  # the halo's outer radius, as a share of half the icon
_HALO_INNER = 0.8
_NIGHT = (12, 12, 16)  # inside the halo
_RAVEN_COLOUR = {"muted": (175, 175, 180), "off": (150, 150, 156)}  # white otherwise


def _inside(points: tuple[tuple[float, float], ...], u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Which of the points (``u``, ``v``) are inside the polygon ``points`` (even-odd)."""
    inside = np.zeros(u.shape, bool)
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1], strict=True):
        if y0 != y1:
            crosses = (y0 > v) != (y1 > v)
            inside ^= crosses & (u < x0 + (v - y0) * (x1 - x0) / (y1 - y0))
    return inside


@functools.cache
def _shapes(size: int, scale: int) -> tuple[np.ndarray, ...]:
    """(u, v, r, raven, rim) on an icon ``size`` pixels across drawn ``scale`` times bigger: each subpixel's place in
    halo units, its distance from the centre, where the raven is, and a thin dark rim round the raven that parts its
    tail from the halo. The same for every frame, so worked out once per size."""
    n = size * scale
    ys, xs = np.mgrid[0:n, 0:n].astype(np.float32)
    u, v = ((a - (n - 1) / 2) / (_HALO * n / 2) for a in (xs, ys))
    raven = _inside(_RAVEN, u, v)
    rim = np.zeros_like(raven)
    for a in np.linspace(0, 2 * np.pi, 8, endpoint=False):
        rim |= _inside(_RAVEN, u + 0.08 * np.cos(a), v + 0.08 * np.sin(a))
    shapes = (u, v, np.hypot(u, v), raven, rim & ~raven)
    for array in shapes:
        array.flags.writeable = False  # shared by every frame: never drawn on
    return shapes


def emblem(
    size: int,
    kind: str | bool = "idle",
    badge: bool = False,
    motion: str | None = None,
    t: float = 0.0,
    level: float = 0.0,
) -> np.ndarray:
    """The icon's pixels, ``size`` x ``size`` RGBA (straight alpha): her white raven in a halo coloured by what she's
    doing (a key of ``_COLOURS``; True is "idle", False "muted", which greys the raven and adds a slash), with a purple
    dot at the lower right for ``badge``. The stars show from 20 px; below that they'd be a speck.

    ``motion`` draws one frame of an animation (a value of ``_STATE_MOTION``), all of it in the halo: "pulse"
    breathes (``t`` from 0 to 1 is one breath), "swirl" runs a bright arc round it (``t`` is one turn), "wave" lights
    and widens it as loud as ``level`` (0 to 1), a ripple running out through it as ``t`` steps on."""
    if isinstance(kind, bool):
        kind = "idle" if kind else "muted"
    scale = 4  # drawn bigger, then averaged down: smooth edges
    u, v, r, raven, rim = _shapes(size, scale)
    loud = min(1.0, max(0.0, level))
    breath = 0.5 - 0.5 * np.cos(2 * np.pi * t) if motion == "pulse" else 1.0  # 0 breathed out, 1 in
    outer = 0.96 + 0.04 * (loud if motion == "wave" else breath)  # breathed out, about half a pixel smaller at 16 px
    disc = r <= outer
    halo = disc & (r > _HALO_INNER)
    raven = raven & disc  # the tail's tip stops at the halo's edge
    # Lit from the upper left: lighter there, deeper towards the lower right.
    light = np.clip(1.0 - np.hypot(u + 0.45, v + 0.55) / 2.2, 0.0, 1.0)
    dark, bright = (np.array(c, np.float32) for c in _COLOURS.get(kind, _COLOURS["idle"]))
    if motion == "pulse":  # dimmer breathed out, as lit as ever breathed in
        dark, bright = dark * (0.6 + 0.4 * breath), bright * (0.75 + 0.25 * breath)
    elif motion == "wave":  # dimmer when she's quiet, as lit as ever when she's loud
        dark, bright = dark * (0.7 + 0.3 * loud), bright * (0.8 + 0.2 * loud)
    rgb = dark + (bright - dark) * light[..., None] ** 1.2
    if motion == "swirl":  # a bright arc with a fading tail, chasing round clockwise
        behind = np.mod(2 * np.pi * t - np.arctan2(v, u), 2 * np.pi)  # how far behind the arc's head
        rgb = rgb * 0.8 + (255 - rgb * 0.8) * (np.exp(-behind / 1.4) * 0.9)[..., None]
    if motion == "wave":  # a ripple from the halo's inside edge to its outside, brighter the louder she is
        at = _HALO_INNER + (outer - _HALO_INNER) * (t % 1.0 + 0.125)
        ripple = np.exp(-(((r - at) / 0.05) ** 2)) * (0.25 + 0.5 * loud)
        rgb += (255 - rgb) * ripple[..., None]
    rgb[~halo | rim] = _NIGHT
    bird = np.array(_RAVEN_COLOUR.get(kind, (248, 248, 250)), np.float32)
    rgb[raven] = bird
    for x, y, radius, smallest in _STARS:
        if size >= smallest:  # a four-point star: the curve |dx|^0.5 + |dy|^0.5 = 1
            star = np.sqrt(np.abs(u - x) / radius) + np.sqrt(np.abs(v - y) / radius) < 1.0
            rgb[star & ~raven] = bird
    alpha = disc.astype(np.float32)
    if kind == "muted":  # a slash from the upper left to the lower right
        across = np.abs(u - v) / np.sqrt(2)
        slash = (across < 0.085) & (r < outer - 0.04)
        rgb[slash] = (205, 45, 45)
    if badge:  # a purple dot with a white rim, at the lower right
        d = np.hypot(u - 0.52 / _HALO, v - 0.52 / _HALO) * _HALO
        rgb[d < 0.36] = (255, 255, 255)
        rgb[d < 0.27] = (150, 70, 220)
        alpha = np.maximum(alpha, (d < 0.36).astype(np.float32))
    pixels = np.concatenate([rgb * alpha[..., None], alpha[..., None] * 255], axis=-1)
    pixels = pixels.reshape(size, scale, size, scale, 4).mean(axis=(1, 3))
    a = pixels[..., 3:4]
    with np.errstate(invalid="ignore", divide="ignore"):
        pixels[..., :3] = np.where(a > 0, pixels[..., :3] * 255 / a, 0)
    return np.clip(pixels, 0, 255).astype(np.uint8)


# How each animation is cut into frames, played at ``FPS``.
FPS = 12
PULSE_FRAMES = 24  # a breath every 2 seconds
SWIRL_FRAMES = 12  # a turn a second
WAVE_LEVELS, WAVE_STEPS = 6, 4  # how loud, and where the ripple has got to


def frame_params(motion: str) -> list[tuple[float, float]]:
    """(t, level) of each of ``motion``'s frames, as ``emblem`` takes them; a wave's level by level."""
    if motion == "pulse":
        return [(i / PULSE_FRAMES, 0.0) for i in range(PULSE_FRAMES)]
    if motion == "swirl":
        return [(i / SWIRL_FRAMES, 0.0) for i in range(SWIRL_FRAMES)]
    if motion == "wave":
        return [(s / WAVE_STEPS, lv / (WAVE_LEVELS - 1)) for lv in range(WAVE_LEVELS) for s in range(WAVE_STEPS)]
    return [(0.0, 0.0)]


def frame_index(motion: str, tick: int, level: float = 0.0) -> int:
    """Which of ``frame_params(motion)`` to show at timer ``tick``; a wave's by her voice's ``level`` (0 to 1)."""
    if motion == "wave":
        step = round(min(1.0, max(0.0, level)) * (WAVE_LEVELS - 1))
        return step * WAVE_STEPS + tick // 2 % WAVE_STEPS  # the ripple steps on every other tick: calmer
    return tick % len(frame_params(motion))


class VoiceLevel:
    """How loud she is, for the wave: from the speaker's peaks, up at once and down over a few frames, so it moves
    with her syllables without flickering. With no speaker to ask, a loop that looks like talk."""

    def __init__(self, peak: Callable[[], float] | None) -> None:
        self._peak = peak  # the loudest sample played since it was last asked (``SoundDevicePlayer.take_peak``)
        self.level = 0.0

    def reset(self) -> None:
        """She's starting to speak: from silence, with the peak kept since she last spoke let go of."""
        self.level = 0.0
        if self._peak is not None:
            with contextlib.suppress(Exception):
                self._peak()

    def next(self, tick: int) -> float:
        if self._peak is None:
            self.level = float(0.55 + 0.35 * np.sin(tick * 0.9) * np.sin(tick * 0.37 + 1.0))
            return self.level
        try:
            peak = float(self._peak())
        except Exception:
            peak = 0.0
        heard = min(1.0, max(0.0, peak) / 0.6) ** 0.7  # her voice peaks at about 0.3 to 0.9
        self.level = max(heard, self.level * 0.6)
        return self.level


def icon_resource(rgba: np.ndarray) -> bytes:
    """An icon resource (as in an .ico file, without its directory) for ``CreateIconFromResourceEx``: a 32-bit
    bottom-up DIB, twice as tall to count its 1-bit AND mask, left empty since the alpha says what's see-through."""
    h, w = rgba.shape[:2]
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    bgra = np.ascontiguousarray(rgba[::-1][..., [2, 1, 0, 3]]).tobytes()
    mask = bytes(((w + 31) // 32) * 4 * h)
    return header + bgra + mask


WM_USER = 0x0400
WM_APP = 0x8000
WM_TRAY = WM_APP + 2  # the icon's callback message: lParam says what the mouse did
WM_TRAY_REDRAW = WM_APP + 3
WM_TRAY_STOP = WM_APP + 4
WM_TIMER = 0x0113
ANIMATION_TIMER = 1
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B


class TrayIcon:
    """The icon by the clock, on a hidden window's own thread. ``start`` returns once it's shown (or failed)."""

    def __init__(self, menu: TrayMenu, peak: Callable[[], float] | None = None) -> None:
        self.menu = menu
        self._hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._icons: dict[tuple[str, bool], int] = {}  # each still icon's handle
        self._frames: dict[tuple[tuple[str, bool], str], list[int]] = {}  # each animation's frames' handles
        self._voice = VoiceLevel(peak)  # ``peak``: the speaker's loudest sample since it was last asked
        self._motion: str | None = None  # the animation playing, or None: the timer is off
        self._tick = 0
        self._showing = 0  # the handle last given to the taskbar
        self._size = 16
        self._user32: Any = None
        self._shell32: Any = None
        self._nid: Any = None  # the NOTIFYICONDATAW each frame is sent with
        self._taskbar_created = 0
        self._shown = False
        self._stopped = False  # stop() was called: a window made after that closes at once
        self.error: str | None = None
        self._class_name = f"OctaviaTray-{os.getpid()}"
        menu.on_change = self.redraw

    def start(self, timeout: float = 5.0) -> bool:
        if not IS_WINDOWS:
            self.error = "the tray icon needs Windows"
            return False
        self._thread = threading.Thread(target=self._run, name="octavia-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.error = "the tray icon's window didn't start"
        if self.error is not None:
            self.stop(timeout=0)  # one that shows up late must not linger
            return False
        return True

    def redraw(self) -> None:
        if self._hwnd:
            import win32gui

            with contextlib.suppress(Exception):
                win32gui.PostMessage(self._hwnd, WM_TRAY_REDRAW, 0, 0)

    def stop(self, timeout: float = 3.0) -> None:
        self._stopped = True
        thread, hwnd = self._thread, self._hwnd
        if thread is None:
            return
        if hwnd:
            import win32gui

            with contextlib.suppress(Exception):
                win32gui.PostMessage(hwnd, WM_TRAY_STOP, 0, 0)
        if thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    # ---------------------------------------------------------------- on the window thread

    def _make_icon(self, rgba: np.ndarray) -> int:
        data = icon_resource(rgba)
        handle = self._user32.CreateIconFromResourceEx(data, len(data), True, 0x00030000, self._size, self._size, 0)
        if not handle:
            raise OSError(f"couldn't make the tray icon (error {ctypes.get_last_error()})")
        return int(handle)

    def _icon(self, kind: tuple[str, bool]) -> int:
        if kind not in self._icons:
            self._icons[kind] = self._make_icon(emblem(self._size, *kind))
        return self._icons[kind]

    def _animation(self, kind: tuple[str, bool], motion: str) -> None:
        """Every frame of ``motion`` in ``kind``'s colours, drawn once: playing it only swaps handles. All or none:
        the ones made before one fails are let go of."""
        handles: list[int] = []
        try:
            for t, level in frame_params(motion):
                handles.append(self._make_icon(emblem(self._size, *kind, motion, t, level)))
                time.sleep(0)  # a moment for the speaker's callback between frames, should she be talking already
        except Exception:
            import win32gui

            for handle in handles:
                with contextlib.suppress(Exception):
                    win32gui.DestroyIcon(handle)
            raise
        self._frames[kind, motion] = handles

    def _prerender(self) -> None:
        """Every animation she can show, drawn as she starts. Only these are ever played: none is drawn while
        she's busy talking, and one that couldn't be drawn shows as the still icon."""
        kinds = {(_STATE_KIND[state], motion) for state, motion in _STATE_MOTION.items()}
        try:
            for kind, motion in sorted(kinds):
                for badge in (False, True):
                    self._animation((kind, badge), motion)
        except Exception:
            log.warning("Couldn't draw the tray icon's animations ahead", exc_info=True)

    def _current(self) -> int:
        """The handle to show now: a still icon, or the animation's frame for this tick. The state may change
        between reading its motion and its colours (it's set on the loop): a pair never drawn shows still."""
        kind, motion = self.menu.kind(), self._motion
        frames = self._frames.get((kind, motion)) if motion is not None else None
        if frames is None:
            return self._icon(kind)
        level = self._voice.next(self._tick) if motion == "wave" else 0.0
        return frames[frame_index(motion, self._tick, level)]  # type: ignore[arg-type]

    def _follow_motion(self) -> None:
        """Start the timer for an animation, or stop it when the icon is still: then it doesn't run at all."""
        motion = self.menu.motion()
        if motion == self._motion:
            return
        if motion is None:
            self._user32.KillTimer(self._hwnd, ANIMATION_TIMER)
        elif self._motion is None:
            self._user32.SetTimer(self._hwnd, ANIMATION_TIMER, round(1000 / FPS), None)
        self._motion, self._tick = motion, 0
        if motion == "wave":
            self._voice.reset()  # what the speaker played before (a chime, the thinking sound) isn't her voice

    def _next_frame(self) -> None:
        """A timer tick: the next frame, sent only if it's a different picture. Through ctypes, which lets go of
        the GIL while the taskbar takes it (Explorer answers in its own time), so the speaker's callback never
        waits on Explorer."""
        self._follow_motion()
        if self._motion is None:
            return
        self._tick += 1
        handle = self._current()
        if handle == self._showing or self._nid is None:
            return
        self._nid.hIcon = handle
        if self._shell32.Shell_NotifyIconW(1, ctypes.byref(self._nid)):  # NIM_MODIFY
            self._showing = handle

    def _notify(self, action: int) -> None:
        import win32gui

        self._follow_motion()
        handle = self._current()
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        data = (self._hwnd, 1, flags, WM_TRAY, handle, self.menu.tip()[:127])
        try:
            win32gui.Shell_NotifyIcon(action, data)
            self._shown, self._showing = True, handle
        except Exception as exc:  # Explorer not up yet (an early autostart): TaskbarCreated adds it once it is
            if action == win32gui.NIM_MODIFY:
                with contextlib.suppress(Exception):
                    win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, data)
                    self._shown, self._showing = True, handle
                    return
            log.warning("Couldn't show the tray icon: %s", exc)

    def _load_api(self) -> None:
        """The few calls made through ctypes, and the NOTIFYICONDATAW each animation frame is sent with."""
        from ctypes import wintypes

        import win32api
        import win32con

        self._size = win32api.GetSystemMetrics(win32con.SM_CXSMICON) or 16
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateIconFromResourceEx.restype = ctypes.c_void_p
        user32.CreateIconFromResourceEx.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32,
                                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]  # fmt: skip
        user32.SetTimer.restype = ctypes.c_size_t
        user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
        user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        class NotifyIconData(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", ctypes.c_void_p),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", ctypes.c_void_p),
            ]  # fmt: skip

        nid = NotifyIconData()
        nid.cbSize, nid.hWnd, nid.uID, nid.uFlags = ctypes.sizeof(NotifyIconData), self._hwnd, 1, 0x2  # NIF_ICON
        self._user32, self._shell32, self._nid = user32, shell32, nid

    def _proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32gui

        try:
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._popup(hwnd)
                return 0
            if msg == WM_TIMER:
                if wparam == ANIMATION_TIMER:
                    self._next_frame()
                return 0
            if msg == WM_TRAY_REDRAW:
                self._notify(win32gui.NIM_MODIFY)
                return 0
            if msg == self._taskbar_created and msg:  # Explorer restarted: the icon is gone, add it again
                self._notify(win32gui.NIM_ADD)
                return 0
            if msg == WM_TRAY_STOP:
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:  # WM_DESTROY
                with contextlib.suppress(Exception):
                    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 1))
                win32gui.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("Tray icon callback failed (message 0x%04X)", msg)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _popup(self, hwnd: int) -> None:
        import win32con
        import win32gui

        menu = win32gui.CreatePopupMenu()
        keys: dict[int, str] = {}
        try:
            self._fill(menu, self.menu.items(), keys)
            x, y = win32gui.GetCursorPos()
            with contextlib.suppress(Exception):  # without it the menu doesn't close on a click elsewhere
                win32gui.SetForegroundWindow(hwnd)
            flags = win32con.TPM_RIGHTBUTTON | win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY
            chosen = win32gui.TrackPopupMenu(menu, flags, x, y, 0, hwnd, None)
            with contextlib.suppress(Exception):  # gone if she stopped while the menu was open
                win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)
        if keys.get(chosen):
            self.menu.choose(keys[chosen])

    def _fill(self, menu: int, items: list[MenuItem] | tuple[MenuItem, ...], keys: dict[int, str]) -> None:
        """Add ``items`` to ``menu`` (submenus too, destroyed with it), numbering the choices into ``keys``."""
        import win32con
        import win32gui

        for item in items:
            if not item.label:
                win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                continue
            flags = win32con.MF_STRING
            if item.checked:
                flags |= win32con.MF_CHECKED
            if not item.enabled:
                flags |= win32con.MF_GRAYED
            if item.children:
                sub = win32gui.CreatePopupMenu()
                self._fill(sub, item.children, keys)
                win32gui.AppendMenu(menu, flags | win32con.MF_POPUP, sub, item.label)
                continue
            command = len(keys) + 1
            win32gui.AppendMenu(menu, flags, command, item.label)
            keys[command] = item.key or ""
            if item.default:
                win32gui.SetMenuDefaultItem(menu, command, False)

    def _run(self) -> None:
        import win32api
        import win32gui

        instance = win32api.GetModuleHandle(None)
        try:
            self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            window_class.lpfnWndProc = self._proc
            win32gui.RegisterClass(window_class)
        except Exception as exc:
            self.error = f"couldn't register the tray window ({exc})"
            self._ready.set()
            return
        try:
            self._hwnd = win32gui.CreateWindow(self._class_name, self.menu.name, 0, 0, 0, 0, 0, 0, 0, instance, None)
            self._load_api()
            self._notify(win32gui.NIM_ADD)
            self._ready.set()
            if self._stopped:  # gave up on while it was starting
                win32gui.PostMessage(self._hwnd, WM_TRAY_STOP, 0, 0)
            self._prerender()
            win32gui.PumpMessages()
        except Exception as exc:
            self.error = f"the tray icon failed ({exc})"
            log.exception("The tray icon failed")
        finally:
            self._ready.set()
            self._hwnd = None
            for handle in [*self._icons.values(), *(h for frames in self._frames.values() for h in frames)]:
                with contextlib.suppress(Exception):
                    win32gui.DestroyIcon(handle)
            self._icons.clear()
            self._frames.clear()
            with contextlib.suppress(Exception):
                win32gui.UnregisterClass(self._class_name, instance)
