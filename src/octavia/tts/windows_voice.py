"""A built-in Windows voice (SAPI 5), the backup for when Fish Audio can't speak.

It runs locally, so it works with no network and no account, and it sounds
plainer than Fish: it's there so she's never silent, not to replace her voice.

SAPI is reached through pywin32 (already a dependency, no new package). Each
sentence is spoken into an ``SpMemoryStream`` set to 16-bit mono PCM at the
rate the player already uses (Fish's), so nothing is heard while it's made and
no resampling is needed for the usual rates; another rate is made at 24 kHz
and resampled. Speaking a sentence takes tens of milliseconds and blocks, so it
runs on one worker thread of its own, where COM is initialized and the voice
object lives (COM objects belong to the thread that made them).

Fish's tone cues like [excited] mean nothing here and would be read out, so
anything in short square brackets is dropped before speaking.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import struct
import sys
import threading
from collections.abc import AsyncIterator

import numpy as np

from octavia.audio.types import AudioFormat
from octavia.tts.base import TTSError, TTSProvider

log = logging.getLogger(__name__)

# SAPI's SpeechAudioFormatType for 16-bit mono PCM at each rate it can make directly.
SAPI_FORMATS = {8000: 6, 11025: 10, 12000: 14, 16000: 18, 22050: 22, 24000: 26, 32000: 30, 44100: 34, 48000: 38}
FALLBACK_RATE = 24000  # made at this rate and resampled when the wanted one isn't in SAPI_FORMATS
SVSF_IS_NOT_XML = 16  # speak the text as it is: "<" and "&" aren't markup
_TAG = re.compile(r"\[[^\[\]]{0,40}\]")


def strip_tags(text: str) -> str:
    """Drop [tone cues] (and any other short bracketed bit), which this voice would read out."""
    return " ".join(_TAG.sub(" ", text).split())


def pcm_from_wav(data: bytes) -> tuple[bytes, int]:
    """The 16-bit mono PCM samples of a WAV file and its sample rate. Raises TTSError on anything else.

    SAPI's memory stream gives raw samples, but a voice that returns a whole WAV
    (with a RIFF header) is read through this, so a header is never played as a click.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise TTSError("not a WAV file")
    pos, rate, pcm = 12, None, None
    while pos + 8 <= len(data):
        chunk_id, size = data[pos : pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8 : pos + 8 + size]
        if chunk_id == b"fmt ":
            if len(body) < 16:
                raise TTSError("WAV format chunk is too short")
            tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", body)
            if tag not in (1, 0xFFFE) or channels != 1 or bits != 16:
                raise TTSError(f"unsupported WAV audio (format {tag}, {channels} channels, {bits} bits)")
        elif chunk_id == b"data":
            pcm = body
            break
        pos += 8 + size + (size & 1)  # chunks are padded to an even length
    if rate is None or pcm is None:
        raise TTSError("WAV file has no audio")
    return pcm[: len(pcm) - len(pcm) % 2], rate


def _is_english(language: str) -> bool:
    """SAPI's Language attribute is hex LCIDs ("409" or "409;9"); English is primary language 0x09."""
    try:
        return (int(language.split(";")[0], 16) & 0x3FF) == 0x09
    except ValueError:
        return False


def pick_voice(voices: list[tuple[str, str, str]], wanted: str = "") -> int | None:
    """Index of the voice to use among (description, gender, language) entries; None = SAPI's default.

    A ``wanted`` name matches as a substring (any case); with none, the first
    female English voice (Zira on most PCs) is used. SAPI only lists the "Desktop" voices, not the
    newer natural ones (Aria) that the WinRT speech API has.
    """
    if wanted:
        for i, (description, _, _) in enumerate(voices):
            if wanted.lower() in description.lower():
                return i
        log.warning("No Windows voice matches tts.windows_voice=%r; using the default one", wanted)
        return None
    for i, (_, gender, language) in enumerate(voices):
        if gender.lower() == "female" and _is_english(language):
            return i
    return None


class WindowsVoiceTTS(TTSProvider):
    name = "windows_voice"

    def __init__(self, sample_rate: int = 44100, voice: str = "") -> None:
        self.format = AudioFormat(sample_rate=sample_rate)
        self.wanted_voice = voice
        self.voice_name = ""  # the voice chosen at start, for logs
        self._sapi_rate = sample_rate if sample_rate in SAPI_FORMATS else FALLBACK_RATE
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._local = threading.local()  # the worker thread's SpVoice

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        if sys.platform != "win32":
            raise TTSError("the Windows voice is only available on Windows")
        try:
            import win32com.client  # noqa: F401
        except ImportError as exc:
            raise TTSError(f"pywin32 isn't installed: {exc}") from exc
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="windows-voice", initializer=_init_com
        )
        try:
            self.voice_name = await self._run(self._choose_voice)
        except TTSError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise TTSError(f"the Windows voice can't start: {exc}") from exc

    async def close(self) -> None:
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)

    async def _run(self, fn, *args):
        if self._executor is None:
            raise TTSError("the Windows voice isn't started")
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    # -- on the worker thread ------------------------------------------------------
    def _choose_voice(self) -> str:
        import win32com.client

        voice = win32com.client.Dispatch("SAPI.SpVoice")
        tokens = list(voice.GetVoices())
        if not tokens:
            raise TTSError("no Windows voices are installed")
        described = [(t.GetDescription(), _attribute(t, "Gender"), _attribute(t, "Language")) for t in tokens]
        index = pick_voice(described, self.wanted_voice)
        if index is not None:
            voice.Voice = tokens[index]
        self._local.voice = voice
        return voice.Voice.GetDescription()

    def _speak(self, text: str) -> bytes:
        import win32com.client

        voice = getattr(self._local, "voice", None)
        if voice is None:
            self._choose_voice()
            voice = self._local.voice
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = SAPI_FORMATS[self._sapi_rate]
        stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
        stream.Format = fmt
        voice.AudioOutputStream = stream  # into memory: nothing reaches the speakers from here
        try:
            voice.Speak(text, SVSF_IS_NOT_XML)
        finally:
            voice.AudioOutputStream = None
        return bytes(stream.GetData())

    # -- speaking ------------------------------------------------------------------
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        text = strip_tags(text)
        if not text:
            return
        try:
            data = await self._run(self._speak, text)
        except TTSError:
            raise
        except Exception as exc:  # a COM error from SAPI
            raise TTSError(f"the Windows voice failed: {exc}") from exc
        pcm = self.to_pcm(data, self._sapi_rate)
        if pcm:
            yield pcm

    def to_pcm(self, data: bytes, rate: int) -> bytes:
        """What SAPI returned (raw samples, or a WAV) as 16-bit PCM at this provider's rate."""
        if data[:4] == b"RIFF":
            data, rate = pcm_from_wav(data)
        data = data[: len(data) - len(data) % 2]
        if rate != self.format.sample_rate and data:
            from octavia.core.speech import resample_linear

            audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            audio = resample_linear(audio, rate, self.format.sample_rate)
            data = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        return data

    def cache_key(self) -> str:
        return f"windows:{self.voice_name or self.wanted_voice or 'default'}:{self.format.sample_rate}"


def _attribute(token, name: str) -> str:
    try:
        return str(token.GetAttribute(name) or "")
    except Exception:  # SAPI raises when a voice doesn't define the attribute
        return ""


def _init_com() -> None:
    import pythoncom

    pythoncom.CoInitialize()
