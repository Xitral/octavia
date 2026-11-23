"""Silero VAD v6 running on onnxruntime (no torch).

Model contract (silero_vad.onnx v6.2):
    inputs  input [1, 576] float32 = 64 samples of context + 512 new samples @ 16 kHz
            state [2, 1, 128] float32
            sr    int64 scalar (16000)
    outputs output [1, 1] speech probability, stateN [2, 1, 128]

Prepending the 64-sample context is mandatory: without it the model runs but
returns garbage. Cost is ~0.1-0.8 ms per 32 ms frame on one thread.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from octavia.vad.base import VoiceActivityDetector

MODEL_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"


class SileroVAD(VoiceActivityDetector):
    name = "silero"
    SAMPLE_RATE = 16000
    FRAME = 512
    CONTEXT = 64

    def __init__(self, model_path: str | Path) -> None:
        self.model_path = Path(model_path)
        self._sess = None
        self._sr = np.array(self.SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def load(self) -> None:
        import onnxruntime as ort

        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Silero VAD model not found at {self.model_path}. Run `octavia setup` to download it."
            )
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self._sess = ort.InferenceSession(str(self.model_path), sess_options=opts, providers=["CPUExecutionProvider"])

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        if self._sess is None:
            raise RuntimeError("SileroVAD.load() was not called")
        x = np.asarray(frame, dtype=np.float32).reshape(1, -1)
        if x.shape[1] != self.FRAME:
            raise ValueError(f"expected {self.FRAME} samples, got {x.shape[1]}")
        x = np.concatenate([self._context, x], axis=1)
        out, self._state = self._sess.run(["output", "stateN"], {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -self.CONTEXT :]
        return float(out[0, 0])
