"""Wake-word engines. ``create_wake`` picks one by ``wake.engine``.

* ``sherpa_kws``: any phrase from config, no training (default).
* ``openwakeword``: a model you trained for your phrase (optional extra).
* ``keyboard``: press Enter; for development.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.setup_models import Asset
    from octavia.wake.base import WakeWordDetector


def create_wake(config: AppConfig) -> WakeWordDetector:
    engine = config.wake.engine
    phrase = config.assistant.wake_phrase
    if engine == "sherpa_kws":
        from octavia.wake.sherpa_kws import SherpaKeywordSpotter

        return SherpaKeywordSpotter(
            phrase, config.wake.sherpa_kws, config.resolve_path(config.wake.sherpa_kws.model_dir)
        )
    if engine == "openwakeword":
        from octavia.wake.openwakeword_detector import OpenWakeWordDetector

        return OpenWakeWordDetector(phrase, config.wake.openwakeword, config)
    if engine == "keyboard":
        from octavia.wake.keyboard import KeyboardWake

        return KeyboardWake(phrase)
    raise ValueError(f"Unknown wake.engine {engine!r}")


def required_assets(config: AppConfig) -> list[Asset]:
    from octavia.setup_models import Asset

    assets: list[Asset] = []
    # The keyword spotter is small (18 MB); fetch it even when another engine is
    # selected, since it's also the fallback.
    from octavia.wake.sherpa_kws import MODEL_FILES, MODEL_SHA256, MODEL_URL

    assets.append(
        Asset(
            "Wake-word model (sherpa-onnx KWS)",
            MODEL_URL,
            config.wake.sherpa_kws.model_dir,
            sha256=MODEL_SHA256,
            archive="tar.bz2",
            marker=MODEL_FILES["encoder"],
        )
    )
    if config.wake.engine == "openwakeword":
        from octavia.wake.openwakeword_detector import feature_assets

        assets += feature_assets(config)
    return assets
