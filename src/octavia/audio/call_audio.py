"""Audio for a voice call (``bridge/discord_voice.py``): the caller's voice into her ears, and her voice out to them.

* ``CallInput`` holds what the caller said, as 16 kHz mono float32, fed from the voice socket's thread. Her ears take
  one 512-sample frame every 32 ms whether or not anything arrived: Discord sends nothing while the caller is quiet,
  and the endpointer only hears the end of an utterance in frames of silence.
* ``CallOutput`` holds what she plays (the speaker's render tap, at the player's rate), turned into what Discord
  sends: 20 ms of 48 kHz stereo 16-bit PCM per read, silence when there's nothing.
* ``CallAwareSource`` is her microphone, except during a call: then her frames come from the call, on a clock of
  their own, and the microphone's are read and dropped (so none of them pile up for after the call).

Both buffers are bounded: the two ends run on different clocks (the sound card, the network, Discord's player), so
the oldest audio is dropped when one runs ahead rather than letting the delay grow.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from octavia.audio.source import AudioSource
from octavia.audio.types import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)

DISCORD_RATE = 48000
DISCORD_FRAME_SAMPLES = DISCORD_RATE // 50  # 20 ms
DISCORD_FRAME_BYTES = DISCORD_FRAME_SAMPLES * 2 * 2  # stereo, 16-bit
_SILENT_READ = bytes(DISCORD_FRAME_BYTES)


class _SampleFifo:
    """Float32 samples in, fixed-size pieces out, at most ``max_samples`` kept (the oldest go first). Thread-safe."""

    def __init__(self, max_samples: int) -> None:
        self.max_samples = max_samples
        self.dropped = 0  # samples thrown away because the reader fell behind
        self._lock = threading.Lock()
        self._chunks: deque[np.ndarray] = deque()
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def push(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        with self._lock:
            self._chunks.append(samples)
            self._size += samples.size
            while self._size > self.max_samples:
                excess = self._size - self.max_samples
                head = self._chunks[0]
                if head.size <= excess:
                    self._chunks.popleft()
                    self._size -= head.size
                    self.dropped += head.size
                else:
                    self._chunks[0] = head[excess:]
                    self._size -= excess
                    self.dropped += excess

    def take(self, n: int) -> np.ndarray:
        """``n`` samples: what's there, then zeros."""
        out = np.zeros(n, dtype=np.float32)
        got = 0
        with self._lock:
            while got < n and self._chunks:
                head = self._chunks[0]
                use = min(n - got, head.size)
                out[got : got + use] = head[:use]
                got += use
                if use == head.size:
                    self._chunks.popleft()
                else:
                    self._chunks[0] = head[use:]
                self._size -= use
        return out

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0


class LinearResampler:
    """Streaming linear-interpolation resampler for mono float32 (her voice up to Discord's 48 kHz: going up, linear
    is clean enough for speech). It carries the last sample and the fractional position between calls."""

    def __init__(self, rate_in: int, rate_out: int) -> None:
        self.step = rate_in / rate_out  # input samples per output sample
        self._pos = 0.0  # where the next output sample falls, in input samples after ``_last``
        self._last: float | None = None

    def process(self, samples: np.ndarray) -> np.ndarray:
        if samples.size == 0:
            return samples.astype(np.float32)
        if self.step == 1.0:
            return samples.astype(np.float32)
        x = samples.astype(np.float64)
        if self._last is not None:
            x = np.concatenate(([self._last], x))
            start = self._pos
        else:
            start = 0.0
        end = x.size - 1  # the last index that can be interpolated to
        positions = np.arange(start, end, self.step) if start < end else np.empty(0)
        out = np.interp(positions, np.arange(x.size), x).astype(np.float32)
        nxt = (positions[-1] + self.step) if positions.size else start
        self._pos = nxt - end  # relative to the new last sample
        self._last = float(x[-1])
        return out


class CallInput:
    """What the caller said, as 16 kHz mono float32 (``feed`` from any thread), handed out 512 samples at a time."""

    def __init__(self, max_s: float = 0.5) -> None:
        self._fifo = _SampleFifo(int(SAMPLE_RATE * max_s))

    def feed_pcm16(self, pcm: bytes) -> None:
        """16 kHz mono signed 16-bit PCM, as the Opus decoder gives it."""
        if pcm:
            self._fifo.push(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0)

    def frame(self) -> np.ndarray:
        return self._fifo.take(FRAME_SAMPLES)

    @property
    def dropped(self) -> int:
        return self._fifo.dropped

    def clear(self) -> None:
        self._fifo.clear()


class CallOutput:
    """Her voice, for the call: ``feed`` takes the speaker's render tap (mono float32 at ``rate``, on the audio thread),
    ``read`` gives Discord 20 ms of 48 kHz stereo 16-bit PCM (silence when there's nothing to send)."""

    def __init__(self, rate: int, max_s: float = 0.6) -> None:
        self.rate = rate
        self._resampler = LinearResampler(rate, DISCORD_RATE)
        self._fifo = _SampleFifo(int(DISCORD_RATE * max_s))
        self.active = False  # fed only during a call; otherwise the tap costs nothing

    def feed(self, block: np.ndarray) -> None:
        if self.active:
            self._fifo.push(self._resampler.process(np.asarray(block, dtype=np.float32).reshape(-1)))

    def read(self) -> bytes:
        if not len(self._fifo):
            return _SILENT_READ
        mono = self._fifo.take(DISCORD_FRAME_SAMPLES)
        pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
        return np.repeat(pcm, 2).tobytes()  # interleaved stereo: the same sample left and right

    def clear(self) -> None:
        self._fifo.clear()


class CallAwareSource(AudioSource):
    """Her microphone, except during a call (``start_call``), when her frames come from the call instead, one every
    32 ms by the clock. Anything else asked of it (``measure_level``, ``choice``...) is the microphone's."""

    def __init__(self, mic: AudioSource, clock: Any = time.monotonic) -> None:
        self.mic = mic
        self.clock = clock
        self.call: CallInput | None = None
        self._changed = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.mic, name)

    @property
    def on_call(self) -> bool:
        return self.call is not None

    def start_call(self, call: CallInput) -> None:
        call.clear()
        self.call = call
        self._changed.set()

    def end_call(self) -> None:
        self.call = None
        self._changed.set()

    async def start(self) -> None:
        await self.mic.start()

    async def close(self) -> None:
        await self.mic.close()

    async def frames(self) -> AsyncIterator[np.ndarray]:
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        pump = asyncio.create_task(self._pump_mic(queue), name="mic-pump")
        ticker = asyncio.create_task(self._tick_call(queue), name="call-ticker")
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, pump}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                pump.result()  # the microphone's frames ended or failed: so do ours
                return
        finally:
            for task in (pump, ticker):
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(pump, ticker, return_exceptions=True)

    async def _pump_mic(self, queue: asyncio.Queue[np.ndarray]) -> None:
        async for frame in self.mic.frames():
            if self.call is None:
                _put(queue, frame)

    async def _tick_call(self, queue: asyncio.Queue[np.ndarray]) -> None:
        period = FRAME_SAMPLES / SAMPLE_RATE
        while True:
            if self.call is None:
                self._changed.clear()
                await self._changed.wait()
                continue
            due = self.clock()
            while (call := self.call) is not None:
                _put(queue, call.frame())
                due += period
                delay = due - self.clock()
                if delay < -0.5:  # the loop was held up: start the clock again rather than catch up in a burst
                    due = self.clock()
                    delay = 0.0
                await asyncio.sleep(max(0.0, delay))


def _put(queue: asyncio.Queue[np.ndarray], frame: np.ndarray) -> None:
    """Queue a frame for her ears; when they've fallen behind, the oldest waiting frame makes room."""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(frame)
