"""Supabase, read-only: the user's projects (their own or their company's databases), whether each is up and its
services healthy, its tables with about how many rows, a SELECT query, and recent logs.

Everything goes through Supabase's Management API (``api.supabase.com``) with a personal access token the user makes
once (supabase.com/dashboard/account/tokens) and pastes into ``data/supabase/token.txt``; see ``_hosting``. That token
could change anything, so the only SQL sent is one SELECT (or WITH ... SELECT) that passes ``read_only_sql``, and it's
sent with ``read_only`` set, so Supabase runs it as its read-only database role in a read-only transaction: a write
that slipped past the check (inside a function, say) is refused by the database itself. Nothing here pauses,
restores, migrates or changes a project.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import Field

from octavia.config import find_project_root
from octavia.net import tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._hosting import UNTRUSTED, clip, missing_token, moment, pick, read_token, when

API_URL = "https://api.supabase.com"
TOKEN_FILE = find_project_root() / "data" / "supabase" / "token.txt"
TIMEOUT = httpx.Timeout(45.0, connect=10.0)
MAX_LISTED = 40
MAX_TABLES = 200
RESULT_CHARS = 9000
ROW_CHARS = 600
HOW = "makes a personal access token at supabase.com/dashboard/account/tokens"
REFUSED = (
    "Supabase refused the access token: it has probably expired or been revoked. The user can make a new one at "
    f"supabase.com/dashboard/account/tokens and paste it into {TOKEN_FILE}; no restart is needed."
)
SERVICES = ("db", "auth", "rest", "realtime", "storage")
STATUSES = {
    "ACTIVE_HEALTHY": "up and healthy",
    "ACTIVE_UNHEALTHY": "up but unhealthy",
    "COMING_UP": "starting up",
    "GOING_DOWN": "shutting down",
    "INACTIVE": "paused",
    "PAUSING": "pausing",
    "PAUSE_FAILED": "failed to pause",
    "INIT_FAILED": "failed to start",
    "RESTORING": "restoring",
    "RESTORE_FAILED": "failed to restore",
    "UPGRADING": "upgrading",
    "RESIZING": "resizing",
    "REMOVED": "deleted",
}
LOG_SOURCES = {
    "postgres": "postgres_logs",
    "api": "edge_logs",
    "auth": "auth_logs",
    "storage": "storage_logs",
    "realtime": "realtime_logs",
    "functions": "function_logs",
    "postgrest": "postgrest_logs",
}
# What an error looks like in a log line; "| 500 |" is a failed request in the API's own log. No backslashes: the
# logs' SQL dialect would read them as escapes.
_LOG_ERRORS = "(?i)error|fatal|panic|exception|failed|denied|timeout|[|] [45][0-9][0-9] [|]"
_SCHEMA = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# ---- the SQL check

# A string, a quoted name or a comment, so each is seen whole: a ";" or "delete" inside quotes isn't SQL.
_TOKENS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", re.DOTALL)
_WRITES = re.compile(r"\b(insert|update|delete|merge|into)\b", re.IGNORECASE)
_LOCKS = re.compile(r"\bfor\s+(no\s+key\s+update|key\s+share|update|share)\b", re.IGNORECASE)
# Functions that act rather than read: end sessions, sleep, change settings or sequences, read the server's files,
# reach the network, or run other SQL given as text.
_ACTING = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_switch_wal|pg_sleep\w*|"
    r"set_config|nextval|setval|dblink\w*|lo_\w+|pg_read_\w*file|pg_ls_\w+|pg_stat_file|pg_\w*advisory\w*|"
    r"pg_notify|pg_create_\w+|pg_drop_\w+|pg_logical_\w+|query_to_xml\w*|http\w*|net\s*\.\s*\w+)\s*\(",
    re.IGNORECASE,
)


class UnsafeQuery(ValueError):
    """The SQL isn't one plain read: the message says why, for the brain to fix the query."""


def read_only_sql(sql: str) -> str:
    """``sql`` as one SELECT statement, comments and trailing semicolons dropped, or UnsafeQuery.

    Only a single ``SELECT`` or ``WITH ... SELECT`` passes: no second statement, no data-changing CTE, no
    ``SELECT ... INTO``, no row locks, and none of the functions in ``_ACTING``. It's the first guard, not the only
    one: the query is also run read-only (see the module docstring)."""
    kept = _TOKENS.sub(lambda m: " " if m.group(0).startswith(("--", "/*")) else m.group(0), sql)
    kept = kept.strip()
    while kept.endswith(";"):
        kept = kept[:-1].rstrip()
    bare = _TOKENS.sub(" '' ", kept)
    if not kept:
        raise UnsafeQuery("the query is empty")
    if "'" in bare.replace("''", "") or '"' in bare or "$" in re.sub(r"\$\d+", "", bare):
        raise UnsafeQuery("it has an unclosed quote or a $-quoted string; write it with plain 'single quotes'")
    if ";" in bare:
        raise UnsafeQuery("only one statement can be run at a time")
    first = bare.split(None, 1)[0].lower().lstrip("(")
    if first not in ("select", "with"):
        raise UnsafeQuery("only a SELECT query can be run (or WITH ... SELECT); nothing that changes the database")
    if match := _WRITES.search(bare):
        raise UnsafeQuery(f"it contains {match.group(1).upper()}, and only reading is allowed")
    if _LOCKS.search(bare):
        raise UnsafeQuery("it locks rows (FOR UPDATE/SHARE), and only reading is allowed")
    if match := _ACTING.search(bare):
        raise UnsafeQuery(f"it calls {match.group(1)}(), which does more than read")
    return kept


# ---- the API


class SupabaseError(RuntimeError):
    """Supabase couldn't be reached, refused, or answered oddly; the message never holds the token."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Project:
    ref: str
    name: str
    status: str = ""
    region: str = ""
    created: str = ""
    postgres: str = ""


def status_word(raw: str) -> str:
    return STATUSES.get(raw.upper(), raw.lower().replace("_", " ") or "in an unknown state")


class SupabaseClient:
    """Supabase's Management API with the user's token, redirects never followed, so the token goes nowhere but
    ``api.supabase.com``. The one POST is the read-only query."""

    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._transport = transport

    async def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=API_URL,
                timeout=TIMEOUT,
                verify=tls_context(),
                transport=self._transport,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise SupabaseError("Supabase took too long to answer; try again in a minute") from None
        except httpx.TransportError as exc:
            raise SupabaseError(
                f"Supabase couldn't be reached ({type(exc).__name__}); check the internet connection"
            ) from None
        if response.status_code == 401:
            raise SupabaseError(REFUSED, 401)
        if response.status_code == 429:
            raise SupabaseError("Supabase says too many requests were made just now; try again in a minute", 429)
        if response.status_code >= 300:
            raise SupabaseError(
                f"Supabase answered HTTP {response.status_code}{_reason(response)}", response.status_code
            )
        try:
            return response.json()
        except ValueError:
            raise SupabaseError("Supabase answered with something that isn't its API") from None

    async def projects(self) -> list[Project]:
        data = await self._send("GET", "/v1/projects")
        found = []
        for raw in data if isinstance(data, list) else []:
            if isinstance(raw, dict) and (raw.get("id") or raw.get("ref")):
                database = raw.get("database") if isinstance(raw.get("database"), dict) else {}
                found.append(
                    Project(
                        ref=str(raw.get("ref") or raw.get("id")),
                        name=str(raw.get("name") or raw.get("ref") or raw.get("id")),
                        status=str(raw.get("status") or ""),
                        region=str(raw.get("region") or ""),
                        created=str(raw.get("created_at") or ""),
                        postgres=str(database.get("version") or ""),
                    )
                )
        return sorted(found, key=lambda p: p.name.casefold())

    async def find_project(self, name: str) -> Project:
        """The project the user named, or their only one when they named none."""
        projects = await self.projects()
        if not name.strip() and len(projects) == 1:
            return projects[0]
        match = pick(projects, name, lambda p: (p.name, p.ref)) if name.strip() else None
        if match is None:
            known = ", ".join(f"{p.name} ({p.ref})" for p in projects[:MAX_LISTED]) or "none"
            which = f"no project matching {name!r}" if name.strip() else "several projects; say which one"
            raise SupabaseError(f"Supabase has {which}. The projects: {known}.")
        return match

    async def health(self, ref: str) -> list[dict[str, Any]]:
        data = await self._send("GET", f"/v1/projects/{ref}/health", params={"services": list(SERVICES)})
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def query(self, ref: str, sql: str) -> list[dict[str, Any]]:
        """Rows of a read-only query; ``sql`` must have passed ``read_only_sql``."""
        data = await self._send("POST", f"/v1/projects/{ref}/database/query", json={"query": sql, "read_only": True})
        if not isinstance(data, list):
            raise SupabaseError("Supabase answered the query with something unexpected")
        return [row for row in data if isinstance(row, dict)]

    async def logs(self, ref: str, sql: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        params = {
            "sql": sql,
            "iso_timestamp_start": start.astimezone(UTC).isoformat(),
            "iso_timestamp_end": end.astimezone(UTC).isoformat(),
        }
        data = await self._send("GET", f"/v1/projects/{ref}/analytics/endpoints/logs.all", params=params)
        if isinstance(data, dict) and data.get("error"):
            raise SupabaseError(f"Supabase couldn't read the logs: {clip(_flat(data['error']), 200)}")
        rows = data.get("result") if isinstance(data, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _flat(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("message") or json.dumps(value, default=str)
    return " ".join(str(value).split())


def _reason(response: httpx.Response) -> str:
    """Supabase's own words for a failure (a SQL error, say), never anything of the request."""
    try:
        data = response.json()
    except ValueError:
        return ""
    message = (data.get("message") or data.get("error")) if isinstance(data, dict) else None
    return f": {clip(_flat(message), 300)}" if message else ""


def rows_text(rows: list[dict[str, Any]], shown: int) -> str:
    lines = [clip(json.dumps(row, default=str, ensure_ascii=False), ROW_CHARS) for row in rows[:shown]]
    return clip("\n".join(lines), RESULT_CHARS)


# ---------------------------------------------------------------------------------------------------- tools


class _SupabaseTool(Tool):
    risk = RiskLevel.SAFE
    category = "work"
    timeout_s = 60.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, token_file: Path = TOKEN_FILE) -> None:
        self._transport = transport
        self._token_file = token_file

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        return None if read_token(self._token_file) else missing_token("Supabase", self._token_file, HOW)

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        token = read_token(self._token_file)
        if not token:
            return ToolResult.error(missing_token("Supabase", self._token_file, HOW))
        try:
            return await self.ask(SupabaseClient(token, self._transport), args)
        except SupabaseError as exc:
            return ToolResult.error(str(exc))

    async def ask(self, client: SupabaseClient, args: Any) -> ToolResult:
        raise NotImplementedError


class ProjectArgs(ToolArgs):
    project: str = Field(default="", max_length=100, description="Name or ref; omit if only one.")


class SupabaseProjects(_SupabaseTool):
    name = "supabase_projects"
    description = "List the user's Supabase projects (their databases)."

    async def ask(self, client: SupabaseClient, args: Any) -> ToolResult:
        projects = await client.projects()
        if not projects:
            return ToolResult.success("The Supabase token can't see any projects.", data={"count": 0})
        lines = []
        for p in projects[:MAX_LISTED]:
            line = f"{p.name} (ref {p.ref}"
            line += f", {p.region})" if p.region else ")"
            line += f": {status_word(p.status)}"
            if created := when(p.created):
                line += f", created {created}"
            lines.append(line)
        return ToolResult.success("\n".join(lines), data={"count": len(projects)})


class SupabaseHealth(_SupabaseTool):
    name = "supabase_health"
    description = "Check whether a Supabase project and its services are up and healthy."
    Args = ProjectArgs

    async def ask(self, client: SupabaseClient, args: ProjectArgs) -> ToolResult:
        project = await client.find_project(args.project)
        head = f"{project.name} is {status_word(project.status)}"
        head += f" (Postgres {project.postgres})." if project.postgres else "."
        if project.status.upper() not in ("ACTIVE_HEALTHY", "ACTIVE_UNHEALTHY", ""):
            return ToolResult.success(head, data={"status": project.status})
        services = await client.health(project.ref)
        lines = []
        unhealthy = 0
        for item in services:
            healthy = bool(item.get("healthy"))
            unhealthy += not healthy
            line = f"{item.get('name') or '?'}: {'healthy' if healthy else 'NOT healthy'}"
            if not healthy and item.get("status"):
                line += f" ({status_word(str(item['status']))})"
            if item.get("error"):
                line += f", {clip(_flat(item['error']), 200)}"
            lines.append(line)
        summary = "All its services are healthy." if services and not unhealthy else ""
        text = "\n".join([head, *lines, summary] if summary else [head, *lines])
        return ToolResult.success(text, data={"status": project.status, "unhealthy": unhealthy})


class TablesArgs(ProjectArgs):
    schema_name: str = Field(default="public", max_length=63)


class SupabaseTables(_SupabaseTool):
    name = "supabase_tables"
    description = "List a Supabase database's tables with estimated row counts. For an exact count use supabase_query."
    Args = TablesArgs

    async def ask(self, client: SupabaseClient, args: TablesArgs) -> ToolResult:
        if not _SCHEMA.fullmatch(args.schema_name):
            return ToolResult.error(f"{args.schema_name!r} isn't a schema name")
        project = await client.find_project(args.project)
        sql = (
            "select c.relname as table_name, greatest(c.reltuples, s.n_live_tup, 0)::bigint as rows "
            "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "left join pg_stat_user_tables s on s.relid = c.oid "
            f"where n.nspname = '{args.schema_name}' and c.relkind in ('r', 'p') "
            f"order by c.relname limit {MAX_TABLES}"
        )
        rows = await client.query(project.ref, sql)
        if not rows:
            return ToolResult.success(f"{project.name} has no tables in the {args.schema_name} schema.")
        lines = [f"{row.get('table_name')}: about {_count(row.get('rows'))} rows" for row in rows]
        text = f"Tables in {project.name} ({args.schema_name} schema):\n" + "\n".join(lines)
        return ToolResult.success(text, data={"count": len(rows)})


def _count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


class QueryArgs(ProjectArgs):
    sql: str = Field(
        min_length=1,
        max_length=8000,
        description="One Postgres SELECT (or WITH ... SELECT).",
    )
    max_rows: int = Field(default=50, ge=1, le=200)


class SupabaseQuery(_SupabaseTool):
    name = "supabase_query"
    description = "Run one read-only SELECT on a Supabase database. Nothing that changes data or schema."
    Args = QueryArgs

    async def ask(self, client: SupabaseClient, args: QueryArgs) -> ToolResult:
        try:
            sql = read_only_sql(args.sql)
        except UnsafeQuery as exc:
            return ToolResult.error(f"That query wasn't run: {exc}.")
        project = await client.find_project(args.project)
        bounded = f"select * from (\n{sql}\n) as octavia_query limit {args.max_rows + 1}"
        rows = await client.query(project.ref, bounded)
        if not rows:
            return ToolResult.success(f"The query returned no rows in {project.name}.", data={"rows": 0})
        more = len(rows) > args.max_rows
        head = f"{min(len(rows), args.max_rows)} row(s) from {project.name}" + (" (more were left out)" if more else "")
        text = f"{head}:\n{rows_text(rows, args.max_rows)}\n{UNTRUSTED}"
        return ToolResult.success(text, data={"rows": min(len(rows), args.max_rows), "more": more})


class LogsArgs(ProjectArgs):
    source: Literal["postgres", "api", "auth", "storage", "realtime", "functions", "postgrest"] = Field(
        default="postgres"
    )
    errors_only: bool = Field(default=False)
    hours: int = Field(default=24, ge=1, le=24)
    limit: int = Field(default=30, ge=1, le=100)


class SupabaseLogs(_SupabaseTool):
    name = "supabase_logs"
    description = "Read a Supabase project's recent logs, newest first."
    Args = LogsArgs

    async def ask(self, client: SupabaseClient, args: LogsArgs) -> ToolResult:
        project = await client.find_project(args.project)
        where = f" where regexp_contains(event_message, '{_LOG_ERRORS}')" if args.errors_only else ""
        sql = (
            f"select timestamp, event_message from {LOG_SOURCES[args.source]}{where} "
            f"order by timestamp desc limit {args.limit}"
        )
        end = datetime.now(UTC)
        rows = await client.logs(project.ref, sql, end - timedelta(hours=args.hours), end)
        what = f"{'error ' if args.errors_only else ''}lines in {project.name}'s {args.source} log"
        if not rows:
            return ToolResult.success(f"No {what} in the last {args.hours} hour(s).", data={"lines": 0})
        lines = []
        for row in rows:
            at = moment(row.get("timestamp"))
            stamp = f"{at.astimezone():%b %d %H:%M:%S} " if at else ""
            lines.append(stamp + clip(" ".join(str(row.get("event_message") or "").split()), ROW_CHARS))
        text = f"{len(rows)} {what}, newest first:\n" + clip("\n".join(lines), RESULT_CHARS) + f"\n{UNTRUSTED}"
        return ToolResult.success(text, data={"lines": len(rows)})


TOOLS = [SupabaseProjects, SupabaseHealth, SupabaseTables, SupabaseQuery, SupabaseLogs]
