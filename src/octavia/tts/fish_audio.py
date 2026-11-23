"""Fish Audio TTS (S2.1 Pro) over HTTP streaming, or the native live WebSocket protocol.

HTTP (POST /v1/tts, the audio streamed back as it's made) is the default: its first audio came 0.63 s after the
request against 1.00 s over the WebSocket, both in latency mode low (medians of 9 sentences each, taken in turn,
2026-11-22; scripts/fish_latency_bench.py). The mode matters far more: "normal" gave its first audio after 2.35 s
(HTTP) or 3.13 s (WebSocket), and delivered at only the speed she talks.

WebSocket protocol (wss://api.fish.audio/v1/tts/live, binary msgpack frames)::

    client: {"event": "start", "request": {...TTSRequest, "text": ""}}
            {"event": "text", "text": "..."} / {"event": "flush"} / {"event": "stop"}
    server: {"event": "audio", "audio": <bytes>}
            {"event": "finish", "reason": "stop" | "error", "message": ...}
            {"event": "error", "error": "...", "max_concurrency": ...}

The official SDK isn't used: its "stream" call buffers the whole response,
and its WebSocket client hangs or leaks tasks on errors and cancellation. The
protocol is small enough to own, which also makes barge-in cancellation
deterministic (close the socket).

Each text segment (sentence) is one short session. Over the WebSocket a spare
socket is kept pre-connected; over HTTP the connection is kept open between
sentences. Either way a connect starts as soon as a turn begins (``prewarm``),
so the TLS handshake (0.1 to 0.35 s) is hidden behind the brain's thinking time.

HTTP also backs up the WebSocket: when the handshake can't complete (a timeout,
a reset, a proxy), the sentence is spoken over HTTP instead of being dropped,
and HTTP stays in use for a while so the following sentences don't each wait
out the timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from octavia.audio.types import AudioFormat
from octavia.config import FishAudioConfig, current_env
from octavia.net import tls_context
from octavia.tts.base import TTSError, TTSProvider
from octavia.tts.delivery import NORMAL, Delivery, SpeakingStyle, whispered

log = logging.getLogger(__name__)

ALLOWED_MODELS = {"s2.1-pro", "s2.1-pro-free", "s2-pro", "s1"}
PCM_RATES = {8000, 16000, 24000, 32000, 44100}
# Inline performance cues S2 understands; anything else in [brackets] is stripped. The timbre-changing cues
# (whispering, sigh, laughing, chuckling, sarcastic) are kept because the user wants the wider range; voice drift
# is held in check by the steady temperature below and the chunker's one-cue-per-reply limit instead.
EXPRESSIVE_TAGS = {
    "excited", "laughing", "chuckling", "sigh", "whispering", "emphasis", "pause", "surprised",
    "sarcastic", "confident", "proud", "playful", "short pause", "break", "inhale",
}  # fmt: skip
# Cues for a moment, not a manner: a laugh, a breath in, one stressed word. S2 places a cue at the word it sits on, so
# one of these may also go inside a sentence (one per sentence), where it doesn't make the voice lurch the way a
# restyling cue there did. The user's blind picks, 2026-11-22: the version with one won 7 of 8 pairs (D-033).
INLINE_TAGS = {"laughing", "chuckling", "emphasis", "inhale"}
# Pacing cues, which don't colour the voice and aren't limited to one per reply.
PAUSE_TAGS = {"pause", "short pause", "break"}
# Used when tts.fish_audio.temperature is unset. The server default (0.7) samples each sentence's voice
# independently enough that one now and then drifts lower or rougher; a lower temperature keeps them consistent.
STEADY_TEMPERATURE = 0.5
# The prosody speeds Fish accepts; a slowed or quickened pace is kept within them.
MIN_PROSODY_SPEED = 0.5
MAX_PROSODY_SPEED = 2.0
# How long to stay on HTTP after a WebSocket handshake failed, before trying the socket again.
WS_COOLDOWN_S = 60.0
# An HTTP connection is kept open this long after its last use, and a turn that begins with none used more recently
# than HTTP_WARM_S opens one while the brain thinks (a sentence on a cold connection: 0.70 s to its first audio, on
# an open one 0.59 s; 2026-11-22).
HTTP_KEEPALIVE_S = 30.0
HTTP_WARM_S = 20.0


class _HandshakeError(TTSError):
    """The WebSocket couldn't be opened for a reason other than the server refusing the request."""


class FishAudioTTS(TTSProvider):
    name = "fish_audio"

    def __init__(self, cfg: FishAudioConfig) -> None:
        if cfg.model not in ALLOWED_MODELS:
            # Fish silently falls back to the paid default for unknown model names.
            raise TTSError(f"Unknown Fish Audio model {cfg.model!r}; use one of {sorted(ALLOWED_MODELS)}")
        if cfg.sample_rate not in PCM_RATES:
            raise TTSError(f"Fish Audio PCM sample_rate must be one of {sorted(PCM_RATES)}")
        self.cfg = cfg
        self.format = AudioFormat(sample_rate=cfg.sample_rate)
        self.api_key = (current_env(cfg.api_key_env) or "").strip().strip("\"'")
        self.reference_id = (cfg.reference_id or current_env(cfg.reference_id_env) or "").strip().strip("\"'") or None
        self.allowed_tags = EXPRESSIVE_TAGS if cfg.expressive_tags else set()
        self.pause_tags = PAUSE_TAGS
        self.inline_tags = INLINE_TAGS if cfg.expressive_tags else set()
        self.temperature = STEADY_TEMPERATURE if cfg.temperature is None else cfg.temperature
        base = cfg.base_url.rstrip("/")
        self._ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/v1/tts/live"
        self._http_url = base + "/v1/tts"
        self._spare: asyncio.Task | None = None
        self._spare_born = 0.0
        self._closing: set[asyncio.Task] = set()  # strong refs: the loop only keeps weak ones
        self._http = None
        self._http_used = float("-inf")  # time.monotonic() of the last HTTP request or connect
        self._http_warming: asyncio.Task | None = None
        self._ws_down_until = 0.0
        self.style: SpeakingStyle | None = None  # a pace or a whisper for every sentence (``use_style``)

    def use_style(self, style: SpeakingStyle) -> bool:
        """Make every sentence with ``style``'s delivery: the pace it asks for, whispered when it says so."""
        self.style = style
        return True

    def _delivery(self) -> Delivery:
        return self.style.current() if self.style is not None else NORMAL

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        if not self.api_key:
            raise TTSError(
                f"Fish Audio API key missing: set {self.cfg.api_key_env} in your environment or in the .env file."
            )
        if self.reference_id is None:
            log.warning("No Fish Audio voice selected (tts.fish_audio.reference_id); using the model's default voice")
        if self.cfg.transport == "http":
            self._http_client()

    def _http_client(self):  # noqa: ANN202
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                verify=tls_context(),
                limits=httpx.Limits(keepalive_expiry=HTTP_KEEPALIVE_S),
            )
        return self._http

    async def close(self) -> None:
        await self._drop_spare()
        warming, self._http_warming = self._http_warming, None
        if warming is not None and not warming.done():
            warming.cancel()
            await asyncio.wait({warming})
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def prewarm(self) -> None:
        """Start connecting now so the first sentence doesn't pay for the handshake."""
        if not self.api_key:
            return
        if self.cfg.transport != "websocket" or self._ws_cooling_down():
            self._prewarm_http()
            return
        if self._spare is None or self._spare_stale() or _connect_failed(self._spare):
            self._replace_spare()

    def _prewarm_http(self) -> None:
        """Open the HTTP connection the next sentence will be asked on, unless one was used moments ago."""
        if time.monotonic() - self._http_used < HTTP_WARM_S:
            return
        if self._http_warming is not None and not self._http_warming.done():
            return
        try:
            self._http_warming = asyncio.get_running_loop().create_task(self._open_http(), name="fish-http-warm")
        except RuntimeError:
            return  # no event loop running: nothing to warm for
        self._http_used = time.monotonic()

    async def _open_http(self) -> None:
        """Any answer will do (a GET here is refused): what's wanted is the open connection it leaves in the pool.
        No text is sent and nothing is made, so it uses none of the Fish allowance."""
        try:
            await self._http_client().get(self._http_url, headers={"Authorization": f"Bearer {self.api_key}"})
        except Exception:
            log.debug("Couldn't open a connection to Fish Audio ahead of the sentence", exc_info=True)

    # -- request -----------------------------------------------------------------
    def build_request(self, text: str) -> dict[str, Any]:
        req: dict[str, Any] = {
            "text": text,
            "format": "pcm",
            "sample_rate": self.cfg.sample_rate,
            "latency": self.cfg.latency,
            "chunk_length": self.cfg.chunk_length,
            "normalize": True,
            "prosody": {"speed": self._speed(), "volume": 0, "normalize_loudness": True},
            "temperature": self.temperature,
        }
        if self.reference_id:
            req["reference_id"] = self.reference_id
        if self.cfg.top_p is not None:
            req["top_p"] = self.cfg.top_p
        return req

    def _speed(self) -> float:
        speed = self._delivery().speed
        if speed == 1.0:
            return self.cfg.speed
        return round(min(MAX_PROSODY_SPEED, max(MIN_PROSODY_SPEED, self.cfg.speed * speed)), 2)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "model": self.cfg.model}

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        if self._delivery().whisper:
            text = whispered(text)
        stream = self._ws_or_http(text) if self.cfg.transport == "websocket" else self._http_synthesize(text)
        carry = b""
        first = True
        # aclosing: close the socket deterministically when the consumer stops early (barge-in).
        async with contextlib.aclosing(stream):
            async for chunk in stream:
                if first:
                    first = False
                    if chunk[:4] == b"RIFF" and len(chunk) >= 44:  # defensive: strip a WAV header if one appears
                        chunk = chunk[44:]
                data = carry + chunk
                usable = len(data) - (len(data) % 2)  # never split a 16-bit sample
                carry = data[usable:]
                if usable:
                    yield data[:usable]

    # -- websocket transport -----------------------------------------------------
    def _ws_cooling_down(self) -> bool:
        return time.monotonic() < self._ws_down_until

    async def _ws_or_http(self, text: str) -> AsyncIterator[bytes]:
        """Speak over the WebSocket, or over HTTP when it can't be opened.

        A handshake failure happens before any audio is yielded, so the HTTP
        retry never repeats something the user already heard.
        """
        ws_error: _HandshakeError | None = None
        if not self._ws_cooling_down():
            try:
                async with contextlib.aclosing(self._ws_synthesize(text)) as stream:
                    async for chunk in stream:
                        yield chunk
                return
            except _HandshakeError as exc:
                ws_error = exc
                self._ws_down_until = time.monotonic() + WS_COOLDOWN_S
                log.warning("%s; speaking over HTTP for the next %.0f s", exc, WS_COOLDOWN_S)
        try:
            async with contextlib.aclosing(self._http_synthesize(text)) as stream:
                async for chunk in stream:
                    yield chunk
        except TTSError as exc:
            if ws_error is None:
                raise
            raise TTSError(f"{ws_error}; the HTTP fallback failed too: {exc}") from exc

    async def _connect(self):
        from websockets.asyncio.client import connect

        return await connect(
            self._ws_url,
            additional_headers=self._headers(),
            max_size=None,
            open_timeout=10.0,
            ping_interval=20,
            ping_timeout=20,
            compression=None,
            ssl=tls_context() if self._ws_url.startswith("wss://") else None,
        )

    def _spare_stale(self) -> bool:
        return (time.monotonic() - self._spare_born) > self.cfg.spare_socket_max_idle_s

    def _replace_spare(self) -> None:
        old = self._spare
        if old is not None:
            self._close_in_background(old)
        self._spare = asyncio.create_task(self._connect(), name="fish-connect")
        self._spare_born = time.monotonic()

    def _close_in_background(self, task: asyncio.Task) -> None:
        """Close a socket off the critical path (a close handshake can take up to a second)."""
        closer = asyncio.create_task(_close_task_socket(task))
        self._closing.add(closer)
        closer.add_done_callback(self._closing.discard)

    async def _drop_spare(self) -> None:
        spare, self._spare = self._spare, None
        if spare is not None:
            await _close_task_socket(spare)

    async def _take_socket(self):
        spare = self._spare
        if spare is not None and (self._spare_stale() or _connect_failed(spare)):
            # A background connect that failed earlier (e.g. a network blip) shouldn't fail this sentence,
            # and closing a stale one mustn't delay it either.
            self._spare = None
            self._close_in_background(spare)
        task = self._spare or asyncio.create_task(self._connect())
        self._spare = None
        try:
            ws = await task
        except asyncio.CancelledError:
            # Barge-in while connecting: the connect may have finished in the same instant.
            await _close_task_socket(task)
            raise
        # Keep one spare warm for the next sentence.
        self._replace_spare()
        return ws

    async def _ws_synthesize(self, text: str) -> AsyncIterator[bytes]:
        import ormsgpack
        from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

        for attempt in range(2):
            try:
                ws = await self._take_socket()
            except InvalidStatus as exc:
                raise TTSError(_describe_http_status(exc.response.status_code, self.cfg.api_key_env)) from exc
            except OSError as exc:  # includes TimeoutError("timed out during opening handshake")
                raise _HandshakeError(f"Can't reach Fish Audio: {exc}") from exc
            except WebSocketException as exc:  # other handshake failures (bad upgrade response, proxy, ...)
                raise _HandshakeError(f"Fish Audio connection failed: {exc}") from exc
            got_audio = False
            finished = False
            try:
                await ws.send(ormsgpack.packb({"event": "start", "request": self.build_request("")}))
                await ws.send(ormsgpack.packb({"event": "text", "text": text}))
                await ws.send(ormsgpack.packb({"event": "stop"}))
                while True:
                    frame = await ws.recv()
                    if isinstance(frame, str):
                        continue
                    msg = ormsgpack.unpackb(frame)
                    event = msg.get("event")
                    if event == "audio":
                        audio = msg.get("audio") or b""
                        if audio:
                            got_audio = True
                            yield audio
                    elif event == "finish":
                        finished = True
                        if msg.get("reason") == "error":
                            raise TTSError(f"Fish Audio error: {msg.get('message') or 'synthesis failed'}")
                        return
                    elif event == "error":
                        extra = f" (max concurrency {msg['max_concurrency']})" if msg.get("max_concurrency") else ""
                        raise TTSError(f"Fish Audio error: {msg.get('error', 'unknown')}{extra}")
            except ConnectionClosed as exc:
                # A pre-warmed socket may have been closed by the server while idle: retry once.
                if not got_audio and attempt == 0:
                    log.debug("Fish socket closed before audio (%s); retrying on a fresh socket", exc)
                    continue
                raise TTSError(f"Fish Audio connection closed: {exc}") from exc
            finally:
                # Bounded: an unresponsive server must not stall the next sentence for the 10 s close_timeout.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ws.close(code=1000, reason="" if finished else "cancel"), 1.0)

    # -- http transport ----------------------------------------------------------
    async def _http_synthesize(self, text: str) -> AsyncIterator[bytes]:
        import ormsgpack

        http = self._http_client()
        headers = {**self._headers(), "Content-Type": "application/msgpack"}
        self._http_used = time.monotonic()
        try:
            async with http.stream(
                "POST", self._http_url, headers=headers, content=ormsgpack.packb(self.build_request(text))
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread())[:300]
                    status = _describe_http_status(response.status_code, self.cfg.api_key_env)
                    raise TTSError(f"{status}: {body!r}")
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
            self._http_used = time.monotonic()
        except TTSError:
            raise
        except Exception as exc:
            if type(exc).__module__.startswith("httpx"):
                raise TTSError(f"Can't reach Fish Audio: {exc}") from exc
            raise

    def cache_key(self) -> str:
        c = self.cfg
        return (
            f"fish:{c.model}:{self.reference_id or 'default'}:{c.sample_rate}:{c.speed}:{c.latency}:"
            f"{self.temperature}:{c.top_p}:{c.chunk_length}"
        )

    def style_hint(self) -> str | None:
        if not self.allowed_tags:
            return None
        tags = ", ".join(f"[{t}]" for t in sorted(self.allowed_tags))
        return (
            "Your voice engine can perform short tone cues written in square brackets at the start of a sentence, "
            f"for example [excited] or [confident]. Allowed cues: {tags}. A reply may use more than one, but at most "
            "one per sentence (pauses aside), always at the very start of that sentence. One exception: a laugh, a "
            "breath or a stressed word happens at a moment, so [laughing], [chuckling], [inhale] or [emphasis] may "
            "instead sit right where it happens inside a sentence (at most one of those per sentence; [emphasis] "
            "goes just before the word you'd stress, [inhale] before a longer thought). Sound as expressive as a lively "
            "friend: most replies carry a cue or two, and vary them, [excited] or [surprised] for good news, "
            "[emphasis] on the word that matters, [chuckling], [laughing] or [sarcastic] for a joke, [sigh], "
            "[whispering] or [inhale] for a softer or thinking moment, never the same cue twice in a row. Never put "
            "anything else in square brackets."
            + (
                " A cue only colours its own sentence: to whisper or slow down for a whole reply or longer, use the "
                "speaking_style tool instead."
                if self.style is not None
                else ""
            )
        )


def _connect_failed(task: asyncio.Task) -> bool:
    return task.done() and (task.cancelled() or task.exception() is not None)


async def _close_task_socket(task: asyncio.Task) -> None:
    """Abort a connect task or close the socket it produced.

    ``asyncio.wait`` (rather than awaiting the task under a blanket suppress)
    retrieves the task's exception without swallowing a cancellation of the caller.
    """
    if not task.done():
        task.cancel()
    await asyncio.wait({task})
    if task.cancelled() or task.exception() is not None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task.result().close(), 1.0)


def _describe_http_status(status: int, key_env: str = "FISH_AUDIO_API_KEY") -> str:
    return {
        401: f"Fish Audio rejected the API key (401). Check {key_env}",
        402: "Fish Audio account is out of credit (402)",
        403: "Fish Audio refused the request (403)",
        404: "Fish Audio voice not found (404). Check tts.fish_audio.reference_id",
        429: "Fish Audio rate limit hit (429)",
    }.get(status, f"Fish Audio HTTP {status}")
