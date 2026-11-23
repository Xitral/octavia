"""The record of one task's steps, and the playbook a task is handed when it first gets to an app or a site.

``PlaybookExecutor`` wraps a task's executor (``TaskExecutor`` for a screen task, a ``ToolExecutor`` for a web task):
every call still goes through the wrapped one, with all its checks, and the wrapper only writes down what came of it
(``Trace``) and where the task was (``Where``: the window in front for a screen task, the page's address for a web
task). The first time a run of the task gets to an app or a site with a playbook it hasn't seen yet, that step's
result carries the playbook (``book.block``), so a task that opens PowerPoint halfway through still gets the notes.

What's written down is meant for the writer, so it holds no more than the steps need: which tool, what it was aimed at
(a button's name, the keys), whether it went through, and the first line of what came back or the error, each run
through ``futures.privacy.redact``. What was typed or copied never is: only how long it was.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from octavia.futures.privacy import redact
from octavia.playbooks.book import Place, Playbooks, app_of, block, site_of
from octavia.recall import privacy
from octavia.tools.base import ToolResult

log = logging.getLogger(__name__)

MAX_STEPS = 200  # the most a trace keeps: its first ones and its latest
_HEAD = 40
# Not about the app or the site: never written down, never a sign of where the task is.
SKIP = frozenset({"get_current_time", "recall", "read_about_me", "list_running_apps"})
# What they put in is the user's (a form's answers, a password, a document's text): only its length is written down.
TYPED = frozenset({"type_text", "web_type", "copy_to_clipboard"})
# Their arguments name the user's own files: none of them is written down.
NO_ARGS = frozenset({"open_file", "find_files", "read_clipboard"})
# The arguments that say what a step was aimed at, in this order.
_AIMS = ("label", "text", "name", "app", "title", "query", "keys", "times", "direction", "ref", "button", "double", "enter")  # fmt: skip
_ADDRESS = re.compile(r"^Address: (\S+)", re.MULTILINE)
# Where a step left the task when that's somewhere nothing is kept of (``book.app_of``, ``book.site_of``: a bank's
# site, a password manager, a private window): the step is written down without what it did there.
PRIVATE = Place("private", "-")


@dataclass(frozen=True)
class Step:
    tool: str
    aim: str  # what it was aimed at, in a few words
    ok: bool
    said: str  # the first line of the result, or the error
    place: Place | None  # where the task was after it

    def line(self) -> str:
        where = f" [{self.place.key}]" if self.place is not None else ""
        aim = f" {self.aim}" if self.aim else ""
        return f"{'ok' if self.ok else 'FAILED'}{where} {self.tool}{aim}: {self.said}".rstrip(": ")


@dataclass
class Trace:
    """One task's steps, over all of its runs (a screen task paused and picked up again is one task)."""

    steps: list[Step] = field(default_factory=list)
    dropped: int = 0  # steps left out of the middle of a long task
    seen: set[Place] = field(default_factory=set)  # places this run already looked up a playbook for
    shown: set[int] = field(default_factory=set)  # playbooks this run was handed (its prompt included)
    current: Place | None = None
    private: bool = False  # the last step left the task somewhere nothing is kept of

    def new_run(self) -> None:
        """A new run of the task (a fresh brain, which hasn't seen any playbook yet)."""
        self.seen.clear()
        self.shown.clear()

    def add(self, step: Step) -> None:
        self.steps.append(step)
        if len(self.steps) > MAX_STEPS:
            del self.steps[_HEAD]
            self.dropped += 1

    def places(self, limit: int = 3) -> list[Place]:
        """Where the task worked, most steps first."""
        counts = Counter(step.place for step in self.steps if step.place is not None)
        return [place for place, _ in counts.most_common(limit)]

    def lines(self) -> list[str]:
        lines = [step.line() for step in self.steps]
        if self.dropped:
            lines.insert(_HEAD, f"[… {self.dropped} more steps …]")
        return lines

    @property
    def failures(self) -> int:
        return sum(1 for step in self.steps if not step.ok)


def aim_of(tool: str, args: dict[str, Any] | None) -> str:
    """What a step was aimed at, in a few words, without anything the user typed or the names of their files."""
    args = args if isinstance(args, dict) else {}
    if tool in NO_ARGS:
        return ""
    parts = []
    if tool in TYPED:
        typed = args.get("text", args.get("value"))
        parts.append(f"({len(str(typed))} characters)" if typed is not None else "")
        for name in ("ref", "label", "enter", "replace"):
            if name in args and args[name] not in (None, "", False):
                parts.append(f"{name}={args[name]}")
    else:
        for name in _AIMS:
            value = args.get(name)
            if value in (None, "", False):
                continue
            parts.append(f"{name}={redact(str(value), 60)}")
        if "url" in args:
            url = privacy.clean_url(str(args["url"]))
            parts.append(f"url={url}" if url else "url=(not a web address)")
    return " ".join(p for p in parts if p)


def said_of(result: ToolResult) -> str:
    first = next((line.strip() for line in result.content.splitlines() if line.strip()), "")
    return redact(first, 200 if not result.ok else 120)


def address_in(result: ToolResult) -> str:
    """The page's address a web tool's result gives (``WebPage.read``), or ""."""
    found = _ADDRESS.search(result.content[:2000])
    return found.group(1) if found else ""


# Where the task is after a step: (the tool, its arguments, its result) -> the app or site, ``PRIVATE``, or None
# when it can't tell (the task is still where it was).
Where = Callable[[str, dict[str, Any], ToolResult], Awaitable[Place | None]]


def web_where(user_sites: Any = ()) -> Where:
    """For a web task: the site of the page a step left it on (nothing for a step whose result doesn't say)."""

    async def where(tool: str, args: dict[str, Any], result: ToolResult) -> Place | None:
        address = address_in(result)
        if not address:
            return None
        return site_of(address, user_sites) or (PRIVATE if privacy.clean_url(address) else None)

    return where


def screen_where(user_apps: Any = (), user_sites: Any = (), foreground: Callable[[], Any] | None = None) -> Where:
    """For a screen task: the app (or the site, in a browser) of the window in front after a step.
    ``foreground``: ``recall.capture.foreground`` unless a test gives its own."""

    async def where(tool: str, args: dict[str, Any], result: ToolResult) -> Place | None:
        get = foreground
        if get is None:
            from octavia.recall.capture import foreground as get
        window = await asyncio.to_thread(get)
        if window is None:
            return None
        found = app_of(window.process, window.app, window.title, user_apps, user_sites)
        return found or PRIVATE

    return where


class PlaybookExecutor:
    """A task's executor with its steps written down and the playbooks handed over (see the module). Everything else
    is the wrapped executor's."""

    def __init__(self, inner: Any, trace: Trace, books: Playbooks | None, where: Where) -> None:
        self.inner = inner
        self.trace = trace
        self.books = books
        self._where = where

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def execute(self, name: str, raw_args: dict[str, Any] | None, call_id: str | None = None) -> ToolResult:
        result = await self.inner.execute(name, raw_args, call_id)
        if name in SKIP:
            return result
        try:
            place = await self._where(name, raw_args or {}, result)
        except Exception:
            log.debug("Couldn't tell where the task is after %s", name, exc_info=True)
            place = None
        if place is PRIVATE:
            self.trace.current, self.trace.private = None, True
            self.trace.add(Step(name, "", result.ok, "(somewhere private: nothing written down)", None))
            return result
        if place is not None:
            self.trace.current, self.trace.private = place, False
        self.trace.add(Step(name, aim_of(name, raw_args), result.ok, said_of(result), self.trace.current))
        return await self._hand_over(place, result)

    async def _hand_over(self, place: Place | None, result: ToolResult) -> ToolResult:
        """``result`` with ``place``'s playbook added when this run gets there for the first time."""
        if place is None or self.books is None or place in self.trace.seen:
            return result
        self.trace.seen.add(place)
        try:
            book = await self.books.find(place)
        except Exception:
            log.warning("Couldn't read the playbook for %s", place.key, exc_info=True)
            return result
        if book is None or book.id in self.trace.shown:
            return result
        self.trace.shown.add(book.id)
        return replace(result, content=f"{result.content}\n\n{block([book])}")
