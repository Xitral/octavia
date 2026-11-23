"""The "ether" sound pack: her sounds as an old console's menu might have played them, soft and far away.

The same cues as the falcon pack, note for note where it has notes, so what you've learned by ear still holds, but
re-skinned: glass chimes with a slow shimmer instead of pings, soft breaths of air, and a faint pad under the few that
open or close something. Then every sound goes through one chain (``ps2``), so they read as one family: pitched down a
touch the way a sample played back slower is, warmed by gentle saturation, put in a long soft hall, and dulled at the
top the way a console's sound chip dulled everything. It's made from scratch: no recorded or borrowed sounds.

All synthesized with numpy at start-up, from seeded noise, so the sounds are the same every time.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from octavia.audio.earcons import _fade, _loop, _note
from octavia.audio.falcon_sounds import _mix, swoosh, thump, tick

# How loud each sound is, relative to a standard cue (``loudness``): each about 2 dB under the falcon's, to sit back.
TIERS: dict[str, float] = {
    # the ones that come often, or while you're at something else: a touch, no more
    "tap": 0.06,
    "panel_update": 0.12,
    "panel_close": 0.22,
    "panel_open": 0.34,
    "thinking": 0.34,
    "shutter": 0.4,
    "building": 0.26,
    "message": 0.34,
    "build_failed": 0.45,
    "sleep": 0.55,
    "muted": 0.6,
    "unmuted": 0.6,
    "declined": 0.62,
    "wake": 0.72,
    "stopped": 0.72,
    "approved": 0.78,
    "complete": 0.88,
    "notify": 0.9,
    "online": 0.95,
    "ask": 0.85,
    "error": 0.9,
    # the one you mustn't miss
    "timer": 1.15,
}
# A standard cue's loudness per unit of ``audio.earcon_volume``: the RMS of its loudest 400 ms, not 30 ms as the
# falcon's, so the hall's ringing counts too. Set so a cue comes out about 2 dB under the falcon's same cue.
STANDARD = 0.21
MOMENT_S = 0.4
PEAK = 1.36  # and no sound peaks above this per unit of it
LOOP_LEVEL = 0.2  # the working loop, relative to a standard cue: under everything, never over her voice
LONGEST_S = 1.95  # no sound, tail and all, runs longer than this

DROP = -0.4  # semitones: every sound a touch lower and slower, as a sample played back on an old console
CUTOFF = 6500.0  # Hz: the top dulled away above this
DRIVE = 1.4  # how hard the saturation leans on the loudest part
SHIMMER = 0.18  # how much of each sound an octave up rings in the hall: the "far away, glowing" part


def glass(
    freq: float,
    ms: float,
    sr: int,
    amp: float = 1.0,
    decay: float = 7.0,
    shimmer: float = 0.25,
    bend: float = 0.0,
    attack_ms: float = 7.0,
) -> np.ndarray:
    """A glass chime: a soft-edged sine with a slowly beating twin, an octave and a bell overtone (2.76 times the
    pitch, ``shimmer`` of it) that fade faster than the note, and a faint slow wobble once it rings. ``bend`` slides
    it that many semitones over its first 80 ms."""
    n = int(sr * ms / 1000)
    t = np.arange(n, dtype=np.float64) / sr
    top = freq * 2 ** (max(bend, 0.0) / 12)
    if top * 2.756 >= sr / 2:  # an overtone the sample rate can't hold would fold back down as a sour note
        shimmer = 0.0
    octave = 0.18 if top * 2 < sr / 2 else 0.0
    wobble = 1 + 0.0012 * np.sin(2 * np.pi * 4.6 * t) * np.clip((t - 0.06) / 0.2, 0.0, 1.0)
    f = freq * wobble
    if bend:
        f = f * 2 ** (bend * (1 - np.exp(-t / 0.03)) / 12)
    phase = 2 * np.pi * np.cumsum(f) / sr
    x = np.sin(phase) + 0.5 * np.sin(phase * 1.0024 + 1.1)  # the twin beats slowly against it
    x += octave * np.exp(-decay * 1.6 * t) * np.sin(phase * 2)
    x += shimmer * np.exp(-decay * 2.2 * t) * np.sin(phase * 2.756)
    x *= np.exp(-decay * t) / (1.5 + octave + shimmer)
    return _fade((x * amp).astype(np.float32), sr, fade_in_ms=attack_ms, fade_out_ms=25.0).astype(np.float32)


def pad(midis: tuple[float, ...], ms: float, sr: int, amp: float = 1.0, attack_ms: float = 120.0) -> np.ndarray:
    """A faint, soft chord that swells in over ``attack_ms`` and fades away over the rest: three slightly detuned
    voices to each note, with only a few soft harmonics (the chain's dull top takes what's left)."""
    n = int(sr * ms / 1000)
    t = np.arange(n, dtype=np.float64) / sr
    x = np.zeros(n)
    for k, m in enumerate(midis):
        for v, cents in enumerate((-7.0, 0.0, 6.0)):
            f = _note(m) * 2 ** (cents / 1200)
            phase = 2 * np.pi * f * t + 0.7 * (k * 3 + v)
            x += np.sin(phase) + 0.3 * np.sin(2 * phase) + 0.1 * np.sin(3 * phase)
    x /= 1.4 * 3 * max(1, len(midis))
    a = max(1, int(sr * attack_ms / 1000))
    env = np.ones(n)
    env[:a] = np.sin(np.linspace(0, np.pi / 2, min(a, n))) ** 2
    rest = n - a
    if rest > 0:
        env[a:] = np.cos(np.linspace(0, np.pi / 2, rest)) ** 2
    return (x * env * amp).astype(np.float32)


def breath(start: float, end: float, ms: float, sr: int, amp: float = 1.0, peak: float = 0.6, seed: int = 0):
    """A soft breath of air: a wide, slow band of noise sliding from ``start`` to ``end`` Hz."""
    return swoosh(start, end, ms, sr, amp, width=0.85, peak=peak, seed=seed)


def loudness(x: np.ndarray, sr: int) -> float:
    """How loud a sound is, ringing and all: the RMS of its loudest ``MOMENT_S``."""
    w = max(1, int(sr * MOMENT_S))
    if len(x) <= w:
        return float(np.sqrt(np.sum(x.astype(np.float64) ** 2) / w)) if len(x) else 0.0
    sums = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    return float(np.sqrt(max(0.0, float(np.max(sums[w:] - sums[:-w])) / w)))


def level(x: np.ndarray, sr: int, target: float, peak: float) -> np.ndarray:
    """``x`` scaled to ``target`` loudness, or less where that would peak above ``peak``."""
    now = loudness(x, sr)
    top = float(np.max(np.abs(x))) if len(x) else 0.0
    if not now or not top:
        return x.astype(np.float32)
    return (x * min(target / now, peak / top)).astype(np.float32)


@lru_cache(maxsize=16)
def _hall(sr: int, seconds: float, seed: int) -> np.ndarray:
    """A long, soft hall's echo: a few early reflections, then noise dying away by 60 dB over ``seconds``, its
    brightness going much sooner than its body, after a short gap so the note itself stays clear."""
    n = int(sr * seconds)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / sr
    body = np.convolve(rng.standard_normal(n), np.ones(12) / 12, mode="same") * np.exp(-6.9 * t / seconds)
    bright = rng.standard_normal(n) * np.exp(-6.9 * t / (seconds * 0.25))
    ir = body * 1.6 + bright * 0.25
    ir *= np.clip(t / 0.06, 0.0, 1.0) ** 1.5  # the tail blooms in, softly, rather than slapping back
    reflection = np.hanning(max(3, int(sr * 0.0006)))  # the same width whatever the sample rate
    reflection /= np.sqrt(np.sum(reflection**2) * 48000 / sr)
    for delay_ms, gain in ((23, 0.5), (37, -0.35), (53, 0.3), (71, -0.22)):  # the nearest walls
        start = int(sr * delay_ms / 1000)
        piece = reflection[: max(0, n - start)]
        ir[start : start + len(piece)] += gain * 4.0 * piece
    ir[: int(sr * 0.018)] = 0.0
    ir /= np.sqrt(np.sum(ir**2))
    ir.flags.writeable = False  # shared between sounds (cached)
    return ir


@lru_cache(maxsize=32)
def _filter_gain(n_fft: int, sr: int, cutoff: float) -> np.ndarray:
    """A gentle low-pass (a second order's slope above ``cutoff``) and a low cut under 70 Hz, as FFT bin gains."""
    f = np.fft.rfftfreq(n_fft, 1 / sr)
    low = 1 / np.sqrt(1 + (f / cutoff) ** 4)
    high = (f / 70.0) ** 2 / np.sqrt(1 + (f / 70.0) ** 4)
    gain = low * high
    gain.flags.writeable = False  # shared between sounds (cached)
    return gain


def _octave_up(x: np.ndarray, grain: int = 2048) -> np.ndarray:
    """``x`` an octave up and just as long, every note where it was: short overlapping grains, each read at double
    speed, so (unlike ``_pitch``) nothing plays early."""
    n = len(x)
    if not n:
        return x
    hop = grain // 4
    window = np.hanning(grain)
    padded = np.concatenate([np.zeros(grain), x, np.zeros(3 * grain)])
    out = np.zeros(n + 2 * grain)
    for p in range(0, n + grain, hop):  # p is where the grain starts in ``out`` (= in ``padded``, less the pad)
        out[p : p + grain] += padded[p + grain // 2 : p + grain // 2 + 2 * grain : 2] * window
    return out[grain : grain + n] / 2.0  # hann at a quarter hop sums to 2


def _pitch(x: np.ndarray, semitones: float) -> np.ndarray:
    """``x`` played back slower (or quicker) so it sounds ``semitones`` higher, as a sampler would."""
    ratio = 2 ** (semitones / 12)
    if not semitones or not len(x):
        return x
    pos = np.arange(int(len(x) / ratio)) * ratio
    return np.interp(pos, np.arange(len(x)), x)


def _saturate(x: np.ndarray, drive: float) -> np.ndarray:
    """A gentle, warm saturation: the loudest part rounded off a little, the quiet parts untouched."""
    top = float(np.max(np.abs(x))) if len(x) else 0.0
    if not top or drive <= 0:
        return x
    return np.tanh(drive * x / top) / np.tanh(drive) * top


def ps2(
    x: np.ndarray,
    sr: int,
    tail: float = 2.2,
    wet: float = 0.4,
    drop: float = DROP,
    cutoff: float = CUTOFF,
    drive: float = DRIVE,
    shimmer: float = SHIMMER,
    seed: int = 7,
) -> np.ndarray:
    """Every one of the pack's sounds goes through this, so they read as one family: pitched ``drop`` semitones,
    warmed, put in a soft hall ringing ``tail`` seconds (``wet`` of it, with ``shimmer`` of the sound an octave up
    ringing in it too), then dulled above ``cutoff``. It ends where its echo has died away, and never runs past
    ``LONGEST_S``."""
    dry = _saturate(_pitch(np.asarray(x, dtype=np.float64), drop), drive)
    ir = _hall(sr, tail, seed)
    size = len(dry) + len(ir)
    n_fft = 1 << (size - 1).bit_length()  # a power of two: many times quicker than an odd length
    spectrum = np.fft.rfft(dry, n_fft)
    feed = spectrum + shimmer * np.fft.rfft(_octave_up(dry), n_fft) if shimmer else spectrum
    out = spectrum + wet * feed * np.fft.rfft(ir, n_fft)
    y = np.fft.irfft(out * _filter_gain(n_fft, sr, cutoff), n_fft)[:size]
    loud = np.flatnonzero(np.abs(y) > 0.003 * np.max(np.abs(y)))  # the echo's end: 50 dB down
    end = max(int(len(dry) * 0.5), int(loud[-1]) + 1 if len(loud) else 0)
    end = min(end, int(sr * LONGEST_S))
    return _fade(y[:end].astype(np.float32), sr, 1.5, min(220.0, end / sr * 250)).astype(np.float32)


def ps2_loop(x: np.ndarray, sr: int, tail: float, wet: float, seed: int = 7) -> np.ndarray:
    """As ``ps2``, for a loop: the pitch drop, warmth, hall and dull top all wrap round its end to its start."""
    x = np.asarray(x, dtype=np.float64)
    ratio = 2 ** (DROP / 12)
    n = int(round(len(x) / ratio))
    pos = np.arange(n) * (len(x) / n)
    dropped = np.interp(pos, np.arange(len(x) + 1), np.concatenate([x, x[:1]]))  # read round the seam too
    dry = _saturate(dropped, DRIVE)
    ir = _hall(sr, tail, seed)
    folded = np.zeros(n)
    for k in range(0, len(ir), n):
        piece = ir[k : k + n]
        folded[: len(piece)] += piece
    up = _octave_up(np.concatenate([dry, dry, dry]))[n : 2 * n]  # an octave up, round the seam too
    spectrum = np.fft.rfft(dry)
    feed = spectrum + SHIMMER * np.fft.rfft(up)
    out = (spectrum + wet * feed * np.fft.rfft(folded)) * _filter_gain(n, sr, CUTOFF)
    return np.fft.irfft(out, n).astype(np.float32)


def _raw(sr: int) -> dict[str, np.ndarray]:
    """The sounds before they're leveled: only their shapes and balance matter here."""
    N = _note  # noqa: N806
    G = glass  # noqa: N806
    return {
        # a breath rising into two quick rising chimes: "I'm listening"
        "wake": ps2(
            _mix(
                sr,
                420,
                [
                    (0, breath(700, 3800, 220, sr, 0.16, peak=0.8, seed=1)),
                    (100, G(N(81), 320, sr, 0.75, decay=10)),
                    (165, G(N(88), 420, sr, 1.0, decay=7)),
                ],
            ),
            sr,
            1.6,
            0.32,
        ),
        # two tiny glints: "got it, on it"
        "thinking": ps2(
            _mix(sr, 240, [(0, G(N(89), 160, sr, 0.7, decay=20)), (85, G(N(93), 200, sr, 0.8, decay=16))]),
            sr,
            1.2,
            0.28,
        ),
        # a soft glass tock for each click, typed text, key press and scroll: barely there
        "tap": ps2(
            _mix(sr, 60, [(0, tick(sr, 0.35, seed=3, noise=0.1)), (0, G(N(91), 70, sr, 0.8, 55, 0.1, attack_ms=1.5))]),
            sr,
            0.35,
            0.12,
            drive=0.8,
            shimmer=0.0,
        ),
        # a muffled snap and a soft catch: the screenshot shutter
        "shutter": ps2(
            _mix(
                sr,
                160,
                [
                    (0, tick(sr, 0.9, seed=11)),
                    (0, G(N(103), 70, sr, 0.22, decay=50, shimmer=0.0, attack_ms=1.0)),
                    (60, tick(sr, 0.55, seed=12)),
                    (60, thump(220, 60, sr, 0.4)),
                ],
            ),
            sr,
            0.15,
            0.1,
            shimmer=0.0,
        ),
        # a rise that lifts at its end, like a raised eyebrow: "I need your answer"
        "ask": ps2(
            _mix(sr, 460, [(0, G(N(86), 260, sr, 0.85, decay=11)), (120, G(N(91), 420, sr, 1.0, 6, bend=2))]),
            sr,
            1.8,
            0.34,
        ),
        # a bright fifth with a sparkle on top: "got your yes"
        "approved": ps2(
            _mix(
                sr,
                460,
                [
                    (0, G(N(86), 280, sr, 0.8, decay=10)),
                    (65, G(N(93), 440, sr, 1.0, decay=7)),
                    (65, G(N(98), 300, sr, 0.22, decay=12, shimmer=0.1)),
                ],
            ),
            sr,
            1.8,
            0.34,
        ),
        # a soft, muted step down: "got your no"
        "declined": ps2(
            _mix(
                sr,
                400,
                [(0, G(N(81), 220, sr, 0.9, 13, shimmer=0.05)), (110, G(N(77), 360, sr, 1.0, 8, shimmer=0.05))],
            ),
            sr,
            1.4,
            0.26,
            cutoff=5000.0,
        ),
        # a breath falling, then a D major arpeggio that rings out: "that task is done"
        "complete": ps2(
            _mix(
                sr,
                1000,
                [
                    (0, breath(3500, 1100, 200, sr, 0.12, peak=0.4, seed=4)),
                    *((120 + i * 60, G(N(m), 420, sr, 0.72, decay=9)) for i, m in enumerate((74, 81, 86))),
                    (300, G(N(90), 800, sr, 0.95, decay=3.8)),
                    (300, G(N(98), 420, sr, 0.16, decay=8, shimmer=0.1)),
                ],
            ),
            sr,
            2.2,
            0.4,
        ),
        # a breath of air, then three glints over a faint chord: "news for you" (she speaks up on her own)
        "notify": ps2(
            _mix(
                sr,
                700,
                [
                    (0, breath(600, 3000, 180, sr, 0.14, peak=0.85, seed=5)),
                    (40, pad((74, 81, 88), 700, sr, 0.09, attack_ms=200)),
                    *((120 + i * 90, G(N(m), 400, sr, 0.85 + 0.05 * i, decay=7.5)) for i, m in enumerate((88, 93, 98))),
                ],
            ),
            sr,
            2.2,
            0.38,
        ),
        # a breath settling down with two falling chimes over a fading chord: "I stopped listening"
        "sleep": ps2(
            _mix(
                sr,
                700,
                [
                    (0, breath(2600, 500, 380, sr, 0.11, peak=0.3, seed=6)),
                    (0, pad((62, 69, 74), 700, sr, 0.08, attack_ms=60)),
                    (50, G(N(81), 300, sr, 0.85, decay=9, shimmer=0.15)),
                    (200, G(N(74), 480, sr, 1.0, decay=5.5, shimmer=0.15)),
                ],
            ),
            sr,
            2.0,
            0.36,
        ),
        # a quick drop onto a soft knock: "stopped"
        "stopped": ps2(
            _mix(
                sr,
                280,
                [(0, G(N(86), 220, sr, 1.0, decay=10, shimmer=0.1, bend=-12)), (130, thump(110, 120, sr, 0.8))],
            ),
            sr,
            0.9,
            0.2,
        ),
        # two low, uneasy notes a tritone apart: "something went wrong"
        "error": ps2(
            _mix(
                sr,
                500,
                [
                    (0, G(N(68), 240, sr, 1.0, decay=8, shimmer=0.45)),
                    (0, G(N(56), 240, sr, 0.5, decay=8, shimmer=0.0)),
                    (160, G(N(62), 380, sr, 1.0, decay=5, shimmer=0.45)),
                    (160, G(N(50), 380, sr, 0.5, decay=5, shimmer=0.0)),
                ],
            ),
            sr,
            1.3,
            0.24,
            cutoff=5000.0,
        ),
        # three bright two-note rings: "your timer is done"
        "timer": ps2(
            _mix(
                sr,
                1300,
                [
                    part
                    for start in (0, 400, 800)
                    for part in ((start, G(N(93), 240, sr, 0.85, 9)), (start + 120, G(N(98), 380, sr, 1.0, 7)))
                ],
            ),
            sr,
            1.0,
            0.22,
            cutoff=8000.0,  # the one you mustn't miss keeps a little more of its edge
        ),
        # a breath rising into a D major chord, struck string by string, over a soft swell: "I'm up and listening"
        "online": ps2(
            _mix(
                sr,
                1300,
                [
                    (0, breath(300, 5000, 460, sr, 0.18, peak=0.85, seed=8)),
                    (180, pad((62, 69, 76), 1100, sr, 0.12, attack_ms=320)),
                    *((360 + i * 34, G(N(m), 940, sr, 0.58, decay=3.6)) for i, m in enumerate((62, 69, 74, 78, 81))),
                    (500, G(N(93), 500, sr, 0.14, decay=6, shimmer=0.1)),
                ],
            ),
            sr,
            2.2,
            0.38,
        ),
        # a closing step down: "listening is off"
        "muted": ps2(
            _mix(sr, 330, [(0, G(N(86), 200, sr, 0.9, 14, 0.1)), (90, G(N(81), 300, sr, 1.0, 10, 0.1))]),
            sr,
            1.2,
            0.26,
        ),
        # a single drop that slides up: "her reply is in the chat window" (when she isn't saying it)
        "message": ps2(G(N(86), 300, sr, 1.0, decay=11, shimmer=0.1, bend=5), sr, 1.2, 0.28),
        # a faint breath of air and a low chime: "she's started building an improvement to herself"
        "building": ps2(
            _mix(
                sr,
                700,
                [
                    (0, breath(350, 1600, 420, sr, 0.45, peak=0.7, seed=9)),
                    (250, G(N(74), 480, sr, 0.8, decay=5.5, shimmer=0.0)),
                ],
            ),
            sr,
            1.8,
            0.34,
        ),
        # two soft low notes falling: "that build didn't work out"
        "build_failed": ps2(
            _mix(
                sr,
                520,
                [(0, G(N(74), 260, sr, 0.9, 9, shimmer=0.0)), (150, G(N(69), 400, sr, 1.0, 6, shimmer=0.0))],
            ),
            sr,
            1.4,
            0.26,
            cutoff=5000.0,
        ),
        # a soft knock and a breath of air under three rising glints over a faint chord: "a panel opens"
        "panel_open": ps2(
            _mix(
                sr,
                480,
                [
                    (0, thump(N(62), 80, sr, 0.3)),
                    (0, breath(1000, 3600, 170, sr, 0.09, peak=0.6, seed=21)),
                    (10, pad((74, 81), 460, sr, 0.06, attack_ms=90)),
                    (20, G(N(81), 240, sr, 0.6, decay=12, shimmer=0.3)),
                    (75, G(N(86), 260, sr, 0.7, decay=11, shimmer=0.3)),
                    (130, G(N(90), 380, sr, 0.85, decay=7, shimmer=0.35)),
                ],
            ),
            sr,
            1.6,
            0.32,
        ),
        # one small crystal tink with a fainter one above it: "the panel changed"
        "panel_update": ps2(
            _mix(sr, 220, [(0, G(N(93), 170, sr, 0.9, decay=17, shimmer=0.25)), (45, G(N(98), 150, sr, 0.35, 20))]),
            sr,
            0.9,
            0.22,
        ),
        # two soft glints falling, the air going out under them: "the panel closed"
        "panel_close": ps2(
            _mix(
                sr,
                380,
                [
                    (0, breath(2600, 800, 200, sr, 0.07, peak=0.4, seed=22)),
                    (0, G(N(86), 200, sr, 0.7, decay=13, shimmer=0.2)),
                    (75, G(N(81), 280, sr, 0.6, decay=10, shimmer=0.15)),
                ],
            ),
            sr,
            1.3,
            0.28,
        ),
        # the same step back up: "listening again"
        "unmuted": ps2(
            _mix(sr, 330, [(0, G(N(81), 200, sr, 0.9, 14, 0.1)), (90, G(N(86), 300, sr, 1.0, 10, 0.1))]),
            sr,
            1.2,
            0.26,
        ),
    }


def _working_loop(sr: int) -> np.ndarray:
    """A sparse, slow drift of glass glints over a low breath, about two and a half seconds round: "thinking".

    The falcon's pattern (D minor, then C, in the upper register), slower and softer, every glint ringing on into a
    long hall that wraps round the loop's end, over a D and A drone that swells and settles once per loop.
    """
    step = 150.0  # ms
    pattern = (86, None, 81, None, 89, None, 81, 93, 84, None, 79, None, 88, None, 79, 91)
    total = len(pattern) * step
    n = int(sr * total / 1000)
    parts = [
        (i * step, glass(_note(m), 800, sr, 1.0 if i % 8 == 0 else 0.6, decay=6, shimmer=0.2, attack_ms=12.0))
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
    return ps2_loop(glints + 0.16 * drone, sr, 2.0, 0.5)


def build(sr: int, volume: float) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """The pack's sounds and loops, at ``volume`` (``audio.earcon_volume``)."""
    sounds = {
        name: level(x, sr, STANDARD * volume * TIERS.get(name, 1.0), PEAK * volume) for name, x in _raw(sr).items()
    }
    loop = _working_loop(sr)
    loops = {"working": level(loop, sr, STANDARD * volume * LOOP_LEVEL, 0.5 * volume)}
    return sounds, loops
