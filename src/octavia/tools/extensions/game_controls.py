"""Game controls: she holds keys and mouse buttons for a while and turns the camera, so she can play real-time games
like Minecraft (walk forward for two seconds, sprint, jump, mine a block, look around).

``press_keys`` and ``click`` press and let go at once, which a game reads as a single frame's input: walking or
mining needs the key or button held down the whole time. Games that capture the cursor turn the camera by the
mouse's raw motion and ignore where the pointer is, so turning is a relative move (``_input.WindowsDesktop.nudge``),
in small steps spread over the time for a smooth turn.

On top of what every control tool does (only in a running turn, one action at a time, stopping once the user took
over):

* Everything held is let go in a ``finally``, without awaiting anything, so it happens even when the call fails,
  times out or is cancelled. Presses and releases run on the event loop rather than in a thread for the same
  reason: a cancelled call must never have a press land after its release. Holds are capped at ``MAX_HOLD_S``.
* What's held is judged the way ``press_keys`` and ``click`` judge it: holding Enter in Discord sends, holding a
  button in a console asks. Holding a button while turning drags, which asks over anything but a game's canvas.
* While holding, the takeover watch can't be used (her own Shift and mouse button look like the user's), so she lets
  go early when the turn is interrupted (the stop hotkey) or the active window changes.
* Watching while holding (``game_hold`` with ``watch``) takes small pictures as she goes (``_game_vision``) and
  lets go when a local check sees danger. The pictures go to Claude, so the first watched hold of a task is asked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import Field, PrivateAttr, field_validator, model_validator

from octavia.core.events import ScreenshotTaken
from octavia.core.text import spoken_list
from octavia.tools.base import RiskFinding, RiskLevel, ToolArgs, ToolContext, ToolResult, TurnInfo
from octavia.tools.extensions._game_vision import Eyes, times_said
from octavia.tools.extensions._input import KEYS, MODIFIERS, KeyCombo, Stroke, Watch, parse_keys
from octavia.tools.extensions._uia import Target
from octavia.tools.extensions.computer_control import (
    INTERRUPTED,
    NOT_IN_TURN,
    SETTLE_S,
    TOOK_OVER_RULE,
    Inspector,
    _Control,
    _needs_yes,
    _own_hotkey,
    app_name,
    classify_click,
    classify_keys,
    current_turn,
)
from octavia.tools.extensions.screen_capture import Capture, ScreenMemory, capture_screen
from octavia.tools.extensions.screen_pointing import avatar_pids

log = logging.getLogger(__name__)

MAX_HOLD_S = 30.0
MAX_LOOK_S = 5.0
MAX_TURN = 20_000  # mouse counts in one call: several full turns at any usual sensitivity
MAX_STEPS = 100
MAX_KEYS = 6
SLICE_S = 0.05  # between checks while holding
TURN_STEP_S = 0.02  # between the steps of a turn made while holding
# What a game draws in, as UI Automation sees it: dragging there turns the camera, not a file or a text selection.
_CANVAS = frozenset({"", "pane", "window"})
FOCUS_MOVED = "The active window changed, so I let go."
# Pictures of the screen leave the computer: asked like look_at_screen, once for the watched holds of a task.
_WATCHING = RiskFinding(RiskLevel.CONFIRM, "watch", "sends small pictures of the screen to Claude")


# --------------------------------------------------------------------------- what's held


@dataclass(frozen=True)
class Held:
    """Keys held down together: the modifiers (canonical names, in the order ctrl, alt, shift, win) and the rest."""

    modifiers: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()

    @property
    def vks(self) -> tuple[int, ...]:
        """The virtual keys to press, modifiers first."""
        return (*(MODIFIERS[m][0] for m in self.modifiers), *(KEYS[k][0] for k in self.keys))

    @property
    def combos(self) -> tuple[KeyCombo, ...]:
        """Each key with all the modifiers, as the risk check and the hotkey check judge them; the modifiers on
        their own when there's no other key."""
        if self.keys:
            return tuple(KeyCombo(self.modifiers, key) for key in self.keys)
        return (KeyCombo(self.modifiers, None),) if self.modifiers else ()

    @property
    def names(self) -> list[str]:
        return [*(MODIFIERS[m][1] for m in self.modifiers), *(KEYS[k][1] for k in self.keys)]


def held_keys(items: Sequence[str]) -> Held:
    """``["shift", "w"]`` or ``["shift+w"]`` -> ``Held``. Raises ValueError, with the names she can use, for anything
    else."""
    modifiers: set[str] = set()
    keys: list[str] = []
    for item in items:
        combo = parse_keys(item)
        modifiers.update(combo.modifiers)
        if combo.key is not None and combo.key not in keys:
            keys.append(combo.key)
    if {"ctrl", "alt"} <= modifiers and "delete" in keys:
        raise ValueError("Windows doesn't let apps press Ctrl+Alt+Delete.")
    return Held(tuple(m for m in MODIFIERS if m in modifiers), tuple(keys))


def _listed(names: Sequence[str]) -> str:
    return spoken_list(names)


def _seconds(seconds: float) -> str:
    return f"{seconds:g} second" + ("" if seconds == 1 else "s")


def _turn_words(dx: int, dy: int) -> str:
    """How a turn is said: "300 right and 50 up" (mouse counts; down is positive, as the mouse reports it)."""
    parts = []
    if dx:
        parts.append(f"{abs(dx)} {'right' if dx > 0 else 'left'}")
    if dy:
        parts.append(f"{abs(dy)} {'down' if dy > 0 else 'up'}")
    return " and ".join(parts)


def hold_risk(
    focus: Target, under: Target | None, held: Held, button: str | None = None, turning: bool = False
) -> RiskFinding | None:
    """Whether holding ``held`` with the keyboard focus on ``focus``, and ``button`` over ``under``, needs the
    user's yes, and why: as pressing the keys and clicking there would, and a drag over anything but a canvas."""
    for combo in held.combos:
        if (found := classify_keys(focus, combo)) is not None:
            return found
    if button is not None and under is not None:
        if (found := classify_click(under, button=button)) is not None:
            return found
        if turning and under.kind not in _CANVAS:
            return RiskFinding(RiskLevel.CONFIRM, "drag", "drags with the mouse, which can move things")
    return None


# --------------------------------------------------------------------------- holding, turning, letting go


@dataclass(frozen=True)
class Plan:
    """One go: keys and a button held for ``seconds``, while the mouse moves (dx, dy) in ``steps`` even steps."""

    vks: tuple[int, ...] = ()
    button: Literal["left", "right"] | None = None
    seconds: float = 0.0
    dx: int = 0
    dy: int = 0
    steps: int = 0


@dataclass(frozen=True)
class _Seen:
    """What the risk check saw, kept on the arguments for ``describe_call`` and ``run``."""

    focus: Target
    under: Target | None
    finding: RiskFinding | None


class _Game(_Control):
    """What both game tools share: pressing, holding, turning, and always letting go."""

    timeout_s = MAX_HOLD_S + 15.0

    def __init__(
        self,
        watch: Watch | None = None,
        inspector: Inspector | None = None,
        memory: ScreenMemory | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(watch, inspector, memory, sleep)
        self.clock = clock or time.monotonic

    async def _play(
        self, ctx: ToolContext, turn: TurnInfo, plan: Plan, look: Callable[[float], Awaitable[str | None]] | None = None
    ) -> tuple[float, str | None]:
        """Press, hold (turning as it goes), let go. Returns the seconds held and why she let go early (None when
        she didn't). Raises OSError when Windows doesn't take the input; whatever was pressed is let go all the
        same. ``look`` is awaited with the seconds held all along (``Eyes.look``): what it returns is a reason to
        let go early."""
        front = self.desktop.foreground()
        pressed: list[int] = []
        button: Literal["left", "right"] | None = None
        try:
            for vk in plan.vks:
                self.desktop.keys([Stroke(vk=vk)])
                pressed.append(vk)
            if plan.button is not None:
                self.desktop.button(plan.button, True)
                button = plan.button
            start = self.clock()
            step = done_x = done_y = 0
            while True:
                elapsed = self.clock() - start
                while step < plan.steps and elapsed >= plan.seconds * (step + 1) / plan.steps:
                    step += 1
                    x, y = round(plan.dx * step / plan.steps), round(plan.dy * step / plan.steps)
                    if (x, y) != (done_x, done_y):
                        self.desktop.nudge(x - done_x, y - done_y)
                        done_x, done_y = x, y
                if elapsed >= plan.seconds and step >= plan.steps:
                    return plan.seconds, None
                if problem := self._let_go_early(ctx, turn, front):
                    return elapsed, problem
                if look is not None and (problem := await look(elapsed)):
                    return elapsed, problem
                due = plan.seconds if step >= plan.steps else plan.seconds * (step + 1) / plan.steps
                await self.sleep(min(SLICE_S, max(due - elapsed, 0.001)))
        finally:
            self._let_go(pressed, button)

    def _let_go_early(self, ctx: ToolContext, turn: TurnInfo, front: int) -> str | None:
        latest = current_turn(ctx)
        if latest is None or latest.id != turn.id:
            return NOT_IN_TURN
        if latest.interrupted:
            return INTERRUPTED
        if self.desktop.foreground() != front:
            return FOCUS_MOVED
        return None

    def _let_go(self, pressed: Sequence[int], button: Literal["left", "right"] | None) -> None:
        """Let go of everything, one at a time, so one release Windows refuses doesn't keep the rest held."""
        if button is not None:
            try:
                self.desktop.button(button, False)
            except Exception:
                log.warning("Letting go of the %s mouse button failed", button, exc_info=True)
        for vk in reversed(pressed):
            try:
                self.desktop.keys([Stroke(vk=vk, up=True)])
            except Exception:
                log.warning("Letting go of key 0x%02X failed", vk, exc_info=True)

    async def _settle(self, turn: TurnInfo) -> None:
        """Her input taken in: the takeover watch's new baseline, where the pointer is now."""
        await self.sleep(SETTLE_S)
        try:
            self.watch.acted(turn.id, self.desktop.cursor())
        except OSError:
            log.debug("Couldn't read the mouse after a game action", exc_info=True)


# ------------------------------------------------------------------ game_hold


class GameHoldArgs(ToolArgs):
    keys: list[str] = Field(
        default_factory=list,
        max_length=MAX_KEYS,
        description="Keys held down together, e.g. ['w'] walks, ['shift', 'w'] sprints, ['w', 'space'] jumps forward.",
    )
    button: Literal["left", "right"] | None = Field(
        default=None, description="Mouse button held too: 'left' mines or attacks, 'right' uses or places."
    )
    seconds: float = Field(default=1.0, ge=0.05, le=MAX_HOLD_S, description="How long to hold.")
    turn_x: int = Field(
        default=0, ge=-MAX_TURN, le=MAX_TURN, description="Turn right (negative: left) while holding, mouse counts."
    )
    turn_y: int = Field(
        default=0, ge=-MAX_TURN, le=MAX_TURN, description="Look down (negative: up) while holding, mouse counts."
    )
    watch: bool = Field(
        default=False,
        description="Get small pictures taken while moving back as one strip, and let go early if a local check "
        "sees danger (view suddenly mostly blue: water or sky; the health bar dropping). Much cheaper than "
        "look_at_screen after every move.",
    )
    watch_every: float = Field(default=1.0, ge=0.5, le=2.0, description="With watch: seconds between pictures.")
    health_bar: list[float] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        description="With watch: the health bar's [x1, y1, x2, y2] in the latest look_at_screen image; lets go "
        "if it drops by a quarter.",
    )
    _seen: _Seen | None = PrivateAttr(default=None)

    @field_validator("keys")
    @classmethod
    def _known_keys(cls, value: list[str]) -> list[str]:
        return [parse_keys(item).text for item in value]

    @model_validator(mode="after")
    def _something_held(self) -> Self:
        if not self.keys and self.button is None:
            raise ValueError("give keys to hold, a mouse button, or both")
        held_keys(self.keys)  # keys that are fine one by one can still be Ctrl+Alt+Delete together
        if self.health_bar is not None and not self.watch:
            raise ValueError("health_bar only works with watch=true")
        return self

    @property
    def held(self) -> Held:
        return held_keys(self.keys)

    @property
    def what(self) -> str:
        return _listed([*self.held.names, *((f"the {self.button} mouse button",) if self.button else ())])


class GameHold(_Game):
    name = "game_hold"
    description = (
        "Hold keys and/or a mouse button for some seconds, for real-time games like Minecraft (button='left' "
        "mines the block in front), turning the camera smoothly with turn_x and turn_y. Everything is let go at "
        "the end. One action runs at a time, so combine what should happen together in one call. For a quick tap "
        "use press_keys or click. To see what happened, set watch=true rather than look_at_screen afterwards. "
        + TOOK_OVER_RULE
    )
    Args = GameHoldArgs

    def __init__(
        self,
        watch: Watch | None = None,
        inspector: Inspector | None = None,
        memory: ScreenMemory | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        clock: Callable[[], float] | None = None,
        capture: Capture | None = None,
    ) -> None:
        super().__init__(watch, inspector, memory, sleep, clock)
        self._capture = capture

    async def inspect_risk(self, args: GameHoldArgs, ctx: ToolContext) -> RiskFinding | None:
        if current_turn(ctx) is None:
            return None  # it won't run: precheck refuses it
        focus, under = await self._look(args, avatar_pids(ctx))
        finding = hold_risk(focus, under, args.held, args.button, bool(args.turn_x or args.turn_y))
        args._seen = _Seen(focus, under, finding)
        if finding is None and args.watch:
            return _WATCHING
        return finding

    async def precheck(self, args: GameHoldArgs, ctx: ToolContext) -> str | None:
        return self._refusal(ctx) or self._hotkey(ctx, args.held) or self._no_health_bar(args)

    async def describe_call(self, args: GameHoldArgs, ctx: ToolContext) -> str:
        app = app_name(args._seen.focus) if args._seen is not None else ""
        turning = _turn_words(args.turn_x, args.turn_y)
        return (
            f"hold {args.what} for {_seconds(args.seconds)}"
            + (f" while turning {turning}" if turning else "")
            + (f" in {app}" if app else "")
            + (" while watching the game" if args.watch else "")
        )

    async def _act(self, args: GameHoldArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        held = args.held
        if problem := self._hotkey(ctx, held) or self._stop(ctx, turn):
            return ToolResult.error(problem)
        pids = avatar_pids(ctx)
        try:
            focus, under = await self._look(args, pids)
            on_avatar = args.button is not None and self._avatar_under(*self.desktop.cursor(), pids)
        except OSError as exc:
            return ToolResult.error(f"I couldn't read the mouse ({exc}), so I didn't hold anything.")
        if on_avatar:
            return ToolResult.error("The avatar is under the pointer, so I didn't press the mouse button.")
        turning = bool(args.turn_x or args.turn_y)
        seen = args._seen
        if seen is None:  # the risk check didn't run, so nobody was asked
            found = hold_risk(focus, under, held, args.button, turning) or (_WATCHING if args.watch else None)
            if found is not None:
                return ToolResult.error(_needs_yes(found))
        elif focus.hwnd != seen.focus.hwnd or not focus.same_thing(seen.focus):
            where = app_name(focus) or "another window"
            return ToolResult.error(
                f"The keyboard focus moved since I checked it (it's in {where} now), so I didn't hold anything. "
                "Look again first."
            )
        elif under is not None and seen.under is not None and not under.same_thing(seen.under):
            return ToolResult.error(
                "Something else is under the pointer since I checked it, so I didn't hold anything. Look again first."
            )
        eyes = None
        if args.watch:
            if problem := self._no_health_bar(args):
                return ToolResult.error(problem)
            shot, box = self.memory.shot(), args.health_bar
            health = shot.box_to_screen((box[0], box[1], box[2], box[3])) if box and shot is not None else None
            eyes = Eyes(self._capture or capture_screen, args.watch_every, health)
            ctx.events.emit(ScreenshotTaken(area="window"))
        steps = max(1, round(args.seconds / TURN_STEP_S)) if turning else 0
        plan = Plan(held.vks, args.button, args.seconds, args.turn_x, args.turn_y, steps)
        try:
            seconds, problem = await self._play(ctx, turn, plan, eyes.look if eyes is not None else None)
        except OSError as exc:
            await self._settle(turn)
            return ToolResult.error(f"Windows didn't take the input ({exc}), so I let go of everything.")
        await self._settle(turn)
        watched, data = await self._seen_while_holding(eyes, problem)
        if problem and (eyes is None or problem != eyes.danger):
            return ToolResult.error(
                f"I held {args.what} for {seconds:.1f} of {args.seconds:g} seconds, then let go. {problem}{watched}",
                data={"seconds": round(seconds, 2), **data},
            )
        app = app_name(focus)
        turned = _turn_words(args.turn_x, args.turn_y)
        if problem:  # the danger check made her let go
            content = f"I held {args.what} for {seconds:.1f} of {args.seconds:g} seconds. {problem}"
        else:
            content = (
                f"Held {args.what} for {_seconds(args.seconds)}"
                + (f" while turning {turned}" if turned else "")
                + (f" in {app}" if app else "")
                + ", then let go."
            )
        return ToolResult.success(
            content + watched,
            data={"keys": list(args.keys), "button": args.button, "seconds": round(seconds, 2), "app": app, **data},
        )

    async def _seen_while_holding(self, eyes: Eyes | None, problem: str | None) -> tuple[str, dict[str, Any]]:
        """What watching adds to the result: a line about the strip of pictures, and the strip itself."""
        if eyes is None:
            return "", {}
        if problem is None or problem == eyes.danger:
            await eyes.last_look()
        picture = await asyncio.to_thread(eyes.picture)
        note = f" {eyes.problem}" if eyes.problem else ""
        if picture is None:
            return note, {}
        image, width, height, times = picture
        count = len(times)
        what = "a small picture" if count == 1 else f"{count} small pictures"
        order = "" if count == 1 else ", left to right, then top to bottom"
        line = f" Attached: {what} of the game ({width}x{height} in all{order}), taken {times_said(times)}.{note}"
        return line, {"image": image, "width": width, "height": height, "frames": count}

    def _no_health_bar(self, args: GameHoldArgs) -> str | None:
        """Why the health bar can't be found: its box is in a screenshot she no longer has."""
        if args.health_bar is not None and self.memory.shot() is None:
            return "I have no recent screenshot to find the health bar in. Look at the screen first, or leave it out."
        return None

    async def _look(self, args: GameHoldArgs, pids: frozenset[int]) -> tuple[Target, Target | None]:
        """What has the keyboard focus, and what's under the pointer when a button will be held."""
        focus = await self.inspector.focus(pids)
        if args.button is None:
            return focus, None
        x, y = self.desktop.cursor()
        return focus, await self.inspector.point(x, y, pids)

    @staticmethod
    def _hotkey(ctx: ToolContext, held: Held) -> str | None:
        return next((problem for combo in held.combos if (problem := _own_hotkey(ctx, combo))), None)


# ------------------------------------------------------------------ game_look


class GameLookArgs(ToolArgs):
    dx: int = Field(default=0, ge=-MAX_TURN, le=MAX_TURN, description="Turn right (negative: left), in mouse counts.")
    dy: int = Field(default=0, ge=-MAX_TURN, le=MAX_TURN, description="Look down (negative: up), in mouse counts.")
    steps: int = Field(default=1, ge=1, le=MAX_STEPS, description="More than 1 for a smooth turn in small moves.")
    seconds: float = Field(default=0.3, ge=0, le=MAX_LOOK_S, description="How long a turn of several steps takes.")

    @model_validator(mode="after")
    def _some_turn(self) -> Self:
        if not self.dx and not self.dy:
            raise ValueError("give dx, dy or both")
        return self


class GameLook(_Game):
    name = "game_look"
    description = (
        "Turn the camera in a game that captures the mouse, like Minecraft, with raw mouse motion (moving the "
        "pointer to a spot does nothing there). A count's turn depends on the game's sensitivity: start with a "
        "few hundred, then look_at_screen and adjust. To turn while holding keys or a button, use game_hold's "
        "turn_x and turn_y. To look straight down, level or at an exact pitch, or turn by degrees, use game_aim."
    )
    Args = GameLookArgs

    async def describe_call(self, args: GameLookArgs, ctx: ToolContext) -> str:
        return f"turn the view {_turn_words(args.dx, args.dy)}"

    async def _act(self, args: GameLookArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        if problem := self._stop(ctx, turn):
            return ToolResult.error(problem)
        seconds = args.seconds if args.steps > 1 else 0.0
        try:
            _, problem = await self._play(ctx, turn, Plan(seconds=seconds, dx=args.dx, dy=args.dy, steps=args.steps))
        except OSError as exc:
            await self._settle(turn)
            return ToolResult.error(f"Windows didn't take the mouse movement ({exc}).")
        await self._settle(turn)
        if problem:
            return ToolResult.error(f"I stopped turning partway. {problem}")
        return ToolResult.success(
            f"Turned {_turn_words(args.dx, args.dy)}. Look at the screen to see where it's facing now.",
            data={"dx": args.dx, "dy": args.dy, "steps": args.steps},
        )


TOOLS = [GameHold, GameLook]
