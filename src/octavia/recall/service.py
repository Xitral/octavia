"""Screen history's watcher: decides when to look at the screen, keeps what's new, and answers the tools.

Every ``poll_s`` seconds it asks Windows which window is in front (a few cheap calls). It reads a window's text only

* once it has stayed in front for ``dwell_s`` (alt-tabbing past windows reads none of them), and again every
  ``recapture_s`` while it stays there and the user is at the PC, so scrolling down a page adds what comes into view;
* while the user is active (no key or mouse for ``idle_after_s`` and it stops looking);
* while the PC isn't busy (above ``max_cpu_percent`` it waits for the next look);
* never in a full-screen app that isn't on ``FULLSCREEN_TEXT_APPS`` (a game): only that it was open is kept.

When UI Automation finds next to no text in an app on the OCR list, Windows' offline OCR reads it instead, at most
every ``ocr_every_s`` per window. Nothing is sent anywhere during any of this: it's all on this PC.

What's kept is what ``privacy`` lets through, and only the lines that are new for that page (the same app and
address, or title). A private window, an excluded app or a banking site leaves no trace at all: not even its title.

Threads: the screen is read on ``capture.thread()``; the database is used from one thread of its own (``_db``). Both
run at background priority. The event loop only waits on them.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

from octavia.recall import capture, privacy
from octavia.recall.store import Hit, ScreenHistoryStore, Span

log = logging.getLogger(__name__)

# Full-screen apps whose text is still read (slides being presented, a PDF, a video call); every other full-screen app
# is taken for a game and only noted as open.
FULLSCREEN_TEXT_APPS: frozenset[str] = privacy.BROWSERS | frozenset(
    {
        "powerpnt.exe",
        "winword.exe",
        "excel.exe",
        "onenote.exe",
        "acrobat.exe",
        "acrord32.exe",
        "sumatrapdf.exe",
        "ms-teams.exe",
        "teams.exe",
        "zoom.exe",
        "code.exe",
        "notepad.exe",
        "obsidian.exe",
        "notion.exe",
    }
)
# Apps that may be read with OCR when they describe too little to UI Automation. Not games: reading a game's window
# could make it stutter.
# Browsers aren't on it: their private badge and address bar come from UI Automation, which OCR would bypass.
OCR_APPS: frozenset[str] = (FULLSCREEN_TEXT_APPS - privacy.BROWSERS) | frozenset(
    {"discord.exe", "slack.exe", "spotify.exe", "whatsapp.exe", "telegram.exe", "signal.exe", "photos.exe"}
)
# Browsers aren't read at all. Reading Vivaldi through UI Automation every 30 s froze it for good (measured
# 2026-10-09 on a test copy: 2 of 9 runs hung mid-read and never recovered; none without the reads), and Windows
# logged 8 Vivaldi hangs in the two days after screen history arrived, none in the month before. Without a read,
# a browser's private badge can't be checked, so nothing of it is kept, not even its title.
READ_BROWSERS = False
OCR_MIN_CHARS = 80  # less text than this from UI Automation, and OCR is tried
SEEN_PAGES = 300  # pages whose lines are remembered, so the same text isn't kept twice
SEEN_LINES = 5000  # per page
OWN_PIDS_EVERY_S = 60.0
PRUNE_EVERY_S = 3600.0
EXTEND_EVERY_S = 30.0  # how often the end of the span in front is written while it stays there
UNTIL_RESUMED = -1.0  # paused_until for a pause with no end


@dataclass
class Options:
    poll_s: float = 2.0
    dwell_s: float = 3.0
    recapture_s: float = 30.0
    idle_after_s: float = 120.0
    max_cpu_percent: float = 85.0
    retention_days: float = 90.0
    max_mb: float = 1000.0
    ocr: bool = True
    ocr_every_s: float = 120.0
    exclude_apps: list[str] = field(default_factory=list)
    exclude_sites: list[str] = field(default_factory=list)


@dataclass
class Stats:
    looks: int = 0  # windows read
    ocr: int = 0
    kept: int = 0  # snippets written
    busy: int = 0  # looks put off because the PC was busy
    blocked: int = 0  # windows not kept at all (private, excluded, banking)
    capture_cpu_s: float = 0.0  # CPU time the reading thread spent, all looks together
    capture_wall_s: float = 0.0
    started: float = field(default_factory=time.time)

    def summary(self) -> dict[str, Any]:
        looks = max(self.looks, 1)
        return {
            "looks": self.looks,
            "ocr": self.ocr,
            "kept": self.kept,
            "busy": self.busy,
            "blocked": self.blocked,
            "cpu_ms_per_look": round(1000 * self.capture_cpu_s / looks, 1),
            "wall_ms_per_look": round(1000 * self.capture_wall_s / looks, 1),
            "cpu_share_percent": round(100 * self.capture_cpu_s / max(time.time() - self.started, 1.0), 3),
        }


@dataclass
class _Current:
    key: tuple[int, str]  # (window, title)
    since: float
    span: int | None = None
    page: str = ""
    url: str = ""
    last_look: float = 0.0
    last_ocr: float = 0.0
    extended: float = 0.0
    done: bool = False  # read, or deliberately not (a game)


def _background_db_thread() -> None:
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), capture._THREAD_MODE_BACKGROUND_BEGIN)
    except Exception:
        pass


def _cpu_percent() -> float:
    try:
        import psutil

        return float(psutil.cpu_percent(None))
    except Exception:
        return 0.0


def _own_pids() -> frozenset[int]:
    """This process and its children (the avatar app): her own windows aren't kept."""
    pids = {os.getpid()}
    try:
        import psutil

        pids |= {child.pid for child in psutil.Process().children(recursive=True)}
    except Exception:
        pass
    return frozenset(pids)


def _hash(line: str) -> bytes:
    return hashlib.blake2b(line.encode("utf-8", "replace"), digest_size=8).digest()


class ScreenHistory:
    def __init__(
        self,
        store: ScreenHistoryStore,
        options: Options | None = None,
        *,
        foreground: Callable[[], capture.Foreground | None] = capture.foreground,
        read: Callable[[int, str, bool], capture.WindowText] = capture.read_window,
        ocr: Callable[[int], list[str]] = capture.ocr_window,
        ocr_available: Callable[[], bool] = capture.ocr_available,
        idle: Callable[[], float] = capture.idle_seconds,
        cpu: Callable[[], float] = _cpu_percent,
        own_pids: Callable[[], frozenset[int]] = _own_pids,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        capture_executor: Any = None,
        db_executor: Any = None,
    ) -> None:
        self.store = store
        self.options = options or Options()
        self._foreground = foreground
        self._read = read
        self._ocr = ocr
        self._ocr_available = ocr_available
        self._idle = idle
        self._cpu = cpu
        self._own_pids_fn = own_pids
        self.clock = clock
        self._sleep = sleep
        self._capture_executor = capture_executor
        self._db_executor = db_executor
        self._task: asyncio.Task[None] | None = None
        self._current: _Current | None = None
        self._seen: OrderedDict[str, set[bytes]] = OrderedDict()
        self._private_windows: set[int] = set()
        self._own: frozenset[int] = frozenset()
        self._own_at = -OWN_PIDS_EVERY_S
        self._pruned_at = 0.0
        self._generation = 0  # goes up when the user forgets or pauses: a look that started before is dropped
        self._paused_until: float | None = None
        self._user_apps: list[str] = []
        self._user_sites: list[str] = []
        self.stats = Stats()
        self.problem = ""
        # Told after anything is deleted, so what's on the user's screen (the filmstrip) goes with it.
        self.on_forgotten: list[Callable[[], Awaitable[None]]] = []
        self._cpu()  # psutil measures from one call to the next

    # ------------------------------------------------------------------ running

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        await self._db(self.store.open)
        self._paused_until = await self._db(self.store.setting, "paused_until", None)
        self._user_apps = list(await self._db(self.store.setting, "exclude_apps", []))
        self._user_sites = list(await self._db(self.store.setting, "exclude_sites", []))
        # Pruning waits for the first look in the background, so a big prune never holds up her start.
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="screen-history")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._end_span()
        with contextlib.suppress(Exception):
            await self._db(self.store.close)
        if self._db_executor is not None and isinstance(self._db_executor, ThreadPoolExecutor):
            self._db_executor.shutdown(wait=False)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
                self.problem = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if str(exc) != self.problem:
                    log.info("Screen history couldn't look at the screen: %s", exc, exc_info=True)
                self.problem = str(exc)
            await self._sleep(self.options.poll_s)

    async def _db(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self._db_executor is None:
            self._db_executor = ThreadPoolExecutor(
                1, thread_name_prefix="octavia-recall-db", initializer=_background_db_thread
            )
        return await asyncio.get_running_loop().run_in_executor(self._db_executor, fn, *args)

    async def _on_capture_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        executor = self._capture_executor if self._capture_executor is not None else capture.thread()
        return await asyncio.get_running_loop().run_in_executor(executor, fn, *args)

    # ------------------------------------------------------------------ one look

    def paused(self, now: float | None = None) -> bool:
        until = self._paused_until
        if until is None:
            return False
        return until == UNTIL_RESUMED or (now if now is not None else self.clock()) < until

    async def tick(self) -> None:
        now = self.clock()
        if now - self._pruned_at >= PRUNE_EVERY_S:
            await self._prune()
        if self.paused(now):
            await self._end_span()
            return
        idle = self._idle()
        if idle >= self.options.idle_after_s:
            await self._end_span(now - idle)  # the window was left when the user stopped using the PC
            return
        fg = await self._on_capture_thread(self._foreground)
        if fg is None or fg.pid in self._own_pids(now) or self._blocked_window(fg):
            await self._end_span()
            return
        key = (fg.hwnd, fg.title)
        current = self._current
        if current is None or current.key != key:
            await self._end_span()
            self._current = _Current(key=key, since=now)
            return
        if current.span is not None and now - current.extended >= EXTEND_EVERY_S:
            current.extended = now  # the span's end is written now and then, and when the window changes
            await self._db(self.store.extend_span, current.span, now)
        if now - current.since < self.options.dwell_s:
            return
        if current.done and now - current.last_look < self.options.recapture_s:
            return
        await self._look(fg, current, now)

    def _own_pids(self, now: float) -> frozenset[int]:
        if now - self._own_at >= OWN_PIDS_EVERY_S:
            self._own, self._own_at = self._own_pids_fn(), now
        return self._own

    def _blocked_window(self, fg: capture.Foreground) -> bool:
        """Never kept, judged before reading anything: an excluded app, a private window, a bank's title."""
        if (
            fg.hwnd in self._private_windows
            or privacy.excluded_app(fg.process, [*self.options.exclude_apps, *self._user_apps])
            or privacy.private_title(fg.title, fg.process)
            or privacy.excluded_site("", fg.title, ())
        ):
            self.stats.blocked += 1
            return True
        return False

    async def _look(self, fg: capture.Foreground, current: _Current, now: float) -> None:
        process = privacy.norm_app(fg.process)
        game = fg.fullscreen and process not in FULLSCREEN_TEXT_APPS
        if game:
            if current.span is None:
                current.span = await self._open_span(fg, current, "")
            current.done, current.last_look = True, now
            return
        if process in privacy.BROWSERS and not READ_BROWSERS:
            current.done, current.last_look = True, now
            self.stats.blocked += 1
            return
        if self._cpu() >= self.options.max_cpu_percent:
            self.stats.busy += 1
            return
        generation = self._generation
        # An Electron app (Discord, Slack, VS Code) gets the shallow read: the full one (the page's text on top of
        # every element and its value) is what froze Vivaldi, which is Chromium too.
        shallow = fg.chromium and process not in privacy.BROWSERS
        found, cpu_s, wall_s = await self._on_capture_thread(_timed, self._read, fg.hwnd, fg.process, shallow)
        self.stats.looks += 1
        self.stats.capture_cpu_s += cpu_s
        self.stats.capture_wall_s += wall_s
        current.done, current.last_look = True, now
        if found.private:
            self._private_windows.add(fg.hwnd)
            await self._drop_current()
            self.stats.blocked += 1
            return
        url = found.url  # never an earlier page's: a page that turned out to be a bank must be judged by its own
        if process in privacy.BROWSERS and not (found.complete and url):
            # A browser whose toolbar couldn't be read (full screen, its own settings and password pages, or a look
            # cut short) can't be checked for a private window or a bank: nothing from this look is kept. What
            # earlier looks of the same window and page kept, when it could be checked, stays.
            self.stats.blocked += 1
            return
        if privacy.excluded_site(url, fg.title, [*self.options.exclude_sites, *self._user_sites]):
            await self._drop_current()
            self.stats.blocked += 1
            return
        lines = found.lines
        source = "uia"
        if (
            self.options.ocr
            and sum(len(line) for line in lines) < OCR_MIN_CHARS
            and process in OCR_APPS
            and now - current.last_ocr >= self.options.ocr_every_s
            and self._ocr_available()
        ):
            current.last_ocr = now
            ocr_lines, cpu_s, wall_s = await self._on_capture_thread(_timed, self._ocr, fg.hwnd)
            self.stats.ocr += 1
            self.stats.capture_cpu_s += cpu_s
            self.stats.capture_wall_s += wall_s
            if ocr_lines:
                lines, source = ocr_lines, "ocr"
        if self._stale(generation, current):
            return  # forgotten or paused meanwhile, or the window changed
        if current.span is None:
            current.url = url
            current.span = await self._open_span(fg, current, url)
            if self._stale(generation, current):
                # The user said forget or pause while the span was being written: it goes, and nothing is added.
                await self._db(self.store.forget_span, current.span)
                await self._forgotten()
                return
        page = f"{process}|{url or fg.title}"
        new = self._new_lines(page, privacy.clean_lines(lines))
        if not new:
            return
        title = privacy.clean_title(fg.title)
        await self._db(self.store.add_snippet, current.span, now, "\n".join(new), title, fg.app, url, source)
        self._mark_seen(page, new)
        self.stats.kept += 1

    def _stale(self, generation: int, current: _Current) -> bool:
        return generation != self._generation or self._current is not current or self.paused()

    async def _open_span(self, fg: capture.Foreground, current: _Current, url: str) -> int:
        return int(
            await self._db(
                self.store.open_span,
                current.since,
                fg.app,
                privacy.norm_app(fg.process),
                privacy.clean_title(fg.title),
                url,
                privacy.host_of(url),
                fg.fullscreen,
            )
        )

    def _new_lines(self, page: str, lines: list[str]) -> list[str]:
        seen = self._seen.get(page)
        if seen is None:
            seen = self._seen[page] = set()
            while len(self._seen) > SEEN_PAGES:
                self._seen.popitem(last=False)
        else:
            self._seen.move_to_end(page)
        new: list[str] = []
        fresh: set[bytes] = set()
        for line in lines:
            digest = _hash(line)
            if digest not in seen and digest not in fresh:
                fresh.add(digest)
                new.append(line)
        return new

    def _mark_seen(self, page: str, lines: list[str]) -> None:
        """Once they're written: a write that failed leaves them to be kept at the next look."""
        seen = self._seen.setdefault(page, set())
        if len(seen) > SEEN_LINES:
            seen.clear()
        seen.update(_hash(line) for line in lines)

    async def _end_span(self, at: float | None = None) -> None:
        current, self._current = self._current, None
        if current is not None and current.span is not None:
            end = self.clock() if at is None else max(at, current.extended, current.since)
            await self._db(self.store.extend_span, current.span, end)

    async def _drop_current(self) -> None:
        """The window in front turned out to be one never kept: what was written for it goes."""
        current, self._current = self._current, None
        if current is not None and current.span is not None:
            await self._db(self.store.forget_span, current.span)
            await self._forgotten()

    async def _forgotten(self) -> None:
        for listener in list(self.on_forgotten):
            try:
                await listener()
            except Exception:
                log.warning("Something showing screen history couldn't follow a forget", exc_info=True)

    async def _prune(self) -> None:
        self._pruned_at = self.clock()
        max_bytes = int(self.options.max_mb * 1024 * 1024)
        try:
            keep = self._current.span if self._current is not None else None
            gone = await self._db(self.store.prune, self.options.retention_days, max_bytes, self._pruned_at, keep)
        except Exception:
            log.warning("Couldn't prune the screen history", exc_info=True)
            return
        if gone:
            log.info(
                "Screen history: %d old windows deleted (older than %g days or over %g MB)",
                gone,
                self.options.retention_days,
                self.options.max_mb,
            )
            await self._forgotten()

    # ------------------------------------------------------------------ what the tools call

    async def search(
        self, query: str, since: float | None, until: float | None, app: str | None, limit: int
    ) -> list[Hit]:
        return list(await self._db(self.store.search, query, since, until, app, limit))

    async def spans(self, since: float | None, until: float | None, app: str | None, limit: int) -> list[Span]:
        return list(await self._db(self.store.spans, since, until, app, limit))

    async def text_of(self, span_id: int, limit_chars: int) -> str:
        return str(await self._db(self.store.text_of_span, span_id, limit_chars))

    async def day(self, since: float, until: float, chars_each: int) -> list[tuple[Span, str]]:
        """A day's windows and their text as the filmstrip may show them. The rules that decide what's kept are asked
        again: something kept before the user excluded it, or before a rule changed, doesn't come back on screen."""
        apps = [*self.options.exclude_apps, *self._user_apps]
        sites = [*self.options.exclude_sites, *self._user_sites]
        shown: list[tuple[Span, str]] = []
        for span, text in await self._db(self.store.day, since, until, chars_each):
            if (
                privacy.excluded_app(span.process, apps)
                or privacy.private_title(span.title, span.process)
                or privacy.excluded_site(span.url, span.title, sites)
            ):
                continue
            title = privacy.clean_title(span.title)
            shown.append((replace(span, title=title), "\n".join(privacy.clean_lines(text.splitlines()))))
        return shown

    async def next_window(self, at: float, *, later: bool) -> float | None:
        return await self._db(partial(self.store.next_window, at, later=later))

    async def pause(self, minutes: float | None) -> float:
        """Stop looking for ``minutes`` (None: until ``resume``). Returns when it ends (``UNTIL_RESUMED``)."""
        until = UNTIL_RESUMED if minutes is None else self.clock() + minutes * 60
        self._generation += 1
        self._paused_until = until
        await self._db(self.store.set_setting, "paused_until", until)
        await self._end_span()
        return until

    async def resume(self) -> None:
        self._paused_until = None
        await self._db(self.store.set_setting, "paused_until", None)

    async def forget_recent(self, minutes: float) -> int:
        self._generation += 1
        self._current = None  # what's in front now starts over as a new window (forget_since handles the old one)
        gone = int(await self._db(self.store.forget_since, self.clock() - minutes * 60))
        await self._forgotten()
        return gone

    async def forget_matching(self, app_or_site: str) -> int:
        self._generation += 1
        self._current = None
        gone = int(await self._db(self.store.forget_matching, app_or_site))
        await self._forgotten()
        return gone

    async def forget_all(self) -> None:
        self._generation += 1
        await self._drop_current()
        self._seen.clear()
        await self._db(self.store.forget_all)
        await self._forgotten()

    async def exclude(self, app_or_site: str) -> tuple[str, str]:
        """Never keep this app or site again, and forget what was kept of it. A site ("mychart.org") is that site;
        a name ("Discord") is both the app and any site of that name (discord.com in a browser). Returns ("site" or
        "app and site", the entry)."""
        value = app_or_site.strip().lower()
        if "." in value and not value.endswith(".exe"):
            entry, kind, lists = privacy.clean_site(value), "site", (("sites", self._user_sites),)
        else:
            entry = value.removesuffix(".exe")
            kind, lists = "app and site", (("apps", self._user_apps), ("sites", self._user_sites))
        for name, kept in lists:
            if entry and entry not in kept:
                kept.append(entry)
                await self._db(self.store.set_setting, f"exclude_{name}", kept)
        await self.forget_matching(entry)
        return kind, entry

    async def include(self, app_or_site: str) -> bool:
        """Take an app or site the user excluded off their list. The built-in exclusions stay."""
        value = app_or_site.strip().lower()
        changed = False
        for kind, kept in (("apps", self._user_apps), ("sites", self._user_sites)):
            gone = [e for e in kept if e in (value, value.removesuffix(".exe"), privacy.clean_site(value))]
            for entry in gone:
                kept.remove(entry)
            if gone:
                changed = True
                await self._db(self.store.set_setting, f"exclude_{kind}", kept)
        return changed

    async def status(self) -> dict[str, Any]:
        counts = await self._db(self.store.counts)
        return {
            **counts,
            "running": self.running,
            "paused": self.paused(),
            "paused_until": self._paused_until,
            "exclude_apps": list(self._user_apps),
            "exclude_sites": list(self._user_sites),
            "ocr": self.options.ocr and self._ocr_available(),
            **self.stats.summary(),
        }


def _timed(fn: Callable[..., Any], *args: Any) -> tuple[Any, float, float]:
    """``fn(*args)`` with the CPU time this thread spent on it and the wall time."""
    cpu, wall = time.thread_time(), time.perf_counter()
    result = fn(*args)
    return result, time.thread_time() - cpu, time.perf_counter() - wall


SERVICE: ScreenHistory | None = None  # set by the app when screen history runs
_service_lock = threading.Lock()


def set_service(service: ScreenHistory | None) -> None:
    global SERVICE
    with _service_lock:
        SERVICE = service
