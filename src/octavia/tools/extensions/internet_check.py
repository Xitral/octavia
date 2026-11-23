"""Internet check: whether the computer is online, the Wi-Fi network and its signal, latency and download speed.

For "is my internet slow" and "how's my Wi-Fi". It only measures, nothing is changed. The Wi-Fi details come from
``netsh wlan show interfaces``, which every Windows has. Latency is the time a TCP connection to a public DNS provider
takes to open: unlike ping (ICMP) it isn't dropped by firewalls that block ping, and it has no DNS lookup in it. The
download speed is a few seconds of Cloudflare's speed-test file, which needs no key or account. Whether the computer
is online at all is asked of Windows' own connectivity-check address, which also tells a hotel or school sign-in page
from the real internet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import Field

from octavia.net import tls_context as _tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

CONNECTIVITY_URL = "http://www.msftconnecttest.com/connecttest.txt"
CONNECTIVITY_TEXT = "Microsoft Connect Test"
SPEED_URL = "https://speed.cloudflare.com/__down"
SPEED_BYTES = 25_000_000  # more than most connections fetch in SPEED_SECONDS, so the time limit ends the test
SPEED_SECONDS = 6.0
LATENCY_HOSTS = (("1.1.1.1", 443, "Cloudflare"), ("8.8.8.8", 443, "Google"))
LATENCY_TRIES = 4
CONNECT_TIMEOUT_S = 2.0
NETSH_TIMEOUT_S = 5.0
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

Connectivity = Literal["online", "portal", "offline"]


@dataclass(frozen=True)
class Wifi:
    connected: bool
    ssid: str | None = None
    signal: int | None = None  # percent, as Windows reports it
    band: str | None = None
    radio: str | None = None
    receive_mbps: float | None = None  # the link rate to the router, not the internet speed


@dataclass(frozen=True)
class Latency:
    provider: str
    host: str
    median_ms: float
    lost: int
    tries: int


@dataclass(frozen=True)
class Speed:
    received_bytes: int
    seconds: float

    @property
    def mbps(self) -> float:
        return self.received_bytes * 8 / max(self.seconds, 1e-3) / 1e6


def _number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.rstrip("% ").replace(",", "."))
    except ValueError:
        return None


def parse_netsh(text: str) -> Wifi | None:
    """The Wi-Fi state from ``netsh wlan show interfaces``; None when the computer has no Wi-Fi adapter.

    Each adapter's block starts with its "Name" line; with several adapters the connected one wins. A connected
    adapter can lack its SSID line: newer Windows hides the network name from apps without location access.
    """
    blocks: list[dict[str, str]] = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not sep:
            continue
        if key == "name":
            blocks.append({})
        if blocks and key not in blocks[-1]:
            blocks[-1][key] = value
    if not blocks:
        return None
    block = next((b for b in blocks if b.get("state", "").lower() == "connected" or b.get("ssid")), blocks[0])
    if block.get("state", "").lower() != "connected" and not block.get("ssid"):
        return Wifi(connected=False)
    signal = _number(block.get("signal"))
    return Wifi(
        connected=True,
        ssid=block.get("ssid") or None,
        signal=round(signal) if signal is not None else None,
        band=block.get("band") or None,
        radio=block.get("radio type") or None,
        receive_mbps=_number(block.get("receive rate (mbps)")),
    )


async def read_netsh() -> str | None:
    """``netsh wlan show interfaces`` output, or None where it can't run (not Windows, netsh missing or stuck)."""
    if sys.platform != "win32":
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "netsh",
            "wlan",
            "show",
            "interfaces",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        log.warning("netsh couldn't be started: %s", exc)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), NETSH_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        log.warning("netsh didn't answer in %s s", NETSH_TIMEOUT_S)
        return None
    return out.decode("oem", errors="replace")


async def tcp_connect_ms(host: str, port: int) -> float | None:
    """Milliseconds to open a TCP connection, or None when it fails or takes longer than ``CONNECT_TIMEOUT_S``."""
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT_S)
    except (OSError, TimeoutError):
        return None
    elapsed = (time.perf_counter() - started) * 1000
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return elapsed


class InternetCheckArgs(ToolArgs):
    speed_test: bool = Field(
        default=True,
        description="~6 s, up to 25 MB. False when only asked if it works.",
    )


class CheckInternet(Tool):
    name = "check_internet"
    description = (
        "Check the internet (read-only): online or stuck behind a Wi-Fi sign-in page, Wi-Fi name/signal, "
        "latency, download speed."
    )
    Args = InternetCheckArgs
    risk = RiskLevel.SAFE
    category = "system"
    timeout_s = 40.0

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        read_wifi: Callable[[], Awaitable[str | None]] | None = None,
        connect: Callable[[str, int], Awaitable[float | None]] | None = None,
    ) -> None:
        self._transport = transport
        self._read_wifi = read_wifi or read_netsh
        self._connect = connect or tcp_connect_ms

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, verify=_tls_context(), transport=self._transport)

    async def _wifi(self) -> Wifi | None:
        try:
            text = await self._read_wifi()
        except Exception:
            log.warning("Reading the Wi-Fi state failed", exc_info=True)
            return None
        return parse_netsh(text) if text else None

    async def _connectivity(self, client: httpx.AsyncClient) -> Connectivity:
        """Any answer but Windows' expected text means something stands in for the internet: a sign-in page."""
        try:
            response = await client.get(CONNECTIVITY_URL)
        except httpx.HTTPError:
            return "offline"
        if response.status_code == 200 and response.text.startswith(CONNECTIVITY_TEXT):
            return "online"
        return "portal"

    async def _latency(self) -> Latency | None:
        """The median of a few connections, one at a time so they don't slow each other; the next provider is tried
        when none of them got through."""
        for host, port, provider in LATENCY_HOSTS:
            times = [await self._connect(host, port) for _ in range(LATENCY_TRIES)]
            ok = [t for t in times if t is not None]
            if ok:
                return Latency(provider, host, statistics.median(ok), len(times) - len(ok), len(times))
        return None

    async def _download(self, client: httpx.AsyncClient) -> Speed:
        """Bytes received in at most ``SPEED_SECONDS``, timed from the response headers so the connection setup
        (already in the latency) doesn't drag a fast line's figure down."""
        received = 0
        async with client.stream("GET", SPEED_URL, params={"bytes": SPEED_BYTES}) as response:
            response.raise_for_status()
            started = time.perf_counter()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(SPEED_SECONDS):
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
            return Speed(received, time.perf_counter() - started)

    async def run(self, args: InternetCheckArgs, ctx: ToolContext) -> ToolResult:
        async with self._client() as client:
            wifi, connectivity, latency = await asyncio.gather(
                self._wifi(), self._connectivity(client), self._latency()
            )
            # A firewall or proxy can block Windows' check address while the internet itself works.
            if connectivity == "offline" and latency is not None:
                connectivity = "online"
            speed: Speed | None = None
            speed_problem = ""
            if args.speed_test and connectivity == "online":
                try:
                    speed = await self._download(client)
                except httpx.HTTPStatusError as exc:
                    log.warning("Speed test answered HTTP %s", exc.response.status_code)
                    speed_problem = f"the speed-test server answered HTTP {exc.response.status_code}"
                except httpx.HTTPError as exc:
                    log.warning("Speed test failed: %s", exc)
                    speed_problem = "the speed-test server couldn't be reached"
                if speed is not None and speed.received_bytes == 0:
                    speed, speed_problem = None, "no data arrived from the speed-test server"
        text = report(connectivity, wifi, latency, speed, speed_problem, args.speed_test)
        data: dict[str, Any] = {
            "connectivity": connectivity,
            "wifi": wifi.__dict__ if wifi else None,
            "latency_ms": round(latency.median_ms) if latency else None,
            "download_mbps": round(speed.mbps, 1) if speed else None,
        }
        return ToolResult.success(text, data=data)


# --------------------------------------------------------------------------- speaking


def _signal_word(percent: int) -> str:
    if percent >= 80:
        return "excellent"
    if percent >= 60:
        return "good"
    if percent >= 40:
        return "fair"
    return "weak"


def _latency_word(ms: float) -> str:
    if ms < 40:
        return "good"
    if ms < 100:
        return "okay"
    if ms < 200:
        return "slow"
    return "very slow"


def _speed_word(mbps: float) -> str:
    if mbps < 5:
        return "slow: web pages will drag and video will buffer"
    if mbps < 25:
        return "okay for browsing, calls and HD video"
    if mbps < 200:
        return "fast"
    return "very fast"


def _mbps(value: float) -> str:
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def report(
    connectivity: Connectivity,
    wifi: Wifi | None,
    latency: Latency | None,
    speed: Speed | None,
    speed_problem: str,
    speed_test: bool,
) -> str:
    """Short lines the model can read out or sum up."""
    lines = [
        {
            "online": "Internet: online.",
            "portal": (
                "Internet: not really online. Something answered in its place, most likely a Wi-Fi sign-in page "
                "(hotel, school, café): opening any website in the browser should show it."
            ),
            "offline": "Internet: offline. No server on the internet answered.",
        }[connectivity]
    ]

    if wifi is None:
        lines.append("Wi-Fi: no Wi-Fi adapter found or it couldn't be read, so this is probably a wired connection.")
    elif not wifi.connected:
        lines.append("Wi-Fi: not connected to any Wi-Fi network.")
    else:
        name = f'"{wifi.ssid}"' if wifi.ssid else "a network whose name Windows hides (location access is off)"
        parts = [f"connected to {name}"]
        if wifi.signal is not None:
            parts.append(f"signal {wifi.signal}% ({_signal_word(wifi.signal)})")
        if wifi.band:
            parts.append(wifi.band)
        if wifi.receive_mbps:
            parts.append(f"link to the router {_mbps(wifi.receive_mbps)} Mbps")
        lines.append("Wi-Fi: " + ", ".join(parts) + ".")

    if latency is not None:
        text = f"Latency: {latency.median_ms:.0f} ms to {latency.provider} ({_latency_word(latency.median_ms)})"
        if latency.lost:
            text += f"; {latency.lost} of {latency.tries} attempts got no answer, so the connection is dropping some"
        lines.append(text + ".")
    elif connectivity != "offline":
        lines.append("Latency: couldn't be measured; the test servers didn't answer.")

    if speed is not None:
        lines.append(f"Download speed: about {_mbps(speed.mbps)} Mbps ({_speed_word(speed.mbps)}).")
    elif speed_problem:
        lines.append(f"Download speed: not measured; {speed_problem}.")
    elif speed_test and connectivity != "online":
        lines.append("Download speed: not measured without a working connection.")
    return "\n".join(lines)


TOOLS = [CheckInternet]
