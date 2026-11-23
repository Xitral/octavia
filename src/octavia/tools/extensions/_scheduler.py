"""Scheduled jobs: instructions she runs on her own, at a set time or when something happens.

A job is a stored instruction plus when to run it: once, every day, on weekdays, on some days of the week, on a
cron schedule, when the computer starts, when the user is back after being away, or when a condition she checks
every few minutes turns true (a calendar event starting soon, a TickTick task nearly due, a disk running low, a high
heart rate or a low Body Battery on the user's Garmin watch). The
checks themselves cost no Claude usage. When a job fires, its instruction runs through the brain as a turn of its
own (``Assistant.run_in_background``) with her usual tools and questions: it runs outside any turn of the user's,
so anything risky is asked first. It waits while the user is talking with her and during quiet hours, and what she
answers is spoken, or shown as a Windows notification.

Jobs are kept in ``data/scheduler/jobs.json`` and every run is added to ``data/scheduler/runs.jsonl``. The jobs
file is read afresh on every look, and each change rewrites only the job it's about, so a job changed while
another runs (or from ``octavia chat``) is never lost. A time that passed while she wasn't running (the computer
was off) is caught up once when she's back, if it's no more than the job's ``catch_up_hours`` late.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octavia.core.jsonfile import read_json, write_json
from octavia.core.text import clock, day, spoken_list

if TYPE_CHECKING:
    from octavia.config import SchedulerConfig

log = logging.getLogger(__name__)

JOBS_FILE = "data/scheduler/jobs.json"
TIMED = ("once", "daily", "weekdays", "weekly", "cron")
KINDS = (*TIMED, "startup", "idle_return", "condition")
CONDITIONS = ("calendar_soon", "ticktick_due_soon", "disk_low", "garmin_heart_rate_high", "garmin_body_battery_low")
DELIVERIES = ("speak", "notify")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# What a condition compares against when the job doesn't say: minutes ahead, gigabytes free, beats per minute, or
# Body Battery.
DEFAULT_THRESHOLD = {
    "calendar_soon": 15.0,
    "ticktick_due_soon": 30.0,
    "disk_low": 10.0,
    "garmin_heart_rate_high": 120.0,
    "garmin_body_battery_low": 20.0,
}
GARMIN_FRESH = timedelta(minutes=30)  # a heart rate reading older than this isn't "now" (the watch hasn't synced)
MAX_SEEN = 200  # what a condition job remembers having fired for
MAX_RESULT = 600  # characters of a run's result kept in the log
KEEP_RUNS = 1000  # the run log is trimmed to this many runs
PROBE_TIMEOUT_S = 30.0
LATE = timedelta(minutes=10)  # a run this late (quiet hours, the user was busy) says so
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def parse_hhmm(text: str) -> time:
    match = _HHMM.match(text.strip())
    if not match:
        raise ValueError(f"“{text}” isn't a time like 08:30")
    return time(int(match[1]), int(match[2]))


def parse_moment(text: str, now: datetime) -> datetime:
    """A one-time job's moment: "2026-09-27 08:30" (or ISO), or just "08:30" for its next occurrence."""
    text = text.strip()
    if _HHMM.match(text):
        at = datetime.combine(now.date(), parse_hhmm(text))
        return at if at > now else at + timedelta(days=1)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"“{text}” isn't a date and time like 2026-09-27 08:30") from None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.replace(second=0, microsecond=0)


# --------------------------------------------------------------------------------------------------- cron


@dataclass(frozen=True)
class Cron:
    """A five-field cron expression: minute, hour, day of month, month, day of week (0 or 7 = Sunday).

    As in cron, when both the day of month and the day of week are restricted, a day matching either one counts.
    """

    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # Python's: 0 = Monday
    any_day: bool
    any_weekday: bool


_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_CRON_DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}


def _cron_field(text: str, low: int, high: int, names: dict[str, int] | None = None) -> set[int]:
    values: set[int] = set()

    def number(part: str) -> int:
        part = part.strip().lower()
        if names and part in names:
            return names[part]
        if not part.isdigit():
            raise ValueError(f"“{part}”")
        return int(part)

    for piece in text.split(","):
        base, _, step_text = piece.partition("/")
        step = int(step_text) if step_text.isdigit() else None
        if step_text and not step:
            raise ValueError(f"“{piece}”")
        if base == "*":
            first, last = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            first, last = number(a), number(b)
        else:
            first = last = number(base)
            if step:
                last = high
        if not (low <= first <= high and low <= last <= high) or first > last:
            raise ValueError(f"“{piece}” is out of range")
        values.update(range(first, last + 1, step or 1))
    return values


def parse_cron(text: str) -> Cron:
    parts = text.split()
    if len(parts) != 5:
        raise ValueError(f"“{text}” isn't a cron schedule: it needs 5 fields (minute hour day month weekday)")
    try:
        minutes = _cron_field(parts[0], 0, 59)
        hours = _cron_field(parts[1], 0, 23)
        days = _cron_field(parts[2], 1, 31)
        months = _cron_field(parts[3], 1, 12, _MONTHS)
        cron_weekdays = _cron_field(parts[4], 0, 7, _CRON_DAYS)
    except ValueError as exc:
        raise ValueError(f"The cron schedule “{text}” has a part it can't read: {exc}") from None
    return Cron(
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        days=frozenset(days),
        months=frozenset(months),
        weekdays=frozenset((d - 1) % 7 for d in cron_weekdays),
        any_day=parts[2] == "*",
        any_weekday=parts[4] == "*",
    )


def next_cron(cron: Cron, after: datetime) -> datetime | None:
    """The first minute strictly after ``after`` that ``cron`` matches (None if none in the next four years)."""
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for offset in range(366 * 4 + 1):
        d = start.date() + timedelta(days=offset)
        if d.month not in cron.months:
            continue
        in_month, on_weekday = d.day in cron.days, d.weekday() in cron.weekdays
        if cron.any_day or cron.any_weekday:
            matches = in_month and on_weekday
        else:
            matches = in_month or on_weekday
        if not matches:
            continue
        for hour in cron.hours:
            for minute in cron.minutes:
                moment = datetime.combine(d, time(hour, minute))
                if moment >= start:
                    return moment
    return None


# --------------------------------------------------------------------------------------------------- jobs


@dataclass
class Job:
    """One scheduled job, as stored. Times are local and naive (ISO strings in the file)."""

    id: int
    name: str
    instruction: str
    kind: str
    at: str = ""  # "HH:MM" (daily, weekdays, weekly); ISO date and time (once)
    days: list[str] = field(default_factory=list)  # weekly: "mon".."sun"
    cron: str = ""
    condition: str = ""
    threshold: float = 0.0  # condition: minutes ahead, GB free, bpm or Body Battery; 0 = default
    drive: str = ""  # disk_low: "D:\"; empty = the system drive
    every_min: float = 5.0  # how often a condition is checked
    idle_min: float = 30.0  # idle_return: away at least this long
    delivery: str = "speak"
    catch_up_hours: float = 12.0  # a time missed while she wasn't running is caught up if it's no later than this
    paused: bool = False
    created: str = ""
    next_run: str | None = None  # timed jobs
    last_run: str | None = None
    last_status: str = ""
    seen: list[str] = field(default_factory=list)  # condition jobs: what already fired, so each thing fires once

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> Job | None:
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            job = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None
        if not isinstance(job.id, int) or job.kind not in KINDS:
            return None
        return job

    @property
    def limit(self) -> float:
        return self.threshold or DEFAULT_THRESHOLD.get(self.condition, 0.0)

    def validate(self, now: datetime) -> None:
        """Raise ValueError, with a message for the user, when this job can't work as set up."""
        if not self.name.strip():
            raise ValueError("The job needs a name.")
        if not self.instruction.strip():
            raise ValueError("The job needs an instruction: what to do when it runs.")
        if self.kind not in KINDS:
            raise ValueError(f"“{self.kind}” isn't a kind of schedule; use one of {', '.join(KINDS)}.")
        if self.delivery not in DELIVERIES:
            raise ValueError("Delivery must be speak or notify.")
        if self.kind in ("daily", "weekdays", "weekly"):
            if not self.at:
                raise ValueError("Say what time it runs, like 08:00.")
            parse_hhmm(self.at)
        if self.kind == "weekly":
            bad = [d for d in self.days if d not in WEEKDAYS]
            if bad or not self.days:
                raise ValueError("A weekly job needs its days, like mon and thu.")
        if self.kind == "once":
            if not self.at:
                raise ValueError("Say when it runs, like 2026-09-27 08:30.")
            if parse_moment(self.at, now) <= now:
                raise ValueError("That time has already passed.")
        if self.kind == "cron":
            parse_cron(self.cron)
        if self.kind == "condition" and self.condition not in CONDITIONS:
            raise ValueError(f"The condition must be one of {', '.join(CONDITIONS)}.")
        if self.kind == "condition" and not 1 <= self.every_min <= 24 * 60:
            raise ValueError("A condition is checked every 1 minute to 24 hours.")
        if self.kind == "idle_return" and not 1 <= self.idle_min <= 24 * 60:
            raise ValueError("Away time must be 1 minute to 24 hours.")
        if self.threshold < 0 or self.catch_up_hours < 0:
            raise ValueError("Numbers can't be negative.")

    def describe_when(self) -> str:
        """When it runs, in words: "on weekdays at 7:30 AM"."""
        if self.kind == "once":
            moment = _parse_dt(self.next_run) or _parse_dt(self.at)
            return f"once, on {day(moment)} at {clock(moment)}" if moment else "once"
        if self.kind in ("daily", "weekdays", "weekly"):
            at = clock(parse_hhmm(self.at))
            if self.kind == "daily":
                return f"every day at {at}"
            if self.kind == "weekdays":
                return f"on weekdays at {at}"
            names = [DAY_NAMES[WEEKDAYS.index(d)] for d in WEEKDAYS if d in self.days]
            listed = spoken_list(names)
            return f"every {listed} at {at}"
        if self.kind == "cron":
            return f"on the cron schedule “{self.cron}”"
        if self.kind == "startup":
            return "when the computer starts"
        if self.kind == "idle_return":
            return f"when the user is back after at least {self.idle_min:g} minutes away"
        if self.condition == "calendar_soon":
            what = f"when a calendar event starts within {self.limit:g} minutes"
        elif self.condition == "ticktick_due_soon":
            what = f"when a TickTick task is due within {self.limit:g} minutes"
        elif self.condition == "garmin_heart_rate_high":
            what = f"when the heart rate on the Garmin watch is above {self.limit:g}"
        elif self.condition == "garmin_body_battery_low":
            what = f"when Body Battery on the Garmin watch is {self.limit:g} or lower"
        else:
            what = f"when {self.drive or 'the system drive'} has less than {self.limit:g} GB free"
        return f"{what} (checked every {self.every_min:g} minutes)"

    def summary(self) -> str:
        """One line for a listing."""
        parts = [f"#{self.id} “{self.name}”: {self.describe_when()}"]
        if self.delivery == "notify":
            parts.append("shown as a notification")
        if self.paused:
            parts.append("PAUSED")
        elif self.kind in TIMED and (upcoming := _parse_dt(self.next_run)):
            parts.append(f"next on {day(upcoming)} at {clock(upcoming)}")
        if last := _parse_dt(self.last_run):
            parts.append(
                f"last ran {day(last)} at {clock(last)}" + (f" ({self.last_status})" if self.last_status else "")
            )
        return "; ".join(parts) + f". Does: {self.instruction}"


def next_time(job: Job, after: datetime) -> datetime | None:
    """When a timed job runs next, strictly after ``after``; None for a one-time job whose moment has passed."""
    if job.kind == "once":
        moment = parse_moment(job.at, after)
        return moment if moment > after else None
    if job.kind == "cron":
        return next_cron(parse_cron(job.cron), after)
    at = parse_hhmm(job.at)
    allowed = {
        "daily": range(7),
        "weekdays": range(5),
        "weekly": [WEEKDAYS.index(d) for d in job.days if d in WEEKDAYS],
    }[job.kind]
    for offset in range(8):
        moment = datetime.combine(after.date() + timedelta(days=offset), at)
        if moment > after and moment.weekday() in allowed:
            return moment
    return None


def job_prompt(job: Job, trigger_note: str | None) -> tuple[str, str]:
    """What the brain gets for a run: the instruction (the turn's text) and a note on where it came from."""
    note = (
        f'this is your scheduled job #{job.id} "{job.name}" ({job.describe_when()}), running on its own: the user '
        "didn't just say it, so do what it asks and tell them the result briefly"
    )
    if trigger_note:
        note += f"; {trigger_note}"
    if job.delivery == "notify":
        note += "; your reply is shown to them as a notification, not spoken, so keep it to a sentence or two"
    return job.instruction, note


# --------------------------------------------------------------------------------------------------- quiet hours


def quiet_span(setting: str) -> tuple[time, time] | None:
    if not setting:
        return None
    start, _, end = setting.partition("-")
    return parse_hhmm(start), parse_hhmm(end)


def in_quiet_hours(now: datetime, setting: str) -> bool:
    """Whether ``now`` falls in "HH:MM-HH:MM" (which may run past midnight)."""
    span = quiet_span(setting)
    if span is None or span[0] == span[1]:
        return False
    start, end = span
    moment = now.time()
    return start <= moment < end if start < end else moment >= start or moment < end


# --------------------------------------------------------------------------------------------------- storage


@dataclass(frozen=True)
class RunRecord:
    """One run of a job, as logged. ``status``: done | failed | interrupted | missed."""

    job_id: int
    name: str
    at: str
    trigger: str
    status: str
    result: str = ""

    def summary(self) -> str:
        moment = _parse_dt(self.at)
        when = f"{day(moment)} at {clock(moment)}" if moment else self.at
        text = f"#{self.job_id} “{self.name}”, {when} ({self.trigger}): {self.status}"
        return text + (f". {self.result}" if self.result else "")


class JobStore:
    """The jobs file and the run log. Every change reads the file first, so it never overwrites another's."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.runs_path = path.with_name("runs.jsonl")

    def load(self) -> list[Job]:
        data = read_json(self.path, "the scheduled jobs")
        items = data.get("jobs", []) if isinstance(data, dict) else []
        return [job for job in (Job.from_json(item) for item in items) if job is not None]

    def _save(self, jobs: Iterable[Job]) -> None:
        write_json(self.path, {"jobs": [j.to_json() for j in jobs]}, indent=1)

    def add(self, job: Job) -> Job:
        jobs = self.load()
        job = replace(job, id=max((j.id for j in jobs), default=0) + 1)
        self._save([*jobs, job])
        return job

    def update(self, job_id: int, **changes: Any) -> Job | None:
        """Change fields of one job; None (and nothing written) when it's gone."""
        jobs = self.load()
        for i, job in enumerate(jobs):
            if job.id == job_id:
                jobs[i] = replace(job, **changes)
                self._save(jobs)
                return jobs[i]
        return None

    def remove(self, job_id: int) -> Job | None:
        jobs = self.load()
        gone = next((j for j in jobs if j.id == job_id), None)
        if gone is not None:
            self._save([j for j in jobs if j.id != job_id])
        return gone

    def log_run(self, record: RunRecord) -> None:
        try:
            self.runs_path.parent.mkdir(parents=True, exist_ok=True)
            with self.runs_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record)) + "\n")
            if self.runs_path.stat().st_size > KEEP_RUNS * 1000:
                lines = self.runs_path.read_text(encoding="utf-8").splitlines()[-KEEP_RUNS:]
                self.runs_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            log.warning("Couldn't log a scheduled job's run", exc_info=True)

    def runs(self, limit: int = 10, job_id: int | None = None) -> list[RunRecord]:
        """The most recent runs, newest first."""
        try:
            lines = self.runs_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        found: list[RunRecord] = []
        for line in reversed(lines):
            try:
                record = RunRecord(**json.loads(line))
            except (ValueError, TypeError):
                continue
            if job_id is None or record.job_id == job_id:
                found.append(record)
                if len(found) >= limit:
                    break
        return found


def find_job(jobs: list[Job], ref: str) -> Job | None:
    """A job by its number ("3", "#3") or its name (exactly, else the one name containing it)."""
    ref = ref.strip()
    number = ref.lstrip("#")
    if number.isdigit():
        return next((j for j in jobs if j.id == int(number)), None)
    wanted = ref.casefold()
    exact = [j for j in jobs if j.name.casefold() == wanted]
    if exact:
        return exact[0]
    partial = [j for j in jobs if wanted and wanted in j.name.casefold()]
    return partial[0] if len(partial) == 1 else None


# --------------------------------------------------------------------------------------------------- conditions


@dataclass(frozen=True)
class Hit:
    """Something a condition found: ``key`` names it (so it fires once), ``text`` says it."""

    key: str
    text: str


Probe = Callable[[Job, datetime], Awaitable[list[Hit]]]


async def disk_low(job: Job, now: datetime) -> list[Hit]:
    import psutil

    drive = job.drive or os.environ.get("SystemDrive", "C:")
    if len(drive) == 2 and drive.endswith(":"):
        drive += "\\"
    usage = await asyncio.to_thread(psutil.disk_usage, drive)
    free_gb = usage.free / 1024**3
    if free_gb >= job.limit:
        return []
    return [Hit(f"disk:{drive.upper()}", f"only {free_gb:.1f} GB is free on {drive}")]


async def calendar_soon(job: Job, now: datetime) -> list[Hit]:
    from octavia.tools.extensions import calendar_feed

    urls = calendar_feed.calendar_urls()
    if not urls:
        raise RuntimeError("no calendar is connected")
    reader = calendar_feed.GetCalendarEvents()
    events: list[calendar_feed.CalendarEvent] = []
    for url in urls:
        events.extend(calendar_feed.parse_ics(await reader._fetch(url)))
    start = now.astimezone()
    found = calendar_feed.occurrences(events, start, start + timedelta(minutes=job.limit))
    hits = []
    for occ in found:
        if occ.all_day or occ.start < start:
            continue
        where = f" at {occ.location}" if occ.location else ""
        begins = occ.start.astimezone()
        hits.append(Hit(f"cal:{occ.title}@{_iso(begins)}", f"“{occ.title}”{where} starts at {clock(begins)}"))
    return hits


async def ticktick_due_soon(job: Job, now: datetime) -> list[Hit]:
    from octavia.tools.extensions import ticktick

    client = ticktick.shared_client()
    if not client.configured:
        raise RuntimeError("TickTick isn't connected")
    horizon = now + timedelta(minutes=job.limit)
    return [
        Hit(f"tt:{task.id}@{_iso(task.due)}", f"the TickTick task “{task.title}” is due at {clock(task.due)}")
        for task in await ticktick.due_tasks(client)
        if not task.all_day and now <= task.due <= horizon
    ]


async def garmin_heart_rate_high(job: Job, now: datetime) -> list[Hit]:
    """The newest synced reading, if it's recent and above the limit. Its key stays the same while the heart rate
    stays high, so it fires once each time it goes over."""
    from octavia.tools.extensions import _garmin

    client = _garmin.shared_client()
    if not client.connected:
        raise RuntimeError("Garmin isn't connected")
    latest = _garmin.latest_heart_rate(await client.heart_rate(now.date()))
    if latest is None:
        return []
    bpm, at = latest
    if bpm <= job.limit or now.astimezone() - at > GARMIN_FRESH:
        return []
    return [Hit("garmin:heart_rate_high", f"the heart rate on the Garmin watch was {bpm} at {clock(at)}")]


async def garmin_body_battery_low(job: Job, now: datetime) -> list[Hit]:
    from octavia.tools.extensions import _garmin

    client = _garmin.shared_client()
    if not client.connected:
        raise RuntimeError("Garmin isn't connected")
    level = (await client.summary(now.date())).get("bodyBatteryMostRecentValue")
    if isinstance(level, bool) or not isinstance(level, int | float) or level > job.limit:
        return []
    return [Hit(f"garmin:body_battery_low:{now.date()}", f"Body Battery on the Garmin watch is down to {round(level)}")]


PROBES: dict[str, Probe] = {
    "disk_low": disk_low,
    "calendar_soon": calendar_soon,
    "ticktick_due_soon": ticktick_due_soon,
    "garmin_heart_rate_high": garmin_heart_rate_high,
    "garmin_body_battery_low": garmin_body_battery_low,
}


# --------------------------------------------------------------------------------------------------- the machine


def idle_seconds() -> float | None:
    """How long since the last keyboard or mouse input, by Windows' clock; None when it can't tell."""
    try:
        from octavia.tools.extensions._input import WindowsDesktop

        desktop = WindowsDesktop()
        return ((desktop.tick() - desktop.last_input()) % 2**32) / 1000
    except Exception:
        return None


def boot_time() -> datetime:
    import psutil

    return datetime.fromtimestamp(psutil.boot_time()).replace(microsecond=0)


def show_notification(title: str, text: str, seconds: float = 10.0) -> None:
    """A Windows notification (a tray balloon, shown as a toast on Windows 10 and 11). Blocks: run it in a thread."""
    import time as clock_time

    import win32api
    import win32con
    import win32gui

    instance = win32api.GetModuleHandle(None)
    window_class = win32gui.WNDCLASS()
    window_class.hInstance = instance
    window_class.lpszClassName = "OctaviaSchedulerNotice"
    window_class.lpfnWndProc = {}
    with contextlib.suppress(win32gui.error):  # already registered by an earlier notification
        win32gui.RegisterClass(window_class)
    hwnd = win32gui.CreateWindow(window_class.lpszClassName, title, 0, 0, 0, 0, 0, 0, 0, instance, None)
    try:
        icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        message = win32con.WM_USER + 20
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (hwnd, 0, win32gui.NIF_ICON | win32gui.NIF_MESSAGE, message, icon))
        win32gui.Shell_NotifyIcon(
            win32gui.NIM_MODIFY, (hwnd, 0, win32gui.NIF_INFO, message, icon, "", text[:255], 200, title[:63])
        )
        clock_time.sleep(seconds)
        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
    finally:
        win32gui.DestroyWindow(hwnd)


# --------------------------------------------------------------------------------------------------- the runner


@dataclass(frozen=True)
class RunOutcome:
    """How a run's turn went: what she answered, or why it failed."""

    text: str = ""
    error: str | None = None
    interrupted: bool = False


Run = Callable[[str, str, bool], Awaitable[RunOutcome]]  # (instruction, note, speak) -> outcome
Notify = Callable[[str, str], Awaitable[None]]


async def _windows_notification(title: str, text: str) -> None:
    await asyncio.to_thread(show_notification, title, text)


class Scheduler:
    """Looks at the jobs every ``check_every_s`` seconds and runs what's due. ``start()``, then ``close()``."""

    def __init__(
        self,
        store: JobStore,
        run: Run,
        settings: SchedulerConfig,
        *,
        notify: Notify = _windows_notification,
        probes: dict[str, Probe] | None = None,
        idle: Callable[[], float | None] = idle_seconds,
        booted: Callable[[], datetime] = boot_time,
        clock: Callable[[], datetime] = _now,
        title: str = "Assistant",
    ) -> None:
        self.store = store
        self.settings = settings
        self._run = run
        self._notify = notify
        self._probes = PROBES if probes is None else probes
        self._idle = idle
        self._booted = booted
        self._clock = clock
        self._title = title
        self._started = clock()
        self._running: dict[int, asyncio.Task[None]] = {}
        self._checked: dict[int, datetime] = {}  # condition jobs: when they were last checked
        self._failing: set[int] = set()  # condition jobs whose check fails (said once, not every time)
        self._last_idle = 0.0
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="scheduler")

    async def close(self) -> None:
        tasks = [t for t in (self._task, *self._running.values()) if t is not None]
        self._task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("The scheduler's check failed")
            await asyncio.sleep(self.settings.check_every_s)

    async def wait_runs(self) -> None:
        """Return once every run started so far has finished (for tests and shutdown)."""
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

    # ------------------------------------------------------------------ one look

    async def tick(self) -> None:
        now = self._clock()
        quiet = in_quiet_hours(now, self.settings.quiet_hours)
        back_after = self._came_back()
        for job in self.store.load():
            if job.paused or job.id in self._running:
                continue
            try:
                if job.kind in TIMED:
                    self._timed(job, now, quiet)
                elif quiet:
                    continue  # held: a startup job or a condition that's still true runs once it's over
                elif job.kind == "startup":
                    self._startup(job, now)
                elif job.kind == "idle_return":
                    if back_after is not None and back_after >= job.idle_min * 60:
                        minutes = round(back_after / 60)
                        self._fire(job, "the user came back", f"the user just came back after about {minutes} minutes")
                elif job.kind == "condition":
                    await self._condition(job, now)
            except Exception:
                log.exception("Couldn't check scheduled job #%s", job.id)

    def _came_back(self) -> float | None:
        """How long the user was away, the moment they're back (their input made the idle time drop)."""
        idle = self._idle()
        if idle is None:
            return None
        before, self._last_idle = self._last_idle, idle
        return before if idle < before else None

    def _timed(self, job: Job, now: datetime, quiet: bool) -> None:
        due = _parse_dt(job.next_run)
        if due is None:
            upcoming = next_time(job, now)
            if upcoming is not None:
                self.store.update(job.id, next_run=_iso(upcoming))
            elif job.kind == "once":
                self.store.remove(job.id)
                self._log(job, now, "its time had passed", "missed")
            return
        if due > now or quiet:
            return  # not yet, or held until quiet hours are over
        upcoming = None if job.kind == "once" else next_time(job, now)
        when = f"{day(due)} at {clock(due)}" if due.date() != now.date() else clock(due)
        while_off = due < self._started - timedelta(minutes=1)
        if job.kind == "once":
            self.store.remove(job.id)
        else:
            self.store.update(job.id, next_run=_iso(upcoming) if upcoming else None)
        if while_off and now - due > timedelta(hours=job.catch_up_hours):
            self._log(job, now, f"due {when}", "missed", "It was due while I wasn't running, too long ago to catch up.")
            return
        if while_off:
            note = f"it was due at {when}, while you weren't running (the computer was off), so it's catching up now"
        elif now - due > LATE:
            note = f"it was due at {when} and runs late (quiet hours, or the user was busy)"
        else:
            note = None
        self._fire(job, f"scheduled for {when}", note)

    def _startup(self, job: Job, now: datetime) -> None:
        booted = self._booted()
        since = max(_parse_dt(job.last_run) or datetime.min, _parse_dt(job.created) or datetime.min)
        if booted > since:
            self.store.update(job.id, last_run=_iso(now))
            self._fire(job, "the computer started", f"the computer started at {clock(booted)}")

    async def _condition(self, job: Job, now: datetime) -> None:
        checked = self._checked.get(job.id)
        if checked is not None and now - checked < timedelta(minutes=job.every_min) - timedelta(seconds=1):
            return
        probe = self._probes.get(job.condition)
        if probe is None:
            return
        self._checked[job.id] = now
        try:
            hits = await asyncio.wait_for(probe(job, now), PROBE_TIMEOUT_S)
        except Exception as exc:
            if job.id not in self._failing:
                log.warning("Couldn't check the condition of scheduled job #%s: %s", job.id, exc)
            self._failing.add(job.id)
            return
        self._failing.discard(job.id)
        new = [hit for hit in hits if hit.key not in job.seen]
        keys = [hit.key for hit in hits][:MAX_SEEN]
        if keys != job.seen:
            self.store.update(job.id, seen=keys)
        if new:
            found = "; ".join(hit.text for hit in new)
            self._fire(job, found, f"it was triggered because {found}")

    # ------------------------------------------------------------------ a run

    def _fire(self, job: Job, trigger: str, note: str | None) -> None:
        log.info("Running scheduled job #%s (%s): %s", job.id, job.name, trigger)
        self._running[job.id] = asyncio.get_running_loop().create_task(
            self._run_job(job, trigger, note), name=f"scheduled-job-{job.id}"
        )

    async def _run_job(self, job: Job, trigger: str, note: str | None) -> None:
        started = self._clock()
        try:
            text, full_note = job_prompt(job, note)
            try:
                outcome = await self._run(text, full_note, job.delivery == "speak")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Scheduled job #%s failed", job.id, exc_info=True)
                outcome = RunOutcome(error=f"{type(exc).__name__}: {exc}")
            status = "failed" if outcome.error else "interrupted" if outcome.interrupted else "done"
            self._log(job, started, trigger, status, outcome.error or outcome.text)
            self.store.update(job.id, last_run=_iso(started), last_status=status)
            if job.delivery == "notify" and outcome.text.strip() and not outcome.error:
                try:
                    await self._notify(f"{self._title}: {job.name}", " ".join(outcome.text.split()))
                except Exception:
                    log.warning("Couldn't show the notification for scheduled job #%s", job.id, exc_info=True)
        finally:
            self._running.pop(job.id, None)

    def _log(self, job: Job, at: datetime, trigger: str, status: str, result: str = "") -> None:
        result = " ".join(result.split())
        if len(result) > MAX_RESULT:
            result = result[: MAX_RESULT - 1] + "…"
        self.store.log_run(RunRecord(job.id, job.name, _iso(at), trigger, status, result))
