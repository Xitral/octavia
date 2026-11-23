"""What her subtitles show for a sentence she's speaking (docs/subtitles.md).

The avatar app draws them beside her; this decides the words. A sentence shows as written for the screen (its
[[shown|said]] marks as shown: ``tts/shown.py``), and not at all when a pop-up panel of hers already shows the same
thing: the formula she's walking through is on the panel, so a second copy beside her is only clutter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

from octavia.tts.shown import clean_for_screen, verbatim_parts

MAX_CHARS = 600  # a subtitle longer than this is cut (the app pages through what it gets)
_MIN_REPEAT_CHARS = 12  # a sentence shorter than this ("Okay.") on a panel isn't the panel's content
_NOISE = re.compile(r"[\s$`]+")


def _norm(text: str) -> str:
    return _NOISE.sub("", text).lower()


def strings(value: Any) -> Iterator[str]:
    """Every piece of text in a panel's spec (``Visual.model_dump()``), however deep. Not a live block's program
    (``code``): it runs on the panel, but its text isn't shown there."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key != "code":
                yield from strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from strings(item)


def on_panel(shown: str, panel_text: Iterable[str]) -> bool:
    """Whether a panel already shows this sentence's content: all its math and code, or the sentence itself."""
    corpus = "\n".join(_norm(t) for t in panel_text)
    if not corpus:
        return False
    parts = [p for p in (_norm(p) for p in verbatim_parts(shown)) if p]
    if parts:
        return all(p in corpus for p in parts)
    whole = _norm(shown).rstrip(".!?…,;:")
    return len(whole) >= _MIN_REPEAT_CHARS and whole in corpus


def subtitle(text: str, shown: str | None, panel_text: Iterable[str] = ()) -> str | None:
    """The subtitle for a sentence said as ``text`` (shown as ``shown`` when it says), or None to show nothing."""
    words = shown if shown else clean_for_screen(text)
    words = words.strip()
    if not words or on_panel(words, panel_text):
        return None
    return words[:MAX_CHARS]
