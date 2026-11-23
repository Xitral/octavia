"""Local speech recognition with NVIDIA Parakeet TDT 0.6B (int8 ONNX via onnx-asr).

Chosen by benchmark on this PC (Ryzen 9 7900X, no CUDA): ~170 ms to
transcribe a short command after the user stops talking, with better
accuracy than faster-whisper small.en (~1.1 s) and on par with
distil-large-v3.5 (~4 s). v2 is English-only; v3 adds 24 more European
languages.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from octavia.config import SttConfig
from octavia.stt.base import SpeechToText, TranscriptionResult

log = logging.getLogger(__name__)

MODEL_FILES = ("config.json", "vocab.txt", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx")
# The fp32 encoder keeps its weights in a separate external-data file (~2.4 GB).
FP32_FILES = ("config.json", "vocab.txt", "encoder-model.onnx", "encoder-model.onnx.data", "decoder_joint-model.onnx")
SOURCE_MARKER = ".hf_repo"  # which Hugging Face repo the folder was downloaded from


class ModelMismatchError(RuntimeError):
    pass


class ParakeetSTT(SpeechToText):
    name = "parakeet"

    def __init__(self, cfg: SttConfig, model_dir: Path) -> None:
        super().__init__()
        self.cfg = cfg
        self.model_dir = model_dir
        self._model = None

    def files_present(self) -> bool:
        return all((self.model_dir / f).exists() for f in self.required_files())

    def required_files(self) -> tuple[str, ...]:
        return MODEL_FILES if self.cfg.quantization == "int8" else FP32_FILES

    def source_repo(self) -> str | None:
        marker = self.model_dir / SOURCE_MARKER
        return marker.read_text(encoding="utf-8").strip() if marker.exists() else None

    def check_source(self) -> None:
        """The model name only picks the architecture; the weights come from the folder.
        So switching stt.hf_repo (e.g. to v3) while model_dir still holds v2 would silently
        keep using v2. Refuse instead."""
        source = self.source_repo()
        if source and source != self.cfg.hf_repo:
            raise ModelMismatchError(
                f"{self.model_dir} holds {source}, but stt.hf_repo is {self.cfg.hf_repo}. "
                "Set stt.model_dir to a new folder (or null to derive one), then run `uv run octavia setup`."
            )

    def load(self) -> None:
        self.check_source()
        if not self.files_present():
            raise FileNotFoundError(f"Speech model missing in {self.model_dir}. Run `uv run octavia setup`.")
        import onnx_asr
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.cfg.num_threads
        opts.log_severity_level = 3
        # With the arena on, memory grows to fit the longest utterance yet (+265 MB after a 30 s one) and is never
        # given back. Off, it stays flat, for about 16 ms more per 4 s command (measured on this PC).
        opts.enable_cpu_mem_arena = False
        started = time.perf_counter()
        self._model = onnx_asr.load_model(
            self.cfg.model,
            str(self.model_dir),
            quantization=self.cfg.quantization,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        # The first inference is slower (allocations); pay it now, not on the first question.
        self._model.recognize(np.zeros(8000, dtype=np.float32), sample_rate=16000)
        log.info("Parakeet loaded in %.1fs", time.perf_counter() - started)

    def transcribe_sync(self, audio: np.ndarray) -> TranscriptionResult:
        if self._model is None:
            raise RuntimeError("ParakeetSTT.load() was not called")
        audio = np.asarray(audio, dtype=np.float32)
        started = time.perf_counter()
        text = self._model.recognize(audio, sample_rate=16000) if audio.size else ""
        if not isinstance(text, str):  # batch inputs return a list
            text = " ".join(text)
        return TranscriptionResult(
            text=text.strip(),
            language=self.cfg.language,
            duration_s=audio.size / 16000,
            latency_s=time.perf_counter() - started,
        )


def download(cfg: SttConfig, model_dir: Path) -> bool:
    """Fetch the model files from Hugging Face into ``model_dir``."""
    from huggingface_hub import snapshot_download

    stt = ParakeetSTT(cfg, model_dir)
    try:
        stt.check_source()
    except ModelMismatchError as exc:
        print(f"  FAIL speech recognition model: {exc}")
        return False
    if stt.files_present():
        print(f"  ok   speech recognition model ({model_dir.name})")
        return True
    size = "about 630 MB" if cfg.quantization == "int8" else "about 2.5 GB"
    print(f"  get  speech recognition model {cfg.hf_repo} ({size})")
    try:
        snapshot_download(cfg.hf_repo, local_dir=str(model_dir), allow_patterns=list(stt.required_files()))
    except Exception as exc:
        log.exception("Speech model download failed")
        print(f"  FAIL speech recognition model: {exc}")
        return False
    ok = stt.files_present()
    if ok:
        (model_dir / SOURCE_MARKER).write_text(cfg.hf_repo, encoding="utf-8")
    print(f"  {'done' if ok else 'FAIL'} speech recognition model")
    return ok
