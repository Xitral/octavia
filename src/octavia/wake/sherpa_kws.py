"""Open-vocabulary wake phrase detection with sherpa-onnx keyword spotting.

A small streaming zipformer transducer (3.3M parameters, trained on
GigaSpeech) listens for a keyword expressed as BPE tokens. The tokens are
generated from ``assistant.wake_phrase`` at startup, so any English phrase
works and renaming the assistant needs no training. Nothing leaves the PC.

Keyword line syntax used by sherpa-onnx: ``<tokens> :<boost> #<threshold> @<LABEL>``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from octavia.audio.types import SAMPLE_RATE
from octavia.config import SherpaKwsConfig
from octavia.wake.base import WakeDetection, WakeWordDetector, WakeWordError

log = logging.getLogger(__name__)

MODEL_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL_NAME}.tar.bz2"
MODEL_SHA256 = "f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a"
_EPOCH = "epoch-12-avg-2-chunk-16-left-64"
MODEL_FILES = {
    "tokens": "tokens.txt",
    "bpe": "bpe.model",
    "encoder": f"encoder-{_EPOCH}.int8.onnx",
    "decoder": f"decoder-{_EPOCH}.onnx",
    "joiner": f"joiner-{_EPOCH}.int8.onnx",
}


_LAST_TOKEN_S = 0.15  # rough duration of the final token (timestamps mark token starts)
_DEFAULT_TRAILING_S = 0.25


def normalize_phrase(phrase: str) -> str:
    """Upper-case words only: the KWS vocabulary has no punctuation or digits."""
    words = re.sub(r"[^A-Za-z' ]+", " ", phrase).upper().split()
    return " ".join(words)


class SherpaKeywordSpotter(WakeWordDetector):
    name = "sherpa_kws"

    def __init__(self, phrase: str, cfg: SherpaKwsConfig, model_dir: Path) -> None:
        super().__init__(phrase)
        self.cfg = cfg
        self.model_dir = model_dir
        self.keyword_line = ""
        self._kws = None
        self._stream = None
        self._stream_samples = 0

    def _path(self, key: str) -> Path:
        return self.model_dir / MODEL_FILES[key]

    def build_keyword_line(self) -> str:
        """All spellings of the phrase, '/'-separated, sharing one label."""
        # Same as sherpa_onnx.text2token(tokens_type="bpe"), which needlessly imports pypinyin.
        import sentencepiece as spm

        text = normalize_phrase(self.phrase)
        if not text:
            raise WakeWordError(f"Wake phrase {self.phrase!r} has no usable words (letters only, e.g. 'hey computer').")
        if len(text.split()) < 2:
            log.warning(
                "One-word wake phrases fire on ordinary sentences; two words (e.g. 'hey %s') work much better",
                text.lower(),
            )
        # model_proto, not model_file: sentencepiece opens paths with the ANSI API on Windows,
        # which fails for folders with non-ASCII names (a user folder named José).
        sp = spm.SentencePieceProcessor(model_proto=self._path("bpe").read_bytes())
        vocab = {
            line.split()[0] for line in self._path("tokens").read_text(encoding="utf-8").splitlines() if line.strip()
        }
        label = text.replace(" ", "_")
        lines = []
        for spelling in [text, *(normalize_phrase(s) for s in self.cfg.extra_spellings)]:
            if not spelling:
                continue
            tokens = sp.encode(spelling, out_type=str)
            unknown = [t for t in tokens if t not in vocab]
            if not tokens or unknown:
                raise WakeWordError(f"Can't spell {spelling!r} with the keyword model's tokens: {unknown}")
            lines.append(f"{' '.join(tokens)} :{self.cfg.boost} #{self.cfg.threshold} @{label}")
        return "/".join(lines)

    def load(self) -> None:
        missing = [name for name in MODEL_FILES.values() if not (self.model_dir / name).exists()]
        if missing:
            raise WakeWordError(
                f"Keyword model missing in {self.model_dir} ({', '.join(missing)}). Run `uv run octavia setup`."
            )
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise WakeWordError("sherpa-onnx is not installed (uv sync)") from exc
        self.keyword_line = self.build_keyword_line()
        # The constructor wants a keywords file; our keyword is passed per stream instead,
        # so the file can stay empty.
        empty = self.model_dir / "_no_default_keywords.txt"
        if not empty.exists():
            empty.write_text("", encoding="utf-8")
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(self._path("tokens")),
            encoder=str(self._path("encoder")),
            decoder=str(self._path("decoder")),
            joiner=str(self._path("joiner")),
            keywords_file=str(empty),
            num_threads=self.cfg.num_threads,
            provider="cpu",
        )
        self.reset()
        log.info("Wake phrase %r -> %s", self.phrase, self.keyword_line)

    def process(self, frame: np.ndarray) -> WakeDetection | None:
        kws, stream = self._kws, self._stream
        if kws is None or stream is None:
            raise RuntimeError("SherpaKeywordSpotter.load() was not called")
        stream.accept_waveform(SAMPLE_RATE, frame)
        self._stream_samples += len(frame)
        while kws.is_ready(stream):
            kws.decode_stream(stream)
            timestamps = self._take_result(kws, stream)
            if timestamps is not None:
                now = self._stream_samples / SAMPLE_RATE
                # Detection lands ~0.3 s after the phrase. Report how much of what followed
                # was already consumed, so a command said in the same breath isn't clipped.
                trailing = now - (timestamps[-1] + _LAST_TOKEN_S) if timestamps else _DEFAULT_TRAILING_S
                # A fresh stream: after reset_stream() the timestamps lose their time base.
                self.reset()
                return WakeDetection(phrase=self.phrase, trailing_s=min(max(trailing, 0.0), 1.0))
        return None

    @staticmethod
    def _take_result(kws, stream) -> list[float] | None:  # noqa: ANN001
        """Timestamps of the detected keyword's tokens, or None. Consumes the result."""
        inner = getattr(kws, "keyword_spotter", None)
        if inner is not None:
            result = inner.get_result(stream)
            return list(result.timestamps) if result.keyword else None
        return [] if kws.get_result(stream) else None

    def reset(self) -> None:
        if self._kws is not None:
            # A fresh stream drops audio buffered from before (e.g. our own speech).
            self._stream = self._kws.create_stream(self.keyword_line)
            self._stream_samples = 0
