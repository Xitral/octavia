"""Zooming into the latest screenshot, pointing at things on the screen with the avatar, and finding labelled
controls with Windows UI Automation.

``look_at_screen`` keeps what the model saw (``screen_capture.SCREEN``) for a few minutes. ``zoom_screen`` crops
that stored picture, so it needs no new capture and no new permission. ``point_at_screen`` turns a pixel position
in the screenshot or the zoom (or a ``find_on_screen`` match) into physical desktop pixels and has the avatar fly
over and point there. ``point_sequence`` does that for several things in order, saying a line for each while she
points at it (``ctx.extras["speak"]``), so each line goes with its spot. ``find_on_screen`` asks UI Automation for
things labelled with some text: exact rectangles, with no screenshot and no image for the model to read. It is
CONFIRM like a screenshot, since it reads what's on the screen, unless the user's own words just asked for it
(``octavia.tools.asked``). It looks past the avatar's own windows: she floats over everything, often right beside the
thing she's asked about next.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult, TurnInfo
from octavia.tools.extensions import _uia
from octavia.tools.extensions._uia import Match, UIAUnavailable, describe_where, rank
from octavia.tools.extensions.screen_capture import (
    SCREEN,
    Rect,
    ScreenMemory,
    Zoom,
    attachment,
    can_mark,
    monitor_rects,
    pointing_avatar,
    prepare_image,
)

log = logging.getLogger(__name__)

LABEL_CHARS = 60
MIN_ZOOM = 64  # screenshot pixels: a narrower region has too little in it to be worth a picture

NO_AVATAR = "The avatar isn't on, so describe where it is instead."
NO_SHOT = "I don't have a recent screenshot. Take one first with look_at_screen."

# --------------------------------------------------------------------------- zoom_screen


class ZoomScreenArgs(ToolArgs):
    x: int = Field(description="Centre of the region: pixels from the left of the latest screenshot.")
    y: int = Field(description="Centre of the region: pixels from the top of the latest screenshot.")
    size: int | None = Field(default=None, description="Region width in screenshot pixels (default: a quarter).")


class ZoomScreen(Tool):
    name = "zoom_screen"
    description = (
        "Look closer at part of the latest look_at_screen screenshot, at full screen detail, as a new picture. Use "
        "it to read small text, or before pointing at something small, crowded or among similar-looking things. "
        "Takes no new screenshot: it shows the screen as it was then."
    )
    Args = ZoomScreenArgs
    risk = RiskLevel.SAFE  # re-reads the picture the user already allowed; nothing new is captured
    category = "system"
    timeout_s = 15.0

    def __init__(self, memory: ScreenMemory | None = None) -> None:
        self.memory = memory if memory is not None else SCREEN

    async def describe_call(self, args: ZoomScreenArgs, ctx: ToolContext) -> str:
        return "zoom in on part of the screenshot"

    async def run(self, args: ZoomScreenArgs, ctx: ToolContext) -> ToolResult:
        shot = self.memory.shot()
        if shot is None:
            return ToolResult.error(NO_SHOT)
        if not (0 <= args.x < shot.width and 0 <= args.y < shot.height):
            return ToolResult.error(f"That point is outside the screenshot, which is {shot.width}x{shot.height}.")
        # The region in screenshot pixels, the screen's shape, shifted to stay inside the picture.
        width = min(max(args.size or shot.width // 4, min(MIN_ZOOM, shot.width)), shot.width)
        height = min(max(1, round(width * shot.height / shot.width)), shot.height)
        left = min(max(args.x - width // 2, 0), shot.width - width)
        top = min(max(args.y - height // 2, 0), shot.height - height)
        # The same region in the stored full-resolution pixels, and on the desktop.
        rows, cols = shot.pixels.shape[:2]
        kx, ky = cols / shot.width, rows / shot.height
        c1, c2 = round(left * kx), max(round(left * kx) + 1, min(round((left + width) * kx), cols))
        r1, r2 = round(top * ky), max(round(top * ky) + 1, min(round((top + height) * ky), rows))
        data, zoom_width, zoom_height = await asyncio.to_thread(prepare_image, shot.pixels[r1:r2, c1:c2])
        x0, y0, x1, y1 = shot.rect
        px, py = (x1 - x0) / cols, (y1 - y0) / rows  # 1 unless the capture was scaled
        source = (round(x0 + c1 * px), round(y0 + r1 * py), round(x0 + c2 * px), round(y0 + r2 * py))
        self.memory.remember_zoom(Zoom(source, zoom_width, zoom_height, shot.taken))
        closer = zoom_width / width
        detail = (
            f"{closer:.1f} times closer" if closer >= 1.05 else "no sharper: the screenshot already has full detail"
        )
        content = (
            f"Zoomed picture attached ({zoom_width}x{zoom_height} pixels, {detail}). It shows x {left} to "
            f"{left + width} and y {top} to {top + height} of the screenshot."
        )
        if pointing_avatar(ctx) is not None:
            content += (
                " To point at something in it, call point_at_screen with image='zoom' and its pixel position in"
                " this picture (not the screenshot's)."
            )
        return ToolResult.success(content, data={"image": attachment(data), "width": zoom_width, "height": zoom_height})


# --------------------------------------------------------------------------- what to point at


class PointTarget(ToolArgs):
    """Something to point at, as point_at_screen and each step of point_sequence take it: a pixel of the latest
    screenshot or zoom (with the thing's extent when it has a clear one), or a find_on_screen match."""

    x: int | None = Field(default=None, description="Pixels from the left edge of 'image'.")
    y: int | None = Field(default=None, description="Pixels from the top edge of 'image'.")
    image: Literal["screenshot", "zoom"] = Field(
        default="screenshot", description="x, y and box are in the latest look_at_screen or zoom_screen image."
    )
    box: list[int] | None = Field(
        default=None, min_length=4, max_length=4, description="[x1, y1, x2, y2] around it, when it has a clear extent."
    )
    element: int | None = Field(default=None, ge=1, description="A find_on_screen match number, instead of x and y.")
    label: str = Field(default="", description="A short name for it, e.g. 'the apple'.")

    @field_validator("label")
    @classmethod
    def _short_label(cls, value: str) -> str:
        return value[:LABEL_CHARS].strip()  # a long label isn't worth failing the call over

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        has_xy = self.x is not None or self.y is not None
        if self.element is not None and has_xy:
            raise ValueError("give either x and y, or element, not both")
        if self.element is None and (self.x is None or self.y is None):
            raise ValueError("give both x and y, or an element number from find_on_screen")
        return self


@dataclass(frozen=True)
class Spot:
    """A place on the desktop to point at: physical pixels, the thing's extent when it has a clear one, its name,
    and where it is in words when a find_on_screen match already said."""

    x: int
    y: int
    box: Rect | None
    label: str
    where: str = ""


def locate(target: PointTarget, memory: ScreenMemory) -> Spot | str:
    """The desktop spot ``target`` names, or why it can't be pointed at (for the model)."""
    if target.element is not None:
        matches = memory.matches()
        if not matches:
            return "I have no recent find_on_screen results. Search again first."
        if target.element > len(matches):
            return f"There's no match {target.element}; pick 1 to {len(matches)}."
        match = matches[target.element - 1]
        (x, y), box = match.centre, match.rect
        return Spot(x, y, box, target.label or match.name, match.where)
    record = memory.shot() if target.image == "screenshot" else memory.zoom()
    if record is None:
        if target.image == "screenshot":
            return NO_SHOT
        return "I don't have a recent zoom. Use zoom_screen first, or the screenshot."
    assert target.x is not None and target.y is not None  # the arguments' validator made sure
    if not (0 <= target.x < record.width and 0 <= target.y < record.height):
        return f"That spot is outside the {target.image}, which is {record.width}x{record.height} pixels."
    box = None
    if target.box is not None:
        x1, x2 = sorted((target.box[0], target.box[2]))
        y1, y2 = sorted((target.box[1], target.box[3]))
        if x2 <= 0 or y2 <= 0 or x1 >= record.width or y1 >= record.height or x1 == x2 or y1 == y2:
            return f"That box isn't inside the {target.image}."
        box = record.box_to_screen((x1, y1, x2, y2))
    x, y = record.to_screen(target.x, target.y)
    return Spot(x, y, box, target.label)


def point_with(avatar: Any, spot: Spot) -> str | None:
    """Has the avatar fly over to ``spot`` and point at it: None once she's on her way, else why not (for the
    model, which is then told to describe where things are instead)."""
    try:
        problem = avatar.point(spot.x, spot.y, box=spot.box, label=spot.label)
    except Exception as exc:
        log.warning("Asking the avatar to point failed", exc_info=True)
        return f"I couldn't point ({type(exc).__name__})"
    return f"I can't point right now ({problem})" if problem else None


# --------------------------------------------------------------------------- point_at_screen


class PointAtScreenArgs(PointTarget):
    pass


class PointAtScreen(Tool):
    name = "point_at_screen"
    description = (
        "Show the user where something on their screen is: the avatar points at it, with a ring on the spot, and "
        "keeps pointing while you talk. To show several things in turn, each with its own line, use point_sequence "
        "instead of calling this several times. Only while the avatar is on; if it isn't, just say where it is."
    )
    Args = PointAtScreenArgs
    risk = RiskLevel.SAFE  # it only moves the avatar
    category = "system"
    timeout_s = 10.0

    def __init__(
        self, memory: ScreenMemory | None = None, monitors: Callable[[], Sequence[Rect]] | None = None
    ) -> None:
        self.memory = memory if memory is not None else SCREEN
        self._monitors = monitors or monitor_rects

    async def describe_call(self, args: PointAtScreenArgs, ctx: ToolContext) -> str:
        return f"point at {args.label or 'it'} on your screen"

    async def run(self, args: PointAtScreenArgs, ctx: ToolContext) -> ToolResult:
        avatar = pointing_avatar(ctx)
        if avatar is None:
            if can_mark(ctx):
                return ToolResult.error("The avatar isn't on: draw it with mark_screen instead (aimed the same way).")
            return ToolResult.error(NO_AVATAR)
        spot = locate(args, self.memory)
        if isinstance(spot, str):
            return ToolResult.error(spot)
        if problem := point_with(avatar, spot):
            return ToolResult.error(f"{problem}, so describe where it is instead.")
        where = spot.where
        if not where:
            try:
                where = describe_where((spot.x, spot.y), await asyncio.to_thread(self._monitors))
            except Exception:
                log.debug("Listing the monitors failed", exc_info=True)
        place = f", {where}" if where else ""
        content = (
            f"Pointing at {spot.label or 'it'} now{place}. Also tell the user in a few words where it is, in case "
            "they aren't watching the avatar."
        )
        box = list(spot.box) if spot.box else None
        return ToolResult.success(content, data={"x": spot.x, "y": spot.y, "box": box})


# --------------------------------------------------------------------------- point_sequence

MAX_STEPS = 8
SAY_CHARS = 200
# Without a voice to say the lines, she stays on each spot about as long as saying its line would take.
PAUSE_S = 0.35
PAUSE_PER_CHAR_S = 0.06
_POLL_S = 0.25  # how often such a pause checks whether the user cut the turn off
# No step starts this close to the tool's time limit: stopping between two steps, and saying so, beats being cut off
# mid-line with nothing to tell the model which lines were heard.
_MARGIN_S = 20.0
_CUT_OFF = "cut off"
_OUT_OF_TIME = "out of time"


class PointStep(PointTarget):
    say: str = Field(
        min_length=1,
        max_length=SAY_CHARS,
        description="One short sentence to say while pointing there, e.g. 'First, open the File menu.'",
    )


class PointSequenceArgs(ToolArgs):
    steps: list[PointStep] = Field(
        min_length=1,
        max_length=MAX_STEPS,
        description="The things to show, in order.",
    )


class PointSequence(Tool):
    name = "point_sequence"
    description = (
        "Walk the user through several things on their screen in order: the avatar points at each while its "
        "spoken line is said, then moves on, staying on the last. Use it instead of several point_at_screen calls "
        "whenever you show more than one thing (steps to follow, parts of a window). Each step is aimed like "
        "point_at_screen. The lines are spoken for you as she points, so don't repeat them in your reply. Only "
        "while the avatar is on; if it isn't, just say where each thing is."
    )
    Args = PointSequenceArgs
    risk = RiskLevel.SAFE  # it only moves the avatar and says the model's own lines
    category = "system"
    timeout_s = 120.0

    def __init__(
        self, memory: ScreenMemory | None = None, sleep: Callable[[float], Awaitable[Any]] | None = None
    ) -> None:
        self.memory = memory if memory is not None else SCREEN
        self._sleep = sleep or asyncio.sleep

    async def describe_call(self, args: PointSequenceArgs, ctx: ToolContext) -> str:
        return f"point at {len(args.steps)} things on your screen, one after another"

    async def run(self, args: PointSequenceArgs, ctx: ToolContext) -> ToolResult:
        avatar = pointing_avatar(ctx)
        if avatar is None:
            if can_mark(ctx):
                return ToolResult.error("The avatar isn't on: use guide_on_screen instead (the same steps).")
            return ToolResult.error("The avatar isn't on, so say where each one is instead.")
        # Every spot is found before she moves: a step that can't work fails the call before anything is shown or
        # said, so the model can fix it and try again without the user hearing the first lines twice.
        spots: list[Spot] = []
        for number, step in enumerate(args.steps, 1):
            spot = locate(step, self.memory)
            if isinstance(spot, str):
                return ToolResult.error(f"Step {number}: {spot} Nothing was pointed at or said.")
            spots.append(spot)
        speak = ctx.extras.get("speak")
        started = _turn(ctx)
        deadline = time.monotonic() + self.timeout_s - _MARGIN_S
        heard: list[bool] = []  # for each step shown so far: whether its line was heard
        stop = ""  # why the rest wasn't shown: _CUT_OFF, _OUT_OF_TIME, or why she couldn't point
        for step, spot in zip(args.steps, spots, strict=True):
            if _cut_off(ctx, started):
                stop = _CUT_OFF
                break
            if heard and time.monotonic() > deadline:
                stop = _OUT_OF_TIME
                break
            if problem := point_with(avatar, spot):
                stop = problem
                break
            said = await _say(speak, step.say)
            heard.append(said)
            if said:
                continue
            if _cut_off(ctx, started):
                stop = _CUT_OFF
                break
            # No voice to say it (or it failed): she still stays on the spot for about as long as the line.
            if not await self._hold(PAUSE_S + PAUSE_PER_CHAR_S * len(step.say), ctx, started):
                stop = _CUT_OFF
                break
        return _sequence_result(len(args.steps), spots, heard, stop)

    async def _hold(self, seconds: float, ctx: ToolContext, started: TurnInfo | None) -> bool:
        """Wait ``seconds`` on a spot; False as soon as the user cuts the turn off."""
        left = seconds
        while left > 0.001:
            pause = min(_POLL_S, left)
            await self._sleep(pause)
            left -= pause
            if _cut_off(ctx, started):
                return False
        return True


def _turn(ctx: ToolContext) -> TurnInfo | None:
    """The user's turn running now (``ctx.extras["turn"]``); None between turns, or when there's no way to tell."""
    current = ctx.extras.get("turn")
    if current is None:
        return None
    try:
        info = current()
    except Exception:
        log.debug("Asking which turn is running failed", exc_info=True)
        return None
    return info if isinstance(info, TurnInfo) else None


def _cut_off(ctx: ToolContext, started: TurnInfo | None) -> bool:
    """Whether the user cut off the turn this call runs in (a new request, the stop hotkey), or it's over."""
    if started is None:
        return False  # not started inside a turn, or no way to tell: nothing to cut off
    now = _turn(ctx)
    return now is None or now.id != started.id or now.interrupted


async def _say(speak: Any, line: str) -> bool:
    """Says ``line`` in the running turn (``ctx.extras["speak"]``) and returns once it's been heard; False when it
    wasn't: no voice, the turn was cut off, or saying it failed."""
    if speak is None:
        return False
    try:
        return await speak(line) is True
    except Exception:
        log.warning("Saying a line while pointing failed", exc_info=True)
        return False


def _steps(numbers: Sequence[int]) -> str:
    """[3] -> "step 3", [1, 2] -> "steps 1 and 2", [1, 2, 4] -> "steps 1, 2 and 4"."""
    if len(numbers) == 1:
        return f"step {numbers[0]}"
    return "steps " + ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def _lines_of(numbers: Sequence[int]) -> str:
    """[2] -> "the line of step 2", [1, 3] -> "the lines of steps 1 and 3"."""
    return f"the {'line' if len(numbers) == 1 else 'lines'} of {_steps(numbers)}"


def _lines(heard: Sequence[bool]) -> str:
    """What became of the lines of the steps shown, and so what the model should still say."""
    said = [n for n, ok in enumerate(heard, 1) if ok]
    unsaid = [n for n, ok in enumerate(heard, 1) if not ok]
    if not unsaid:
        if len(said) == 1:
            return "the user heard its line as she pointed, so don't say it again"
        return "the user heard each line as she pointed there, so don't say them again"
    if not said:
        return (
            "the lines couldn't be said out loud (there's no voice right now), so give the user those lines now, "
            "in order and briefly"
        )
    return (
        f"the user heard {_lines_of(said)} as she pointed, so don't repeat {'it' if len(said) == 1 else 'those'}; "
        f"{_lines_of(unsaid)} couldn't be said, so give {'it' if len(unsaid) == 1 else 'those'} now, briefly"
    )


def _sequence_result(total: int, spots: Sequence[Spot], heard: Sequence[bool], stop: str) -> ToolResult:
    """What the model learns once a sequence is over: what was shown and heard, and what it should still say."""
    shown = len(heard)
    data = {"steps": total, "pointed": shown, "heard": sum(heard), "stopped": stop or None}
    rest = _steps(range(shown + 1, total + 1)) if shown < total else ""
    last = spots[shown - 1].label if shown else ""
    if stop == _CUT_OFF:
        if not shown:
            return ToolResult.success("The user cut the turn off before the first step: nothing was shown.", data=data)
        content = f"The user cut the turn off, so the sequence stopped after step {shown} of {total}."
        if any(heard):
            content += f" They heard {_lines_of([n for n, ok in enumerate(heard, 1) if ok])}."
        return ToolResult.success(content + " Don't carry on with the rest unless they ask.", data=data)
    if stop == _OUT_OF_TIME:
        return ToolResult.success(
            f"Out of time after step {shown} of {total}: {_lines(heard)}. Tell the user about the rest ({rest}) "
            f"yourself, briefly. She's still pointing at {last or 'the last one shown'}.",
            data=data,
        )
    if stop:  # she couldn't point any more
        if not shown:
            return ToolResult.error(f"{stop}, so say where each one is instead.", data=data)
        return ToolResult.error(
            f"Pointed at {shown} of {total}: {_lines(heard)}. {stop} for {rest}, so describe those yourself instead.",
            data=data,
        )
    what = "it" if total == 1 else f"all {total} in order"
    content = f"Pointed at {what}: {_lines(heard)}."
    if all(heard):
        content += " Reply in a few words at most, or not at all."
    target = last or ("it" if total == 1 else "the last one")
    return ToolResult.success(f"{content} She's still pointing at {target}.", data=data)


# --------------------------------------------------------------------------- find_on_screen


class FindOnScreenArgs(ToolArgs):
    text: str = Field(
        min_length=2,
        max_length=80,
        description="The visible label, as written on screen, e.g. 'Save' or 'Wi-Fi'. Case doesn't matter.",
    )


# A search gets the text and the processes whose windows to look past (``_uia.find``).
Search = Callable[[str, Collection[int]], Awaitable[list[Match]]]


def avatar_pids(ctx: ToolContext) -> frozenset[int]:
    """Her own apps' processes while they run, the avatar's, her pop-up panels' (``extras["visuals"]``), her
    marks' (``extras["marks"]``) and her chat window's (``extras["chat"]``): their windows are never searched, nor
    taken for something covering a match (she may well be standing right beside it), a click, scroll or key never
    goes to them, and the user typing or clicking there is talking to her, not taking a task's mouse and keyboard
    back. Empty when none runs or they can't say."""
    pids: set[int] = set()
    for name, attr in (("avatar", "app_pid"), ("visuals", "pid"), ("marks", "pid"), ("chat", "pids")):
        try:
            found = getattr(ctx.extras.get(name), attr, None)
        except Exception:  # a bridge in a bad state
            log.debug("Asking the %s for its process failed", name, exc_info=True)
            continue
        for pid in found if isinstance(found, frozenset | set | tuple | list) else (found,):
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                pids.add(pid)
    return frozenset(pids)


class FindOnScreen(Tool):
    name = "find_on_screen"
    description = (
        "Find buttons, links, menu items, tabs, settings and other labelled things on the user's screen by their "
        "text (Windows accessibility data): exact positions, no screenshot, much cheaper than looking. Use it when "
        "the user asks where a named control is or to point at one. Pictures, games, videos and drawings have no "
        "labels: use look_at_screen for those, or when nothing useful is found."
    )
    Args = FindOnScreenArgs
    risk = RiskLevel.CONFIRM  # it reads what's on the screen, like a screenshot does
    category = "system"
    timeout_s = 10.0

    def __init__(self, search: Search | None = None, memory: ScreenMemory | None = None) -> None:
        self._search = search
        self.memory = memory if memory is not None else SCREEN

    async def describe_call(self, args: FindOnScreenArgs, ctx: ToolContext) -> str:
        return f'look for "{args.text}" on your screen'

    async def precheck(self, args: FindOnScreenArgs, ctx: ToolContext) -> str | None:
        if self._search is None and sys.platform != "win32":
            return "Finding things on screen only works on Windows."
        return None

    async def run(self, args: FindOnScreenArgs, ctx: ToolContext) -> ToolResult:
        if problem := await self.precheck(args, ctx):
            return ToolResult.error(problem)
        try:
            found = await (self._search or _uia.find)(args.text, avatar_pids(ctx))
        except UIAUnavailable as exc:
            log.warning("UI Automation is unavailable: %s", exc)
            return ToolResult.error("I can't read the screen's labels here, so use look_at_screen instead.")
        except TimeoutError:
            log.warning("UI Automation search for %r held up by an app that isn't answering", args.text)
            return ToolResult.error(
                "An app on screen isn't answering, so its labels couldn't be read. Use look_at_screen instead."
            )
        except Exception as exc:
            log.warning("UI Automation search failed", exc_info=True)
            return ToolResult.error(f"Searching the screen failed ({type(exc).__name__}). Use look_at_screen instead.")
        ranked = rank(found, args.text)
        shown = ranked[: _uia.MAX_MATCHES]
        self.memory.remember_matches(shown)  # numbered as shown; an empty list forgets older numbers
        if not shown:
            return ToolResult.success(
                f'Nothing on screen has a label with "{args.text}" in it. Pictures, games, videos and drawings have '
                "no labels: use look_at_screen to look instead."
            )
        things = "thing" if len(ranked) == 1 else "things"
        lines = [f'Found {len(ranked)} {things} on screen labelled with "{args.text}", best first:']
        for number, match in enumerate(shown, 1):
            line = f'{number}. {match.kind} "{match.name}"'
            if match.window and match.window.casefold() != match.name.casefold():
                line += f' in "{match.window}"'
            if match.where:
                line += f", {match.where}"
            if match.covered:
                line += " (behind another window)"
            lines.append(line)
        if len(ranked) > len(shown):
            lines.append(f"({len(ranked) - len(shown)} more not shown.)")
        if pointing_avatar(ctx) is not None:
            lines.append(
                "To show the user one, call point_at_screen with element set to its number. If none is what they "
                "mean, use look_at_screen instead."
            )
        elif can_mark(ctx):
            lines.append(
                "To show the user one, call mark_screen with element set to its number. If none is what they mean, "
                "use look_at_screen instead."
            )
        else:
            lines.append("Tell the user where it is. If none is what they mean, use look_at_screen instead.")
        return ToolResult.success("\n".join(lines), data={"matches": len(ranked)})


TOOLS = [ZoomScreen, PointAtScreen, PointSequence, FindOnScreen]
