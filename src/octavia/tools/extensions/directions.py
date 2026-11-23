"""Directions: distance, travel time and the main steps of a route by car, on foot or by bike.

Uses OpenStreetMap, free and without a key or account: Nominatim finds the places and the FOSSGIS OSRM servers
(the ones openstreetmap.org's own directions use, with separate car, foot and bike profiles; the OSRM project's demo
server only knows cars) find the route. Both ask clients to identify themselves with a real User-Agent, and
Nominatim allows at most one request a second, so lookups are spaced out and remembered. Routes start at the
user's home (``assistant.home``, core/home.py) unless told otherwise, and place lookups prefer the area around it
so "the airport" means the local one.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Literal

import httpx
from pydantic import Field

from octavia.core import home as _home
from octavia.net import tls_context as _tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.weather import NO_HOME, Place
from octavia.tools.extensions.weather import _is_home as _is_home_city

log = logging.getLogger(__name__)

GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
ROUTE_URL = "https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/{coordinates}"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
GEOCODE_INTERVAL_S = 1.1  # Nominatim's usage policy: an absolute maximum of one request per second
CACHE_SIZE = 200
METERS_PER_MILE = 1609.344

_PROFILES = {"driving": "car", "walking": "foot", "cycling": "bike"}
_HOME_WORDS = {"home", "my home", "my house", "my place", "here", "current location", "my location", "where i am"}
_COMPASS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth"}
# Steps that don't ask the traveller to do anything: their distance belongs to the step before.
_PASS_THROUGH = {"new name", "notification", "exit roundabout", "exit rotary", "use lane"}


@functools.cache
def user_agent() -> str:
    """Who is asking, as Nominatim's and FOSSGIS's usage policies require: a stock library User-Agent is refused."""
    try:
        version = metadata.version("octavia")
    except metadata.PackageNotFoundError:
        version = "0.1"
    return (
        f"Octavia/{version} (personal Windows voice assistant; https://operations.osmfoundation.org/policies/nominatim/)"
        f" httpx/{httpx.__version__}"
    )


class Throttle:
    """Spaces calls at least ``interval`` seconds apart, however many lookups run at once."""

    def __init__(
        self,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last = -math.inf
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self._interval - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._last = self._clock()


class PlaceNotFoundError(LookupError):
    pass


class HomeNotSetError(PlaceNotFoundError):
    """Home was meant (no place given, or "home") but none is set: the user is asked."""


def home_area() -> str | None:
    """The lon/lat box around home that Nominatim prefers results in (not limited to), when home is located."""
    return _home.current().area()


def _lookup_params(query: str) -> dict[str, Any]:
    params: dict[str, Any] = {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    if area := home_area():
        params["viewbox"] = area
    return params


class RouteError(RuntimeError):
    """The routing service answered but had no route; the message is meant for the user."""


def _is_home(place: str) -> bool:
    return place.strip(" .").lower() in _HOME_WORDS or _is_home_city(place, _home.current().name)


def _label(result: dict[str, Any]) -> str:
    """A short spoken name for a Nominatim result: the place and its town, not the whole postal address."""
    address = result.get("address") or {}
    name = result.get("name") or ""
    if not name and address.get("road"):
        name = f"{address.get('house_number', '')} {address['road']}".strip()
    if not name:
        name = str(result.get("display_name", "")).split(",")[0]
    town = next((address[k] for k in ("city", "town", "village", "hamlet", "suburb") if address.get(k)), "")
    parts = [name, town, address.get("state", "")]
    return ", ".join(p for p in dict.fromkeys(parts) if p)


class DirectionsArgs(ToolArgs):
    destination: str = Field(
        min_length=1,
        max_length=200,
        description="Where to go: an address, a business or landmark, or a town, e.g. 'the airport' or 'Seattle, WA'.",
    )
    origin: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Where to start. Omit for the user's home ({{home}}; the city center, so pass their street address "
            "instead if you know it)."
        ),
    )
    mode: Literal["driving", "walking", "cycling"] = Field(default="driving", description="How the user travels.")
    steps: int = Field(
        default=6, ge=0, le=20, description="How many main steps of the route to list (0 for just time and distance)."
    )


class GetDirections(Tool):
    name = "get_directions"
    description = (
        "Travel time, distance and directions between two places by car, on foot or by bike (read-only), from "
        "OpenStreetMap. Starts from the user's home ({{home}}) unless another origin is given. "
        "Use for 'how long to drive to ...', 'how far is ...', 'how do I get to ...', 'can I walk to ...'. Driving "
        "times don't include live traffic."
    )
    Args = DirectionsArgs
    risk = RiskLevel.SAFE
    category = "travel"
    timeout_s = 40.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, throttle: Throttle | None = None) -> None:
        self._transport = transport
        self._throttle = throttle or Throttle(GEOCODE_INTERVAL_S)
        self._places: dict[str, Place] = {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=TIMEOUT,
            verify=_tls_context(),
            transport=self._transport,
            headers={"User-Agent": user_agent(), "Accept-Language": "en"},
        )

    async def _home(self, client: httpx.AsyncClient) -> Place:
        """The user's home: its coordinates when set, else its name looked up (once); HomeNotSetError without one."""
        home = _home.current()
        if not home.is_set:
            raise HomeNotSetError("home")
        if home.located:
            return Place(home.name, float(home.latitude), float(home.longitude))  # type: ignore[arg-type]
        return await self._find(client, home.name)

    async def _find(self, client: httpx.AsyncClient, query: str) -> Place:
        key = " ".join(query.lower().split())
        if (cached := self._places.get(key)) is not None:
            return cached
        await self._throttle.wait()
        response = await client.get(GEOCODE_URL, params=_lookup_params(query))
        response.raise_for_status()
        results = response.json()
        if not results:
            raise PlaceNotFoundError(query)
        place = Place(_label(results[0]) or query, float(results[0]["lat"]), float(results[0]["lon"]))
        if len(self._places) >= CACHE_SIZE:
            self._places.pop(next(iter(self._places)))
        self._places[key] = place
        return place

    async def _route(self, client: httpx.AsyncClient, start: Place, end: Place, mode: str) -> dict[str, Any]:
        coordinates = f"{start.longitude},{start.latitude};{end.longitude},{end.latitude}"
        url = ROUTE_URL.format(profile=_PROFILES[mode], coordinates=coordinates)
        response = await client.get(url, params={"overview": "false", "steps": "true", "alternatives": "false"})
        if response.status_code != 400:  # OSRM explains a route it can't plan in a 400's body
            response.raise_for_status()
        data = response.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            code = data.get("code")
            if code in ("NoRoute", "NoSegment"):
                raise RouteError(f"There's no {mode} route between {start.name} and {end.name}.")
            raise RouteError(f"The routing service couldn't plan that route ({code or 'no answer'}).")
        return data["routes"][0]

    async def run(self, args: DirectionsArgs, ctx: ToolContext) -> ToolResult:
        try:
            async with self._client() as client:
                start = (
                    await self._home(client)
                    if not args.origin or _is_home(args.origin)
                    else await self._find(client, args.origin)
                )
                end = (
                    await self._home(client)
                    if _is_home(args.destination)
                    else await self._find(client, args.destination)
                )
                route = await self._route(client, start, end, args.mode)
        except HomeNotSetError:
            return ToolResult.error(NO_HOME)
        except PlaceNotFoundError as exc:
            return ToolResult.error(f"Couldn't find a place called {exc.args[0]!r}. Try adding the town or state.")
        except RouteError as exc:
            return ToolResult.error(str(exc))
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.warning("Directions service answered HTTP %s", status)
            if status == 429:
                return ToolResult.error("The map service says it's had too many requests; try again in a minute.")
            return ToolResult.error(f"The map service answered HTTP {status}; try again later.")
        except httpx.HTTPError as exc:
            log.warning("Directions service couldn't be reached: %s", exc)
            return ToolResult.error("Couldn't reach the map service; check the internet connection.")
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            log.warning("Directions service sent something unreadable: %s", exc)
            return ToolResult.error("The map service sent back something unreadable; try again later.")
        try:
            text = report(start, end, route, args)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            log.warning("Route couldn't be read: %s", exc)
            return ToolResult.error("The map service sent back an incomplete route; try again later.")
        data = {
            "origin": start.name,
            "destination": end.name,
            "mode": args.mode,
            "distance_m": route["distance"],
            "duration_s": route["duration"],
        }
        return ToolResult.success(text, data=data)


# --------------------------------------------------------------------------- speaking


@dataclass
class Move:
    text: str
    distance: float  # meters travelled after the move, up to the next one


def distance(meters: float) -> str:
    miles = meters / METERS_PER_MILE
    if miles < 0.1:
        feet = max(50, round(meters * 3.28084 / 50) * 50)
        return f"{feet} feet"
    amount = f"{miles:.1f}".removesuffix(".0") if miles < 10 else str(round(miles))
    return f"{amount} mile" + ("" if amount == "1" else "s")


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours} hour" + ("s" if hours != 1 else "")] if hours else []
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    return " ".join(parts)


def _road(step: dict[str, Any]) -> str:
    return (step.get("name") or "").strip() or (step.get("ref") or "").strip()


def _instruction(step: dict[str, Any]) -> str | None:
    """What the traveller does at one OSRM step, or None when the step is only a change of street name."""
    maneuver = step.get("maneuver") or {}
    kind = maneuver.get("type", "")
    modifier = maneuver.get("modifier") or ""
    side = modifier.removeprefix("slight ").removeprefix("sharp ")
    road = _road(step)
    onto = f" onto {road}" if road else ""
    toward = f" toward {step['destinations']}" if step.get("destinations") else ""
    if kind == "depart":
        return f"Head {_COMPASS[round(maneuver.get('bearing_after', 0) / 45) % 8]}" + (f" on {road}" if road else "")
    if kind in _PASS_THROUGH or (kind == "continue" and side in ("", "straight")):
        return None
    if kind in ("roundabout", "rotary"):
        exit_number = maneuver.get("exit")
        which = f"the {_ORDINALS.get(exit_number, f'number {exit_number}')} exit" if exit_number else "the exit"
        return f"At the roundabout, take {which}{onto}"
    if kind == "on ramp":
        return f"Take the ramp{onto or toward}"
    if kind == "off ramp":
        exits = f" {step['exits'].split(';')[0]}" if step.get("exits") else ""
        return f"Take exit{exits}{onto or toward}" if exits else f"Take the exit{onto or toward}"
    if kind == "fork":
        return f"Keep {side}{onto or toward}"
    if kind == "merge":
        return f"Merge{onto}"
    if kind == "end of road":
        return f"At the end of the road, turn {side}{onto}"
    if kind == "continue":
        return f"Keep {side} to stay on {road}" if road else f"Keep {side}"
    if modifier == "uturn":
        return f"Make a U-turn{onto}"
    if side == "straight":
        return f"Continue straight{onto}"
    if modifier.startswith("slight "):
        return f"Bear {side}{onto}"
    return f"Turn {modifier}{onto}" if modifier else f"Continue{onto}"


def moves(route: dict[str, Any]) -> list[Move]:
    """The route's steps as things to do, the distance of steps that ask nothing added to the one before."""
    result: list[Move] = []
    for leg in route.get("legs") or []:
        for step in leg.get("steps") or []:
            if (step.get("maneuver") or {}).get("type") == "arrive":
                continue
            text = _instruction(step)
            if text is None and result:
                result[-1].distance += float(step.get("distance") or 0)
            elif text is not None:
                result.append(Move(text, float(step.get("distance") or 0)))
    return result


def main_moves(all_moves: list[Move], limit: int) -> list[Move]:
    """The moves worth saying out loud: the first and the last, then the longest stretches, kept in route order."""
    if len(all_moves) <= limit:
        return all_moves
    if limit <= 2:
        return [all_moves[0], all_moves[-1]][:limit]
    middle = sorted(range(1, len(all_moves) - 1), key=lambda i: all_moves[i].distance, reverse=True)[: limit - 2]
    return [all_moves[i] for i in sorted({0, len(all_moves) - 1, *middle})]


def report(start: Place, end: Place, route: dict[str, Any], args: DirectionsArgs) -> str:
    """The route as short lines the model can read out or summarize."""
    verb = {"driving": "Driving", "walking": "Walking", "cycling": "Cycling"}[args.mode]
    time_text = f"about {duration(route['duration'])}"
    if args.mode == "driving":
        time_text += " without traffic"
    headline = f"{verb} from {start.name} to {end.name}: {distance(route['distance'])}, {time_text}"
    roads = [r.strip() for leg in route.get("legs") or [] for r in (leg.get("summary") or "").split(",") if r.strip()]
    if roads:
        headline += ", mainly via " + " and ".join(dict.fromkeys(roads))
    lines = [headline + "."]
    if args.steps:
        everything = moves(route)
        chosen = main_moves(everything, args.steps)
        if chosen:
            lines.append("Main steps:")
            for number, move in enumerate(chosen, start=1):
                lines.append(f"{number}. {move.text} for {distance(move.distance)}.")
            if skipped := len(everything) - len(chosen):
                lines.append(f"({skipped} smaller turn{'s' if skipped != 1 else ''} left out.)")
            lines.append(f"Then arrive at {end.name}.")
    return "\n".join(lines)


TOOLS = [GetDirections]
