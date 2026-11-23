"""Tools to set up the jobs she runs on her own (see ``_scheduler`` for how they run).

Creating or changing a job follows ``scheduler.changes`` (by default: no question when the user asks for it, asked
when she'd do it on her own, since a job goes on acting later); deleting one is always asked. Pausing, resuming and
reading them never are. The jobs are only run by the voice assistant (``App._start_scheduler``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._scheduler import (
    JOBS_FILE,
    TIMED,
    Job,
    JobStore,
    _iso,
    _now,
    clock,
    day,
    find_job,
    in_quiet_hours,
    next_time,
    parse_moment,
    quiet_span,
)

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.tools.permissions import Decision

Kind = Literal["once", "daily", "weekdays", "weekly", "cron", "startup", "idle_return", "condition"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Condition = Literal[
    "calendar_soon", "ticktick_due_soon", "disk_low", "garmin_heart_rate_high", "garmin_body_battery_low"
]
Example = Literal["morning_briefing", "downloads_cleanup", "low_disk_warning", "meeting_heads_up"]

# Ready-made jobs: ``example`` fills in whatever the call leaves out.
EXAMPLES: dict[str, dict[str, Any]] = {
    "morning_briefing": {
        "name": "Morning briefing",
        "kind": "daily",
        "at": "08:00",
        "instruction": (
            "Give me a short morning briefing: what's on my calendar today, my TickTick tasks due today and anything "
            "overdue, and today's weather. Keep it under half a minute."
        ),
    },
    "downloads_cleanup": {
        "name": "Downloads cleanup",
        "kind": "weekly",
        "days": ["sun"],
        "at": "18:00",
        "instruction": (
            "Look through my Downloads folder and suggest what I could clean up: old installers, duplicates and big "
            "files I probably don't need anymore. Only suggest; don't delete or move anything unless I say so."
        ),
    },
    "low_disk_warning": {
        "name": "Low disk warning",
        "kind": "condition",
        "condition": "disk_low",
        "threshold": 10.0,
        "every_min": 60.0,
        "instruction": "Tell me the drive is running low on space, and offer to look for big files I could remove.",
    },
    "meeting_heads_up": {
        "name": "Meeting heads-up",
        "kind": "condition",
        "condition": "calendar_soon",
        "threshold": 10.0,
        "every_min": 5.0,
        "instruction": "Give me a quick heads-up about the calendar event that's about to start, and where it is.",
    },
}

# Argument name -> the job's field of that name, where they differ.
_FIELD = {"when": "kind", "check_every_min": "every_min"}
_TIMING = {"kind", "at", "days", "cron", "condition", "threshold", "drive", "every_min", "idle_min"}
# What an edit writes back: everything it may have changed, and nothing a run may be updating meanwhile.
_EDITABLE = _TIMING | {"name", "instruction", "delivery", "catch_up_hours", "next_run", "seen"}


class JobFields(ToolArgs):
    name: str | None = Field(default=None, max_length=80, description="A short name.")
    instruction: str | None = Field(
        default=None,
        max_length=2000,
        description=("A request to yourself, e.g. 'Check the weather and tell me if I need an umbrella'."),
    )
    when: Kind | None = Field(
        default=None,
        description=(
            "weekly: on 'days'; idle_return: user back after 'idle_min' away; condition: checked every "
            "'check_every_min' min."
        ),
    )
    at: str | None = Field(
        default=None,
        description="'HH:MM' (24h) for daily/weekdays/weekly; 'YYYY-MM-DD HH:MM' or 'HH:MM' for once.",
    )
    days: list[Weekday] | None = None
    cron: str | None = Field(default=None, description="'min hour day month weekday', e.g. '0 9 * * 1-5'.")
    condition: Condition | None = Field(
        default=None,
        description=(
            "calendar_soon / ticktick_due_soon: within 'threshold' min (default 15 / 30); disk_low: under "
            "'threshold' GB free on 'drive' (default 10); garmin_heart_rate_high: Garmin watch above 'threshold' "
            "bpm (default 120); garmin_body_battery_low: Body Battery at or under 'threshold' (default 20)."
        ),
    )
    threshold: float | None = Field(default=None, ge=0)
    drive: str | None = Field(default=None, max_length=10, description="Default C:.")
    check_every_min: float | None = Field(default=None, ge=1, le=1440, description="Default 5.")
    idle_min: float | None = Field(default=None, ge=1, le=1440, description="Default 30.")
    delivery: Literal["speak", "notify"] | None = Field(default=None, description="Default speak.")
    catch_up_hours: float | None = Field(
        default=None,
        ge=0,
        le=168,
        description="Run a time missed while off if at most this late. Default 12.",
    )


class CreateJobArgs(JobFields):
    example: Example | None = Field(
        default=None,
        description=("A ready-made job (downloads_cleanup only suggests); other fields override it."),
    )


class JobRef(ToolArgs):
    job: str = Field(description="Number or name.")


class EditJobArgs(JobFields):
    job: str = Field(description="Number or name.")


class PauseJobArgs(JobRef):
    paused: bool = Field(default=True, description="false resumes.")


class HistoryArgs(ToolArgs):
    job: str | None = Field(default=None, description="Number or name; omit for all.")
    limit: int = Field(default=10, ge=1, le=50)


def _store(ctx: ToolContext) -> JobStore:
    return JobStore(ctx.config.resolve_path(JOBS_FILE))


def _changes(args: JobFields) -> dict[str, Any]:
    given = args.model_dump(exclude_none=True, include=set(JobFields.model_fields))
    return {_FIELD.get(key, key): value for key, value in given.items()}


def build(job: Job, changes: dict[str, Any], now: datetime) -> Job:
    """``job`` with ``changes``, checked, with its next run worked out. Raises ValueError for the user."""
    job = replace(job, **changes)
    if job.kind == "once" and job.at:
        job = replace(job, at=_iso(parse_moment(job.at, now)))
    if job.drive:
        job = replace(job, drive=job.drive.upper())
    job.validate(now)
    if _TIMING & changes.keys():
        upcoming = next_time(job, now) if job.kind in TIMED else None
        job = replace(job, next_run=_iso(upcoming) if upcoming else None, seen=[])
    return job


def _setup_problem(job: Job) -> str | None:
    """Why a condition can't be checked yet (what it needs isn't connected), or None."""
    if job.kind != "condition":
        return None
    if job.condition == "calendar_soon":
        from octavia.tools.extensions.calendar_feed import calendar_urls, setup_hint

        return None if calendar_urls() else setup_hint()
    if job.condition == "ticktick_due_soon":
        from octavia.tools.extensions import ticktick

        return None if ticktick.shared_client().configured else "TickTick isn't connected, so there's nothing to check."
    if job.condition.startswith("garmin_"):
        from octavia.tools.extensions import _garmin

        return None if _garmin.shared_client().connected else _garmin.NOT_CONNECTED
    return None


def _outlook(job: Job, config: AppConfig) -> str:
    """What to know about when it'll run: its next time, and quiet hours or a scheduler that's off."""
    notes = []
    upcoming = datetime.fromisoformat(job.next_run) if job.next_run else None
    if upcoming is not None:
        notes.append(f"Next run: {day(upcoming)} at {clock(upcoming)}.")
        span = quiet_span(config.scheduler.quiet_hours)
        if span and in_quiet_hours(upcoming, config.scheduler.quiet_hours):
            notes.append(
                f"That's in quiet hours ({config.scheduler.quiet_hours}), so it'll wait until {clock(span[1])}."
            )
    if not config.scheduler.enabled:
        notes.append("Scheduled jobs are turned off in the config (scheduler.enabled), so it won't run until on.")
    return " ".join(notes)


class _JobTool(Tool):
    category = "scheduler"

    def __init__(self, clock_now: Callable[[], datetime] = _now) -> None:
        self._now = clock_now


class CreateScheduledJob(_JobTool):
    name = "create_scheduled_job"
    description = (
        "Schedule something to do on your own later (at times, at startup, when the user returns, or when a "
        "condition turns true); it runs through you, risky steps asked first. E.g. 'every morning give me a "
        "briefing', 'remind me at 5 to ...', 'tell me when my disk is nearly full'. For a plain task or reminder, "
        "use TickTick."
    )
    Args = CreateJobArgs
    risk = RiskLevel.CONFIRM

    def _build(self, args: CreateJobArgs) -> Job:
        given = {**EXAMPLES.get(args.example or "", {}), **_changes(args)}
        needed = {"name": "a name", "instruction": "an instruction", "kind": "when it runs"}
        missing = [label for key, label in needed.items() if not given.get(key)]
        if missing:
            raise ValueError(f"The job needs {' and '.join(missing)} (or an example to start from).")
        now = self._now()
        draft = Job(id=0, name=given["name"], instruction=given["instruction"], kind=given["kind"], created=_iso(now))
        return build(draft, given, now)

    async def precheck(self, args: CreateJobArgs, ctx: ToolContext) -> str | None:
        try:
            return _setup_problem(self._build(args))
        except ValueError as exc:
            return str(exc)

    async def describe_call(self, args: CreateJobArgs, ctx: ToolContext) -> str:
        try:
            job = self._build(args)
        except ValueError:
            return "schedule a job"
        return f"schedule “{job.name}” {job.describe_when()}"

    async def run(self, args: CreateJobArgs, ctx: ToolContext) -> ToolResult:
        try:
            job = self._build(args)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        job = _store(ctx).add(job)
        text = f"Scheduled #{job.id} “{job.name}”, {job.describe_when()}. {_outlook(job, ctx.config)}"
        return ToolResult.success(text.strip(), data={"id": job.id})


class ListScheduledJobs(_JobTool):
    name = "list_scheduled_jobs"
    description = "List your scheduled jobs with their numbers. Use before changing, pausing or deleting one."
    risk = RiskLevel.SAFE

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        jobs = _store(ctx).load()
        settings = ctx.config.scheduler
        if not jobs:
            return ToolResult.success("No scheduled jobs yet.")
        lines = [job.summary() for job in jobs]
        if settings.quiet_hours:
            lines.append(f"(Quiet hours: {settings.quiet_hours}; jobs due then wait until they're over.)")
        if not settings.enabled:
            lines.append("(Scheduled jobs are turned off in the config: scheduler.enabled.)")
        return ToolResult.success("\n".join(lines), data={"count": len(jobs)})


class EditScheduledJob(_JobTool):
    name = "edit_scheduled_job"
    description = "Change a scheduled job; only the fields given change."
    Args = EditJobArgs
    risk = RiskLevel.CONFIRM

    def _edit(self, args: EditJobArgs, ctx: ToolContext) -> tuple[Job, Job]:
        job = find_job(_store(ctx).load(), args.job)
        if job is None:
            raise ValueError(f"There's no scheduled job “{args.job}”; list_scheduled_jobs shows them.")
        changes = _changes(args)
        if not changes:
            raise ValueError("Nothing to change was given.")
        return job, build(job, changes, self._now())

    async def precheck(self, args: EditJobArgs, ctx: ToolContext) -> str | None:
        try:
            return _setup_problem(self._edit(args, ctx)[1])
        except ValueError as exc:
            return str(exc)

    async def describe_call(self, args: EditJobArgs, ctx: ToolContext) -> str:
        try:
            old, new = self._edit(args, ctx)
        except ValueError:
            return "change a scheduled job"
        parts = []
        if new.name != old.name:
            parts.append(f"call it “{new.name}”")
        if _TIMING & _changes(args).keys():
            parts.append(f"run it {new.describe_when()}")
        if new.instruction != old.instruction:
            parts.append(f"have it do this: {new.instruction}")
        if new.delivery != old.delivery:
            parts.append("speak the result" if new.delivery == "speak" else "show the result as a notification")
        return f"change the scheduled job “{old.name}”" + (": " + ", and ".join(parts) if parts else "")

    async def run(self, args: EditJobArgs, ctx: ToolContext) -> ToolResult:
        try:
            _, new = self._edit(args, ctx)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        saved = _store(ctx).update(new.id, **{name: getattr(new, name) for name in _EDITABLE})
        if saved is None:
            return ToolResult.error("That job was deleted in the meantime.")
        text = f"Changed #{saved.id} “{saved.name}”: it runs {saved.describe_when()}. {_outlook(saved, ctx.config)}"
        return ToolResult.success(text.strip())


class PauseScheduledJob(_JobTool):
    name = "pause_scheduled_job"
    description = "Pause or resume a scheduled job."
    Args = PauseJobArgs
    risk = RiskLevel.SAFE

    async def run(self, args: PauseJobArgs, ctx: ToolContext) -> ToolResult:
        store = _store(ctx)
        job = find_job(store.load(), args.job)
        if job is None:
            return ToolResult.error(f"There's no scheduled job “{args.job}”; list_scheduled_jobs shows them.")
        changes: dict[str, Any] = {"paused": args.paused}
        if not args.paused and job.kind in TIMED:
            # Times that passed while it was paused aren't caught up: it goes on from now.
            upcoming = next_time(job, self._now())
            if upcoming is None:
                return ToolResult.error(f"“{job.name}” was a one-time job whose time has passed; create a new one.")
            changes["next_run"] = _iso(upcoming)
        saved = store.update(job.id, **changes)
        if saved is None:
            return ToolResult.error("That job was deleted in the meantime.")
        if args.paused:
            return ToolResult.success(f"Paused #{saved.id} “{saved.name}”.")
        return ToolResult.success(f"Resumed #{saved.id} “{saved.name}”. {_outlook(saved, ctx.config)}".strip())


class DeleteScheduledJob(_JobTool):
    name = "delete_scheduled_job"
    description = "Delete a scheduled job for good (pause_scheduled_job only stops it for a while)."
    Args = JobRef
    risk = RiskLevel.CONFIRM

    async def precheck(self, args: JobRef, ctx: ToolContext) -> str | None:
        if find_job(_store(ctx).load(), args.job) is None:
            return f"There's no scheduled job “{args.job}”; list_scheduled_jobs shows them."
        return None

    async def describe_call(self, args: JobRef, ctx: ToolContext) -> str:
        job = find_job(_store(ctx).load(), args.job)
        return f"delete the scheduled job “{job.name}”" if job else "delete a scheduled job"

    async def run(self, args: JobRef, ctx: ToolContext) -> ToolResult:
        store = _store(ctx)
        job = find_job(store.load(), args.job)
        if job is None or store.remove(job.id) is None:
            return ToolResult.error(f"There's no scheduled job “{args.job}”.")
        return ToolResult.success(f"Deleted #{job.id} “{job.name}”.")


class ScheduledJobHistory(_JobTool):
    name = "scheduled_job_history"
    description = "What your scheduled jobs did when they ran, including runs missed while the computer was off."
    Args = HistoryArgs
    risk = RiskLevel.SAFE

    async def run(self, args: HistoryArgs, ctx: ToolContext) -> ToolResult:
        store = _store(ctx)
        job_id = None
        if args.job:
            job = find_job(store.load(), args.job)
            if job is None and not args.job.lstrip("#").isdigit():
                return ToolResult.error(f"There's no scheduled job “{args.job}”.")
            job_id = job.id if job else int(args.job.lstrip("#"))
        runs = store.runs(args.limit, job_id)
        if not runs:
            return ToolResult.success("No runs yet.")
        return ToolResult.success("\n".join(run.summary() for run in runs), data={"count": len(runs)})


def permission_rules(config: AppConfig) -> dict[str, Decision]:
    """Creating and changing jobs follow ``scheduler.changes``; a rule in ``permissions.tools`` still wins."""
    return {CreateScheduledJob.name: config.scheduler.changes, EditScheduledJob.name: config.scheduler.changes}


TOOLS = [
    CreateScheduledJob,
    ListScheduledJobs,
    EditScheduledJob,
    PauseScheduledJob,
    DeleteScheduledJob,
    ScheduledJobHistory,
]
