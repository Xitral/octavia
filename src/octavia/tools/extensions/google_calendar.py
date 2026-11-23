"""Google Calendar through Google's API: add, change and delete the user's events directly, each after a yes.

``add_calendar_event`` only opens Google's filled-in "new event" page, which the user still has to save, and the iCal
feed (``calendar_feed``) can only be read. Writing needs an OAuth sign-in with the ``calendar.events`` scope (events
only: no calendar settings or sharing). It is Gmail's sign-in (``gmail_api``): the same OAuth client file in
``data/gmail/``, the same browser flow and token refresh, with the tokens kept apart from Gmail's and Google Chat's.

Events go in each account's primary calendar. To change or delete one, the event is found by words of its title
(and its day, when known) before the user is asked, so the question names exactly the event that will change, and
the very event the user said yes to is the one changed. Several matches are listed instead of guessed between. A
repeating event is changed or deleted only on the one day found, never the whole series.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import Field, model_validator
from rapidfuzz import fuzz

from octavia.core.text import day as _day
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.calendar_add import EventArgs, event_span, say_span
from octavia.tools.extensions.gmail_api import (
    CLIENT_DIR,
    ConnectGmailAccount,
    ConnectGmailArgs,
    GmailApi,
    Gone,
    GoogleAuth,
    GoogleError,
    NotConnected,
    _http,
    load_client,
)
from octavia.tools.extensions.google_chat import _identity

log = logging.getLogger(__name__)

CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary"
EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
FULL_SCOPE = "https://www.googleapis.com/auth/calendar"
SCOPES = f"openid email {EVENTS_SCOPE}"
STATE_NAMESPACE = "google_calendar"
SEARCH_DAYS = 60  # how far ahead an event is looked for when its day isn't given
MAX_LISTED = 50
MAX_CHOICES = 8
MATCH_SCORE = 80
REMEMBER_S = 600.0  # how long the event found before the question is the one acted on

NOT_CONNECTED = (
    "Google Calendar isn't connected for adding or changing events: use connect_google_calendar so the user can sign "
    "in once in the browser. Until then add_calendar_event can open a filled-in event page instead."
)
BLOCKED = (
    "If Google said 'Access blocked', 'admin_policy_enforced' or 'This app is blocked', the account's administrator "
    "(a school's IT) doesn't let apps it hasn't approved change its calendar, so it can't be connected this way; "
    "add_calendar_event still works for it."
)
API_OFF = (
    "the Google Calendar API isn't turned on in the user's Google Cloud project: turn it on at "
    "console.cloud.google.com/apis/library/calendar-json.googleapis.com, then try again"
)


def setup_hint(folder: Path = CLIENT_DIR) -> str:
    """How to make the Google OAuth client that Gmail search, Google Chat and Google Calendar share, once."""
    return (
        "Changing Google Calendar directly isn't set up yet; it needs a Google OAuth client, made once (the same one "
        "Gmail search uses). The user goes to console.cloud.google.com, creates a project, turns on the Google "
        "Calendar API, and sets up the OAuth consent screen (External; add every address to connect as a test user, "
        "or publish the app). Then under Credentials they create an OAuth client ID of type 'Desktop app', download "
        f"its JSON and save it in {folder} keeping its client_secret....json name. Then they ask me to connect Google "
        "Calendar. Until then add_calendar_event opens a filled-in event page to save."
    )


class CalendarAuth(GoogleAuth):
    """Google Calendar's sign-ins, kept next to Gmail's in the tool state store but never mixed with them."""

    namespace = STATE_NAMESPACE

    async def exchange(self, code: str, verifier: str, redirect_uri: str) -> str:
        answer = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )
        granted = str(answer.get("scope", "")).split()
        if EVENTS_SCOPE not in granted and FULL_SCOPE not in granted:
            raise GoogleError(
                "the permission to change calendar events wasn't given; connect again and tick the Google Calendar "
                "permission"
            )
        if not answer.get("refresh_token"):
            raise GoogleError("Google didn't send a lasting sign-in; connect again")
        address, _ = _identity(answer)
        if not address:
            raise GoogleError("Google didn't say which account signed in")
        await self._save(address, answer)
        return address


def calendar_problem(response: httpx.Response) -> GoogleError:
    status, text = response.status_code, response.text
    if status == 403 and ("accessNotConfigured" in text or "SERVICE_DISABLED" in text):
        return GoogleError(API_OFF)
    if status == 403 and ("insufficientPermissions" in text or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in text):
        return NotConnected("the sign-in doesn't include permission to change calendar events")
    if status == 403:
        return GoogleError(
            "Google refused (HTTP 403); the event may belong to someone else's calendar, or a school or work "
            "account's admin may not let apps change it"
        )
    if status in (404, 410):
        return Gone("Google Calendar has no such event any more (it may have been deleted already)")
    if status == 429:
        return GoogleError("Google says there were too many requests; try again in a minute")
    return GoogleError(f"Google Calendar answered HTTP {status}")


class CalendarApi(GmailApi):
    base_url = CALENDAR_URL
    service = "Google Calendar"

    def problem(self, response: httpx.Response) -> GoogleError:
        return calendar_problem(response)


# --------------------------------------------------------------------------- events


@dataclass(frozen=True)
class FoundEvent:
    """An event in a connected calendar. ``start`` and ``end`` are dates for an all-day event (the end day excluded),
    else aware times."""

    account: str
    id: str
    title: str
    start: date
    end: date
    repeats: bool = False

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    @property
    def when(self) -> str:
        return say_span(self.start, self.end)

    @property
    def named(self) -> str:
        return f"“{self.title}” ({self.when})"


def _parse_time(value: Any) -> date | None:
    if not isinstance(value, dict):
        return None
    try:
        if value.get("dateTime"):
            return datetime.fromisoformat(str(value["dateTime"])).astimezone()
        if value.get("date"):
            return date.fromisoformat(str(value["date"]))
    except ValueError:
        return None
    return None


def parse_event(account: str, item: Any) -> FoundEvent | None:
    """A FoundEvent from an event resource; None for a cancelled one or one that can't be read."""
    if not isinstance(item, dict) or not item.get("id") or item.get("status") == "cancelled":
        return None
    start, end = _parse_time(item.get("start")), _parse_time(item.get("end"))
    if start is None or end is None or isinstance(start, datetime) != isinstance(end, datetime):
        return None
    title = " ".join(str(item.get("summary") or "").split()) or "(untitled)"
    return FoundEvent(account, str(item["id"]), title, start, end, bool(item.get("recurringEventId")))


def _when_body(value: date) -> dict[str, str]:
    if isinstance(value, datetime):
        return {"dateTime": value.isoformat(timespec="seconds")}
    return {"date": value.isoformat()}


def _local_midnight(value: date) -> datetime:
    return datetime.combine(value, clock_time()).astimezone()


def _title_matches(wanted: str, title: str) -> bool:
    return fuzz.partial_ratio(wanted.casefold(), title.casefold()) >= MATCH_SCORE


# --------------------------------------------------------------------------- tools


class ConnectGoogleCalendar(ConnectGmailAccount):
    name = "connect_google_calendar"
    description = (
        "Browser sign-in to the user's Google Calendar for the *_google_calendar_event tools (separate from "
        "connect_gmail_account)."
    )
    Args = ConnectGmailArgs
    category = "calendar"
    service = "Google Calendar"
    scope = SCOPES
    auth_class = CalendarAuth
    blocked = BLOCKED

    def setup_hint(self) -> str:
        return setup_hint(self._client_dir)

    def connected(self, address: str, accounts: list[str]) -> str:
        return (
            f"Connected Google Calendar for {address}; events can be added, changed and deleted there directly now, "
            f"each after the user says yes. Connected accounts: {', '.join(accounts)}."
        )

    async def describe_call(self, args: ConnectGmailArgs, ctx: ToolContext) -> str:
        which = f" for {args.account}" if args.account else ""
        return f"open Google's sign-in page in the browser to connect Google Calendar{which}, to add and change events"


class _CalendarTool(Tool):
    """What the three event tools share: the sign-in, choosing the account, and reaching the API."""

    category = "calendar"
    timeout_s = 30.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, client_dir: Path | None = None) -> None:
        self._transport = transport
        self._client_dir = client_dir if client_dir is not None else CLIENT_DIR

    def _auth(self, ctx: ToolContext) -> CalendarAuth | str:
        client = load_client(self._client_dir)
        if client is None:
            return setup_hint(self._client_dir)
        return CalendarAuth(ctx, client, self._transport)

    async def _chosen(self, auth: CalendarAuth, account: str | None) -> tuple[list[str], str | None]:
        """The connected accounts ``account`` names (all of them when it's None), or why there are none."""
        accounts = sorted(await auth.accounts())
        if not accounts:
            return [], NOT_CONNECTED
        if not account:
            return accounts, None
        wanted = account.lower()
        chosen = [a for a in accounts if a == wanted] or [a for a in accounts if wanted in a]
        if not chosen:
            return [], (
                f"No account with Google Calendar connected matches {account!r}; connected: {', '.join(accounts)}. "
                "Use connect_google_calendar to add it."
            )
        return chosen, None


def _failure(address: str, exc: GoogleError) -> str:
    if isinstance(exc, NotConnected):
        return f"{address}: {exc}; use connect_google_calendar to connect it again"
    return f"{address}: {exc}"


class CreateEventArgs(EventArgs):
    account: str | None = Field(
        default=None,
        description="Address or part, if several.",
    )


class CreateGoogleCalendarEvent(_CalendarTool):
    name = "create_google_calendar_event"
    description = (
        "Add an event straight to the user's connected Google Calendar after their yes. Work out the date first."
    )
    Args = CreateEventArgs
    risk = RiskLevel.CONFIRM

    async def _account(self, args: CreateEventArgs, ctx: ToolContext) -> tuple[CalendarAuth | None, str]:
        """The auth and the one account to add to, or None and why not."""
        auth = self._auth(ctx)
        if isinstance(auth, str):
            return None, auth
        accounts, problem = await self._chosen(auth, args.account)
        if problem:
            return None, problem
        if len(accounts) > 1:
            return None, (
                f"Several Google Calendars are connected ({', '.join(accounts)}): ask the user which one, then call "
                "again with its account."
            )
        return auth, accounts[0]

    async def precheck(self, args: CreateEventArgs, ctx: ToolContext) -> str | None:
        try:
            event_span(args.start, args.end, args.duration_minutes, args.all_day)
        except ValueError as exc:
            return str(exc)
        auth, found = await self._account(args, ctx)
        return None if auth is not None else found

    async def describe_call(self, args: CreateEventArgs, ctx: ToolContext) -> str:
        _, _, when = event_span(args.start, args.end, args.duration_minutes, args.all_day)
        where = "Google Calendar"
        if args.account:
            auth, account = await self._account(args, ctx)
            where = f"the Google Calendar of {account}" if auth is not None else where
        return f"add “{args.title}” to {where} for {when}"

    async def run(self, args: CreateEventArgs, ctx: ToolContext) -> ToolResult:
        try:
            start, end, when = event_span(args.start, args.end, args.duration_minutes, args.all_day)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        auth, account = await self._account(args, ctx)
        if auth is None:
            return ToolResult.error(account)
        body: dict[str, Any] = {"summary": args.title, "start": _when_body(start), "end": _when_body(end)}
        if args.location:
            body["location"] = args.location
        if args.details:
            body["description"] = args.details
        async with _http(self._transport) as http:
            try:
                answer = await CalendarApi(auth, account, http).request("POST", "/events", body=body)
            except GoogleError as exc:
                log.warning("Couldn't add a Google Calendar event: %s", exc)
                return ToolResult.error(f"I couldn't add it to Google Calendar. {_failure(account, exc)}")
        event_id = str(answer.get("id") or "") if isinstance(answer, dict) else ""
        return ToolResult.success(
            f"Added “{args.title}” to Google Calendar ({account}) for {when}. Its event_id is {event_id}.",
            data={"account": account, "event_id": event_id, "when": when},
        )


class FindEventArgs(ToolArgs):
    event: str | None = Field(default=None, max_length=200, description="Title words, e.g. 'dentist'.")
    on: date | None = Field(
        default=None,
        description=f"YYYY-MM-DD; omit to search the next {SEARCH_DAYS} days.",
    )
    event_id: str | None = Field(
        default=None,
        max_length=1024,
        pattern=r"^[A-Za-z0-9_-]+$",  # it goes in the request's path
        description="From an earlier answer.",
    )
    account: str | None = Field(default=None, description="Address or part.")

    @model_validator(mode="after")
    def _names_an_event(self) -> Self:
        if not self.event and not self.event_id:
            raise ValueError("say which event: give words from its title (event) or its event_id")
        return self


class _ChangeTool(_CalendarTool):
    """Finds the one event a change is about, before the question, and keeps it so the run changes that event."""

    risk = RiskLevel.CONFIRM

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, client_dir: Path | None = None) -> None:
        super().__init__(transport, client_dir)
        self._found: dict[str, tuple[float, CalendarAuth, FoundEvent]] = {}

    async def _search(self, api: CalendarApi, args: FindEventArgs) -> list[FoundEvent]:
        if args.event_id:
            try:
                return [e for e in [parse_event(api.address, await api.get(f"/events/{args.event_id}"))] if e]
            except Gone:
                return []
        first = _local_midnight(args.on) if args.on else _local_midnight(date.today())
        last = first + timedelta(days=1 if args.on else SEARCH_DAYS)
        params = {
            "q": args.event or "",
            "timeMin": first.isoformat(timespec="seconds"),
            "timeMax": last.isoformat(timespec="seconds"),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": MAX_LISTED,
        }
        answer = await api.get("/events", params)
        items = answer.get("items") if isinstance(answer, dict) else None
        return [e for e in (parse_event(api.address, i) for i in items or []) if e]

    async def _resolve(self, args: FindEventArgs, ctx: ToolContext) -> tuple[CalendarAuth, FoundEvent] | str:
        """The one event ``args`` names, or why there isn't one."""
        key = args.model_dump_json()
        if (kept := self._found.get(key)) is not None and time.monotonic() - kept[0] < REMEMBER_S:
            return kept[1], kept[2]
        auth = self._auth(ctx)
        if isinstance(auth, str):
            return auth
        accounts, problem = await self._chosen(auth, args.account)
        if problem:
            return problem
        found: list[FoundEvent] = []
        failures: list[str] = []
        async with _http(self._transport) as http:
            for address in accounts:  # one at a time: a refreshed token is saved over the others' entry
                try:
                    found += await self._search(CalendarApi(auth, address, http), args)
                except GoogleError as exc:
                    log.warning("Couldn't look in Google Calendar: %s", exc)
                    failures.append(_failure(address, exc))
        if args.event and not args.event_id:
            titled = [e for e in found if _title_matches(args.event, e.title)]
            found = titled or found
        if len(found) == 1:
            self._found[key] = (time.monotonic(), auth, found[0])
            return auth, found[0]
        if failures and not found:
            return "I couldn't look in Google Calendar. " + " ".join(failures)
        if not found:
            if args.event_id:
                what = f"with the id {args.event_id}"
            else:
                period = f"on {_day(args.on)}" if args.on else f"in the next {SEARCH_DAYS} days"
                what = f"matching {args.event!r} {period}"
            return f"No event {what} in Google Calendar." + (
                " Not looked in: " + " ".join(failures) if failures else ""
            )
        tagged = len(accounts) > 1
        listed = "; ".join(
            f"{e.named}{f' [{e.account}]' if tagged else ''}, event_id {e.id}" for e in found[:MAX_CHOICES]
        )
        more = f" and {len(found) - MAX_CHOICES} more" if len(found) > MAX_CHOICES else ""
        return (
            f"{len(found)} events match: {listed}{more}. Ask the user which one, then call again with its event_id "
            "(or give its day)."
        )

    async def precheck(self, args: FindEventArgs, ctx: ToolContext) -> str | None:
        resolved = await self._resolve(args, ctx)
        return resolved if isinstance(resolved, str) else None

    async def _event(self, args: FindEventArgs, ctx: ToolContext) -> tuple[CalendarAuth, FoundEvent] | str:
        """The event to act on: the one found before the question, else found now; forgotten once used."""
        resolved = await self._resolve(args, ctx)
        self._found.pop(args.model_dump_json(), None)
        return resolved


def _only_this_time(event: FoundEvent) -> str:
    return ", only this time (it repeats)" if event.repeats else ""


class DeleteGoogleCalendarEvent(_ChangeTool):
    name = "delete_google_calendar_event"
    description = (
        "Delete an event from the user's connected Google Calendar after their yes; for a repeating one, only that "
        "day's."
    )
    Args = FindEventArgs

    async def describe_call(self, args: FindEventArgs, ctx: ToolContext) -> str:
        resolved = await self._resolve(args, ctx)
        if isinstance(resolved, str):
            return "delete an event from Google Calendar"
        event = resolved[1]
        return f"delete {event.named} from Google Calendar{_only_this_time(event)}"

    async def run(self, args: FindEventArgs, ctx: ToolContext) -> ToolResult:
        resolved = await self._event(args, ctx)
        if isinstance(resolved, str):
            return ToolResult.error(resolved)
        auth, event = resolved
        async with _http(self._transport) as http:
            try:
                await CalendarApi(auth, event.account, http).request("DELETE", f"/events/{event.id}")
            except Gone:
                return ToolResult.success(f"{event.named} was already gone from Google Calendar.")
            except GoogleError as exc:
                log.warning("Couldn't delete a Google Calendar event: %s", exc)
                return ToolResult.error(f"I couldn't delete {event.named}. {_failure(event.account, exc)}")
        return ToolResult.success(
            f"Deleted {event.named} from Google Calendar{_only_this_time(event)}.",
            data={"account": event.account, "event_id": event.id},
        )


class UpdateEventArgs(FindEventArgs):
    new_title: str | None = Field(default=None, min_length=1, max_length=200)
    new_start: datetime | None = Field(
        default=None,
        description="Local YYYY-MM-DDTHH:MM; keeps its length unless new_end or duration_minutes.",
    )
    new_end: datetime | None = Field(default=None, description="All-day: its last day.")
    duration_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    all_day: bool | None = Field(default=None)
    location: str | None = Field(default=None, max_length=300, description="Empty clears it.")
    details: str | None = Field(default=None, max_length=2000, description="Empty clears it.")

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        fields = ("new_title", "new_start", "new_end", "duration_minutes", "all_day", "location", "details")
        if all(getattr(self, f) is None for f in fields):
            raise ValueError("say what to change: a new title, time, place or notes")
        return self


def _as_time(value: date) -> datetime:
    return value if isinstance(value, datetime) else _local_midnight(value)


def changes(event: FoundEvent, args: UpdateEventArgs) -> tuple[dict[str, Any], list[str]]:
    """The patch for ``event`` and the changes said in words. Raises ValueError for times that can't be."""
    body: dict[str, Any] = {}
    said: list[str] = []
    if args.new_start or args.new_end or args.duration_minutes or args.all_day is not None:
        all_day = event.all_day if args.all_day is None else args.all_day
        start = args.new_start or _as_time(event.start)
        end: datetime | None = args.new_end
        minutes = args.duration_minutes or 60
        if end is None and args.duration_minutes is None and all_day == event.all_day:
            if all_day:  # the same number of days
                end = start + (event.end - event.start) - timedelta(days=1)
            elif args.new_start:  # the same length
                minutes = max(1, round((_as_time(event.end) - _as_time(event.start)).total_seconds() / 60))
            else:
                end = _as_time(event.end)
        begins, ends, when = event_span(start, end, minutes, all_day)
        body.update(start=_when_body(begins), end=_when_body(ends))
        said.append(f"move it to {when}")
    if args.new_title is not None:
        body["summary"] = args.new_title
        said.append(f"rename it “{args.new_title}”")
    if args.location is not None:
        body["location"] = args.location
        said.append(f"set the place to “{args.location}”" if args.location else "clear the place")
    if args.details is not None:
        body["description"] = args.details
        said.append("replace its notes" if args.details else "clear its notes")
    return body, said


class UpdateGoogleCalendarEvent(_ChangeTool):
    name = "update_google_calendar_event"
    description = (
        "Move, rename or edit an event in the user's connected Google Calendar after their yes; for a repeating "
        "one, only that day's."
    )
    Args = UpdateEventArgs

    async def precheck(self, args: UpdateEventArgs, ctx: ToolContext) -> str | None:
        resolved = await self._resolve(args, ctx)
        if isinstance(resolved, str):
            return resolved
        try:
            changes(resolved[1], args)
        except ValueError as exc:
            return str(exc)
        return None

    async def describe_call(self, args: UpdateEventArgs, ctx: ToolContext) -> str:
        resolved = await self._resolve(args, ctx)
        if isinstance(resolved, str):
            return "change an event in Google Calendar"
        event = resolved[1]
        _, said = changes(event, args)
        return f"change {event.named} in Google Calendar{_only_this_time(event)}: {', '.join(said)}"

    async def run(self, args: UpdateEventArgs, ctx: ToolContext) -> ToolResult:
        resolved = await self._event(args, ctx)
        if isinstance(resolved, str):
            return ToolResult.error(resolved)
        auth, event = resolved
        try:
            body, said = changes(event, args)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        async with _http(self._transport) as http:
            try:
                answer = await CalendarApi(auth, event.account, http).request("PATCH", f"/events/{event.id}", body=body)
            except GoogleError as exc:
                log.warning("Couldn't change a Google Calendar event: %s", exc)
                return ToolResult.error(f"I couldn't change {event.named}. {_failure(event.account, exc)}")
        now = parse_event(event.account, answer)
        result = f" It's now {now.named}." if now is not None else ""
        return ToolResult.success(
            f"Changed {event.named} in Google Calendar{_only_this_time(event)}: {', '.join(said)}.{result}",
            data={"account": event.account, "event_id": event.id},
        )


TOOLS = [ConnectGoogleCalendar, CreateGoogleCalendarEvent, UpdateGoogleCalendarEvent, DeleteGoogleCalendarEvent]
