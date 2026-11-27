"""Her speech rhythm: pauses that vary with how a sentence ended, and a pace for each sentence.

Each piece of a reply is synthesized on its own and played back to back, so every join was the same ~0.25 to 0.3 s
of the voice's own trailing silence (measured 2026-11-25 on Fish: 35 to 80 ms in, 190 to 330 ms out), every piece
at one speed: the even, metronome timing the user named as the clearest robotic tell ("vary the speed and add
natural pauses, slowing for emphasis and breaking at commas and full stops"). People pause longer after a question
or a full stop than at a comma, and a short quip runs quicker than a careful sentence with a stressed word.

* ``pause_before`` is the silence wanted between the end of one piece and the start of the next, by how the piece
  before ended; ``SpeechStream`` adds only what the silence already there (the voice's own tail, a late piece) lacks,
  so a late piece is never made later, and the first words of a reply never wait on it.
* ``pace_for`` is a speed for a piece's voice: a little quicker for a short line, a little slower for a long one or
  one with a stressed word or a beat, and a small, repeatable wobble so no two run exactly alike.

Both are small on purpose (pace 0.94 to 1.06): past that a voice starts to sound sped up or dragged. D-042.
"""

from __future__ import annotations

import re
import zlib

# The silence wanted after a piece, in seconds, by how it ended, as (least, most); the choice within is repeatable
# per text. It includes the voice's own trailing silence (~0.2 to 0.3 s on Fish), so a comma adds nearly nothing.
PAUSES = {
    "comma": (0.18, 0.28),  # a sentence cut at a clause: barely a breath
    "full stop": (0.38, 0.6),
    "question": (0.5, 0.75),  # a question leaves room for it to land
    "exclamation": (0.32, 0.5),
    "ellipsis": (0.6, 0.85),
    "line": (0.6, 0.9),  # a new paragraph: a new thought
}
PACE_MIN, PACE_MAX = 0.94, 1.06
# The trailing silence the voice leaves on its own at the end of a piece (Fish: 190 to 330 ms, 2026-11-25), already
# part of the pause: ``SpeechStream`` adds only what's still missing.
VOICE_TAIL_S = 0.22

_TAG = re.compile(r"\[[^\[\]]{1,30}\]")
_WORD = re.compile(r"[A-Za-z0-9']+")


def _spread(text: str, low: float, high: float, salt: str = "") -> float:
    """A value between ``low`` and ``high`` that's the same every time for the same text."""
    unit = (zlib.crc32((salt + text).encode("utf-8")) % 1000) / 999
    return low + (high - low) * unit


def ending(text: str) -> str:
    """How a piece ended: a key of ``PAUSES``."""
    plain = _TAG.sub("", text).rstrip(" \t\"')]")
    if text.rstrip(" \t").endswith("\n"):
        return "line"
    if plain.endswith(("...", "…")):
        return "ellipsis"
    last = plain[-1:] if plain else "."
    return {"?": "question", "!": "exclamation", ",": "comma", ";": "comma", ":": "comma", "—": "comma"}.get(
        last, "full stop"
    )


def pause_before(previous: str) -> float:
    """The silence wanted between ``previous`` (the piece just said) and the next one, in seconds."""
    low, high = PAUSES[ending(previous)]
    return round(_spread(previous, low, high, "pause"), 3)


def pace_for(text: str) -> float:
    """A speed for this piece (1.0 = as configured)."""
    words = _WORD.findall(_TAG.sub(" ", text))
    n = len(words)
    pace = 1.0
    if n <= 6:
        pace = 1.04  # a quick line ("Fair point.", "Not quite.") runs quicker
    elif n >= 22:
        pace = 0.97  # a long careful one, slower
    if re.search(r"\[(?:emphasis|break|long-break|sigh|inhale)\]", text, re.IGNORECASE):
        pace -= 0.03  # a stressed word or a beat: slower, so it lands
    pace += _spread(text, -0.02, 0.02, "pace")
    return round(min(PACE_MAX, max(PACE_MIN, pace)), 3)
