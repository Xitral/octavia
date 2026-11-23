"""Short phrases in the assistant's own voice, ready to play instantly.

Some things are said often and never change: the "On it!" while the brain
works, the "Anytime!" after a thank-you. Each one is synthesized once through
the configured TTS voice and cached on disk, keyed by voice and text, so at
runtime it plays from memory with no network round-trip. New or edited
phrases are synthesized in the background at startup, and clips that nothing
uses any more are deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from octavia.audio import voice_effects
from octavia.audio.types import pcm16_to_float32
from octavia.audio.voice_volume import apply_gain
from octavia.core.speech import resample_linear
from octavia.tts.base import TTSProvider

log = logging.getLogger(__name__)


def trim_silence(audio: np.ndarray, sample_rate: int, floor_db: float = -45.0) -> np.ndarray:
    """Cut dead air around a clip (keeps 20 ms before and 80 ms after the sound) so it plays snappily."""
    floor = 10 ** (floor_db / 20)
    loud = np.flatnonzero(np.abs(audio) > floor)
    if loud.size == 0:
        return audio[:0]
    start = max(0, int(loud[0]) - int(0.02 * sample_rate))
    end = min(audio.size, int(loud[-1]) + int(0.08 * sample_rate))
    return audio[start:end]


def _clean(phrase: str) -> str:
    return " ".join(str(phrase).split())


class PhraseCache:
    """Audio for a fixed set of phrases. Register them with ``add``, then ``warm`` (or ``start_warming``)."""

    def __init__(self, tts: TTSProvider, cache_dir: Path, sample_rate: int, volume: float = 1.0) -> None:
        # Only the main voice is cached: with a backup voice, a phrase made while Fish was down would otherwise be
        # stored (and played for good) under Fish's voice key. A phrase that can't be made now is made next start.
        self.tts = tts.cacheable()
        self.cache_dir = cache_dir
        self.sample_rate = sample_rate  # the player's rate
        self.volume = volume  # applied as each phrase is fetched, so a change is heard on the next one
        self._effect = voice_effects.OFF  # her voice effect, applied as each phrase is fetched (``effect``)
        # (plain, volume, through the effect at that volume), made on first use
        self._effected: dict[str, tuple[np.ndarray, float, np.ndarray]] = {}
        self._phrases: dict[str, None] = {}  # ordered set: prepared in the order added
        self._audio: dict[str, np.ndarray] = {}
        self._warm_task: asyncio.Task | None = None

    def add(self, phrases: Iterable[str]) -> None:
        for phrase in phrases:
            if cleaned := _clean(phrase):
                self._phrases.setdefault(cleaned, None)

    def get(self, phrase: str) -> np.ndarray | None:
        """The phrase's audio at the player's rate, or None while it isn't ready (or couldn't be made)."""
        text = _clean(phrase)
        clip = self._audio.get(text)
        if clip is None:
            return None
        if self._effect == voice_effects.OFF:
            return apply_gain(clip, self.volume, self.sample_rate)
        made = self._effected.get(text)
        if made is None or made[0] is not clip or made[1] != self.volume:  # new, made again, or a new volume
            effected = voice_effects.apply(clip, self._effect, self.sample_rate)
            made = self._effected[text] = (clip, self.volume, apply_gain(effected, self.volume, self.sample_rate, True))
        return made[2]

    @property
    def effect(self) -> str:
        return self._effect

    @effect.setter
    def effect(self, name: str) -> None:
        """Her voice effect for the phrases fetched from now on (the cached clips stay plain)."""
        name = voice_effects.picked(name)
        if name != self._effect:
            self._effect, self._effected = name, {}

    @property
    def ready(self) -> int:
        return len(self._audio)

    # -- disk cache --------------------------------------------------------------
    def _cache_file(self, text: str) -> Path:
        key = hashlib.sha1(f"{self.tts.cache_key()}|{text}".encode()).hexdigest()[:20]
        return self.cache_dir / f"{key}.npy"

    def _load_cached(self) -> list[str]:
        """Load what's already on disk; return the phrases still missing."""
        missing = []
        for phrase in self._phrases:
            path = self._cache_file(phrase)
            if path.exists():
                try:
                    self._audio[phrase] = self._prepare(np.load(path))
                    continue
                except Exception:
                    log.warning("Unreadable cached phrase %s; re-creating it", path.name)
            missing.append(phrase)
        self._prune()
        return missing

    def _prune(self) -> None:
        """Delete cached clips that no current phrase uses (edited phrases, a previous voice)."""
        keep = {self._cache_file(p).name for p in self._phrases}
        for path in self.cache_dir.glob("*.npy"):
            if path.name not in keep:
                with contextlib.suppress(OSError):
                    path.unlink()

    def _prepare(self, clip: np.ndarray) -> np.ndarray:
        clip = np.asarray(clip, dtype=np.float32)
        rate = self.tts.format.sample_rate
        if rate != self.sample_rate:
            clip = resample_linear(clip, rate, self.sample_rate)
        return clip

    async def _synthesize(self, phrase: str) -> np.ndarray:
        chunks: list[bytes] = []
        async with contextlib.aclosing(self.tts.synthesize(phrase)) as stream:
            async for chunk in stream:
                chunks.append(chunk)
        audio = pcm16_to_float32(b"".join(chunks))
        return trim_silence(audio, self.tts.format.sample_rate)

    # -- warming -------------------------------------------------------------------
    async def warm(self) -> None:
        """Make every phrase playable: load the disk cache, synthesize what's missing (once)."""
        missing = await asyncio.to_thread(self._load_cached)
        if not missing:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        wait_ready = getattr(self.tts, "wait_ready", None)  # a local voice that's still loading
        if wait_ready is not None and not await wait_ready():
            log.warning("The voice didn't finish loading, so %d phrases weren't prepared", len(missing))
            return
        made = 0
        for phrase in missing:
            try:
                clip = await self._synthesize(phrase)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Couldn't prepare the phrase %r: %s", phrase, exc)
                break  # likely the same failure for the rest (no key, no credit, offline)
            if clip.size == 0:
                continue
            await asyncio.to_thread(np.save, self._cache_file(phrase), clip)
            self._audio[phrase] = self._prepare(clip)
            made += 1
        if made:
            log.info("Prepared %d phrases in the current voice", made)

    def start_warming(self) -> asyncio.Task:
        self._warm_task = asyncio.create_task(self.warm(), name="phrase-cache")
        return self._warm_task

    async def close(self) -> None:
        if self._warm_task is not None and not self._warm_task.done():
            self._warm_task.cancel()
            with contextlib.suppress(BaseException):
                await self._warm_task
