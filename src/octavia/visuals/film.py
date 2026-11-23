"""The screen history filmstrip: a day of what she kept, on one panel (id ``film``), to drag through.

A frame is a window that was in front: its app, title, site and the text read of it. Screen history keeps text, never
pictures (docs/screen-history.md), and this doesn't change that: the frames show the kept words, nothing is captured
for them. A stretch with no window is a gap on the strip that says only that nothing was kept, not why.

The page holds the whole day (``FilmBlock``), so dragging never comes back here; it says where the playhead stopped
(``claim``) and when a day arrow is pressed (``drive``: back, next).

What keeps it within screen history's rules:

- Words come only through ``ScreenHistory.day``, which asks ``recall.privacy`` again.
- ``ScreenHistory.on_forgotten``: a forget, an exclusion or a prune redraws the panel from what's left before the
  user's request returns (closes it when nothing is left, or when it can't be redrawn).
- None of the words reach her brain by way of the panel: it's ``self_updating`` (out of her note and not hers to
  edit), ``summary`` counts frames only, and ``look_at_panel`` refuses it. She's told the playhead's time and uses
  ``screen_history``, which marks the turn as having read outside text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from octavia.recall import privacy
from octavia.visuals.host import PanelEvent, PanelHost, VisualsError
from octavia.visuals.spec import FilmVisual

if TYPE_CHECKING:
    from octavia.recall.service import ScreenHistory
    from octavia.recall.store import Span

log = logging.getLogger(__name__)

PANEL_ID = "film"
TITLE = "Screen history"
STAY_S = 600.0  # the count waits while the pointer is on it
MAX_FRAMES = 400
MERGE_GAP_S = 120.0  # the same window back within this long is one frame
TEXT_EACH = 900  # characters of a frame's text at most
TEXT_BUDGET = 60_000  # and of a day's
MAX_WIRE_FRAMES = 135_000  # all of it, as it's sent: the panel's limit is a little over this
TITLE_CHARS = 100
REOPEN_GAP_S = 1.0  # a panel just closed is still folding away: a show of its id would meet its "closed"


def day_bounds(at: float) -> tuple[float, float]:
    """Local midnight before ``at`` and the next one, as epoch seconds."""
    start = datetime.fromtimestamp(at).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def day_name(at: float, now: float) -> str:
    moment, today = datetime.fromtimestamp(at).date(), datetime.fromtimestamp(now).date()
    if moment == today:
        return "Today"
    if moment == today - timedelta(days=1):
        return "Yesterday"
    return f"{datetime.fromtimestamp(at):%A, %b} {moment.day}"


def clock_time(at: float) -> str:
    return f"{datetime.fromtimestamp(at):%H:%M}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _share_text(lengths: list[int], budget: int) -> int:
    """The most characters each frame may have so that all of them together fit ``budget``: frames with less leave
    their share to the rest."""
    left, count = budget, len(lengths)
    for length in sorted(lengths):
        if length * count > left:
            return left // count
        left, count = left - length, count - 1
    return budget


def build_frames(rows: list[tuple[Span, str]]) -> tuple[list[dict[str, Any]], int]:
    """A day's windows (oldest first) as frames, and how many brief ones had to be left out to fit the panel. The same
    window coming back soon after is one frame."""
    merged: list[dict[str, Any]] = []
    for span, text in rows:
        last = merged[-1] if merged else None
        if (
            last is not None
            and last["process"] == span.process
            and last["full_title"] == span.title
            and span.started - last["end"] <= MERGE_GAP_S
        ):
            last["end"] = max(last["end"], span.ended)
            if text:
                last["text"] = f"{last['text']}\n{text}" if last["text"] else text
            continue
        merged.append(
            {"start": span.started, "end": span.ended, "app": span.app or span.process, "process": span.process,
             "full_title": span.title, "site": privacy.host_of(span.url), "text": text}
        )  # fmt: skip
    left_out = max(0, len(merged) - MAX_FRAMES)
    if left_out:
        by_worth = sorted(merged, key=lambda f: (bool(f["text"]), f["end"] - f["start"]))
        gone = {id(f) for f in by_worth[:left_out]}
        merged = [f for f in merged if id(f) not in gone]
    each = min(TEXT_EACH, _share_text([min(len(f["text"]), TEXT_EACH) for f in merged], TEXT_BUDGET))
    while True:
        frames = [
            {"start": f["start"], "end": f["end"], "app": _clip(f["app"], 120),
             "title": _clip(f["full_title"], TITLE_CHARS), "site": _clip(f["site"], 120), "text": _clip(f["text"], each)}
            for f in merged
        ]  # fmt: skip
        if each < 50 or len(json.dumps(frames, ensure_ascii=False)) <= MAX_WIRE_FRAMES:
            break
        each //= 2  # text full of quotes and line ends is longer on the wire than it looks
    return frames, left_out


def place_playhead(frames: list[dict[str, Any]], at: float) -> tuple[float, bool]:
    """Where the playhead goes for ``at``, and whether a window was kept at that moment."""
    if at < frames[0]["start"]:
        return frames[0]["start"], False
    if at > frames[-1]["end"]:
        return frames[-1]["end"], False
    return at, any(f["start"] <= at <= f["end"] for f in frames)


@dataclass(frozen=True)
class FilmShown:
    """What opening the filmstrip came to: ``at`` is where the playhead is, ``exact`` whether a window was kept there."""

    at: float
    day: str
    frames: int
    exact: bool
    left_out: int = 0


class Filmstrip:
    """One per screen history. ``open`` from her tool; everything else follows the panel and the history by itself."""

    def __init__(self, host: PanelHost, history: ScreenHistory, *, clock: Callable[[], float] = time.time) -> None:
        self.host = host
        self.history = history
        self._clock = clock
        self._lock = asyncio.Lock()
        self._day = 0.0  # the midnight the panel's day starts at
        self._at = 0.0
        self._seek = 0
        self._closed_at = float("-inf")
        self._tasks: set[asyncio.Task[None]] = set()
        host.self_updating.add(PANEL_ID)
        host.own_notes[PANEL_ID] = self.note
        host.subscribe(self._on_panel)
        history.on_forgotten.append(self._on_forgotten)

    @property
    def up(self) -> bool:
        return PANEL_ID in self.host.open_panels

    def note(self) -> str:
        return (
            f"[The screen history filmstrip is on screen: {day_name(self._at, self._clock())}, its playhead at "
            f"{clock_time(self._at)}, moved by the user or by screen_filmstrip. You can't see its words; to read what "
            "was on screen around then, call screen_history with since and until.]"
        )

    # ------------------------------------------------------------------ opening

    async def open(self, at: float | None = None) -> FilmShown | None:
        """Shows the day of ``at`` (None: now) with the playhead there, gliding when that day is already up. None when
        nothing was kept that day (for no time given: nothing at all). Raises ``VisualsError`` when it can't be shown."""
        async with self._lock:
            target = self._clock() if at is None else at
            rows = await self._rows(day_bounds(target)[0])
            if not rows and at is None:  # nothing yet today: the last day that has something
                earlier = await self.history.next_window(target, later=False)
                if earlier is not None:
                    target = earlier
                    rows = await self._rows(day_bounds(target)[0])
                    target = max((span.ended for span, _ in rows), default=target)
            if not rows:
                return None
            frames, left_out = build_frames(rows)
            place, exact = place_playhead(frames, target)
            day = day_bounds(target)[0]
            if self.up and day == self._day:
                self._seek += 1  # the same day: the page glides there
            self._day, self._at = day, place
            await self._show(frames, left_out, sound=True)
            return FilmShown(place, day_name(place, self._clock()), len(frames), exact, left_out)

    async def _rows(self, day: float) -> list[tuple[Span, str]]:
        since, until = day, day_bounds(day)[1]
        return await self.history.day(since, until, TEXT_EACH)

    async def _show(self, frames: list[dict[str, Any]], left_out: int, *, sound: bool) -> None:
        since, until = day_bounds(self._day)
        earlier = await self.history.next_window(since, later=False)
        later = await self.history.next_window(until, later=True)
        block = {
            "kind": "film",
            "day": day_name(self._day, self._clock()),
            "frames": frames,
            "at": self._at,
            "seek": self._seek,
            "earlier": earlier is not None,
            "later": later is not None,
            "caption": f"{left_out} brief windows are left out to fit." if left_out else "",
        }
        visual = FilmVisual.model_validate({"title": TITLE, "blocks": [block]})
        gap = REOPEN_GAP_S - (time.monotonic() - self._closed_at)
        if gap > 0 and not self.up:
            await asyncio.sleep(gap)
        self.host.claim(PANEL_ID, self._on_sent)
        self.host.drive(PANEL_ID, self._on_control)
        await self.host.show(visual, panel_id=PANEL_ID, stay_s=STAY_S, sound=sound)

    # ------------------------------------------------------------------ what the page says

    def _on_sent(self, values: dict[str, str]) -> None:
        try:
            at = float(values.get("at", ""))
        except ValueError:
            return
        since, until = day_bounds(self._day)
        if since <= at < until:
            self._at = at

    def _on_control(self, msg: dict[str, Any]) -> None:
        action = msg.get("action")
        if action in ("back", "next"):
            self._spawn(self._change_day(later=action == "next"))

    def _spawn(self, work: Any) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _change_day(self, *, later: bool) -> None:
        """Back to the end of the nearest earlier day that has something, or on to the start of the next."""
        async with self._lock:
            if not self.up:
                return
            since, until = day_bounds(self._day)
            found = await self.history.next_window(until if later else since, later=later)
            if found is None:
                return
            day = day_bounds(found)[0]
            rows = await self._rows(day)
            if not rows:
                return
            frames, left_out = build_frames(rows)
            self._day, self._at = day, frames[0]["start"] if later else frames[-1]["end"]
            try:
                await self._show(frames, left_out, sound=True)
            except VisualsError as exc:
                log.info("The filmstrip couldn't change day: %s", exc)

    # ------------------------------------------------------------------ following the history

    def _on_panel(self, event: PanelEvent) -> None:
        if event.id == PANEL_ID:
            self._closed_at = time.monotonic()

    async def _on_forgotten(self) -> None:
        """Something was deleted: the panel shows only what's left, or goes. It never stays up with what's gone."""
        async with self._lock:
            if not self.up:
                return
            try:
                rows = await self._rows(self._day)
                if not rows:
                    await self.host.close(PANEL_ID)
                    return
                frames, left_out = build_frames(rows)
                self._at, _ = place_playhead(frames, self._at)
                await self._show(frames, left_out, sound=False)
            except Exception:
                log.warning("The filmstrip couldn't follow a forget, so it's closing", exc_info=True)
                await self.host.close(PANEL_ID)
