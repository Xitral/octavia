"""The "falcon" sound pack: sleek, dark and quick, like her.

Glassy pings (a sine with a bell's inharmonic overtone and a slightly detuned twin, so it shimmers), rushes of air
(noise swept through a moving band, a falcon's stoop), and a short airy room around them. Everything is in D: D minor
for the neutral and the dark, a major third (F sharp) only where something went well. Every sound is leveled to the
same loudness (``level``), scaled by how much it should stand out: a blip for each click stays far below a timer.

All synthesized with numpy at start-up, from seeded noise, so the sounds are the same every time.
"""

from __future__ import annotations

import numpy as np

from octavia.audio.earcons import _fade, _loop, _note

# How loud each sound is, relative to a standard cue (the loudness ``level`` gives them).
TIERS: dict[str, float] = {
    # the ones that come often, or while you're at something else: a touch, no more
    "tap": 0.13,
    "panel_update": 0.16,
    "panel_close": 0.26,
    "panel_open": 0.36,
    "thinking": 0.4,
    "shutter": 0.45,
    "building": 0.3,
    "message": 0.45,
    "build_failed": 0.5,
    "sleep": 0.6,
    "muted": 0.65,
    "unmuted": 0.65,
    "declined": 0.7,
    "wake": 0.8,
    "stopped": 0.8,
    "approved": 0.85,
    "complete": 0.85,
    "notify": 0.85,
    "online": 0.85,
    # the one you mustn't miss
    "timer": 1.2,
}
STANDARD = 0.55  # a standard cue's loudness (its loudest 30 ms, RMS) per unit of ``audio.earcon_volume``
PEAK = 1.36  # and no sound peaks above this per unit of it
LOOP_LEVEL = 0.22  # the working loop, relative to a standard cue: under everything, never over her voice


def ping(
    freq: float,
    ms: float,
    sr: int,
    amp: float = 1.0,
    decay: float = 10.0,
    shimmer: float = 0.3,
    bend: float = 0.0,
    attack_ms: float = 2.5,
) -> np.ndarray:
    """A glassy note. ``shimmer`` is its bell overtone (2.76 times the pitch, fading faster than the note);
    ``bend`` slides it that many semitones over its first 80 ms; ``attack_ms`` softens its start."""
    n = int(sr * ms / 1000)
    t = np.arange(n, dtype=np.float64) / sr
    top = freq * 2 ** (max(bend, 0.0) / 12)
    if top * 2.756 >= sr / 2:  # an overtone the sample rate can't hold would fold back down as a sour note
        shimmer = 0.0
    octave = 0.12 if top * 2 < sr / 2 else 0.0
    f = freq * 2 ** (bend * (1 - np.exp(-t / 0.03)) / 12) if bend else np.full(n, freq)
    phase = 2 * np.pi * np.cumsum(f) / sr
    x = np.sin(phase) + 0.45 * np.sin(phase * 1.0035)  # the twin beats slowly against it: a shimmer
    x += shimmer * np.exp(-decay * 1.8 * t) * np.sin(phase * 2.756)
    x += octave * np.exp(-decay * 1.3 * t) * np.sin(phase * 2)
    x *= np.exp(-decay * t) / (1.45 + shimmer + octave)
    return _fade((x * amp).astype(np.float32), sr, fade_in_ms=attack_ms, fade_out_ms=20.0).astype(np.float32)


def swoosh(
    start: float, end: float, ms: float, sr: int, amp: float = 1.0, width: float = 0.5, peak: float = 0.6, seed: int = 0
) -> np.ndarray:
    """A rush of air: noise in a band ``width`` octaves wide sliding from ``start`` to ``end`` Hz, swelling to its
    loudest ``peak`` of the way through."""
    n = int(sr * ms / 1000)
    nfft, hop = 1024, 256
    frames = n // hop + nfft // hop + 1
    rng = np.random.default_rng(seed)
    spectrum = rng.standard_normal((frames, nfft // 2 + 1)) + 1j * rng.standard_normal((frames, nfft // 2 + 1))
    centre = np.log2(start) + (np.log2(end) - np.log2(start)) * np.linspace(0, 1, frames)
    bins = np.log2(np.maximum(np.fft.rfftfreq(nfft, 1 / sr), 1.0))
    mask = np.exp(-0.5 * ((bins[None, :] - centre[:, None]) / width) ** 2)
    pieces = np.fft.irfft(spectrum * mask, nfft) * np.hanning(nfft)
    out = np.zeros(frames * hop + nfft)
    for i, piece in enumerate(pieces):
        out[i * hop : i * hop + nfft] += piece
    x = out[nfft // 2 : nfft // 2 + n]
    p = np.linspace(0, 1, n)
    env = np.where(p < peak, np.sin(np.pi / 2 * p / peak) ** 2, np.cos(np.pi / 2 * (p - peak) / (1 - peak)) ** 2)
    x = x * env
    top = float(np.max(np.abs(x))) if n else 0.0
    return _fade((x / (top or 1.0) * amp).astype(np.float32), sr, 2.0, 10.0).astype(np.float32)


def tick(sr: int, amp: float = 1.0, seed: int = 0, noise: float = 0.55) -> np.ndarray:
    """A crisp, tiny tick: a few milliseconds of bright noise (``noise`` of it) with a high glint."""
    n = int(sr * 0.018)
    x = np.diff(np.random.default_rng(seed).uniform(-1, 1, n + 1))  # differenced: only the bright end of it
    x *= np.exp(-np.arange(n) / sr * 420)
    x /= float(np.max(np.abs(x))) or 1.0
    t = np.arange(n) / sr
    x = noise * x + np.sin(2 * np.pi * 2637.0 * t) * np.exp(-t * 260)
    return _fade((x * amp).astype(np.float32), sr, 0.3, 4.0).astype(np.float32)


def thump(freq: float, ms: float, sr: int, amp: float = 1.0) -> np.ndarray:
    """A soft low knock, felt more than heard: a sine falling an octave as it dies away."""
    n = int(sr * ms / 1000)
    t = np.arange(n) / sr
    phase = 2 * np.pi * np.cumsum(freq * 2 ** (-t / (ms / 1000))) / sr
    return _fade((np.sin(phase) * np.exp(-t * 30) * amp).astype(np.float32), sr, 1.0, 15.0).astype(np.float32)


def _mix(sr: int, total_ms: float, parts: list[tuple[float, np.ndarray]]) -> np.ndarray:
    """``parts`` (an offset in ms, a sound) laid together, at least ``total_ms`` long: never cutting one short."""
    n = max([int(sr * total_ms / 1000), *(int(sr * offset_ms / 1000) + len(part) for offset_ms, part in parts)])
    out = np.zeros(n, dtype=np.float64)
    for offset_ms, part in parts:  # summed as they are: ``level`` brings the whole down, so nothing is clipped
        start = int(sr * offset_ms / 1000)
        out[start : start + len(part)] += part
    return out


def _impulse(sr: int, seconds: float, seed: int) -> np.ndarray:
    """A room's echo: noise dying away by 60 dB over ``seconds``, its brightest part going first."""
    n = int(sr * seconds)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / sr
    bright = rng.standard_normal(n) * np.exp(-6.9 * t / (seconds * 0.5))
    dark = np.convolve(rng.standard_normal(n), np.ones(8) / 8, mode="same") * np.exp(-6.9 * t / seconds)
    ir = bright * 0.5 + dark
    ir[: int(sr * 0.008)] = 0.0  # a little gap before the echo, so the note itself stays crisp
    return ir / np.sqrt(np.sum(ir**2))


def room(x: np.ndarray, sr: int, seconds: float = 0.5, mix: float = 0.3, seed: int = 7) -> np.ndarray:
    """``x`` in a small airy space, its echo ringing on past its end."""
    ir = _impulse(sr, seconds, seed)
    size = len(x) + len(ir)
    fft = 1 << (size - 1).bit_length()  # a power of two: many times quicker than an odd length
    wet = np.fft.irfft(np.fft.rfft(x, fft) * np.fft.rfft(ir, fft), fft)[:size]
    out = np.concatenate([x, np.zeros(len(ir))]) + mix * wet
    loud = np.flatnonzero(np.abs(out) > 0.002 * np.max(np.abs(out)))  # the echo's end: 54 dB down
    end = max(len(x), int(loud[-1]) + 1 if len(loud) else 0)
    return _fade(out[:end].astype(np.float32), sr, 0.0, 60.0).astype(np.float32)


def room_loop(x: np.ndarray, sr: int, seconds: float, mix: float, seed: int = 7) -> np.ndarray:
    """As ``room``, for a loop: the echo past its end wraps round to its start."""
    n = len(x)
    ir = _impulse(sr, seconds, seed)
    folded = np.zeros(n)
    for k in range(0, len(ir), n):
        piece = ir[k : k + n]
        folded[: len(piece)] += piece
    wet = np.fft.irfft(np.fft.rfft(x) * np.fft.rfft(folded), n)
    return (x + mix * wet).astype(np.float32)


def loudness(x: np.ndarray, sr: int) -> float:
    """How loud a sound is: the RMS of its loudest 30 ms."""
    w = max(1, int(sr * 0.03))
    if len(x) <= w:
        return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0
    sums = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    return float(np.sqrt(max(0.0, float(np.max(sums[w:] - sums[:-w])) / w)))


def level(x: np.ndarray, sr: int, target: float, peak: float) -> np.ndarray:
    """``x`` scaled to ``target`` loudness, or less where that would peak above ``peak``."""
    now = loudness(x, sr)
    top = float(np.max(np.abs(x))) if len(x) else 0.0
    if not now or not top:
        return x.astype(np.float32)
    gain = min(target / now, peak / top)
    return (x * gain).astype(np.float32)


def _raw(sr: int) -> dict[str, np.ndarray]:
    """The sounds before they're leveled: only their shapes and balance matter here."""
    N = _note  # noqa: N806
    return {
        # air rushing up into two quick rising pings: "I'm listening"
        "wake": room(
            _mix(
                sr,
                360,
                [
                    (0, swoosh(900, 5000, 170, sr, 0.18, width=0.6, peak=0.8, seed=1)),
                    (90, ping(N(81), 200, sr, 0.8, decay=16)),
                    (150, ping(N(88), 210, sr, 1.0, decay=12)),
                ],
            ),
            sr,
            0.35,
            0.22,
        ),
        # two tiny glints: "got it, on it" (when she has no voiced acknowledgment to give)
        "thinking": room(
            _mix(sr, 200, [(0, ping(N(89), 90, sr, 0.7, decay=30)), (80, ping(N(93), 110, sr, 0.8, decay=26))]),
            sr,
            0.3,
            0.2,
        ),
        # a crisp tick for each click, typed text, key press and scroll
        "tap": tick(sr, seed=3, noise=0.25),
        # a quick snap and a soft catch with a glint: the screenshot shutter
        "shutter": _mix(
            sr,
            140,
            [
                (0, tick(sr, 1.0, seed=11)),
                (0, ping(N(103), 60, sr, 0.25, decay=60, shimmer=0.0)),
                (55, tick(sr, 0.6, seed=12)),
                (55, thump(220, 50, sr, 0.35)),
            ],
        ),
        # a rise that lifts at its end, like a raised eyebrow: "I need your answer"
        "ask": room(
            _mix(sr, 380, [(0, ping(N(86), 170, sr, 0.85, decay=18)), (110, ping(N(91), 270, sr, 1.0, 9, bend=2))]),
            sr,
            0.4,
            0.22,
        ),
        # a bright fifth with a sparkle on top: "got your yes"
        "approved": room(
            _mix(
                sr,
                380,
                [
                    (0, ping(N(86), 180, sr, 0.8, decay=16)),
                    (60, ping(N(93), 300, sr, 1.0, decay=11)),
                    (60, ping(N(98), 200, sr, 0.25, decay=18, shimmer=0.1)),
                ],
            ),
            sr,
            0.4,
            0.22,
        ),
        # a soft, muted step down: "got your no"
        "declined": room(
            _mix(
                sr,
                340,
                [(0, ping(N(81), 140, sr, 0.9, 20, shimmer=0.05)), (100, ping(N(77), 240, sr, 1.0, 13, shimmer=0.05))],
            ),
            sr,
            0.3,
            0.15,
        ),
        # a dive, then a bright D major arpeggio that rings out: "that task is done"
        "complete": room(
            _mix(
                sr,
                900,
                [
                    (0, swoosh(4000, 1200, 160, sr, 0.14, width=0.5, peak=0.4, seed=4)),
                    *((110 + i * 55, ping(N(m), 300, sr, 0.75, decay=13)) for i, m in enumerate((74, 81, 86))),
                    (275, ping(N(90), 620, sr, 0.95, decay=5.5)),
                    (275, ping(N(98), 300, sr, 0.18, decay=12, shimmer=0.1)),
                ],
            ),
            sr,
            0.8,
            0.3,
        ),
        # a breath of air, then three glints: "news for you" (she speaks up on her own)
        "notify": room(
            _mix(
                sr,
                560,
                [
                    (0, swoosh(700, 3500, 140, sr, 0.15, width=0.5, peak=0.85, seed=5)),
                    *(
                        (110 + i * 85, ping(N(m), 280, sr, 0.85 + 0.05 * i, decay=11))
                        for i, m in enumerate((88, 93, 98))
                    ),
                ],
            ),
            sr,
            0.6,
            0.28,
        ),
        # air settling down with two falling pings: "I stopped listening"
        "sleep": room(
            _mix(
                sr,
                520,
                [
                    (0, swoosh(3000, 600, 300, sr, 0.12, width=0.6, peak=0.3, seed=6)),
                    (40, ping(N(81), 200, sr, 0.85, decay=14, shimmer=0.15)),
                    (180, ping(N(74), 320, sr, 1.0, decay=9, shimmer=0.15)),
                ],
            ),
            sr,
            0.5,
            0.22,
        ),
        # a quick drop onto a soft knock: "stopped"
        "stopped": _mix(
            sr,
            230,
            [(0, ping(N(86), 170, sr, 1.0, decay=14, shimmer=0.1, bend=-12)), (120, thump(110, 100, sr, 0.8))],
        ),
        # two low, uneasy notes a tritone apart: "something went wrong"
        "error": room(
            _mix(
                sr,
                420,
                [
                    (0, ping(N(68), 160, sr, 1.0, decay=12, shimmer=0.5)),
                    (0, ping(N(56), 160, sr, 0.5, decay=12, shimmer=0.0)),
                    (150, ping(N(62), 270, sr, 1.0, decay=8, shimmer=0.5)),
                    (150, ping(N(50), 270, sr, 0.5, decay=8, shimmer=0.0)),
                ],
            ),
            sr,
            0.35,
            0.15,
        ),
        # three bright two-note rings: "your timer is done"
        "timer": room(
            _mix(
                sr,
                1300,
                [
                    part
                    for start in (0, 400, 800)
                    for part in ((start, ping(N(93), 160, sr, 0.85, 12)), (start + 120, ping(N(98), 300, sr, 1.0, 9)))
                ],
            ),
            sr,
            0.4,
            0.2,
        ),
        # air rising into a D major chord, struck string by string: "I'm up and listening"
        "online": room(
            _mix(
                sr,
                1300,
                [
                    (0, swoosh(300, 6000, 420, sr, 0.22, width=0.7, peak=0.85, seed=8)),
                    *((330 + i * 30, ping(N(m), 950, sr, 0.6, decay=4.5)) for i, m in enumerate((62, 69, 74, 78, 81))),
                    (450, ping(N(93), 500, sr, 0.15, decay=8, shimmer=0.1)),
                ],
            ),
            sr,
            1.0,
            0.3,
        ),
        # a closing step down: "listening is off"
        "muted": room(
            _mix(sr, 280, [(0, ping(N(86), 130, sr, 0.9, 22, 0.1)), (85, ping(N(81), 200, sr, 1.0, 16, 0.1))]),
            sr,
            0.3,
            0.18,
        ),
        # a single drop that slides up: "her reply is in the chat window" (when she isn't saying it)
        "message": room(ping(N(86), 220, sr, 1.0, decay=18, shimmer=0.1, bend=5), sr, 0.3, 0.18),
        # a faint breath of air and a low glint: "she's started building an improvement to herself"
        "building": room(
            _mix(
                sr,
                600,
                [
                    (0, swoosh(400, 1800, 380, sr, 0.5, width=0.6, peak=0.7, seed=9)),
                    (230, ping(N(74), 360, sr, 0.8, decay=8, shimmer=0.0)),
                ],
            ),
            sr,
            0.5,
            0.25,
        ),
        # two soft low notes falling: "that build didn't work out"
        "build_failed": room(
            _mix(
                sr,
                450,
                [(0, ping(N(74), 180, sr, 0.9, 14, shimmer=0.0)), (140, ping(N(69), 300, sr, 1.0, 9, shimmer=0.0))],
            ),
            sr,
            0.35,
            0.18,
        ),
        # Her pop-up panels, in the spirit of a PS2 menu (a crystal sparkle like Kingdom Hearts' menus, a small wooden
        # knock like Dark Cloud's), made from scratch: a soft knock and a breath of air under three rising glints,
        # "a panel opens"
        "panel_open": room(
            _mix(
                sr,
                420,
                [
                    (0, thump(N(62), 70, sr, 0.35)),
                    (0, swoosh(1200, 4200, 130, sr, 0.1, width=0.5, peak=0.6, seed=21)),
                    (20, ping(N(81), 170, sr, 0.6, decay=18, shimmer=0.35)),
                    (70, ping(N(86), 180, sr, 0.7, decay=16, shimmer=0.35)),
                    (120, ping(N(90), 280, sr, 0.85, decay=10, shimmer=0.4)),
                ],
            ),
            sr,
            0.4,
            0.24,
        ),
        # one small crystal tink with a fainter one above it: "the panel changed"
        "panel_update": room(
            _mix(
                sr, 200, [(0, ping(N(93), 120, sr, 0.9, decay=26, shimmer=0.3)), (40, ping(N(98), 110, sr, 0.35, 30))]
            ),
            sr,
            0.3,
            0.2,
        ),
        # two soft glints falling, the air going out under them: "the panel closed"
        "panel_close": room(
            _mix(
                sr,
                320,
                [
                    (0, swoosh(3000, 900, 160, sr, 0.08, width=0.5, peak=0.4, seed=22)),
                    (0, ping(N(86), 140, sr, 0.7, decay=20, shimmer=0.2)),
                    (70, ping(N(81), 200, sr, 0.6, decay=14, shimmer=0.15)),
                ],
            ),
            sr,
            0.35,
            0.2,
        ),
        # the same step back up: "listening again"
        "unmuted": room(
            _mix(sr, 280, [(0, ping(N(81), 130, sr, 0.9, 22, 0.1)), (85, ping(N(86), 200, sr, 1.0, 16, 0.1))]),
            sr,
            0.3,
            0.18,
        ),
    }


def _working_loop(sr: int) -> np.ndarray:
    """A sparse, airy scan of glints over a low breath, about two seconds round: "thinking, working".

    D minor, then C, picked out in the upper register; every glint rings on into a long, soft room that wraps round
    the loop's end, over a D and A drone that swells and settles once per loop.
    """
    step = 140.0  # ms
    pattern = (86, None, 81, None, 89, None, 81, 93, 84, None, 79, None, 88, None, 79, 91)
    total = len(pattern) * step
    n = int(sr * total / 1000)
    parts = [
        (i * step, ping(_note(m), 600, sr, 1.0 if i % 8 == 0 else 0.65, decay=9, shimmer=0.25, attack_ms=6.0))
        for i, m in enumerate(pattern)
        if m is not None
    ]
    glints = _loop(sr, total, parts).astype(np.float64)
    seconds = n / sr
    t = np.arange(n) / sr
    drone = np.zeros(n)
    for freq, a in ((_note(50), 1.0), (_note(57), 0.6), (_note(62), 0.25)):
        f = round(freq * seconds) / seconds  # whole cycles per loop, so it wraps without a seam
        drone += a * np.sin(2 * np.pi * f * t)
    drone *= 0.55 + 0.45 * np.sin(np.pi * t / seconds) ** 2  # a breath per loop
    mixed = room_loop(glints, sr, 1.4, 0.45) + 0.16 * drone
    return mixed.astype(np.float32)


def build(sr: int, volume: float) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """The pack's sounds and loops, at ``volume`` (``audio.earcon_volume``)."""
    sounds = {
        name: level(x, sr, STANDARD * volume * TIERS.get(name, 1.0), PEAK * volume) for name, x in _raw(sr).items()
    }
    loop = _working_loop(sr)
    loops = {"working": level(loop, sr, STANDARD * volume * LOOP_LEVEL, 0.5 * volume)}
    return sounds, loops
