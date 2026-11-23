"""Finding routines in the observations (``observe.Observations``): plain counting, no model, the same answer every
time for the same records.

Three kinds:

* ``app_time``: an app first comes to the front around the same time of day ("Spotify around 9 PM on weekdays"):
  the first time it came up each day, the hour-long window (``rules.WINDOW_MIN``) holding the most of those days.
* ``app_after``: one app follows another in a sitting ("Discord after Roblox"): B first comes up within
  ``rules.FOLLOW_S`` of A's first appearance in a sitting, and does so clearly more often than B turns up in a
  sitting at all (``rules.AFTER_LIFT``), so an app that's in every sitting (a browser) is never "after" anything.
* ``ask_time``: she's asked for the same read-only thing around the same time of day (the weather in the morning).

Each routine says how sure it is: on how many days it happened, out of how many it could have (``days``/``out_of``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from octavia.routines import rules
from octavia.routines.observe import Observations, Seen

DAY_TYPES = ("any", "weekday", "weekend")
SPECIFIC_BY = 0.15  # a weekday-only (or weekend-only) routine must fit that many more of its days than "any"


@dataclass(frozen=True)
class Routine:
    key: str  # "app_time:spotify.exe", "app_after:roblox.exe>discord.exe", "ask_time:get_weather"
    kind: str  # app_time | app_after | ask_time
    status: str  # learned (may be offered) | watching (only listed)
    app: str = ""  # the app offered (display name)
    process: str = ""
    after_app: str = ""  # app_after: the app it follows
    after_process: str = ""
    tool: str = ""  # ask_time
    day_type: str = "any"
    minute: int = 0  # usual time, minutes since the day's start (rules.DAY_START_H)
    days: int = 0  # days (app_after: sittings) it happened
    out_of: int = 0  # days (sittings) it could have
    support: float = 0.0

    def describe(self) -> str:
        """One line about it, as it's said: what, when, and the evidence."""
        days = {"any": "days", "weekday": "weekdays", "weekend": "weekend days"}[self.day_type]
        if self.kind == "app_after":
            return (
                f"opens {self.app} after {self.after_app} ({self.days} of {self.out_of} times {self.after_app} "
                "came up first in a sitting)"
            )
        what = f"opens {self.app}" if self.kind == "app_time" else f"asks for {rules.ASK_TOOLS[self.tool].what}"
        return f"{what} around {rules.clock(self.minute)} ({self.days} of the last {self.out_of} {days} with records)"


def day_of(ts: float, local_time: Callable[[float], datetime]) -> tuple[str, int, bool]:
    """(the day as "2026-10-09", minutes since its start, weekend?), days running 04:00 to 04:00."""
    shifted = local_time(ts) - timedelta(hours=rules.DAY_START_H)
    return shifted.date().isoformat(), shifted.hour * 60 + shifted.minute, shifted.weekday() >= 5


def mine(
    obs: Observations,
    *,
    local_time: Callable[[float], datetime] = datetime.fromtimestamp,
    min_days: int = 4,
    min_support: float = 0.5,
) -> list[Routine]:
    """Every routine the records show, learned or only watched, strongest first. None at all before the records span
    ``rules.MIN_HISTORY_DAYS``."""
    first, last = obs.first, obs.last
    if first is None or last is None or last - first < rules.MIN_HISTORY_DAYS * 86400:
        return []
    active: dict[str, bool] = {}  # day -> weekend?
    for stamp in [s.ts for s in obs.apps] + [u.ts for u in obs.asks]:
        day, _, weekend = day_of(stamp, local_time)
        active[day] = weekend
    names = _display_names(obs.apps)
    found: list[Routine] = []
    firsts = _first_per_day(((s.ts, s.process) for s in obs.apps), local_time)
    for process, per_day in firsts.items():
        best = _best_window(per_day, active, min_days, min_support)
        if best is not None:
            found.append(
                Routine(key=f"app_time:{process}", kind="app_time", app=names[process], process=process, **best)
            )
    asks = _first_per_day(((u.ts, u.tool) for u in obs.asks), local_time)
    for tool, per_day in asks.items():
        best = _best_window(per_day, active, min_days, min_support)
        if best is not None:
            found.append(Routine(key=f"ask_time:{tool}", kind="ask_time", tool=tool, **best))
    found += _after(obs.apps, names, local_time, min_days, min_support)
    order = {"learned": 0, "watching": 1}
    return sorted(found, key=lambda r: (order[r.status], -r.support, -r.days, r.key))


def _display_names(apps: Iterable[Seen]) -> dict[str, str]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for seen in apps:
        counts[seen.process][seen.app] += 1
    return {process: names.most_common(1)[0][0] for process, names in counts.items()}


def _first_per_day(
    items: Iterable[tuple[float, str]], local_time: Callable[[float], datetime]
) -> dict[str, dict[str, int]]:
    """For each app (or tool): day -> minute it first came up that day."""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for ts, what in sorted(items):
        day, minute, _ = day_of(ts, local_time)
        out[what].setdefault(day, minute)
    return out


def _best_window(
    per_day: dict[str, int], active: dict[str, bool], min_days: int, min_support: float
) -> dict[str, object] | None:
    """The day type and hour-long window that fit the most days, with its evidence; None when it's not even worth
    watching."""
    results = {}
    for day_type in DAY_TYPES:
        days = [d for d, weekend in active.items() if _fits(day_type, weekend)]
        minutes = sorted(m for d, m in per_day.items() if d in active and _fits(day_type, active[d]))
        if not days or not minutes:
            continue
        count, inside = _densest(minutes)
        results[day_type] = (count, len(days), count / len(days), inside)
    if not results:
        return None
    pick = "any" if "any" in results else next(iter(results))
    for day_type in ("weekday", "weekend"):
        if day_type in results and results[day_type][2] >= results[pick][2] + SPECIFIC_BY:
            pick = day_type if results[day_type][0] >= rules.WATCH_MIN_DAYS else pick
    count, out_of, support, inside = results[pick]
    if count >= min_days and support >= min_support:
        status = "learned"
    elif count >= rules.WATCH_MIN_DAYS and support >= rules.WATCH_MIN_SUPPORT:
        status = "watching"
    else:
        return None
    return {
        "status": status,
        "day_type": pick,
        "minute": inside[len(inside) // 2],
        "days": count,
        "out_of": out_of,
        "support": round(support, 3),
    }


def _fits(day_type: str, weekend: bool) -> bool:
    return day_type == "any" or (day_type == "weekend") == weekend


def _densest(minutes: list[int]) -> tuple[int, list[int]]:
    """The most minutes within one ``rules.WINDOW_MIN`` window (sorted input), and those minutes."""
    best: list[int] = []
    start = 0
    for end in range(len(minutes)):
        while minutes[end] - minutes[start] > rules.WINDOW_MIN:
            start += 1
        if end - start + 1 > len(best):
            best = minutes[start : end + 1]
    return len(best), best


def sittings(apps: list[Seen]) -> list[list[Seen]]:
    """The records split wherever nothing came to the front for ``rules.SESSION_GAP_S``."""
    out: list[list[Seen]] = []
    for seen in sorted(apps, key=lambda s: s.ts):
        if not out or seen.ts - out[-1][-1].ts >= rules.SESSION_GAP_S:
            out.append([])
        out[-1].append(seen)
    return out


def _after(
    apps: list[Seen],
    names: dict[str, str],
    local_time: Callable[[float], datetime],
    min_days: int,
    min_support: float,
) -> list[Routine]:
    firsts = []
    for sitting in sittings(apps):
        first: dict[str, float] = {}
        for seen in sitting:
            first.setdefault(seen.process, seen.ts)
        firsts.append(first)
    total = len(firsts)
    if not total:
        return []
    present = Counter(p for first in firsts for p in first)
    hits: Counter[tuple[str, str]] = Counter()
    eligible: Counter[tuple[str, str]] = Counter()
    hit_days: dict[tuple[str, str], set[str]] = defaultdict(set)
    for first in firsts:
        for a, ta in first.items():
            for b in present:
                if b == a:
                    continue
                tb = first.get(b)
                if tb is not None and tb < ta:
                    continue  # B was already up: it can't follow A in this sitting
                eligible[(a, b)] += 1
                if tb is not None and tb - ta <= rules.FOLLOW_S:
                    hits[(a, b)] += 1
                    hit_days[(a, b)].add(day_of(ta, local_time)[0])
    out = []
    for (a, b), n in hits.items():
        support = n / eligible[(a, b)]
        base = present[b] / total
        days = len(hit_days[(a, b)])
        if support < base + rules.AFTER_LIFT:
            continue
        if days >= min_days and support >= max(min_support, 0.6):
            status = "learned"
        elif days >= rules.WATCH_MIN_DAYS and support >= 0.4:
            status = "watching"
        else:
            continue
        out.append(
            Routine(
                key=f"app_after:{a}>{b}",
                kind="app_after",
                status=status,
                app=names[b],
                process=b,
                after_app=names[a],
                after_process=a,
                days=n,
                out_of=eligible[(a, b)],
                support=round(support, 3),
            )
        )
    return out
