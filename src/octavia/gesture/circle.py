"""Telling a deliberate circle from everything else a mouse does, from its positions alone.

Pure: it's fed (time, x, y) and says when a loop has just closed. What it looks for is a path that turns one way all
the way round something, as a person draws one: an oval or a rounded box counts (text is wide), a wobble is fine. What
it must not take for one: going back and forth (hunting for the pointer, scrubbing), crossing the screen on a curve, a
scribble, tiny jitter, a figure of eight, or one slow turn over several seconds of reading.

A loop is found by walking back from the newest point until the path has turned a full circle; only that stretch is
judged, so whatever the mouse did on the way there doesn't count against it.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

Rect = tuple[int, int, int, int]  # left, top, right, bottom

MIN_STEP_PX = 6.0  # closer points are jitter (and a turn measured over them is noise)
WINDOW_S = 3.2  # the slowest loop that still reads as drawing one
MIN_LOOP_S = 0.18  # quicker is a flick or a shake; their own quick circles take about 0.25 s
MIN_SIDE_PX = 44  # the smallest thing someone circles is a word or an icon; tighter is fidgeting on the spot
MAX_SIDE_PX = 1500
MAX_ASPECT_WIDE = 5.0  # a line of text is about as wide as a circle round something gets
MAX_ASPECT_TALL = 2.6  # going down a page and back up isn't circling a column
TURN = 340.0  # degrees: a whole loop, give or take where the hand stopped
TURN_EAGER = 300.0  # ...while they're talking to her: a circle is far likelier then, and they may not finish it first
MIN_SAME_WAY = 0.86  # the share of the way round that went the loop's own way
MAX_RADIUS_SPREAD = 0.27  # how far the path strays from an even ring round its middle (scaled to its own box)
MAX_GAP = 0.9  # how far apart the loop's ends may be, with its box scaled to a ring of radius 1
MAX_GAP_EAGER = 1.2
MIN_SECTORS = 7  # of 8 slices of the way round: it went all the way round, not over one side twice
MIN_WOUND = 270.0  # degrees actually gone round its middle, with the backward bits taken off
MIN_WOUND_EAGER = 250.0
MAX_LENGTH_RATIO = 1.75  # how much longer than the ring round its box the path may be: more is a scribble
MAX_SHARP_TURN = 130.0  # the most it may turn in any tenth of its length: a hand drawing a loop has no hairpins...
MAX_SHARP_TURN_LONG = 172.0  # ...though the ends of a long oval come close
MAX_STALL_S = 0.25  # stopping part-way isn't drawing a circle
QUIET_S = 0.45  # no movement this long ends a gesture: the next circle is a new one


@dataclass(frozen=True)
class Loop:
    """A loop that just closed."""

    rect: Rect
    began: float
    ended: float
    path: tuple[tuple[int, int], ...]  # the positions it went through, for drawing on the picture
    fresh: bool  # a new circle; False: they went round again, so this only refines the one they've made


def _wrap(angle: float) -> float:
    """An angle difference in (-180, 180]."""
    return (angle + 180.0) % 360.0 - 180.0


def _hairpin(points: list[tuple[float, float, float]], length: float, aspect: float) -> bool:
    """Whether the path doubles back anywhere: too many degrees of turn in a tenth of its length, for its shape."""
    reach = length / 10
    limit = min(MAX_SHARP_TURN + (aspect - 1.0) * 20.0, MAX_SHARP_TURN_LONG)
    start = 0
    run = 0.0  # the length of steps start+1 ... i
    steps = [(p1[1] - p0[1], p1[2] - p0[2]) for p0, p1 in zip(points, points[1:], strict=False)]
    headings = [math.degrees(math.atan2(dy, dx)) for dx, dy in steps]
    sizes = [math.hypot(dx, dy) for dx, dy in steps]
    turned = 0.0
    for i in range(1, len(steps)):
        turned += _wrap(headings[i] - headings[i - 1])
        run += sizes[i]
        while run > reach and start < i - 1:
            start += 1
            turned -= _wrap(headings[start] - headings[start - 1])
            run -= sizes[start]
        if abs(turned) > limit:
            return True
    return False


def judge(points: list[tuple[float, float, float]], *, eager: bool = False) -> Rect | None:
    """The box of ``points`` (oldest first) if they make one deliberate loop, else None. ``eager``: they're talking to
    her, so a loop that hasn't quite closed is enough."""
    if len(points) < 6:
        return None
    if max(b[0] - a[0] for a, b in zip(points, points[1:], strict=False)) > MAX_STALL_S:
        return None
    duration = points[-1][0] - points[0][0]
    if not MIN_LOOP_S <= duration <= WINDOW_S:
        return None
    xs, ys = [p[1] for p in points], [p[2] for p in points]
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    width, height = right - left, bottom - top
    if min(width, height) < MIN_SIDE_PX or max(width, height) > MAX_SIDE_PX:
        return None
    if width / height > MAX_ASPECT_WIDE or height / width > MAX_ASPECT_TALL:
        return None
    mid_x, mid_y = (left + right) / 2, (top + bottom) / 2
    half_w, half_h = width / 2, height / 2
    # Everything from here on in the loop's own proportions, so an oval is judged as a circle is.
    unit = [((x - mid_x) / half_w, (y - mid_y) / half_h) for x, y in zip(xs, ys, strict=True)]
    radii = [math.hypot(x, y) for x, y in unit]
    mean = sum(radii) / len(radii)
    if mean <= 0 or math.sqrt(sum((r - mean) ** 2 for r in radii) / len(radii)) / mean > MAX_RADIUS_SPREAD:
        return None
    if math.hypot(unit[-1][0] - unit[0][0], unit[-1][1] - unit[0][1]) > (MAX_GAP_EAGER if eager else MAX_GAP):
        return None
    angles = [math.degrees(math.atan2(y, x)) for x, y in unit]
    onward = back = 0.0
    sectors = {int((angles[0] % 360) // 45)}
    for before, after in zip(angles, angles[1:], strict=False):
        step = _wrap(after - before)
        onward += max(step, 0.0)
        back += max(-step, 0.0)
        sectors.add(int((after % 360) // 45))
    wound = abs(onward - back)
    if len(sectors) < MIN_SECTORS or wound < (MIN_WOUND_EAGER if eager else MIN_WOUND):
        return None
    if max(onward, back) / (onward + back) < MIN_SAME_WAY:
        return None
    length = sum(math.hypot(q[1] - p[1], q[2] - p[2]) for p, q in zip(points, points[1:], strict=False))
    # Ramanujan's ring round an oval this size, for as much of the way round as it went.
    ring = math.pi * (3 * (half_w + half_h) - math.sqrt((3 * half_w + half_h) * (half_w + 3 * half_h)))
    if length > MAX_LENGTH_RATIO * ring * wound / 360.0 or _hairpin(
        points, length, max(width, height) / min(width, height)
    ):
        return None
    return round(left), round(top), round(right), round(bottom)


class CircleDetector:
    """Fed the pointer's positions; ``feed`` returns a ``Loop`` as one closes. A pressed button or a pointer that's
    hidden or held in place (a game looking around) is no circle: ``reset`` drops what was seen so far."""

    def __init__(self) -> None:
        self._points: deque[tuple[float, float, float]] = deque()
        self._drawing: Rect | None = None  # the circle being drawn: the pointer hasn't rested since it closed
        self._last_move = -math.inf

    def reset(self) -> None:
        self._points.clear()

    def feed(self, now: float, x: float, y: float, *, eager: bool = False) -> Loop | None:
        """A position at ``now`` (seconds). ``eager``: they're talking to her right now."""
        points = self._points
        if points and math.hypot(x - points[-1][1], y - points[-1][2]) < MIN_STEP_PX:
            return None
        if now - self._last_move > QUIET_S:
            points.clear()  # a rest: what came before isn't the same stroke
            self._drawing = None
        self._last_move = now
        points.append((now, x, y))
        while now - points[0][0] > WINDOW_S:
            points.popleft()
        loop = self._closing(TURN_EAGER if eager else TURN)
        if loop is None:
            return None
        rect = judge(loop, eager=eager)
        if rect is None:
            return None
        path = tuple((round(p[1]), round(p[2])) for p in loop)
        again = self._drawing is not None and _same_place(rect, self._drawing)
        self._drawing = rect
        # What's been judged is used up: going round again has to make a loop of its own.
        points.clear()
        points.append(loop[-1])
        return Loop(rect, loop[0][0], now, path, not again)

    def _closing(self, target: float) -> list[tuple[float, float, float]] | None:
        """The shortest stretch ending at the newest point whose direction has turned ``target`` degrees, net."""
        points = list(self._points)
        if len(points) < 6:
            return None
        turned = 0.0
        heading = None
        for i in range(len(points) - 1, 0, -1):
            dx, dy = points[i][1] - points[i - 1][1], points[i][2] - points[i - 1][2]
            this = math.degrees(math.atan2(dy, dx))
            if heading is not None:
                turned += _wrap(heading - this)
                if abs(turned) >= target:
                    return points[i - 1 :]
            heading = this
        return None


def _same_place(a: Rect, b: Rect) -> bool:
    """Whether two boxes cover much the same spot: their overlap is at least a third of the smaller."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return False
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return width * height >= smaller / 3
