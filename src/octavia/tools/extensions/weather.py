"""Weather: current conditions and the forecast (hourly and daily, with the chance of rain) for a place.

Uses Open-Meteo, which is free and needs no key or account, so the tool works without any setup. The default place
is the user's home (``assistant.home``, core/home.py); with its coordinates set the usual question ("what's the
weather", a morning briefing) is one request. Other places are looked up with Open-Meteo's geocoding service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import Field

from octavia.core import home as _home
from octavia.core.text import clock as _clock
from octavia.core.text import day as _day
from octavia.net import tls_context as _tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RAINY_PERCENT = 30  # from this chance of rain on, an hour is worth pointing out


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float


NO_HOME = "No home place is set. Ask the user which place, or they can set their home in her settings (Home)."


def home_place() -> Place | None:
    """The user's home as a Place when its coordinates are set, else None (``home_name`` says whether one is named)."""
    home = _home.current()
    if not home.is_set or not home.located:
        return None
    return Place(home.name, float(home.latitude), float(home.longitude))  # type: ignore[arg-type]


def home_name() -> str:
    return _home.current().name


_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
    "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin",
    "wy": "wyoming", "dc": "district of columbia",
}  # fmt: skip
_COUNTRY_ALIASES = {"usa": "us", "u.s.": "us", "u.s.a.": "us", "united states of america": "us", "uk": "gb"}

# WMO weather interpretation codes, as Open-Meteo reports them.
_CONDITIONS = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "light snow showers",
    86: "snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "severe thunderstorms with hail",
}


def condition(code: Any) -> str:
    return _CONDITIONS.get(code, "unknown conditions") if isinstance(code, int) else "unknown conditions"


class PlaceNotFoundError(LookupError):
    pass


def _matches(result: dict[str, Any], qualifier: str) -> bool:
    """Whether a geocoding result fits one part of the asked-for place after the name: a state, region or country."""
    q = qualifier.lower().strip(" .")
    q = _US_STATES.get(q, _COUNTRY_ALIASES.get(q, q))
    fields = (result.get("admin1"), result.get("admin2"), result.get("country"), result.get("country_code"))
    return any(isinstance(f, str) and f.lower() == q for f in fields)


def _label(result: dict[str, Any]) -> str:
    parts = [result.get("name"), result.get("admin1")]
    if result.get("country_code") != "US":
        parts.append(result.get("country"))
    return ", ".join(p for p in dict.fromkeys(parts) if isinstance(p, str) and p)


def _is_home(location: str, home: str = "") -> bool:
    """Whether ``location`` names the home place ("Portland" or "Portland, OR" for "Portland, Oregon")."""
    parts = [w.strip() for w in home.split(",") if w.strip()]
    if not parts:
        return False
    words = [w.strip(" .").lower() for w in location.split(",")]
    region = {"admin1": parts[1]} if len(parts) > 1 else {}
    if len(parts) > 1 and parts[1].lower() in _US_STATES.values():
        region["country_code"] = "US"
    return words[0] == parts[0].lower() and all(_matches(region, w) for w in words[1:])


class WeatherArgs(ToolArgs):
    location: str | None = Field(
        default=None,
        max_length=100,
        description="City, e.g. 'Portland, OR'. Omit for home ({{home}}).",
    )
    days: int = Field(default=3, ge=1, le=7, description="1 = today only.")
    hours: int = Field(default=12, ge=0, le=48, description="Hourly forecast hours (0 = none).")
    units: Literal["fahrenheit", "celsius"] = Field(default="fahrenheit")


class GetWeather(Tool):
    name = "get_weather"
    description = (
        "Weather now and forecast (read-only): hourly, highs/lows, rain chance. For 'will it rain', 'do I "
        "need a jacket', briefings."
    )
    Args = WeatherArgs
    risk = RiskLevel.SAFE
    category = "weather"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, verify=_tls_context(), transport=self._transport)

    async def _find(self, client: httpx.AsyncClient, location: str) -> Place:
        """The place a spoken location means: the name is searched and anything after a comma (a state, region or
        country) picks among the results. "Portland OR" without a comma is tried as "Portland, OR" too."""
        name, *qualifiers = [p.strip() for p in location.split(",") if p.strip()]
        attempts = [(name, qualifiers)]
        if not qualifiers and " " in name:
            head, tail = name.rsplit(" ", 1)
            attempts.append((head, [tail]))
        for query, wanted in attempts:
            response = await client.get(GEOCODE_URL, params={"name": query, "count": 20, "language": "en"})
            response.raise_for_status()
            results = response.json().get("results") or []
            for result in results:
                if all(_matches(result, q) for q in wanted):
                    return Place(_label(result), float(result["latitude"]), float(result["longitude"]))
        raise PlaceNotFoundError(location)

    async def _forecast(self, client: httpx.AsyncClient, place: Place, args: WeatherArgs) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": place.latitude,
            "longitude": place.longitude,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,"
            "wind_gusts_10m,precipitation",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
            "precipitation_sum,sunrise,sunset",
            "temperature_unit": args.units,
            "wind_speed_unit": "mph" if args.units == "fahrenheit" else "kmh",
            "precipitation_unit": "inch" if args.units == "fahrenheit" else "mm",
            "timezone": "auto",
            # Hourly data runs out with the last forecast day, so an evening look 12 hours ahead needs tomorrow too.
            "forecast_days": max(args.days, 1 + (args.hours + 23) // 24),
        }
        response = await client.get(FORECAST_URL, params=params)
        response.raise_for_status()
        return response.json()

    async def run(self, args: WeatherArgs, ctx: ToolContext) -> ToolResult:
        location = args.location or ""
        try:
            async with self._client() as client:
                named = home_name()
                if not location and not named:
                    return ToolResult.error(NO_HOME)
                if not location or _is_home(location, named):
                    place = home_place() or await self._find(client, named)
                else:
                    place = await self._find(client, location)
                data = await self._forecast(client, place, args)
        except PlaceNotFoundError:
            return ToolResult.error(f"Couldn't find a place called {location!r}. Try adding the state or country.")
        except httpx.HTTPStatusError as exc:
            log.warning("Weather service answered HTTP %s", exc.response.status_code)
            return ToolResult.error(f"The weather service answered HTTP {exc.response.status_code}; try again later.")
        except httpx.HTTPError as exc:
            log.warning("Weather service couldn't be reached: %s", exc)
            return ToolResult.error("Couldn't reach the weather service; check the internet connection.")
        except ValueError as exc:
            log.warning("Weather service sent something unreadable: %s", exc)
            return ToolResult.error("The weather service sent back something unreadable; try again later.")
        try:
            text = report(place, data, args)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            log.warning("Weather forecast couldn't be read: %s", exc)
            return ToolResult.error("The weather service sent back an incomplete forecast; try again later.")
        return ToolResult.success(text, data={"location": place.name})


# --------------------------------------------------------------------------- speaking


def _num(value: Any) -> int | None:
    return round(value) if isinstance(value, int | float) else None


def _at(series: dict[str, Any], key: str, index: int) -> Any:
    """One value of an optional forecast series; None when the service left it out."""
    values = series.get(key) or []
    return values[index] if index < len(values) else None


def _hour(value: datetime) -> str:
    return f"{value:%I %p}".lstrip("0")


def report(place: Place, data: dict[str, Any], args: WeatherArgs) -> str:
    """The forecast as short lines the model can read out or summarize. Times are the place's local time."""
    deg = "°F" if args.units == "fahrenheit" else "°C"
    speed = "mph" if args.units == "fahrenheit" else "km/h"
    lines = [f"Weather for {place.name}:"]

    current = data["current"]
    now = datetime.fromisoformat(current["time"])
    temp, feels = _num(current.get("temperature_2m")), _num(current.get("apparent_temperature"))
    parts = [f"{temp}{deg}" if temp is not None else "temperature unknown"]
    if feels is not None and feels != temp:
        parts[0] += f" (feels like {feels}{deg})"
    parts.append(condition(current.get("weather_code")))
    if (humidity := _num(current.get("relative_humidity_2m"))) is not None:
        parts.append(f"humidity {humidity}%")
    if (wind := _num(current.get("wind_speed_10m"))) is not None:
        gusts = _num(current.get("wind_gusts_10m"))
        parts.append(f"wind {wind} {speed}" + (f" gusting to {gusts}" if gusts and gusts > wind + 5 else ""))
    lines.append(f"Now ({_clock(now)}): {', '.join(parts)}.")

    hourly = data.get("hourly") or {}
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    upcoming = [
        (datetime.fromisoformat(t), temp, chance, code)
        for t, temp, chance, code in zip(
            hourly.get("time", []),
            hourly.get("temperature_2m", []),
            hourly.get("precipitation_probability", []),
            hourly.get("weather_code", []),
            strict=False,
        )
        if datetime.fromisoformat(t) > this_hour
    ][: args.hours]
    if upcoming:
        hours = []
        for when, temp, chance, code in upcoming:
            rain = f", {_num(chance)}% rain" if _num(chance) else ""
            hours.append(f"{_hour(when)} {_num(temp)}{deg} {condition(code)}{rain}")
        lines.append("Next hours: " + "; ".join(hours) + ".")
        rainy = [(when, _num(chance) or 0) for when, _, chance, _ in upcoming if (_num(chance) or 0) >= RAINY_PERCENT]
        if rainy:
            peak_when, peak = max(rainy, key=lambda r: r[1])
            lines.append(
                f"Rain likeliest around {_hour(peak_when)} ({peak}%), possible from {_hour(rainy[0][0])} "
                f"to {_hour(rainy[-1][0])}."
            )
        else:
            lines.append(f"No rain expected in the next {len(upcoming)} hours.")

    daily = data["daily"]
    for index, day in enumerate(daily["time"][: args.days]):
        when = datetime.fromisoformat(day)
        label = "Today" if when.date() == now.date() else "Tomorrow" if (when.date() - now.date()).days == 1 else ""
        heading = f"{label} ({_day(when)})" if label else _day(when)
        high, low = _num(daily["temperature_2m_max"][index]), _num(daily["temperature_2m_min"][index])
        text = f"{heading}: {condition(daily['weather_code'][index])}, high {high}{deg}, low {low}{deg}"
        if (chance := _num(_at(daily, "precipitation_probability_max", index))) is not None:
            text += f", {chance}% chance of rain"
        amount = _at(daily, "precipitation_sum", index)
        if isinstance(amount, int | float) and amount > 0:
            text += f" ({amount:g} {'in' if args.units == 'fahrenheit' else 'mm'} expected)"
        sunrise, sunset = _at(daily, "sunrise", index), _at(daily, "sunset", index)
        if index == 0 and sunrise and sunset:
            text += (
                f"; sunrise {_clock(datetime.fromisoformat(sunrise))}, sunset {_clock(datetime.fromisoformat(sunset))}"
            )
        lines.append(text + ".")
    return "\n".join(lines)


TOOLS = [GetWeather]
