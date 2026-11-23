"""Calendar: read the user's events from private iCal (.ics) feed addresses.

Google Calendar, Outlook.com and iCloud all publish a calendar as a secret iCal address, which works without
OAuth or extra packages: an HTTP GET returns the whole calendar. The addresses are secrets (anyone with one can
read the calendar), so like the other keys they come from the environment (``.env``), never from YAML, and they
are never put in a tool result or a log line.

Repeating events are expanded here with the common subset of RFC 5545 rules (daily, weekly, monthly and yearly,
with INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, BYMONTH, EXDATE and edited or cancelled single instances). A rule
using anything rarer only shows its first occurrence rather than guessing wrong dates.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
import socket
import ssl
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from urllib.parse import SplitResult, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field

from octavia import keys
from octavia.config import AppConfig, current_env, dotenv_file, read_dotenv
from octavia.core.text import clock, day
from octavia.net import tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

CALENDAR_URLS_ENV = "CALENDAR_ICS_URLS"
SETUP_HINT = keys.hint(CALENDAR_URLS_ENV)
CONNECTIVITY_URL = "http://www.msftconnecttest.com/connecttest.txt"
# Google builds a big feed on request and can take well over 15 seconds before the first byte.
FETCH_TIMEOUT = httpx.Timeout(45.0, connect=10.0)
# Where each service shows a calendar's private iCal address, matched by the end of the feed's host name.
_SETTINGS_PAGES = {
    "calendar.google.com": ("Google Calendar", "https://calendar.google.com/calendar/r/settings"),
    "outlook.live.com": ("Outlook", "https://outlook.live.com/calendar/0/options/calendar/SharedCalendars"),
    "outlook.office365.com": ("Outlook", "https://outlook.office.com/calendar/options/calendar/SharedCalendars"),
    "outlook.office.com": ("Outlook", "https://outlook.office.com/calendar/options/calendar/SharedCalendars"),
    "icloud.com": ("iCloud Calendar", "https://www.icloud.com/calendar/"),
}
MAX_LISTED = 40
_MAX_PERIODS = 20_000  # a daily event started ~50 years ago; bounds the work for any rule
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_BYDAY = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")
_DURATION = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
_SUPPORTED_RULE_PARTS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"}
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_URL = re.compile(r"https?://[^\s<>\"'\\]+")
# Hosts of online meetings, matched by the end of a link's host name: only a link to one of these is an event's
# meeting link (a description is full of other links: a syllabus, a map, a sign-up form).
MEETING_HOSTS = (
    "zoom.us",
    "zoomgov.com",
    "meet.google.com",
    "teams.microsoft.com",
    "teams.live.com",
    "webex.com",
    "whereby.com",
    "gotomeeting.com",
    "meet.jit.si",
    "chime.aws",
    "bluejeans.com",
)

Params = dict[str, str]


def _split(url: str) -> SplitResult | None:
    """``url`` split into its parts, or None when it can't be: urlsplit raises ValueError ("Invalid IPv6 URL") for
    a stray square bracket in the host, e.g. a ``https://[paste the address here]`` placeholder left in ``.env``.
    """
    try:
        return urlsplit(url)
    except ValueError:
        return None


def _host(url: str) -> str:
    parts = _split(url)
    return (parts.hostname or "") if parts else ""


def setup_hint(elsewhere: tuple[Path, ...] = ()) -> str:
    """Why no calendar is connected, naming the exact file and variable checked, then how to connect one
    (``keys.missing``: the addresses in another ``.env`` such as the assistant's own folder, ``elsewhere``, a
    misspelled name, an empty value with the addresses on the lines below). Only key names and paths are quoted:
    the addresses are secrets."""
    return keys.missing(CALENDAR_URLS_ENV, also=elsewhere)


def _keys(path: Path) -> dict[str, str | None]:
    try:
        return read_dotenv(path)
    except (OSError, ValueError):
        return {}


def _elsewhere(config: AppConfig) -> tuple[Path, ...]:
    """Other ``.env`` files the addresses might have been put in: the assistant's work folder and the current one."""
    return keys.elsewhere(config)


def process_env_override() -> str:
    """A note for the user when the addresses in use come from the process environment instead of ``.env``
    (``keys.override_note``), or "" when the file's addresses are the ones in use or the variable holds none."""
    if not _parse_urls(current_env(CALENDAR_URLS_ENV)):
        return ""
    return keys.override_note(CALENDAR_URLS_ENV, same=lambda a, b: _parse_urls(a) == _parse_urls(b))


def _parse_urls(raw: str | None) -> list[str]:
    raw = _MARKDOWN_LINK.sub(r"\1", raw or "")
    urls = []
    for part in re.split(r"[\s,]+", raw.strip()):
        part = part.strip("<>\"'[]()")
        if part.lower().startswith("webcal://"):
            part = "https://" + part[len("webcal://") :]
        elif part and "://" not in part:
            part = "https://" + part
        if part:
            urls.append(part)
    return urls


def calendar_urls() -> list[str]:
    """The configured feed addresses, with webcal:// (what calendar apps hand out) turned into https://.

    Quotes, angle, square or round brackets pasted around an address (a Markdown ``[name](address)`` link keeps
    just the address), and a missing https://, are forgiven: httpx would otherwise fail them as a connection error
    that looked like the service being down. Read on every call, following edits to ``.env``, so connecting a
    calendar needs no restart. A variable that is set but has no address in it (left empty by a launcher or an old
    session) doesn't hide the addresses in ``.env``.
    """
    raw = current_env(CALENDAR_URLS_ENV)
    urls = _parse_urls(raw)
    if raw is not None and not urls and (checked := dotenv_file()) is not None:
        urls = _parse_urls(_keys(checked).get(CALENDAR_URLS_ENV))
    return urls


_tls_context = tls_context  # one trust store for every integration (octavia.net)


# --------------------------------------------------------------------------- parsing


@dataclass
class CalendarEvent:
    """One VEVENT. ``start`` is a date for all-day events, else a datetime (naive = the user's local time)."""

    title: str
    start: date
    duration: timedelta
    location: str = ""
    uid: str = ""
    rule: dict[str, str] | None = None
    exdates: set[datetime] = field(default_factory=set)
    recurrence_id: datetime | None = None
    cancelled: bool = False
    link: str = ""  # its online meeting's link (``meeting_link``), if it has one

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


@dataclass(frozen=True)
class Occurrence:
    title: str
    start: datetime  # aware
    end: datetime
    all_day: bool
    location: str = ""
    link: str = ""  # the online meeting's link, if it has one


def meeting_link(*texts: str) -> str:
    """The first link to an online meeting (``MEETING_HOSTS``) in these texts (an event's location, URL and
    description, in that order), or ""."""
    for text in texts:
        for found in _URL.findall(text or ""):
            url = found.rstrip(".,;:)]}>")
            host = _host(url)
            if any(host == h or host.endswith("." + h) for h in MEETING_HOSTS):
                return url
    return ""


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, Params, str]:
    quoted, colon = False, -1
    for index, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            colon = index
            break
    if colon < 0:
        return line.upper(), {}, ""
    name, *raw_params = line[:colon].split(";")
    params = {}
    for p in raw_params:
        key, _, value = p.partition("=")
        params[key.upper()] = value.strip('"')
    return name.upper(), params, line[colon + 1 :]


def _unescape(text: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), text).strip()


def _zone(tzid: str | None) -> tzinfo | None:
    """IANA zone for a TZID; None (the user's local time) for zones Python doesn't know, e.g. Windows names."""
    if not tzid:
        return None
    try:
        return ZoneInfo(tzid.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _parse_when(value: str, params: Params) -> date:
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").date()
    if value.upper().endswith("Z"):
        return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=_zone(params.get("TZID")))


def _parse_duration(value: str) -> timedelta:
    m = _DURATION.match(value.strip().upper())
    if not m:
        raise ValueError(f"bad duration {value!r}")
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in m.groups()[1:])
    delta = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -delta if m.group(1) == "-" else delta


def _local(naive: datetime) -> datetime:
    try:
        return naive.astimezone()
    except (OSError, OverflowError, ValueError):  # Windows can't convert times before 1970
        return naive.replace(tzinfo=datetime.now().astimezone().tzinfo)


def _key(value: date) -> datetime:
    """An aware datetime for comparing any start: dates are local midnight, naive times are local."""
    if isinstance(value, datetime):
        return value if value.tzinfo else _local(value)
    return _local(datetime.combine(value, time()))


def _build_event(props: dict[str, list[tuple[Params, str]]]) -> CalendarEvent | None:
    def first(name: str) -> tuple[Params, str] | None:
        return props[name][0] if props.get(name) else None

    dtstart = first("DTSTART")
    if dtstart is None:
        return None
    start = _parse_when(dtstart[1], dtstart[0])
    if dtend := first("DTEND"):
        end = _parse_when(dtend[1], dtend[0])
        both_dates = not isinstance(start, datetime) and not isinstance(end, datetime)
        duration = end - start if both_dates else _key(end) - _key(start)
    elif duration_prop := first("DURATION"):
        duration = _parse_duration(duration_prop[1])
    else:
        duration = timedelta(days=1) if not isinstance(start, datetime) else timedelta(0)

    rule = None
    if rrule := first("RRULE"):
        rule = {k.upper(): v for k, _, v in (p.partition("=") for p in rrule[1].strip().split(";")) if k}
    exdates = set()
    for params, value in props.get("EXDATE", []):
        for part in value.split(","):
            if part.strip():
                exdates.add(_key(_parse_when(part, params)))
    recurrence = first("RECURRENCE-ID")
    summary, location, uid, status = first("SUMMARY"), first("LOCATION"), first("UID"), first("STATUS")
    url, description = first("URL"), first("DESCRIPTION")
    link = meeting_link(
        _unescape(location[1]) if location else "",
        url[1].strip() if url else "",
        _unescape(description[1]) if description else "",
    )
    return CalendarEvent(
        title=_unescape(summary[1]) if summary else "",
        start=start,
        duration=max(duration, timedelta(0)),
        location=_unescape(location[1]) if location else "",
        uid=uid[1].strip() if uid else "",
        rule=rule,
        exdates=exdates,
        recurrence_id=_key(_parse_when(recurrence[1], recurrence[0])) if recurrence else None,
        cancelled=bool(status and status[1].strip().upper() == "CANCELLED"),
        link=link,
    )


def parse_ics(text: str) -> list[CalendarEvent]:
    """The events in an iCalendar document. Malformed events are skipped, not fatal."""
    events: list[CalendarEvent] = []
    stack: list[str] = []
    props: dict[str, list[tuple[Params, str]]] = {}
    for line in _unfold(text):
        name, params, value = _split_property(line)
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                props = {}
        elif name == "END":
            if stack and stack.pop() == "VEVENT":
                try:
                    if event := _build_event(props):
                        events.append(event)
                except (ValueError, KeyError, TypeError) as exc:
                    log.debug("Skipped a calendar event that couldn't be read: %s", exc)
        elif stack and stack[-1] == "VEVENT":
            props.setdefault(name, []).append((params, value))
    return events


# --------------------------------------------------------------------------- recurrence


def _ints(value: str | None) -> list[int]:
    return [int(v) for v in value.split(",") if v.strip()] if value else []


def _bydays(value: str | None) -> list[tuple[int | None, int]]:
    out = []
    for part in (value or "").split(","):
        if m := _BYDAY.match(part.strip().upper()):
            out.append((int(m.group(1)) if m.group(1) else None, _WEEKDAYS[m.group(2)]))
    return out


def _days_in_month(
    year: int, month: int, bydays: list[tuple[int | None, int]], bymonthday: list[int], default: int
) -> list[int]:
    """The matching days of one month. A default day the month lacks (the 31st) is skipped, as RFC 5545 says."""
    last = calendar.monthrange(year, month)[1]
    monthdays = {d if d > 0 else last + 1 + d for d in bymonthday} & set(range(1, last + 1))
    if bydays:
        days: set[int] = set()
        for nth, weekday in bydays:
            matches = [d for d in range(1, last + 1) if calendar.weekday(year, month, d) == weekday]
            if nth is None:
                days.update(matches)
            elif 0 < abs(nth) <= len(matches):
                days.add(matches[nth - 1 if nth > 0 else nth])
        return sorted(days & monthdays if bymonthday else days)
    if bymonthday:
        return sorted(monthdays)
    return [default] if default <= last else []


def _period(start: date, rule: dict[str, str], k: int) -> list[date]:
    """Candidate starts in the k-th period (day, week, month or year) of a rule, in order."""
    freq, interval = rule["FREQ"].upper(), max(int(rule.get("INTERVAL", "1")), 1)
    bydays, bymonthday, bymonth = _bydays(rule.get("BYDAY")), _ints(rule.get("BYMONTHDAY")), _ints(rule.get("BYMONTH"))
    if freq == "DAILY":
        day = start + timedelta(days=k * interval)
        if bydays and day.weekday() not in {w for _, w in bydays}:
            return []
        if bymonth and day.month not in bymonth:
            return []
        return [day]
    if freq == "WEEKLY":
        week = start - timedelta(days=start.weekday()) + timedelta(weeks=k * interval)
        weekdays = sorted({w for _, w in bydays}) or [start.weekday()]
        return [d for d in (week + timedelta(days=w) for w in weekdays) if not bymonth or d.month in bymonth]
    if freq == "MONTHLY":
        total = start.month - 1 + k * interval
        year, month = start.year + total // 12, total % 12 + 1
        if bymonth and month not in bymonth:
            return []
        return [
            start.replace(year=year, month=month, day=d)
            for d in _days_in_month(year, month, bydays, bymonthday, start.day)
        ]
    if freq == "YEARLY":
        year = start.year + k * interval
        out: list[date] = []
        for month in bymonth or [start.month]:
            days = _days_in_month(year, month, bydays, bymonthday, start.day)
            out.extend(start.replace(year=year, month=month, day=d) for d in days)
        return out
    raise ValueError(f"unsupported frequency {freq}")


def _starts(event: CalendarEvent, horizon: datetime) -> Iterator[date]:
    """Every start of ``event`` up to ``horizon``, oldest first."""
    rule = event.rule
    if not rule or "FREQ" not in rule or set(rule) - _SUPPORTED_RULE_PARTS:
        yield event.start
        return
    count = int(rule["COUNT"]) if rule.get("COUNT") else None
    until = _key(_parse_when(rule["UNTIL"], {})) if rule.get("UNTIL") else None
    produced = 0
    for k in range(_MAX_PERIODS):
        for candidate in _period(event.start, rule, k):
            if candidate < event.start:
                continue
            key = _key(candidate)
            if (until and key > until) or key > horizon:
                return
            produced += 1
            yield candidate
            if count and produced >= count:
                return


def occurrences(events: list[CalendarEvent], window_start: datetime, window_end: datetime) -> list[Occurrence]:
    """Every occurrence overlapping [window_start, window_end), sorted by start."""
    edited: dict[str, set[datetime]] = {}
    for event in events:
        if event.recurrence_id is not None:
            edited.setdefault(event.uid, set()).add(event.recurrence_id)
    out = []
    for event in events:
        if event.cancelled:
            continue
        skip = event.exdates | (edited.get(event.uid, set()) if event.recurrence_id is None else set())
        try:
            for start in _starts(event, window_end):
                begin = _key(start)
                if begin in skip:
                    continue
                end = _key(start + event.duration)
                if begin < window_end and (end > window_start or (end == begin and begin >= window_start)):
                    out.append(
                        Occurrence(event.title or "(untitled)", begin, end, event.all_day, event.location, event.link)
                    )
        except (ValueError, KeyError, TypeError) as exc:
            log.debug("Skipped a repeating calendar event: %s", exc)
    return sorted(out, key=lambda o: (o.start, not o.all_day, o.title))


# --------------------------------------------------------------------------- speaking


_day = day
_clock = clock


def describe(occ: Occurrence, window_start: datetime) -> str:
    where = f" ({occ.location})" if occ.location and occ.location != occ.link else ""
    if occ.link:
        where += f" [meeting link: {occ.link}]"
    if occ.all_day:
        shown = max(occ.start, window_start)
        last = occ.end - timedelta(days=1)
        through = f" through {_day(last)}" if last.date() > shown.date() else ""
        return f"{_day(shown)}: all day{through}, {occ.title}{where}"
    start, end = occ.start.astimezone(), occ.end.astimezone()
    if end == start:
        when = _clock(start)
    elif end.date() == start.date():
        when = f"{_clock(start)} to {_clock(end)}"
    else:
        when = f"{_clock(start)} to {_day(end)} {_clock(end)}"
    return f"{_day(start)}, {when}: {occ.title}{where}"


# --------------------------------------------------------------------------- tool


class CalendarArgs(ToolArgs):
    start_date: date | None = Field(default=None, description="YYYY-MM-DD; omit for today.")
    days: int = Field(default=1, ge=1, le=31, description="7 = a week.")


class GetCalendarEvents(Tool):
    name = "get_calendar_events"
    description = (
        "Read the user's calendar (read-only). To change it: the *_google_calendar_event tools if Google "
        "Calendar is connected, else add_calendar_event."
    )
    Args = CalendarArgs
    risk = RiskLevel.SAFE
    category = "calendar"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: CalendarArgs, ctx: ToolContext) -> str | None:
        return None if calendar_urls() else setup_hint(_elsewhere(ctx.config))

    async def _fetch(self, url: str) -> str:
        parts = _split(url)
        if parts is None:
            raise BadAddressError("the calendar address in the .env file isn't a valid web address")
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise BadAddressError("the calendar address in the .env file isn't a web address")
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT, follow_redirects=True, verify=_tls_context(), transport=self._transport
        ) as client:
            try:
                response = await client.get(url)
            except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
                raise BadAddressError("the calendar address in the .env file isn't a valid web address") from exc
            response.raise_for_status()
        if "BEGIN:VCALENDAR" not in response.text[:2000].upper():
            raise NotACalendarError("the address didn't return a calendar")
        return response.text

    async def _online(self) -> bool:
        """Whether the internet works at all, so a failed fetch can be blamed on the network or on the service.

        Asks Windows' own connectivity-check address; any answer (even a captive portal's redirect) means online.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0, transport=self._transport) as client:
                await client.get(CONNECTIVITY_URL)
        except httpx.HTTPError:
            return False
        return True

    async def run(self, args: CalendarArgs, ctx: ToolContext) -> ToolResult:
        urls = calendar_urls()
        if not urls:
            return ToolResult.error(setup_hint(_elsewhere(ctx.config)))
        first = args.start_date or date.today()
        window_start = _local(datetime.combine(first, time()))
        window_end = _local(datetime.combine(first + timedelta(days=args.days), time()))

        results = await asyncio.gather(*(self._fetch(url) for url in urls), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result
        network_failed = any(isinstance(r, httpx.TransportError) for r in results)
        online = await self._online() if network_failed else True
        events: list[CalendarEvent] = []
        problems = []
        for number, (url, result) in enumerate(zip(urls, results, strict=True), start=1):
            if isinstance(result, BaseException):
                reason = _reason(result, url, online)
                log.warning(
                    "Calendar %d (%s) couldn't be read: %s [%s]",
                    number,
                    _host(url) or "unreadable address",
                    reason,
                    _detail(result, url),
                )
                problems.append(f"calendar {number}: {reason}" if len(urls) > 1 else reason)
                continue
            events.extend(parse_ics(result))
        override = f" {note}" if problems and (note := process_env_override()) else ""
        if len(problems) == len(urls):
            return ToolResult.error(f"Couldn't read the calendar ({'; '.join(problems)}).{override}")

        found = occurrences(events, window_start, window_end)
        period = _day(window_start) if args.days == 1 else f"{_day(window_start)} to {_day(window_end - timedelta(1))}"
        lines = [describe(o, window_start) for o in found[:MAX_LISTED]]
        if len(found) > MAX_LISTED:
            lines.append(f"...and {len(found) - MAX_LISTED} more.")
        text = "\n".join(lines) if lines else f"Nothing on the calendar for {period}."
        if problems:
            text += f"\n(Couldn't read {'; '.join(problems)}.){override}"
        return ToolResult.success(text, data={"count": len(found), "period": period})


class NotACalendarError(ValueError):
    """The address answered with something else, usually a sign-in page because the private address expired."""


class BadAddressError(ValueError):
    """The configured address can't be requested at all, a setup mistake rather than anything on the network."""


def _reconnect(url: str) -> str:
    """How to reconnect the feed at ``url``: a fresh private address, since an iCal feed has no sign-in to renew."""
    host = _host(url).lower()
    service, page = next(
        (
            (name, link)
            for suffix, (name, link) in _SETTINGS_PAGES.items()
            if host == suffix or host.endswith("." + suffix)
        ),
        ("the calendar app", ""),
    )
    where = f" at {page}" if page else ""
    return (
        f"To reconnect, copy the calendar's private iCal address again from {service}'s settings{where} and put it "
        f"in {keys.env_file() or 'the .env file'} as {CALENDAR_URLS_ENV} (no restart needed). Offer to open that "
        "settings page."
    )


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and what caused it, outermost first: httpx wraps the socket error that says what really went wrong.

    Also looks inside exception groups, where anyio puts the errors of each address it tried to connect to.
    """
    out: list[BaseException] = []
    pending = [exc]
    while pending:
        current = pending.pop(0)
        if any(current is seen for seen in out):
            continue
        out.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if cause := current.__cause__ or current.__context__:
            pending.append(cause)
    return out


def _in_chain(exc: BaseException, kind: type[BaseException] | tuple[type[BaseException], ...]) -> bool:
    return any(isinstance(e, kind) for e in _chain(exc))


def _detail(exc: BaseException, url: str) -> str:
    """The underlying error in technical terms, for the log and for failures with no clearer explanation.

    The address and its secret path are cut out, since httpx and socket messages can quote the request.
    """
    root = _chain(exc)[-1]
    text = f"{type(root).__name__}: {root}" if str(root) else type(root).__name__
    parts = _split(url)
    for secret in (url, parts.path, parts.query) if parts else (url,):
        if len(secret) > 1:
            text = text.replace(secret, "…")
    return text[:200]


def _reason(exc: BaseException, url: str, online: bool) -> str:
    """Why a fetch failed and what to do about it: no network, something on this computer blocking the connection,
    a service outage, or a mistyped or expired address. An outage is only blamed when the evidence points there.

    Never includes the address itself (httpx puts the URL, a secret, in its messages).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403, 404, 410):
            return (
                f"the address was refused (HTTP {code}), so the calendar's private address has expired or been "
                f"reset. {_reconnect(url)}"
            )
        if code == 429:
            return "the calendar service is limiting requests right now (HTTP 429); try again in a few minutes"
        if code >= 500:
            return f"the calendar service is having an outage (HTTP {code}); the internet works, so try again later"
        return f"the calendar service answered HTTP {code}"
    if isinstance(exc, NotACalendarError):
        return (
            f"{exc}: it gave back a web page, usually a sign-in page, so the private address has expired or been "
            f"reset. {_reconnect(url)}"
        )
    if isinstance(exc, BadAddressError):
        return (
            f"{exc}: it has to be the calendar's private iCal address, starting with https:// or webcal://. "
            f"{_reconnect(url)}"
        )
    if isinstance(exc, httpx.TransportError):
        if not online:
            return (
                "the calendar service couldn't be reached because this computer isn't connected to the internet "
                "(no website answers); check the Wi-Fi or network cable"
            )
        if isinstance(exc, httpx.ProxyError):
            return "the calendar service couldn't be reached through the proxy; check the proxy settings"
        if _in_chain(exc, ssl.SSLError):
            return (
                "the calendar service couldn't be reached securely although the internet works; a firewall, "
                f"antivirus, proxy or a wrong system clock may be interfering ({_detail(exc, url)})"
            )
        if _in_chain(exc, socket.gaierror):
            return (
                "the calendar service couldn't be reached: its server name couldn't be looked up although the "
                "internet works, so the service may be down or the address mistyped"
            )
        if _in_chain(exc, (ConnectionError, PermissionError)):
            return (
                "the connection to the calendar service was refused or cut off although the internet works, so "
                "something on this computer or network (a firewall, antivirus, VPN or proxy) is probably blocking "
                f"it rather than the service having an outage ({_detail(exc, url)})"
            )
        if isinstance(exc, httpx.ConnectTimeout):
            return (
                "the calendar service couldn't be reached: it didn't answer in time although the internet works, "
                "so either it's having an outage or a firewall, VPN or proxy is holding up the connection"
            )
        if isinstance(exc, httpx.TimeoutException):
            return (
                "the calendar service was reached but took too long to send the calendar; a very large calendar "
                "or a slow connection can cause this, so try again in a minute"
            )
        if isinstance(exc, httpx.RemoteProtocolError):
            return (
                "the calendar service closed the connection without sending the calendar, often a VPN, proxy or "
                f"antivirus interfering; try again in a minute ({_detail(exc, url)})"
            )
        return f"the calendar service couldn't be reached although the internet works ({_detail(exc, url)})"
    if isinstance(exc, httpx.HTTPError):
        return f"the calendar request failed ({_detail(exc, url)})"
    if isinstance(exc, ValueError):
        return str(exc)
    return _detail(exc, url)


TOOLS = [GetCalendarEvents]
