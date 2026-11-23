"""Her own mood across the day: how the day has gone for her, in a word and a reason, carried from one conversation
to the next.

Ordinary code, no model and no Claude usage: it counts what's already recorded since the day started
(``DAY_STARTS_AT`` local time). Her builds that went in or failed (the evolve journal), how the user has sounded (the
voice tone in the routing log, counted, never quoted), and how much the two of them have talked. The line goes into
her system prompt when a conversation starts and with her quick voice's small talk, and only colors her tone: she
says it only when asked how she is, and never names how the user sounded (they asked her not to describe their tone).

Under the day's mood there's a slower one: how most of her last nights' reflections left her feeling
(``InnerLife.lately``), so a run of good or rough days shows over a week, not just today.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DAY_STARTS_AT = 4  # local hour: a night owl's 2 AM still belongs to the day before
_TAIL_BYTES = 512 * 1024  # how much of the end of the routing log is read
# The voice tone's words (``emotion/tone.py``) for a good and for a hard time.
UP = frozenset({"cheerful", "happy", "excited", "surprised"})
DOWN = frozenset({"stressed", "down", "sad", "tired", "frustrated", "annoyed", "anxious", "angry"})
# A hard day for the user: the tone reads one of DOWN on 10-20% of an ordinary day's turns (Oct 7-9, 2026), so it
# takes clearly more than that.
HARD_DAY_MIN = 5
HARD_DAY_SHARE = 0.25
# Every mood ``mood_of`` can give, in the words it gives them (preset replies key their lines by these).
MOODS = ("protective", "a little salty", "buzzing", "upbeat", "warmed up")

NOTE = (
    "Your own mood right now, from how today has gone (a feeling, not news to announce): {mood}, {why}. Let it "
    "color your tone a little; say it only if they ask how you are, and drop it the moment they need you sharp."
)
LATELY_NOTE = (
    "Your undertone lately, from how most of your last nights' reflections left you (slower than today's mood, and "
    "just as private): {word}{why}."
)

Lately = Callable[[], "tuple[str, str] | None"]


def lately_line(lately: Lately | None) -> str:
    """The line for her undertone, or "" when there's none (or it can't be read). Never raises."""
    if lately is None:
        return ""
    try:
        found = lately()
    except Exception:
        log.warning("Couldn't read her undertone", exc_info=True)
        return ""
    if not found:
        return ""
    word, why = found
    return LATELY_NOTE.format(word=word, why=f", {why}" if why else "")


@dataclass(frozen=True)
class Day:
    """What today held, as counts."""

    installed: int = 0  # her updates that went in
    failed: int = 0  # her builds that failed
    up: int = 0  # turns where the user sounded upbeat
    down: int = 0  # ...and where they sounded stressed, tired or low
    turns: int = 0  # turns the user spoke or typed


def day_start(now: float) -> float:
    local = datetime.fromtimestamp(now)
    start = local.replace(hour=DAY_STARTS_AT, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return start.timestamp()


def mood_of(day: Day) -> tuple[str, str] | None:
    """Her mood and why, or None on a day with nothing to feel about yet. The user's own hard day comes first: then
    she's on their side, not salty about her builds."""
    if day.down >= HARD_DAY_MIN and day.down >= HARD_DAY_SHARE * day.turns and day.down > 2 * day.up:
        return "protective", "today's been a hard one for them, so go easy and be warm"
    if day.failed >= 2 and day.installed == 0:
        return "a little salty", f"{day.failed} of your own builds failed today and none went in"
    if day.installed >= 2 or (day.installed and day.up >= 2):
        return "buzzing", f"{_count(day.installed, 'update')} of yours went in today"
    if day.up >= 3 and day.up > day.down:
        return "upbeat", "today's been a good one for the two of you"
    if day.turns >= 40:
        return "warmed up", "you two have been at it most of the day"
    return None


def _count(n: int, word: str) -> str:
    return f"one {word}" if n == 1 else f"{n} {word}s"


def builds_since(journal: Path, since: float) -> tuple[int, int]:
    """(installed, failed) since ``since``, from the evolve journal; (0, 0) when there's none or it can't be read."""
    if not journal.is_file():
        return 0, 0
    try:
        conn = sqlite3.connect(f"file:{journal.as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            installed = conn.execute(
                "SELECT COUNT(*) FROM improvements WHERE status='installed' AND updated_at>=?", (since,)
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM builds WHERE outcome='failed' AND finished_at>=?", (since,)
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        log.debug("Couldn't read the evolve journal for her mood", exc_info=True)
        return 0, 0
    return int(installed), int(failed)


def turns_since(routing_log: Path, since: float) -> tuple[int, int, int]:
    """(up, down, turns) since ``since``, from the end of the routing log; zeros when it can't be read."""
    if not routing_log.is_file():
        return 0, 0, 0
    try:
        with routing_log.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            raw = f.read()
    except OSError:
        return 0, 0, 0
    up = down = turns = 0
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # the cut first line, or a broken one
        if not isinstance(row, dict) or float(row.get("ts") or 0) < since:
            continue
        turns += 1
        tone = row.get("tone")
        name = tone.get("tone") if isinstance(tone, dict) else None
        if name in UP:
            up += 1
        elif name in DOWN:
            down += 1
    return up, down, turns


def today(journal: Path, routing_log: Path, now: float | None = None) -> Day:
    since = day_start(time.time() if now is None else now)
    installed, failed = builds_since(journal, since)
    up, down, turns = turns_since(routing_log, since)
    return Day(installed=installed, failed=failed, up=up, down=down, turns=turns)


class Mood:
    """Her mood line for the prompts, worked out at most every ``ttl_s``: small talk asks for it with every message,
    and preset replies ask for her mood word (``word``) with every "thanks"."""

    def __init__(
        self, journal: Path, routing_log: Path, ttl_s: float = 120.0, clock=time.time, lately: Lately | None = None
    ) -> None:
        self.journal = journal
        self.routing_log = routing_log
        self.ttl_s = ttl_s
        self._clock = clock
        self.lately = lately  # her undertone over the last nights (``InnerLife.lately``), None without a journal
        self._at: float | None = None
        self._found: tuple[str, str] | None = None
        self._undertone = ""

    def _current(self) -> tuple[str, str] | None:
        now = self._clock()
        if self._at is None or now - self._at >= self.ttl_s:
            self._found = _mood_now(self.journal, self.routing_log, now)
            self._undertone = lately_line(self.lately)
            self._at = now
        return self._found

    def __call__(self) -> str:
        found = self._current()
        day = NOTE.format(mood=found[0], why=found[1]) if found else ""
        return " ".join(part for part in (day, self._undertone) if part)

    def word(self) -> str | None:
        """Her mood in a word ("buzzing"), or None when today gives her no particular mood."""
        found = self._current()
        return found[0] if found else None


def _mood_now(journal: Path, routing_log: Path, now: float | None) -> tuple[str, str] | None:
    try:
        return mood_of(today(journal, routing_log, now))
    except Exception:
        log.warning("Couldn't work out her mood", exc_info=True)
        return None


def mood_note(journal: Path, routing_log: Path, now: float | None = None) -> str:
    """The line for her prompt, or "" when today gives her no particular mood. Never raises."""
    found = _mood_now(journal, routing_log, now)
    return NOTE.format(mood=found[0], why=found[1]) if found else ""
