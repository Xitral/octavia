"""Drag and drop: she presses the mouse button on one thing, carries it across and lets go on another, for what only
a drag does (sorting cards into buckets in a course exercise, reordering a list, moving a slider).

It works like ``click`` (computer_control), at both ends: only in a running turn, one action at a time, the spots
given as click takes them (a find_on_screen match or a pixel of the latest look), both looked at again right before
the press, and she stops once the user took over. What a drag would do is judged from the screen
(``classify_drag``): files dragged in or out of File Explorer or the desktop get moved, copied or uploaded, a drop on
a Delete or Trash target deletes, and a drag from one window into another hands something to another app; those ask
first.

Sorting six cards used to take a find_on_screen, a drag and two looks per card (the first look came while the drop
was still animating). So one call can make several drags (``then``), each waiting for the drop before the next, and
``look`` returns a small picture once the screen has stopped moving, kept like a look_at_screen one so the next drag
can aim in it. That picture leaves the computer, so it's decided like a look_at_screen (by the user's rule for that).
Several drags in a call must all be harmless; a risky one goes on its own, so the user's yes covers exactly it.

While the button is down the takeover watch can't be used (her own button looks like the user's), so between steps
she checks that the pointer is still where she put it. The button is pressed and let go on the event loop, the
letting go in a ``finally``, so a failed or cancelled call never leaves it held.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, PrivateAttr, field_validator, model_validator

from octavia.core.events import ScreenshotTaken
from octavia.tools.base import RiskFinding, RiskLevel, ToolContext, ToolResult, TurnInfo
from octavia.tools.extensions._input import MOVED_PX, Watch
from octavia.tools.extensions._uia import Target
from octavia.tools.extensions.computer_control import (
    _SURFACES,
    HOVER_S,
    INTERRUPTED,
    LABEL_CHARS,
    NOT_IN_TURN,
    POINT_WAIT_S,
    SETTLE_S,
    TAKEN_OVER,
    TOOK_OVER_RULE,
    Inspector,
    Spot,
    _action,
    _action_name,
    _bare,
    _command,
    _Control,
    _name_of,
    _needs_yes,
    _pointing_box,
    _processes,
    _SpotArgs,
    _still_there,
    _unchecked,
    app_name,
    classify_click,
    current_turn,
    is_shell,
    risk_in_name,
    thing,
)
from octavia.tools.extensions.screen_capture import (
    Area,
    Capture,
    Rect,
    ScreenMemory,
    Screenshot,
    attachment,
    capture_screen,
    encode_image,
    fit_for_claude,
    scale_down,
)
from octavia.tools.extensions.screen_pointing import avatar_pids

log = logging.getLogger(__name__)

DRAG_STEPS = 12
STEP_S = 0.03  # between the steps: pages follow the pointer as it goes, and some only see a drag in motion
PRESS_S = 0.15  # holding still on it before moving: some pages pick a thing up only after a moment
START_PX = 8  # the first small move: pages wait for the pointer to move a few pixels before a press is a drag
DROP_S = 0.5  # after letting go: a page animates the drop into place before the next drag or look
MAX_DRAGS = 10
STILL_EVERY_S = 0.15  # between the pictures that tell whether the drop has settled
MAX_STILL_LOOKS = 11  # 1.5 s at most: an animation that never stops (a video, a spinner) doesn't hold it up longer
STILL_DIFF = 1.5  # mean change per pixel and colour (0-255) of the compare pictures below which nothing moves
COMPARE_SIDE = 160
AFTER_TOKENS = 800  # the after-picture's budget in Claude's image patches: about half a full screenshot
# Where any drag moves, copies or uploads files: File Explorer and the desktop (and the taskbar, which opens them).
FILE_PLACES = frozenset({"explorer.exe"})
# The picture afterwards leaves the computer: it's a look_at_screen, so it's decided by the user's rule for that
# (with look_at_screen: auto, never asked while they're working with her) and otherwise asked once for a task's drags.
_LOOKING = RiskFinding(
    RiskLevel.CONFIRM, "look", "sends a small picture of the screen to Claude", like="look_at_screen"
)
ONE_AT_A_TIME = "One of those drags needs the user's yes, so I didn't drag. Make that one on its own, in its own call."
_WHAT = {"screen": "the screen", "window": "the active window", "all": "all the screens"}


def classify_drag(
    start: Target, end: Target, label: str = "", to_label: str = "", risky: bool = False
) -> RiskFinding | None:
    """Whether pressing on ``start`` and letting go on ``end`` needs the user's yes, and why. ``label``, ``to_label``
    and ``risky`` are the model's word for it: they can only add risk."""
    if is_shell(start) or is_shell(end):
        return _command("drag")
    if start.timed_out or end.timed_out:
        return _unchecked(f"drag {label or 'it'}")
    if (_processes(start) | _processes(end)) & FILE_PLACES:
        return RiskFinding(RiskLevel.CONFIRM, "files", "moves, copies or uploads files")
    if (found := classify_click(end, to_label)) is not None:
        return found  # letting go on a button works it, like a click
    if (risk := risk_in_name(end.name)) is not None and risk.scope == "delete":
        return RiskFinding(RiskLevel.CONFIRM, risk.scope, risk.reason)  # a drop zone called Trash or Delete
    if start.hwnd and end.hwnd and start.hwnd != end.hwnd:
        return RiskFinding(RiskLevel.CONFIRM, "other window", "moves something into another window")
    if risky:
        return RiskFinding(RiskLevel.CONFIRM, "flagged", "can't easily be undone")
    return None


def _kind(finding: RiskFinding) -> str:
    """A finding's scope without the number that makes a command's or an unchecked input's scope new each time."""
    return finding.scope.split("#", 1)[0]


def picks_up(now: Target, seen: Target, label: str = "", found: str = "") -> bool:
    """Whether the look right before the press still shows what the risk check saw at the start. Besides what a click
    allows, a plain surface with nothing to press in the same window and app: a web page answers a look at a card
    with the group around it from one moment to the next, and pressing there works nothing (what the drop does is
    still checked)."""
    if _still_there(now, seen, label, found):
        return True
    return (
        now.kind in _SURFACES
        and _action(now) is None
        and (now.hwnd, now.pid) == (seen.hwnd, seen.pid)
        and (not now.name or (now.kind, now.name) in seen.chain)
    )


def _named_else(now: Target, seen: Target) -> bool:
    """Whether ``now`` is plainly another thing by its name. For the later drags of a call: an earlier drop can close
    a list up and bring the next card under the spot, which a plain surface on both looks wouldn't show."""
    name, was = _bare(_action_name(now) or now.name), _bare(_action_name(seen) or seen.name)
    if not name or not was or name == was or now.same_thing(seen):
        return False
    return (now.kind, now.name) not in seen.chain


def path(start: Spot, end: Spot, steps: int = DRAG_STEPS) -> list[tuple[int, int]]:
    """The points the pointer goes through after the press: a few pixels toward the end first, then even steps
    all the way."""
    dx, dy = end.x - start.x, end.y - start.y
    distance = math.hypot(dx, dy)
    points = []
    if distance > START_PX:
        points.append((start.x + round(dx * START_PX / distance), start.y + round(dy * START_PX / distance)))
    points += [(start.x + round(dx * i / steps), start.y + round(dy * i / steps)) for i in range(1, steps + 1)]
    return [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]


def _still(a: np.ndarray, b: np.ndarray) -> bool:
    return a.shape == b.shape and float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()) < STILL_DIFF


@dataclass(frozen=True)
class _Seen:
    """What the risk check saw at both ends of one drag, kept on the arguments for ``describe_call`` and ``run``: the
    drag goes exactly where it looked, even if a newer screenshot came in meanwhile."""

    start: Target
    end: Target
    finding: RiskFinding | None
    start_spot: Spot
    end_spot: Spot


@dataclass(frozen=True)
class _Dragged:
    picked: Target
    dropped: Target
    start: Spot
    end: Spot


class DragStep(_SpotArgs):
    """One drag: press at x and y (or element), let go at to_x and to_y (or to_element)."""

    to_x: int | None = Field(default=None, description="Where to let go, like x, in 'to_image'.")
    to_y: int | None = Field(default=None, description="Where to let go, like y, in 'to_image'.")
    to_image: Literal["screenshot", "zoom"] = "screenshot"
    to_element: int | None = Field(default=None, ge=1, description="A find_on_screen match to let go on.")
    label: str = Field(default="", description="What's dragged: 'the card “Mean”'.")
    to_label: str = Field(default="", description="Where it goes: 'the bucket “Center”'.")

    @field_validator("label", "to_label")
    @classmethod
    def _short_label(cls, value: str) -> str:
        return value[:LABEL_CHARS].strip()

    @model_validator(mode="after")
    def _both_ends(self) -> Self:
        self._check_spot(required=True)
        has_xy = self.to_x is not None or self.to_y is not None
        if self.to_element is not None and has_xy:
            raise ValueError("give either to_x and to_y, or to_element, not both")
        if has_xy and (self.to_x is None or self.to_y is None):
            raise ValueError("give both to_x and to_y")
        if self.to_element is None and not has_xy:
            raise ValueError("give to_x and to_y, or a to_element number from find_on_screen, for where to let go")
        return self

    @property
    def destination(self) -> _SpotArgs:
        return _SpotArgs.model_construct(x=self.to_x, y=self.to_y, image=self.to_image, element=self.to_element)


class DragArgs(DragStep):
    risky: bool = Field(
        default=False, description="True when you know the drag deletes, moves files or can't easily be undone."
    )
    then: list[DragStep] = Field(
        default_factory=list,
        max_length=MAX_DRAGS - 1,
        description="More drags after this one, in order, each with fields as above, aimed in the same pictures and "
        "find_on_screen results.",
    )
    look: bool = Field(
        default=False,
        description="True to get a small picture once the last drop settled; it works as a look_at_screen image.",
    )
    _seen: tuple[_Seen, ...] = PrivateAttr(default=())

    @property
    def steps(self) -> list[DragStep]:
        return [self, *self.then]


class Drag(_Control):
    name = "drag"
    description = (
        "Drag and drop with the mouse, for what a click can't do (sort cards into buckets, reorder a list, move a "
        "slider). Aim both ends like click: element (from the latest find_on_screen) or x and y; to_element or "
        "to_x and to_y. For several drags (sort all the cards), put the rest in 'then'; it stops if a drop moved "
        "what the next one starts on. look=true instead of calling look_at_screen to check it landed. The system "
        "asks the user itself before risky drags (files, a drop on Trash, between windows); make a risky drag on "
        "its own. " + TOOK_OVER_RULE
    )
    Args = DragArgs
    timeout_s = 90.0

    def __init__(
        self,
        watch: Watch | None = None,
        inspector: Inspector | None = None,
        memory: ScreenMemory | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        capture: Capture | None = None,
    ) -> None:
        super().__init__(watch, inspector, memory, sleep)
        self._capture = capture

    def _ends(self, step: DragStep) -> tuple[Spot, Spot] | str:
        """Both ends on the desktop, or why there aren't (for the model)."""
        start = self._locate(step)
        if isinstance(start, str):
            return start
        end = self._locate(step.destination)
        if isinstance(end, str):
            return f"Where to let go: {end}"
        if (start.x, start.y) == (end.x, end.y):
            return "Both ends are the same spot. Give where to let go, or use click to click it."
        return start, end

    def _all_ends(self, args: DragArgs) -> list[tuple[Spot, Spot]] | str:
        found = []
        for i, step in enumerate(args.steps):
            ends = self._ends(step)
            if isinstance(ends, str):
                return ends if i == 0 else f"Drag {i + 1}: {ends}"
            found.append(ends)
        return found

    async def inspect_risk(self, args: DragArgs, ctx: ToolContext) -> RiskFinding | None:
        if current_turn(ctx) is None:
            return None  # it won't run: precheck refuses it
        ends = self._all_ends(args)
        if isinstance(ends, str):
            return None  # likewise
        pids = avatar_pids(ctx)
        seen = []
        for step, (start, end) in zip(args.steps, ends, strict=True):
            picked = await self.inspector.point(start.x, start.y, pids)
            dropped = await self.inspector.point(end.x, end.y, pids)
            finding = classify_drag(picked, dropped, step.label, step.to_label, args.risky)
            seen.append(_Seen(picked, dropped, finding, start, end))
        args._seen = tuple(seen)
        finding = next((s.finding for s in seen if s.finding is not None), None)
        if finding is not None and args.look:
            return dataclasses.replace(finding, reason=f"{finding.reason}, and sends a small picture of the screen")
        return finding or (_LOOKING if args.look else None)

    async def precheck(self, args: DragArgs, ctx: ToolContext) -> str | None:
        if problem := self._refusal(ctx):
            return problem
        ends = self._all_ends(args)
        if isinstance(ends, str):
            return ends
        if len(args.steps) > 1 and any(s.finding is not None for s in args._seen):
            return ONE_AT_A_TIME
        return None

    async def describe_call(self, args: DragArgs, ctx: ToolContext) -> str:
        seen = args._seen[0] if args._seen else None
        what = (_name_of(seen.start) if seen is not None else "") or args.label or "it"
        where = (_name_of(seen.end) if seen is not None else "") or args.to_label or "that spot"
        app = app_name(seen.start) if seen is not None else ""
        more = f" and make {len(args.then)} more drags" if args.then else ""
        return f"drag {what} to {where}{more}" + (f" in {app}" if app else "")

    async def _act(self, args: DragArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        steps = args.steps
        if len(steps) > 1 and any(s.finding is not None for s in args._seen):
            return ToolResult.error(ONE_AT_A_TIME)
        done: list[_Dragged] = []
        problem = None
        for i, step in enumerate(steps):
            seen = args._seen[i] if i < len(args._seen) else None
            dragged = await self._drag(ctx, turn, step, seen, args.risky, later=i > 0)
            if isinstance(dragged, str):
                problem = dragged
                break
            done.append(dragged)
            if i < len(steps) - 1 or not args.look:
                await self.sleep(DROP_S)
        content = ""
        if done:
            app = app_name(done[0].picked)
            content = "Dragged " + "; ".join(f"{thing(d.picked)} to {thing(d.dropped)}" for d in done)
            content += f" in {app}." if app else "."
            if problem:
                content += f" Then I stopped at drag {len(done) + 1} of {len(steps)}: {problem}"
        else:
            content = problem or ""
        data: dict[str, Any] = {"drags": [{"from": [d.start.x, d.start.y], "to": [d.end.x, d.end.y]} for d in done]}
        # No picture once the user took over, interrupted or the turn ended: the screen is theirs again.
        if args.look and (done or problem is None) and self._stop(ctx, turn) is None:
            seen_now = await self._after_picture(ctx)
            if isinstance(seen_now, str):
                content += f" {seen_now}"
            else:
                content += f" {seen_now[0]}"
                data.update(seen_now[1])
        elif not problem:
            content += " Look at the screen to check it landed."
        return ToolResult.error(content, data) if problem else ToolResult.success(content, data)

    async def _drag(
        self, ctx: ToolContext, turn: TurnInfo, step: DragStep, seen: _Seen | None, risky: bool, later: bool
    ) -> _Dragged | str:
        """One drag of the call, checked at both ends again right before it; why it didn't (or only partly) happen
        otherwise. ``later``: not the call's first, so an earlier drop may have moved what it starts on."""
        ends = (seen.start_spot, seen.end_spot) if seen is not None else self._ends(step)
        if isinstance(ends, str):
            return ends
        start, end = ends
        if problem := self._stop(ctx, turn):
            return problem
        pids = avatar_pids(ctx)
        label = step.label or (_name_of(seen.start) if seen is not None else "")
        pointed = self._point(ctx, start, start.box or _pointing_box(seen.start if seen is not None else None), label)
        if pointed:
            await self.sleep(POINT_WAIT_S)
            if problem := self._stop(ctx, turn):
                return problem
        # Still what the risk check saw? The user may have taken a while to answer, or the page moved on.
        picked = await self.inspector.point(start.x, start.y, pids)
        dropped = await self.inspector.point(end.x, end.y, pids)
        found = classify_drag(picked, dropped, step.label, step.to_label, risky)
        if seen is None:  # the risk check didn't run, so nobody was asked
            if found is not None:
                return _needs_yes(found)
        elif not picks_up(picked, seen.start, step.label, start.name) or (later and _named_else(picked, seen.start)):
            log.info("Drag refused, the start changed: checked %r, now %r", seen.start, picked)
            return (
                f"The screen changed where I'd pick it up: it's {thing(picked)} now, so I didn't drag. "
                "Look at the screen again first."
            )
        elif found is not None and (seen.finding is None or _kind(found) != _kind(seen.finding)):
            return f"That drag now {found.reason} (it ends on {thing(dropped)}), so I didn't drag. Look again first."
        for spot in (start, end):
            if await asyncio.to_thread(self._avatar_under, spot.x, spot.y, pids):
                return "The avatar is in the way of that drag, so I didn't drag. Try again."
        if problem := self._stop(ctx, turn):
            return problem
        try:
            where = await asyncio.to_thread(self.desktop.move, start.x, start.y)
        except OSError as exc:
            return f"I couldn't move the pointer there ({exc})."
        await self.sleep(HOVER_S)
        self.watch.acted(turn.id, where)
        if problem := self._stop(ctx, turn):
            return problem
        if pointed:
            self._press(ctx)  # she jabs at it as it's picked up
        try:
            problem = await self._carry(ctx, turn, start, end)
        except OSError as exc:
            problem = f"Windows didn't take the drag ({exc}), so I let go."
        await self.sleep(SETTLE_S)
        if problem == TAKEN_OVER:
            self._stop(ctx, turn)  # the watch sees the pointer away from where she left it: taken over for the turn
            return f"The user moved the mouse while I was dragging, so I let go. {TAKEN_OVER}"
        try:
            self.watch.acted(turn.id, self.desktop.cursor())
        except OSError:
            log.debug("Couldn't read the mouse after a drag", exc_info=True)
        if problem:
            return f"I let go partway. {problem}"
        return _Dragged(picked, dropped, start, end)

    async def _carry(self, ctx: ToolContext, turn: TurnInfo, start: Spot, end: Spot) -> str | None:
        """Press where the pointer is (``start``), move to ``end`` in steps, let go there. Returns why she let go
        early, or None. Raises OSError when Windows doesn't take the input; the button is let go all the same."""
        held = False
        try:
            self.desktop.button("left", True)
            held = True
            await self.sleep(PRESS_S)
            at = (start.x, start.y)
            for x, y in path(start, end):
                if problem := self._while_held(ctx, turn, at):
                    return problem
                at = await asyncio.to_thread(self.desktop.move, x, y)
                await self.sleep(STEP_S)
            await self.sleep(HOVER_S)  # a moment over the target: pages mark where it'll go as the pointer rests
            return None
        finally:
            if held:
                self._let_go()

    def _while_held(self, ctx: ToolContext, turn: TurnInfo, at: tuple[int, int]) -> str | None:
        """Why to let go before the next step: the turn is over or cut off, or the pointer isn't where she put it
        (the user moved the mouse)."""
        latest = current_turn(ctx)
        if latest is None or latest.id != turn.id:
            return NOT_IN_TURN
        if latest.interrupted:
            return INTERRUPTED
        if math.dist(self.desktop.cursor(), at) > MOVED_PX:
            return TAKEN_OVER
        return None

    def _let_go(self) -> None:
        try:
            self.desktop.button("left", False)
        except Exception:
            log.warning("Letting go of the mouse button after a drag failed", exc_info=True)

    async def _settled(self) -> tuple[np.ndarray, Area, Rect]:
        """The active window once nothing in it moves any more (the drop's animation is over), or as it is after
        ``MAX_STILL_LOOKS`` pictures. Its pixels, the area really captured and where it is on the desktop."""
        capture = self._capture or capture_screen
        last = None
        for looked in range(MAX_STILL_LOOKS):
            pixels, area, *where = await asyncio.to_thread(capture, "window")
            rect: Rect = where[0] if where else (0, 0, pixels.shape[1], pixels.shape[0])
            small = await asyncio.to_thread(scale_down, pixels, (COMPARE_SIDE, COMPARE_SIDE))
            if (last is not None and _still(small, last)) or looked == MAX_STILL_LOOKS - 1:
                break
            last = small
            await self.sleep(STILL_EVERY_S)
        return pixels, area, rect

    async def _after_picture(self, ctx: ToolContext) -> tuple[str, dict[str, Any]] | str:
        """A small picture of the active window once the drop has settled, remembered like a look_at_screen one so
        the next drag can aim in it: (what to tell the model, the result's data), or why there's none."""
        ctx.events.emit(ScreenshotTaken(area="window"))
        try:
            pixels, area, rect = await self._settled()
        except Exception as exc:  # OSError from ctypes, ScreenUnavailable, anything else GDI throws
            log.warning("The picture after a drag failed", exc_info=True)
            return f"I couldn't take a picture afterwards ({exc}); look at the screen to check it landed."
        taken = self.memory.now()
        height, width = pixels.shape[:2]
        small = await asyncio.to_thread(scale_down, pixels, fit_for_claude(width, height, max_tokens=AFTER_TOKENS))
        data = await asyncio.to_thread(encode_image, small)
        width, height = small.shape[1], small.shape[0]
        self.memory.remember_shot(Screenshot(pixels, rect, width, height, area, taken))
        image = attachment(data)
        said = (
            f"A picture of {_WHAT.get(area, 'the screen')} after the drop settled is attached ({width}x{height} "
            "pixels): check it landed there, and aim the next drag or click in it like a look_at_screen image. Never "
            "read out secrets you can see in it."
        )
        return said, {"image": image, "width": width, "height": height}


TOOLS = [Drag]
