"""Game tunnel: digging a proper two-tall mining tunnel in Minecraft, one block at a time, with torches.

Tunnelling by holding W and the mouse button together, at whatever pitch the view happened to be, dug a staircase:
the view drifts, the player walks into half-mined blocks, and a block under the feet gets dug too. Each block here is
the same few exact moves, on the pitches ``game_aim`` lands on (so the camera needs ``game_calibrate_look`` first):

* the upper block, looking straight ahead: the eyes are 1.62 blocks up, in the upper block's row;
* the lower block, looking ``LOWER_PITCH`` down through the gap the upper one left. That ray meets the lower block's
  top wherever the player stands (0.3 to 1.3 blocks from the wall), and once that block is gone it goes on to the next lower
  block ahead rather than the floor, so mining a little too long never digs a hole to fall into. Aiming at the lower
  block's face instead would need a steeper look, which ends in the floor;
* a step forward sneaking (Shift+W): the next wall stops it after one block, and sneaking never walks off an edge
  into a cave, a ravine or lava.

Torches go down every few blocks, on the floor where she stands (torches don't block the way), switching to their
hotbar slot and back. She can't read the coordinates or spot ores herself: the result asks the model to look at the
screen (F3) for both.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, PrivateAttr, model_validator

from octavia.tools.base import RiskFinding, ToolArgs, ToolContext, ToolResult, TurnInfo
from octavia.tools.extensions._input import KEYS, TAP_S
from octavia.tools.extensions._uia import Target
from octavia.tools.extensions.computer_control import NOT_IN_TURN, _needs_yes, app_name, current_turn
from octavia.tools.extensions.game_camera import _GAME_FIELD, load_counts, pitch_moves, smooth
from octavia.tools.extensions.game_controls import Held, Plan, _Game, _Seen, held_keys, hold_risk
from octavia.tools.extensions.screen_pointing import avatar_pids

MAX_BLOCKS = 16
LOWER_PITCH = 35.0  # degrees down: through the upper gap onto the lower block's top, then on to the next one
STEP_S = 1.0  # sneaking covers a block in about 0.8 s; the next wall stops the rest
PLACE_S = 0.1  # a right-click held this long places one torch (holding repeats every 0.2 s)
MINE_S = 0.9  # stone with a stone pickaxe takes 0.6 s
MAX_MINE_S = 4.0
TIMEOUT_S = 300.0
_MARGIN_S = 15.0


# --------------------------------------------------------------------------- the moves


def _aim(pitch: float, per_degree: float) -> list[Plan]:
    return [smooth(dx, dy) for dx, dy in pitch_moves(pitch, per_degree)]


def _slot(slot: int) -> tuple[int, ...]:
    return (KEYS[str(slot)][0],)


def dig_plans(per_degree: float, mine_s: float) -> list[Plan]:
    """One block forward: the upper block, the lower one, then a sneaking step into the gap."""
    return [
        *_aim(0.0, per_degree),
        Plan(button="left", seconds=mine_s),
        *_aim(LOWER_PITCH, per_degree),
        Plan(button="left", seconds=mine_s),
        Plan(vks=held_keys(["shift", "w"]).vks, seconds=STEP_S),
    ]


def torch_plans(torch_slot: int, tool_slot: int, per_degree: float) -> list[Plan]:
    """A torch on the floor underfoot, then the pickaxe back in hand."""
    return [
        Plan(vks=_slot(torch_slot), seconds=TAP_S),
        *_aim(90.0, per_degree),
        Plan(button="right", seconds=PLACE_S),
        Plan(vks=_slot(tool_slot), seconds=TAP_S),
    ]


def _seconds(plans: list[Plan]) -> float:
    return sum(plan.seconds for plan in plans)


# ------------------------------------------------------------------ game_tunnel


class GameTunnelArgs(ToolArgs):
    blocks: int = Field(default=8, ge=1, le=MAX_BLOCKS, description="How many blocks forward to dig.")
    mine_seconds: float = Field(
        default=MINE_S,
        ge=0.2,
        le=MAX_MINE_S,
        description="Seconds per block: stone with a stone pickaxe 0.6, iron ore or deepslate 1.2 (iron pickaxe "
        "about half); gravel and dirt about 1 without a shovel.",
    )
    tool_slot: int | None = Field(default=None, ge=1, le=9, description="Hotbar slot with the pickaxe.")
    torch_slot: int | None = Field(
        default=None, ge=1, le=9, description="Hotbar slot with torches, to place them (needs tool_slot)."
    )
    torch_every: int = Field(default=8, ge=1, le=MAX_BLOCKS, description="Place a torch every this many blocks.")
    game: str = _GAME_FIELD
    _seen: _Seen | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _torches_need_the_pickaxe_slot(self) -> Self:
        if self.torch_slot is not None:
            if self.tool_slot is None:
                raise ValueError("give tool_slot too, to take the pickaxe back after placing a torch")
            if self.torch_slot == self.tool_slot:
                raise ValueError("torch_slot and tool_slot must be different slots")
        return self

    @property
    def held(self) -> Held:
        slots = [str(slot) for slot in (self.tool_slot, self.torch_slot) if slot is not None]
        return held_keys(["shift", "w", *slots])

    def torches_at(self) -> list[int]:
        """The blocks after which a torch goes down."""
        if self.torch_slot is None:
            return []
        return list(range(self.torch_every, self.blocks + 1, self.torch_every))


class GameTunnel(_Game):
    name = "game_tunnel"
    description = (
        "Dig a straight two-block-tall tunnel forward in Minecraft, sneaking (never walks off an edge), with "
        "optional torches. For strip mining (iron is commonest around Y 16) and to get out of a shaft sideways. "
        "First: close menus, stand right against the wall, face straight along an axis (F3 yaw a multiple of 90; "
        "turn with game_aim). Needs game_calibrate_look once. It can't see: afterwards look_at_screen, check with "
        "F3 that the coordinates moved as expected, and look for ores (iron ore: stone with tan-orange spots), "
        "lava, water or caves ahead before digging on."
    )
    Args = GameTunnelArgs
    timeout_s = TIMEOUT_S

    async def inspect_risk(self, args: GameTunnelArgs, ctx: ToolContext) -> RiskFinding | None:
        if current_turn(ctx) is None:
            return None  # it won't run: precheck refuses it
        pids = avatar_pids(ctx)
        focus = await self.inspector.focus(pids)
        x, y = self.desktop.cursor()
        under = await self.inspector.point(x, y, pids)
        finding = self._risk(args, focus, under)
        args._seen = _Seen(focus, under, finding)
        return finding

    async def describe_call(self, args: GameTunnelArgs, ctx: ToolContext) -> str:
        app = app_name(args._seen.focus) if args._seen is not None else ""
        return f"dig a tunnel {args.blocks} block{'s' if args.blocks != 1 else ''} forward" + (
            f" in {app}" if app else ""
        )

    async def _act(self, args: GameTunnelArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        if problem := self._stop(ctx, turn):
            return ToolResult.error(problem)
        per_degree = load_counts(ctx.config, args.game)
        if per_degree is None:
            return ToolResult.error(
                f"I don't know yet how far a mouse count turns in {args.game.strip()}, and the tunnel needs exact "
                "pitches: run game_calibrate_look first."
            )
        dig = dig_plans(per_degree, args.mine_seconds)
        torch = torch_plans(args.torch_slot, args.tool_slot, per_degree) if args.torch_slot and args.tool_slot else []
        torches_at = args.torches_at()
        estimate = _seconds(dig) * args.blocks + _seconds(torch) * len(torches_at)
        if estimate > self.timeout_s - _MARGIN_S:
            return ToolResult.error(
                f"That would take about {estimate:.0f} seconds, more than one call may: dig fewer blocks at a time."
            )
        if problem := await self._checked(args, ctx):
            return ToolResult.error(problem)

        dug = placed = 0
        start = [Plan(vks=_slot(args.tool_slot), seconds=TAP_S)] if args.tool_slot else []
        problem = await self._run(ctx, turn, start)
        while problem is None and dug < args.blocks:
            if problem := self._stop(ctx, turn):
                break
            if problem := await self._run(ctx, turn, dig):
                break
            dug += 1
            if dug in torches_at:
                if problem := await self._run(ctx, turn, torch):
                    break
                placed += 1
        data = {"blocks": dug, "torches": placed, "asked": args.blocks}
        torches = f" and placed {placed} torch{'es' if placed != 1 else ''}" if placed else ""
        if problem:
            return ToolResult.error(f"I dug {dug} of {args.blocks} blocks{torches}, then stopped. {problem}", data=data)
        return ToolResult.success(
            f"Dug {dug} block{'s' if dug != 1 else ''} of a two-tall tunnel forward{torches}. I can't see the game: "
            "look at the screen now. Check the F3 coordinates moved that far (a block that didn't break, like "
            "falling gravel, leaves the tunnel short), and look for ores, lava, water or caves before digging on.",
            data=data,
        )

    @staticmethod
    def _risk(args: GameTunnelArgs, focus: Target, under: Target) -> RiskFinding | None:
        """As holding the keys and buttons would be judged: the pickaxe's left button, and the right for torches."""
        if (found := hold_risk(focus, under, args.held, "left", turning=True)) is not None:
            return found
        if args.torch_slot is not None:
            return hold_risk(focus, under, Held(), "right", turning=True)
        return None

    async def _checked(self, args: GameTunnelArgs, ctx: ToolContext) -> str | None:
        """Why not to start: the risk check's view no longer holds, or the avatar is under the pointer. None to go."""
        pids = avatar_pids(ctx)
        try:
            focus = await self.inspector.focus(pids)
            x, y = self.desktop.cursor()
            under = await self.inspector.point(x, y, pids)
            on_avatar = self._avatar_under(x, y, pids)
        except OSError as exc:
            return f"I couldn't read the mouse ({exc}), so I didn't dig."
        if on_avatar:
            return "The avatar is under the pointer, so I didn't press the mouse button."
        seen = args._seen
        if seen is None:  # the risk check didn't run, so nobody was asked
            if (found := self._risk(args, focus, under)) is not None:
                return _needs_yes(found)
        elif focus.hwnd != seen.focus.hwnd or not focus.same_thing(seen.focus):
            where = app_name(focus) or "another window"
            return f"The keyboard focus moved since I checked it (it's in {where} now), so I didn't dig. Look again."
        return None

    async def _run(self, ctx: ToolContext, turn: TurnInfo, plans: list[Plan]) -> str | None:
        """Play the plans one after the other, then let the input settle. Returns why it stopped early, or None."""
        problem = None
        try:
            for plan in plans:
                _, stopped = await self._play(ctx, turn, plan)
                if stopped:
                    problem = stopped
                    break
        except OSError as exc:
            problem = f"Windows didn't take the input ({exc}), so I let go of everything."
        await self._settle(turn)
        return problem


TOOLS = [GameTunnel]
