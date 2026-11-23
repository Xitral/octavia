"""Pictures from the web for her panels: "show me Sora from Kingdom Hearts" finds one with an image search and puts
it on a panel instead of opening Google Images in the browser (``show_image``).

* ``search`` asks Bing's image results (no key needed) and, should that fail, Wikimedia Commons, and returns
  candidates in their order: the full picture's address, Bing's own small copy of it, the page it's on, its title.
  Only those whose title or addresses name what was asked are kept (``relevant``).
* ``fetch`` downloads one, only from public addresses (never this PC or the home network: every name is looked up
  first, and every redirect checked again), up to ``MAX_BYTES``, and never a web page.
* ``clean`` decodes it with Pillow and draws it again as a fresh JPEG (PNG when it's see-through): only the first
  frame of an animation (nothing loops on a panel), no metadata, at most ``MAX_SIDE`` pixels a side, and only real
  pictures of a sane size. Whatever the file held besides its pixels is gone.
* ``ImageStore`` keeps the cleaned pictures in ``data/visuals/images`` under names made from their own bytes; the
  panel app serves only that folder to its pages (``images/<name>``), and old ones are pruned.

``find_pictures`` puts it together: the first ``count`` candidates that download and clean, tried a few at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import io
import ipaddress
import json
import logging
import math
import re
import secrets
import socket
import time
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 12 * 1024 * 1024  # one download
MAX_PIXELS = 40_000_000  # a picture bigger than this (a decompression bomb, or a poster scan) isn't decoded
MAX_SIDE = 1600  # the cleaned picture's longest side
MIN_SIDE = 64  # an icon or a tracking pixel isn't a picture of anything
MAX_REDIRECTS = 4
FETCH_TIMEOUT = httpx.Timeout(7.0, connect=4.0)
SEARCH_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
TRIED_AT_ONCE = 4
NAME = re.compile(r"^img-[0-9a-f]{32}\.(jpg|png)$")  # a stored picture's name (the panel page asks for it by this)
FORMATS = {"JPEG", "PNG", "GIF", "WEBP", "BMP"}
# Bing answers its image results to a browser; to anything else it answers differently or not at all.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
_BING_ITEM = re.compile(r'class="iusc"[^>]*?\sm="([^"]+)"')

_DECODING = asyncio.Semaphore(2)
Safe = Literal["strict", "moderate", "off"]
Resolver = Callable[[str], Awaitable[list[str]]]


class PictureError(Exception):
    """A picture that couldn't be had; the message says why, in words she can pass on."""


@dataclass(frozen=True)
class Candidate:
    """One image search result."""

    image_url: str  # the full picture
    thumb_url: str  # the search engine's own small copy ("" when there's none)
    page_url: str  # the page it's on
    title: str

    @property
    def site(self) -> str:
        return site_of(self.page_url or self.image_url)


@dataclass(frozen=True)
class Cleaned:
    data: bytes
    ext: str  # "jpg" or "png"
    width: int
    height: int


@dataclass(frozen=True)
class Picture:
    """A picture found, cleaned and stored, ready for a panel."""

    name: str  # in the ImageStore
    width: int
    height: int
    page_url: str  # where it's from (the page, or the picture itself when there's no page)
    site: str
    title: str
    data: bytes  # the cleaned bytes (for her own small look at it)


def site_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


# --------------------------------------------------------------------------- only public addresses


def public_ip(address: str) -> bool:
    """True for an address on the open internet: not this PC, the home network, link-local, carrier NAT,
    multicast or reserved."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inside = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inside is None and int(ip) >> 32 == 0 and int(ip) > 1:
            inside = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)  # ::a.b.c.d, IPv4-compatible
        if inside is None and int(ip) >> 32 == 0x64FF9B << 64:
            inside = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)  # NAT64
        if inside is not None:
            ip = inside
        elif ip.is_site_local:
            return False
    return ip.is_global and not ip.is_multicast


async def resolve_host(host: str) -> list[str]:
    """Every address ``host`` resolves to (the system's resolver, off the loop)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def web_address(url: str) -> str:
    """``url`` as an http(s) address she may fetch or show, or ``PictureError`` saying why not (only the address's
    form: ``check_public`` looks up where it leads)."""
    url = url.strip()
    if any(ch in url for ch in "\r\n\t\x00 "):
        raise PictureError("that address has spaces or control characters in it")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise PictureError("only http and https addresses can be shown")
    if parts.username or parts.password:
        raise PictureError("addresses with a name or password in them aren't allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise PictureError("that doesn't look like a web address")
    host = host.rstrip(".")  # "localhost." is localhost
    if host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".internal", ".home.arpa", ".test")):
        raise PictureError("that address is on this PC or the home network, not the web")
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host.strip("[]"))
        if not public_ip(host.strip("[]")):
            raise PictureError("that address is on this PC or the home network, not the web")
        return url
    if "." not in host:
        raise PictureError("that address is on this PC or the home network, not the web")
    return url


async def check_public(url: str, resolve: Resolver = resolve_host) -> None:
    """Raises ``PictureError`` unless ``url`` is an http(s) address whose host is only on the open internet."""
    web_address(url)
    host = (urlsplit(url).hostname or "").strip("[]").rstrip(".")
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return  # a public address literal (web_address checked it)
    try:
        addresses = await asyncio.wait_for(resolve(host), 5.0)
    except (OSError, TimeoutError, UnicodeError) as exc:
        raise PictureError(f"couldn't find the site {host}") from exc
    if not addresses:
        raise PictureError(f"couldn't find the site {host}")
    if not all(public_ip(a) for a in addresses):
        raise PictureError(f"{host} leads to this PC or the home network, not the web")


# --------------------------------------------------------------------------- downloading


class PublicOnlyBackend:
    """httpcore's network backend, connecting only to the public address it looked up and checked itself: a name
    that answers a public address to ``check_public`` and this PC's to the connection (DNS rebinding) gets nowhere.
    TLS still checks the certificate against the name (httpcore passes it to ``start_tls`` separately)."""

    def __init__(self, resolve: Resolver = resolve_host, inner: Any = None) -> None:
        import httpcore

        self._resolve = resolve
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:  # fmt: skip
        import httpcore

        literal = host.strip("[]")
        try:
            ipaddress.ip_address(literal)
            addresses = [literal]
        except ValueError:
            try:
                addresses = await asyncio.wait_for(self._resolve(host), 5.0)
            except (OSError, TimeoutError, UnicodeError) as exc:
                raise httpcore.ConnectError(f"couldn't find {host}") from exc
        if not addresses or not all(public_ip(a) for a in addresses):
            raise httpcore.ConnectError(f"{host} leads to this PC or the home network")
        return await self._inner.connect_tcp(addresses[0], port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        import httpcore

        raise httpcore.ConnectError("no local sockets")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def public_client(resolve: Resolver = resolve_host) -> httpx.AsyncClient:
    """An httpx client whose every connection goes only to a checked public address (``PublicOnlyBackend``), trusting
    the certificates the browser trusts."""
    import httpcore

    from octavia.net import tls_context

    transport = httpx.AsyncHTTPTransport(verify=tls_context())
    transport._pool = httpcore.AsyncConnectionPool(  # httpx has no option for the backend; the pool takes one
        ssl_context=tls_context(), network_backend=PublicOnlyBackend(resolve)
    )
    return httpx.AsyncClient(transport=transport)


async def fetch(
    url: str,
    client: httpx.AsyncClient,
    *,
    resolve: Resolver = resolve_host,
    max_bytes: int = MAX_BYTES,
) -> bytes:
    """The bytes at ``url``: a picture, not a page. Every hop (redirects too) is checked with ``check_public``."""
    for _ in range(MAX_REDIRECTS + 1):
        await check_public(url, resolve)
        try:
            async with client.stream(
                "GET", url, headers={"Accept": "image/*,*/*;q=0.5"}, follow_redirects=False, timeout=FETCH_TIMEOUT
            ) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code >= 400:
                    raise PictureError(f"{site_of(url)} answered {response.status_code}")
                kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if kind.startswith(("text/", "application/json", "application/xhtml", "image/svg")):
                    raise PictureError(f"{site_of(url)} sent {kind or 'something'}, not a picture")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise PictureError(f"the picture is too big ({int(declared) // 1_000_000} MB)")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise PictureError(f"the picture is too big (over {max_bytes // 1_000_000} MB)")
                return bytes(body)
        except httpx.TimeoutException as exc:
            raise PictureError(f"{site_of(url)} took too long") from exc
        except httpx.HTTPError as exc:
            raise PictureError(f"couldn't reach {site_of(url)}") from exc
    raise PictureError("too many redirects")


# --------------------------------------------------------------------------- cleaning


def clean(data: bytes, *, max_side: int = MAX_SIDE) -> Cleaned:
    """``data`` decoded and drawn again as a fresh JPEG (PNG when it has see-through parts): first frame only, no
    metadata, at most ``max_side`` a side. ``PictureError`` for anything that isn't a sane picture."""
    from PIL import Image, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                if im.format not in FORMATS:
                    raise PictureError(f"that isn't a picture format a panel shows ({im.format or 'unknown'})")
                width, height = im.size
                if width * height > MAX_PIXELS:
                    raise PictureError("the picture is far too big to show")
                if min(width, height) < MIN_SIDE:
                    raise PictureError("the picture is too small to be worth showing")
                with contextlib.suppress(EOFError):
                    im.seek(0)  # an animation: its first frame
                im.load()
                alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
                frame = im.convert("RGBA" if alpha else "RGB")
    except PictureError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PictureError("that isn't a picture a panel can show") from exc
    except (OSError, ValueError, SyntaxError, MemoryError) as exc:  # truncated or broken files
        raise PictureError("the picture is broken") from exc
    if alpha and frame.getchannel("A").getextrema()[0] == 255:
        frame, alpha = frame.convert("RGB"), False  # an alpha channel with nothing see-through
    frame.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if alpha:
        frame.save(out, "PNG", optimize=True)
        ext = "png"
    else:
        frame.save(out, "JPEG", quality=88, optimize=True, progressive=True)
        ext = "jpg"
    return Cleaned(out.getvalue(), ext, frame.width, frame.height)


def thumbnails(pictures: list[Picture], *, height: int = 300) -> bytes:
    """One JPEG with the pictures side by side, ``height`` tall: her own look at what she put up."""
    from PIL import Image

    tiles = []
    for picture in pictures:
        with Image.open(io.BytesIO(picture.data)) as im:
            tile = im.convert("RGB")
        tile.thumbnail((int(height * 2.5), height))
        tiles.append(tile)
    gap = 8
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + gap * (len(tiles) - 1), max(t.height for t in tiles)))
    x = 0
    for tile in tiles:
        sheet.paste(tile, (x, 0))
        x += tile.width + gap
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=80)
    return out.getvalue()


# --------------------------------------------------------------------------- the store


class ImageStore:
    """Cleaned pictures on disk for the panel app to serve, named by their own bytes."""

    def __init__(self, folder: Path, *, keep_s: float = 24 * 3600, max_files: int = 80) -> None:
        self.folder = folder
        self.keep_s = keep_s
        self.max_files = max_files

    def save(self, cleaned: Cleaned) -> str:
        name = f"img-{hashlib.sha256(cleaned.data).hexdigest()[:32]}.{cleaned.ext}"
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / name
        if not path.exists():
            temp = path.with_name(f"{name}.{secrets.token_hex(4)}.part")  # two saves at once never share one
            temp.write_bytes(cleaned.data)
            try:
                temp.replace(path)
            except OSError:
                temp.unlink(missing_ok=True)
                if not path.exists():
                    raise
        else:
            with contextlib.suppress(OSError):
                path.touch()  # shown again: newest now, so pruning keeps it
        self.prune(keep=name)
        return name

    def path(self, name: str) -> Path | None:
        """Where a stored picture is, or None for a name that isn't one."""
        if not NAME.fullmatch(name):
            return None
        path = self.folder / name
        return path if path.is_file() else None

    def prune(self, keep: str = "") -> None:
        """Drops pictures older than ``keep_s`` and the oldest past ``max_files`` (never ``keep``)."""
        try:
            files = sorted(
                (p for p in self.folder.iterdir() if NAME.fullmatch(p.name) or p.suffix == ".part"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        now = time.time()
        for k, p in enumerate(files):
            if p.name == keep:
                continue
            with contextlib.suppress(OSError):
                if k >= self.max_files or now - p.stat().st_mtime > self.keep_s:
                    p.unlink()


# --------------------------------------------------------------------------- searching


async def bing(query: str, client: httpx.AsyncClient, safe: Safe = "moderate") -> list[Candidate]:
    """Bing's image results for ``query``, best first."""
    adult = {"strict": "Strict", "moderate": "Moderate", "off": "Off"}[safe]
    response = await client.get(
        "https://www.bing.com/images/async",
        params={"q": query, "first": "0", "count": "24", "adlt": adult.lower(), "mmasync": "1"},
        headers={
            "User-Agent": BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": f"SRCHHPGUSR=ADLT={adult.upper()}",
        },
        timeout=SEARCH_TIMEOUT,
        follow_redirects=True,
    )
    response.raise_for_status()
    found = []
    for raw in _BING_ITEM.findall(response.text):
        try:
            item = json.loads(html.unescape(raw))
        except ValueError:
            continue
        image = str(item.get("murl") or "")
        if not image.startswith(("http://", "https://")):
            continue
        thumb = str(item.get("turl") or "")
        title = re.sub(r"[]", "", str(item.get("t") or "")).strip()
        found.append(
            Candidate(image, thumb if thumb.startswith("https://") else "", str(item.get("purl") or ""), title)
        )
    return found


async def commons(query: str, client: httpx.AsyncClient) -> list[Candidate]:
    """Wikimedia Commons' pictures for ``query`` (free to use, fewer of popular culture), best first."""
    from octavia.net import wikimedia_user_agent

    response = await client.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6",
            "gsrlimit": "10",
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": str(MAX_SIDE),
        },
        headers={"User-Agent": wikimedia_user_agent()},
        timeout=SEARCH_TIMEOUT,
    )
    response.raise_for_status()
    pages = (response.json().get("query") or {}).get("pages") or {}
    found = []
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        image = str(info.get("thumburl") or info.get("url") or "")
        if image.startswith("https://"):
            title = re.sub(r"^File:|\.\w+$", "", str(page.get("title") or "")).strip()
            found.append(Candidate(image, "", str(info.get("descriptionurl") or ""), title))
    return found


_FILLER = frozenset(
    "the and with from for of on in at by to an a its his her their this that what how picture photo image "
    "pictures photos images show me".split()
)


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def relevant(query: str, candidate: Candidate) -> bool:
    """Whether the result's title and addresses name enough of what was asked (a third of its words, at least one):
    Bing sometimes answers a query it didn't take with its own unrelated pictures (a rock arch and Santorini for
    "Kazakh eagle hunter"), and those never mention it. A word counts when it's there whole, inside a longer one
    (addresses run words together) or as the same stem ("hunters", "hunter")."""
    wanted = list(dict.fromkeys(w for w in _words(query) if len(w) >= 3 and w not in _FILLER))
    if not wanted:
        return True
    words = _words(" ".join((candidate.title, unquote(candidate.page_url), unquote(candidate.image_url))))
    have, joined = set(words), " ".join(words)

    def named(word: str) -> bool:
        if word in have or (len(word) >= 4 and word in joined):
            return True
        return len(word) >= 6 and word[:5] in joined

    return sum(named(w) for w in wanted) >= max(1, math.ceil(len(wanted) / 3))


async def search(query: str, client: httpx.AsyncClient, safe: Safe = "moderate") -> list[Candidate]:
    """Image results for ``query`` that are about it (``relevant``): Bing's, or Commons' when Bing gives none of
    those or fails. ``PictureError`` when neither answers."""
    errors = []
    for engine in (lambda: bing(query, client, safe), lambda: commons(query, client)):
        try:
            found = await engine()
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(exc)
            log.info("Image search failed: %r", exc)
            continue
        about = [c for c in found if relevant(query, c)]
        if found and not about:
            log.info("Image search for %r gave %d unrelated results", query[:80], len(found))
        if about:
            return about
    if errors and len(errors) == 2:
        raise PictureError("the image search didn't answer")
    return []


# --------------------------------------------------------------------------- all together


async def _one(candidate: Candidate, client: httpx.AsyncClient, resolve: Resolver) -> tuple[Cleaned, Candidate] | None:
    """The candidate's picture, cleaned: the full one, else the search engine's own small copy."""
    for url in (candidate.image_url, candidate.thumb_url):
        if not url:
            continue
        try:
            data = await fetch(url, client, resolve=resolve)
            async with _DECODING:  # a few big decodes at once at most: each can take a few hundred MB
                return await asyncio.to_thread(clean, data), candidate
        except PictureError as exc:
            log.debug("Picture %s skipped: %s", url[:120], exc)
    return None


async def find_pictures(
    candidates: list[Candidate],
    count: int,
    store: ImageStore,
    client: httpx.AsyncClient,
    *,
    resolve: Resolver = resolve_host,
    budget_s: float = 15.0,
) -> list[Picture]:
    """The first ``count`` candidates (in their order) whose picture downloads and cleans, stored. Tried
    ``TRIED_AT_ONCE`` at a time; gives up after ``budget_s`` with what it has."""
    pictures: list[Picture] = []
    seen: set[str] = set()
    pending = list(candidates[:16])
    deadline = asyncio.get_running_loop().time() + budget_s
    while pending and len(pictures) < count:
        batch, pending = pending[:TRIED_AT_ONCE], pending[TRIED_AT_ONCE:]
        tasks = [asyncio.ensure_future(_one(c, client, resolve)) for c in batch]
        try:
            for task in tasks:  # in rank order: the best that worked comes first
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    break
                try:
                    got = await asyncio.wait_for(asyncio.shield(task), left)
                except TimeoutError:
                    break
                if got is None:
                    continue
                cleaned, candidate = got
                digest = hashlib.sha256(cleaned.data).hexdigest()
                if digest in seen:
                    continue  # the same picture on two sites
                seen.add(digest)
                try:
                    name = await asyncio.to_thread(store.save, cleaned)
                except OSError as exc:
                    log.warning("Couldn't keep a picture: %s", exc)
                    continue
                source = candidate.page_url or candidate.image_url
                pictures.append(
                    Picture(name, cleaned.width, cleaned.height, source, site_of(source), candidate.title, cleaned.data)
                )
                if len(pictures) >= count:
                    break  # enough: the rest of the batch is cancelled, not waited for
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if asyncio.get_running_loop().time() >= deadline:
            break
    return pictures
