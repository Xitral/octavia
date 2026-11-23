"""Runs her marks app and draws marks with it: arrows, circles, boxes, highlights and labels over the user's real
screen, to guide them ("click here, then this field"). Create one ``MarksHost`` (the app keeps it in
``ctx.extras["marks"]``); nothing starts until the first ``draw``.

The app (``avatar/marks``, on the avatar's Electron) draws in one click-through, never-focusable, always-on-top
window per monitor, converting the physical desktop pixels her screenshots and UI Automation use into each
monitor's own scaling. It's started on first use and quits after ``visuals.idle_quit_s`` with nothing up, like the
panel app (``octavia.visuals.host``), whose ways it follows.

One set of marks is up at a time: ``draw`` replaces it in place (so a walkthrough updates as she talks) and returns
only once every monitor it needs has painted them, or raises ``MarksError`` saying why not: she never says she marked
something that isn't there. They clear by themselves after ``visuals.marks_stay_s``, when she clears them, or when
the user presses her stop hotkey (``clear_soon``).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from octavia.config import AppConfig

log = logging.getLogger(__name__)

APP = "avatar/marks"
CONFIG_FILE = "data/visuals/marks.json"
READY_TIMEOUT_S = 20.0  # Electron's first start after boot can take a few seconds
DRAWN_TIMEOUT_S = 8.0  # the app gives up on a paint after 4 s and says so
CLOSE_WAIT_S = 3.0
SEND_TIMEOUT_S = 3.0
MAX_MARKS = 24  # what the app draws at most
LABEL_CHARS = 80

Kind = Literal["circle", "box", "highlight", "arrow", "label"]
KINDS: tuple[str, ...] = ("circle", "box", "highlight", "arrow", "label")
Rect = tuple[int, int, int, int]  # left, top, right, bottom in physical desktop pixels


class MarksError(RuntimeError):
    """Marks couldn't be drawn (or cleared); the message says why, in words she can pass on."""


class MarksCleared(MarksError):
    """The marks were cleared (the user's stop hotkey, or her) while this draw was on its way: nothing is up."""


@dataclass(frozen=True)
class Mark:
    """One mark, in physical desktop pixels: the spot it's about, and the thing's extent when it has one."""

    kind: Kind
    x: int
    y: int
    box: Rect | None = None
    label: str = ""  # words written beside it
    step: int | None = None  # its number in a walkthrough
    done: bool = False  # a step already shown: drawn quieter

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "x": self.x,
            "y": self.y,
            "box": list(self.box) if self.box else None,
            "label": self.label[:LABEL_CHARS],
            "step": self.step,
            "done": self.done,
        }


@dataclass(frozen=True)
class Drawn:
    """Marks that are on screen now."""

    id: str
    shown: int  # how many were drawn
    missing: tuple[int, ...]  # the indexes of marks not drawn: on no monitor, or nothing the app could draw
    displays: int  # how many monitors they're on
    stay_s: float  # 0: until they're cleared


@dataclass(frozen=True)
class MarksEvent:
    kind: str  # "cleared"
    id: str
    by: str  # "timer", "octavia", "user" (the stop hotkey), "app" (the app went away)


class MarksHost:
    def __init__(
        self,
        config: AppConfig,
        *,
        command: list[str] | None = None,
        spawn: Callable[..., Any] | None = None,
    ) -> None:
        """``command`` replaces the app's command line (tests run a stand-in); ``spawn`` stands in for
        ``asyncio.create_subprocess_exec``."""
        self.config = config
        self._command = command
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._proc: Any = None
        self._alive = False
        self._ready: asyncio.Future[None] | None = None
        self._start_lock = asyncio.Lock()
        self._draw_lock = asyncio.Lock()  # one draw at a time, in order
        self._readers: list[asyncio.Task[None]] = []
        self._waiter: tuple[str, asyncio.Future[dict[str, Any]]] | None = None
        self._up: tuple[str, tuple[Mark, ...]] | None = None  # the draw on screen now
        self._listeners: list[Callable[[MarksEvent], None]] = []
        self._idle: asyncio.TimerHandle | None = None
        self._ids = itertools.count(1)
        self._clears = 0  # how many clears there have been: a draw that sees this change on its way is called off
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    # ------------------------------------------------------------------ what's up

    @property
    def on_screen(self) -> tuple[Mark, ...]:
        """The marks up now (empty when none)."""
        return self._up[1] if self._up else ()

    @property
    def clears(self) -> int:
        """How many times the marks were cleared by her or the user (not by the timer): a walkthrough stops when this
        changes under it."""
        return self._clears

    @property
    def pid(self) -> int | None:
        """The marks app's process id while it runs (its windows are hers, not the user's), else None."""
        proc = self._proc
        if proc is None or not self._alive or proc.returncode is not None:
            return None
        pid = getattr(proc, "pid", None)
        return pid if isinstance(pid, int) else None

    def subscribe(self, listener: Callable[[MarksEvent], None]) -> Callable[[], None]:
        """Calls ``listener`` (on the event loop) each time the marks are cleared. Returns a function that stops it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    # ------------------------------------------------------------------ drawing and clearing

    async def draw(self, marks: Sequence[Mark], *, stay_s: float | None = None) -> Drawn:
        """Puts ``marks`` on screen in place of whatever was up, and returns once they're showing. ``stay_s``: how
        long they stay; None for the setting, 0 for until they're cleared."""
        if self._closed:
            raise MarksError("I'm shutting down, so I can't draw anything.")
        if not marks:
            raise MarksError("There was nothing to draw.")
        if len(marks) > MAX_MARKS:
            raise MarksError(f"That's too many marks at once (at most {MAX_MARKS}).")
        stay = self.config.visuals.marks_stay_s if stay_s is None else max(0.0, min(float(stay_s), 3600.0))
        clears = self._clears
        async with self._draw_lock:
            try:
                return await self._draw(marks, stay, clears)
            finally:
                if self._up is None and not self._closed:
                    self._schedule_idle_quit()  # nothing up: the app may quit after a while, however this went

    async def _draw(self, marks: Sequence[Mark], stay: float, clears: int) -> Drawn:
        await self._ensure_started()
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if self._clears != clears:  # cleared (the stop hotkey) while the app was starting
            raise MarksCleared("The marks were cleared before they came up.")
        draw_id = f"m{next(self._ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiter = (draw_id, future)
        settled = False
        try:
            wire = [m.to_wire() for m in marks]
            await self._send({"type": "draw", "id": draw_id, "marks": wire, "stay_s": stay})
            msg = await asyncio.wait_for(future, DRAWN_TIMEOUT_S)
            settled = True
        except TimeoutError:
            raise MarksError("The marks didn't come up in time.") from None
        finally:
            if self._waiter is not None and self._waiter[0] == draw_id:
                self._waiter = None
            if not settled:  # given up on: nothing she doesn't know of may stay up
                self._forget("octavia")
                self._clear_in_background()
        if self._clears != clears:  # cleared while it was being drawn: the app took it down, or will
            self._forget("user")
            self._clear_in_background()
            raise MarksCleared("The marks were cleared before they came up.")
        if msg.get("type") == "error":
            self._forget("octavia")
            self._clear_in_background()  # whatever was up before goes too: she's told nothing is
            raise MarksError(f"The marks couldn't be drawn: {msg.get('message') or 'no reason given'}.")
        gone = [*(msg.get("missing") or ()), *(msg.get("dropped") or ())]
        missing = tuple(sorted({int(i) for i in gone if isinstance(i, int) and not isinstance(i, bool)}))
        shown = int(msg.get("shown") or 0)
        if shown <= 0:
            self._forget("octavia")
            raise MarksError("None of those places is on a screen now, so nothing was drawn.")
        self._up = (draw_id, tuple(marks))
        return Drawn(draw_id, shown, missing, int(msg.get("displays") or 0), stay)

    async def clear(self, by: str = "octavia") -> bool:
        """Takes every mark down, and calls off a draw on its way. Returns whether any were up."""
        had = self._up is not None
        self._clears += 1
        self._forget(by)
        if self.pid is None:
            if not self._closed:
                self._schedule_idle_quit()
            return had
        with contextlib.suppress(MarksError):
            await self._send({"type": "clear", "by": "user" if by == "user" else "octavia"})
        return had

    def clear_soon(self, by: str = "user") -> None:
        """``clear`` from outside a coroutine on the event loop (the stop hotkey): nothing to wait for."""
        if self._up is None and not self._draw_lock.locked():
            return  # nothing up and nothing on its way
        self._clears += 1  # at once: a draw finishing before the task runs is called off too
        task = asyncio.get_running_loop().create_task(self.clear(by))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _forget(self, by: str) -> None:
        up, self._up = self._up, None
        if up is not None:
            self._emit(MarksEvent("cleared", up[0], by))

    def _clear_in_background(self) -> None:
        if self.pid is None:
            return
        task = asyncio.get_running_loop().create_task(self._send({"type": "clear", "by": "octavia"}))
        self._tasks.add(task)
        task.add_done_callback(lambda t: (self._tasks.discard(t), t.cancelled() or t.exception()))

    def _emit(self, event: MarksEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("A marks listener failed")
        if self._up is None and not self._closed:
            self._schedule_idle_quit()

    # ------------------------------------------------------------------ the app

    def _app_command(self, config_path: str) -> list[str]:
        from octavia.avatar.bridge import electron_path  # the avatar's Electron, whether or not the avatar is on

        root = self.config.root
        exe = electron_path(root, self.config.avatar.electron)
        if not exe.is_file():
            raise MarksError("Drawing on the screen needs the avatar app's Electron; run `uv run octavia avatar setup`.")
        return [str(exe), str(root / APP), "--config", config_path]

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._alive and self._proc is not None and self._proc.returncode is None:
                return
            if self._proc is not None:  # one that crashed or never got going: make sure it's gone first
                await self._stop(self._proc)
            config_path = self.config.resolve_path(CONFIG_FILE)
            app_config = {
                "hideFromCapture": self.config.visuals.hide_from_capture,
                "debug": log.isEnabledFor(logging.DEBUG),
            }
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(json.dumps(app_config), encoding="utf-8")
            except OSError as exc:
                raise MarksError(f"I couldn't write the marks app's settings ({exc.strerror or exc}).") from None
            command = self._command or self._app_command(str(config_path))
            self._ready = asyncio.get_running_loop().create_future()
            try:
                self._proc = await self._spawn(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise MarksError(f"The marks app didn't start ({exc.strerror or exc}).") from None
            proc = self._proc
            self._readers = [
                asyncio.create_task(self._read_stdout(proc), name="marks-stdout"),
                asyncio.create_task(self._read_stderr(proc), name="marks-stderr"),
            ]
            try:
                await asyncio.wait_for(asyncio.shield(self._ready), READY_TIMEOUT_S)
                self._alive = self._proc is proc and proc.returncode is None
                if not self._alive:
                    raise MarksError("The marks app closed as it started.")
            except TimeoutError:
                await self._stop(proc)
                raise MarksError("The marks app didn't start in time.") from None
            except (MarksError, asyncio.CancelledError):
                await asyncio.shield(self._stop(proc))  # a start given up on leaves no app behind
                raise

    async def _send(self, msg: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or not self._alive or proc.returncode is not None or proc.stdin is None:
            raise MarksError("The marks app isn't running.")
        try:
            proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await asyncio.wait_for(proc.stdin.drain(), SEND_TIMEOUT_S)
        except TimeoutError:
            self._alive = False
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()  # it stopped reading: its stdout closing then reports its marks as gone
            raise MarksError("The marks app stopped responding, so I closed it.") from None
        except (ConnectionError, OSError) as exc:
            raise MarksError(f"The marks app stopped listening ({exc}).") from None

    async def _read_stdout(self, proc: Any) -> None:
        stream = proc.stdout
        try:
            while stream is not None:
                try:
                    line = await stream.readline()
                except ValueError:  # a line too long to read: skipped, not taken for the app going away
                    continue
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    log.debug("marks app: %s", line[:200])
                    continue
                if isinstance(msg, dict):
                    self._on_message(msg)
        finally:
            self._app_gone(proc)

    async def _read_stderr(self, proc: Any) -> None:
        stream = proc.stderr
        while stream is not None:
            try:
                line = await stream.readline()
            except ValueError:
                continue
            if not line:
                return
            log.debug("marks app: %s", line.decode("utf-8", errors="replace").rstrip())

    def _on_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)
            return
        mid = msg.get("id")
        waiter = self._waiter
        if kind in ("drawn", "error") and waiter is not None and mid == waiter[0]:
            if not waiter[1].done():
                waiter[1].set_result(msg)
            return
        if kind == "error":
            log.warning("Marks app: %s", msg.get("message"))
        elif kind == "cleared" and self._up is not None and mid == self._up[0]:
            self._forget(str(msg.get("by") or "app"))  # the timer ran out

    def _app_gone(self, proc: Any) -> None:
        """The app's stdout closed: it exited or crashed. Whatever it had up is gone with it."""
        if proc is not self._proc:
            return
        self._alive = False
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(MarksError("The marks app closed as it started."))
            self._ready.exception()  # retrieved, so an unawaited failure isn't logged as never seen
        if self._waiter is not None and not self._waiter[1].done():
            self._waiter[1].set_exception(MarksError("The marks app closed before the marks came up."))
        self._forget("app")

    def _schedule_idle_quit(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        delay = self.config.visuals.idle_quit_s
        if delay <= 0 or self._proc is None:
            return
        loop = asyncio.get_running_loop()
        self._idle = loop.call_later(delay, self._start_idle_quit)

    def _start_idle_quit(self) -> None:
        task = asyncio.get_running_loop().create_task(self._quit_if_idle())
        self._tasks.add(task)
        task.add_done_callback(lambda t: (self._tasks.discard(t), t.cancelled() or t.exception()))

    async def _quit_if_idle(self) -> None:
        self._idle = None
        if self._up is not None or self._waiter is not None or self._proc is None:
            return
        async with self._start_lock:
            if self._up is None and self._waiter is None and self._proc is not None:
                await self._stop(self._proc)

    async def _stop(self, proc: Any) -> None:
        if proc.returncode is None:
            with contextlib.suppress(MarksError):
                await self._send({"type": "quit"})
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), CLOSE_WAIT_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), CLOSE_WAIT_S)
        readers = asyncio.gather(*self._readers, return_exceptions=True)
        try:
            await asyncio.wait_for(readers, CLOSE_WAIT_S)
        except TimeoutError:
            log.warning("The marks app's output didn't close")
        if self._proc is proc:
            self._proc = None
            self._ready = None
            self._alive = False
        self._readers = []

    async def aclose(self) -> None:
        """Clears the marks and closes the app (she's shutting down)."""
        if self._closed:
            return
        self._closed = True
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        self._up = None
        for task in list(self._tasks):
            task.cancel()
        if self._proc is not None:
            await self._stop(self._proc)
