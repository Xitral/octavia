"""Speech-emotion awareness: how the user sounds, not only what they say (``emotion`` in the config).

Everything is local and nothing is kept: the utterance the endpointer captured goes through a small speech-emotion
model (``model.py``, emotion2vec+ base on onnxruntime) and a few numpy measurements of loudness, pitch and pace
against the user's own usual (``prosody.py``), and comes out as one cautious label (``tone.py``). The audio itself is
never saved; the baseline file (data/emotion/baseline.json) holds a few averages.

It's built to cost the reply nothing. ``core/hearing.py`` hands the utterance to the worker while the endpointer is
still waiting out the end-of-speech silence (``vad.end_silence_ms``, 0.7-0.9 s), so the reading is usually done
before the transcript; when it isn't, the turn waits at most ``emotion.max_wait_ms`` and then goes on without it.
Preset replies, quick commands and the acknowledgment never wait for it at all.

``create_voice_tone`` makes the feature (None when it's off or its model isn't downloaded); ``prefetch`` is its part
of `octavia setup`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.emotion.tone import VoiceTone

log = logging.getLogger(__name__)

BASELINE_FILE = "data/emotion/baseline.json"


def create_voice_tone(config: AppConfig) -> VoiceTone | None:
    """The feature for ``config``, not loaded yet (``VoiceTone.load``); None when it's off or the model is missing,
    with one line in the log saying how to get it."""
    cfg = config.emotion
    if not cfg.enabled:
        return None
    from octavia.emotion.model import EmotionModel
    from octavia.emotion.tone import VoiceTone

    model = EmotionModel(config.resolve_path(cfg.model_dir), cfg.num_threads, cfg.max_audio_s)
    if not model.files_present():
        log.info("Voice tone is off: its model isn't in %s (run `uv run octavia setup` to get it)", cfg.model_dir)
        return None
    return VoiceTone(cfg, model, config.resolve_path(BASELINE_FILE))


def prefetch(config: AppConfig) -> bool:
    """Download the speech-emotion model (`octavia setup`), unless the feature is off."""
    if not config.emotion.enabled:
        return True
    from octavia.emotion.model import download

    return download(config.resolve_path(config.emotion.model_dir))
