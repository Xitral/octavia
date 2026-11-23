"""Shared audio constants and helpers.

Internally, microphone audio is always 16 kHz mono float32 in [-1, 1],
delivered in fixed frames of 512 samples (32 ms), which is what Silero VAD
expects. Wake-word engines with other frame sizes buffer internally.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
FRAME_MS = 1000 * FRAME_SAMPLES / SAMPLE_RATE


@dataclass(frozen=True)
class AudioFormat:
    """PCM format of audio produced by a TTS provider."""

    sample_rate: int
    channels: int = 1
    sample_width: int = 2  # bytes per sample; 2 = signed 16-bit little-endian

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width


def pcm16_to_float32(data: bytes | np.ndarray) -> np.ndarray:
    arr = np.frombuffer(data, dtype="<i2") if isinstance(data, (bytes, bytearray, memoryview)) else data
    return arr.astype(np.float32) / 32768.0


def rms_dbfs(audio: np.ndarray) -> float:
    if audio.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    return 20.0 * np.log10(max(rms, 1e-6))
