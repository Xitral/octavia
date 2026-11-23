"""Application tools: open, list, focus and close apps, and re-scan installed apps.

Opening only ever launches entries from the installed-app index (or an
allow-listed URI such as ``ms-settings:bluetooth``); when a spoken name is
ambiguous the tool returns candidates for the model to ask about rather than
guessing. Closing posts ``WM_CLOSE`` (like clicking X) to that app's windows
and never terminates a process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field

from pydantic import Field
from rapidfuzz import fuzz, process

from octavia.platform import windows as win
from octavia.platform.apps import AppIndex, LaunchRefused, looks_like_path, looks_like_uri, normalize
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

NOT_WINDOWS = "App control only works on Windows."
INDEX_CACHE = "data/app_index.json"
REFRESH_ON_MISS_AFTER_S = 120.0  # an unknown name may be an app installed since the last scan
MAX_APPS_LISTED = 20
MAX_TITLES_PER_APP = 3
TITLE_CHARS = 60
CLOSE_WAIT_S = 2.0

# Processes whose version-resource description is unhelpful for speech.
_APP_NAME_OVERRIDES = {
    "explorer.exe": "File Explorer",
    "windowsterminal.exe": "Terminal",
    "applicationframehost.exe": "",  # store app we couldn't resolve: use the window title
}

_index: AppIndex | None = None
_index_key: tuple | None = None
_index_lock: asyncio.Lock | None = None
_index_lock_loop: asyncio.AbstractEventLoop | None = None


# --------------------------------------------------------------------------- app index


def _lock() -> asyncio.Lock:
    """One lock per event loop, so the module state also works across test loops."""
    global _index_lock, _index_lock_loop
    loop = asyncio.get_running_loop()
    if _index_lock is None or _index_lock_loop is not loop:
        _index_lock, _index_lock_loop = asyncio.Lock(), loop
    return _index_lock


def _config_key(ctx: ToolContext) -> tuple:
    tools = ctx.config.tools
    return (
        str(ctx.config.resolve_path(INDEX_CACHE)),
        tuple(sorted(tools.app_aliases.items())),
        tuple(tools.extra_uri_prefixes),
        tools.app_index_ttl_h,
    )


def _new_index(ctx: ToolContext) -> AppIndex:
    tools = ctx.config.tools
    return AppIndex(
        ctx.config.resolve_path(INDEX_CACHE),
        tools.app_aliases,
        ttl_s=tools.app_index_ttl_h * 3600,
        extra_uri_prefixes=tools.extra_uri_prefixes,
    )


async def get_app_index(ctx: ToolContext, *, refresh: bool = False) -> AppIndex:
    """The shared index, built lazily in a worker thread (a cold scan takes about 2 s).

    Call it once at startup to warm the cache so the first "open ..." is instant.
    """
    global _index, _index_key
    key = _config_key(ctx)
    async with _lock():
        if _index is None or _index_key != key:
            index = _new_index(ctx)
            await asyncio.to_thread(index.load, refresh)
            _index, _index_key = index, key
        elif refresh or _index.is_stale():
            await asyncio.to_thread(_index.load, refresh)
        return _index


# --------------------------------------------------------------------------- running windows


@dataclass
class AppGroup:
    app: str  # speakable name, e.g. "Visual Studio Code"
    process: str  # image name, e.g. "Code.exe"
    windows: list[win.WindowInfo] = field(default_factory=list)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.process)[0]


@dataclass
class WindowMatch:
    app: str | None
    windows: list[win.WindowInfo]
    candidates: list[str]
    by_title: bool = False


def app_display_name(info: win.WindowInfo) -> str:
    override = _APP_NAME_OVERRIDES.get(info.process.lower())
    if override is not None:
        return override or info.title
    description = win.exe_description(info.exe) if info.exe else None
    if description and 2 <= len(description) <= 40:
        return description
    return os.path.splitext(info.process)[0] or info.title


def group_windows(windows: list[win.WindowInfo]) -> list[AppGroup]:
    """Group Alt-Tab windows by app, keeping Z order (most recently used app first)."""
    groups: dict[tuple[str, str], AppGroup] = {}
    for info in windows:
        app = app_display_name(info)
        key = (info.process.lower(), app)
        if key not in groups:
            groups[key] = AppGroup(app=app, process=info.process)
        groups[key].windows.append(info)
    return list(groups.values())


def _running_apps() -> list[AppGroup]:
    return group_windows(win.list_windows())


def match_windows(query: str, groups: list[AppGroup], aliases: dict[str, str] | None = None) -> WindowMatch:
    """Find the app (or specific windows) the user means, by app name, process name or window title."""
    q = normalize(query)
    if not q:
        return WindowMatch(None, [], [])
    queries = [q]
    alias = {normalize(k): v for k, v in (aliases or {}).items()}.get(q)
    if alias and not looks_like_uri(alias):
        queries.insert(0, normalize(alias))

    def names(g: AppGroup) -> set[str]:
        return {normalize(g.app), normalize(g.stem)} - {""}

    def pick(found: list[AppGroup]) -> WindowMatch | None:
        apps = list(dict.fromkeys(g.app for g in found))
        if len(apps) == 1:
            return WindowMatch(apps[0], [w for g in found for w in g.windows], [])
        if apps:
            return WindowMatch(None, [], apps[:8])
        return None

    for qq in queries:
        if hit := pick([g for g in groups if qq in names(g)]):
            return hit
    for qq in queries:
        words = set(qq.split())
        if hit := pick([g for g in groups if any(words <= set(n.split()) for n in names(g))]):
            return hit

    words = set(q.split())
    titled = [(g, w) for g in groups for w in g.windows if words <= set(normalize(w.title).split())]
    if titled:
        apps = list(dict.fromkeys(g.app for g, _ in titled))
        if len(apps) == 1:
            return WindowMatch(apps[0], [w for _, w in titled], [], by_title=True)
        return WindowMatch(None, [], [f"{g.app} ({_short(w.title)})" for g, w in titled[:8]])

    choices = {n: g for g in groups for n in names(g)}
    top = process.extract(q, list(choices), scorer=fuzz.WRatio, processor=None, limit=5)
    ranked = []  # best score per app
    for name, score, _ in top:
        if choices[name].app not in [g.app for g, _ in ranked]:
            ranked.append((choices[name], score))
    if ranked and ranked[0][1] >= 90 and (len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= 8):
        best = ranked[0][0]
        return WindowMatch(best.app, list(best.windows), [])
    return WindowMatch(None, [], [g.app for g, score in ranked if score >= 60])


def _short(title: str, limit: int = TITLE_CHARS) -> str:
    title = " ".join(title.split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


# Ancestors that are the Windows shell or system, not "the window I'm running in". explorer.exe
# in particular launches autostart entries AND hosts every File Explorer folder window.
_SHELL_PROCESSES = {"explorer.exe", "sihost.exe", "svchost.exe", "userinit.exe", "services.exe", "wininit.exe",
                    "winlogon.exe", "taskhostw.exe", "smss.exe", "csrss.exe"}  # fmt: skip


def _protected_pids() -> set[int]:
    """This process and its ancestors up to the shell (e.g. the terminal it runs in): never close those."""
    pids = {os.getpid()}
    try:
        import psutil

        for parent in psutil.Process().parents():
            try:
                if parent.name().lower() in _SHELL_PROCESSES:
                    break
            except psutil.Error:
                break
            pids.add(parent.pid)
    except Exception:
        pass
    return pids


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --------------------------------------------------------------------------- tools


class OpenApplicationArgs(ToolArgs):
    name: str = Field(
        min_length=1,
        max_length=120,
        description="As said, e.g. 'VS Code' or 'ms-settings:bluetooth'. Never a path, command or arguments.",
    )


class OpenApplication(Tool):
    name = "open_application"
    description = (
        "Open an installed app or an ms-settings: page. If ambiguous it lists close matches: ask the user, "
        "don't guess. For websites use open_url."
    )
    Args = OpenApplicationArgs
    risk = RiskLevel.SAFE
    category = "apps"
    timeout_s = 45.0

    async def run(self, args: OpenApplicationArgs, ctx: ToolContext) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.error(NOT_WINDOWS)
        name = args.name
        index = await get_app_index(ctx)
        entry, candidates = index.resolve(name)
        special = looks_like_uri(name) or looks_like_path(name)
        if entry is None and not special and index.age_s > REFRESH_ON_MISS_AFTER_S:
            index = await get_app_index(ctx, refresh=True)
            entry, candidates = index.resolve(name)

        if entry is None:
            if looks_like_uri(name):
                if name.lower().startswith(("http:", "https:")):
                    return ToolResult.error("That's a web address; use open_url for websites.")
                allowed = ", ".join(index.uri_prefixes)
                return ToolResult.error(f"Opening '{name.split(':', 1)[0]}:' links isn't allowed. Allowed: {allowed}.")
            if looks_like_path(name):
                return ToolResult.error("I can only open installed apps by name, not files, paths or commands.")
            if candidates:
                return ToolResult.error(
                    f"No confident match for '{name}'. Closest installed apps: {', '.join(candidates)}. "
                    "Ask the user which one they meant (or whether it's installed), then call open_application "
                    "with that exact name.",
                    data={"candidates": candidates},
                )
            return ToolResult.error(
                f"No installed app matches '{name}'. It may not be installed; the user can add a spoken alias "
                "under tools.app_aliases in the config."
            )

        try:
            target = await asyncio.to_thread(index.launch, entry)
        except LaunchRefused as exc:
            return ToolResult.error(f"Refused to open that: {exc}.")
        except OSError as exc:
            return ToolResult.error(f"Windows couldn't start {entry.name}: {exc.strerror or exc}.")
        return ToolResult.success(
            f"Opened {entry.name}.", data={"app": entry.name, "kind": entry.kind, "target": target}
        )


class ListRunningApps(Tool):
    name = "list_running_apps"
    description = "Apps with open windows, and their titles."
    risk = RiskLevel.SAFE
    category = "apps"

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.error(NOT_WINDOWS)
        groups = await asyncio.to_thread(_running_apps)
        if not groups:
            return ToolResult.success("No app windows are open.", data={"apps": []})
        lines = [f"{_plural(len(groups), 'app')} with open windows (most recently used first):"]
        for g in groups[:MAX_APPS_LISTED]:
            titles = [_short(w.title) for w in g.windows if normalize(w.title) != normalize(g.app)]
            shown = ", ".join(f'"{t}"' for t in titles[:MAX_TITLES_PER_APP])
            if len(titles) > MAX_TITLES_PER_APP:
                shown += f" and {len(titles) - MAX_TITLES_PER_APP} more"
            count = f" ({len(g.windows)} windows)" if len(g.windows) > 1 else ""
            state = " [minimized]" if all(w.minimized for w in g.windows) else ""
            lines.append(f"- {g.app}{count}{state}" + (f": {shown}" if shown else ""))
        if len(groups) > MAX_APPS_LISTED:
            lines.append(f"...and {len(groups) - MAX_APPS_LISTED} more apps.")
        data = {
            "apps": [
                {
                    "app": g.app,
                    "process": g.process,
                    "windows": [{"title": w.title, "pid": w.pid, "minimized": w.minimized} for w in g.windows],
                }
                for g in groups
            ]
        }
        return ToolResult.success("\n".join(lines), data=data)


class WindowArgs(ToolArgs):
    name: str = Field(
        min_length=1,
        max_length=120,
        description="App name or words from a window title.",
    )


def _no_window_message(name: str, match: WindowMatch, groups: list[AppGroup]) -> str:
    if match.candidates:
        return f"Not sure which window '{name}' means. Possible matches: {', '.join(match.candidates)}. Ask the user."
    if groups:
        return f"No open window matches '{name}'. Open apps: {', '.join(g.app for g in groups[:10])}."
    return f"No open window matches '{name}'; no app has an open window."


class FocusWindow(Tool):
    name = "focus_window"
    description = "Bring an open app's window to the front. If it isn't running, use open_application."
    Args = WindowArgs
    risk = RiskLevel.SAFE
    category = "apps"

    async def run(self, args: WindowArgs, ctx: ToolContext) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.error(NOT_WINDOWS)
        groups = await asyncio.to_thread(_running_apps)
        match = match_windows(args.name, groups, ctx.config.tools.app_aliases)
        tab = ""
        if not match.windows and not match.candidates:
            # A browser tab is no window ("Overleaf", "ChatGPT"): brought to the front of its window, then that
            # window, which is named for its tab in front now (2026-11-04: both names found no window).
            from octavia.tools.extensions import _uia

            found = await _uia.find_tab(args.name)
            if found is not None:
                await asyncio.sleep(0.4)
                groups = await asyncio.to_thread(_running_apps)
                match = match_windows(found[1], groups, ctx.config.tools.app_aliases)
                tab = found[1]
        if not match.windows:
            return ToolResult.error(_no_window_message(args.name, match, groups), data={"candidates": match.candidates})
        target = match.windows[0]  # Z order: the one used most recently
        focused = await asyncio.to_thread(win.focus_window, target.hwnd)
        data = {"app": match.app, "title": target.title, "focused": focused, "tab": tab}
        if focused:
            if tab:
                return ToolResult.success(
                    f"Switched to the tab “{tab}” and brought {match.app} to the front.", data=data
                )
            return ToolResult.success(f"Brought {match.app} to the front.", data=data)
        return ToolResult.error(
            f"Windows didn't let me bring {match.app} to the front (it blocks background apps from stealing "
            "focus); its taskbar button may be flashing instead.",
            data=data,
        )


class CloseApplication(Tool):
    name = "close_application"
    description = (
        "Politely close an app's windows, like clicking X; a window-title match closes just that window. "
        "Never force-kills."
    )
    Args = WindowArgs
    risk = RiskLevel.CONFIRM
    category = "apps"

    async def describe_call(self, args: WindowArgs, ctx: ToolContext) -> str:
        if sys.platform != "win32":
            return f"close {args.name}"
        try:
            groups = await asyncio.to_thread(_running_apps)
        except Exception:
            return f"close {args.name}"
        match = match_windows(args.name, groups, ctx.config.tools.app_aliases)
        if not match.windows:
            return f"close {args.name}"
        if match.by_title and len(match.windows) == 1:
            return f"close the {match.app} window {_short(match.windows[0].title, 40)}"
        if len(match.windows) > 1:
            return f"close all {len(match.windows)} {match.app} windows"
        return f"close {match.app}"

    async def run(self, args: WindowArgs, ctx: ToolContext) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.error(NOT_WINDOWS)
        groups = await asyncio.to_thread(_running_apps)
        match = match_windows(args.name, groups, ctx.config.tools.app_aliases)
        if not match.windows:
            return ToolResult.error(_no_window_message(args.name, match, groups), data={"candidates": match.candidates})
        protected = _protected_pids()
        targets = [w for w in match.windows if w.pid not in protected]
        if not targets:
            return ToolResult.error(f"{match.app} is the window I'm running in, so I won't close it.")
        still_open, blocked = await asyncio.to_thread(_close_and_wait, [w.hwnd for w in targets])
        closed = len(targets) - len(still_open) - len(blocked)
        data = {
            "app": match.app,
            "requested": len(targets),
            "closed": closed,
            "still_open": len(still_open),
            "blocked": len(blocked),
        }
        if blocked and not still_open and not closed:
            return ToolResult.error(
                f"Windows wouldn't let me close {match.app}: it runs as administrator, so the user has to close it.",
                data=data,
            )
        if not still_open and not blocked:
            what = "its window" if len(targets) == 1 else f"all {len(targets)} windows"
            return ToolResult.success(f"Closed {match.app} ({what}).", data=data)
        notes = []
        if still_open:
            notes.append(
                f"{_plural(len(still_open), 'window')} still open, probably asking to save changes "
                "(the user needs to answer that prompt)"
            )
        if blocked:
            notes.append(f"{_plural(len(blocked), 'window')} runs as administrator and couldn't be closed")
        return ToolResult.success(f"Asked {match.app} to close; " + "; ".join(notes) + ".", data=data)


def _close_and_wait(hwnds: list[int]) -> tuple[list[int], list[int]]:
    """Post WM_CLOSE to each window, then wait briefly.

    Returns (windows still open after the request, windows Windows refused to message).
    """
    posted: list[int] = []
    blocked: list[int] = []
    for hwnd in hwnds:
        if win.close_window(hwnd):
            posted.append(hwnd)
        elif win.last_error() != win.ERROR_INVALID_WINDOW_HANDLE:  # already gone counts as closed
            blocked.append(hwnd)  # e.g. access denied: an elevated window (UIPI)
    pending = list(posted)
    deadline = time.monotonic() + CLOSE_WAIT_S
    while pending and time.monotonic() < deadline:
        time.sleep(0.1)
        pending = [h for h in pending if win.window_is_open(h)]
    return pending, blocked


class RefreshAppIndex(Tool):
    name = "refresh_app_index"
    description = "Re-scan installed apps when open_application misses a new install."
    risk = RiskLevel.SAFE
    category = "apps"
    timeout_s = 60.0

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.error(NOT_WINDOWS)
        index = await get_app_index(ctx, refresh=True)
        count = len(index.entries)
        return ToolResult.success(f"Re-scanned installed apps: {count} found.", data={"count": count})


TOOLS = [OpenApplication, ListRunningApps, FocusWindow, CloseApplication, RefreshAppIndex]
