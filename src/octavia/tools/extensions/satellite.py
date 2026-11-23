"""Satellite imagery: a picture of a place from above for her to look at, or a satellite map on the user's screen.

Two free sources that need no key or account, each fetched as one finished picture (there's no image library to
stitch map tiles with):

- "detailed": Esri's World Imagery, the sharp aerial and satellite mosaic (Maxar and others) most map sites show.
  Buildings, fields and coastlines are clear, but the photos are months or years old, never live.
- "daily": NASA's Worldview snapshots of one day's true-colour pass (VIIRS, MODIS before 2012), about 250 m a pixel:
  clouds, storms, smoke, snow and floods as they were that day.

Places are found with OpenStreetMap's Nominatim, the same way and under the same one-request-a-second limit as
``directions``; its bounding box sizes the picture to the place. The picture goes to the model as an image attached
to the result; the user sees the same picture in a window of its own on their screen (``picture_window``), not a
browser. Only when they ask for an interactive map is one opened in the browser: Google Maps' satellite view for
"detailed", or NASA Worldview on the same day's pass for "daily". The latest picture is also kept in memory for a few
minutes (``_pictures``), so ``send_to_phone`` can send it to the user's phone.

Showing the user a place ("detailed" imagery, in a window or the browser) opens it on God's Eye View's 3D globe
first (``gods_eye``), unless ``gods_eye.enabled`` is off or they asked for the plain picture or Google Maps. Only when
that fails does the window or Google Maps open, with a note saying why.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import Field

from octavia.core import home as _home
from octavia.net import tls_context as _tls_context
from octavia.platform.picture_window import show_picture, warm_up
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._gods_eye import GodsEye, GodsEyeError, view_width
from octavia.tools.extensions._pictures import PICTURES, PictureMemory
from octavia.tools.extensions.directions import (
    GEOCODE_INTERVAL_S,
    GEOCODE_URL,
    HomeNotSetError,
    PlaceNotFoundError,
    Throttle,
    _is_home,
    _label,
    _lookup_params,
    user_agent,
)
from octavia.tools.extensions.weather import NO_HOME

log = logging.getLogger(__name__)

ESRI_EXPORT_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
SNAPSHOT_URL = "https://wvs.earthdata.nasa.gov/api/v1/snapshot"
MAPS_URL = "https://www.google.com/maps/@"
WORLDVIEW_URL = "https://worldview.earthdata.nasa.gov/"
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
WIDTH, HEIGHT = 1024, 768  # inside Claude's image limits, so it isn't scaled down again
MAX_IMAGE_BYTES = 3_500_000  # well under the API's 5 MB per image once base64-encoded
KM_PER_DEGREE = 111.32
EARTH_RADIUS_M = 6378137.0  # Web Mercator's sphere
CACHE_SIZE = 200

Imagery = Literal["detailed", "daily"]
# How wide a picture can be, in km, for each source: below "detailed"'s smallest the photo is only blur, and
# "daily"'s 250 m pixels show nothing in less than a few dozen km.
_WIDTH_RANGE = {"detailed": (0.1, 300.0), "daily": (20.0, 5000.0)}
_DEFAULT_WIDTH = {"detailed": (0.4, 60.0), "daily": (500.0, 5000.0)}  # a place's own size, kept inside these
_HOME_WIDTH_KM = 20.0  # a city, for "detailed" pictures of home
_FIRST_DAY = date(2000, 2, 24)  # MODIS Terra's first images
_VIIRS_FROM = date(2012, 1, 19)
_COORDINATES = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class Spot:
    """A found place, and how wide it is (km east to west, height scaled to the picture's shape), when known."""

    name: str
    latitude: float
    longitude: float
    width_km: float | None = None


def _place_width(result: dict[str, Any], latitude: float) -> float | None:
    """How wide a picture shows all of a Nominatim result's bounding box, with a margin: None without one."""
    try:
        south, north, west, east = (float(v) for v in result["boundingbox"])
    except (KeyError, TypeError, ValueError):
        return None
    across = (east - west) * KM_PER_DEGREE * math.cos(math.radians(latitude))
    tall = (north - south) * KM_PER_DEGREE
    return max(across, tall * WIDTH / HEIGHT) * 1.3


def picture_width(spot: Spot, imagery: Imagery, asked: float | None) -> float:
    """The width in km to show: the one asked for, else the place's own size (the city for home), kept to what the
    source can show."""
    low, high = _WIDTH_RANGE[imagery]
    if asked is None:
        smallest, largest = _DEFAULT_WIDTH[imagery]
        asked = spot.width_km if spot.width_km is not None else _HOME_WIDTH_KM
        asked = min(max(asked, smallest), largest)
    return min(max(asked, low), high)


def mercator_box(latitude: float, longitude: float, width_km: float) -> tuple[float, float, float, float]:
    """The Web Mercator box (xmin, ymin, xmax, ymax in meters) around a point, ``width_km`` across on the ground
    and the picture's shape, so its pixels are square on the ground."""
    latitude = max(-85.0, min(85.0, latitude))
    stretch = 1 / math.cos(math.radians(latitude))  # Mercator's scale at this latitude
    x = EARTH_RADIUS_M * math.radians(longitude)
    y = EARTH_RADIUS_M * math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2))
    half_w = width_km * 500 * stretch
    half_h = half_w * HEIGHT / WIDTH
    return x - half_w, y - half_h, x + half_w, y + half_h


def degree_box(latitude: float, longitude: float, width_km: float) -> tuple[float, float, float, float]:
    """The (south, west, north, east) box around a point, ``width_km`` across on the ground and the picture's shape.
    Drawn into a ``WIDTH`` x ``HEIGHT`` picture, its pixels are square on the ground."""
    half_lat = width_km * HEIGHT / WIDTH / 2 / KM_PER_DEGREE
    half_lon = width_km / 2 / (KM_PER_DEGREE * max(math.cos(math.radians(latitude)), 0.01))
    south, north = max(-90.0, latitude - half_lat), min(90.0, latitude + half_lat)
    west, east = max(-180.0, longitude - half_lon), min(180.0, longitude + half_lon)
    return south, west, north, east


def maps_zoom(latitude: float, width_km: float) -> int:
    """The Google Maps zoom level at which about ``width_km`` fills a browser window some 1400 pixels wide."""
    meters_per_pixel = width_km * 1000 / 1400
    zoom = math.log2(156543.03 * math.cos(math.radians(latitude)) / meters_per_pixel)
    return max(3, min(20, math.floor(zoom)))


def maps_link(spot: Spot, width_km: float) -> str:
    """Google Maps' documented map URL, centred on the place in its satellite view."""
    query = {
        "api": "1",
        "map_action": "map",
        "center": f"{spot.latitude:.6f},{spot.longitude:.6f}",
        "zoom": str(maps_zoom(spot.latitude, width_km)),
        "basemap": "satellite",
    }
    return f"{MAPS_URL}?{urlencode(query)}"


def true_colour_layer(day: date) -> str:
    return f"{'VIIRS_SNPP' if day >= _VIIRS_FROM else 'MODIS_Terra'}_CorrectedReflectance_TrueColor"


def worldview_link(spot: Spot, width_km: float, day: date) -> str:
    """NASA Worldview's permalink showing the same area, day and layer as the "daily" picture, with coastlines and
    place names over it so the user can tell where they're looking."""
    south, west, north, east = degree_box(spot.latitude, spot.longitude, width_km)
    query = {
        "v": f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}",
        "l": f"Reference_Labels_15m,Coastlines_15m,{true_colour_layer(day)}",
        "t": f"{day.isoformat()}-T00:00:00Z",
    }
    return f"{WORLDVIEW_URL}?{urlencode(query)}"


def daily_day(asked: date | None, today: date) -> date:
    """The day of a "daily" picture: the one asked for, else yesterday, as today's pass may not be ready yet."""
    day = asked or today - timedelta(days=1)
    if day > today:
        raise ImageryError(f"There's no daily satellite picture for {spoken_day(day)} yet; it's in the future.")
    if day < _FIRST_DAY:
        raise ImageryError("NASA's daily satellite pictures start on February 24, 2000.")
    return day


def size_text(km: float) -> str:
    if km < 1:
        return f"{round(km * 1000, -1):.0f} meters"
    return f"{km:.1f}".removesuffix(".0") + " km" if km < 10 else f"{round(km):,} km"


def spoken_day(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def _is_picture(content: bytes) -> bool:
    return content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG\r\n\x1a\n")


class ImageryError(RuntimeError):
    """The imagery service answered without a picture; the message is meant for the user."""


class Locator:
    """Finds places on OpenStreetMap, remembering them; "lat, lon" and the user's home need no lookup."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, throttle: Throttle | None = None) -> None:
        self._transport = transport
        self._throttle = throttle or Throttle(GEOCODE_INTERVAL_S)
        self._spots: dict[str, Spot] = {}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=TIMEOUT,
            verify=_tls_context(),
            transport=self._transport,
            headers={"User-Agent": user_agent(), "Accept-Language": "en"},
            follow_redirects=True,
        )

    async def find(self, client: httpx.AsyncClient, place: str | None) -> Spot:
        if not place or _is_home(place):
            home = _home.current()
            if not home.is_set:
                raise HomeNotSetError("home")
            if home.located:
                return Spot(home.name, float(home.latitude), float(home.longitude))  # type: ignore[arg-type]
            place = home.name
        if match := _COORDINATES.match(place):
            latitude, longitude = float(match[1]), float(match[2])
            if abs(latitude) <= 90 and abs(longitude) <= 180:
                return Spot(f"{latitude:.4f}, {longitude:.4f}", latitude, longitude)
        key = " ".join(place.lower().split())
        if (cached := self._spots.get(key)) is not None:
            return cached
        await self._throttle.wait()
        response = await client.get(GEOCODE_URL, params=_lookup_params(place))
        response.raise_for_status()
        results = response.json()
        if not results:
            raise PlaceNotFoundError(place)
        latitude, longitude = float(results[0]["lat"]), float(results[0]["lon"])
        spot = Spot(_label(results[0]) or place, latitude, longitude, _place_width(results[0], latitude))
        if len(self._spots) >= CACHE_SIZE:
            self._spots.pop(next(iter(self._spots)))
        self._spots[key] = spot
        return spot


def _service_error(exc: Exception, what: str) -> ToolResult:
    """A failed lookup or download as an error the user can act on."""
    if isinstance(exc, HomeNotSetError):
        return ToolResult.error(NO_HOME)
    if isinstance(exc, PlaceNotFoundError):
        return ToolResult.error(f"Couldn't find a place called {exc.args[0]!r}. Try adding the town or state.")
    if isinstance(exc, ImageryError):
        return ToolResult.error(str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        log.warning("%s answered HTTP %s", what, status)
        if status == 429:
            return ToolResult.error(f"The {what} says it's had too many requests; try again in a minute.")
        return ToolResult.error(f"The {what} answered HTTP {status}; try again later.")
    if isinstance(exc, httpx.HTTPError):
        log.warning("%s couldn't be reached: %s", what, exc)
        return ToolResult.error(f"Couldn't reach the {what}; check the internet connection.")
    log.warning("%s sent something unreadable: %s", what, exc)
    return ToolResult.error(f"The {what} sent back something unreadable; try again later.")


_FAILURES = (PlaceNotFoundError, ImageryError, httpx.HTTPError, ValueError, KeyError, TypeError, IndexError)


async def fetch_picture(
    client: httpx.AsyncClient, spot: Spot, width_km: float, day: date | None, *, png: bool = False
) -> bytes:
    """The "detailed" picture, or the "daily" one with a day, as JPEG for the model or PNG for a window on the
    screen (Tk can't read JPEG)."""
    if day is None:
        url = ESRI_EXPORT_URL
        params = {
            "bbox": ",".join(f"{v:.2f}" for v in mercator_box(spot.latitude, spot.longitude, width_km)),
            "bboxSR": "3857",
            "imageSR": "3857",
            "size": f"{WIDTH},{HEIGHT}",
            "format": "png24" if png else "jpg",
            "f": "image",
        }
    else:
        url = SNAPSHOT_URL
        params = {
            "REQUEST": "GetSnapshot",
            "TIME": day.isoformat(),
            "BBOX": ",".join(f"{v:.4f}" for v in degree_box(spot.latitude, spot.longitude, width_km)),
            "CRS": "EPSG:4326",
            "LAYERS": true_colour_layer(day),
            "WRAP": "day",
            "FORMAT": "image/png" if png else "image/jpeg",
            "WIDTH": str(WIDTH),
            "HEIGHT": str(HEIGHT),
        }
    response = await client.get(url, params=params)
    response.raise_for_status()
    if not _is_picture(response.content):
        raise ImageryError("The satellite imagery service didn't send back a picture; try again later.")
    return response.content


class SatelliteImageArgs(ToolArgs):
    place: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "An address, landmark, town, region or 'latitude, longitude'. Omit for the user's home ({{home}})."
        ),
    )
    imagery: Imagery = Field(
        default="detailed",
        description=(
            "detailed: a sharp aerial photo (buildings, fields, coastlines), but months or years old. daily: NASA's "
            "pass of one day over the whole region, about 250 m a pixel, for clouds, storms, smoke, snow or floods."
        ),
    )
    day: date | None = Field(
        default=None,
        description="daily only: the day, YYYY-MM-DD. Omit for yesterday (today's pass may not be ready yet).",
    )
    width_km: float | None = Field(
        default=None,
        gt=0,
        le=5000,
        description="How wide an area to show. Omit to fit the place (detailed) or show about 500 km around it (daily).",
    )


_GODS_EYE_FIELD = Field(
    default=True,
    description=(
        "detailed only: open the place on God's Eye View's 3D globe in the browser (the default). False only when the "
        "user asks for the plain picture, Google Maps, or not God's Eye."
    ),
)


class ShowSatelliteImageArgs(SatelliteImageArgs):
    gods_eye: bool = _GODS_EYE_FIELD


class LookAtSatelliteImage(Tool):
    name = "look_at_satellite_image"
    description = (
        "Get a satellite picture of a place, north up, for you to look at (read-only). Use for 'what does ... look "
        "like from above', 'a satellite view of ...', or with imagery='daily' for 'is there a storm, smoke or snow "
        "over ...'. Only you see it: describe it, and to show it to the user call show_satellite_image with the same "
        "place, imagery, day and width_km (open_satellite_map only if they ask for an interactive map), or to put it "
        "on their phone send_to_phone with picture=true."
    )
    Args = SatelliteImageArgs
    risk = RiskLevel.SAFE
    category = "travel"
    timeout_s = 60.0

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        throttle: Throttle | None = None,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
        pictures: PictureMemory | None = None,
    ) -> None:
        self._locator = Locator(transport, throttle)
        self._today = today
        self._pictures = pictures if pictures is not None else PICTURES

    async def run(self, args: SatelliteImageArgs, ctx: ToolContext) -> ToolResult:
        try:
            day = daily_day(args.day, self._today()) if args.imagery == "daily" else None
            async with self._locator.client() as client:
                spot = await self._locator.find(client, args.place)
                width_km = picture_width(spot, args.imagery, args.width_km)
                picture = await fetch_picture(client, spot, width_km, day)
                if len(picture) > MAX_IMAGE_BYTES:
                    raise ImageryError("The satellite picture came back too big to look at; try a smaller area.")
        except _FAILURES as exc:
            return _service_error(exc, "satellite imagery service")
        self._pictures.remember(picture, _title(spot, day))
        mime_type = "image/png" if picture.startswith(b"\x89PNG") else "image/jpeg"
        across = f"about {size_text(width_km)} across, north at the top"
        if day is None:
            content = (
                f"Satellite picture of {spot.name}, {across}. From Esri World Imagery (Maxar and others): sharp, "
                "but taken months or years ago, not live."
            )
        else:
            content = (
                f"NASA's satellite picture of {spot.name} on {spoken_day(day)}, {across}, about 250 m "
                "a pixel. Black or blank parts weren't covered by that day's pass; the day before may have them."
            )
        image = {"data": base64.b64encode(picture).decode("ascii"), "mime_type": mime_type}
        data = {
            "image": image,
            "place": spot.name,
            "latitude": spot.latitude,
            "longitude": spot.longitude,
            "width_km": width_km,
            "imagery": args.imagery,
            "width": WIDTH,
            "height": HEIGHT,
        }
        if day is not None:
            data["day"] = day.isoformat()
        return ToolResult.success(content, data=data)


class ShowSatelliteImage(Tool):
    name = "show_satellite_image"
    description = (
        "Show the user a place: 'show me ...', 'pull up ...', 'a view of ...', '... from above'. It opens on God's "
        "Eye View, a live 3D globe, in their browser. With gods_eye=false, or if God's Eye can't open, it's a "
        "satellite picture in its own window (zoom with the mouse wheel, drag, Escape closes), the same one "
        "look_at_satellite_image gets, so pass the same place, imagery, day and width_km."
    )
    Args = ShowSatelliteImageArgs
    risk = RiskLevel.SAFE  # only opens a window the user closes
    category = "travel"
    timeout_s = 90.0  # God's Eye View starting (up to 30 s), then the picture window if it can't

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        throttle: Throttle | None = None,
        show: Callable[[bytes, str, str], Awaitable[bool]] = show_picture,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
        warm: Callable[[], Awaitable[None]] | None = None,
        pictures: PictureMemory | None = None,
        gods_eye: GodsEye | None = None,
    ) -> None:
        self._locator = Locator(transport, throttle)
        self._show = show
        self._today = today
        self._pictures = pictures if pictures is not None else PICTURES
        # A viewer started ahead only helps the real window: a stand-in ``show`` gets none unless it brings its own.
        self._warm = warm or (warm_up if show is show_picture else _nothing)
        # Likewise the real God's Eye View (a server and a browser tab) only with the real window.
        self._gods_eye = gods_eye or (shared_gods_eye() if show is show_picture else None)

    async def run(self, args: ShowSatelliteImageArgs, ctx: ToolContext) -> ToolResult:
        note = ""
        if self._gods_eye is not None and args.gods_eye and args.imagery == "detailed" and ctx.config.gods_eye.enabled:
            shown = await _in_gods_eye(self._gods_eye, self._locator, args.place, args.width_km, ctx)
            if isinstance(shown, ToolResult):
                if shown.ok:
                    self._pictures.clear()  # "send it to my phone" mustn't send an earlier place's picture
                return shown
            note = shown
        # The window's process starts while the place is looked up and the picture downloads, not after: it's up
        # (and she can say so) sooner.
        warming = asyncio.create_task(self._warm())
        try:
            day = daily_day(args.day, self._today()) if args.imagery == "daily" else None
            async with self._locator.client() as client:
                spot = await self._locator.find(client, args.place)
                width_km = picture_width(spot, args.imagery, args.width_km)
                picture = await fetch_picture(client, spot, width_km, day, png=True)
                if not picture.startswith(b"\x89PNG"):
                    raise ImageryError("The satellite imagery service sent a picture the window can't show.")
        except _FAILURES as exc:
            return _service_error(exc, "satellite imagery service")
        finally:
            await warming
        self._pictures.remember(picture, _title(spot, day))
        across = f"about {size_text(width_km)} across, north up"
        if day is None:
            caption = f"{spot.name}, {across}. Esri World Imagery, not live."
        else:
            caption = f"{spot.name}, {spoken_day(day)}, {across}. NASA {true_colour_layer(day).split('_')[0]}."
        if not await self._show(picture, f"Satellite: {spot.name}", caption):
            return ToolResult.error("The picture window couldn't be opened on the screen.")
        data = {"place": spot.name, "latitude": spot.latitude, "longitude": spot.longitude, "width_km": width_km}
        if day is not None:
            data["day"] = day.isoformat()
        return ToolResult.success(
            f"{note}The satellite picture is on the screen in its own window: {caption}", data=data
        )


def _title(spot: Spot, day: date | None) -> str:
    """The picture's name on the user's phone (``send_to_phone``)."""
    return f"Satellite - {spot.name}" + (f" - {day.isoformat()}" if day is not None else "")


async def _nothing() -> None:
    return None


async def _open_in_browser(url: str) -> bool:
    return await asyncio.to_thread(webbrowser.open, url, 2)


def shared_gods_eye() -> GodsEye:
    """One for all her tools, so a server she started is known (and restarted when its keys change) by each."""
    global _SHARED_GODS_EYE
    if _SHARED_GODS_EYE is None:
        _SHARED_GODS_EYE = GodsEye(_open_in_browser)
    return _SHARED_GODS_EYE


_SHARED_GODS_EYE: GodsEye | None = None


async def _in_gods_eye(
    gods_eye: GodsEye, locator: Locator, place: str | None, width_km: float | None, ctx: ToolContext
) -> ToolResult | str:
    """The place opened in God's Eye View, or, when it couldn't be, a note for the fallback's result saying why. A
    place that isn't found is answered as before: no fallback would find it either."""
    try:
        async with locator.client() as client:
            spot = await locator.find(client, place)
    except _FAILURES as exc:
        return _service_error(exc, "map service")
    width = view_width(spot.width_km, width_km)
    try:
        url = await gods_eye.show(ctx.config, spot.latitude, spot.longitude, width)
    except (GodsEyeError, OSError, ValueError, httpx.HTTPError, httpx.InvalidURL) as exc:
        log.warning("God's Eye View couldn't show %s: %s", spot.name, exc)
        return f"God's Eye View couldn't open ({exc}), so this is the usual view instead. "
    data = {"place": spot.name, "latitude": spot.latitude, "longitude": spot.longitude, "url": url}
    return ToolResult.success(
        f"Opened {spot.name} in God's Eye View in the browser, a live 3D globe, about {size_text(width)} across. "
        "They can drag to look around and scroll to zoom.",
        data={**data, "viewer": "gods_eye"},
    )


class SatelliteMapArgs(ToolArgs):
    place: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "An address, landmark, town, region or 'latitude, longitude'. Omit for the user's home ({{home}})."
        ),
    )
    imagery: Imagery = Field(
        default="detailed",
        description=(
            "detailed: Google Maps' sharp satellite view. daily: NASA Worldview showing one day's pass, the same "
            "picture look_at_satellite_image fetches with imagery='daily'."
        ),
    )
    day: date | None = Field(
        default=None,
        description="daily only: the day, YYYY-MM-DD. Omit for yesterday (today's pass may not be ready yet).",
    )
    width_km: float | None = Field(
        default=None,
        gt=0,
        le=5000,
        description="How wide an area to show. Omit to fit the place (detailed) or show about 500 km around it (daily).",
    )
    gods_eye: bool = _GODS_EYE_FIELD


class OpenSatelliteMap(Tool):
    name = "open_satellite_map"
    description = (
        "Open an interactive map of a place in the user's browser: God's Eye View's 3D globe by default, Google "
        "Maps' satellite view with gods_eye=false (or if God's Eye can't open), or with imagery='daily' NASA "
        "Worldview on that day's pass. When they ask for an interactive map, Google Maps, Worldview or the browser; "
        "for 'show me' use show_satellite_image."
    )
    Args = SatelliteMapArgs
    risk = RiskLevel.SAFE  # only opens a web page
    category = "travel"
    timeout_s = 75.0  # God's Eye View starting (up to 30 s), then Google Maps if it can't

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        throttle: Throttle | None = None,
        open_url: Callable[[str], Awaitable[bool]] = _open_in_browser,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
        gods_eye: GodsEye | None = None,
    ) -> None:
        self._locator = Locator(transport, throttle)
        self._open_url = open_url
        self._today = today
        self._gods_eye = gods_eye or (shared_gods_eye() if open_url is _open_in_browser else None)

    async def run(self, args: SatelliteMapArgs, ctx: ToolContext) -> ToolResult:
        note = ""
        if self._gods_eye is not None and args.gods_eye and args.imagery == "detailed" and ctx.config.gods_eye.enabled:
            shown = await _in_gods_eye(self._gods_eye, self._locator, args.place, args.width_km, ctx)
            if isinstance(shown, ToolResult):
                return shown
            note = shown
        try:
            day = daily_day(args.day, self._today()) if args.imagery == "daily" else None
            async with self._locator.client() as client:
                spot = await self._locator.find(client, args.place)
        except _FAILURES as exc:
            return _service_error(exc, "map service")
        width_km = picture_width(spot, args.imagery, args.width_km)
        across = f"about {size_text(width_km)} across"
        if day is None:
            url = maps_link(spot, width_km)
            opened = f"Opened a satellite map of {spot.name} in the browser, {across}."
        else:
            url = worldview_link(spot, width_km, day)
            opened = f"Opened NASA Worldview on {spot.name} for {spoken_day(day)} in the browser, {across}."
        if not await self._open_url(url):
            return ToolResult.error("No web browser responded, so the satellite map couldn't be opened.")
        data = {"place": spot.name, "latitude": spot.latitude, "longitude": spot.longitude, "url": url}
        if day is not None:
            data["day"] = day.isoformat()
        return ToolResult.success(note + opened, data=data)


TOOLS = [LookAtSatelliteImage, ShowSatelliteImage, OpenSatelliteMap]
