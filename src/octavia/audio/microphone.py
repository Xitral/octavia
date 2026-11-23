"""Microphone capture: PortAudio callback -> asyncio frames.

Opens a 16 kHz mono int16 stream with 512-sample blocks (one Silero frame
per callback), copies each block into an asyncio queue, and yields float32
frames. A watchdog reopens the stream if frames stop arriving (for example
when a USB headset is unplugged and plugged back in).

With an echo canceller the callback only queues the raw block: a processing
thread runs the canceller (too slow for the PortAudio thread, and all APM
calls must stay on one thread so the speaker reference is always fed before
the capture it echoes into) and re-cuts the cleaned audio into 512-sample
frames.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

import numpy as np

from octavia.audio.devices import DeviceChoice, add_reinit_hook, choose_device, ensure_com, reinitialize_portaudio
from octavia.audio.source import AudioSource
from octavia.audio.types import FRAME_SAMPLES, SAMPLE_RATE

if TYPE_CHECKING:
    from octavia.audio.aec import EchoCanceller

log = logging.getLogger(__name__)

_RAW_QUEUE_BLOCKS = 64  # ~2 s of 32 ms blocks waiting for the echo canceller


class MicrophoneSource(AudioSource):
    def __init__(
        self,
        device: int | str | None = None,
        host_api: str | None = None,
        max_queue: int = 256,
        stall_timeout_s: float = 3.0,
        echo_canceller: EchoCanceller | None = None,
    ) -> None:
        self.device_ref = device
        self.host_api = host_api
        self.max_queue = max_queue
        self.stall_timeout_s = stall_timeout_s
        self.echo_canceller = echo_canceller
        self.choice: DeviceChoice | None = None
        self.overflows = 0
        self.dropped = 0
        self.aec_errors = 0
        self.input_latency = 0.0
        self.output_latency = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._stream = None
        self._raw: queue.Queue[np.ndarray | None] | None = None
        self._worker: threading.Thread | None = None
        self._worker_stop: threading.Event | None = None
        self._active = False
        self._remove_hook: Callable[[], None] | None = None

    # -- PortAudio thread -------------------------------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:
        if status.input_overflow:
            self.overflows += 1
        block = indata[:, 0].copy()  # the buffer is reused by PortAudio: copy is required
        if self.echo_canceller is None:
            self._deliver(block)
            return
        raw = self._raw
        if raw is None:
            return
        try:
            raw.put_nowait(block)
        except queue.Full:
            self.dropped += 1

    def _deliver(self, block: np.ndarray) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._put, block)
            except RuntimeError:
                pass  # loop closing

    def _put(self, block: np.ndarray) -> None:
        assert self._queue is not None
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(block)

    # -- echo-cancellation thread ---------------------------------------------------
    def _process_loop(
        self, canceller: EchoCanceller, raw: queue.Queue[np.ndarray | None], stop: threading.Event
    ) -> None:
        pending = np.zeros(0, dtype=np.int16)
        while not stop.is_set():
            try:
                block = raw.get(timeout=0.25)
            except queue.Empty:
                continue
            if block is None:
                break
            try:
                clean = canceller.process(block)
            except Exception:
                self.aec_errors += 1
                if self.aec_errors <= 3 or self.aec_errors % 1000 == 0:
                    log.exception("Echo cancellation failed (%d errors); using raw audio", self.aec_errors)
                clean = block
            pending = np.concatenate((pending, clean)) if pending.size else clean
            n = pending.size // FRAME_SAMPLES
            for i in range(n):
                self._deliver(pending[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES])
            pending = pending[n * FRAME_SAMPLES :].copy()

    def _start_worker(self) -> None:
        canceller = self.echo_canceller
        assert canceller is not None
        canceller.reset()  # stale reference/capture audio from before a reopen would confuse it
        raw: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=_RAW_QUEUE_BLOCKS)
        stop = threading.Event()
        worker = threading.Thread(target=self._process_loop, args=(canceller, raw, stop), name="mic-aec", daemon=True)
        self._raw, self._worker_stop, self._worker = raw, stop, worker
        worker.start()

    def _stop_worker(self) -> None:
        worker, stop, raw = self._worker, self._worker_stop, self._raw
        self._worker = self._worker_stop = self._raw = None
        if stop is not None:
            stop.set()
        if raw is not None:
            try:
                raw.put_nowait(None)
            except queue.Full:
                pass  # the stop event ends it within one get() timeout
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
            if worker.is_alive():
                log.warning("Echo-cancellation thread did not stop")

    # -- stream lifecycle ---------------------------------------------------------
    def _open(self) -> None:
        import sounddevice as sd

        ensure_com()  # this may run on any worker thread

        self.choice = choose_device(self.device_ref, "input", self.host_api)
        self._stream = sd.InputStream(
            device=self.choice.index,
            channels=1,
            samplerate=SAMPLE_RATE,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            latency="low",
            callback=self._callback,
            extra_settings=self.choice.extra_settings,
        )
        try:
            if self.echo_canceller is not None:
                self._start_worker()
            self._stream.start()
        except Exception:
            self._close_stream()
            raise
        self.input_latency = float(getattr(self._stream, "latency", 0.0) or 0.0)
        self._apply_latencies()
        log.info(
            "Microphone: %s (%s), latency %.0f ms%s",
            self.choice.name,
            self.choice.host_api,
            self.input_latency * 1000,
            ", echo cancellation on" if self.echo_canceller is not None else "",
        )

    def _close_stream(self) -> None:
        ensure_com()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("Error closing input stream", exc_info=True)
        self._stop_worker()

    def set_output_latency(self, seconds: float) -> None:
        """Speaker latency (``SoundDevicePlayer.latency``), for the echo canceller's delay hint."""
        self.output_latency = max(0.0, float(seconds))
        self._apply_latencies()

    def _apply_latencies(self) -> None:
        if self.echo_canceller is not None:
            self.echo_canceller.set_latencies(self.output_latency, self.input_latency)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.max_queue)
        await asyncio.to_thread(self._open)
        self._active = True
        self._remove_hook = add_reinit_hook(self._after_reinit)

    def _after_reinit(self) -> None:
        """PortAudio was re-initialized (by us or the speaker): our old stream is gone, open a new one."""
        if not self._active:
            return
        self._stream = None  # freed by Pa_Terminate: don't call into it
        self._stop_worker()
        self._open()

    async def frames(self) -> AsyncIterator[np.ndarray]:
        assert self._queue is not None, "call start() first"
        while True:
            try:
                block = await asyncio.wait_for(self._queue.get(), self.stall_timeout_s)
            except TimeoutError:
                await self._recover()
                continue
            yield block.astype(np.float32) * (1.0 / 32768.0)

    async def _recover(self) -> None:
        log.warning("Microphone stopped delivering audio; reopening")
        await asyncio.to_thread(self._close_stream)
        delay = 1.0
        while True:
            # Re-initializing PortAudio re-scans devices (a re-plugged headset gets a new
            # index) and reopens every registered stream, including this one via _after_reinit.
            try:
                await asyncio.to_thread(reinitialize_portaudio)
            except Exception as exc:
                log.warning("PortAudio re-init failed: %s", exc)
            if self._stream is None:  # the re-init hook didn't reopen us: try directly
                try:
                    await asyncio.to_thread(self._open)
                except Exception as exc:
                    log.warning("Microphone reopen failed: %s", exc)
            if self._stream is not None:
                return
            log.warning("Microphone reopen failed (retrying in %.0fs)", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def measure_level(self, seconds: float = 1.0) -> float:
        """Peak absolute sample value (0..1) over the next ``seconds``. Used as a startup self-test."""
        assert self._queue is not None
        peak = 0.0
        needed = int(seconds * SAMPLE_RATE / FRAME_SAMPLES)
        for _ in range(needed):
            block = await asyncio.wait_for(self._queue.get(), self.stall_timeout_s)
            peak = max(peak, float(np.abs(block.astype(np.int32)).max()) / 32768.0)
        return peak

    async def close(self) -> None:
        self._active = False
        if self._remove_hook is not None:
            self._remove_hook()
            self._remove_hook = None
        self._loop = None
        await asyncio.to_thread(self._close_stream)
