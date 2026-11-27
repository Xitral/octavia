"""Interruptible streaming audio player (PortAudio callback output).

A float32 mono callback stream (WASAPI shared mode with auto_convert on
Windows, ``OUTPUT_LATENCY_S`` of buffer) plays chunks from a deque. ``clear()`` drops
everything with a 6 ms fade so barge-in is click-free; what the device already
holds (``OUTPUT_LATENCY_S``) still plays out.
``mark(cb)`` lets the speech pipeline learn when each sentence has been played.
With a ``Panner`` (``audio/spatial.py``, ``audio.directional``) the stream is stereo: everything is mixed in mono as
before, then spread over the two channels by where she is on screen.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from octavia.audio.devices import DeviceChoice, add_reinit_hook, choose_device, ensure_com, reinitialize_portaudio
from octavia.audio.sink import AudioSink
from octavia.audio.spatial import Panner

log = logging.getLogger(__name__)

# How much audio the speaker holds ahead. The callback runs in Python, so a busy moment (a game, the avatar, the
# local model loading) can hold it up for 30 ms or more; with the ~23 ms WASAPI's "low" gives, that ran the device
# dry and popped. This much rides out such a stall, and is still only a few frames more before a stop is heard.
OUTPUT_LATENCY_S = 0.08


class _Mark:
    __slots__ = ("callback",)

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback


class SoundDevicePlayer(AudioSink):
    def __init__(
        self,
        sample_rate: int = 44100,
        device: int | str | None = None,
        host_api: str | None = None,
        fade_ms: float = 6.0,
        latency: str | float = OUTPUT_LATENCY_S,
        spatial: Panner | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        # Directional audio (``audio/spatial.py``): a stereo stream, her mono mix panned by where she is. None: mono,
        # as it always was. A speaker that won't open in stereo opens in mono instead (``channels`` says which).
        self.spatial = spatial
        self.channels = 2 if spatial is not None else 1
        self.spatial_errors = 0
        self.device_ref = device
        self.host_api = host_api
        self.latency_setting = latency
        self.choice: DeviceChoice | None = None
        self.underruns = 0  # the queue ran out mid-sentence: the audio wasn't there in time
        self.device_underruns = 0  # the device asked and we were too late to answer (the process was busy)
        self.underrun_s = 0.0  # how long the speaker was silent in all those mid-sentence run-outs
        self.longest_underrun_s = 0.0
        self._dry_since = 0.0  # ``time.perf_counter()`` when the queue ran out mid-sentence, until audio came back
        self._starved_after = 0.0  # a gap between callbacks this long means the device ran out (its buffer's length)
        self._last_block = 0.0  # ``time.perf_counter()`` of the last callback since the stream opened (0: none yet)
        self.render_tap_errors = 0
        # Tap for the echo-cancellation reference. Runs on the audio thread with a copy of each
        # post-mix block exactly as written to the device, so it must be fast and must not block.
        self.on_render: Callable[[np.ndarray], None] | None = None
        # Nothing comes out of this PC's speaker (a voice call has her voice instead): the render tap still gets
        # every block, so whatever listens there (the call) hears her as usual.
        self.muted = False
        # The loudest sample written since ``take_peak`` last asked (the tray icon's wave). Only kept once asked.
        self._peak = 0.0
        self._metering = False
        self._fade_len = max(1, int(sample_rate * fade_ms / 1000))
        self._lock = threading.Lock()
        self._items: deque[np.ndarray | _Mark] = deque()
        self._head_pos = 0
        self._queued = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = None
        self._active = False
        self._last_callback = 0.0
        self._rendered = 0  # samples of real (queued) audio handed to the device
        self.mark_played_seconds = 0.0  # where the audio stood at the last mark reached (``_marked``)
        self._watchdog: asyncio.Task | None = None
        self._remove_hook: Callable[[], None] | None = None
        # Mixed over the queue (``overlay``, ``set_loop``): [sound, samples played] each, and a loop with its gain.
        self._overlays: list[list] = []
        self._loop_audio: np.ndarray | None = None
        self._loop_pos = 0
        self._loop_gain = 0.0
        self._loop_target = 0.0
        self._loop_in = 1.0 / max(1, int(sample_rate * 0.35))  # gain per sample: fades in over 350 ms...
        self._loop_out = 1.0 / max(1, int(sample_rate * 0.12))  # ...and out over 120 ms

    # -- lifecycle -------------------------------------------------------------
    def _open(self) -> None:
        import sounddevice as sd

        ensure_com()  # this may run on any worker thread
        self._starved_after = 0.0  # a stream starting up isn't the speaker running dry

        self.choice = choose_device(self.device_ref, "output", self.host_api)

        def opened(channels: int):
            return sd.OutputStream(
                samplerate=self.sample_rate,
                channels=channels,
                dtype="float32",
                device=self.choice.index,
                latency=self.latency_setting,
                extra_settings=self.choice.extra_settings,
                callback=self._callback,
            )

        try:
            stream = opened(self.channels)
        except Exception:
            if self.channels == 1:
                raise
            log.warning("The speaker didn't open in stereo, so her voice comes from the middle", exc_info=True)
            self.channels = 1
            stream = opened(1)
        self._last_callback = time.monotonic()
        self._last_block = 0.0
        stream.start()
        self._stream = stream
        self._starved_after = self.latency
        log.info(
            "Speaker: %s (%s), latency %.0f ms%s",
            self.choice.name,
            self.choice.host_api,
            self.latency * 1000,
            ", stereo (her voice from where she is)" if self.channels == 2 else "",
        )

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._open)
        self._active = True
        self._remove_hook = add_reinit_hook(self._after_reinit)
        self._watchdog = asyncio.create_task(self._watch(), name="speaker-watchdog")

    def _after_reinit(self) -> None:
        """PortAudio was re-initialized (by us or the mic): our old stream is gone, open a new one."""
        if not self._active:
            return
        self._stream = None  # freed by Pa_Terminate: don't call into it
        self._open()  # queued audio and marks are kept and play on the new stream

    async def _watch(self) -> None:
        """Recover when the output device disappears (headset unplugged, Bluetooth off).

        Without this the callback silently stops, playback marks never fire, and
        every reply waits forever for audio that can't play.
        """
        failures = 0
        while True:
            await asyncio.sleep(0.5)
            if not self._stalled():
                failures = 0
                continue
            failures += 1
            log.warning("Speaker stopped playing (attempt %d); reopening the audio device", failures)
            try:
                await asyncio.to_thread(reinitialize_portaudio)
            except Exception:
                log.warning("Audio re-init failed", exc_info=True)
            await asyncio.sleep(1.0)
            if self._stalled() and failures >= 2:
                # Device really gone: release waiters instead of hanging the conversation.
                self.fail_pending()
                await asyncio.sleep(min(30.0, 2.0 * failures))

    def _stalled(self) -> bool:
        stream = self._stream
        if stream is None:
            return self._has_pending()
        try:
            active = bool(stream.active)
        except Exception:  # e.g. "Invalid stream pointer" after the device vanished
            active = False
        waiting = self._has_pending()
        return waiting and (not active or time.monotonic() - self._last_callback > 1.5)

    def _has_pending(self) -> bool:
        with self._lock:
            return bool(self._items)

    def fail_pending(self) -> None:
        """Drop queued audio and fire every pending mark, so nothing waits on a dead device."""
        with self._lock:
            marks = [item.callback for item in self._items if isinstance(item, _Mark)]
            self._items.clear()
            self._head_pos = 0
            self._queued = 0
        if marks:
            log.warning("Dropped queued audio: the speaker isn't playing")
            self._fire(marks)

    async def close(self) -> None:
        self._active = False
        if self._remove_hook is not None:
            self._remove_hook()
            self._remove_hook = None
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        self.clear()
        with self._lock:
            self._overlays.clear()
            self._loop_audio, self._loop_gain, self._loop_target = None, 0.0, 0.0
        stream, self._stream = self._stream, None
        if stream is not None:
            await asyncio.to_thread(lambda: (ensure_com(), stream.stop(), stream.close()))

    @property
    def latency(self) -> float:
        stream = self._stream
        try:
            return float(stream.latency) if stream is not None else 0.0
        except Exception:
            return 0.0

    def take_peak(self) -> float:
        """The loudest sample played since this was last called (0 to 1), from any thread; it starts over at 0."""
        self._metering = True
        peak, self._peak = self._peak, 0.0
        return peak

    @property
    def played_seconds(self) -> float:
        """Total real audio rendered so far (silence between replies doesn't count)."""
        return self._rendered / self.sample_rate

    # -- producer API (event loop thread) ---------------------------------------
    def enqueue(self, audio: np.ndarray) -> None:
        chunk = np.asarray(audio, dtype=np.float32).reshape(-1)
        if chunk.size == 0:
            return
        with self._lock:
            self._items.append(chunk)
            self._queued += chunk.size

    def _marked(self, callback: Callable[[], None], at: float) -> None:
        self.mark_played_seconds = at  # ``played_seconds`` as the mark was reached, for its callback to read
        callback()

    def mark(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._queued == 0 and not self._items:
                immediate = True
            else:
                immediate = False
                self._items.append(_Mark(callback))
        if immediate:
            self._fire([functools.partial(self._marked, callback, self.played_seconds)])

    def clear(self) -> None:
        with self._lock:
            tail = np.zeros(self._fade_len, dtype=np.float32)
            got, _ = self._pull_locked(self._fade_len, tail, collect_marks=False)
            self._items.clear()
            self._head_pos = 0
            self._queued = 0
            self._dry_since = 0.0  # stopped on purpose: what follows isn't a gap
            if got:
                self._items.append(tail[:got] * np.linspace(1.0, 0.0, got, dtype=np.float32))
                self._queued = got

    def pending_seconds(self) -> float:
        return self._queued / self.sample_rate

    def overlay(self, audio: np.ndarray) -> None:
        sound = np.asarray(audio, dtype=np.float32).reshape(-1)
        if sound.size:
            with self._lock:
                self._overlays.append([sound, 0])

    def set_loop(self, audio: np.ndarray | None) -> None:
        with self._lock:
            if audio is None:
                self._loop_target = 0.0
                return
            sound = np.asarray(audio, dtype=np.float32).reshape(-1)
            if not sound.size:
                return
            if self._loop_audio is not sound:
                if self._loop_gain == 0.0 or self._loop_audio is None:
                    self._loop_pos = 0
                else:
                    self._loop_pos %= sound.size
                self._loop_audio = sound
            self._loop_target = 1.0

    def _mix_locked(self, buf: np.ndarray) -> bool:
        """Add the overlays and the loop into ``buf``; True when anything was added."""
        frames = buf.size
        mixed = False
        if self._overlays:
            for item in self._overlays:
                sound, pos = item
                take = min(frames, sound.size - pos)
                buf[:take] += sound[pos : pos + take]
                item[1] = pos + take
            self._overlays = [item for item in self._overlays if item[1] < item[0].size]
            mixed = True
        loop = self._loop_audio
        if loop is not None and (self._loop_gain > 0.0 or self._loop_target > 0.0):
            idx = (self._loop_pos + np.arange(frames)) % loop.size
            if self._loop_gain == self._loop_target:
                gains: np.ndarray | float = self._loop_gain
            else:
                rate = self._loop_in if self._loop_target > self._loop_gain else -self._loop_out
                gains = np.clip(self._loop_gain + rate * np.arange(1, frames + 1, dtype=np.float32), 0.0, 1.0)
                gains = np.minimum(gains, self._loop_target) if rate > 0 else np.maximum(gains, self._loop_target)
                self._loop_gain = float(gains[-1])
            buf += loop[idx] * gains
            self._loop_pos = int((self._loop_pos + frames) % loop.size)
            if self._loop_gain == 0.0 and self._loop_target == 0.0:
                self._loop_audio = None
            mixed = True
        if mixed:
            np.clip(buf, -1.0, 1.0, out=buf)
        return mixed

    # -- audio thread -----------------------------------------------------------
    def _pull_locked(self, n: int, out: np.ndarray, collect_marks: bool = True) -> tuple[int, list[Callable[[], None]]]:
        got = 0
        marks: list[Callable[[], None]] = []
        while self._items:
            head = self._items[0]
            if isinstance(head, _Mark):
                self._items.popleft()
                if collect_marks:
                    # Where the audio stood at the mark, exactly: the block it falls in is counted whole before
                    # the mark runs on the event loop, and the loop may be late.
                    marks.append(
                        functools.partial(self._marked, head.callback, (self._rendered + got) / self.sample_rate)
                    )
                continue
            if got >= n:
                break
            take = min(n - got, head.size - self._head_pos)
            out[got : got + take] = head[self._head_pos : self._head_pos + take]
            got += take
            self._head_pos += take
            if self._head_pos >= head.size:
                self._items.popleft()
                self._head_pos = 0
        self._queued -= got
        return got, marks

    def _callback(self, outdata, frames, time_info, status) -> None:
        self._last_callback = time.monotonic()
        now = time.perf_counter()  # fine-grained: monotonic ticks in 15.6 ms steps on Windows
        late = self._starved_after and self._last_block and now - self._last_block > self._starved_after
        self._last_block = now
        # WASAPI shared mode doesn't always flag an underflow, so a callback that came later than the device's whole
        # buffer counts too: the device had nothing left to play.
        if late or getattr(status, "output_underflow", False):
            self.device_underruns += 1
        stereo = self.channels == 2 and outdata.shape[1] == 2
        # In stereo everything is mixed in mono first, as it always was, and spread over the channels at the end.
        buf = np.zeros(frames, dtype=np.float32) if stereo else outdata[:, 0]
        if not stereo:
            buf.fill(0.0)
        with self._lock:
            had_audio = self._queued > 0
            got, marks = self._pull_locked(frames, buf)
            self._rendered += got
            self._mix_locked(buf)
            if self._dry_since and (got or marks):  # the audio came back (or the sentence ended): how long it was out
                gap = now - self._dry_since
                self.underrun_s += gap
                self.longest_underrun_s = max(self.longest_underrun_s, gap)
                self._dry_since = 0.0
            if had_audio and got < frames and self._queued == 0 and not marks:
                self.underruns += 1
                self._dry_since = now + got / self.sample_rate  # where in the block it ran out (within a block)
        if self._metering:  # two passes over the block, no copy: a few microseconds
            peak = max(float(buf.max()), -float(buf.min()))
            if peak > self._peak:
                self._peak = peak
        on_render = self.on_render  # read once: the app may swap it from another thread
        if on_render is not None:
            try:
                on_render(buf.copy())  # exactly the samples written to the device (AEC reference)
            except Exception:
                self.render_tap_errors += 1
                if self.render_tap_errors == 1:
                    log.warning("Playback tap failed; continuing without it", exc_info=True)
        if self.muted:
            buf.fill(0.0)
        if stereo:
            spatial = self.spatial
            placed = False
            if not self.muted and spatial is not None:
                try:
                    spatial.render(buf, outdata)
                    placed = True
                except Exception:
                    self.spatial_errors += 1  # a panner that fails must never stop her voice
                    if self.spatial_errors == 1:
                        log.warning("Placing her voice failed; she plays from the middle", exc_info=True)
            if not placed:
                outdata[:, 0] = buf  # the middle, both channels the same, as mono was
                outdata[:, 1] = buf
        if marks:
            self._fire(marks)

    def _fire(self, callbacks: list[Callable[[], None]]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for cb in callbacks:
            try:
                loop.call_soon_threadsafe(cb)
            except RuntimeError:
                pass  # loop closing
