"""Speech-to-text providers. ``create_stt`` picks one by ``stt.engine``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.setup_models import Asset
    from octavia.stt.base import SpeechToText


def create_stt(config: AppConfig) -> SpeechToText:
    engine = config.stt.engine
    if engine == "parakeet":
        from octavia.stt.parakeet import ParakeetSTT

        return ParakeetSTT(config.stt, config.resolve_path(config.stt.resolved_model_dir()))
    raise ValueError(f"Unknown stt.engine {engine!r}")


def required_assets(config: AppConfig) -> list[Asset]:
    return []  # Parakeet comes from Hugging Face; see prefetch()


def prefetch(config: AppConfig) -> bool:
    if config.stt.engine == "parakeet":
        from octavia.stt.parakeet import download

        return download(config.stt, config.resolve_path(config.stt.resolved_model_dir()))
    return True
