"""Directional audio: her voice and her sounds come from where she is on the screen.

The avatar app reports where she is across the main monitor (``{"type": "where", "x": ...}``, -1 at its left edge,
1 at its right, 0 in its middle; ``avatar/lib/where.js``), and the speaker (``audio/player.py``) plays everything
through a ``Panner``: her voice, her sound cues and her thinking loop, all mixed in mono as before, then spread over
the two channels. So with her in the bottom right corner (where the orb starts), she's a little to your right; flying
over to point at something on the left, her voice goes with her.

Subtle on purpose, and never louder than before:

* The side she's on stays at full level and only the far side is turned down (``gains``), by a cosine: at the
  screen's edge with ``audio.directional_width`` 0.5 the far ear is 3 dB down. In the middle both are at full level,
  exactly what a mono stream gave both ears, so nothing can clip that didn't before.
* A move glides (``GLIDE_S``), ramped sample by sample across each block, so she never jumps from ear to ear and a
  change of place can't click.
* Only level, no delay between the ears: nothing is held back, so it adds no latency, and a moving delay would
  bend her pitch.

What isn't panned: the copy the speaker hands its render tap (echo cancellation's reference and a Discord call) is
the mono mix as it was, so a call hears her as always.

``audio.directional`` off at startup opens the plain mono stream, exactly as before this existed. Turned off while
she runs, the stereo stream stays and she's simply put back in the middle (both channels the same, as mono was);
turned on while she runs on a mono stream, it waits for a restart (``app.py``'s live hooks).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np

DEFAULT_WIDTH = 0.5  # how far to the side she sounds at the screen's edge (0: never; 1: the far ear silent)
GLIDE_S = 0.15  # a move to a new place settles in about this long (a one-pole glide, per block)


def _clamp(x: float, low: float, high: float) -> float:
    return low if x < low else high if x > high else x


def gains(pan: float) -> tuple[float, float]:
    """(left, right) for ``pan`` (-1: all left, 0: middle, 1: all right): the near side at 1, the far side at
    cos(|pan| * pi/2), so the middle is (1, 1) and nothing is ever louder than the mono it comes from."""
    pan = _clamp(float(pan), -1.0, 1.0) if math.isfinite(pan) else 0.0
    far = math.cos(abs(pan) * math.pi / 2)
    return (far, 1.0) if pan > 0 else (1.0, far)


class Panner:
    """Spreads the speaker's mono blocks over two channels by where she is (``place``). ``place`` runs on the event
    loop, ``render`` on the audio thread: each reads or writes one float at a time, so no lock is needed."""

    def __init__(self, sample_rate: int, width: float = DEFAULT_WIDTH, glide_s: float = GLIDE_S) -> None:
        self.sample_rate = sample_rate
        self.width = width  # ``audio.directional_width``, changed live
        self.enabled = True  # ``audio.directional`` turned off while she runs: she's put back in the middle
        self.glide_s = glide_s
        self.where = 0.0  # across the main monitor, -1..1 (``place``)
        self._now = (1.0, 1.0)  # the gains the last block ended on

    def place(self, x: float | None) -> None:
        """She's at ``x`` across the main monitor (-1 its left edge, 1 its right); None: nowhere known (the avatar
        app isn't running), so the middle."""
        try:
            x = float(x) if x is not None else 0.0
        except (TypeError, ValueError):
            return
        self.where = _clamp(x, -1.0, 1.0) if math.isfinite(x) else 0.0

    def target(self) -> tuple[float, float]:
        """The gains she's gliding to."""
        if not self.enabled:
            return (1.0, 1.0)
        return gains(self.where * _clamp(float(self.width), 0.0, 1.0))

    def render(self, mono: np.ndarray, out: np.ndarray) -> None:
        """``mono`` (frames,) into ``out`` (frames, 2), gliding from the last block's gains toward ``target``."""
        frames = mono.size
        left, right = self._now
        goal_left, goal_right = self.target()
        if frames == 0:
            return
        step = 1.0 - math.exp(-frames / max(1.0, self.sample_rate * self.glide_s)) if self.glide_s > 0 else 1.0
        new_left = left + (goal_left - left) * step
        new_right = right + (goal_right - right) * step
        if abs(new_left - goal_left) < 1e-4 and abs(new_right - goal_right) < 1e-4:
            new_left, new_right = goal_left, goal_right  # close enough: land on it, so a still place is exact
        if new_left == left and new_right == right:
            out[:, 0] = mono * np.float32(left)
            out[:, 1] = mono * np.float32(right)
        else:
            ramp = np.arange(1, frames + 1, dtype=np.float32) / np.float32(frames)
            out[:, 0] = mono * (np.float32(left) + np.float32(new_left - left) * ramp)
            out[:, 1] = mono * (np.float32(right) + np.float32(new_right - right) * ramp)
        self._now = (new_left, new_right)


def live_hooks(player: Any) -> dict[str, Callable[[Any], bool | None]]:
    """``audio.directional`` and ``audio.directional_width`` changed while she runs (``core/live_settings.py``): on a
    stereo speaker at once; turning it on with a mono one (it was off at startup) waits for a restart."""
    panner = getattr(player, "spatial", None)
    stereo = panner is not None and getattr(player, "channels", 1) == 2

    def directional(on: bool) -> bool:
        if not stereo:
            return not on  # off is what a mono speaker already is; on needs the stereo stream a restart opens
        panner.enabled = bool(on)  # off: back in the middle, both channels the same, as mono was
        return True

    def width(value: float) -> None:
        if panner is not None:
            panner.width = float(value)

    return {"audio.directional": directional, "audio.directional_width": width}
