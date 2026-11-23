"""Text-to-speech provider interface.

Providers synthesize one text *segment* at a time and stream audio back as it
is generated. The ``SpeechOutput`` pipeline feeds segments (sentences) as
the brain produces them and plays audio as it arrives, so speech starts before
the full reply exists. Because the pipeline knows segment boundaries, it can
tell exactly what was spoken when the user interrupts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from octavia.audio.types import AudioFormat


class TTSError(RuntimeError):
    pass


# Yielded by a voice whose sentences wait their turn (the local voice's worker makes one at a time) when this one
# actually starts: the backup's first-audio clock starts there, not at the request. Not audio; ``FallbackTTS``
# drops it.
STARTED = b""


class NotReady(TTSError):
    """The voice is still starting (a local model loading). A backup says the sentence, and that isn't a failure:
    nothing is announced and the voice is tried again at the next reply (one voice per reply)."""


class TTSProvider(ABC):
    name: str = "tts"
    format: AudioFormat  # PCM16 format of the bytes yielded by ``synthesize``

    async def start(self) -> None:
        """Open connections / validate credentials. Raise TTSError on setup problems."""

    @abstractmethod
    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream raw PCM16 audio for ``text``. Cancelling the iteration must stop work promptly."""

    def prewarm(self) -> None:
        """Optional: get a connection ready now, because a reply is coming soon. Must not block."""

    def style_hint(self) -> str | None:
        """Optional: a sentence for the system prompt about markup this voice can perform."""
        return None

    def cache_key(self) -> str:
        """Identifies the voice, so audio cached for one voice/model/speed is never reused for another."""
        return f"{self.name}:{self.format.sample_rate}"

    def cacheable(self) -> TTSProvider:
        """The provider whose audio may be cached under ``cache_key``: itself, except a wrapper that can switch
        to another voice (``FallbackTTS``), which hands out its main voice so backup audio is never cached."""
        return self

    async def close(self) -> None:
        pass
