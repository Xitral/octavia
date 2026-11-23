"""openWakeWord engine, for a model trained for your specific wake phrase.

openWakeWord can't listen for an arbitrary phrase: each phrase needs its own
trained model (see docs/wake-word.md for training one for free on Colab). Use
it when you want the extra robustness of a dedicated model. The default
``sherpa_kws`` engine needs no training.

Optional dependency: ``uv sync --extra openwakeword``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

from octavia.config import OpenWakeWordConfig
from octavia.wake.base import WakeDetection, WakeWordDetector, WakeWordError

if TYPE_CHECKING:
    from octavia.config import AppConfig

log = logging.getLogger(__name__)

FEATURE_DIR = "models/openwakeword"
_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
FEATURE_MODELS = {
    "melspectrogram.onnx": f"{_RELEASE}/melspectrogram.onnx",
    "embedding_model.onnx": f"{_RELEASE}/embedding_model.onnx",
}
CHUNK = 1280  # openWakeWord consumes 80 ms of 16 kHz int16 audio per prediction


class OpenWakeWordDetector(WakeWordDetector):
    name = "openwakeword"

    def __init__(self, phrase: str, cfg: OpenWakeWordConfig, config: AppConfig) -> None:
        super().__init__(phrase)
        self.cfg = cfg
        self.model_path = config.resolve_path(cfg.model_path) if cfg.model_path else None
        self.feature_dir = config.resolve_path(FEATURE_DIR)
        self._model = None
        self._pending = np.zeros(0, dtype=np.int16)

    def load(self) -> None:
        if self.model_path is None:
            raise WakeWordError(
                f"wake.openwakeword.model_path is empty. openWakeWord needs a model trained for "
                f"'{self.phrase}' (see docs/wake-word.md), or use wake.engine: sherpa_kws, which needs no training."
            )
        if not self.model_path.exists():
            raise WakeWordError(f"openWakeWord model not found: {self.model_path}")
        missing = [name for name in FEATURE_MODELS if not (self.feature_dir / name).exists()]
        if missing:
            raise WakeWordError(
                f"openWakeWord feature models missing in {self.feature_dir}; run `uv run octavia setup`."
            )
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise WakeWordError("openWakeWord isn't installed. Run: uv sync --extra openwakeword") from exc
        self._model = Model(
            wakeword_models=[str(self.model_path)],
            inference_framework="onnx",
            melspec_model_path=str(self.feature_dir / "melspectrogram.onnx"),
            embedding_model_path=str(self.feature_dir / "embedding_model.onnx"),
        )
        log.info("openWakeWord model %s loaded for phrase %r", self.model_path.name, self.phrase)

    def process(self, frame: np.ndarray) -> WakeDetection | None:
        if self._model is None:
            raise RuntimeError("OpenWakeWordDetector.load() was not called")
        pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
        self._pending = np.concatenate((self._pending, pcm)) if self._pending.size else pcm
        best = 0.0
        while self._pending.size >= CHUNK:
            scores = self._model.predict(self._pending[:CHUNK])
            self._pending = self._pending[CHUNK:]
            best = max(best, max(scores.values(), default=0.0))
        if best >= self.cfg.threshold:
            self.reset()  # don't fire again on the tail of the same utterance
            # Scores peak ~0.2 s after the phrase ends; no timestamps, so estimate.
            return WakeDetection(phrase=self.phrase, score=float(best), trailing_s=0.2)
        return None

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)
        if self._model is not None:
            self._model.reset()


def feature_assets(config: AppConfig) -> list:
    from octavia.setup_models import Asset

    return [Asset(f"openWakeWord {name}", url, f"{FEATURE_DIR}/{name}") for name, url in FEATURE_MODELS.items()]
