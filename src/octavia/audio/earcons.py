"""Short synthesized notification sounds (no audio files needed).

Tones use a few soft harmonics, exponential decay and raised-cosine fades so
they start and end on exactly zero and never click.
"""

from __future__ import annotations

import numpy as np


def _fade(x: np.ndarray, sr: int, fade_in_ms: float = 5.0, fade_out_ms: float = 20.0) -> np.ndarray:
    n_in = min(len(x), int(sr * fade_in_ms / 1000))
    n_out = min(len(x), int(sr * fade_out_ms / 1000))
    if n_in:
        x[:n_in] *= 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n_in, dtype=np.float32))
    if n_out:
        x[-n_out:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, n_out, dtype=np.float32))
    return x


def tone(
    freq: float,
    ms: float,
    sr: int,
    amp: float = 0.2,
    harmonics: tuple[float, ...] = (1.0, 0.25, 0.08),
    decay: float = 0.0,
) -> np.ndarray:
    t = np.arange(int(sr * ms / 1000), dtype=np.float32) / sr
    x = np.zeros_like(t)
    for k, a in enumerate(harmonics, start=1):
        x += a * np.sin(2 * np.pi * freq * k * t, dtype=np.float32)
    x /= sum(harmonics)
    if decay:
        x *= np.exp(-decay * t, dtype=np.float32)
    return _fade(x * amp, sr).astype(np.float32)


def click(ms: float, sr: int, amp: float = 0.2, decay: float = 90.0, smooth: int = 1, seed: int = 0) -> np.ndarray:
    """A burst of decaying noise, like a mechanism snapping. ``smooth`` (a moving average, in samples) dulls it.

    Seeded, so the sound is the same every time.
    """
    n = int(sr * ms / 1000)
    x = np.random.default_rng(seed).uniform(-1.0, 1.0, n).astype(np.float32)
    if smooth > 1:
        x = np.convolve(x, np.full(smooth, 1.0 / smooth, np.float32), mode="same").astype(np.float32)
    peak = float(np.max(np.abs(x))) if n else 0.0
    if peak:
        x /= peak
    x *= np.exp(-decay * np.arange(n, dtype=np.float32) / sr, dtype=np.float32)
    return _fade(x * amp, sr, fade_in_ms=0.5, fade_out_ms=5.0).astype(np.float32)


def bell(freq: float, ms: float, sr: int, amp: float = 0.2, decay: float = 9.0, brightness: float = 1.2) -> np.ndarray:
    """A soft music-box or celesta note: a sine whose overtones (a little FM) fade faster than the note itself."""
    t = np.arange(int(sr * ms / 1000), dtype=np.float32) / sr
    index = brightness * np.exp(-decay * 2.5 * t, dtype=np.float32)
    x = np.sin(2 * np.pi * freq * t + index * np.sin(2 * np.pi * freq * 2 * t, dtype=np.float32), dtype=np.float32)
    x *= np.exp(-decay * t, dtype=np.float32)
    return _fade(x * amp, sr, fade_in_ms=2.0, fade_out_ms=25.0).astype(np.float32)


def glide(start: float, end: float, ms: float, sr: int, amp: float = 0.2, decay: float = 6.0) -> np.ndarray:
    """A note sliding from ``start`` to ``end`` Hz (exponentially, as pitch is heard), with a soft second harmonic."""
    n = int(sr * ms / 1000)
    t = np.arange(n, dtype=np.float32) / sr
    freq = start * (end / start) ** (t / max(t[-1], 1e-6)) if n else t
    phase = 2 * np.pi * np.cumsum(freq, dtype=np.float64).astype(np.float32) / sr
    x = (np.sin(phase) + 0.2 * np.sin(2 * phase)) / 1.2 * np.exp(-decay * t, dtype=np.float32)
    return _fade(x.astype(np.float32) * amp, sr, fade_in_ms=3.0, fade_out_ms=30.0).astype(np.float32)


def _loop(sr: int, total_ms: float, parts: list[tuple[float, np.ndarray]]) -> np.ndarray:
    """Like ``_overlay``, but what rings past the end wraps round to the start, so the sound loops seamlessly."""
    n = int(sr * total_ms / 1000)
    out = np.zeros(n, dtype=np.float32)
    for offset_ms, part in parts:
        start = int(sr * offset_ms / 1000) % n
        for k in range(0, len(part), n):
            piece = part[k : k + n]
            first = min(len(piece), n - start)
            out[start : start + first] += piece[:first]
            out[: len(piece) - first] += piece[first:]
    return np.clip(out, -1.0, 1.0)


def _note(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _overlay(sr: int, total_ms: float, parts: list[tuple[float, np.ndarray]]) -> np.ndarray:
    out = np.zeros(int(sr * total_ms / 1000), dtype=np.float32)
    for offset_ms, part in parts:
        start = int(sr * offset_ms / 1000)
        end = min(len(out), start + len(part))
        out[start:end] += part[: end - start]
    return np.clip(out, -1.0, 1.0)


PACKS = ("ether", "falcon", "classic")  # ``audio.sound_pack``


class Earcons:
    """Pre-rendered sounds at the player's sample rate, from one of the packs (``PACKS``)."""

    def __init__(self, sample_rate: int, volume: float = 0.25, pack: str = "ether") -> None:
        volume = max(0.0, min(1.0, volume))
        if pack == "classic":
            self.sounds, self.loops = _classic(sample_rate, volume)
        elif pack == "ether":
            from octavia.audio.ether_sounds import build

            self.sounds, self.loops = build(sample_rate, volume)
        else:
            from octavia.audio.falcon_sounds import build

            self.sounds, self.loops = build(sample_rate, volume)

    def get(self, name: str) -> np.ndarray | None:
        return self.sounds.get(name)

    def loop(self, name: str) -> np.ndarray | None:
        return self.loops.get(name)


def _classic(sr: int, amp: float) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """The first pack: chimes, music-box bells and a soft low pulse."""
    sounds: dict[str, np.ndarray] = {
        # rising two-note chime: "I'm listening"
        "wake": _overlay(
            sr, 230, [(0, tone(1318.5, 90, sr, amp, decay=12)), (70, tone(1975.5, 140, sr, amp, decay=10))]
        ),
        # soft double tick: "got it, working on it"
        "thinking": _overlay(
            sr,
            210,
            [(0, tone(987.8, 70, sr, amp * 0.6, decay=25)), (110, tone(1174.7, 90, sr, amp * 0.6, decay=25))],
        ),
        # falling two-note chime: "done / going idle"
        "done": np.concatenate([tone(1975.5, 80, sr, amp * 0.8, decay=12), tone(1318.5, 130, sr, amp * 0.8, decay=10)]),
        # low buzzy pair: "something went wrong"
        "error": np.concatenate(
            [
                tone(440.0, 140, sr, amp, harmonics=(1.0, 0.5, 0.3, 0.15)),
                np.zeros(int(sr * 0.04), np.float32),
                tone(329.6, 220, sr, amp, harmonics=(1.0, 0.5, 0.3, 0.15), decay=4),
            ]
        ),
        # camera shutter, a bright snap then a duller clack: "taking a screenshot"
        "shutter": _overlay(
            sr,
            150,
            [
                (0, click(45, sr, amp, decay=110, seed=1)),
                (0, tone(2600.0, 25, sr, amp * 0.25, decay=150)),
                (65, click(70, sr, amp * 0.8, decay=70, smooth=max(1, sr // 6000), seed=2)),
            ],
        ),
        # three bright two-note rings: "your timer is done"
        "timer": _overlay(
            sr,
            1500,
            [
                part
                for start in (0, 450, 900)
                for part in (
                    (start, tone(1568.0, 140, sr, amp, decay=8)),
                    (start + 140, tone(2093.0, 260, sr, amp, decay=8)),
                )
            ],
        ),
        # a bright step up with a sparkle on top, like a menu choice: "got your yes"
        "approved": _overlay(
            sr,
            380,
            [
                (0, bell(_note(88), 160, sr, amp * 0.6, decay=14)),
                (70, bell(_note(93), 300, sr, amp * 0.7, decay=9)),
                (70, bell(_note(105), 200, sr, amp * 0.18, decay=16, brightness=0.6)),
            ],
        ),
        # a soft step down, like backing out of a menu: "got your no"
        "declined": _overlay(
            sr,
            330,
            [
                (0, tone(_note(79), 110, sr, amp * 0.75, harmonics=(1.0, 0.0, 0.3, 0.0, 0.1), decay=14)),
                (95, tone(_note(72), 230, sr, amp * 0.75, harmonics=(1.0, 0.0, 0.3, 0.0, 0.1), decay=10)),
            ],
        ),
        # a curious rise before a question: "I need your answer"
        "ask": _overlay(
            sr,
            360,
            [
                (0, bell(_note(81), 130, sr, amp * 0.7, decay=16)),
                (110, bell(_note(88), 250, sr, amp * 0.8, decay=10)),
            ],
        ),
        # a tiny cursor blip for each click, typed text, key press and scroll: "working the mouse and keys"
        "tap": tone(_note(91), 30, sr, amp * 0.3, harmonics=(1.0, 0.15), decay=120),
        # a quick rising arpeggio that rings out: "that task is done"
        "complete": _overlay(
            sr,
            900,
            [
                *((i * 75, bell(_note(m), 260, sr, amp * 0.55, decay=12)) for i, m in enumerate((84, 88, 91))),
                (225, bell(_note(96), 650, sr, amp * 0.65, decay=5)),
                (225, bell(_note(103), 400, sr, amp * 0.15, decay=9, brightness=0.5)),
            ],
        ),
        # two soft falling notes, slow: "I've stopped listening"
        "sleep": _overlay(
            sr,
            520,
            [
                (0, bell(_note(86), 220, sr, amp * 0.5, decay=9, brightness=0.6)),
                (160, bell(_note(79), 360, sr, amp * 0.45, decay=7, brightness=0.6)),
            ],
        ),
        # a quick slide down: "stopped"
        "stopped": glide(_note(84), _note(67), 200, sr, amp * 0.7, decay=8),
        # a three-note sparkle before she speaks up on her own: "news for you"
        "notify": _overlay(
            sr,
            600,
            [
                *((i * 90, bell(_note(m), 300, sr, amp * 0.7, decay=10)) for i, m in enumerate((88, 95, 100))),
                (180, bell(_note(112), 300, sr, amp * 0.12, decay=12, brightness=0.4)),
            ],
        ),
    }
    # Her pop-up panels: a rising music-box sparkle opening, one high note for a change, two falling closing.
    sounds["panel_open"] = _overlay(
        sr, 420, [(i * 55, bell(_note(m), 260, sr, amp * 0.35, decay=12)) for i, m in enumerate((86, 90, 93))]
    )
    sounds["panel_update"] = bell(_note(98), 140, sr, amp * 0.2, decay=20)
    sounds["panel_close"] = _overlay(
        sr,
        320,
        [(0, bell(_note(90), 160, sr, amp * 0.25, decay=16)), (70, bell(_note(86), 220, sr, amp * 0.22, decay=12))],
    )
    # It had no sounds of their own for these: muting sounds like going to sleep, unmuting like a yes.
    sounds["muted"] = sounds["sleep"].copy()
    sounds["unmuted"] = sounds["approved"].copy()
    sounds["message"] = sounds["done"].copy()
    # Loops, played under everything else for as long as a state lasts (``AudioSink.set_loop``).
    return sounds, {"working": _working_loop(sr, amp * 0.22)}


def _working_loop(sr: int, amp: float) -> np.ndarray:
    """A calm, airy music-box pattern over a soft low pulse, a few seconds long, that loops: "thinking, working".

    Two chords (F major 7, then E minor 7), each picked out gently in the upper register, with a round bass note at
    the start of each. Every note rings on into the next and past the loop's end, which wraps round to its start.
    """
    step = 170.0  # ms: a relaxed pace
    melody = (77, 81, 84, 88, 84, 81, 84, 81, 76, 79, 83, 86, 83, 79, 83, 79)
    parts: list[tuple[float, np.ndarray]] = []
    for i, m in enumerate(melody):
        accent = 1.0 if i % 4 == 0 else 0.7
        parts.append((i * step, bell(_note(m), 700, sr, amp * accent, decay=5.5, brightness=0.7)))
    for i, m in enumerate((53, 52)):
        parts.append((i * 8 * step, tone(_note(m), 1300, sr, amp * 0.9, harmonics=(1.0, 0.3), decay=2.5)))
    return _loop(sr, len(melody) * step, parts)
