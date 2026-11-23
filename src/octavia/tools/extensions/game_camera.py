"""Game camera: learning how far a mouse count turns the view, and looking at an exact pitch (straight down, level,
straight up) or turning by degrees, in games like Minecraft that turn the camera by raw mouse motion.

Turning by counts alone was guesswork: how far a count turns depends on the game's sensitivity, and one big move
sent at once can be cut short. A game that doesn't read raw input takes the pointer's own motion instead, recentring
it once a frame, so a jump of 1500 counts stops at the edge of the screen. Every move here goes in steps of at most
``STEP_COUNTS``, one per ``TURN_STEP_S`` (about a frame), so the game takes each one in before the next.

Pitch has a hard limit at straight up and straight down, which makes it measurable without knowing anything: turn
far past the limit and the view stops exactly there. ``game_calibrate_look`` does that, then turns down a known
number of counts; the model reads the pitch the game shows (Minecraft's F3 screen) and the counts per degree are
stored in ``data/games/camera.json``, per game. ``game_aim`` then goes past the nearest limit and comes back the
right number of counts, which lands on an exact pitch however the view was facing. Minecraft has one sensitivity for
both axes, so the same number turns left and right by degrees.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from pydantic import Field, model_validator

from octavia.core.jsonfile import read_json, write_json
from octavia.tools.base import ToolArgs, ToolContext, ToolResult, TurnInfo
from octavia.tools.extensions.computer_control import NOT_IN_TURN
from octavia.tools.extensions.game_controls import MAX_TURN, TURN_STEP_S, Plan, _Game

if TYPE_CHECKING:
    from octavia.config import AppConfig

log = logging.getLogger(__name__)

FOLDER = "data/games"
FILE_NAME = "camera.json"
STEP_COUNTS = 100  # the most in one move: well within half a screen, even with the pointer's acceleration
CAL_COUNTS = 250  # the calibration's known turn: 37.5 degrees at Minecraft's default sensitivity, 150 at its highest
PITCH_LIMIT = 90.0
OVERSHOOT_DEG = 200.0  # past the whole range of pitch (180 degrees), with room to spare
DEFAULT_OVERSHOOT = 6000  # counts, before calibrating: past the whole range at all but the lowest sensitivities
DEFAULT_GAME = "Minecraft"
MAX_TURN_DEG = 720.0
_GAME_FIELD = Field(
    default=DEFAULT_GAME, min_length=1, max_length=60, description="Which game, for its own calibration."
)


# --------------------------------------------------------------------------- the stored calibration


def _path(config: AppConfig) -> Path:
    return config.resolve_path(FOLDER) / FILE_NAME


def _key(game: str) -> str:
    return " ".join(game.split()).casefold()


def _read(config: AppConfig) -> dict[str, Any]:
    stored = read_json(_path(config), "the camera calibration")
    return stored if isinstance(stored, dict) else {}


def load_counts(config: AppConfig, game: str) -> float | None:
    """Mouse counts per degree of turn in ``game``, or None before it's calibrated."""
    entry = _read(config).get(_key(game))
    value = entry.get("counts_per_degree") if isinstance(entry, dict) else None
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def save_counts(config: AppConfig, game: str, counts_per_degree: float) -> None:
    """Store the calibration, keeping every other game's. Raises OSError when it can't be written."""
    stored = _read(config)
    stored[_key(game)] = {"game": game.strip(), "counts_per_degree": round(counts_per_degree, 4)}
    write_json(_path(config), stored, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- moving


def smooth(dx: int, dy: int) -> Plan:
    """A move in steps of at most ``STEP_COUNTS``, one every ``TURN_STEP_S``."""
    steps = max(1, math.ceil(max(abs(dx), abs(dy)) / STEP_COUNTS))
    return Plan(seconds=steps * TURN_STEP_S, dx=dx, dy=dy, steps=steps)


def overshoot(counts_per_degree: float | None) -> int:
    """Counts that surely go past a pitch limit from anywhere."""
    if counts_per_degree is None:
        return DEFAULT_OVERSHOOT
    return min(MAX_TURN, math.ceil(OVERSHOOT_DEG * counts_per_degree))


def pitch_moves(pitch: float, counts_per_degree: float | None) -> list[tuple[int, int]]:
    """The moves that end at ``pitch`` (-90 up, 90 down): past the nearer limit, then back from it. Only the limits
    themselves can be reached without ``counts_per_degree``."""
    down = pitch > 0
    moves = [(0, overshoot(counts_per_degree) * (1 if down else -1))]
    if abs(pitch) < PITCH_LIMIT:
        assert counts_per_degree is not None
        back = round((PITCH_LIMIT - abs(pitch)) * counts_per_degree)
        if back:
            moves.append((0, -back if down else back))
    return moves


def pitch_words(pitch: float) -> str:
    if pitch <= -PITCH_LIMIT:
        return "straight up"
    if pitch >= PITCH_LIMIT:
        return "straight down"
    if pitch == 0:
        return "straight ahead"
    return f"{abs(pitch):g} degrees {'down' if pitch > 0 else 'up'}"


def turn_words(degrees: float) -> str:
    return f"{abs(degrees):g} degrees {'right' if degrees > 0 else 'left'}"


class _Camera(_Game):
    async def _moves(self, ctx: ToolContext, turn: TurnInfo, moves: list[tuple[int, int]]) -> str | None:
        """Make the moves one after the other, then let the input settle. Returns why it stopped early, or None."""
        problem = None
        try:
            for dx, dy in moves:
                _, stopped = await self._play(ctx, turn, smooth(dx, dy))
                if stopped:
                    problem = f"I stopped turning partway. {stopped}"
                    break
        except OSError as exc:
            problem = f"Windows didn't take the mouse movement ({exc})."
        await self._settle(turn)
        return problem


# ------------------------------------------------------------------ game_calibrate_look


class GameCalibrateLookArgs(ToolArgs):
    pitch: float | None = Field(
        default=None,
        ge=-PITCH_LIMIT,
        le=PITCH_LIMIT,
        description="Leave out to start; then, to finish, the pitch the game shows (Minecraft: F3 'Facing', the "
        "second number).",
    )
    game: str = _GAME_FIELD


class GameCalibrateLook(_Camera):
    name = "game_calibrate_look"
    description = (
        "Learn how far a mouse count turns the camera in a game like Minecraft, so game_aim can aim at an exact "
        "pitch and turn by degrees. Close menus first. Call it without pitch, read the pitch on screen (Minecraft: "
        "F3, then F3 again after), and call again with it. Stored per game: redo only after the game's mouse "
        "sensitivity changes."
    )
    Args = GameCalibrateLookArgs

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pending: dict[str, int] = {}  # game -> counts turned down from straight up, waiting for the pitch

    async def describe_call(self, args: GameCalibrateLookArgs, ctx: ToolContext) -> str:
        if args.pitch is not None:
            return f"store the camera calibration for {args.game}"
        return f"look straight up, then {CAL_COUNTS} counts down"

    async def _act(self, args: GameCalibrateLookArgs, ctx: ToolContext) -> ToolResult:
        key = _key(args.game)
        if args.pitch is not None:
            return self._finish(args, ctx, key)
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        if problem := self._stop(ctx, turn):
            return ToolResult.error(problem)
        self._pending.pop(key, None)
        upward = overshoot(load_counts(ctx.config, args.game))
        if problem := await self._moves(ctx, turn, [(0, -upward), (0, CAL_COUNTS)]):
            return ToolResult.error(problem)
        self._pending[key] = CAL_COUNTS
        return ToolResult.success(
            f"Looked straight up, then turned {CAL_COUNTS} counts down. Now read the pitch on screen (in Minecraft "
            "press F3: 'Facing' ends with (yaw / pitch)), then call game_calibrate_look again with that pitch.",
            data={"counts": CAL_COUNTS},
        )

    def _finish(self, args: GameCalibrateLookArgs, ctx: ToolContext, key: str) -> ToolResult:
        assert args.pitch is not None
        counts = self._pending.get(key)
        if counts is None:
            return ToolResult.error("Call game_calibrate_look without a pitch first, then read the pitch it leaves.")
        if args.pitch >= PITCH_LIMIT - 0.5:
            self._pending.pop(key, None)
            return ToolResult.error(
                f"That's straight down: {counts} counts went past the whole range, so I can't tell how far they "
                "turn. Is the game's mouse inverted? Turn that off, then start again."
            )
        degrees = args.pitch + PITCH_LIMIT
        if degrees < 0.5:
            self._pending.pop(key, None)
            return ToolResult.error(
                "The view barely moved down. Is a menu open, or is the game in another window? Close it and start "
                "again."
            )
        per_degree = counts / degrees
        self._pending.pop(key, None)
        try:
            save_counts(ctx.config, args.game, per_degree)
        except OSError as exc:
            return ToolResult.error(f"I measured {per_degree:.2f} counts per degree but couldn't store it ({exc}).")
        return ToolResult.success(
            f"Calibrated {args.game.strip()}: {per_degree:.2f} mouse counts per degree ({counts} counts turned "
            f"{degrees:g} degrees). game_aim uses it from now on.",
            data={"game": args.game.strip(), "counts_per_degree": round(per_degree, 4)},
        )


# ------------------------------------------------------------------ game_aim


class GameAimArgs(ToolArgs):
    pitch: float | None = Field(
        default=None,
        ge=-PITCH_LIMIT,
        le=PITCH_LIMIT,
        description="Degrees as Minecraft's F3 shows: -90 straight up, 0 level, 90 straight down.",
    )
    turn: float | None = Field(
        default=None,
        ge=-MAX_TURN_DEG,
        le=MAX_TURN_DEG,
        description="Degrees right (negative: left); 180 turns around.",
    )
    game: str = _GAME_FIELD

    @model_validator(mode="after")
    def _something_to_do(self) -> Self:
        if self.pitch is None and not self.turn:
            raise ValueError("give pitch, turn or both")
        return self


class GameAim(_Camera):
    name = "game_aim"
    description = (
        "Point the camera exactly in a game like Minecraft (pitch=90 to dig down or place a block under you). "
        "Straight up and down work any time; other pitches and turns need game_calibrate_look once."
    )
    Args = GameAimArgs

    async def describe_call(self, args: GameAimArgs, ctx: ToolContext) -> str:
        return "point the view " + self._words(args)

    async def _act(self, args: GameAimArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        if problem := self._stop(ctx, turn):
            return ToolResult.error(problem)
        per_degree = load_counts(ctx.config, args.game)
        exact_pitch = args.pitch is not None and abs(args.pitch) < PITCH_LIMIT
        if per_degree is None and (args.turn or exact_pitch):
            return ToolResult.error(
                f"I don't know yet how far a mouse count turns in {args.game.strip()}: run game_calibrate_look first. "
                "Straight up and straight down work without it."
            )
        moves = pitch_moves(args.pitch, per_degree) if args.pitch is not None else []
        if args.turn:
            assert per_degree is not None
            dx = round(args.turn * per_degree)
            if abs(dx) > MAX_TURN:
                return ToolResult.error(f"That's more than {MAX_TURN} mouse counts in one go: turn in smaller parts.")
            moves = [(dx, moves[0][1]), *moves[1:]] if moves else [(dx, 0)]
        if problem := await self._moves(ctx, turn, moves):
            return ToolResult.error(problem)
        return ToolResult.success(
            f"Pointed the view {self._words(args)}. Look at the screen to check.",
            data={"pitch": args.pitch, "turn": args.turn, "counts_per_degree": per_degree},
        )

    @staticmethod
    def _words(args: GameAimArgs) -> str:
        parts = [pitch_words(args.pitch)] if args.pitch is not None else []
        if args.turn:
            parts.append(f"after turning {turn_words(args.turn)}" if parts else turn_words(args.turn))
        return " ".join(parts)


TOOLS = [GameCalibrateLook, GameAim]
