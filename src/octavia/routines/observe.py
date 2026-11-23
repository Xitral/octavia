"""What learning by watching reads, all of it already recorded by other parts of her, through read-only connections:

* Futures' event log (``data/futures/futures.db``): an app coming to the front (``computer.context_changed``, program
  and display names only), and the read-only tools (``rules.ASK_TOOLS``) she used in a turn the user started
  (``computer.action_performed`` joined to ``user.request`` by turn). Events in a domain the user put off-limits, or
  marked sensitive, are skipped.
* Screen history's timeline (``data/recall/screen_history.db``, the ``activity`` table): which app was in front when.
  Never its text.

Nothing here writes, and nothing is captured that wasn't already. Apps ``rules.may_learn_app`` rules out (and the
user's own screen-history exclusions) are dropped as they're read.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from octavia.routines import rules

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Seen:
    """An app came to the front (``ts`` epoch seconds)."""

    ts: float
    app: str
    process: str  # lower case, "spotify.exe"


@dataclass(frozen=True)
class Used:
    """She used a read-only tool in a turn the user started."""

    ts: float
    tool: str


@dataclass
class Observations:
    apps: list[Seen] = field(default_factory=list)
    asks: list[Used] = field(default_factory=list)

    @property
    def first(self) -> float | None:
        stamps = [s.ts for s in self.apps] + [u.ts for u in self.asks]
        return min(stamps) if stamps else None

    @property
    def last(self) -> float | None:
        stamps = [s.ts for s in self.apps] + [u.ts for u in self.asks]
        return max(stamps) if stamps else None


@contextmanager
def read_only(path: Path) -> Iterator[sqlite3.Connection | None]:
    """A read-only connection to ``path`` (None when there's no such database yet)."""
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 2000")  # the assistant may be writing
        yield conn
    finally:
        conn.close()


def off_limits(conn: sqlite3.Connection | None, configured: Iterable[str] = ()) -> frozenset[str]:
    """The domains the user put off-limits: the config's list and what they told Futures."""
    found = {d.lower() for d in configured}
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM controls WHERE key='off_limits'").fetchone()
            if row and row[0]:
                found |= {str(d).lower() for d in json.loads(row[0]) or []}
        except (sqlite3.Error, ValueError, TypeError):
            log.debug("Couldn't read Futures' off-limits domains", exc_info=True)
    return frozenset(found)


def screen_exclusions(conn: sqlite3.Connection | None, configured: Iterable[str] = ()) -> list[str]:
    """The apps the user keeps out of screen history: the config's and what they said ("never keep Signal")."""
    found = [str(a) for a in configured]
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='exclude_apps'").fetchone()
            if row and row[0]:
                found += [str(a) for a in json.loads(row[0]) or []]
        except (sqlite3.Error, ValueError, TypeError):
            log.debug("Couldn't read screen history's excluded apps", exc_info=True)
    return found


def read(
    futures_db: Path,
    screen_db: Path | None,
    *,
    since: float,
    off_limits_config: Iterable[str] = (),
    exclude_apps: Iterable[str] = (),
    exclude_sources: Iterable[str] = (),
) -> Observations:
    """Everything since ``since`` the miner looks at. ``exclude_sources``: Futures' sources the user keeps out
    (``futures.exclude_sources``): what they recorded before isn't learned from either."""
    obs = Observations()
    with read_only(futures_db) as fut, read_only(screen_db) if screen_db else _none() as scr:
        banned = off_limits(fut, off_limits_config)
        if rules.DOMAIN in banned:
            return obs  # the user put their routines off-limits: nothing is learned at all
        excluded = [*exclude_apps, *screen_exclusions(scr)]
        if fut is not None:
            _read_futures(fut, since, banned, excluded, {s.lower() for s in exclude_sources}, obs)
        if scr is not None and rules.APP_DOMAIN not in banned:
            _read_screen(scr, since, excluded, obs)
    obs.apps.sort(key=lambda s: s.ts)
    obs.asks.sort(key=lambda u: u.ts)
    return obs


@contextmanager
def _none() -> Iterator[None]:
    yield None


def _allowed(domain: str | None, sensitivity: str | None, banned: frozenset[str]) -> bool:
    return (domain or "").lower() not in banned and (sensitivity or "") != "sensitive"


def _read_futures(
    conn: sqlite3.Connection,
    since: float,
    banned: frozenset[str],
    excluded: list[str],
    sources: set[str],
    obs: Observations,
) -> None:
    try:
        rows = (
            []
            if "computer" in sources
            else conn.execute(
                "SELECT ts, data, domain, sensitivity FROM events WHERE type='computer.context_changed' AND ts>=?",
                (since,),
            ).fetchall()
        )
        for ts, data, domain, sensitivity in rows:
            if not _allowed(domain, sensitivity, banned):
                continue
            info = _json(data)
            app, process = str(info.get("app") or ""), rules.norm_process(str(info.get("process") or ""))
            if rules.may_learn_app(process, app, excluded):
                obs.apps.append(Seen(float(ts), app, process))
        turns = {
            str(_json(ctx).get("turn") or "")
            for ctx, domain, sensitivity in conn.execute(
                "SELECT context, domain, sensitivity FROM events WHERE type='user.request' AND ts>=?", (since,)
            )
            if _allowed(domain, sensitivity, banned)
        }
        turns.discard("")
        if sources & {"assistant", "tools"}:
            return
        marks = ",".join("?" * len(rules.ASK_TOOLS))
        for ts, action, ctx, domain, sensitivity in conn.execute(
            "SELECT ts, action, context, domain, sensitivity FROM events WHERE type='computer.action_performed' "
            f"AND ts>=? AND action IN ({marks})",
            (since, *rules.ASK_TOOLS),
        ):
            tool = str(action or "")
            if (
                _allowed(domain, sensitivity, banned)
                and rules.ASK_TOOLS[tool].domain not in banned
                and str(_json(ctx).get("turn") or "") in turns
            ):
                obs.asks.append(Used(float(ts), tool))
    except sqlite3.Error:
        log.warning("Couldn't read Futures' events for routines", exc_info=True)


def _read_screen(conn: sqlite3.Connection, since: float, excluded: list[str], obs: Observations) -> None:
    try:
        with closing(conn.execute("SELECT started, app, process FROM activity WHERE started>=?", (since,))) as cur:
            for started, app, process in cur:
                process = rules.norm_process(str(process or ""))
                if rules.may_learn_app(process, str(app or ""), excluded):
                    obs.apps.append(Seen(float(started), str(app), process))
    except sqlite3.Error:
        log.warning("Couldn't read screen history's timeline for routines", exc_info=True)


def _json(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
