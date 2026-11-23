"""Learning by watching, running: mines the records once a day (``routines.mine_at``, and at start when the last
mining is a day old), and hands the proactive gate an offer when the moment for a learned routine comes.

Speaking up goes only through Futures' proactive gate (``FuturesService.add_source`` with a ``RemarkSource``), so
quiet hours, focus mode and Do Not Disturb, the budgets, cooldowns and mutes all apply as to anything she says on her
own. On top of the gate, this offers:

* only a ``learned`` routine, at most ``offers_per_day`` a day, once a day per routine;
* only while the user is at the PC (input in the last ``rules.ACTIVE_S``), and only when it hasn't happened already
  today (the app came up, the thing was asked for) and the app isn't already open;
* only what the user's privacy settings allow right now (off-limits domains, excluded apps and sources), not just
  when it was learned;
* never again after a "no" (``declined``: dismissed for good, and nothing else offering the same app or lookup), and
  not after ``rules.MAX_MISSES`` offers in a row that weren't taken up (retired).

Each offer expires within ``PENDING_S``: one the gate holds back (a conversation, focus mode) is dropped rather than
said later, when these checks may no longer hold; it's proposed afresh at the next look if they still do.

An offer only asks. A yes goes to the brain as an ordinary turn, with a note saying what was offered; nothing here
opens or runs anything.

Costs: mining is counting over a few thousand rows on a background thread, read-only, a few milliseconds; no Claude
call and no GPU, so the usage budget never comes into it. The check at each Futures look (every minute) is a few
indexed queries and, only inside a routine's window, a list of the open windows.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octavia.routines import mine as mining
from octavia.routines import observe, rules
from octavia.routines.store import RoutineStore

if TYPE_CHECKING:
    from octavia.config import RoutinesConfig
    from octavia.futures.proactive import Candidate

log = logging.getLogger(__name__)

PREFIX = "routine_"  # the kinds of remark this hands the gate
KINDS = {"app_time": "routine_app", "app_after": "routine_after", "ask_time": "routine_ask"}
CATCH_UP_S = 20 * 3600  # at start, mine again when the last mining is older than this
START_DELAY_S = 90.0  # ...but not in the first moments after start, which are busy enough
PENDING_S = 150.0  # an offer held back by the gate is dropped after this (it's proposed again at each look)
PRIVACY_CACHE_S = 60.0  # the user's privacy settings are read again at most this often


class RoutineLearner:
    def __init__(
        self,
        store: RoutineStore,
        settings: RoutinesConfig,
        *,
        futures_db: Path,
        screen_db: Path | None,
        off_limits_config: Iterable[str] = (),
        exclude_apps: Iterable[str] = (),
        exclude_sources: Iterable[str] = (),
        idle_s: Callable[[], float | None] = lambda: None,
        running: Callable[[], set[str] | None] = lambda: None,
        clock: Callable[[], float] = time.time,
        local_time: Callable[[float], datetime] = datetime.fromtimestamp,
        epoch_of: Callable[[datetime], float] = datetime.timestamp,
    ) -> None:
        self.store = store
        self.settings = settings
        self.futures_db = futures_db
        self.screen_db = screen_db
        self.off_limits_config = tuple(off_limits_config)
        self.exclude_apps = tuple(exclude_apps)
        self.exclude_sources = tuple(s.lower() for s in exclude_sources)
        self.idle_s = idle_s
        self.running = running
        self.clock = clock
        self.local_time = local_time
        self.epoch_of = epoch_of  # the inverse of ``local_time``
        self._task: asyncio.Task[None] | None = None
        self._lock = threading.Lock()
        self._live: dict[str, sqlite3.Connection] = {}  # read-only: Futures' events and screen history's timeline
        self._privacy: tuple[float, frozenset[str], list[str]] | None = None  # (read at, off-limits, excluded apps)
        self._closed = False
        self._tools: dict[str, str] = {}  # remark key -> the tool its yes uses (``offer_tool``)
        self._proposed: tuple[str, float] | None = None  # the last offer handed the gate, and when

    # ------------------------------------------------------------------ running
    async def start(self) -> None:
        await asyncio.to_thread(self.store.open)
        self._task = asyncio.create_task(self._loop(), name="routines")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        with self._lock:
            self._closed = True  # the Futures worker may still ask for offers: it gets none, and opens nothing
            for conn in self._live.values():
                conn.close()
            self._live.clear()

    async def _loop(self) -> None:
        await asyncio.sleep(START_DELAY_S)
        last = await asyncio.to_thread(self.store.meta, "last_mined", 0.0)
        due = self.clock() - float(last or 0.0) >= CATCH_UP_S  # at start, only when a day has passed
        while True:
            if due:
                try:
                    await asyncio.to_thread(self.mine_now)
                except Exception:
                    log.warning("Couldn't look for routines", exc_info=True)
            await asyncio.sleep(max(60.0, self._next_mining(self.clock()) - self.clock()))
            due = True  # at the daily time, always

    def _next_mining(self, now: float) -> float:
        hour, minute = (int(x) for x in self.settings.mine_at.split(":"))
        local = self.local_time(now)
        at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
        return self.epoch_of(at)

    def mine_now(self) -> tuple[int, int]:
        """Look for routines in the records now (blocking: call off the event loop). Returns (learned, watching)."""
        now = self.clock()
        with _background_priority():
            obs = observe.read(
                self.futures_db,
                self.screen_db,
                since=now - self.settings.history_days * 86400,
                off_limits_config=self.off_limits_config,
                exclude_apps=self.exclude_apps,
                exclude_sources=self.exclude_sources,
            )
            found = mining.mine(
                obs, local_time=self.local_time, min_days=self.settings.min_days, min_support=self.settings.min_support
            )
        counts = self.store.replace_mined(found)
        self.store.set_meta("last_mined", now)
        log.info("Routines: %d learned, %d watching (%d app records)", *counts, len(obs.apps))
        return counts

    # ------------------------------------------------------------------ the user's switches
    @property
    def offers_on(self) -> bool:
        return bool(self.settings.offers) and bool(self.store.meta("offers", True))

    def set_offers(self, on: bool) -> None:
        self.store.set_meta("offers", bool(on))

    def forget(self, number: int) -> str | None:
        """Never offer routine ``number`` again. Its description, or None when there's no such routine."""
        row = self.store.by_id(number)
        if row is None:
            return None
        self.store.dismiss(row["key"], "you said to forget it")
        return self.store.as_routine(row).describe()

    # ------------------------------------------------------------------ the gate's source (Futures worker thread)
    def candidates(self, now: float) -> list[Candidate]:
        """An offer for a learned routine whose moment it is, if any (at most one at a time). Also settles how the
        earlier offers went."""
        if self._closed:
            return []
        try:
            self._settle(now)
            if not self.offers_on or not self._at_pc():
                return []
            day_start = self._day_start(now)
            if self.store.offers_since(day_start) >= self.settings.offers_per_day:
                return []
            turned_down = self.store.turned_down_targets()
            for row in self.store.all(("learned",)):
                if row["last_offered"] is not None and row["last_offered"] >= day_start:
                    continue
                routine = self.store.as_routine(row)
                if (routine.tool or routine.process) in turned_down or not self._allowed_now(routine):
                    continue
                candidate = self._moment(routine, now, day_start)
                if candidate is None:
                    continue
                last = self._proposed
                if last is not None and last[0] != candidate.key and now - last[1] < PENDING_S:
                    return []  # another one may still be waiting at the gate: one at a time, within the day's count
                self._proposed = (candidate.key, now)
                return [candidate]
        except Exception:
            log.warning("Couldn't check for a routine to offer", exc_info=True)
        return []

    def shared(self, key: str) -> None:
        """The gate said the offer: count it, and see within ``rules.OUTCOME_S`` whether it was taken up."""
        self.store.offered(_routine_key(key), self.clock())

    def declined(self, key: str) -> None:
        """The user said no to it: never offered again."""
        self.store.dismiss(_routine_key(key), "you said no to the offer")

    def offer_tool(self, key: str) -> str | None:
        """The (read-only) tool a yes to this offer uses, so the yes goes to the brain with its tools."""
        with self._lock:
            return self._tools.get(key)

    def _moment(self, routine: mining.Routine, now: float, day_start: float) -> Candidate | None:
        if routine.kind == "app_after":
            return self._after_moment(routine, now)
        local = self.local_time(now)
        if routine.day_type != "any" and (routine.day_type == "weekend") != ((local - _shift()).weekday() >= 5):
            return None
        minute = mining.day_of(now, self.local_time)[1]
        if not routine.minute - rules.OFFER_BEFORE_MIN <= minute <= routine.minute + rules.OFFER_AFTER_MIN:
            return None
        if routine.kind == "app_time":
            if self._app_seen_since(routine.process, day_start) or self._open(routine.process):
                return None
        elif self._tool_used_since(routine.tool, day_start):
            return None
        window_end = now + (routine.minute + rules.OFFER_AFTER_MIN - minute) * 60
        return self._candidate(routine, now, min(window_end, now + PENDING_S))

    def _after_moment(self, routine: mining.Routine, now: float) -> Candidate | None:
        recent = self._recent_apps(now - 12 * 3600)
        sitting = mining.sittings(recent)[-1] if recent else []
        if not sitting or now - sitting[-1].ts >= rules.SESSION_GAP_S:
            return None
        first = next((s for s in sitting if s.process == routine.after_process), None)
        if first is None or now - first.ts > rules.TRIGGER_S:
            return None
        if any(s.process == routine.process for s in sitting) or self._open(routine.process):
            return None
        return self._candidate(routine, now, min(first.ts + rules.TRIGGER_S + 60, now + PENDING_S))

    def _candidate(self, routine: mining.Routine, now: float, expires: float) -> Candidate:
        from octavia.futures.explain import Trace
        from octavia.futures.proactive import Candidate
        from octavia.futures.schema import Policy

        if routine.kind == "app_time":
            text, tool = rules.app_time_offer(routine.app), rules.APP_TOOL
            do = f"open {routine.app} with {tool}"
        elif routine.kind == "app_after":
            text, tool = rules.app_after_offer(routine.app, routine.after_app), rules.APP_TOOL
            do = f"open {routine.app} with {tool}"
        else:
            ask = rules.ASK_TOOLS[routine.tool]
            text, tool = rules.ask_time_offer(routine.tool), routine.tool
            do = f"get {ask.what} with {tool} and give them what matters"
        key = f"{KINDS[routine.kind]}:{routine.key}@{mining.day_of(now, self.local_time)[0]}"
        with self._lock:
            self._tools = {k: v for k, v in self._tools.items() if k.split("@")[-1] >= key.split("@")[-1]}
            self._tools[key] = tool
        evidence = routine.describe()
        return Candidate(
            kind=KINDS[routine.kind],
            key=key,
            text=text,
            policy=Policy.ASK,
            urgency=0.6,
            relevance=min(1.0, 0.6 + 0.4 * routine.support),
            domain=rules.DOMAIN,
            sensitivity="personal",
            subject=None,
            note=(
                f"(Routines) You just offered, unprompted, because the user usually {evidence}. On a yes, {do}. "
                "On a no, just say okay: you won't offer this one again. On a not now, just say okay."
            ),
            trace=Trace(
                observations=({"event": "", "what": f"the user usually {evidence}", "at": now},),
                assumptions=("a habit from the last weeks of records, not a plan of theirs",),
                confidence="moderate" if routine.support < 0.75 else "strong",
            ).to_dict(),
            expires_at=expires,
            listen=True,
        )

    def _settle(self, now: float) -> None:
        for row in self.store.pending_outcomes(now - rules.OUTCOME_S):
            since = float(row["last_offered"])
            if row["kind"] == "ask_time":
                taken = self._tool_used_since(row["tool"], since, until=since + rules.OUTCOME_S)
            else:
                taken = self._app_seen_since(row["process"], since, until=since + rules.OUTCOME_S)
            if self.store.outcome(row["key"], taken, rules.MAX_MISSES) == "retired":
                log.info("Routines: stopped offering %s (not taken up)", row["key"])

    # ------------------------------------------------------------------ what's going on now
    def _at_pc(self) -> bool:
        try:
            idle = self.idle_s()
        except Exception:
            idle = None
        return idle is not None and idle < rules.ACTIVE_S

    def _open(self, process: str) -> bool:
        """Whether the app has a window open (unknown counts as open: no offer to open what may be there)."""
        try:
            running = self.running()
        except Exception:
            running = None
        return running is None or process in running

    def _day_start(self, now: float) -> float:
        """Epoch seconds of the start of the user's day (``rules.DAY_START_H`` local) by the wall clock, so a
        daylight-saving change in the night doesn't move it."""
        local = self.local_time(now)
        start = local.replace(hour=rules.DAY_START_H, minute=0, second=0, microsecond=0)
        if local < start:
            start -= timedelta(days=1)
        return self.epoch_of(start)

    def _allowed_now(self, routine: mining.Routine) -> bool:
        """What the user's privacy settings say now: being learned under older settings isn't enough."""
        banned, excluded = self._privacy_now()
        if rules.DOMAIN in banned:
            return False
        if routine.tool:
            if {"assistant", "tools"} & set(self.exclude_sources):
                return False
            return rules.ASK_TOOLS[routine.tool].domain not in banned
        if rules.APP_DOMAIN in banned or "computer" in self.exclude_sources:
            return False
        apps = [(routine.process, routine.app), (routine.after_process, routine.after_app)]
        return all(rules.may_learn_app(p, a, excluded) for p, a in apps if p)

    def _privacy_now(self) -> tuple[frozenset[str], list[str]]:
        now = time.monotonic()
        with self._lock:
            cached = self._privacy
            if cached is not None and now - cached[0] < PRIVACY_CACHE_S:
                return cached[1], cached[2]
            banned = observe.off_limits(self._conn("futures"), self.off_limits_config)
            excluded = [*self.exclude_apps, *observe.screen_exclusions(self._conn("screen"))]
            self._privacy = (now, banned, excluded)
            return banned, excluded

    def _conn(self, which: str) -> sqlite3.Connection | None:
        """A read-only connection to Futures' events ("futures") or screen history ("screen"); call holding the
        lock. None when that database isn't there, or this is closed."""
        conn = self._live.get(which)
        if conn is None and not self._closed:
            path = self.futures_db if which == "futures" else self.screen_db
            if path is None or not path.exists():
                return None
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout = 1000")
            self._live[which] = conn
        return conn

    def _query(self, which: str, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        with self._lock:
            conn = self._conn(which)
            if conn is None:
                return []
            try:
                return conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                log.debug("Routines couldn't read %s", which, exc_info=True)
                return []

    def _app_seen_since(self, process: str, since: float, until: float | None = None) -> bool:
        """The app came to the front (by Futures' watch, or in screen history's timeline) in ``[since, until)``."""
        end = until if until is not None else float("inf")
        found = self._query(
            "futures",
            "SELECT 1 FROM events WHERE type='computer.context_changed' AND ts>=? AND ts<? "
            "AND lower(json_extract(data, '$.process'))=? LIMIT 1",
            (since, end, process),
        )
        return bool(found) or bool(
            self._query(
                "screen",
                "SELECT 1 FROM activity WHERE started>=? AND started<? AND lower(process)=? LIMIT 1",
                (since, end, process),
            )
        )

    def _tool_used_since(self, tool: str, since: float, until: float | None = None) -> bool:
        """She used the tool in a turn the user started (not a scheduled one) in ``[since, until)``."""
        rows = self._query(
            "futures",
            "SELECT 1 FROM events WHERE type='computer.action_performed' AND ts>=? AND ts<? AND action=? "
            "AND json_extract(context, '$.turn') IN (SELECT json_extract(context, '$.turn') FROM events "
            "WHERE type='user.request' AND ts>=?) LIMIT 1",
            (since, until if until is not None else float("inf"), tool, since - 3600),
        )
        return bool(rows)

    def _recent_apps(self, since: float) -> list[observe.Seen]:
        rows = self._query(
            "futures",
            "SELECT ts, json_extract(data, '$.app'), json_extract(data, '$.process') FROM events "
            "WHERE type='computer.context_changed' AND ts>=?",
            (since,),
        )
        rows += self._query("screen", "SELECT started, app, process FROM activity WHERE started>=?", (since,))
        seen = [observe.Seen(float(ts), str(app or ""), rules.norm_process(str(p or ""))) for ts, app, p in rows]
        return sorted(seen, key=lambda s: s.ts)


def _routine_key(remark_key: str) -> str:
    """``routine_app:app_time:spotify.exe@2026-10-09`` -> ``app_time:spotify.exe``."""
    return remark_key.split(":", 1)[-1].rsplit("@", 1)[0]


def _shift() -> timedelta:
    return timedelta(hours=rules.DAY_START_H)


def running_processes() -> set[str] | None:
    """The programs with a window open (lower case), or None when that can't be told (not Windows)."""
    try:
        from octavia.tools.builtin.app_tools import _running_apps

        return {rules.norm_process(group.process) for group in _running_apps()}
    except Exception:
        log.debug("Couldn't list the open windows", exc_info=True)
        return None


@contextlib.contextmanager
def _background_priority() -> Iterator[None]:
    """This thread at background priority (lower CPU, disk and memory priority) while it mines, then back: it's a
    pool thread other work runs on later. Windows only."""
    kernel32 = None
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _BACKGROUND_BEGIN)
        except Exception:
            kernel32 = None
    try:
        yield
    finally:
        if kernel32 is not None:
            with contextlib.suppress(Exception):
                kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _BACKGROUND_END)


_BACKGROUND_BEGIN = 0x00010000  # THREAD_MODE_BACKGROUND_BEGIN
_BACKGROUND_END = 0x00020000
