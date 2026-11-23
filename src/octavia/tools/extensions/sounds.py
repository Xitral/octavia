"""Sounds on request: "what does a peregrine falcon sound like", "play a thunderstorm", "play a dog bark".

The clip comes from Wikimedia Commons, which is free, needs no key or account and holds a large library of freely
licensed recordings (many of xeno-canto's bird calls among them). Its search is limited to audio files; a result is
used in MP3 (the file itself, or the MP3 copy Commons makes of Ogg and FLAC files) or small WAV, the formats Windows
plays without extra codecs.

Playback goes through Windows' own MCI player (winmm), which decodes MP3 itself, on a thread of its own: the clip
plays in the background while she answers, stops by itself after a few seconds, and ``stop_sound`` cuts it off.
SAFE: it only plays a short sound, which stops on its own.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from octavia.net import tls_context as _tls_context
from octavia.net import wikimedia_user_agent
from octavia.tools.base import NoArgs, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

API_URL = "https://commons.wikimedia.org/w/api.php"
SOURCE = "Wikimedia Commons"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_BYTES = 8_000_000  # about eight minutes of MP3: far more than the longest clip played
MAX_TRIES = 3  # results tried when one can't be downloaded or played
SOUND_DIR = Path(tempfile.gettempdir()) / "octavia-sounds"
_WAV = ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave")


class SoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class Clip:
    title: str
    url: str  # the file to download: MP3 or WAV
    suffix: str
    page: str  # the file's page on Commons
    duration: float | None = None
    artist: str = ""
    license: str = ""


# --------------------------------------------------------------------------- finding a clip


def _plain(value: Any, limit: int = 80) -> str:
    """Commons' metadata is HTML ("<a href=...>Name</a>"): the words alone, short enough to say."""
    if not isinstance(value, str):
        return ""
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _title(page_title: str) -> str:
    name = page_title.removeprefix("File:")
    name = name.rsplit(".", 1)[0] if "." in name else name
    return " ".join(name.replace("_", " ").split())


def _meta(info: dict[str, Any], key: str) -> str:
    entry = (info.get("extmetadata") or {}).get(key) or {}
    return _plain(entry.get("value")) if isinstance(entry, dict) else ""


def _playable(info: dict[str, Any]) -> tuple[str, str] | None:
    """The URL and file suffix of a version Windows can play, or None."""
    mime = str(info.get("mime") or "").lower()
    url = info.get("url")
    if mime == "audio/mpeg" and isinstance(url, str):
        return url, ".mp3"
    for derivative in info.get("derivatives") or []:
        if not isinstance(derivative, dict) or not isinstance(derivative.get("src"), str):
            continue
        if derivative.get("transcodekey") == "mp3" or str(derivative.get("type", "")).startswith("audio/mpeg"):
            return derivative["src"], ".mp3"
    size = info.get("size")
    if mime in _WAV and isinstance(url, str) and isinstance(size, int) and size <= MAX_BYTES:
        return url, ".wav"
    return None


def clips(data: dict[str, Any]) -> list[Clip]:
    """The playable results of a Commons search, best match first."""
    pages = (data.get("query") or {}).get("pages") or []
    if isinstance(pages, dict):
        pages = list(pages.values())
    found: list[tuple[int, Clip]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        info = (page.get("videoinfo") or [None])[0]
        if not isinstance(info, dict) or (playable := _playable(info)) is None:
            continue
        duration = info.get("duration")
        duration = float(duration) if isinstance(duration, int | float) else None
        if duration is not None and duration < 0.5:
            continue
        title = str(page.get("title") or "")
        clip = Clip(
            title=_title(title) or "an untitled recording",
            url=playable[0],
            suffix=playable[1],
            page=str(info.get("descriptionurl") or ""),
            duration=duration,
            artist=_meta(info, "Artist"),
            license=_meta(info, "LicenseShortName"),
        )
        found.append((int(page.get("index") or 0), clip))
    return [clip for _, clip in sorted(found, key=lambda item: item[0])]


# --------------------------------------------------------------------------- playing it


def _mci(command: str) -> str:
    """One MCI command string; its answer, or SoundError with Windows' explanation."""
    import ctypes

    winmm = ctypes.WinDLL("winmm")
    answer = ctypes.create_unicode_buffer(256)
    code = winmm.mciSendStringW(command, answer, len(answer), None)
    if code:
        text = ctypes.create_unicode_buffer(256)
        winmm.mciGetErrorStringW(code, text, len(text))
        raise SoundError(text.value or f"MCI error {code}")
    return answer.value


class MciPlayer:
    """Plays one file at a time. Every MCI command for a file is sent from the one thread that opened it, as MCI
    devices belong to that thread; ``stop`` only signals it, so it's safe from anywhere."""

    def __init__(self, send: Callable[[str], str] = _mci, poll_s: float = 0.2) -> None:
        self._send = send
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def playing(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def play(self, path: Path, limit_s: float) -> None:
        """Start playing ``path`` (stopping whatever played before) for at most ``limit_s`` seconds; returns once it
        has started, or raises SoundError when Windows can't play it."""
        with self._lock:
            self._stop_locked()
            stop, started = threading.Event(), threading.Event()
            problem: list[str] = []
            thread = threading.Thread(
                target=self._run, args=(path, limit_s, stop, started, problem), name="sound-player", daemon=True
            )
            thread.start()
            if not started.wait(10.0):
                stop.set()
                raise SoundError("Windows didn't start playing it")
            if problem:
                raise SoundError(problem[0])
            self._thread, self._stop = thread, stop

    def stop(self) -> bool:
        """Stop the sound; False when nothing was playing."""
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> bool:
        thread, self._thread = self._thread, None
        if thread is None or not thread.is_alive():
            return False
        self._stop.set()
        thread.join(3.0)
        return True

    def _run(
        self, path: Path, limit_s: float, stop: threading.Event, started: threading.Event, problem: list[str]
    ) -> None:
        from octavia.audio.devices import ensure_com

        ensure_com()  # MCI's MP3 player is built on DirectShow, which is COM
        alias = f"octavia_sound_{uuid.uuid4().hex[:8]}"
        try:
            self._send(f'open "{path}" type mpegvideo alias {alias}')
        except SoundError as exc:
            problem.append(str(exc))
            started.set()
            return
        try:
            self._send(f"play {alias}")
        except SoundError as exc:
            self._quietly(f"close {alias}")
            problem.append(str(exc))
            started.set()
            return
        started.set()
        deadline = time.monotonic() + limit_s
        try:
            while not stop.wait(self._poll_s) and time.monotonic() < deadline:
                if self._send(f"status {alias} mode") == "stopped":
                    break
        except SoundError:
            log.debug("Lost track of the sound", exc_info=True)
        finally:
            self._quietly(f"stop {alias}")
            self._quietly(f"close {alias}")

    def _quietly(self, command: str) -> None:
        try:
            self._send(command)
        except SoundError:
            log.debug("MCI %r failed", command, exc_info=True)


PLAYER = MciPlayer()


def _clear_old_sounds() -> None:
    try:
        for old in SOUND_DIR.glob("*"):
            old.unlink(missing_ok=True)
    except OSError:
        log.debug("Couldn't clear old sounds", exc_info=True)


# --------------------------------------------------------------------------- tools


class PlaySoundArgs(ToolArgs):
    query: str = Field(
        min_length=2,
        max_length=100,
        description="E.g. 'peregrine falcon', 'thunderstorm'; for an animal, its name alone.",
    )
    seconds: int = Field(default=20, ge=3, le=60)


class PlaySound(Tool):
    name = "play_sound"
    description = (
        "Play a short sound recording (animal call, weather, machine, sound effect) from Wikimedia Commons in "
        "the background. Not for music (use Spotify). stop_sound stops it early."
    )
    Args = PlaySoundArgs
    risk = RiskLevel.SAFE
    category = "media"
    timeout_s = 45.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, player: MciPlayer | None = None) -> None:
        self._transport = transport
        self._player = player or PLAYER

    async def precheck(self, args: PlaySoundArgs, ctx: ToolContext) -> str | None:
        if self._player is PLAYER and sys.platform != "win32":
            return "Playing sounds needs Windows."
        return None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=TIMEOUT,
            verify=_tls_context(),
            transport=self._transport,
            headers={"User-Agent": wikimedia_user_agent()},
            follow_redirects=True,
        )

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[Clip]:
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": f"{query} filetype:audio",
            "gsrnamespace": "6",
            "gsrlimit": "10",
            "prop": "videoinfo",
            "viprop": "url|mime|size|derivatives|extmetadata",
            "viextmetadatafilter": "Artist|LicenseShortName",
        }
        response = await client.get(API_URL, params=params)
        response.raise_for_status()
        return clips(response.json())

    async def _download(self, client: httpx.AsyncClient, clip: Clip) -> Path:
        """The clip saved to a temporary file. A long recording is cut off at MAX_BYTES: MP3 plays fine cut short."""
        await asyncio.to_thread(SOUND_DIR.mkdir, parents=True, exist_ok=True)
        path = SOUND_DIR / f"{uuid.uuid4().hex}{clip.suffix}"
        size = 0
        async with client.stream("GET", clip.url) as response:
            response.raise_for_status()
            with path.open("wb") as file:
                async for chunk in response.aiter_bytes():
                    file.write(chunk[: MAX_BYTES - size])
                    size += len(chunk)
                    if size >= MAX_BYTES:
                        break
        if size == 0:
            path.unlink(missing_ok=True)
            raise SoundError("the file was empty")
        return path

    async def run(self, args: PlaySoundArgs, ctx: ToolContext) -> ToolResult:
        # The old sound stops first: its file can't be replaced while it's open, and a new request replaces it anyway.
        await asyncio.to_thread(self._player.stop)
        await asyncio.to_thread(_clear_old_sounds)
        try:
            async with self._client() as client:
                found = await self._search(client, args.query)
                if not found:
                    return ToolResult.error(
                        f"Found no recording of {args.query!r} on {SOURCE}. Try other words, such as the animal's "
                        "common name or a simpler description."
                    )
                problems: list[str] = []
                for clip in found[:MAX_TRIES]:
                    try:
                        path = await self._download(client, clip)
                        await asyncio.to_thread(self._player.play, path, float(args.seconds))
                    except (httpx.HTTPError, SoundError, OSError) as exc:
                        log.info("Couldn't play %r: %s", clip.title, exc)
                        problems.append(str(exc) or type(exc).__name__)
                        continue
                    return ToolResult.success(_playing(clip, args.seconds), data=_data(clip, args.seconds))
        except httpx.HTTPStatusError as exc:
            log.warning("Wikimedia Commons answered HTTP %s", exc.response.status_code)
            return ToolResult.error(f"{SOURCE} answered HTTP {exc.response.status_code}; try again later.")
        except httpx.HTTPError as exc:
            log.warning("Wikimedia Commons couldn't be reached: %s", exc)
            return ToolResult.error(f"Couldn't reach {SOURCE}; check the internet connection.")
        except ValueError as exc:
            log.warning("Wikimedia Commons sent something unreadable: %s", exc)
            return ToolResult.error(f"{SOURCE} sent back something unreadable; try again later.")
        return ToolResult.error(f"Found recordings of {args.query!r} but couldn't play them ({problems[-1]}).")


def _playing(clip: Clip, seconds: int) -> str:
    credit = ", ".join(part for part in (f"by {clip.artist}" if clip.artist else "", clip.license) if part)
    if clip.duration is not None and clip.duration <= seconds:
        length = f"it plays in full ({max(1, round(clip.duration))} seconds)"
    else:
        length = f"it plays for up to {seconds} seconds"
    return (
        f'Now playing "{clip.title}" from {SOURCE}' + (f" ({credit})" if credit else "") + f"; {length}, in the "
        "background. Say briefly what it is."
    )


def _data(clip: Clip, seconds: int) -> dict[str, Any]:
    return {"title": clip.title, "source": SOURCE, "page": clip.page, "url": clip.url, "seconds": seconds}


class StopSound(Tool):
    name = "stop_sound"
    description = "Stop the sound play_sound started."
    Args = NoArgs
    risk = RiskLevel.SAFE
    category = "media"

    def __init__(self, player: MciPlayer | None = None) -> None:
        self._player = player or PLAYER

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        if await asyncio.to_thread(self._player.stop):
            return ToolResult.success("Stopped the sound.")
        return ToolResult.success("No sound is playing.")


TOOLS = [PlaySound, StopSound]
