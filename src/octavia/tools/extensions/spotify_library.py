"""Spotify library: the Web API sign-in and its requests, and reading the user's playlists.

The desktop app's ``spotify:`` links (see ``spotify.py``) can open things but can't tell what's in the user's
library or reliably start playback; only the Web API can (``spotify_player.py`` drives playback with it), and it
needs the user's permission through OAuth. A sign-in made before playback control was asked for lacks its scopes;
the granted scopes are kept with the tokens so the player can tell, and connecting again once adds them. The Authorization Code flow with
PKCE is used because it needs no client secret: the user creates a Spotify app once, puts its client ID in ``.env``
like the other keys, and signs in once in the browser. Spotify then redirects to a loopback address this module
listens on for the length of the sign-in, and the refresh token is kept in the tool state store so later calls
need nothing from the user. Tokens are never put in a tool result or a log line.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import Field

from octavia import keys
from octavia.config import AppConfig
from octavia.net import tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

CLIENT_ID_ENV = "SPOTIFY_CLIENT_ID"
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8898
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
PLAYBACK_SCOPES = frozenset(
    {"user-modify-playback-state", "user-read-playback-state", "user-read-currently-playing", "user-library-read"}
)
SCOPES = " ".join(["playlist-read-private", "playlist-read-collaborative", *sorted(PLAYBACK_SCOPES)])
STATE_NAMESPACE = "spotify"
STATE_KEY = "auth"
SIGN_IN_WAIT_S = 170.0
_EXPIRY_MARGIN_S = 60.0
_MAX_HEADER_LINES = 100

_THEN = "and asks me to connect Spotify"
SETUP_HINT = keys.hint(CLIENT_ID_ENV, then=_THEN)
NOT_CONNECTED = "Spotify isn't connected yet: use connect_spotify so the user can sign in once in the browser."


class SpotifyError(RuntimeError):
    """The Web API couldn't be asked or refused; the message is safe to show (it never holds a token)."""


class NotConnected(SpotifyError):
    """No usable sign-in: the user has to connect Spotify (again)."""


class MissingScopes(SpotifyError):
    """The sign-in works but predates a permission the call needs; connecting again once asks for it."""


class NoActiveDevice(SpotifyError):
    """Spotify has no device to play on right now (the app is closed or idle)."""


def client_id() -> str:
    return keys.value(CLIENT_ID_ENV)


def setup_hint(config: AppConfig | None = None) -> str:
    """How to set up reading the library, naming the .env file checked and why the ID looks missing."""
    return keys.missing(CLIENT_ID_ENV, cfg=config, then=_THEN)


def pkce_pair() -> tuple[str, str]:
    """``(verifier, challenge)`` for PKCE (RFC 7636, S256)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(client: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = {
        "client_id": client,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def _reason(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Spotify answered HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Spotify didn't answer in time"
    if isinstance(exc, httpx.HTTPError):
        return "Spotify couldn't be reached"
    return type(exc).__name__


# --------------------------------------------------------------------------- tokens


class SpotifyAuth:
    """Access tokens for the Web API, refreshed from the stored refresh token as they expire."""

    def __init__(self, ctx: ToolContext, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._ctx = ctx
        self._transport = transport

    async def load(self) -> dict[str, Any] | None:
        """The stored sign-in, or None when there is none for the current client ID."""
        if self._ctx.state is None:
            return None
        saved = await self._ctx.state.get(STATE_NAMESPACE, STATE_KEY)
        if not isinstance(saved, dict) or not saved.get("refresh_token") or saved.get("client_id") != client_id():
            return None
        return saved

    async def forget(self) -> None:
        if self._ctx.state is not None:
            await self._ctx.state.delete(STATE_NAMESPACE, STATE_KEY)

    async def _save(self, answer: dict[str, Any], previous: dict[str, Any] | None = None) -> None:
        if self._ctx.state is None:
            raise SpotifyError("there's no place to keep the Spotify sign-in")
        previous = previous or {}
        record = {
            "client_id": client_id(),
            "access_token": answer["access_token"],
            # Spotify may rotate the refresh token; when it doesn't send one, the old one stays valid.
            "refresh_token": answer.get("refresh_token") or previous.get("refresh_token", ""),
            "expires_at": time.time() + float(answer.get("expires_in", 3600)),
        }
        scope = answer.get("scope") if isinstance(answer.get("scope"), str) else previous.get("scope")
        if isinstance(scope, str):
            record["scope"] = scope
        await self._ctx.state.set(STATE_NAMESPACE, STATE_KEY, record)

    async def granted_scopes(self) -> frozenset[str] | None:
        """What the stored sign-in allows, or None when that isn't known (a sign-in kept before it was recorded).

        Spotify names the granted scopes with every token, so refreshing once makes an unknown answer known.
        """
        saved = await self.load()
        if saved is not None and "scope" not in saved:
            await self.access_token(force_refresh=True)
            saved = await self.load()
        if saved is None or not isinstance(saved.get("scope"), str):
            return None
        return frozenset(saved["scope"].split())

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=tls_context(), transport=self._transport) as client:
                response = await client.post(TOKEN_URL, data=form)
        except httpx.HTTPError as exc:
            raise SpotifyError(_reason(exc)) from None
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if response.status_code == 400 and isinstance(answer, dict) and answer.get("error") == "invalid_grant":
            raise NotConnected("Spotify no longer accepts the sign-in")
        if response.is_error or not isinstance(answer, dict) or not answer.get("access_token"):
            raise SpotifyError(f"Spotify's sign-in service answered HTTP {response.status_code}")
        return answer

    async def exchange(self, code: str, verifier: str, redirect_uri: str) -> None:
        answer = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id(),
                "code_verifier": verifier,
            }
        )
        await self._save(answer)

    async def access_token(self, force_refresh: bool = False) -> str:
        saved = await self.load()
        if saved is None:
            raise NotConnected("Spotify isn't connected")
        if (
            not force_refresh
            and saved.get("access_token")
            and saved.get("expires_at", 0) > time.time() + _EXPIRY_MARGIN_S
        ):
            return saved["access_token"]
        form = {"grant_type": "refresh_token", "refresh_token": saved["refresh_token"], "client_id": client_id()}
        try:
            answer = await self._token_request(form)
        except NotConnected:
            await self.forget()
            raise
        await self._save(answer, saved)
        return answer["access_token"]


# --------------------------------------------------------------------------- Web API calls


def _api_error(response: httpx.Response) -> tuple[str, str]:
    """``(message, reason)`` from a Web API error body, or empty strings."""
    try:
        body = response.json()
    except ValueError:
        return "", ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return "", ""
    return str(error.get("message") or "").strip()[:160], str(error.get("reason") or "")


async def api_call(
    auth: SpotifyAuth,
    transport: httpx.AsyncBaseTransport | None,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    """One Web API request, refreshing a rejected token once. The decoded answer, or None when it has no body
    (the player's commands answer 204)."""
    token = await auth.access_token()
    async with httpx.AsyncClient(timeout=15.0, verify=tls_context(), transport=transport) as client:
        for attempt in range(2):
            try:
                response = await client.request(
                    method, f"{API_URL}{path}", params=params, json=json, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                raise SpotifyError(_reason(exc)) from None
            if response.status_code == 401 and attempt == 0:
                token = await auth.access_token(force_refresh=True)
                continue
            break
    if response.status_code == 401:
        await auth.forget()
        raise NotConnected("Spotify no longer accepts the sign-in")
    message, reason = _api_error(response) if response.is_error else ("", "")
    if response.status_code == 403:
        if "scope" in message.lower():
            raise MissingScopes("the Spotify sign-in doesn't allow that yet")
        if reason == "PREMIUM_REQUIRED":
            raise SpotifyError("Spotify only lets Premium accounts control playback")
        if message and "registered" not in message.lower():
            raise SpotifyError(f"Spotify refused: {message}")
        raise SpotifyError(
            "Spotify refused (HTTP 403); while the Spotify app is in development mode, the user's Spotify "
            "account has to be added under User Management in the app's dashboard"
        )
    if response.status_code == 404 and (reason == "NO_ACTIVE_DEVICE" or "no active device" in message.lower()):
        raise NoActiveDevice("Spotify has no active device")
    if response.status_code == 429:
        raise SpotifyError("Spotify says there were too many requests; try again in a minute")
    if response.is_error:
        raise SpotifyError(f"Spotify answered HTTP {response.status_code}")
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        if method != "GET":  # a command that went through; some answer with a bare text body
            return None
        raise SpotifyError("Spotify sent an answer I couldn't read") from None


# --------------------------------------------------------------------------- sign-in listener


@dataclass(frozen=True)
class Callback:
    code: str = ""
    state: str = ""
    error: str = ""


_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Octavia</title></head>"
    "<body style='font-family:sans-serif;margin:3em'><h2>{message}</h2><p>You can close this tab.</p></body></html>"
)


async def _answer(writer: asyncio.StreamWriter, status: str, message: str) -> None:
    body = _PAGE.format(message=escape(message)).encode()
    head = f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
    writer.write(head.encode() + b"Connection: close\r\n\r\n" + body)
    try:
        await writer.drain()
    finally:
        writer.close()


class CallbackServer:
    """Listens on the loopback redirect address until the service (Spotify, Google) sends the browser back with a
    code (or an error)."""

    def __init__(self, port: int, service: str = "Spotify") -> None:
        self._port = port
        self._service = service
        self._server: asyncio.Server | None = None
        self.result: asyncio.Future[Callback] = asyncio.get_running_loop().create_future()

    async def __aenter__(self) -> CallbackServer:
        self._server = await asyncio.start_server(self._handle, REDIRECT_HOST, self._port)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def redirect_uri(self) -> str:
        port = self._server.sockets[0].getsockname()[1] if self._server else self._port
        return f"http://{REDIRECT_HOST}:{port}{REDIRECT_PATH}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = (await asyncio.wait_for(reader.readline(), 10.0)).decode("latin-1")
            for _ in range(_MAX_HEADER_LINES):
                if (await asyncio.wait_for(reader.readline(), 10.0)).strip() == b"":
                    break
            parts = request.split()
            target = urlsplit(parts[1]) if len(parts) >= 2 and parts[0] == "GET" else None
            if target is None or target.path != REDIRECT_PATH or self.result.done():
                await _answer(writer, "404 Not Found", "Nothing here.")
                return
            query = {k: v[0] for k, v in parse_qs(target.query).items()}
            self.result.set_result(Callback(query.get("code", ""), query.get("state", ""), query.get("error", "")))
            ok = bool(query.get("code")) and not query.get("error")
            done = f"{self._service} is connected." if ok else f"{self._service} wasn't connected."
            await _answer(writer, "200 OK", done)
        except (OSError, TimeoutError, UnicodeError):
            writer.close()


async def _open_in_browser(url: str) -> bool:
    return await asyncio.to_thread(webbrowser.open, url, 2)


# --------------------------------------------------------------------------- tools


class ConnectSpotify(Tool):
    name = "connect_spotify"
    description = (
        "Connect the user's Spotify (one-time browser sign-in), when a Spotify tool says it isn't connected "
        "or lacks playback permission."
    )
    risk = RiskLevel.CONFIRM
    category = "media"
    timeout_s = SIGN_IN_WAIT_S + 20.0

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        open_browser: Callable[[str], Awaitable[bool]] | None = None,
        port: int = REDIRECT_PORT,
        wait_s: float = SIGN_IN_WAIT_S,
    ) -> None:
        self._transport = transport
        self._open_browser = open_browser or _open_in_browser
        self._port = port
        self._wait_s = wait_s

    async def describe_call(self, args: Any, ctx: ToolContext) -> str:
        return "open Spotify's sign-in page in the browser to connect your Spotify account"

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        if not client_id():
            return setup_hint(ctx.config)
        return None if ctx.state is not None else "There's no place to keep the Spotify sign-in."

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        try:
            async with CallbackServer(self._port) as server:
                url = authorize_url(client_id(), server.redirect_uri, challenge, state)
                if not await self._open_browser(url):
                    return ToolResult.error("No web browser responded, so I couldn't open Spotify's sign-in page.")
                try:
                    callback = await asyncio.wait_for(server.result, self._wait_s)
                except TimeoutError:
                    return ToolResult.error(
                        "The Spotify sign-in wasn't finished in time. The user can ask to connect Spotify again."
                    )
                redirect_uri = server.redirect_uri
        except OSError as exc:
            log.warning("Couldn't listen for the Spotify sign-in on port %d: %s", self._port, exc)
            return ToolResult.error(
                f"I couldn't listen for Spotify's answer on port {self._port}; another program may be using it."
            )
        if callback.error:
            if callback.error == "access_denied":
                return ToolResult.error("The user didn't allow access in Spotify, so it isn't connected.")
            return ToolResult.error(f"Spotify refused the sign-in ({callback.error[:80]}).")
        if not callback.code or not secrets.compare_digest(callback.state, state):
            return ToolResult.error("Spotify's answer didn't match this sign-in, so it isn't connected.")
        try:
            await SpotifyAuth(ctx, self._transport).exchange(callback.code, verifier, redirect_uri)
        except SpotifyError as exc:
            log.warning("Spotify sign-in failed: %s", exc)
            return ToolResult.error(f"I couldn't finish connecting Spotify: {exc}.")
        return ToolResult.success(
            "Spotify is connected; its playlists can be read and its playback controlled now.",
            data={"connected": True},
        )


class ListPlaylistsArgs(ToolArgs):
    limit: int = Field(default=10, ge=1, le=50, description="1 for 'my latest one'.")


@dataclass(frozen=True)
class Playlist:
    name: str
    uri: str
    owner: str = ""
    tracks: int | None = None

    def describe(self) -> str:
        extra = [f"{self.tracks} songs"] if self.tracks is not None else []
        if self.owner:
            extra.append(f"by {self.owner}")
        return f"{self.name} ({', '.join(extra)}) [{self.uri}]" if extra else f"{self.name} [{self.uri}]"


def parse_playlists(payload: Any) -> tuple[list[Playlist], int]:
    """The playlists in a ``/me/playlists`` page and the library's total count."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise SpotifyError("Spotify sent an answer I couldn't read")
    found = []
    for item in payload["items"]:
        if not isinstance(item, dict) or not item.get("uri"):
            continue
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        # Spotify renamed the track summary from "tracks" to "items"; read whichever is there.
        summary = next((v for k in ("tracks", "items") if isinstance(v := item.get(k), dict)), {})
        total = summary.get("total")
        found.append(
            Playlist(
                name=str(item.get("name") or "(untitled)").strip(),
                uri=str(item["uri"]),
                owner=str(owner.get("display_name") or "").strip(),
                tracks=total if isinstance(total, int) else None,
            )
        )
    total = payload.get("total")
    return found, total if isinstance(total, int) else len(found)


class ListSpotifyPlaylists(Tool):
    name = "list_spotify_playlists"
    description = "The user's Spotify playlists, newest first, with URIs for play_on_spotify."
    Args = ListPlaylistsArgs
    risk = RiskLevel.SAFE
    category = "media"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: ListPlaylistsArgs, ctx: ToolContext) -> str | None:
        if not client_id():
            return setup_hint(ctx.config)
        return None if await SpotifyAuth(ctx, self._transport).load() else NOT_CONNECTED

    async def _get(self, auth: SpotifyAuth, path: str, params: dict[str, Any]) -> Any:
        return await api_call(auth, self._transport, "GET", path, params=params)

    async def run(self, args: ListPlaylistsArgs, ctx: ToolContext) -> ToolResult:
        if not client_id():
            return ToolResult.error(setup_hint(ctx.config))
        auth = SpotifyAuth(ctx, self._transport)
        try:
            playlists, total = parse_playlists(await self._get(auth, "/me/playlists", {"limit": args.limit}))
        except NotConnected as exc:
            return ToolResult.error(f"{exc}. {NOT_CONNECTED}")
        except SpotifyError as exc:
            log.warning("Couldn't list Spotify playlists: %s", exc)
            return ToolResult.error(f"I couldn't read the Spotify playlists: {exc}.")
        if not playlists:
            return ToolResult.success("The user has no playlists in their Spotify library.", data={"count": 0})
        lines = [f"{n}. {p.describe()}" for n, p in enumerate(playlists, start=1)]
        more = f"\n...and {total - len(playlists)} more." if total > len(playlists) else ""
        return ToolResult.success(
            "Spotify playlists, newest first:\n" + "\n".join(lines) + more,
            data={"count": len(playlists), "total": total, "playlists": [asdict(p) for p in playlists]},
        )


TOOLS = [ConnectSpotify, ListSpotifyPlaylists]
