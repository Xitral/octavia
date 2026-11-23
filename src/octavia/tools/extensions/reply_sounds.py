"""Sounds in the middle of a spoken answer: she says a line, the falcon's call plays, then she goes on.

The model calls ``play_sound_in_reply`` between two sentences of its reply. The clip goes into the reply's own speech
queue (``ctx.extras["play_in_reply"]``, ``Assistant.play_during_turn``) right after what she's said so far, so it never
plays over her voice, and what she says next waits for it. The call returns at once: the reply keeps streaming behind
the clip.

Only WAV can be read (the standard library's ``wave``; nothing installed decodes MP3 or Ogg). A clip comes from:

* a WAV file on this computer, or a direct http(s) link to one;
* a description, matched against the WAV files in ``data/sounds`` (the user's own clips) and Windows' own sounds, then
  looked up on Wikimedia Commons, whose recordings are free to use.

Each clip is cut to ``MAX_CLIP_S``, faded at both ends, leveled to about her voice's loudness and given a short breath
of silence before and after, so it doesn't run into her words. At most ``PER_TURN`` per answer: it's there to explain,
not to decorate.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import wave
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from pydantic import Field
from rapidfuzz import fuzz, utils

from octavia.net import tls_context as _tls_context
from octavia.net import wikimedia_user_agent
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult, TurnInfo

log = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
USER_SOUNDS = "data/sounds"  # the user's own clips, under the project root
WAV_MIMES = frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})
MAX_BYTES = 12_000_000
MAX_CLIP_S = 12.0
GAP_S = 0.25  # silence before and after the clip
PEAK = 0.6  # about as loud as her voice
MAX_GAIN = 4.0  # a faint recording is brought up, but not its hiss
FADE_IN_S = 0.005
FADE_OUT_S = 0.05
MATCH_SCORE = 80  # a local file's name must fit the description this well
PER_TURN = 2


class ClipError(Exception):
    """Why a sound can't be played, as the model is told."""


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Mono float32 samples and their rate from a WAV file's bytes (8, 16, 24 or 32-bit PCM), at most ``MAX_CLIP_S``."""
    try:
        with wave.open(io.BytesIO(data)) as wav:
            channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
            frames = wav.readframes(min(wav.getnframes(), int(rate * MAX_CLIP_S)))
    except (wave.Error, EOFError, ValueError) as exc:
        raise ClipError(f"it isn't a WAV file that can be read ({exc})") from None
    if rate <= 0 or channels <= 0 or width not in (1, 2, 3, 4):
        raise ClipError("its audio format isn't supported")
    frame = width * channels
    frames = frames[: len(frames) // frame * frame]
    if width == 1:
        samples = (np.frombuffer(frames, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(frames, "<i2").astype(np.float32) / 32768.0
    elif width == 3:
        raw = np.frombuffer(frames, np.uint8).reshape(-1, 3).astype(np.int32)
        packed = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        samples = ((packed << 8) >> 8).astype(np.float32) / 8388608.0
    else:
        samples = np.frombuffer(frames, "<i4").astype(np.float32) / 2147483648.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float32), rate


def prepare(audio: np.ndarray, rate: int) -> np.ndarray:
    """The clip as it goes between two sentences: faded in and out, leveled, with a short silence on each side."""
    clip = np.asarray(audio, dtype=np.float32).reshape(-1)[: int(rate * MAX_CLIP_S)].copy()
    peak = float(np.max(np.abs(clip))) if clip.size else 0.0
    if peak < 1e-4:
        raise ClipError("it's silent")
    clip *= min(MAX_GAIN, PEAK / peak)
    n_in, n_out = min(clip.size, int(rate * FADE_IN_S)), min(clip.size, int(rate * FADE_OUT_S))
    if n_in:
        clip[:n_in] *= np.linspace(0.0, 1.0, n_in, dtype=np.float32)
    if n_out:
        clip[-n_out:] *= np.linspace(1.0, 0.0, n_out, dtype=np.float32)
    gap = np.zeros(int(rate * GAP_S), dtype=np.float32)
    return np.concatenate([gap, clip, gap])


def _name(text: str) -> str:
    """A file or page name as words: "File:Peregrine_falcon-call.wav" -> "Peregrine falcon call"."""
    stem = Path(text.split(":", 1)[-1] if text.startswith("File:") else text).stem
    return " ".join(re.sub(r"[_\-.]+", " ", stem).split())


def sound_folders(ctx: ToolContext) -> list[Path]:
    """Where the user's own clips and Windows' sounds are, the user's first."""
    windows = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows")
    return [ctx.config.resolve_path(USER_SOUNDS), windows / "Media"]


def find_local(description: str, folders: list[Path]) -> Path | None:
    """The WAV file whose name best fits the description, in the first folder that has one; None when none fits."""
    for folder in folders:
        try:
            files = [p for p in folder.rglob("*") if p.suffix.lower() in (".wav", ".wave") and p.is_file()]
        except OSError:
            continue
        scored = [(fuzz.token_set_ratio(description, _name(p.name), processor=utils.default_process), p) for p in files]
        best = max(scored, key=lambda s: s[0], default=None)
        if best is not None and best[0] >= MATCH_SCORE:
            return best[1]
    return None


def _read_file(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ClipError("the file is too big for a short clip")
        return path.read_bytes()
    except OSError as exc:
        raise ClipError(f"the file couldn't be read ({exc.strerror or exc})") from None


def _turn(ctx: ToolContext) -> TurnInfo | None:
    """The user's turn running now (``ctx.extras["turn"]``); None between turns, or when there's no way to tell."""
    current = ctx.extras.get("turn")
    if current is None:
        return None
    try:
        info = current()
    except Exception:
        log.debug("Asking which turn is running failed", exc_info=True)
        return None
    return info if isinstance(info, TurnInfo) else None


class PlaySoundInReplyArgs(ToolArgs):
    sound: str = Field(
        min_length=1,
        max_length=500,
        description="A description ('tada', 'falcon call'), a .wav path, or an http(s) .wav link.",
    )


class PlaySoundInReply(Tool):
    name = "play_sound_in_reply"
    description = (
        "Play a short real sound between two sentences of your spoken answer: say the lead-in ('Here's a "
        "peregrine falcon.'), call this, carry on. At most once or twice an answer, only when hearing it "
        "helps or the user asks. WAV only. Don't announce it; if it fails, go on in words."
    )
    Args = PlaySoundInReplyArgs
    risk = RiskLevel.SAFE
    category = "audio"
    timeout_s = 30.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._turn_id: int | None = None
        self._played = 0  # clips played in that turn

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=TIMEOUT,
            verify=_tls_context(),
            transport=self._transport,
            headers={"User-Agent": wikimedia_user_agent()},
            follow_redirects=True,
        )

    async def run(self, args: PlaySoundInReplyArgs, ctx: ToolContext) -> ToolResult:
        play = ctx.extras.get("play_in_reply")
        turn = _turn(ctx)
        if play is None or turn is None:
            return ToolResult.error("There's no spoken answer to put a sound into right now; describe it in words.")
        if turn.interrupted:
            return ToolResult.error("The user cut this answer off; don't play anything.")
        if turn.id != self._turn_id:
            self._turn_id, self._played = turn.id, 0
        if self._played >= PER_TURN:
            return ToolResult.error(f"That's {PER_TURN} sounds in this answer already; go on in words.")
        try:
            data, label = await self._fetch(args.sound, ctx)
            audio, rate = await asyncio.to_thread(decode_wav, data)
            clip = prepare(audio, rate)
        except ClipError as exc:
            return ToolResult.error(f"Couldn't play {args.sound!r}: {exc}. Carry on without it.")
        except httpx.HTTPStatusError as exc:
            log.warning("Fetching a sound answered HTTP %s", exc.response.status_code)
            return ToolResult.error(f"Fetching the sound answered HTTP {exc.response.status_code}; go on without it.")
        except httpx.HTTPError as exc:
            log.warning("Couldn't fetch a sound: %s", exc)
            return ToolResult.error("Couldn't reach the internet for the sound; go on without it.")
        if not play(label, clip, rate):
            return ToolResult.error("The sound couldn't go into the answer (no voice, or the user cut in).")
        self._played += 1
        seconds = clip.size / rate
        return ToolResult.success(
            f"Queued {label} ({seconds:.1f} s): it plays right after what you've said so far, then your next words. "
            "Go straight on.",
            data={"sound": label, "seconds": round(seconds, 1)},
        )

    async def _fetch(self, sound: str, ctx: ToolContext) -> tuple[bytes, str]:
        """The clip's bytes and a short name for it."""
        if re.match(r"https?://", sound, re.IGNORECASE):
            async with self._client() as client:
                return await self._download(client, sound), _name(httpx.URL(sound).path) or "a sound"
        path = Path(os.path.expandvars(sound)).expanduser()
        if path.suffix.lower() in (".wav", ".wave") and (path.is_absolute() or path.exists()):
            return await asyncio.to_thread(_read_file, path), _name(path.name)
        if path.suffix.lower() in (".mp3", ".ogg", ".oga", ".flac", ".m4a", ".opus"):
            raise ClipError("only WAV files can be played")
        local = await asyncio.to_thread(find_local, sound, sound_folders(ctx))
        if local is not None:
            return await asyncio.to_thread(_read_file, local), _name(local.name)
        async with self._client() as client:
            url, label = await self._search_commons(client, sound)
            return await self._download(client, url), label

    async def _search_commons(self, client: httpx.AsyncClient, description: str) -> tuple[str, str]:
        """The best-ranked WAV recording on Wikimedia Commons for the description: its link and its name."""
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": f"{description} filetype:audio",
            "gsrnamespace": "6",
            "gsrlimit": "50",
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
        }
        response = await client.get(COMMONS_API, params=params)
        response.raise_for_status()
        try:
            pages: list[dict[str, Any]] = (response.json().get("query") or {}).get("pages") or []
        except ValueError:
            raise ClipError("Wikimedia Commons sent back something unreadable") from None
        for page in sorted(pages, key=lambda p: p.get("index", 0)):
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("mime") in WAV_MIMES and info.get("url") and 0 < int(info.get("size") or 0) <= MAX_BYTES:
                return str(info["url"]), _name(str(page.get("title", ""))) or description
        raise ClipError("no WAV recording of it was found (on this computer or Wikimedia Commons)")

    async def _download(self, client: httpx.AsyncClient, url: str) -> bytes:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if int(response.headers.get("content-length") or 0) > MAX_BYTES:
                raise ClipError("the file is too big for a short clip")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    raise ClipError("the file is too big for a short clip")
        return bytes(data)


TOOLS = [PlaySoundInReply]
