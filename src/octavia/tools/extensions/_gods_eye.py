"""God's Eye View (github.com/bilawalsidhu/gods-eye-view, MIT): the 3D globe she shows a place on when the user
asks to see it.

It's a local web app: a Vite dev server in its own checkout (``gods_eye.folder``, by default ``gods-eye-view`` next
to her folder), answering at ``gods_eye.address``. When nothing answers there she starts it herself, hidden, with
node and the checkout's own Vite, and it keeps running after her for the next look (its process id and a hash of
its keys in ``data/gods_eye_server.json``, so she knows it after a restart). Its optional keys (CESIUM_ION_TOKEN,
GOOGLE_MAPS_API_KEY) come from her .env and go to the server as environment variables, which win over its own .env;
nothing else of hers does (her other keys stay hers). When they change she restarts a server she started; one the
user started is used as it is.

A place is opened with the app's own share-link format, ``#lat=..&lon=..&alt=..&heading=..&pitch=..`` (its
``src/sharelink.js``): the camera's position and angle, not the point looked at. So for a tilted view the camera is
put south of the place, high enough above the ground for the whole place to fit: ``alt`` is above the WGS84
ellipsoid, open-meteo's elevation above sea level, so the higher of the ground there and under the camera gets a
margin for the difference (the geoid is up to about 85 m above the ellipsoid) and for buildings. Anything going wrong is a ``GodsEyeError`` whose message says why, and the satellite tools fall
back to what they did before.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

import httpx
import psutil

from octavia import keys
from octavia.net import tls_context as _tls_context

if TYPE_CHECKING:
    from octavia.config import AppConfig

log = logging.getLogger(__name__)

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
FOLDER_NAME = "gods-eye-view"
LOG_FILE = "data/logs/gods_eye.log"
LOG_LIMIT_BYTES = 5_000_000  # started afresh past this
STATE_FILE = "data/gods_eye_server.json"
GROUND_MARGIN_M = 120.0
# What node and npm need from her environment; nothing else of hers (her keys, HOST) goes to the server.
PASSED_ENV = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "APPDATA",
        "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "PROGRAMDATA", "PROGRAMFILES",
        "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
        "USERNAME", "COMPUTERNAME", "LANG", "TZ",
    }
)  # fmt: skip
KEY_NAMES = ("CESIUM_ION_TOKEN", "GOOGLE_MAPS_API_KEY")
TITLE = b"God's Eye View"
START_TIMEOUT_S = 30.0  # Vite answers in a second or two; the tools leave room for their fallback after this
TILT_DEG = 45.0  # how far down a close view looks: a 3D city reads best at an angle
TOP_DOWN_KM = 200.0  # areas wider than this are looked at straight down
MIN_WIDTH_KM, MAX_WIDTH_KM = 0.3, 8000.0
HOME_WIDTH_KM = 20.0
DISTANCE_PER_WIDTH = 0.87  # 1 / (2 tan 30 deg): a 60 degree view fits a width at this distance
M_PER_DEGREE = 111_320.0


class GodsEyeError(RuntimeError):
    """God's Eye View couldn't show the place; the message says why, for the log and her fallback's note."""


@dataclass(frozen=True)
class Camera:
    latitude: float
    longitude: float
    altitude_m: float
    pitch_deg: float


def view_width(width_km: float | None, asked: float | None) -> float:
    """How wide an area to show, in km: the one asked for, else the place's own size (the city for home)."""
    width = asked if asked is not None else width_km if width_km is not None else HOME_WIDTH_KM
    return min(max(width, MIN_WIDTH_KM), MAX_WIDTH_KM)


def camera(latitude: float, longitude: float, width_km: float, ground_m: float = 0.0) -> Camera:
    """Where the camera goes for ``width_km`` around a point to fill the view: tilted and south of it for a town or
    smaller, straight down over it for a region."""
    distance = width_km * 1000 * DISTANCE_PER_WIDTH
    if width_km > TOP_DOWN_KM:
        return Camera(latitude, longitude, ground_m + distance, -90.0)
    tilt = math.radians(TILT_DEG)
    back = distance * math.cos(tilt) / M_PER_DEGREE
    return Camera(max(-89.9, latitude - back), longitude, ground_m + distance * math.sin(tilt), -TILT_DEG)


def view_link(address: str, cam: Camera) -> str:
    """The app's share link for a camera: looking north, at ``pitch`` degrees below the horizon."""
    params = {
        "lat": f"{cam.latitude:.6f}",
        "lon": f"{cam.longitude:.6f}",
        "alt": str(round(cam.altitude_m)),
        "heading": "0",
        "pitch": f"{cam.pitch_deg:g}",
    }
    return f"{address.rstrip('/')}/#{urlencode(params)}"


def folder_of(cfg: AppConfig) -> Path:
    if cfg.gods_eye.folder.strip():
        return cfg.resolve_path(cfg.gods_eye.folder.strip())
    return cfg.root.parent / FOLDER_NAME


def server_keys() -> dict[str, str]:
    """Its keys from her .env, only the ones she has: an empty one would hide what its own .env says."""
    return {name: value for name in KEY_NAMES if (value := (keys.value(name) or "").strip())}


def keys_hash(wanted: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(wanted, sort_keys=True).encode("utf-8")).hexdigest()


def server_env(wanted: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.upper() in PASSED_ENV} | wanted


def _port(address: str) -> int:
    try:
        parts = urlsplit(address)
        port = parts.port
    except ValueError as exc:
        raise GodsEyeError(f"God's Eye View's address {address!r} isn't a web address.") from exc
    if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise GodsEyeError(f"God's Eye View's address {address} isn't on this PC, so I can't start it.")
    return port or (443 if parts.scheme == "https" else 80)


def _launch(folder: Path, port: int, env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    """Its Vite dev server, hidden and detached so it outlives her, its output in ``log_path``."""
    node = shutil.which("node")
    if node is None:
        raise GodsEyeError("Node.js isn't installed, and God's Eye View runs on it.")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if log_path.stat().st_size > LOG_LIMIT_BYTES:
            log_path.unlink()
    # A console of its own that's never shown (not DETACHED_PROCESS: Windows then ignores CREATE_NO_WINDOW, and a
    # program the server runs would open a console of its own), outside her job so it outlives her when it may.
    flags = [0]
    if os.name == "nt":
        hidden = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        flags = [hidden | subprocess.CREATE_BREAKAWAY_FROM_JOB, hidden]
    command = [node, str(folder / "node_modules" / "vite" / "bin" / "vite.js"), "--port", str(port), "--strictPort"]
    with log_path.open("ab") as out:
        for flag in flags[:-1]:
            with contextlib.suppress(PermissionError):  # her job doesn't allow breaking away
                return _popen(command, folder, env, out, flag)
        return _popen(command, folder, env, out, flags[-1])


def _popen(command: list[str], folder: Path, env: dict[str, str], out: Any, flags: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        cwd=folder,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )


class GodsEye:
    """Opens places in God's Eye View, starting its server when it isn't running."""

    def __init__(
        self,
        open_url: Callable[[str], Awaitable[bool]],
        transport: httpx.AsyncBaseTransport | None = None,
        launch: Callable[[Path, int, dict[str, str], Path], subprocess.Popen[bytes]] = _launch,
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self._open_url = open_url
        self._transport = transport
        self._launch = launch
        self._start_timeout_s = start_timeout_s
        self._server: subprocess.Popen[bytes] | _Earlier | None = None
        self._server_keys: dict[str, str] = {}
        self._server_address = ""
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=2.0),
            verify=_tls_context(),
            transport=self._transport,
            follow_redirects=True,
        )

    async def _answers(self, client: httpx.AsyncClient, address: str) -> bool:
        try:
            response = await client.get(address, timeout=httpx.Timeout(3.0, connect=1.0))
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and TITLE in response.content

    async def ensure_running(self, cfg: AppConfig, client: httpx.AsyncClient) -> None:
        address = cfg.gods_eye.address
        async with self._lock:
            wanted = server_keys()
            port = _port(address)
            if self._server is None:
                self._server, self._server_keys = await asyncio.to_thread(_earlier_server, cfg, wanted)
                self._server_address = address
            mine = self._server is not None and self._server.poll() is None
            if mine and (wanted != self._server_keys or address != self._server_address):
                log.info("God's Eye View's keys or address changed: restarting its server")
                await asyncio.to_thread(self._stop)
            elif mine:
                # Hers and running, maybe still starting (a look that timed out while it did): waited for, never a
                # second one beside it.
                if await self._started(client, address):
                    return
                log.info("God's Eye View's server isn't answering: restarting it")
                await asyncio.to_thread(self._stop)
            elif await self._answers(client, address):
                return  # the user's own, used as it is
            folder = folder_of(cfg)
            if not (folder / "package.json").is_file():
                raise GodsEyeError(f"God's Eye View isn't installed at {folder}.")
            if not (folder / "node_modules" / "vite").is_dir():
                raise GodsEyeError(f"God's Eye View at {folder} hasn't had its packages installed (npm ci).")
            log.info("Starting God's Eye View from %s on port %d", folder, port)
            env = server_env(wanted)
            self._server = await asyncio.to_thread(self._launch, folder, port, env, cfg.resolve_path(LOG_FILE))
            self._server_keys, self._server_address = wanted, address
            await asyncio.to_thread(_remember_server, cfg, self._server.pid, wanted)
            if not await self._started(client, address):
                raise GodsEyeError(f"God's Eye View's server didn't answer at {address} in time; see {LOG_FILE}.")

    async def _started(self, client: httpx.AsyncClient, address: str) -> bool:
        """Whether her server answers within the start time. Raises ``GodsEyeError`` when it stops instead."""
        server = self._server
        assert server is not None
        deadline = time.monotonic() + self._start_timeout_s
        while time.monotonic() < deadline:
            if server.poll() is not None:
                self._server = None
                raise GodsEyeError(f"God's Eye View's server stopped as it started; see {LOG_FILE}.")
            if await self._answers(client, address):
                return True
            await asyncio.sleep(0.5)
        return False

    def _stop(self) -> None:
        if self._server is None:
            return
        with contextlib.suppress(OSError, psutil.Error):
            self._server.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired, psutil.Error):
            self._server.wait(timeout=5)
        self._server = None

    async def _ground(self, client: httpx.AsyncClient, *points: tuple[float, float]) -> float:
        """How high to count the ground: the highest of ``points`` above sea level (open-meteo), plus a margin, or
        just the margin when open-meteo can't say. A camera at a mountain's foot height would be inside it."""
        params = {
            "latitude": ",".join(f"{lat:.5f}" for lat, _ in points),
            "longitude": ",".join(f"{lon:.5f}" for _, lon in points),
        }
        try:
            response = await client.get(ELEVATION_URL, params=params)
            response.raise_for_status()
            highest = max(float(v) for v in response.json()["elevation"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            log.info("No elevation for %s; looking from sea level", points)
            highest = 0.0
        return max(0.0, highest) + GROUND_MARGIN_M

    async def show(self, cfg: AppConfig, latitude: float, longitude: float, width_km: float) -> str:
        """Open the place in the browser; the link opened. Raises ``GodsEyeError``."""
        spot = camera(latitude, longitude, width_km)  # where the camera stands; its height needs the ground
        async with self._client() as client:
            ground = asyncio.create_task(self._ground(client, (latitude, longitude), (spot.latitude, spot.longitude)))
            try:
                await self.ensure_running(cfg, client)
            except BaseException:
                ground.cancel()
                with contextlib.suppress(BaseException):
                    await ground
                raise
            height = await ground
        url = view_link(cfg.gods_eye.address, camera(latitude, longitude, width_km, height))
        if not await self._open_url(url):
            raise GodsEyeError("No web browser responded.")
        return url


class _Earlier:
    """A server she started before she last restarted, known by its process id: stopped like her own."""

    def __init__(self, process: psutil.Process) -> None:
        self._process = process
        self.pid = process.pid

    def poll(self) -> int | None:
        return None if self._process.is_running() else 0

    def terminate(self) -> None:
        self._process.terminate()

    def wait(self, timeout: float) -> None:
        self._process.wait(timeout)


def _remember_server(cfg: AppConfig, pid: int, wanted: dict[str, str]) -> None:
    path = cfg.resolve_path(STATE_FILE)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": pid, "keys": keys_hash(wanted)}), encoding="utf-8")


def _earlier_server(cfg: AppConfig, wanted: dict[str, str]) -> tuple[_Earlier | None, dict[str, str]]:
    """The server she started in an earlier run, when that process is still its node, and the keys it was started
    with: ``wanted`` when its hash matches, else a stand-in that differs, so a changed key restarts it."""
    try:
        state = json.loads(cfg.resolve_path(STATE_FILE).read_text(encoding="utf-8"))
        process = psutil.Process(int(state["pid"]))
        if not process.name().lower().startswith("node") or "vite" not in " ".join(process.cmdline()):
            return None, {}  # the id went to something else
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None, {}
    return _Earlier(process), wanted if state.get("keys") == keys_hash(wanted) else {"": "changed"}
