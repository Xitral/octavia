"""Razer mouse lighting through the Razer Chroma SDK's local REST API: a color, off, breathing, a flash, or back to
the lighting Synapse shows on its own.

Synapse runs a small web server on the computer (``localhost:54235``) while Chroma is on. An app opens a session
there (``POST /razer/chromasdk``), which answers with the session's own address; effects are ``PUT`` to
``<session>/mouse``, and the session ends with ``DELETE <session>``. Chroma closes a session it hasn't heard from in
15 seconds and shows Synapse's lighting again, so while she holds a color a heartbeat keeps the session open; going
back to the default lighting is simply ending it. A session is shown on top of Synapse's own lighting while it's open.

The REST API only has a static color (and none) for a mouse, so breathing and flashing are animated here, frame by
frame. A flash puts back whatever was showing before it, so it serves as an alert (a scheduled job can flash the
mouse when something is due) without undoing the color the user picked. The session lives only while she runs: when
she quits, Chroma ends it and the mouse goes back to its usual lighting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

CHROMA_URL = "http://localhost:54235/razer/chromasdk"
TIMEOUT = httpx.Timeout(5.0, connect=2.0)
HEARTBEAT_S = 5.0  # well inside Chroma's 15 s
READY_TRIES = 6  # a new session's address takes a moment to start answering
READY_S = 0.5
FRAME_S = 1 / 15
BREATH_S = 4.0  # one breath, in and out
BREATH_FLOOR = 0.06  # the dimmest a breath gets, so it never looks switched off
BLINK_S = 0.25

APP_INFO: dict[str, Any] = {
    "title": "Octavia",
    "description": "Octavia, a voice assistant, sets the mouse lighting when asked.",
    "author": {"name": "Octavia", "contact": "localhost"},
    "device_supported": ["mouse"],
    "category": "application",
}

NOT_RUNNING = (
    "Razer Chroma isn't running, so the mouse lighting can't be changed. Open Razer Synapse and make sure Chroma "
    "(and its Chroma Connect / app lighting option) is on, then try again."
)

# The Chroma SDK's result codes worth putting in words; others are given as the number.
RESULT_TEXT = {
    5: "Chroma refused access",
    87: "Chroma rejected the request",
    1062: "Chroma's service isn't active",
    1167: "no Razer mouse is connected",
    1168: "Chroma couldn't find a Razer mouse",
    4309: "app lighting is turned off in Synapse (Chroma Connect)",
    4319: "the Razer mouse isn't available",
}

COLORS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "yellow": (255, 200, 0),
    "orange": (255, 90, 0),
    "purple": (140, 0, 255),
    "violet": (140, 0, 255),
    "pink": (255, 40, 140),
    "magenta": (255, 0, 255),
    "cyan": (0, 255, 255),
    "teal": (0, 160, 140),
    "lime": (120, 255, 0),
    "gold": (255, 160, 0),
    "razer green": (68, 214, 44),
}

_HEX = re.compile(r"#?([0-9a-fA-F]{6})")

RGB = tuple[int, int, int]


def parse_color(text: str) -> RGB | None:
    """A color name ("red", "light blue" is taken as "blue") or "#RRGGBB"; None when it's neither."""
    text = text.strip().casefold()
    if match := _HEX.fullmatch(text):
        value = int(match.group(1), 16)
        return (value >> 16) & 255, (value >> 8) & 255, value & 255
    if text in COLORS:
        return COLORS[text]
    words = [w for w in re.split(r"[\s-]+", text) if w in COLORS]
    return COLORS[words[-1]] if words else None


def chroma_color(rgb: RGB, level: float = 1.0) -> int:
    """Chroma's color number: blue, green, red from the high byte down (0x00BBGGRR), dimmed to ``level``."""
    r, g, b = (max(0, min(255, round(c * level))) for c in rgb)
    return r | (g << 8) | (b << 16)


class ChromaError(Exception):
    """Something the user should hear: Chroma isn't running, or it refused the effect."""


@dataclass(frozen=True)
class Look:
    """What the mouse shows while a session is open: a color, a breathing color, or nothing ("off")."""

    kind: Literal["color", "breathe", "off"]
    rgb: RGB = (0, 0, 0)


class MouseLights:
    """The one Chroma session she holds, its heartbeat and the breathing animation. Call on the event loop."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        heartbeat_s: float = HEARTBEAT_S,
        ready_s: float = READY_S,
        frame_s: float = FRAME_S,
        blink_s: float = BLINK_S,
    ) -> None:
        self._transport = transport
        self._heartbeat_s = heartbeat_s
        self._ready_s = ready_s
        self._frame_s = frame_s
        self._blink_s = blink_s
        self._client: httpx.AsyncClient | None = None
        self._session: str | None = None
        self._look: Look | None = None  # None: Synapse's own lighting
        self._keeper: asyncio.Task[None] | None = None
        self._animation: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def look(self) -> Look | None:
        return self._look

    def _http(self) -> httpx.AsyncClient:
        """Chroma is on this computer, so no proxy from the environment may stand in between."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport, trust_env=False)
        return self._client

    async def _open(self) -> str:
        try:
            response = await self._http().post(CHROMA_URL, json=APP_INFO)
            response.raise_for_status()
            uri = response.json().get("uri")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.info("Razer Chroma didn't open a session: %s", exc)
            raise ChromaError(NOT_RUNNING) from exc
        if not isinstance(uri, str) or not uri.startswith("http"):
            raise ChromaError(NOT_RUNNING)
        self._session = uri.rstrip("/")
        self._keeper = asyncio.create_task(self._keep_alive(self._session))
        return self._session

    def _forget(self) -> None:
        """The session is gone (ended, or Chroma closed it). What it showed is kept: a new session can show it."""
        self._session = None
        if self._keeper is not None and self._keeper is not asyncio.current_task():
            self._keeper.cancel()
        self._keeper = None

    async def _keep_alive(self, session: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_s)
            try:
                (await self._http().put(f"{session}/heartbeat")).raise_for_status()
            except httpx.HTTPError as exc:
                log.info("Razer Chroma session ended: %s", exc)
                if self._session == session:
                    await self._stop_animation()
                    self._forget()
                    self._look = None
                return

    async def _put(self, session: str, effect: dict[str, Any]) -> None:
        response = await self._http().put(f"{session}/mouse", json=effect)
        response.raise_for_status()
        try:
            result = response.json().get("result", 0)
        except (ValueError, AttributeError):
            result = 0
        if result:
            reason = RESULT_TEXT.get(result, f"Chroma answered error {result}")
            raise ChromaError(f"The mouse lighting couldn't be changed: {reason}.")

    async def _apply(self, effect: dict[str, Any]) -> None:
        """Show ``effect``, opening a session first when there's none. A new session's address can take a moment to
        answer, and one Chroma closed behind her back gets one fresh session."""
        fresh = self._session is None
        session = self._session or await self._open()
        tries = READY_TRIES if fresh else 1
        for attempt in range(tries):
            try:
                await self._put(session, effect)
                return
            except ChromaError:
                if fresh:  # a session that never showed anything isn't kept open
                    self._forget()
                raise
            except httpx.HTTPError as exc:
                if attempt + 1 < tries:
                    await asyncio.sleep(self._ready_s)
                    continue
                log.info("Razer Chroma didn't take the effect: %s", exc)
        self._forget()
        if fresh:
            raise ChromaError(NOT_RUNNING)
        await self._apply(effect)

    async def _show(self, look: Look, level: float = 1.0) -> None:
        if look.kind == "off":
            await self._apply({"effect": "CHROMA_NONE"})
        else:
            await self._apply({"effect": "CHROMA_STATIC", "param": {"color": chroma_color(look.rgb, level)}})

    async def _stop_animation(self) -> None:
        """Waited for, so a breath's last frame can't land after what's shown next."""
        animation, self._animation = self._animation, None
        if animation is not None and animation is not asyncio.current_task():
            animation.cancel()
            try:
                await animation
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise

    async def _breathe(self, rgb: RGB) -> None:
        started = asyncio.get_running_loop().time()
        try:
            while self._session is not None:
                phase = (asyncio.get_running_loop().time() - started) / BREATH_S
                level = BREATH_FLOOR + (1 - BREATH_FLOOR) * (1 - math.cos(2 * math.pi * phase)) / 2
                await self._apply({"effect": "CHROMA_STATIC", "param": {"color": chroma_color(rgb, level)}})
                await asyncio.sleep(self._frame_s)
        except ChromaError as exc:
            log.info("Breathing stopped: %s", exc)

    async def _set(self, look: Look) -> None:
        await self._stop_animation()
        if look.kind == "breathe":
            await self._show(look, BREATH_FLOOR)  # opens the session, so a missing Chroma is told now
            self._look = look
            self._animation = asyncio.create_task(self._breathe(look.rgb))
        else:
            await self._show(look)
            self._look = look

    async def set(self, look: Look) -> None:
        async with self._lock:
            await self._set(look)

    async def flash(self, rgb: RGB, times: int) -> None:
        """Blink ``rgb`` ``times`` times, then put back what showed before (Synapse's lighting included)."""
        async with self._lock:
            before = self._look
            await self._stop_animation()
            flash = Look("color", rgb)
            for _ in range(times):
                await self._show(flash)
                await asyncio.sleep(self._blink_s)
                await self._show(Look("off"))
                await asyncio.sleep(self._blink_s)
            if before is None:
                await self._end()
            else:
                await self._set(before)

    async def _end(self) -> None:
        await self._stop_animation()
        session = self._session
        self._forget()
        self._look = None
        if session is not None:
            with contextlib.suppress(httpx.HTTPError):
                await self._http().delete(session)

    async def default(self) -> None:
        """Back to the lighting Synapse shows on its own: the session ends."""
        async with self._lock:
            await self._end()


LIGHTS = MouseLights()


class RazerLightingArgs(ToolArgs):
    action: Literal["color", "off", "breathe", "flash", "default"] = Field(
        description="color: steady color; off: lights off; breathe: slow pulsing; flash: quick blinks, then back to "
        "what showed before (for alerts); default: back to Synapse's own lighting."
    )
    color: str = Field(default="", max_length=30, description="Name (red, blue, purple...) or #RRGGBB.")
    times: int = Field(default=3, ge=1, le=10, description="How many blinks, for flash.")


class RazerLighting(Tool):
    name = "razer_mouse_lighting"
    description = (
        "Set the lighting on the user's Razer mouse through Razer Chroma (Synapse must be running): a color, off, "
        "breathing, a quick flash (also as an alert, e.g. in a scheduled job when something is due), or back to the "
        "default Synapse lighting."
    )
    Args = RazerLightingArgs
    risk = RiskLevel.SAFE
    category = "system"

    def __init__(self, lights: MouseLights | None = None) -> None:
        self.lights = lights if lights is not None else LIGHTS

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        return _color_problem(args)

    async def run(self, args: RazerLightingArgs, ctx: ToolContext) -> ToolResult:
        if problem := _color_problem(args):
            return ToolResult.error(problem)
        rgb = parse_color(args.color) or COLORS["white"]
        try:
            if args.action == "default":
                await self.lights.default()
                return ToolResult.success("The mouse is back to its default Synapse lighting.")
            if args.action == "off":
                await self.lights.set(Look("off"))
                return ToolResult.success("The mouse lights are off.")
            if args.action == "flash":
                await self.lights.flash(rgb, args.times)
                return ToolResult.success(f"Flashed the mouse {args.times} time{'s' if args.times != 1 else ''}.")
            await self.lights.set(Look("breathe" if args.action == "breathe" else "color", rgb))
        except ChromaError as exc:
            return ToolResult.error(str(exc))
        how = "breathing" if args.action == "breathe" else "set to"
        return ToolResult.success(f"The mouse is {how} {args.color}.", data={"rgb": list(rgb)})


def _color_problem(args: RazerLightingArgs) -> str | None:
    if args.action in ("color", "breathe") and not args.color:
        return f"Say which color for {args.action}."
    if args.color and parse_color(args.color) is None:
        return f"“{args.color}” isn't a color I know. Use a name ({', '.join(list(COLORS)[:10])}...) or #RRGGBB."
    return None


TOOLS = [RazerLighting]
