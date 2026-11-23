"""The speech-emotion model: emotion2vec+ base as ONNX, on onnxruntime and the CPU (no torch, nothing online).

emotion2vec+ (Ma et al., "emotion2vec: Self-Supervised Pre-Training for Speech Emotion Representation", ACL 2024;
Alibaba's FunASR team) is a data2vec-style speech model (about 90M parameters) fine-tuned on about 4,800 hours of
pseudo-labeled emotional speech in many languages; its authors report it well ahead of the other popular open
speech-emotion models across languages and recording conditions. The ONNX export (``REPO``, pinned at
``REVISION``) takes the raw 16 kHz waveform (its normalization is inside the graph) and gives 768 features per
20 ms; their mean through the linear head in ``HEAD_FILE`` gives nine classes: angry, disgusted, fearful, happy,
neutral, other, sad, surprised, unknown.

License: the FunASR Model Open Source License 1.1, which lets you use, copy, modify and share it as long as the
source and the model's name are credited (``LICENSE`` in the model folder; this docstring and the README credit it).

Measured on this PC (Ryzen 9 7900X, fp32, 4 threads): about 80 ms for 2 s of speech, 120 ms for 3 s and 180-240 ms
for 4 s, growing with the length, so only the last ``emotion.max_audio_s`` (3 s) of an utterance is read. An int8
dynamic quantization was tried: six times slower on this CPU, and it changed the answers, so fp32 it is. The time is
hidden anyway: the reading starts while the endpointer is still waiting out the end-of-speech silence.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

REPO = "pankotaro/emotion2vec-plus-base-onnx"
REVISION = "334d4376993c381f059cc36d2c6ed05cbfec5ca1"  # 2026-09-21; pinned so a changed upload is never picked up
MODEL_FILE = "emotion2vec_plus_base.onnx"
HEAD_FILE = "emotion2vec_head.json"
FILES = (MODEL_FILE, HEAD_FILE, "LICENSE", "README.md")
SIZE = "about 375 MB"
LICENSE = "FunASR Model Open Source License 1.1 (credit emotion2vec+ and FunASR)"
LABELS = ("angry", "disgusted", "fearful", "happy", "neutral", "other", "sad", "surprised", "unknown")
SAMPLE_RATE = 16000
_MIN_SAMPLES = 8000  # under half a second there's too little voice to read


class EmotionModel:
    """emotion2vec+ base: ``classify`` gives the probability of each of ``LABELS`` for one utterance."""

    name = "emotion2vec+ base"

    def __init__(self, model_dir: Path, num_threads: int = 4, max_audio_s: float = 3.0) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.max_samples = int(max_audio_s * SAMPLE_RATE)
        self._session = None
        self._input = "input"
        self._weight: np.ndarray | None = None
        self._bias: np.ndarray | None = None
        self.labels: tuple[str, ...] = LABELS

    def files_present(self) -> bool:
        return all((self.model_dir / f).is_file() for f in (MODEL_FILE, HEAD_FILE))

    def load(self) -> None:
        """Load the model and run it once (the first run is the slow one). Blocking: call it from a worker thread."""
        if not self.files_present():
            raise FileNotFoundError(f"Speech emotion model missing in {self.model_dir}. Run `uv run octavia setup`.")
        import onnxruntime as ort

        started = time.perf_counter()
        head = json.loads((self.model_dir / HEAD_FILE).read_text(encoding="utf-8"))
        weight = np.asarray(head["weight"], dtype=np.float32)
        bias = np.asarray(head["bias"], dtype=np.float32)
        labels = tuple(str(label) for label in head["labels"])
        if weight.ndim != 2 or weight.shape[0] != len(labels) or bias.shape != (len(labels),):
            raise ValueError(f"{HEAD_FILE} doesn't match its labels: weight {weight.shape}, bias {bias.shape}")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.num_threads
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        opts.enable_cpu_mem_arena = False  # the arena kept ~70 MB after a 3 s read (see stt/parakeet.py)
        session = ort.InferenceSession(
            str(self.model_dir / MODEL_FILE), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input = session.get_inputs()[0].name
        self._session, self._weight, self._bias, self.labels = session, weight, bias, labels
        self.classify(np.zeros(SAMPLE_RATE, dtype=np.float32))
        log.info("Speech emotion model loaded in %.1fs", time.perf_counter() - started)

    def classify(self, audio: np.ndarray) -> dict[str, float] | None:
        """Probability of each label for 16 kHz mono ``audio`` (its last ``max_audio_s``), or None when it's too
        short to read. Blocking, and not to be run twice at once (the caller has one worker thread)."""
        if self._session is None or self._weight is None or self._bias is None:
            raise RuntimeError("EmotionModel.load() was not called")
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size < _MIN_SAMPLES:
            return None
        x = x[-self.max_samples :]
        features = self._session.run(None, {self._input: x[None, :]})[0]
        pooled = features[0].mean(axis=0)
        logits = self._weight @ pooled + self._bias
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
        return {label: float(p) for label, p in zip(self.labels, probs, strict=True)}


def download(model_dir: Path) -> bool:
    """Fetch the model from Hugging Face into ``model_dir`` (`octavia setup`)."""
    from huggingface_hub import snapshot_download

    model = EmotionModel(model_dir)
    if model.files_present():
        print(f"  ok   speech emotion model ({model_dir.name})")
        return True
    print(f"  get  speech emotion model {REPO} ({SIZE}; {LICENSE})")
    try:
        snapshot_download(REPO, revision=REVISION, local_dir=str(model_dir), allow_patterns=list(FILES))
    except Exception as exc:
        log.exception("Speech emotion model download failed")
        print(f"  FAIL speech emotion model: {exc}")
        return False
    ok = model.files_present()
    print(f"  {'done' if ok else 'FAIL'} speech emotion model")
    return ok
