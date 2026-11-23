"""Her Claude usage: how full your subscription's limits are, and a budget for the work she does on her own.

Every Claude Code turn reports the account's limits (the SDK's ``RateLimitEvent``): the 5-hour window and the
weekly one, how full each is (0..1) and when it resets, plus whether a limit is near (``allowed_warning``) or hit
(``rejected``). They are your limits, shared with your own Claude Code sessions, so they are the real signal; nothing
here estimates usage from tokens or prices. ``observe`` appends each report to ``observations.jsonl`` in the usage
folder, from any process that knows the folder (the app, and the eval runs' subprocess through ``DIR_ENV``).

Her background work asks ``Budget.check`` before each job (a night-shift or self-started build, the real-request
check, the nightly reflection) and is skipped or put off when:

* a limit is hit or near (the last report said ``rejected`` or ``allowed_warning``, until that window resets),
* your 5-hour window is at least ``max_five_hour`` full, or your weekly limit at least ``max_weekly``, or
* today's background jobs already used ``daily_share`` of your weekly limit.

Claude Code sends a report when a session starts and whenever the numbers change, so between Claude calls of hers
the last one can be hours old while your own Claude Code sessions fill the window. ``check_fresh`` first takes a
fresh reading when the last one is older than ``FRESH_S``: one tiny Haiku call with no tools and no MCP servers (a
few hundred tokens, about two seconds).

A job's use is how much the weekly window grew over its run, from the report just before it (when recent) to the last
one in it. Whatever else ran at the same time (your own Claude Code, a conversation with her) is counted too, which
errs on the side of leaving your limit alone. What she skipped goes to ``skips.jsonl`` and the morning report.

The conversation itself is never checked: only ``observe`` runs on that path, and it never raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DIR_ENV = "OCTAVIA_USAGE_DIR"  # where a subprocess (the eval runs) appends its reports
OBSERVATIONS = "observations.jsonl"
JOBS = "jobs.jsonl"
SKIPS = "skips.jsonl"
RECENT_S = 10 * 60  # a report this fresh before a job counts as where the job started
FRESH_S = 10 * 60  # an older one is refreshed (``probe``) before a background job is allowed to start
PROBE_TIMEOUT_S = 60.0
PROBE_PROMPT = "ok"
KEEP_S = 8 * 86400  # reports older than this are dropped when the file is tidied
TAIL_BYTES = 2_000_000  # how much of the end of a file is read: days of reports
FIVE_HOUR = "five_hour"
WEEKLY = "seven_day"


# ---------------------------------------------------------------------------------------------------- reports


@dataclass(frozen=True)
class Window:
    utilization: float  # 0..1
    resets_at: float | None  # epoch seconds

    def current(self, now: float) -> bool:
        """Still the window it was when reported (it hasn't reset since)."""
        return self.resets_at is None or self.resets_at > now


@dataclass(frozen=True)
class Reading:
    at: float
    status: str  # allowed | allowed_warning | rejected
    kind: str | None = None  # the window the status is about (five_hour, seven_day, ...)
    resets_at: float | None = None  # when that window resets
    windows: Mapping[str, Window] = field(default_factory=dict)
    # Extra usage past the limits, paid from credits ("allowed": on, with credit left; "rejected": off or out).
    overage: str | None = None

    @property
    def paid_past_limit(self) -> bool:
        """A limit is hit, but extra usage is on and has credit: Claude goes on answering from credits, so the hit
        limit isn't a reason to hold her work back. Below the limit the usual guards still leave the user's window
        alone: extra usage being on says nothing about how full it is."""
        return self.status == "rejected" and self.overage == "allowed"

    @property
    def five_hour(self) -> Window | None:
        return self.windows.get(FIVE_HOUR)

    @property
    def weekly(self) -> Window | None:
        """The fullest weekly window (seven_day, or a per-model one like seven_day_opus)."""
        weekly = [w for name, w in self.windows.items() if name.startswith(WEEKLY)]
        return max(weekly, key=lambda w: w.utilization) if weekly else None

    def to_json(self) -> dict[str, Any]:
        return {
            "at": round(self.at, 3),
            "status": self.status,
            "kind": self.kind,
            "resets_at": self.resets_at,
            "windows": {k: [w.utilization, w.resets_at] for k, w in self.windows.items()},
            **({"overage": self.overage} if self.overage else {}),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Reading:
        windows = {
            str(k): Window(float(v[0]), _num(v[1]))
            for k, v in (data.get("windows") or {}).items()
            if v and v[0] is not None
        }
        return cls(
            at=float(data["at"]),
            status=str(data.get("status") or "allowed"),
            kind=data.get("kind"),
            resets_at=_num(data.get("resets_at")),
            windows=windows,
            overage=str(data["overage"]) if data.get("overage") else None,
        )


def parse(info: Any, at: float | None = None) -> Reading | None:
    """A Reading from a ``RateLimitInfo`` (or its raw dict, as the CLI sends it); None when it isn't one."""
    raw = getattr(info, "raw", None) if not isinstance(info, Mapping) else info
    if not isinstance(raw, Mapping) or not raw.get("status"):
        status = getattr(info, "status", None)
        if not status:
            return None
        raw = {
            "status": status,
            "resetsAt": getattr(info, "resets_at", None),
            "rateLimitType": getattr(info, "rate_limit_type", None),
            "utilization": getattr(info, "utilization", None),
        }
    windows: dict[str, Window] = {}
    unified = raw.get("unifiedWindows")
    for name, window in unified.items() if isinstance(unified, Mapping) else ():
        if isinstance(window, Mapping) and _num(window.get("utilization")) is not None:
            windows[str(name)] = Window(float(window["utilization"]), _num(window.get("resetsAt")))
    kind = raw.get("rateLimitType")
    # An older CLI reports only the window the status is about.
    if kind and kind not in windows and _num(raw.get("utilization")) is not None:
        windows[str(kind)] = Window(float(raw["utilization"]), _num(raw.get("resetsAt")))
    return Reading(
        at=time.time() if at is None else at,
        status=str(raw["status"]),
        kind=str(kind) if kind else None,
        resets_at=_num(raw.get("resetsAt")),
        windows=windows,
        overage=str(raw["overageStatus"]) if raw.get("overageStatus") else None,
    )


def _num(value: Any) -> float | None:
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------- the folder

_directory: Path | None = None
_budget: Budget | None = None


def directory() -> Path | None:
    """Where reports go: the configured folder, else ``DIR_ENV`` (a subprocess), else nowhere."""
    if _directory is not None:
        return _directory
    env = os.environ.get(DIR_ENV)
    return Path(env) if env else None


def observe(info: Any) -> Reading | None:
    """Record one report of the account's limits. Never raises: it runs inside every Claude turn."""
    try:
        folder = directory()
        reading = parse(info)
        if folder is None or reading is None:
            return reading
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / OBSERVATIONS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(reading.to_json()) + "\n")
        return reading
    except Exception:
        log.debug("Couldn't record a Claude usage report", exc_info=True)
        return None


def configure(config: Any, *, tidy: bool = False) -> Budget:
    """Set up the usage folder and the budget from the app's config (``usage_budget``). Returns the budget.
    ``tidy``: drop old reports (the app only: rewriting the files while it appends to them could lose a row)."""
    global _directory, _budget
    folder = config.resolve_path("data/usage")
    _directory = folder
    _budget = Budget(
        BudgetSettings.from_config(config.usage_budget),
        folder,
        probe=_cli_probe(config),
        api_key=config.brain.claude.auth == "api_key",
    )
    if tidy:
        _budget.tidy()
    return _budget


def budget() -> Budget | None:
    """The configured budget (None: no app here, nothing is checked)."""
    return _budget


def reset() -> None:
    """Forget the configuration (tests)."""
    global _directory, _budget
    _directory = _budget = None


def check(job: str, *, share: bool = True) -> str | None:
    """Why background ``job`` mustn't run now, by the last report (None: it may, or no budget is configured).
    ``share=False``: only your limits count, not today's share for her own work (work you handed her)."""
    if _budget is None:
        return None
    return _budget.check(job) if share else _budget.check(job, share=False)


async def refresh(*, share: bool = True) -> None:
    """Take a fresh reading when the last one is stale (nothing when no budget is configured)."""
    if _budget is not None:
        await (_budget.refresh() if share else _budget.refresh(share=False))


async def check_fresh(job: str, *, share: bool = True) -> str | None:
    """``check`` on a fresh reading: what a background job asks before it starts."""
    if share:  # the plain calls, as every caller before work the user asked for made them
        await refresh()
        return check(job)
    await refresh(share=False)
    return check(job, share=False)


def used_between(start: float, end: float) -> float | None:
    """How much of your weekly limit grew from ``start`` to ``end`` (epoch seconds), whatever ran meanwhile; None
    when no budget is configured."""
    return _budget.used_between(start, end) if _budget is not None else None


@contextlib.asynccontextmanager
async def charged(job: str) -> AsyncIterator[None]:
    """Count what ``job`` used against today's share (nothing when no budget is configured)."""
    if _budget is None:
        yield
        return
    async with _budget.charged(job):
        yield


# ---------------------------------------------------------------------------------------------------- the budget


@dataclass(frozen=True)
class BudgetSettings:
    enabled: bool = True
    daily_share: float = 0.05  # of the weekly limit, per local day
    max_five_hour: float = 0.6
    max_weekly: float = 0.8

    @classmethod
    def from_config(cls, cfg: Any) -> BudgetSettings:
        return cls(
            enabled=bool(cfg.enabled),
            daily_share=float(cfg.daily_share),
            max_five_hour=float(cfg.max_five_hour),
            max_weekly=float(cfg.max_weekly),
        )


Probe = Callable[[], Awaitable[None]]


class Budget:
    def __init__(
        self,
        settings: BudgetSettings,
        folder: Path,
        clock: Any = time.time,
        probe: Probe | None = None,
        *,
        api_key: bool = False,
    ) -> None:
        self.settings = settings
        self.folder = folder
        # Billed to the user's API credits (``brain.claude.auth: api_key``, which her builds use too): the
        # subscription's limits don't apply, and its last report (maybe days old, "rejected" until next week) mustn't
        # hold her work back. Builds were skipped "until 4 pm tomorrow" while the API credits paid for every turn.
        self.api_key = api_key
        self._clock = clock
        self._probe = probe  # takes a fresh reading (a Claude call that ``observe``s its report)
        self._probing: asyncio.Lock | None = None
        self._said: set[tuple[str, str]] = set()  # (day, job): skips already written today
        self._running: dict[int, tuple[str, float]] = {}  # jobs under way: (name, start)
        self._next = 0

    # ------------------------------------------------------------------ reading

    def readings(self, since: float = 0.0) -> list[Reading]:
        out = []
        for row in _tail(self.folder / OBSERVATIONS):
            try:
                reading = Reading.from_json(row)
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if reading.at >= since:
                out.append(reading)
        out.sort(key=lambda r: r.at)
        return out

    def latest(self) -> Reading | None:
        readings = self.readings()
        return readings[-1] if readings else None

    def jobs(self, since: float = 0.0) -> list[dict[str, Any]]:
        return [j for j in _tail(self.folder / JOBS) if float(j.get("start") or 0) >= since]

    def skips(self, since: float = 0.0) -> list[dict[str, Any]]:
        return [s for s in _tail(self.folder / SKIPS) if float(s.get("at") or 0) >= since]

    def used_today(self, now: float | None = None) -> float:
        """How much of the weekly limit background jobs used today (local day), the ones under way included.

        Jobs that overlap (builds run side by side) are counted once for the time they overlap: it's the weekly
        window's growth over the stretches when any of them ran."""
        now = self._clock() if now is None else now
        day = _day_start(now)
        spans = [(float(j["start"]), float(j.get("end") or j["start"])) for j in self.jobs(day) if "start" in j]
        spans += [(start, now) for _, start in self._running.values() if start >= day]
        if not spans:
            return 0.0
        readings = self.readings(min(s for s, _ in spans) - RECENT_S)
        return sum(_span_growth(readings, start, end) for start, end in _merged(spans))

    # ------------------------------------------------------------------ deciding

    async def refresh(self, *, share: bool = True) -> None:
        """A fresh reading, when the budget is on, there's a way to take one, and the last is older than FRESH_S."""
        if not self.settings.enabled or self._probe is None or self._fresh():
            return
        if share and self.used_today() >= self.settings.daily_share:
            return  # no reading can give back today's share: spend nothing finding out
        self._probing = self._probing or asyncio.Lock()
        async with self._probing:
            if self._fresh():  # another check took one meanwhile
                return
            try:
                async with asyncio.timeout(PROBE_TIMEOUT_S):
                    await self._probe()
            except Exception as exc:  # no reading: decide on the last one
                log.info("Couldn't take a fresh Claude usage reading: %s", exc)

    def _fresh(self) -> bool:
        latest = self.latest()
        return latest is not None and self._clock() - latest.at < FRESH_S

    def check(self, job: str, *, share: bool = True) -> str | None:
        """Why ``job`` mustn't run now, in words for the user (None: go ahead). A refusal is logged and written to
        the skip log, once a day per job. ``share=False``: today's share for her own work doesn't count."""
        reason = self.reason(share=share)
        if reason is not None:
            self.skipped(job, reason)
        return reason

    def reason(self, now: float | None = None, *, share: bool = True) -> str | None:
        s = self.settings
        if not s.enabled:
            return None
        if self.api_key:  # paid from API credits: no subscription limit or share of it to keep
            if share and s.daily_share <= 0:
                return "background Claude work is switched off (usage_budget.daily_share is 0)"
            return None
        now = self._clock() if now is None else now
        latest = self.latest()
        if latest is not None and not latest.paid_past_limit:
            hit_until = latest.resets_at if latest.resets_at is not None else latest.at + 3600
            if latest.status == "rejected" and hit_until > now:
                return f"your Claude usage limit is reached until {_when(hit_until, now)}"
            if latest.status == "allowed_warning" and hit_until > now:
                return f"you're close to your Claude usage limit (until {_when(hit_until, now)})"
            five = latest.five_hour
            if five is not None and five.current(now) and five.utilization >= s.max_five_hour:
                return (
                    f"your 5-hour Claude window is {_pct(five.utilization)} used, and I leave the rest to you "
                    f"(it resets {_when(five.resets_at, now)})"
                )
            weekly = latest.weekly
            if weekly is not None and weekly.current(now) and weekly.utilization >= s.max_weekly:
                return f"your weekly Claude limit is {_pct(weekly.utilization)} used, and I leave the rest to you"
        if not share:
            return None
        used = self.used_today(now)
        if used >= s.daily_share:
            if s.daily_share <= 0:
                return "background Claude work is switched off (usage_budget.daily_share is 0)"
            return (
                f"today's share for my own work is used up ({_pct(used)} of your weekly limit, of "
                f"{_pct(s.daily_share)})"
            )
        return None

    def skipped(self, job: str, reason: str) -> None:
        now = self._clock()
        key = (datetime.fromtimestamp(now).date().isoformat(), job)
        if key in self._said:
            return
        self._said.add(key)
        log.info("Skipped %s to save your Claude usage: %s", job, reason)
        _append(self.folder / SKIPS, {"at": round(now, 3), "job": job, "reason": reason})

    def used_between(self, start: float, end: float) -> float:
        """The weekly window's growth from ``start`` to ``end``: what work over that stretch cost, counting whatever
        else ran at the same time."""
        return _span_growth(self.readings(start - RECENT_S), start, end)

    # ------------------------------------------------------------------ charging

    @contextlib.asynccontextmanager
    async def charged(self, job: str) -> AsyncIterator[None]:
        """Count the time ``job`` runs as background work (``used_today``), and write what it used when it ends."""
        start = self._clock()
        self._next += 1
        token = self._next
        self._running[token] = (job, start)
        try:
            yield
        finally:
            self._running.pop(token, None)
            with contextlib.suppress(Exception):
                self.charge(job, start, self._clock())

    def charge(self, job: str, start: float, end: float) -> dict[str, Any]:
        """Write ``job``'s run to the job log, with the growth of the weekly window over it (anything else that ran
        meanwhile included; ``used_today`` counts overlapping jobs once)."""
        readings = self.readings(start - RECENT_S)
        last = _last(readings, end)
        record = {
            "job": job,
            "start": round(start, 3),
            "end": round(end, 3),
            "used": round(_span_growth(readings, start, end), 4),
            "five_hour_used": round(_span_growth(readings, start, end, FIVE_HOUR), 4),
            "weekly_after": last.weekly.utilization if last and last.weekly else None,
            "five_hour_after": last.five_hour.utilization if last and last.five_hour else None,
        }
        _append(self.folder / JOBS, record)
        log.info("%s used %s of your weekly Claude limit", job, _pct(record["used"]))
        return record

    # ------------------------------------------------------------------ upkeep

    def tidy(self) -> None:
        """Drop reports older than ``KEEP_S``, so the folder stays small."""
        cutoff = self._clock() - KEEP_S
        for name, key in ((OBSERVATIONS, "at"), (JOBS, "start"), (SKIPS, "at")):
            path = self.folder / name
            try:
                if not path.exists() or path.stat().st_size < 512_000:
                    continue
                rows = [r for r in _tail(path) if float(r.get(key) or 0) >= cutoff]
                tmp = path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
                tmp.replace(path)
            except Exception:
                log.debug("Couldn't tidy %s", path, exc_info=True)

    # ------------------------------------------------------------------ telling

    def describe(self, now: float | None = None) -> str:
        """Where your limits stand, what her own work used today, and what she skipped lately (a few sentences)."""
        now = self._clock() if now is None else now
        s = self.settings
        latest = self.latest()
        parts = []
        if self.api_key:
            # Her own reports come from the subscription; on API credits there are none, and an old one says nothing.
            return (
                "I run on your Anthropic API credits (brain.claude.auth is api_key), so your subscription's limits "
                "don't hold me or my builds back. I can't see how much credit is left: that's on the Claude Console."
            )
        if latest is None:
            parts.append("I haven't had a usage report from Claude yet; one comes with my next Claude reply.")
        else:
            limits = []
            five = latest.five_hour
            if five is not None:
                limits.append(
                    f"your 5-hour window is {_pct(five.utilization if five.current(now) else 0)} used"
                    + (f" (resets {_when(five.resets_at, now)})" if five.current(now) and five.resets_at else "")
                )
            weekly = latest.weekly
            if weekly is not None:
                limits.append(
                    f"your weekly limit is {_pct(weekly.utilization if weekly.current(now) else 0)} used"
                    + (f" (resets {_when(weekly.resets_at, now)})" if weekly.current(now) and weekly.resets_at else "")
                )
            if limits:
                parts.append(f"As of {_ago(latest.at, now)}, " + " and ".join(limits) + ".")
            if latest.status == "rejected" and (latest.resets_at or now + 1) > now:
                parts.append("The limit is reached right now.")
        if not s.enabled:
            parts.append("The budget for my own background work is off, so I don't hold it back.")
        else:
            used = self.used_today(now)
            parts.append(
                f"My own background work used {_pct(used)} of your weekly limit today, out of a daily share of "
                f"{_pct(s.daily_share)}."
            )
            reason = self.reason(now)
            if reason:
                parts.append(f"I'm holding it back right now: {reason}.")
        skipped = self.skips(now - 86400)
        if skipped:
            parts.append("In the last day I skipped " + spoken_skips(skipped) + ".")
        return " ".join(parts)


def report(budget: Budget, days: float = 7) -> str:
    """``octavia usage``: where things stand, then her background jobs and what she skipped over ``days``."""
    now = budget._clock()
    since = now - days * 86400
    lines = [budget.describe(now), "", f"Her background jobs (last {days:g} days):"]
    jobs = budget.jobs(since)
    for job in jobs:
        lines.append(
            f"  {_stamp(float(job['start']))}  {job.get('job')}: {_pct(float(job.get('used') or 0))} of the week, "
            f"{_pct(float(job.get('five_hour_used') or 0))} of a 5-hour window, "
            f"{round((float(job.get('end') or job['start']) - float(job['start'])) / 60)} min"
        )
    if not jobs:
        lines.append("  none")
    lines.append(f"Skipped to save your usage (last {days:g} days):")
    skipped = budget.skips(since)
    lines += [f"  {_stamp(float(s['at']))}  {s.get('job')}: {s.get('reason')}" for s in skipped if "at" in s]
    if not skipped:
        lines.append("  nothing")
    s = budget.settings
    lines += [
        "",
        f"Budget (usage_budget in config): {'on' if s.enabled else 'off'}, daily share {_pct(s.daily_share)} of the "
        f"week, waits at {_pct(s.max_five_hour)} of the 5-hour window or {_pct(s.max_weekly)} of the week.",
    ]
    return "\n".join(lines)


def _cli_probe(config: Any) -> Probe | None:
    """A fresh reading from the Claude Code CLI on her login: one Haiku call with no tools, no MCP servers and no
    settings, whose report ``observe`` records. None when she isn't on the subscription (an API key has no such
    limits to read)."""
    claude = config.brain.claude
    if claude.auth != "subscription":
        return None

    async def probe() -> None:
        from claude_agent_sdk import ClaudeAgentOptions, RateLimitEvent, query

        from octavia.brain.claude import cli_environment, prepare_process_environment

        prepare_process_environment(claude.auth)
        config_dir = config.resolve_path(claude.config_dir) if claude.config_dir else None
        options = ClaudeAgentOptions(
            model="haiku",
            max_turns=1,
            tools=[],
            system_prompt="Reply with the single word ok.",
            setting_sources=[],
            effort="low",
            extra_args={"strict-mcp-config": None},
            env=cli_environment(config_dir),
            cli_path=config.claude_cli_path(),
            cwd=str(_folder(config)),
        )
        async for message in query(prompt=PROBE_PROMPT, options=options):
            if isinstance(message, RateLimitEvent):
                observe(message.rate_limit_info)

    return probe


def _folder(config: Any) -> Path:
    folder = config.resolve_path("data/usage")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def spoken_skips(skips: Iterable[Mapping[str, Any]]) -> str:
    """ "my reflection (your 5-hour window was 72% used) and a fix" style: each job once, with its first reason."""
    seen: dict[str, str] = {}
    for skip in skips:
        seen.setdefault(str(skip.get("job") or "something"), str(skip.get("reason") or ""))
    items = [f"{job} ({reason})" if reason else job for job, reason in seen.items()]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------------------------------------------- helpers


def _last(readings: list[Reading], end: float) -> Reading | None:
    upto = [r for r in readings if r.at <= end + 5]
    return upto[-1] if upto else None


def _span_growth(readings: list[Reading], start: float, end: float, which: str = WEEKLY) -> float:
    """How much window ``which`` grew from ``start`` to ``end``: from the report just before ``start`` (if within
    ``RECENT_S``; else the first one after it, which misses that call) to the last one by ``end``. For the week,
    each weekly window (seven_day, seven_day_opus...) is followed on its own, and the one that grew most counts."""
    names = {n for r in readings for n in r.windows if n == which or (which == WEEKLY and n.startswith(WEEKLY))}
    grown = [0.0]
    for name in names:
        with_window = [r for r in readings if name in r.windows]
        before = [r for r in with_window if start - RECENT_S <= r.at <= start]
        during = [r for r in with_window if start < r.at <= end + 5]
        if during:
            first = before[-1] if before else during[0]
            grown.append(_growth(first.windows[name], during[-1].windows[name]))
    return max(grown)


def _merged(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _growth(first: Window | None, last: Window | None) -> float:
    if first is None or last is None:
        return 0.0
    if first.resets_at is not None and last.resets_at is not None and last.resets_at - first.resets_at > 600:
        return max(0.0, last.utilization)  # the window reset during the job: all of it is since then
    return max(0.0, last.utilization - first.utilization)


def _tail(path: Path) -> list[dict[str, Any]]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if len(data) >= TAIL_BYTES and lines:
        lines = lines[1:]  # the first may be cut in half
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append(path: Path, row: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        log.warning("Couldn't write %s", path, exc_info=True)


def _day_start(now: float) -> float:
    local = datetime.fromtimestamp(now)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _pct(fraction: float) -> str:
    return f"{round(fraction * 100)}%"


def _when(ts: float | None, now: float) -> str:
    if ts is None:
        return "later"
    then = datetime.fromtimestamp(ts)
    clock = then.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ").lower()
    days = (then.date() - datetime.fromtimestamp(now).date()).days
    if days <= 0:
        return f"at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    return f"{then.strftime('%A')} at {clock}"


def _ago(ts: float, now: float) -> str:
    minutes = int(max(0.0, now - ts) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    return f"{round(minutes / 60)} hours ago"
