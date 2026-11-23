"""Acoustic echo cancellation (WebRTC AEC3 via the ``livekit`` package).

Lets voice barge-in work on speakers: the microphone hears the assistant's
own voice, and the canceller subtracts it using the exact samples that were
sent to the speaker (the *reference*).

Threading model, which matters more than any tuning knob:

* ``add_reference`` runs on the PortAudio output callback thread. It only
  converts and re-cuts the block into 10 ms frames and appends them to a
  bounded deque; no APM calls there.
* ``process`` runs on exactly one capture-processing thread. Before every
  10 ms mic frame it feeds *all* pending reference frames, so a reference
  frame can never reach the APM after the capture frame holding its echo.
  Late references are catastrophic (30 ms late: ~30 dB -> ~8 dB of echo
  reduction), early ones are harmless because AEC3 estimates the delay.

The APM is created once and kept: AEC3 needs a few seconds of playback to
converge, so recreating it on a device hiccup would throw that away.

Wiring: ``player.on_render = lambda b: ec.add_reference(b, player.sample_rate)``,
``MicrophoneSource(echo_canceller=ec)``, ``mic.set_output_latency(player.latency)``,
and ``ec.close()`` at shutdown.
"""

from __future__ import annotations

import atexit
import importlib
import logging
import math
import threading
import time
import weakref
from collections import deque
from types import ModuleType
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

MAX_STREAM_DELAY_MS = 500  # set_stream_delay_ms raises RuntimeError above this
_FAR_ACTIVE_RMS = 32768 * 10 ** (-50 / 20)  # reference louder than -50 dBFS counts as "speaker playing"
_FAR_HOLD_FRAMES = 30  # echo (delay + reverb) outlasts the reference by up to ~300 ms
_MIC_FLOOR_RMS = 32768 * 10 ** (-80 / 20)
_ERLE_ALPHA = 0.995  # ~2 s time constant at 100 frames/s

_live: weakref.WeakSet[EchoCanceller] = weakref.WeakSet()
_atexit_registered = False


def _load_rtc() -> ModuleType:
    return importlib.import_module("livekit.rtc")


def _close_all() -> None:
    for canceller in list(_live):
        canceller.close()


def _register_atexit() -> None:
    """Close leftover cancellers before livekit's own atexit hook disposes the FFI runtime.

    A native handle dropped after that prints an AssertionError from
    ``FfiHandle.__del__``. atexit runs hooks last-in first-out, and livekit
    registers its hook when the first APM is created, so ours is registered
    after that point.
    """
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(_close_all)
        _atexit_registered = True


class EchoCanceller:
    """WebRTC AEC3 for a mono capture stream, fed with the speaker output as reference."""

    MAX_REFERENCE_BACKLOG_S = 2.0

    @classmethod
    def available(cls) -> bool:
        try:
            _load_rtc()
        except Exception:
            return False
        return True

    def __init__(
        self,
        capture_rate: int = 16000,
        noise_suppression: bool = False,
        high_pass_filter: bool = True,
    ) -> None:
        if capture_rate <= 0 or capture_rate % 100:
            raise ValueError(f"capture_rate must be a multiple of 100 Hz, got {capture_rate}")
        try:
            rtc = _load_rtc()
            apm = rtc.AudioProcessingModule(
                echo_cancellation=True,
                noise_suppression=noise_suppression,
                high_pass_filter=high_pass_filter,
                auto_gain_control=False,
            )
        except Exception as exc:  # not installed, or its native library failed to load
            raise RuntimeError(f"Echo cancellation needs a working 'livekit' package ({exc})") from exc
        self.capture_rate = capture_rate
        self._rtc = rtc
        self._apm: Any = apm
        self._lock = threading.Lock()  # guards the APM and capture-side state
        self._closed = False
        self._cap_spf = capture_rate // 100
        # The APM processes in place; one reusable frame avoids per-frame allocations.
        self._cap_buf = bytearray(self._cap_spf * 2)
        self._cap_view = np.frombuffer(self._cap_buf, dtype=np.int16)
        self._cap_frame = rtc.AudioFrame(self._cap_buf, capture_rate, 1, self._cap_spf)
        self._cap_rem = np.zeros(0, dtype=np.int16)
        self._rev_frames: dict[int, tuple[np.ndarray, Any]] = {}
        self._ref: deque[tuple[int, np.ndarray]] = deque(maxlen=int(self.MAX_REFERENCE_BACKLOG_S * 100))
        self._ref_rate = 0  # output-thread state
        self._ref_rem = np.zeros(0, dtype=np.int16)
        self._bad_rate_logged = False
        self.output_latency = 0.0
        self.input_latency = 0.0
        self._delay_ms = 0
        # stats
        self.reference_frames = 0
        self.reference_dropped = 0
        self.capture_frames = 0
        self.errors = 0
        self.max_backlog = 0
        self._proc_ms = 0.0
        self._far_hold = 0
        self._mic_e = 0.0
        self._out_e = 0.0
        self._erle_frames = 0
        _live.add(self)
        _register_atexit()

    # -- configuration ----------------------------------------------------------
    def set_latencies(self, output_s: float, input_s: float) -> None:
        """Delay hint = speaker + mic buffer latency; AEC3 refines it, but it helps at longer delays."""
        self.output_latency = max(0.0, float(output_s))
        self.input_latency = max(0.0, float(input_s))
        total_ms = (self.output_latency + self.input_latency) * 1000.0
        self._delay_ms = int(min(MAX_STREAM_DELAY_MS, max(0.0, round(total_ms))))

    @property
    def delay_ms(self) -> int:
        return self._delay_ms

    # -- output callback thread -------------------------------------------------
    def add_reference(self, block: np.ndarray, sample_rate: int) -> None:
        """Queue what was just written to the speaker (float32 mono, any block size)."""
        if self._closed:
            return
        sample_rate = int(sample_rate)
        spf = sample_rate // 100
        if spf <= 0 or sample_rate % 100:
            if not self._bad_rate_logged:
                self._bad_rate_logged = True
                log.warning("Echo cancellation can't use a %d Hz output stream (not a multiple of 100 Hz)", sample_rate)
            return
        x = np.asarray(block, dtype=np.float32)
        if x.ndim > 1:
            x = x[:, 0]
        pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
        if sample_rate != self._ref_rate:
            self._ref_rate = sample_rate
            self._ref_rem = pcm[:0]
        rem = np.concatenate((self._ref_rem, pcm)) if self._ref_rem.size else pcm
        n = rem.size // spf
        ref = self._ref
        for i in range(n):
            if len(ref) == ref.maxlen:
                self.reference_dropped += 1  # nobody is consuming (mic stopped); keep the newest
            ref.append((sample_rate, rem[i * spf : (i + 1) * spf]))
        self._ref_rem = rem[n * spf :].copy()

    # -- capture processing thread ----------------------------------------------
    def process(self, block: np.ndarray) -> np.ndarray:
        """Clean int16 mic samples at ``capture_rate``.

        Returns only whole 10 ms frames; the remainder is kept for the next call,
        so the output trails the input by a fixed amount under 10 ms (plus the
        APM's own 4 ms at 16 kHz).
        """
        block = np.asarray(block, dtype=np.int16).reshape(-1)
        with self._lock:
            apm = self._apm
            if apm is None:
                return block.copy()
            spf = self._cap_spf
            rem = np.concatenate((self._cap_rem, block)) if self._cap_rem.size else block
            n = rem.size // spf
            out = np.empty(n * spf, dtype=np.int16)
            for i in range(n):
                a, b = i * spf, (i + 1) * spf
                out[a:b] = self._process_frame(apm, rem[a:b])
            self._cap_rem = rem[n * spf :].copy()
            return out

    def _process_frame(self, apm: Any, raw: np.ndarray) -> np.ndarray:
        t0 = time.perf_counter()
        try:
            self._feed_reference(apm)
            apm.set_stream_delay_ms(self._delay_ms)
            self._cap_view[:] = raw
            apm.process_stream(self._cap_frame)
        except Exception:
            self.errors += 1
            if self.errors <= 3 or self.errors % 1000 == 0:
                log.warning("Echo canceller failed (%d errors); passing audio through", self.errors, exc_info=True)
            return raw
        clean = self._cap_view
        self.capture_frames += 1
        self._proc_ms += 0.01 * ((time.perf_counter() - t0) * 1000.0 - self._proc_ms)
        self._track_erle(raw, clean)
        return clean

    def _feed_reference(self, apm: Any) -> None:
        ref = self._ref
        fed = 0
        while ref:
            try:
                rate, samples = ref.popleft()
            except IndexError:  # reset() raced us
                break
            view, frame = self._reverse_frame(rate, samples.size)
            view[:] = samples
            apm.process_reverse_stream(frame)
            fed += 1
            if self._far_hold < _FAR_HOLD_FRAMES:
                f = samples.astype(np.float32)
                if float(np.dot(f, f)) > _FAR_ACTIVE_RMS**2 * f.size:
                    self._far_hold = _FAR_HOLD_FRAMES
        self.reference_frames += fed
        self.max_backlog = max(self.max_backlog, fed)

    def _reverse_frame(self, rate: int, spf: int) -> tuple[np.ndarray, Any]:
        cached = self._rev_frames.get(rate)
        if cached is None:
            buf = bytearray(spf * 2)
            cached = (np.frombuffer(buf, dtype=np.int16), self._rtc.AudioFrame(buf, rate, 1, spf))
            self._rev_frames[rate] = cached
        return cached

    def _track_erle(self, raw: np.ndarray, clean: np.ndarray) -> None:
        """Rough echo-reduction monitor over frames where the speaker was playing.

        Double-talk (the user speaking over playback) pulls it down; it is a
        health indicator, not a measurement.
        """
        if self._far_hold <= 0:
            return
        self._far_hold -= 1
        r = raw.astype(np.float32)
        mic_e = float(np.dot(r, r)) / r.size
        if mic_e < _MIC_FLOOR_RMS**2:
            return
        c = clean.astype(np.float32)
        out_e = float(np.dot(c, c)) / c.size
        self._mic_e = _ERLE_ALPHA * self._mic_e + (1 - _ERLE_ALPHA) * mic_e
        self._out_e = _ERLE_ALPHA * self._out_e + (1 - _ERLE_ALPHA) * out_e
        self._erle_frames += 1

    @property
    def erle_db(self) -> float | None:
        """Smoothed echo reduction while the speaker plays; None until there is enough data."""
        if self._erle_frames < 100 or self._mic_e <= 0:
            return None
        return 10.0 * math.log10(self._mic_e / max(self._out_e, 1e-3))

    def stats(self) -> dict[str, Any]:
        erle = self.erle_db
        return {
            "reference_frames": self.reference_frames,
            "reference_pending": len(self._ref),
            "reference_dropped": self.reference_dropped,
            "capture_frames": self.capture_frames,
            "errors": self.errors,
            "max_backlog": self.max_backlog,
            "delay_ms": self._delay_ms,
            "proc_ms": round(self._proc_ms, 3),
            "erle_db": None if erle is None else round(erle, 1),
        }

    # -- lifecycle ----------------------------------------------------------------
    def reset(self) -> None:
        """Drop buffered audio (after a stream restart). The adaptive filter state is kept."""
        with self._lock:
            self._ref.clear()
            self._cap_rem = np.zeros(0, dtype=np.int16)
            self._far_hold = 0

    def close(self) -> None:
        if not self._lock.acquire(timeout=2.0):
            log.warning("Echo canceller busy; not releasing it")
            return
        try:
            self._closed = True
            apm, self._apm = self._apm, None
            self._ref.clear()
        finally:
            self._lock.release()
        _live.discard(self)
        if apm is not None:
            # Drop the native APM now; if it survives until interpreter exit,
            # livekit's FfiHandle.__del__ fails with an AssertionError.
            del apm
