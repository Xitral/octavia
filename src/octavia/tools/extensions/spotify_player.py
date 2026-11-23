"""Spotify playback through the Web API's player endpoints.

Clicking around the desktop app can't pick a search result or reliably start Liked Songs, and the media keys only
reach whichever app played last. The Web API starts exactly the item asked for, on the user's active device, and
says what's playing, so ``play_on_spotify`` and ``media_control`` (when it's aimed at Spotify) use it whenever the
user has connected Spotify, and fall back to the app only when the API can't be used or fails.

When no device is active (the app is closed, or idle long enough that Spotify forgot it), the app is opened and
playback is moved to its device before the command is sent again. Controlling playback needs Spotify Premium and
scopes a sign-in made for reading playlists lacks; that case is told apart so the user is asked to connect once more.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import Field, model_validator

from octavia.platform.apps import _startfile
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.spotify_library import (
    NOT_CONNECTED,
    PLAYBACK_SCOPES,
    MissingScopes,
    NoActiveDevice,
    NotConnected,
    SpotifyAuth,
    SpotifyError,
    api_call,
    client_id,
    setup_hint,
)

log = logging.getLogger(__name__)

DEVICE_WAIT_S = 15.0  # a cold start takes several seconds before Spotify lists the app as a device
CONFIRM_WAIT_S = 3.0
LIKED_LIMIT = 50
_DEVICE_POLL_S = 1.0
_CONFIRM_POLL_S = 0.5
_ONE_ITEM_KINDS = ("track", "episode")

RECONNECT = (
    "Spotify's sign-in was made before playback control was asked for: use connect_spotify once so the user can "
    "allow it, then songs start directly"
)

SearchKind = Literal["track", "album", "playlist", "artist"]


async def _open_spotify() -> None:
    await asyncio.to_thread(_startfile, "spotify:")


@dataclass(frozen=True)
class Found:
    """The best search result."""

    kind: str
    uri: str
    name: str
    by: str = ""

    def describe(self) -> str:
        return f"{self.name} by {self.by}" if self.by else self.name


def _names(people: Any) -> str:
    if not isinstance(people, list):
        return ""
    return ", ".join(str(p.get("name") or "").strip() for p in people if isinstance(p, dict) and p.get("name"))


def parse_search(payload: Any, kind: str) -> Found | None:
    """The first usable item of one kind in a ``/search`` answer (Spotify pads playlist results with nulls)."""
    group = payload.get(f"{kind}s") if isinstance(payload, dict) else None
    items = group.get("items") if isinstance(group, dict) else None
    for item in items or []:
        if not isinstance(item, dict) or not item.get("uri"):
            continue
        if kind == "playlist":
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            by = str(owner.get("display_name") or "").strip()
        else:
            by = _names(item.get("artists"))
        return Found(kind, str(item["uri"]), str(item.get("name") or "").strip() or "(untitled)", by)
    return None


def parse_state(payload: Any) -> dict[str, Any] | None:
    """What ``/me/player/currently-playing`` says, or None when nothing is loaded."""
    if not isinstance(payload, dict) or not isinstance(payload.get("item"), dict):
        return None
    item = payload["item"]
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    show = item.get("show") if isinstance(item.get("show"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    linked = item.get("linked_from") if isinstance(item.get("linked_from"), dict) else {}
    return {
        "playing": bool(payload.get("is_playing")),
        "title": str(item.get("name") or "").strip(),
        "artist": _names(item.get("artists")) or str(show.get("name") or "").strip(),
        "album": str(album.get("name") or "").strip(),
        "uri": str(item.get("uri") or ""),
        "context": str(context.get("uri") or ""),
        "linked": str(linked.get("uri") or ""),
    }


def _pick_device(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """The active device, else the app on a computer (the one just opened here), else any that takes commands."""
    usable = [d for d in devices if not d.get("is_restricted")] or devices
    for wanted in (lambda d: d.get("is_active"), lambda d: str(d.get("type", "")).lower() == "computer"):
        if match := next((d for d in usable if wanted(d)), None):
            return match
    return usable[0]


def title_of(state: dict[str, Any]) -> str:
    return f"{state['title']} by {state['artist']}" if state["artist"] else state["title"] or "something untitled"


class SpotifyPlayer:
    """The user's Spotify playback, through the Web API."""

    def __init__(
        self,
        ctx: ToolContext,
        transport: httpx.AsyncBaseTransport | None = None,
        open_app: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._auth = SpotifyAuth(ctx, transport)
        self._transport = transport
        self._open_app = open_app or _open_spotify

    async def usable(self) -> bool:
        """Whether the user set up and connected the Web API at all."""
        return bool(client_id()) and await self._auth.load() is not None

    async def check_scopes(self) -> None:
        granted = await self._auth.granted_scopes()
        if granted is not None and not PLAYBACK_SCOPES <= granted:
            raise MissingScopes("the Spotify sign-in doesn't allow playback control yet")

    async def call(
        self, method: str, path: str, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None
    ) -> Any:
        return await api_call(self._auth, self._transport, method, path, params=params, json=json)

    async def devices(self) -> list[dict[str, Any]]:
        answer = await self.call("GET", "/me/player/devices")
        found = answer.get("devices") if isinstance(answer, dict) else None
        return [d for d in found or [] if isinstance(d, dict) and d.get("id")]

    async def wake(self) -> dict[str, Any]:
        """Make a device the active one, opening the Spotify app first when it has none, and return it."""
        found = await self.devices()
        if not found:
            try:
                await self._open_app()
            except Exception as exc:
                log.info("Couldn't open the Spotify app: %s", exc)
                raise NoActiveDevice("Spotify isn't open on any device and couldn't be started here") from None
            deadline = time.monotonic() + DEVICE_WAIT_S
            while not found and time.monotonic() < deadline:
                await asyncio.sleep(_DEVICE_POLL_S)
                found = await self.devices()
            if not found:
                raise NoActiveDevice("the Spotify app was opened, but it didn't show up as a device in time")
        device = _pick_device(found)
        await self.call("PUT", "/me/player", json={"device_ids": [device["id"]], "play": False})
        return device

    async def on_device(
        self, method: str, path: str, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None
    ) -> str:
        """Send a player command; with no active device, wake one and send it there. The woken device's name."""
        try:
            await self.call(method, path, params, json)
            return ""
        except NoActiveDevice:
            log.info("Spotify has no active device; opening one for %s %s", method, path)
        device = await self.wake()
        await self.call(method, path, {**(params or {}), "device_id": device["id"]}, json)
        return str(device.get("name") or "this computer")

    async def state(self) -> dict[str, Any] | None:
        return parse_state(await self.call("GET", "/me/player/currently-playing", {"additional_types": "episode"}))

    async def search(self, query: str, kind: str) -> Found | None:
        return parse_search(await self.call("GET", "/search", {"q": query, "type": kind, "limit": 5}), kind)

    async def liked_tracks(self) -> list[str]:
        answer = await self.call("GET", "/me/tracks", {"limit": LIKED_LIMIT})
        items = answer.get("items") if isinstance(answer, dict) else None
        tracks = [i.get("track") for i in items or [] if isinstance(i, dict)]
        return [str(t["uri"]) for t in tracks if isinstance(t, dict) and t.get("uri") and not t.get("is_local")]

    async def play(self, uri: str, kind: str, uris: list[str] | None = None) -> str:
        """Start an item (or a list of tracks); the device that had to be woken, if any."""
        if uris is None:
            body: dict[str, Any] = {"uris": [uri]} if kind in _ONE_ITEM_KINDS else {"context_uri": uri}
        else:
            body = {"uris": uris}
        return await self.on_device("PUT", "/me/player/play", json=body)

    async def confirm(self, expected: set[str]) -> dict[str, Any] | None:
        """What's playing once it's one of ``expected`` (a track, or the album/playlist/artist it's from), or None
        when Spotify doesn't say so within ``CONFIRM_WAIT_S``."""
        deadline = time.monotonic() + CONFIRM_WAIT_S
        while True:
            try:
                state = await self.state()
            except SpotifyError as exc:
                log.debug("Couldn't read Spotify's playback: %s", exc)
                return None
            if state and state["playing"] and expected & {state["uri"], state["context"], state["linked"]}:
                return state
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_CONFIRM_POLL_S)


def _woken(device: str) -> str:
    return f" (Spotify wasn't active, so playback was moved to {device})" if device else ""


async def play_with_api(player: SpotifyPlayer, what: str, uri: str, kind: str, search_kind: str) -> ToolResult:
    """``play_on_spotify`` through the Web API. Raises ``SpotifyError`` when the API can't do it."""
    await player.check_scopes()
    uris: list[str] | None = None
    if kind == "Liked Songs":
        uris = await player.liked_tracks()
        if not uris:
            return ToolResult.success(
                "The user's Liked Songs is empty, so there's nothing to play.", data={"kind": kind, "playing": False}
            )
        label, expected = "Liked Songs", set(uris)
    elif kind == "search":
        found = await player.search(what, search_kind)
        if found is None:
            return ToolResult.error(
                f"Spotify found no {search_kind} for '{what}'. Try other words, or another kind (track, album, "
                "playlist, artist)."
            )
        uri, kind, label = found.uri, found.kind, f"the {found.kind} {found.describe()}"
        expected = {uri}
    else:
        label, expected = f"the {kind}", {uri}
    device = await player.play(uri, kind, uris)
    data: dict[str, Any] = {"uri": uri, "kind": kind, "method": "web_api"}
    state = await player.confirm(expected)
    if state:
        return ToolResult.success(
            f"Started {label} on Spotify{_woken(device)}; now playing {title_of(state)}.", data={**data, **state}
        )
    return ToolResult.success(
        f"Asked Spotify to play {label}{_woken(device)}, but it hasn't confirmed that it's playing yet.",
        data={**data, "playing": False},
    )


_SKIPS = {"next": "Skipped to the next track", "previous": "Went back a track"}


async def _media_with_api(player: SpotifyPlayer, action: str) -> ToolResult:
    await player.check_scopes()
    data: dict[str, Any] = {"app": "Spotify", "method": "web_api"}
    if action in _SKIPS:
        done = _SKIPS[action]
        device = await player.on_device("POST", f"/me/player/{action}")
        return ToolResult.success(f"{done} in Spotify{_woken(device)}.", data=data)
    state = await player.state()
    playing = bool(state and state["playing"])
    if action == "now_playing":
        if state is None:
            return ToolResult.success("Nothing is playing in Spotify right now.", data={**data, "playing": False})
        return ToolResult.success(
            f"{'Playing' if playing else 'Paused'} in Spotify: {title_of(state)}.", data={**data, **state}
        )
    if action == "play_pause":
        action = "pause" if playing else "play"
    if action == "play":
        if playing:
            return ToolResult.success("Spotify is already playing.", data={**data, "playing": True})
        device = await player.on_device("PUT", "/me/player/play")
        return ToolResult.success(f"Resumed Spotify{_woken(device)}.", data={**data, "playing": True})
    if not playing:  # pause or stop
        return ToolResult.success("Spotify isn't playing.", data={**data, "playing": False})
    await player.call("PUT", "/me/player/pause")
    return ToolResult.success("Paused Spotify.", data={**data, "playing": False})


async def control_spotify(
    action: str,
    ctx: ToolContext,
    fallback: Callable[[], Awaitable[ToolResult]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolResult:
    """``media_control`` aimed at Spotify: through the Web API when it's connected, else (or when it fails) through
    ``fallback``, the app's media session, with a note on why."""
    player = SpotifyPlayer(ctx, transport)
    if not await player.usable():
        return await fallback()
    try:
        return await _media_with_api(player, action)
    except NotConnected as exc:
        note = f"{exc}. {NOT_CONNECTED}"
    except MissingScopes:
        note = RECONNECT
    except SpotifyError as exc:
        log.warning("Spotify's Web API couldn't %s: %s", action, exc)
        note = f"Spotify's Web API failed ({exc}), so the app's media controls were used"
    result = await fallback()
    return ToolResult(ok=result.ok, content=f"{result.content} ({note}.)", data=result.data)


# --------------------------------------------------------------------------- shuffle and volume


class SpotifyPlaybackArgs(ToolArgs):
    shuffle: bool | None = Field(default=None)
    volume: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> SpotifyPlaybackArgs:
        if self.shuffle is None and self.volume is None:
            raise ValueError("give shuffle (true/false) or a volume (0-100)")
        return self


class SetSpotifyPlayback(Tool):
    name = "set_spotify_playback"
    description = "Set Spotify's shuffle or its own volume (not the system volume). Needs connect_spotify."
    Args = SpotifyPlaybackArgs
    risk = RiskLevel.SAFE
    category = "media"
    timeout_s = DEVICE_WAIT_S + 15.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: SpotifyPlaybackArgs, ctx: ToolContext) -> str | None:
        if not client_id():
            return setup_hint(ctx.config)
        return None if await SpotifyAuth(ctx, self._transport).load() else NOT_CONNECTED

    async def run(self, args: SpotifyPlaybackArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        player = SpotifyPlayer(ctx, self._transport)
        done: list[str] = []
        try:
            await player.check_scopes()
            device = ""
            if args.shuffle is not None:
                state = "true" if args.shuffle else "false"
                device = await player.on_device("PUT", "/me/player/shuffle", {"state": state})
                done.append(f"turned shuffle {'on' if args.shuffle else 'off'}")
            if args.volume is not None:
                device = await player.on_device("PUT", "/me/player/volume", {"volume_percent": args.volume}) or device
                done.append(f"set Spotify's volume to {args.volume}%")
        except NotConnected as exc:
            return ToolResult.error(f"{exc}. {NOT_CONNECTED}")
        except MissingScopes:
            return ToolResult.error(f"{RECONNECT}.")
        except SpotifyError as exc:
            log.warning("Couldn't change Spotify's playback: %s", exc)
            already = f" (I already {' and '.join(done)})" if done else ""
            return ToolResult.error(f"I couldn't change Spotify's playback: {exc}{already}.")
        text = " and ".join(done)
        return ToolResult.success(
            f"{text[0].upper()}{text[1:]}{_woken(device)}.",
            data={"shuffle": args.shuffle, "volume": args.volume, "method": "web_api"},
        )


TOOLS = [SetSpotifyPlayback]
