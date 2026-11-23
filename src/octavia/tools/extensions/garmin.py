"""Garmin: the user's watch data from Garmin Connect, read-only, as short spoken summaries.

Connecting opens a small window where the user types their Garmin Connect email and password (see ``_garmin`` for
why it's the Connect account and not an official API, and how the sign-in is kept); a code Garmin emails for two-step
verification is passed on with ``enter_garmin_code``. After that she reads heart rate, HRV, stress, Body Battery,
sleep, steps, blood oxygen and recent activities for any day. Watching in the background ("tell me when my heart
rate is over 120", "when my Body Battery is low") is a scheduled job with a Garmin condition (``_scheduler``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Literal

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._garmin import (
    NOT_CONNECTED,
    GarminClient,
    GarminError,
    parse_day,
    say_activity,
    say_body_battery,
    say_heart_rate,
    say_hrv,
    say_latest_heart_rate,
    say_sleep,
    say_spo2,
    say_steps,
    say_stress,
    shared_client,
)

Metric = Literal["overview", "heart_rate", "hrv", "stress", "body_battery", "sleep", "steps", "spo2"]
AskSignIn = Callable[[str], Awaitable[tuple[str, str] | None]]
SERVICE = "Garmin Connect"
NOT_SYNCED = "Garmin has no {what} for {day} yet (the watch may not have synced, or doesn't measure it)."
WATCHING = " To watch in the background, schedule a condition job (garmin_heart_rate_high or garmin_body_battery_low)."


async def _ask_in_window(service: str) -> tuple[str, str] | None:
    from octavia.platform.sign_in_window import ask_sign_in

    return await ask_sign_in(service)


def _day_words(day: date, today: date) -> str:
    if day == today:
        return "today"
    if (today - day).days == 1:
        return "yesterday"
    return f"{day:%A, %B} {day.day}"


class _GarminTool(Tool):
    category = "health"

    def __init__(self, client: GarminClient | None = None, today: Callable[[], date] = date.today) -> None:
        self._client = client
        self._today = today

    @property
    def client(self) -> GarminClient:
        return self._client if self._client is not None else shared_client()


class ConnectGarmin(_GarminTool):
    name = "connect_garmin"
    description = (
        "Connect the user's Garmin watch data (Garmin Connect): opens a window where they type their email and "
        "password themselves. Never ask for the password aloud."
    )
    risk = RiskLevel.CONFIRM
    timeout_s = 360.0

    def __init__(
        self,
        client: GarminClient | None = None,
        today: Callable[[], date] = date.today,
        ask: AskSignIn = _ask_in_window,
    ) -> None:
        super().__init__(client, today)
        self._ask = ask

    async def describe_call(self, args: ToolArgs, ctx: ToolContext) -> str:
        return "open a Garmin sign-in window to connect your Garmin account, so I can read your watch's health data"

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        given = await self._ask(SERVICE)
        if given is None:
            return ToolResult.error("The sign-in window was closed without signing in, so Garmin isn't connected.")
        email, password = given
        try:
            done = await self.client.auth.start(email, password)
        except GarminError as exc:
            return ToolResult.error(str(exc))
        if not done:
            return ToolResult.success(
                "Garmin sent the user a verification code (by email or text). Ask them for it, then call "
                "enter_garmin_code. It's good for 10 minutes."
            )
        return ToolResult.success("Connected Garmin; their watch data can be read now." + WATCHING)


class EnterGarminCodeArgs(ToolArgs):
    code: str = Field(pattern=r"^\s*\d[\d ]{3,11}\s*$", description="The verification code Garmin sent.")


class EnterGarminCode(_GarminTool):
    name = "enter_garmin_code"
    description = "Finish connecting Garmin with the verification code Garmin sent, after connect_garmin asked for it."
    Args = EnterGarminCodeArgs
    risk = RiskLevel.SAFE  # finishes the sign-in the user agreed to and typed their password for
    private_args = frozenset({"code"})

    async def run(self, args: EnterGarminCodeArgs, ctx: ToolContext) -> ToolResult:
        try:
            await self.client.auth.finish(args.code.replace(" ", ""))
        except GarminError as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success("Connected Garmin; their watch data can be read now." + WATCHING)


class DisconnectGarmin(_GarminTool):
    name = "disconnect_garmin"
    description = "Forget the saved Garmin sign-in; reading Garmin data needs connect_garmin again afterwards."
    risk = RiskLevel.CONFIRM

    async def describe_call(self, args: ToolArgs, ctx: ToolContext) -> str:
        return "forget your Garmin sign-in"

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        if self.client.auth.disconnect():
            return ToolResult.success("Forgot the Garmin sign-in.")
        return ToolResult.success("Garmin wasn't connected.")


class GarminVitalsArgs(ToolArgs):
    metric: Metric = Field(
        default="overview",
        description=(
            "overview: steps, the latest heart rate (today), resting heart rate, Body Battery, stress and sleep. "
            "heart_rate: the latest reading, resting and range; for their heart rate now, give the latest reading, "
            "never the resting one. sleep for a date is the night ending that morning."
        ),
    )
    date: str | None = Field(default=None, description="YYYY-MM-DD, 'today' or 'yesterday'. Default today.")


class GarminVitals(_GarminTool):
    name = "garmin_vitals"
    description = (
        "The user's health data from their Garmin watch: heart rate, HRV, stress, Body Battery, sleep, steps, "
        "blood oxygen, or an overview, for today or a date. Answer in a sentence or two."
    )
    Args = GarminVitalsArgs
    risk = RiskLevel.SAFE

    async def precheck(self, args: GarminVitalsArgs, ctx: ToolContext) -> str | None:
        return None if self.client.connected else NOT_CONNECTED

    async def run(self, args: GarminVitalsArgs, ctx: ToolContext) -> ToolResult:
        today = self._today()
        try:
            day = parse_day(args.date, today)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        when = _day_words(day, today)
        try:
            text = await self._read(args.metric, day)
        except GarminError as exc:
            return ToolResult.error(str(exc))
        if not text:
            what = "data" if args.metric == "overview" else args.metric.replace("_", " ")
            return ToolResult.success(NOT_SYNCED.format(what=what, day=when))
        return ToolResult.success(f"Garmin, {when}: {text}", data={"metric": args.metric, "date": day.isoformat()})

    async def _read(self, metric: str, day: date) -> str:
        client = self.client
        if metric == "heart_rate":
            return say_heart_rate(await client.heart_rate(day))
        if metric == "hrv":
            return say_hrv(await client.hrv(day))
        if metric == "stress":
            return say_stress(await client.stress(day))
        if metric == "sleep":
            return say_sleep(await client.sleep(day))
        if metric == "spo2":
            return say_spo2(await client.spo2(day))
        summary = await client.summary(day)
        if metric == "body_battery":
            return say_body_battery(summary)
        if metric == "steps":
            return say_steps(summary)
        parts = [say_steps(summary)]
        if day == self._today():  # "check my vitals" today means now: the latest reading leads, not the resting one
            try:
                parts.append(say_latest_heart_rate(await client.heart_rate(day)))
            except GarminError:
                pass  # the overview still says the rest
        if resting := summary.get("restingHeartRate"):
            parts.append(f"Resting heart rate {resting} (a daily baseline, not a current reading).")
        parts.append(say_body_battery(summary))
        parts.append(say_stress({"avgStressLevel": summary.get("averageStressLevel")}))
        parts.append(say_sleep(await client.sleep(day)))
        return " ".join(p for p in parts if p)


class GarminActivitiesArgs(ToolArgs):
    limit: int = Field(default=3, ge=1, le=10)
    date: str | None = Field(default=None, description="Only that day: YYYY-MM-DD, 'today' or 'yesterday'.")


class GarminActivities(_GarminTool):
    name = "garmin_activities"
    description = "The user's recent workouts recorded on their Garmin watch (runs, rides, walks...), newest first."
    Args = GarminActivitiesArgs
    risk = RiskLevel.SAFE

    async def precheck(self, args: GarminActivitiesArgs, ctx: ToolContext) -> str | None:
        return None if self.client.connected else NOT_CONNECTED

    async def run(self, args: GarminActivitiesArgs, ctx: ToolContext) -> ToolResult:
        today = self._today()
        try:
            day = parse_day(args.date, today) if args.date else None
            found = await self.client.activities(args.limit, day)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except GarminError as exc:
            return ToolResult.error(str(exc))
        if not found:
            where = f" on {_day_words(day, today)}" if day else ""
            return ToolResult.success(f"No activities recorded on Garmin{where}.")
        return ToolResult.success("\n".join(say_activity(a) for a in found), data={"count": len(found)})


TOOLS = [ConnectGarmin, EnterGarminCode, DisconnectGarmin, GarminVitals, GarminActivities]
