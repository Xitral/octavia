"""Canvas (the user's school, at ``CANVAS_BASE_URL``): what's due across their courses, and copying it into TickTick.

Canvas's planner API (``/api/v1/planner/items``) lists every course's assignments, quizzes and graded discussions
with their due dates and whether they're submitted, in one paginated request, so no per-course walk is needed. It
authenticates with a personal access token the user makes once in Canvas (Account, Settings, New Access Token).
Like the other keys it comes from the environment (``.env``: ``CANVAS_API_TOKEN``), never from YAML, is only ever
sent to the Canvas address, and is never put in a tool result or a log line.

The sync goes through the same TickTick session as the ``ticktick_`` tools. Which TickTick task each Canvas item
became is kept in ``data/canvas/ticktick.json``, so a later sync adds only what's new and moves a task when its due
date changes in Canvas, instead of adding it twice. TickTick's server describes its own arguments (``tools/list``),
so the task's fields are fitted to whatever names and nesting ``create_task`` and ``update_task`` declare.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field

from octavia import keys
from octavia.config import AppConfig
from octavia.core.jsonfile import read_json, write_json
from octavia.core.text import clock as _clock
from octavia.core.text import day as _day
from octavia.net import tls_context as _tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions import ticktick
from octavia.tools.extensions.ticktick import (
    ROOT,
    Answer,
    RemoteTool,
    TickTickError,
    flat_schema,
    parse_tasks,
    shared_client,
)

TOKEN_ENV = "CANVAS_API_TOKEN"
URL_ENV = "CANVAS_BASE_URL"
DEFAULT_URL = ""  # no school is assumed: Canvas is set up with both CANVAS_API_TOKEN and CANVAS_BASE_URL
STORE = ROOT / "data" / "canvas" / "ticktick.json"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_PAGES = 20
MAX_REDIRECTS = 5
MAX_LISTED = 40
# What counts as work that's due; the planner also lists notes, calendar events, pages and announcements.
KINDS = {"assignment": "assignment", "quiz": "quiz", "discussion_topic": "discussion", "sub_assignment": "assignment"}

SETUP_HINT = keys.hint(TOKEN_ENV)
REFUSED_HINT = (
    "Canvas refused the access token: it has probably expired or been deleted. The user can make a new one in Canvas "
    f"(Account, Settings, '+ New Access Token') and put it in the .env file as {TOKEN_ENV}; no restart is needed."
)


def setup_hint(config: AppConfig | None = None) -> str:
    """How to connect Canvas, naming the .env file checked and why the token looks missing (``keys.missing``)."""
    if keys.value(TOKEN_ENV) and not canvas_url():
        return keys.missing(URL_ENV, cfg=config)
    return keys.missing(TOKEN_ENV, cfg=config)


def canvas_url() -> str:
    """The school's Canvas address from ``CANVAS_BASE_URL`` ("" when it isn't set)."""
    raw = keys.value(URL_ENV).rstrip("/")
    if raw and "://" not in raw:
        raw = "https://" + raw
    return raw or DEFAULT_URL


def canvas_token() -> str:
    """The access token, or "" while Canvas isn't set up (no token, or no school address to use it with)."""
    return keys.value(TOKEN_ENV) if canvas_url() else ""


# ---------------------------------------------------------------------------------------------------- Canvas


class CanvasError(RuntimeError):
    """Canvas couldn't be reached, refused the token, or answered with something unexpected."""


@dataclass(frozen=True)
class CanvasItem:
    """One piece of graded work with a due date. ``key`` is stable across syncs ("assignment:123")."""

    key: str
    kind: str
    title: str
    course: str
    due: datetime  # aware
    url: str = ""
    done: bool = False


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_item(raw: Any, base: str) -> CanvasItem | None:
    """A planner item, or None for anything that isn't graded work with a due date."""
    if not isinstance(raw, dict):
        return None
    kind = KINDS.get(str(raw.get("plannable_type") or ""))
    plannable = raw.get("plannable") if isinstance(raw.get("plannable"), dict) else {}
    ident = raw.get("plannable_id") or plannable.get("id")
    if kind is None or ident is None:
        return None
    due = _parse_time(plannable.get("due_at")) or _parse_time(raw.get("plannable_date"))
    if due is None:
        return None
    submissions = raw.get("submissions") if isinstance(raw.get("submissions"), dict) else {}
    override = raw.get("planner_override") if isinstance(raw.get("planner_override"), dict) else {}
    done = bool(submissions.get("submitted") or submissions.get("excused") or override.get("marked_complete"))
    url = str(raw.get("html_url") or "")
    return CanvasItem(
        key=f"{raw.get('plannable_type')}:{ident}",
        kind=kind,
        title=" ".join(str(plannable.get("title") or plannable.get("name") or "Untitled").split())[:200],
        course=" ".join(str(raw.get("context_name") or "").split()),
        due=due,
        url=urljoin(base + "/", url) if url else "",
        done=done,
    )


class CanvasClient:
    """Canvas's REST API with the user's token. Redirects aren't followed (but a download's, without the token) and
    only next-page links on the same host are, so the token never goes anywhere but Canvas."""

    def __init__(self, base: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = base.rstrip("/")
        self._token = token
        self._transport = transport

    def session(self) -> httpx.AsyncClient:
        """One connection to Canvas carrying the token, for any number of ``get_all`` calls."""
        return httpx.AsyncClient(
            timeout=TIMEOUT,
            verify=_tls_context(),
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )

    async def get_all(self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None) -> list[Any]:
        """Every entry of a paginated list (``/api/v1/...``), following next-page links only on Canvas's host."""
        host = urlsplit(self.base).hostname
        url: str | None = f"{self.base}{path}"
        entries: list[Any] = []
        for _ in range(MAX_PAGES):
            if url is None:
                break
            response = await self._get(client, url, params)
            try:
                data = response.json()
            except ValueError:
                raise CanvasError("Canvas answered with something that isn't its API (a sign-in page?)") from None
            if not isinstance(data, list):
                raise CanvasError("Canvas answered with something unexpected")
            entries.extend(data)
            following = response.links.get("next", {}).get("url")
            url = following if following and urlsplit(following).hostname == host else None
            params = None  # the next-page link carries them
        return entries

    async def get_one(
        self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One object (``/api/v1/...``), such as a single assignment, page or file."""
        response = await self._get(client, f"{self.base}{path}", params)
        try:
            data = response.json()
        except ValueError:
            raise CanvasError("Canvas answered with something that isn't its API (a sign-in page?)") from None
        if not isinstance(data, dict):
            raise CanvasError("Canvas answered with something unexpected")
        return data

    async def download(self, client: httpx.AsyncClient, url: str, limit: int) -> bytes:
        """A file's bytes, at most ``limit`` of them. Canvas answers a download with a redirect to its file storage:
        the token goes only to Canvas's own host (over its own scheme), and the storage gets a request without it,
        since the link Canvas sends there is already signed."""
        base = urlsplit(self.base)
        target = urljoin(self.base + "/", url)
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_tls_context(), transport=self._transport) as plain:
            for _ in range(MAX_REDIRECTS):
                parts = urlsplit(target)
                own = parts.hostname == base.hostname and parts.scheme == base.scheme
                if not own and parts.scheme != "https":
                    raise CanvasError("Canvas sent the download to an address that isn't secure, so it wasn't fetched")
                try:
                    async with (client if own else plain).stream("GET", target) as response:
                        if response.is_redirect:
                            target = urljoin(target, response.headers["location"])
                            continue
                        if own:
                            self._check(response)
                        elif response.status_code >= 400:
                            raise CanvasError(f"Canvas's file storage answered HTTP {response.status_code}")
                        declared = response.headers.get("content-length", "")
                        if declared.isdigit() and int(declared) > limit:
                            raise CanvasError(f"The file is over {limit // 1_000_000} MB, too big to read here")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body += chunk
                            if len(body) > limit:
                                raise CanvasError(f"The file is over {limit // 1_000_000} MB, too big to read here")
                        return bytes(body)
                except httpx.TimeoutException:
                    raise CanvasError("The download took too long; try again in a minute") from None
                except httpx.TransportError as exc:
                    raise CanvasError(
                        f"The download failed ({type(exc).__name__}); check the internet connection"
                    ) from None
        raise CanvasError("Canvas redirected the download too many times")

    async def planner_items(self, start: datetime, end: datetime) -> list[CanvasItem]:
        """Every course's graded work due in [start, end), soonest first."""
        params = {
            "start_date": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "per_page": 100,
        }
        async with self.session() as client:
            entries = await self.get_all(client, "/api/v1/planner/items", params)
        found: dict[str, CanvasItem] = {}
        for raw in entries:
            if (item := parse_item(raw, self.base)) is not None and start <= item.due < end:
                found[item.key] = item
        return sorted(found.values(), key=lambda i: (i.due, i.course, i.title))

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, Any] | None) -> httpx.Response:
        try:
            response = await client.get(url, params=params)
        except httpx.TimeoutException:
            raise CanvasError("Canvas took too long to answer; try again in a minute") from None
        except httpx.TransportError as exc:
            raise CanvasError(
                f"Canvas couldn't be reached ({type(exc).__name__}); check the internet connection"
            ) from None
        self._check(response)
        return response

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise CanvasError(REFUSED_HINT)
        if response.status_code == 403:
            raise CanvasError("Canvas didn't allow reading this with the token (HTTP 403)")
        if 300 <= response.status_code < 400:
            raise CanvasError(
                f"Canvas sent the request elsewhere (HTTP {response.status_code}); check {URL_ENV} in the .env file"
            )
        if response.status_code >= 400:
            raise CanvasError(f"Canvas answered HTTP {response.status_code}")


async def fetch_items(
    start: datetime, end: datetime, transport: httpx.AsyncBaseTransport | None = None
) -> list[CanvasItem]:
    token = canvas_token()
    if not token:
        raise CanvasError(setup_hint())
    return await CanvasClient(canvas_url(), token, transport).planner_items(start, end)


def window(start_date: date | None, days: int) -> tuple[datetime, datetime]:
    """From local midnight of ``start_date`` (today) for ``days`` days: work due earlier today still shows."""
    first = start_date or date.today()
    start = datetime.combine(first, time()).astimezone()
    return start, datetime.combine(first + timedelta(days=days), time()).astimezone()


def describe(item: CanvasItem) -> str:
    local = item.due.astimezone()
    kind = "" if item.kind == "assignment" else f" ({item.kind})"
    course = f" for {item.course}" if item.course else ""
    status = " [submitted]" if item.done else ""
    return f"{_day(local)} at {_clock(local)}: {item.title}{kind}{course}{status}"


# ---------------------------------------------------------------------------------------------------- TickTick


class TaskServer(Protocol):
    """What the sync needs of TickTick's server (``TickTickClient``)."""

    async def list_tools(self) -> list[RemoteTool]: ...

    async def call(self, name: str, arguments: dict[str, Any], *, retry: bool = False) -> Answer: ...


# Our names for a task's fields, and the names (lower case, no separators) a server may give them, best first.
_FIELDS = {
    "title": ("title",),
    "content": ("content", "desc", "description", "notes", "note"),
    "due_date": ("duedate", "due", "duetime"),
    "start_date": ("startdate", "start"),
    "is_all_day": ("isallday", "allday"),
    "project_id": ("projectid", "listid"),
    "task_id": ("taskid", "id"),
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _properties(schema: Any) -> dict[str, Any] | None:
    """An object schema's properties, looking through ``anyOf``/``oneOf`` (an optional object)."""
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("properties"), dict) and schema["properties"]:
        return schema["properties"]
    for option in (schema.get("anyOf") or []) + (schema.get("oneOf") or []):
        if (found := _properties(option)) is not None:
            return found
    return None


def fit_arguments(schema: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """``values`` (our field names) under the names and nesting ``schema`` declares, and the fields it has no place
    for. The task's own fields go in a nested object (``{"task": {...}}``) only when the top level has no title,
    so a checklist's items are never taken for the task. A schema without properties gets our (snake_case) names,
    the ones TickTick's server answers with."""
    properties = _properties(flat_schema(schema))
    if properties is None:
        return dict(values), set()
    names = {_norm(key): key for key in properties}
    out: dict[str, Any] = {}
    left: dict[str, Any] = {}
    for name, value in values.items():
        key = next((names[alias] for alias in _FIELDS[name] if alias in names), None)
        if key is not None and key not in out:
            out[key] = value
        else:
            left[name] = value
    if "title" in left:
        for key, sub in properties.items():
            if key in out or not isinstance(sub, dict):
                continue
            is_list = sub.get("type") == "array"
            target = sub.get("items") if is_list else sub
            if _properties(target) is None:
                continue
            inner, rest = fit_arguments(target, left)
            if "title" not in rest:
                out[key] = [inner] if is_list else inner
                left = {k: v for k, v in left.items() if k in rest}
                break
    return out, set(left)


def _ticktick_time(moment: datetime) -> str:
    """TickTick's own format, in local time with its offset: "2026-09-29T23:59:59-0400"."""
    return moment.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _created_ids(answer: Answer) -> tuple[str, str]:
    """The new task's id and its list's id, from ``create_task``'s answer."""
    for task in parse_tasks(answer.text):
        if task.get("id"):
            return str(task["id"]), str(task.get("project_id") or task.get("projectId") or "")
    match = re.search(r"\b[0-9a-f]{24}\b", answer.text)  # TickTick's ids, when the answer is prose
    return (match.group(0) if match else ""), ""


@dataclass
class SyncReport:
    added: list[CanvasItem]
    moved: list[CanvasItem]
    unchanged: int
    submitted: int
    problems: list[str]

    def text(self) -> str:
        parts = []
        if self.added:
            parts.append(f"Added {len(self.added)} to TickTick: " + "; ".join(describe(i) for i in self.added[:15]))
        if self.moved:
            parts.append(
                f"Updated {len(self.moved)} whose due date or name changed in Canvas: "
                + "; ".join(describe(i) for i in self.moved[:15])
            )
        if not self.added and not self.moved:
            parts.append("Nothing new: TickTick already had everything that's due.")
        if self.unchanged and (self.added or self.moved):
            parts.append(f"{self.unchanged} were already in TickTick and unchanged.")
        if self.submitted:
            parts.append(f"{self.submitted} already submitted were left out.")
        if self.problems:
            parts.append("Problems: " + "; ".join(self.problems[:5]))
        return "\n".join(parts)


class SyncStore:
    """Which TickTick task each Canvas item became: ``{key: {task_id, project_id, due, title}}``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: dict[str, dict[str, str]] = {}
        data = read_json(path)
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, dict):
            self.items = {
                str(k): {str(f): str(v) for f, v in entry.items()}
                for k, entry in items.items()
                if isinstance(entry, dict)
            }

    def save(self) -> None:
        write_json(self.path, {"items": self.items}, indent=1)


async def _project_id(server: TaskServer, name: str) -> str:
    answer = await server.call("list_projects", {}, retry=True)
    if not answer.ok:
        raise TickTickError(answer.text[:200])
    projects = [p for p in parse_tasks(answer.text) if p.get("id") and p.get("name")]
    wanted = name.strip().casefold()
    for project in projects:
        if str(project["name"]).strip().casefold() == wanted:
            return str(project["id"])
    known = ", ".join(str(p["name"]) for p in projects[:30]) or "none found"
    raise TickTickError(f"TickTick has no list called {name!r} (its lists: {known})")


async def sync_items(
    items: Sequence[CanvasItem], server: TaskServer, store: SyncStore, list_name: str = ""
) -> SyncReport:
    """Add each unsubmitted item TickTick doesn't have yet, and update the ones whose due date or name changed.

    What each item became is saved after every task, so a sync cut off halfway never adds the same one twice."""
    report = SyncReport(added=[], moved=[], unchanged=0, submitted=0, problems=[])
    todo = []
    for item in items:
        if item.done:
            report.submitted += 1
            continue
        known = store.items.get(item.key)
        if known is None:
            todo.append(item)
        elif known.get("due") != item.due.astimezone(UTC).isoformat() or known.get("title") != item.title:
            todo.append(item)
        else:
            report.unchanged += 1
    if not todo:
        return report

    schemas = {tool.name: tool.schema for tool in await server.list_tools()}
    project = await _project_id(server, list_name) if list_name.strip() else ""
    for item in todo:
        due = _ticktick_time(item.due)
        fields: dict[str, Any] = {
            "title": item.title,
            "content": "\n".join(p for p in (item.course, item.url) if p),
            "start_date": due,
            "due_date": due,
            "is_all_day": False,
        }
        known = store.items.get(item.key)
        if known is None:
            if project:
                fields["project_id"] = project
            ok = await _create(server, schemas, item, fields, store, report)
        else:
            ok = await _update(server, schemas, item, fields, known, report)
        if ok:
            store.save()
    return report


async def _create(
    server: TaskServer,
    schemas: dict[str, dict[str, Any]],
    item: CanvasItem,
    fields: dict[str, Any],
    store: SyncStore,
    report: SyncReport,
) -> bool:
    if "create_task" not in schemas:
        report.problems.append("TickTick's server has no create_task tool")
        return False
    arguments, missing = fit_arguments(schemas["create_task"], fields)
    if "title" in missing:
        report.problems.append("TickTick's create_task takes arguments this doesn't understand")
        return False
    try:
        answer = await server.call("create_task", arguments)
    except TickTickError as exc:
        report.problems.append(f"couldn't add {item.title!r}: {exc}")
        return False
    if not answer.ok:
        report.problems.append(f"couldn't add {item.title!r}: {answer.text[:150]}")
        return False
    task_id, project_id = _created_ids(answer)
    store.items[item.key] = {
        "task_id": task_id,
        "project_id": project_id or str(fields.get("project_id") or ""),
        "due": item.due.astimezone(UTC).isoformat(),
        "title": item.title,
    }
    report.added.append(item)
    return True


async def _update(
    server: TaskServer,
    schemas: dict[str, dict[str, Any]],
    item: CanvasItem,
    fields: dict[str, Any],
    known: dict[str, str],
    report: SyncReport,
) -> bool:
    if not known.get("task_id") or "update_task" not in schemas:
        report.problems.append(f"couldn't update {item.title!r}: its TickTick task isn't known")
        return False
    changes = {k: v for k, v in fields.items() if k != "content"}  # the user's own notes on the task stay
    changes["task_id"] = known["task_id"]
    if known.get("project_id"):
        changes["project_id"] = known["project_id"]
    arguments, missing = fit_arguments(schemas["update_task"], changes)
    if "task_id" in missing or "due_date" in missing:
        report.problems.append("TickTick's update_task takes arguments this doesn't understand")
        return False
    try:
        answer = await server.call("update_task", arguments)
    except TickTickError as exc:
        report.problems.append(f"couldn't update {item.title!r}: {exc}")
        return False
    if not answer.ok:
        report.problems.append(f"couldn't update {item.title!r} (deleted in TickTick?): {answer.text[:150]}")
        return False
    known["due"] = item.due.astimezone(UTC).isoformat()
    known["title"] = item.title
    report.moved.append(item)
    return True


# ---------------------------------------------------------------------------------------------------- tools


class DueArgs(ToolArgs):
    start_date: date | None = Field(default=None, description="YYYY-MM-DD; omit for today.")
    days: int = Field(default=7, ge=1, le=120)
    include_submitted: bool = Field(default=False)


class CanvasDue(Tool):
    name = "canvas_due"
    description = (
        "Canvas (the user's school) coursework due across all courses, with times (read-only). Use for 'what's due this "
        "week'."
    )
    Args = DueArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 60.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def precheck(self, args: DueArgs, ctx: ToolContext) -> str | None:
        return None if canvas_token() else setup_hint(ctx.config)

    async def run(self, args: DueArgs, ctx: ToolContext) -> ToolResult:
        start, end = window(args.start_date, args.days)
        try:
            items = await fetch_items(start, end, self._transport)
        except CanvasError as exc:
            return ToolResult.error(str(exc))
        shown = items if args.include_submitted else [i for i in items if not i.done]
        period = _day(start) if args.days == 1 else f"{_day(start)} to {_day(end - timedelta(days=1))}"
        lines = [describe(i) for i in shown[:MAX_LISTED]]
        if len(shown) > MAX_LISTED:
            lines.append(f"...and {len(shown) - MAX_LISTED} more.")
        text = "\n".join(lines) if lines else f"Nothing due in Canvas from {period}."
        hidden = len(items) - len(shown)
        if hidden:
            text += f"\n({hidden} already submitted not listed.)"
        return ToolResult.success(text, data={"count": len(shown), "period": period})


class SyncArgs(ToolArgs):
    days: int = Field(default=30, ge=1, le=120)
    list_name: str = Field(default="", max_length=100, description="Omit for Inbox.")


_sync_lock = asyncio.Lock()


class CanvasSyncTickTick(Tool):
    name = "canvas_sync_ticktick"
    description = (
        "Copy upcoming unsubmitted Canvas work into TickTick as tasks with the same due times. Safe to rerun: no "
        "duplicates."
    )
    Args = SyncArgs
    risk = RiskLevel.CONFIRM
    category = "school"
    timeout_s = 240.0

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        server: Callable[[], TaskServer] | None = None,
        store: Path = STORE,
    ) -> None:
        self._transport = transport
        self._server = server or shared_client
        self._store = store

    async def describe_call(self, args: SyncArgs, ctx: ToolContext) -> str:
        where = f"your {args.list_name} list" if args.list_name else "TickTick"
        return f"copy what's due in Canvas over the next {args.days} days into {where}"

    async def precheck(self, args: SyncArgs, ctx: ToolContext) -> str | None:
        if not canvas_token():
            return setup_hint(ctx.config)
        return None if keys.is_set(ticktick.TOKEN_ENV) else ticktick.setup_hint(ctx.config)

    async def run(self, args: SyncArgs, ctx: ToolContext) -> ToolResult:
        start, end = window(None, args.days)
        try:
            items = await fetch_items(start, end, self._transport)
        except CanvasError as exc:
            return ToolResult.error(str(exc))
        async with _sync_lock:
            store = SyncStore(self._store)
            try:
                report = await sync_items(items, self._server(), store, args.list_name)
            except TickTickError as exc:
                return ToolResult.error(f"TickTick: {exc}")
            except OSError as exc:
                return ToolResult.error(f"Couldn't save what was synced: {exc}")
        data = {"added": len(report.added), "updated": len(report.moved), "problems": len(report.problems)}
        if report.problems and not report.added and not report.moved:
            return ToolResult.error(report.text(), data=data)
        return ToolResult.success(report.text(), data=data)


TOOLS = [CanvasDue, CanvasSyncTickTick]
