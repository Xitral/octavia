"""Smart Turn: whether the user has finished their turn, heard from their voice (intonation, rhythm), not their words.

Pipecat's Smart Turn v3 (BSD-2, https://huggingface.co/pipecat-ai/smart-turn-v3) is a Whisper Tiny encoder with a
small classifier: the last 8 s of the utterance in, the probability that the turn is complete out. 8 MB int8 ONNX,
a few tens of ms on the CPU, so it doesn't touch the GPU (games, the local model).

Today it only runs in **shadow**: in each pause of a request it gives its verdict, which goes to the routing log next
to what actually happened (the user went on talking, or the full silence ended the utterance). Nothing she does
changes. ``octavia routing`` then says how often it would have ended a turn early and how often it would have cut the
user off, which decides whether the end-of-speech silence can safely drop (see docs/instant-conversation.md).

The features are Whisper's log-mel spectrogram, written here with numpy (no transformers/torch): the audio is
normalised to zero mean and unit variance, padded with zeros at the end to 8 s, and turned into 80 mel bands x 800
frames, exactly as ``WhisperFeatureExtractor(chunk_length=8)`` does in Smart Turn's own ``inference.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np

from octavia.vad.endpointer import Endpointer

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WINDOW_S = 8
_N_SAMPLES = SAMPLE_RATE * WINDOW_S
_N_FFT = 400
_HOP = 160
_N_MELS = 80


@cache
def _mel_filters() -> np.ndarray:
    """Whisper's mel filterbank (librosa's ``mel(sr=16000, n_fft=400, n_mels=80)``: Slaney scale and norm), (80, 201)."""
    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0

    def hz_to_mel(f: np.ndarray) -> np.ndarray:
        f = np.asarray(f, dtype=np.float64)
        return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, f / f_sp)

    def mel_to_hz(m: np.ndarray) -> np.ndarray:
        return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f_sp * m)

    fft_freqs = np.linspace(0, SAMPLE_RATE / 2, 1 + _N_FFT // 2)
    mel_pts = mel_to_hz(np.linspace(hz_to_mel(np.array(0.0)), hz_to_mel(np.array(SAMPLE_RATE / 2)), _N_MELS + 2))
    fdiff = np.diff(mel_pts)
    ramps = mel_pts[:, None] - fft_freqs[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_pts[2 : _N_MELS + 2] - mel_pts[:_N_MELS]))[:, None]
    return weights.astype(np.float32)


@cache
def _window() -> np.ndarray:
    return (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_N_FFT) / _N_FFT)).astype(np.float32)  # periodic Hann


def features(audio: np.ndarray) -> np.ndarray:
    """The model's input for ``audio`` (16 kHz mono float): its last 8 s as Whisper log-mel features, (1, 80, 800)."""
    x = np.asarray(audio, dtype=np.float32).reshape(-1)[-_N_SAMPLES:]
    if x.size:
        x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    x = np.pad(x, (0, _N_SAMPLES - x.size))
    padded = np.pad(x, _N_FFT // 2, mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, _N_FFT)[::_HOP][:-1]  # 800 frames (the last dropped)
    power = np.abs(np.fft.rfft(frames * _window(), axis=1)) ** 2
    mel = _mel_filters() @ power.T.astype(np.float32)
    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0)[None].astype(np.float32)


class SmartTurn:
    """The model, loaded on first use. ``probability`` is blocking (run it off the event loop) and thread-safe."""

    def __init__(self, model_path: str | Path, threads: int = 1) -> None:
        self.model_path = Path(model_path)
        self.threads = threads
        self._sess = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.model_path.is_file()

    def load(self) -> None:
        import onnxruntime as ort

        if self._sess is not None:
            return
        opts = ort.SessionOptions()
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = self.threads
        opts.log_severity_level = 3
        self._sess = ort.InferenceSession(str(self.model_path), sess_options=opts, providers=["CPUExecutionProvider"])

    def probability(self, audio: np.ndarray) -> float:
        """How sure it is that the speaker has finished (0-1), from the end of ``audio``."""
        with self._lock:
            self.load()
            assert self._sess is not None
            started = time.perf_counter()
            out = self._sess.run(None, {"input_features": features(audio)})
            log.debug("Smart Turn: %.3f in %.0f ms", float(out[0][0][0]), 1000 * (time.perf_counter() - started))
            return float(out[0][0][0])


MODEL_URL = "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx"
MODEL_SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"

# Into a pause when the verdict is asked for: what Pipecat waits (its VAD's stop_secs) before asking Smart Turn.
AT_MS = 200.0


@dataclass
class _Pause:
    future: Future[float]
    longest_ms: float  # how long the pause went on (the last one: the silence that ended the utterance)


class TurnShadow:
    """Smart Turn's verdict on each pause of one capture, asked for ``AT_MS`` into it, off the audio thread.

    ``frame`` is called for each captured frame after the endpointer has seen it, ``reset`` for a new capture, and
    ``verdicts`` when the utterance has ended. A pause is told apart by the endpointer's ``sound_frames`` (unchanged
    = the same pause). One verdict at a time: a pause that comes while one is running gets its own on a later frame,
    if it's still going. A failed verdict is just missing.
    """

    def __init__(self, model: SmartTurn, endpointer: Endpointer) -> None:
        self.model = model
        self.endpointer = endpointer
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="smart-turn")
        self._pauses: dict[int, _Pause] = {}
        self._running: Future[float] | None = None
        self._failed = False
        self._pool.submit(model.load)  # loaded now, so the first pause's verdict isn't late

    def reset(self) -> None:
        self._pauses = {}

    def frame(self) -> None:
        ep = self.endpointer
        pause = ep.pause_ms
        if pause <= 0 or self._failed:
            return
        key = ep.sound_frames
        known = self._pauses.get(key)
        if known is not None:
            known.longest_ms = pause
            return
        if pause < max(AT_MS, ep.s.tail_ms) or (self._running is not None and not self._running.done()):
            return
        audio = ep.speech_so_far()
        if audio.size == 0:
            return
        future = self._pool.submit(self.model.probability, audio)
        future.add_done_callback(self._check_failure)
        self._running = future
        self._pauses[key] = _Pause(future, pause)

    def verdicts(self) -> tuple[float | None, tuple[tuple[float, float], ...]]:
        """For the capture that just ended: the verdict on its last pause (None: none ready), and (verdict, pause
        length in ms) for each earlier pause, after which the user went on talking."""
        last_key = self.endpointer.sound_frames
        last = None
        earlier = []
        for key, pause in self._pauses.items():
            p = _ready(pause.future)
            if p is None:
                continue
            if key == last_key:
                last = round(p, 3)
            else:
                earlier.append((round(p, 3), round(pause.longest_ms)))
        return last, tuple(earlier)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _check_failure(self, future: Future[float]) -> None:
        if not future.cancelled() and (exc := future.exception()) is not None and not self._failed:
            self._failed = True  # a missing or broken model: say so once, then stay out of the way
            log.warning("Smart Turn failed; its shadow verdicts are off until restart: %s", exc)


def _ready(future: Future[float]) -> float | None:
    if not future.done() or future.cancelled() or future.exception() is not None:
        return None
    return future.result()
