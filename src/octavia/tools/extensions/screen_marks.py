"""Drawing on the user's real screen to guide them: arrows, rings, boxes, highlights and labels over whatever is
there ("click here, then this field"), in her orb's hazy blue (``octavia.visuals.marks``).

``mark_screen`` puts up a set of marks, aimed like ``point_at_screen`` (a pixel of the latest screenshot or zoom, or
a ``find_on_screen`` match), and calling it again swaps them in place. ``guide_on_screen`` walks through several
steps, each marked while its line is said (``ctx.extras["speak"]``), earlier steps staying on quieter with their
numbers, like ``point_sequence`` does with the avatar. ``clear_marks`` takes them down; they also clear by
themselves (``visuals.marks_stay_s``) and on the stop hotkey. All SAFE: they only draw on the user's own screen, in
click-through windows kept out of screenshots and shares, and need no avatar.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult, TurnInfo
from octavia.tools.extensions._uia import describe_where
from octavia.tools.extensions.screen_capture import SCREEN, Rect, ScreenMemory, monitor_rects
from octavia.tools.extensions.screen_pointing import (
    _CUT_OFF,
    _MARGIN_S,
    _OUT_OF_TIME,
    PAUSE_PER_CHAR_S,
    PAUSE_S,
    SAY_CHARS,
    PointTarget,
    Spot,
    _cut_off,
    _lines,
    _lines_of,
    _say,
    _steps,
    _turn,
    locate,
)
from octavia.visuals.marks import Mark, MarksCleared, MarksError, MarksHost

OFF = "Drawing on the screen is turned off (Settings > Pop-up panels). Say where it is instead."
NOT_RUNNING = "I can't draw on the screen in this session. Say where it is instead."
REMOTE = "They're not at the screen (this came from their phone), so describe where it is instead."
STALE_S = 20.0  # a screenshot older than this may no longer match the screen: she's told how old it is
MAX_MARKS = 8
MAX_STEPS = 8
_POLL_S = 0.25

_NAMES = {"circle": "a ring round", "box": "a box round", "highlight": "a highlight on", "arrow": "an arrow to"}


def _host(ctx: ToolContext) -> MarksHost | None:
    host = ctx.extras.get("marks")
    return host if isinstance(host, MarksHost) else None


def _unavailable(ctx: ToolContext) -> str | None:
    """Why she can't draw now, or None."""
    if _host(ctx) is None:
        return NOT_RUNNING
    if not ctx.config.visuals.marks:
        return OFF
    turn = _turn(ctx)
    if turn is not None and turn.remote:
        return REMOTE
    return None


class MarkTarget(PointTarget):
    """One mark: what to draw, and where (aimed like point_at_screen)."""

    kind: Literal["circle", "box", "highlight", "arrow", "label"] = Field(
        default="circle",
        description=(
            "circle: a ring round it. box: corner brackets round it. highlight: tints it. arrow: points at it from "
            "the side with room. label: just words beside it, with a line to the spot."
        ),
    )
    label: str = Field(
        default="",
        description="Short words written beside it, e.g. 'Save' or 'type your email here'. Needed for kind label.",
    )

    @model_validator(mode="after")
    def _label_has_words(self) -> Self:
        self.label = " ".join(self.label.split())  # as the app writes it: blanks alone are no words
        if self.kind == "label" and not self.label:
            raise ValueError("a label mark needs its words in label")
        return self


def _mark(target: MarkTarget, spot: Spot, step: int | None = None, done: bool = False) -> Mark:
    return Mark(target.kind, spot.x, spot.y, spot.box, target.label, step, done)


def _locate_all(targets: Sequence[MarkTarget], memory: ScreenMemory, what: str) -> list[Spot] | str:
    spots: list[Spot] = []
    for number, target in enumerate(targets, 1):
        spot = locate(target, memory)
        if isinstance(spot, str):
            return f"{what} {number}: {spot} Nothing was drawn."
        spots.append(spot)
    return spots


def _age_note(targets: Sequence[MarkTarget], memory: ScreenMemory) -> str:
    """A caution when a mark is placed from a picture old enough that the screen may have moved on."""
    if all(t.element is not None for t in targets):
        record = None
    else:
        record = memory.zoom() if any(t.image == "zoom" for t in targets) else memory.shot()
    taken = getattr(record, "taken", None)
    if taken is None:
        return ""
    age = memory.now() - taken
    if age < STALE_S:
        return ""
    return (
        f" They're placed from a picture {age:.0f} s old: if the screen has changed since (scrolled, another "
        "window), look again and redraw."
    )


def _what(targets: Sequence[MarkTarget], spots: Sequence[Spot]) -> str:
    parts = []
    for target, spot in zip(targets, spots, strict=True):
        name = target.label or spot.label or "it"
        if target.kind == "label":
            parts.append(f"the words “{target.label}”")
        else:
            parts.append(f"{_NAMES[target.kind]} {name}")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _missing(missing: Sequence[int]) -> str:
    if not missing:
        return ""
    numbers = ", ".join(str(i + 1) for i in missing)
    return f" Mark {numbers} {'is' if len(missing) == 1 else 'are'} off every screen, so not drawn."


def _stay(stay_s: float) -> str:
    if not stay_s:
        return "They stay until cleared"
    return f"They clear by themselves in about {stay_s:g} s"


def _where(spot: Spot, monitors: Callable[[], Sequence[Rect]]) -> str:
    if spot.where:
        return spot.where
    try:
        return describe_where((spot.x, spot.y), monitors())
    except Exception:
        return ""


# --------------------------------------------------------------------------- mark_screen


class MarkScreenArgs(ToolArgs):
    marks: list[MarkTarget] = Field(min_length=1, max_length=MAX_MARKS, description="What to draw.")
    add: bool = Field(default=False, description="Keep the marks already up and add these; else these replace them.")
    stay_s: float | None = Field(
        default=None, ge=0, le=600, description="Seconds they stay (default 90); 0 = until cleared."
    )


class MarkScreen(Tool):
    name = "mark_screen"
    description = (
        "Draw on the user's screen to show them where things are: a ring, box or highlight round something, an "
        "arrow to it, or a short label beside it ('Save', 'your email goes here'). Use it on your own, unasked, "
        "whenever what you're saying is about something they can see there. Works with the avatar off. Aim "
        "each mark like point_at_screen: a pixel (and box) of the latest look_at_screen or zoom_screen picture, or a "
        "find_on_screen element. Calling it again replaces the marks in place, so update them as you talk. To walk "
        "through several steps with a line each, use guide_on_screen. They clear by themselves, on clear_marks, or "
        "on the user's stop hotkey."
    )
    Args = MarkScreenArgs
    risk = RiskLevel.SAFE  # draws on their own screen only, click-through and kept out of screenshots and shares
    category = "system"
    timeout_s = 40.0  # the marks app's first start, plus the draw

    def __init__(
        self, memory: ScreenMemory | None = None, monitors: Callable[[], Sequence[Rect]] | None = None
    ) -> None:
        self.memory = memory if memory is not None else SCREEN
        self._monitors = monitors or monitor_rects

    async def describe_call(self, args: MarkScreenArgs, ctx: ToolContext) -> str:
        return "draw on your screen"

    async def run(self, args: MarkScreenArgs, ctx: ToolContext) -> ToolResult:
        if problem := _unavailable(ctx):
            return ToolResult.error(problem)
        host = _host(ctx)
        assert host is not None
        spots = _locate_all(args.marks, self.memory, "Mark")
        if isinstance(spots, str):
            return ToolResult.error(spots)
        new = [_mark(t, s) for t, s in zip(args.marks, spots, strict=True)]
        kept = list(host.on_screen) if args.add else []
        room = MAX_MARKS * 2 - len(new)
        dropped = max(0, len(kept) - room)
        kept = kept[dropped:]
        marks = kept + new
        try:
            drawn = await host.draw(marks, stay_s=args.stay_s)
        except MarksCleared:
            return ToolResult.success(
                "The user cleared the marks (their stop shortcut) before they came up, so nothing is drawn. Don't "
                "draw them again unless they ask.",
                data={"shown": 0},
            )
        except MarksError as exc:
            return ToolResult.error(f"{exc} Say where it is instead.")
        offset = len(marks) - len(new)
        missing = [i - offset for i in drawn.missing if i >= offset]
        verb = "Added" if kept else "On the screen now"
        if dropped:
            verb = f"Added (the {dropped} oldest marks went, to make room)"
        where = _where(spots[0], self._monitors)
        content = (
            f"{verb}: {_what(args.marks, spots)}{f', {where}' if where else ''}.{_missing(missing)} "
            f"{_stay(drawn.stay_s)}; they don't move if the screen does.{_age_note(args.marks, self.memory)} "
            "Tell the user in a few words what you marked."
        )
        return ToolResult.success(content, data={"shown": drawn.shown, "missing": missing, "id": drawn.id})


# --------------------------------------------------------------------------- guide_on_screen


class GuideStep(MarkTarget):
    say: str = Field(
        min_length=1,
        max_length=SAY_CHARS,
        description="One short sentence said while it's marked, e.g. 'First, open the File menu.'",
    )


class GuideOnScreenArgs(ToolArgs):
    steps: list[GuideStep] = Field(min_length=1, max_length=MAX_STEPS, description="The steps, in order.")
    stay_s: float | None = Field(
        default=None, ge=0, le=600, description="Seconds the marks stay once the last step is up (default 90)."
    )


class GuideOnScreen(Tool):
    name = "guide_on_screen"
    description = (
        "Walk the user through steps on their screen: each step is marked (ring, box, highlight, arrow or label, "
        "numbered) while its line is said, then the next; earlier steps stay on, quieter, and the marks stay up "
        "after the last. Use it to show how to do something ('click here, then this field'). Each step is aimed "
        "like mark_screen. The lines are spoken for you, so don't repeat them in your reply. Works with the avatar "
        "off."
    )
    Args = GuideOnScreenArgs
    risk = RiskLevel.SAFE  # it only draws on their screen and says the model's own lines
    category = "system"
    timeout_s = 120.0

    def __init__(
        self, memory: ScreenMemory | None = None, sleep: Callable[[float], Awaitable[Any]] | None = None
    ) -> None:
        self.memory = memory if memory is not None else SCREEN
        self._sleep = sleep or asyncio.sleep

    async def describe_call(self, args: GuideOnScreenArgs, ctx: ToolContext) -> str:
        return f"mark {len(args.steps)} steps on your screen, one after another"

    async def run(self, args: GuideOnScreenArgs, ctx: ToolContext) -> ToolResult:
        if problem := _unavailable(ctx):
            return ToolResult.error(problem.replace("where it is", "where each step is"))
        host = _host(ctx)
        assert host is not None
        # Every step is placed before anything is drawn or said: a step that can't work fails the call untouched.
        spots = _locate_all(args.steps, self.memory, "Step")
        if isinstance(spots, str):
            return ToolResult.error(spots.replace("Nothing was drawn.", "Nothing was drawn or said."))
        numbered = len(args.steps) > 1
        speak = ctx.extras.get("speak")
        started = _turn(ctx)
        deadline = time.monotonic() + self.timeout_s - _MARGIN_S
        heard: list[bool] = []
        stop = ""
        missing: list[int] = []
        clears = host.clears
        for n in range(len(args.steps)):
            if _cut_off(ctx, started):
                stop = _CUT_OFF
                break
            if heard and time.monotonic() > deadline:
                stop = _OUT_OF_TIME
                break
            marks = [
                _mark(t, s, step=i + 1 if numbered else None, done=i < n)
                for i, (t, s) in enumerate(zip(args.steps[: n + 1], spots[: n + 1], strict=True))
            ]
            if host.clears != clears:  # the user cleared them (the stop shortcut): the walkthrough is over
                stop = _CUT_OFF
                break
            try:
                drawn = await host.draw(marks, stay_s=args.stay_s)
            except MarksCleared:
                stop = _CUT_OFF
                break
            except MarksError as exc:
                if _cut_off(ctx, started):
                    stop = _CUT_OFF
                    break
                stop = str(exc)
                break
            if n in drawn.missing:
                missing.append(n + 1)
            said = await _say(speak, args.steps[n].say)
            heard.append(said)
            if said:
                continue
            if _cut_off(ctx, started):
                stop = _CUT_OFF
                break
            if not await self._hold(PAUSE_S + PAUSE_PER_CHAR_S * len(args.steps[n].say), ctx, started):
                stop = _CUT_OFF
                break
        return _guide_result(len(args.steps), heard, stop, missing, host)

    async def _hold(self, seconds: float, ctx: ToolContext, started: TurnInfo | None) -> bool:
        left = seconds
        while left > 0.001:
            pause = min(_POLL_S, left)
            await self._sleep(pause)
            left -= pause
            if _cut_off(ctx, started):
                return False
        return True


def _guide_result(total: int, heard: Sequence[bool], stop: str, missing: Sequence[int], host: MarksHost) -> ToolResult:
    """What the model learns once a walkthrough is over: what was marked and heard, and what it should still say."""
    shown = len(heard)
    data = {"steps": total, "marked": shown, "heard": sum(heard), "stopped": stop or None, "missing": list(missing)}
    off = (
        f" {_steps(list(missing)).capitalize()} {'was' if len(missing) == 1 else 'were'} off every screen."
        if missing
        else ""
    )
    up = " The marks are still up." if host.on_screen else ""
    rest = _steps(list(range(shown + 1, total + 1))) if shown < total else ""
    if stop == _CUT_OFF:
        if not shown:
            return ToolResult.success("The user cut the turn off before the first step: nothing was shown.", data=data)
        content = f"The user cut the turn off, so the walkthrough stopped after step {shown} of {total}."
        if any(heard):
            content += f" They heard {_lines_of([n for n, ok in enumerate(heard, 1) if ok])}."
        return ToolResult.success(content + up + " Don't carry on with the rest unless they ask.", data=data)
    if stop == _OUT_OF_TIME:
        return ToolResult.success(
            f"Out of time after step {shown} of {total}: {_lines(heard)}. Tell the user about the rest ({rest}) "
            f"yourself, briefly.{off}{up}",
            data=data,
        )
    if stop:  # the marks couldn't be drawn
        if not shown:
            return ToolResult.error(f"{stop} Nothing was drawn or said, so say where each step is instead.", data=data)
        return ToolResult.error(
            f"Marked {shown} of {total}: {_lines(heard)}. Then: {stop} Describe {rest} yourself instead.{off}{up}",
            data=data,
        )
    content = f"Marked {'it' if total == 1 else f'all {total} steps in order'}: {_lines(heard)}.{off}"
    if all(heard):
        content += " Reply in a few words at most, or not at all."
    return ToolResult.success(content + up, data=data)


# --------------------------------------------------------------------------- clear_marks


class ClearMarks(Tool):
    name = "clear_marks"
    description = "Take down the marks you drew on the user's screen (mark_screen, guide_on_screen)."
    risk = RiskLevel.SAFE
    category = "system"
    timeout_s = 10.0

    async def describe_call(self, args: ToolArgs, ctx: ToolContext) -> str:
        return "clear the marks on your screen"

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        host = _host(ctx)
        if host is None:
            return ToolResult.success("No marks were up.", data={"cleared": False})
        had = await host.clear("octavia")
        return ToolResult.success("Cleared the marks." if had else "No marks were up.", data={"cleared": had})


TOOLS = [MarkScreen, GuideOnScreen, ClearMarks]
