"""Spotify: start a song, album, playlist, artist or the user's Liked Songs.

When the user has connected Spotify's Web API, playback goes through it (``spotify_player.py``), which can also find
a song by name and play it. The desktop app below is the fallback when the API isn't connected or fails.

Spotify registers the ``spotify:`` URI scheme, so opening ``spotify:track:<id>`` hands the item to the app without
the Web API (which needs OAuth). A name without a link can't be turned into an item offline, so it opens Spotify's
search results instead. Whether Spotify actually started the item is read back from its Windows media session, so
the reply never claims music is playing when it isn't.

A running Spotify can ignore ``spotify:collection:tracks`` and stay on whatever page it showed, so Liked Songs is also
opened with Spotify's own shortcut (Alt+Shift+S), pressed only once Spotify's window is really in front so the keys
never reach another app.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field

from octavia.platform import windows as win
from octavia.platform.apps import _startfile
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.builtin import system_tools
from octavia.tools.extensions.spotify_library import NOT_CONNECTED, MissingScopes, NotConnected, SpotifyError
from octavia.tools.extensions.spotify_player import RECONNECT, SearchKind, SpotifyPlayer, play_with_api

log = logging.getLogger(__name__)

SPOTIFY_APP = "spotify"
SPOTIFY_EXE = "spotify.exe"
PLAY_WAIT_S = 6.0
WINDOW_WAIT_S = 8.0  # a cold start takes a few seconds to show its window
UI_SETTLE_S = 2.0  # a window that just appeared ignores shortcuts until its page has loaded
LIKED_SONGS_KEYS = (win.VK_MENU, win.VK_SHIFT, ord("S"))
_POLL_S = 0.5
_KINDS = "track|album|playlist|artist|episode|show"
_LINK = re.compile(
    rf"^https?://open\.spotify\.com/(?:intl-[a-z]+(?:-[a-z]+)?/)?(?:embed/)?({_KINDS})/([A-Za-z0-9]{{22}})(?:[/?#].*)?$",
    re.IGNORECASE,
)
_URI = re.compile(rf"^spotify:({_KINDS}):([A-Za-z0-9]{{22}})$", re.IGNORECASE)
_LIKED = {"liked songs", "my liked songs", "liked", "liked music", "saved songs", "my saved songs"}


def spotify_target(what: str) -> tuple[str, str]:
    """``(uri, kind)`` for a link, a URI, Liked Songs, or else a search ("search")."""
    what = what.strip()
    if match := _LINK.match(what) or _URI.match(what):
        kind = match.group(1).lower()
        return f"spotify:{kind}:{match.group(2)}", kind
    if " ".join(what.lower().split()) in _LIKED or what.lower() in ("spotify:collection", "spotify:collection:tracks"):
        return "spotify:collection:tracks", "Liked Songs"
    return f"spotify:search:{quote(what, safe='')}", "search"


async def _now_playing() -> dict[str, Any] | None:
    try:
        return await system_tools.app_now_playing(SPOTIFY_APP)
    except Exception:
        log.debug("Couldn't read Spotify's media session", exc_info=True)
        return None


def _started(state: dict[str, Any] | None, before: dict[str, Any] | None) -> bool:
    """Playing, and not just the track that was already playing before the item was opened."""
    if not state or not state["playing"]:
        return False
    return not before or not before["playing"] or state["title"] != before["title"]


async def _wait_for_playback(before: dict[str, Any] | None) -> dict[str, Any] | None:
    """Spotify's state once it plays something new, or its last state when it doesn't within ``PLAY_WAIT_S``."""
    deadline = time.monotonic() + PLAY_WAIT_S
    state = None
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_S)
        state = await _now_playing()
        if _started(state, before):
            break
    return state


def _spotify_window() -> int:
    return next((w.hwnd for w in win.list_windows() if w.process.lower() == SPOTIFY_EXE), 0)


def _press_liked_songs(had_window: bool) -> bool:
    """Press the Liked Songs shortcut in Spotify's window; False when that window never got to the front."""
    deadline = time.monotonic() + WINDOW_WAIT_S
    while True:
        if hwnd := _spotify_window():
            if not had_window:
                time.sleep(UI_SETTLE_S)
                had_window = True
            if win.focus_window(hwnd) and win.foreground_window() == hwnd:
                win.tap_combo(LIKED_SONGS_KEYS)
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)


async def _has_window() -> bool:
    try:
        return bool(await asyncio.to_thread(_spotify_window))
    except Exception:
        log.debug("Couldn't list Spotify's windows", exc_info=True)
        return False


async def _show_liked_songs(had_window: bool) -> bool:
    try:
        return await asyncio.to_thread(_press_liked_songs, had_window)
    except Exception:
        log.warning("Couldn't switch Spotify to Liked Songs", exc_info=True)
        return False


class PlayOnSpotifyArgs(ToolArgs):
    what: str = Field(
        min_length=1,
        max_length=200,
        description="Spotify link/URI, 'liked songs', or search words.",
    )
    kind: SearchKind = Field(
        default="track",
        description="For plain words, e.g. artist for 'play some Drake'.",
    )


class PlayOnSpotify(Tool):
    name = "play_on_spotify"
    description = (
        "Play a song, album, playlist, artist or Liked Songs on Spotify (plain words search for the best "
        "match). To just resume, use media_control play with app 'Spotify'."
    )
    Args = PlayOnSpotifyArgs
    risk = RiskLevel.SAFE
    category = "media"
    timeout_s = 45.0  # waiting for a closed Spotify to start, then the app fallback

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def run(self, args: PlayOnSpotifyArgs, ctx: ToolContext) -> ToolResult:
        uri, kind = spotify_target(args.what)
        note = ""
        player = SpotifyPlayer(ctx, self._transport)
        if await player.usable():
            try:
                return await play_with_api(player, args.what, uri, kind, args.kind)
            except NotConnected as exc:
                note = f" ({exc}. {NOT_CONNECTED})"
            except MissingScopes:
                note = f" ({RECONNECT}.)"
            except SpotifyError as exc:
                log.warning("Spotify's Web API couldn't play %s: %s", kind, exc)
                note = f" (Spotify's Web API failed: {exc}, so the app was used instead.)"
        if sys.platform != "win32":
            return ToolResult.error(f"Spotify's app can only be controlled on Windows.{note}")
        return await self._play_in_app(args, uri, kind, note)

    async def _play_in_app(self, args: PlayOnSpotifyArgs, uri: str, kind: str, api_note: str) -> ToolResult:
        """The app fallback: open the item's ``spotify:`` link and watch its media session for playback."""
        before = None if kind == "search" else await _now_playing()
        had_window = kind == "Liked Songs" and await _has_window()
        try:
            await asyncio.to_thread(_startfile, uri)
        except OSError as exc:
            return ToolResult.error(
                f"Windows couldn't open Spotify ({exc.strerror or exc}); is it installed?{api_note}"
            )
        data: dict[str, Any] = {"uri": uri, "kind": kind}
        if kind == "search":
            return ToolResult.success(
                f"Opened Spotify's search results for '{args.what}'. I can't pick a result myself, so the user "
                f"chooses one there (or gives a Spotify link to play).{api_note}",
                data=data,
            )
        opened, note = f"Opened the {kind} in Spotify", api_note
        if kind == "Liked Songs":
            data["shown"] = await _show_liked_songs(had_window)
            opened = "Opened Liked Songs in Spotify"
            if not data["shown"]:
                opened = "Asked Spotify to open Liked Songs"
                note += (
                    " I couldn't bring Spotify's window to the front to switch it to Liked Songs, so it may still "
                    "show its previous page; the user can click Liked Songs in Spotify's library."
                )
        state = await _wait_for_playback(before)
        if state and _started(state, before):
            title = f"{state['title']} by {state['artist']}" if state["artist"] else state["title"] or "it"
            return ToolResult.success(f"{opened}; now playing {title}.{note}", data={**data, **state})
        return ToolResult.success(
            f"{opened}, but it hasn't started playing. The user can press play on it there; "
            f"resuming with media_control would play Spotify's previous queue instead.{note}",
            data={**data, "playing": False},
        )


TOOLS = [PlayOnSpotify]
