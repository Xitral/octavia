"""Song recognition: name the music the PC is playing, Shazam-style, e.g. in a game or a film.

A few seconds of what the speakers play are recorded through WASAPI loopback (the system's sound output, never the
microphone) and sent to AudD's recognition API, which answers with the title and artist. Recording happens only
inside a call of this tool, the clip is kept in memory and dropped afterwards, and the AudD key comes from the
environment (``.env``) like the other keys, never from YAML, a tool result or a log line.

Neither sounddevice's PortAudio build nor pycaw offers loopback capture, so the few COM interfaces it needs are
declared here with comtypes (installed with pycaw). Declaring them here also avoids depending on which interfaces a
given pycaw version happens to ship.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import sys
import threading
import time
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np
from pydantic import Field

from octavia import keys
from octavia.net import tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

AUDD_TOKEN_ENV = "AUDD_API_TOKEN"
AUDD_URL = "https://api.audd.io/"
SETUP_HINT = keys.hint(AUDD_TOKEN_ENV)
DEFAULT_SECONDS = 10
UPLOAD_RATE = 16000
SILENCE_DBFS = -55.0  # quieter than this over the whole clip means nothing was really playing

# WASAPI constants (audioclient.h, mmreg.h, ksmedia.h)
_E_RENDER, _E_MULTIMEDIA = 0, 1
_CLSCTX_ALL = 0x17
_AUDCLNT_SHAREMODE_SHARED = 0
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_BUFFER_100NS = 10_000_000  # a one-second device buffer: polling every 10 ms never lets it overflow
_WAVE_FORMAT_PCM, _WAVE_FORMAT_IEEE_FLOAT, _WAVE_FORMAT_EXTENSIBLE = 0x0001, 0x0003, 0xFFFE
_POLL_S = 0.01


class LoopbackUnavailable(RuntimeError):
    """The PC's sound output can't be recorded (no output device, unsupported format, COM failure)."""


@dataclass(frozen=True)
class Clip:
    """Mono audio in [-1, 1] and its sample rate."""

    samples: np.ndarray
    rate: int

    @property
    def seconds(self) -> float:
        return self.samples.size / self.rate if self.rate else 0.0


# --------------------------------------------------------------------------- audio helpers


def to_mono(raw: bytes, channels: int, bits: int, is_float: bool) -> np.ndarray:
    """Interleaved device samples as mono float32 in [-1, 1]."""
    if is_float and bits == 32:
        x = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    elif not is_float and bits == 16:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif not is_float and bits == 32:
        x = (np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2**31).astype(np.float32)
    elif not is_float and bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = (np.where(v >= 1 << 23, v - (1 << 24), v) / float(1 << 23)).astype(np.float32)
    else:
        raise LoopbackUnavailable(f"the output device uses an unsupported sample format ({bits}-bit)")
    channels = max(channels, 1)
    usable = x.size - x.size % channels
    return np.clip(x[:usable].reshape(-1, channels).mean(axis=1), -1.0, 1.0).astype(np.float32)


def level_dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return float("-inf")
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20.0 * np.log10(rms) if rms > 0 else float("-inf")


def downsample(clip: Clip, target: int = UPLOAD_RATE) -> Clip:
    """Decimate by a whole factor, averaging each group first to keep aliasing down.

    A 96 kHz device would otherwise upload ~2 MB for ten seconds, while fingerprints only use the lower few kHz.
    """
    factor = clip.rate // target
    if factor <= 1:
        return clip
    usable = clip.samples.size - clip.samples.size % factor
    samples = clip.samples[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
    return Clip(samples, clip.rate // factor)


def wav_bytes(clip: Clip) -> bytes:
    """16-bit mono WAV at about ``UPLOAD_RATE``: small to upload, and plenty for fingerprinting."""
    clip = downsample(clip)
    pcm = (np.clip(clip.samples, -1.0, 1.0) * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(clip.rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


# --------------------------------------------------------------------------- WASAPI loopback


@functools.cache
def _wasapi() -> Any:
    """The COM declarations, built on first use because comtypes only exists on Windows."""
    from ctypes import POINTER, Structure, c_int64, c_ubyte, c_uint64, c_void_p
    from ctypes.wintypes import DWORD, HANDLE, UINT, WORD
    from types import SimpleNamespace

    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class WAVEFORMATEX(Structure):
        _pack_ = 1
        _fields_ = [
            ("wFormatTag", WORD),
            ("nChannels", WORD),
            ("nSamplesPerSec", DWORD),
            ("nAvgBytesPerSec", DWORD),
            ("nBlockAlign", WORD),
            ("wBitsPerSample", WORD),
            ("cbSize", WORD),
        ]

    class WAVEFORMATEXTENSIBLE(Structure):
        _pack_ = 1
        _fields_ = [
            ("Format", WAVEFORMATEX),
            ("wValidBitsPerSample", WORD),
            ("dwChannelMask", DWORD),
            ("SubFormat", GUID),
        ]

    class IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "GetBuffer",
                (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                (["out"], POINTER(UINT), "pNumFramesToRead"),
                (["out"], POINTER(DWORD), "pdwFlags"),
                (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                (["out"], POINTER(c_uint64), "pu64QPCPosition"),
            ),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], UINT, "NumFramesRead")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(UINT), "pNumFramesInNextPacket")),
        ]

    class IAudioClient(IUnknown):
        _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "Initialize",
                (["in"], DWORD, "ShareMode"),
                (["in"], DWORD, "StreamFlags"),
                (["in"], c_int64, "hnsBufferDuration"),
                (["in"], c_int64, "hnsPeriodicity"),
                (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                (["in"], POINTER(GUID), "AudioSessionGuid"),
            ),
            COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(UINT), "pNumBufferFrames")),
            COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_int64), "phnsLatency")),
            COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(UINT), "pNumPaddingFrames")),
            COMMETHOD(
                [],
                HRESULT,
                "IsFormatSupported",
                (["in"], DWORD, "ShareMode"),
                (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch"),
            ),
            COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
            COMMETHOD(
                [],
                HRESULT,
                "GetDevicePeriod",
                (["out"], POINTER(c_int64), "phnsDefaultDevicePeriod"),
                (["out"], POINTER(c_int64), "phnsMinimumDevicePeriod"),
            ),
            COMMETHOD([], HRESULT, "Start"),
            COMMETHOD([], HRESULT, "Stop"),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "SetEventHandle", (["in"], HANDLE, "eventHandle")),
            COMMETHOD(
                [],
                HRESULT,
                "GetService",
                (["in"], POINTER(GUID), "riid"),
                (["out"], POINTER(POINTER(IUnknown)), "ppv"),
            ),
        ]

    # Only the methods up to the ones used are declared; the vtable order before them is what matters.
    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "Activate",
                (["in"], POINTER(GUID), "iid"),
                (["in"], DWORD, "dwClsCtx"),
                (["in"], c_void_p, "pActivationParams"),
                (["out"], POINTER(POINTER(IUnknown)), "ppInterface"),
            ),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "EnumAudioEndpoints",
                (["in"], DWORD, "dataFlow"),
                (["in"], DWORD, "dwStateMask"),
                (["out"], POINTER(c_void_p), "ppDevices"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "GetDefaultAudioEndpoint",
                (["in"], DWORD, "dataFlow"),
                (["in"], DWORD, "role"),
                (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
            ),
        ]

    return SimpleNamespace(
        WAVEFORMATEX=WAVEFORMATEX,
        WAVEFORMATEXTENSIBLE=WAVEFORMATEXTENSIBLE,
        IAudioCaptureClient=IAudioCaptureClient,
        IAudioClient=IAudioClient,
        IMMDeviceEnumerator=IMMDeviceEnumerator,
        CLSID_MMDeviceEnumerator=GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        KSDATAFORMAT_SUBTYPE_IEEE_FLOAT=GUID("{00000003-0000-0010-8000-00AA00389B71}"),
    )


def capture_loopback(seconds: float) -> Clip:
    """Record what the default output device plays for ``seconds``. Blocking; runs on a COM-initialised thread.

    WASAPI delivers no packets while nothing plays, so the loop runs on wall time, and a silent PC gives a short or
    empty clip rather than a hang.
    """
    import ctypes

    import comtypes

    api = _wasapi()
    try:
        enumerator = comtypes.CoCreateInstance(
            api.CLSID_MMDeviceEnumerator, api.IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
        )
        device = enumerator.GetDefaultAudioEndpoint(_E_RENDER, _E_MULTIMEDIA)
    except Exception as exc:  # comtypes.COMError (E_NOTFOUND) when no output device is enabled
        raise LoopbackUnavailable("there's no sound output device to listen to") from exc
    client = device.Activate(api.IAudioClient._iid_, _CLSCTX_ALL, None).QueryInterface(api.IAudioClient)
    fmt_ptr = client.GetMixFormat()
    try:
        fmt = fmt_ptr.contents
        channels, rate, bits, block = fmt.nChannels, fmt.nSamplesPerSec, fmt.wBitsPerSample, fmt.nBlockAlign
        is_float = fmt.wFormatTag == _WAVE_FORMAT_IEEE_FLOAT
        if fmt.wFormatTag == _WAVE_FORMAT_EXTENSIBLE:
            ext = ctypes.cast(fmt_ptr, ctypes.POINTER(api.WAVEFORMATEXTENSIBLE)).contents
            is_float = ext.SubFormat == api.KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
        elif fmt.wFormatTag not in (_WAVE_FORMAT_PCM, _WAVE_FORMAT_IEEE_FLOAT):
            raise LoopbackUnavailable(f"the output device uses an unsupported format (tag {fmt.wFormatTag:#x})")
        client.Initialize(_AUDCLNT_SHAREMODE_SHARED, _AUDCLNT_STREAMFLAGS_LOOPBACK, _BUFFER_100NS, 0, fmt_ptr, None)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(fmt_ptr)
    capture = client.GetService(api.IAudioCaptureClient._iid_).QueryInterface(api.IAudioCaptureClient)

    chunks: list[bytes] = []
    client.Start()
    try:
        deadline = time.monotonic() + seconds
        while True:
            while frames := capture.GetNextPacketSize():
                data, frames, flags, _, _ = capture.GetBuffer()
                size = frames * block
                if flags & _AUDCLNT_BUFFERFLAGS_SILENT or not data:
                    chunks.append(bytes(size))
                else:
                    chunks.append(ctypes.string_at(data, size))
                capture.ReleaseBuffer(frames)
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_S)
    finally:
        client.Stop()
        del capture, client, device, enumerator  # release COM pointers on this thread
    return Clip(to_mono(b"".join(chunks), channels, bits, is_float), rate)


_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _init_com() -> None:
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        log.debug("COM initialisation on the loopback thread failed", exc_info=True)


async def _on_capture_thread(fn: Callable[[], Clip]) -> Clip:
    """Own thread, so a recording doesn't hold up volume changes on the shared audio thread."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(1, thread_name_prefix="octavia-loopback", initializer=_init_com)
    return await asyncio.get_running_loop().run_in_executor(_executor, fn)


# --------------------------------------------------------------------------- recognition


class RecognitionError(RuntimeError):
    """AudD couldn't be asked, or refused; the message is safe to show (it never holds the token)."""


@dataclass(frozen=True)
class Song:
    title: str
    artist: str
    album: str = ""
    release_date: str = ""
    link: str = ""

    def describe(self) -> str:
        text = f'"{self.title}" by {self.artist}' if self.artist else f'"{self.title}"'
        extra = []
        if self.album and self.album.lower() != self.title.lower():
            extra.append(f"from the album {self.album}")
        if year := self.release_date[:4]:
            extra.append(year)
        return f"{text} ({', '.join(extra)})" if extra else text


def parse_audd(payload: Any) -> Song | None:
    """The song in an AudD answer, None when nothing matched. Raises ``RecognitionError`` for an error answer."""
    if not isinstance(payload, dict):
        raise RecognitionError("the recognition service sent an answer I couldn't read")
    if payload.get("status") != "success":
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = error.get("error_code")
        if code in (900, 901):
            raise RecognitionError(
                f"the AudD token was refused; check {AUDD_TOKEN_ENV} in {keys.env_file() or 'the .env file'}"
            )
        if code == 902:
            raise RecognitionError("the AudD token has used up its requests for now")
        message = str(error.get("error_message") or "unknown error")[:200]
        raise RecognitionError(f"the recognition service reported an error ({message})")
    result = payload.get("result")
    if not isinstance(result, dict) or not result.get("title"):
        return None
    return Song(
        title=str(result.get("title") or "").strip(),
        artist=str(result.get("artist") or "").strip(),
        album=str(result.get("album") or "").strip(),
        release_date=str(result.get("release_date") or "").strip(),
        link=str(result.get("song_link") or "").strip(),
    )


async def recognize(clip: Clip, token: str, transport: httpx.AsyncBaseTransport | None = None) -> Song | None:
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=tls_context(), transport=transport) as client:
            response = await client.post(
                AUDD_URL,
                data={"api_token": token},
                files={"file": ("clip.wav", wav_bytes(clip), "audio/wav")},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise RecognitionError(f"the recognition service answered HTTP {exc.response.status_code}") from None
    except httpx.TimeoutException:
        raise RecognitionError("the recognition service didn't answer in time") from None
    except httpx.HTTPError:
        raise RecognitionError("the recognition service couldn't be reached") from None
    except ValueError:
        raise RecognitionError("the recognition service sent an answer I couldn't read") from None
    return parse_audd(payload)


# --------------------------------------------------------------------------- tool


class IdentifySongArgs(ToolArgs):
    seconds: int = Field(
        default=DEFAULT_SECONDS,
        ge=5,
        le=20,
        description="Raise only if a first try found nothing.",
    )


class IdentifySong(Tool):
    name = "identify_song"
    description = (
        "Identify the song playing on this PC, like Shazam, from its sound output (not the mic). For what a "
        "media app reports, use media_control now_playing."
    )
    Args = IdentifySongArgs
    risk = RiskLevel.CONFIRM
    category = "media"
    timeout_s = 45.0

    def __init__(
        self,
        capture: Callable[[float], Clip] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._capture = capture
        self._transport = transport

    async def describe_call(self, args: IdentifySongArgs, ctx: ToolContext) -> str:
        return f"record {args.seconds} seconds of the PC's sound and send it to AudD to name the song"

    async def precheck(self, args: IdentifySongArgs, ctx: ToolContext) -> str | None:
        if self._capture is None and sys.platform != "win32":
            return "Song recognition only works on Windows."
        return None if keys.is_set(AUDD_TOKEN_ENV) else keys.missing(AUDD_TOKEN_ENV, cfg=ctx.config)

    async def _record(self, seconds: float) -> Clip:
        if self._capture is not None:
            return await asyncio.to_thread(self._capture, seconds)
        return await _on_capture_thread(lambda: capture_loopback(seconds))

    async def run(self, args: IdentifySongArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        token = keys.value(AUDD_TOKEN_ENV)
        try:
            clip = await self._record(args.seconds)
        except LoopbackUnavailable as exc:
            return ToolResult.error(f"I couldn't record the PC's sound: {exc}.")
        except Exception as exc:  # comtypes.COMError from the audio stack
            log.warning("Loopback recording failed", exc_info=True)
            return ToolResult.error(f"I couldn't record the PC's sound ({type(exc).__name__}).")
        if clip.seconds < 2.0 or level_dbfs(clip.samples) < SILENCE_DBFS:
            return ToolResult.error(
                "Nothing audible is playing on the PC, so there's no song to identify.",
                data={"seconds": round(clip.seconds, 1)},
            )
        try:
            song = await recognize(clip, token, self._transport)
        except RecognitionError as exc:
            log.warning("Song recognition failed: %s", exc)
            return ToolResult.error(f"I couldn't identify it: {exc}.")
        if song is None:
            return ToolResult.success(
                "No match: the recognition service didn't recognise the music. It works best when the music is "
                "louder than dialogue or game sounds; trying again with a longer listen may help.",
                data={"found": False},
            )
        data = {"found": True, "title": song.title, "artist": song.artist, "album": song.album, "link": song.link}
        return ToolResult.success(f"It's {song.describe()}.", data=data)


TOOLS = [IdentifySong]
