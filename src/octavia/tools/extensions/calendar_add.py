"""Calendar: add an event by opening the calendar's own "new event" page with everything filled in.

The connected calendars are private iCal feeds (``calendar_feed``), which can only be read. Writing through the
calendar APIs needs an OAuth sign-in: a Google Calendar connected that way (``google_calendar``) saves events
directly. Without one, both Google and Microsoft accept a link that opens their event editor already filled in, so
adding an event needs no key at all. The event is saved when the user clicks Save, which also lets them check the
details and pick which calendar it goes in.

Times are sent in UTC, so the event lands at the time meant on this computer whatever time zone the calendar
account is set to.
"""

from __future__ import annotations

import asyncio
import webbrowser
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode, urlsplit

from pydantic import Field

from octavia.core.text import clock, day
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.calendar_feed import calendar_urls

Service = Literal["google", "outlook", "outlook_work"]

SERVICE_NAMES: dict[str, str] = {"google": "Google Calendar", "outlook": "Outlook", "outlook_work": "Outlook"}
_OUTLOOK_COMPOSE = {
    "outlook": "https://outlook.live.com/calendar/0/deeplink/compose",
    "outlook_work": "https://outlook.office.com/calendar/0/deeplink/compose",
}
# Feed hosts, matched by their end, that say which service the user's calendar lives in.
_FEED_SERVICES: dict[str, Service] = {
    "calendar.google.com": "google",
    "outlook.live.com": "outlook",
    "outlook.office365.com": "outlook_work",
    "outlook.office.com": "outlook_work",
}


def _feed_service(url: str) -> Service | None:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    return next((s for suffix, s in _FEED_SERVICES.items() if host == suffix or host.endswith("." + suffix)), None)


def default_service() -> Service:
    """The service of the first connected calendar it recognises; Google Calendar when none is connected."""
    for url in calendar_urls():
        if service := _feed_service(url):
            return service
    return "google"


def _aware(value: datetime) -> datetime:
    """A time without a zone is this computer's local time."""
    return value if value.tzinfo else value.astimezone()


def _utc(value: datetime) -> datetime:
    return _aware(value).astimezone(UTC)


def _stamp(value: date, date_format: str, time_format: str) -> str:
    """A date as itself (all-day events), a time in UTC."""
    return f"{_utc(value):{time_format}}" if isinstance(value, datetime) else f"{value:{date_format}}"


def google_link(title: str, start: date, end: date, location: str = "", details: str = "") -> str:
    """Google's event template. ``start`` and ``end`` are dates for an all-day event (the end day excluded)."""
    dates = f"{_stamp(start, '%Y%m%d', '%Y%m%dT%H%M%SZ')}/{_stamp(end, '%Y%m%d', '%Y%m%dT%H%M%SZ')}"
    query = {"action": "TEMPLATE", "text": title, "dates": dates}
    query.update({k: v for k, v in (("location", location), ("details", details)) if v})
    return f"https://calendar.google.com/calendar/render?{urlencode(query)}"


def outlook_link(service: Service, title: str, start: date, end: date, location: str = "", details: str = "") -> str:
    """Outlook on the web's compose deeplink; ``outlook_work`` is the Microsoft 365 (work or school) one."""
    query = {
        "path": "/calendar/action/compose",
        "rru": "addevent",
        "subject": title,
        "startdt": _stamp(start, "%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"),
        "enddt": _stamp(end, "%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"),
    }
    if not isinstance(start, datetime):
        query["allday"] = "true"
    query.update({k: v for k, v in (("location", location), ("body", details)) if v})
    return f"{_OUTLOOK_COMPOSE[service]}?{urlencode(query)}"


def say_span(start: date, end: date) -> str:
    """How an event's span is said; ``start`` and ``end`` are dates for an all-day event (the end day excluded)."""
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        last = end - timedelta(days=1)
        return f"{day(start)}, all day" if last <= start else f"{day(start)} through {day(last)}, all day"
    begins, ends = _aware(start).astimezone(), _aware(end).astimezone()
    until = clock(ends) if ends.date() == begins.date() else f"{day(ends)} {clock(ends)}"
    return f"{day(begins)}, {clock(begins)} to {until}"


def event_span(start: datetime, end: datetime | None, duration_minutes: int, all_day: bool) -> tuple[date, date, str]:
    """An event's start and end, as dates for an all-day event (its end day excluded) and local times otherwise, and
    how they're said. Raises ValueError for an event that ends before it starts."""
    if all_day:
        first = start.date()
        last = end.date() if end else first
        if last < first:
            raise ValueError("The event's last day is before its first day.")
        return first, last + timedelta(days=1), say_span(first, last + timedelta(days=1))
    begins = _aware(start).astimezone()
    ends = _aware(end).astimezone() if end else begins + timedelta(minutes=duration_minutes)
    if ends <= begins:
        raise ValueError("The event has to end after it starts.")
    return begins, ends, say_span(begins, ends)


async def _open_in_browser(url: str) -> bool:
    return await asyncio.to_thread(webbrowser.open, url, 2)


class EventArgs(ToolArgs):
    """A new event's fields, shared with ``google_calendar``'s direct one."""

    title: str = Field(min_length=1, max_length=200)
    start: datetime = Field(description="Local time, YYYY-MM-DDTHH:MM (all-day: just the day).")
    end: datetime | None = Field(
        default=None,
        description="Omit to use duration_minutes. All-day: its last day.",
    )
    duration_minutes: int = Field(default=60, ge=1, le=24 * 60)
    all_day: bool = Field(default=False)
    location: str = Field(default="", max_length=300)
    details: str = Field(default="", max_length=2000)


class AddCalendarEventArgs(EventArgs):
    service: Service | None = Field(
        default=None,
        description=("outlook_work = Microsoft 365. Omit for the connected one."),
    )


class AddCalendarEvent(Tool):
    name = "add_calendar_event"
    description = (
        "Open a filled-in Google Calendar or Outlook new-event page for the user to Save. Use for Outlook, or when "
        "Google Calendar isn't connected (else create_google_calendar_event). Work out the date first."
    )
    Args = AddCalendarEventArgs
    risk = RiskLevel.SAFE  # only opens a filled-in form; nothing is saved until the user clicks Save
    category = "calendar"

    async def run(self, args: AddCalendarEventArgs, ctx: ToolContext) -> ToolResult:
        service = args.service or default_service()
        try:
            start, end, when = event_span(args.start, args.end, args.duration_minutes, args.all_day)
        except ValueError as exc:
            return ToolResult.error(str(exc))

        if service == "google":
            url = google_link(args.title, start, end, args.location, args.details)
        else:
            url = outlook_link(service, args.title, start, end, args.location, args.details)
        name = SERVICE_NAMES[service]
        if not await _open_in_browser(url):
            return ToolResult.error(f"No web browser responded, so {name} couldn't be opened.")
        return ToolResult.success(
            f"Opened {name} in the browser with '{args.title}' filled in for {when}. It's added once the user "
            "checks it and clicks Save.",
            data={"service": service, "when": when},
        )


TOOLS = [AddCalendarEvent]
