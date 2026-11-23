"""Vercel, read-only: the user's projects (their own or their company's sites), recent deployments and whether each
is ready, failed or still building, a deployment's build log and errors, and domains.

Vercel's REST API (``api.vercel.com``) takes a personal access token the user makes once (vercel.com/account/tokens)
and pastes into ``data/vercel/token.txt``; see ``_hosting``. A team's projects are only listed with its ``teamId``, so
every call goes to the user's own account and to each team the token can see (``/v2/teams``), and a project found in
a team is asked about there. Only GET requests are made: nothing here deploys, redeploys, cancels or deletes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from octavia.config import find_project_root
from octavia.net import tls_context
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._hosting import UNTRUSTED, clip, missing_token, moment, pick, read_token, when

API_URL = "https://api.vercel.com"
TOKEN_FILE = find_project_root() / "data" / "vercel" / "token.txt"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_TEAMS = 10
MAX_PROJECTS = 100  # per account or team
MAX_LISTED = 40
MAX_ERROR_DETAILS = 3  # failed deployments whose error message is fetched
LOG_EVENTS = 1000
LOG_CHARS = 9000
HOW = "makes an access token at vercel.com/account/tokens (scoped to the team that owns the projects)"
REFUSED = (
    "Vercel refused the access token: it has probably expired or been deleted. The user can make a new one at "
    f"vercel.com/account/tokens and paste it into {TOKEN_FILE}; no restart is needed."
)
# Vercel's deployment states, as she says them.
STATES = {
    "READY": "ready",
    "ERROR": "failed",
    "BUILDING": "building",
    "QUEUED": "queued",
    "INITIALIZING": "starting",
    "CANCELED": "canceled",
}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ERROR_LINE = re.compile(r"\b(error|err!|failed|failure|fatal|exception|cannot find|not found)\b", re.IGNORECASE)


def setup_hint(path: Path = TOKEN_FILE) -> str:
    return missing_token("Vercel", path, HOW)


class VercelError(RuntimeError):
    """Vercel couldn't be reached, refused, or answered oddly; the message never holds the token."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Scope:
    """Where projects live: the user's own account (``team`` None) or one of their teams."""

    team: str | None
    label: str


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    scope: Scope
    framework: str = ""
    production: str = ""  # the state of its latest production deployment, as said ("ready")
    production_url: str = ""


@dataclass(frozen=True)
class Deployment:
    id: str
    project: str
    state: str  # Vercel's own word: READY, ERROR, ...
    created: Any
    scope: Scope
    url: str = ""
    target: str = ""
    branch: str = ""
    commit: str = ""
    creator: str = ""
    error: str = ""


def state_word(raw: Any) -> str:
    text = str(raw or "").upper()
    return STATES.get(text, text.lower() or "unknown")


def parse_deployment(raw: Any, scope: Scope) -> Deployment | None:
    if not isinstance(raw, dict):
        return None
    ident = raw.get("uid") or raw.get("id")
    if not ident:
        return None
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    creator = raw.get("creator") if isinstance(raw.get("creator"), dict) else {}
    return Deployment(
        id=str(ident),
        project=str(raw.get("name") or ""),
        state=str(raw.get("readyState") or raw.get("state") or ""),
        created=raw.get("created") or raw.get("createdAt"),
        scope=scope,
        url=str(raw.get("url") or ""),
        target=str(raw.get("target") or ""),
        branch=str(meta.get("githubCommitRef") or meta.get("gitlabCommitRef") or meta.get("bitbucketCommitRef") or ""),
        commit=" ".join(str(meta.get("githubCommitMessage") or meta.get("gitlabCommitMessage") or "").split()),
        creator=str(creator.get("username") or ""),
        error=" ".join(str(raw.get("errorMessage") or "").split()),
    )


def describe(d: Deployment, with_project: bool = True) -> str:
    parts = [f"{d.project}: " if with_project and d.project else ""]
    parts.append(state_word(d.state))
    if d.target == "production":
        parts.append(" (production)")
    if created := when(d.created):
        parts.append(f", {created}")
    if d.branch:
        parts.append(f", branch {d.branch}")
    if d.commit:
        parts.append(f", commit {clip(d.commit, 90)!r}")
    if d.creator:
        parts.append(f", by {d.creator}")
    if d.url:
        parts.append(f", {d.url}")
    parts.append(f" [id {d.id}]")
    if d.error:
        parts.append(f"\n  error: {clip(d.error, 300)}")
    return "".join(parts)


class VercelClient:
    """Vercel's REST API with the user's token: GET requests only, redirects never followed, so the token goes
    nowhere but ``api.vercel.com``."""

    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._transport = transport
        self._scopes: list[Scope] | None = None

    def _session(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_URL,
            timeout=TIMEOUT,
            verify=tls_context(),
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )

    async def get(self, path: str, params: dict[str, Any] | None = None, scope: Scope | None = None) -> Any:
        query = dict(params or {})
        if scope is not None and scope.team:
            query["teamId"] = scope.team
        try:
            async with self._session() as client:
                response = await client.get(path, params=query)
        except httpx.TimeoutException:
            raise VercelError("Vercel took too long to answer; try again in a minute") from None
        except httpx.TransportError as exc:
            raise VercelError(
                f"Vercel couldn't be reached ({type(exc).__name__}); check the internet connection"
            ) from None
        if response.status_code == 401:
            raise VercelError(REFUSED, 401)
        if response.status_code >= 300:
            raise VercelError(f"Vercel answered HTTP {response.status_code}{_reason(response)}", response.status_code)
        try:
            return response.json()
        except ValueError:
            raise VercelError("Vercel answered with something that isn't its API") from None

    async def scopes(self) -> list[Scope]:
        """The user's own account, then each team the token can see."""
        if self._scopes is None:
            found = [Scope(None, "your account")]
            try:
                data = await self.get("/v2/teams", {"limit": MAX_TEAMS})
            except VercelError as exc:
                if exc.status in (0, 401):
                    raise
                data = {}  # a token scoped to one team may not list teams
            teams = data.get("teams") if isinstance(data, dict) else None
            for team in teams if isinstance(teams, list) else []:
                if isinstance(team, dict) and team.get("id"):
                    found.append(Scope(str(team["id"]), str(team.get("name") or team.get("slug") or team["id"])))
            self._scopes = found
        return self._scopes

    async def each_scope(self, path: str, params: dict[str, Any] | None = None) -> list[tuple[Scope, Any]]:
        """``path`` asked in every scope, skipping the ones the token can't read. Raises only when none could be."""
        answers: list[tuple[Scope, Any]] = []
        first: VercelError | None = None
        for scope in await self.scopes():
            try:
                answers.append((scope, await self.get(path, params, scope)))
            except VercelError as exc:
                if exc.status in (0, 401):
                    raise
                first = first or exc
        if not answers and first is not None:
            raise first
        return answers

    async def projects(self) -> list[Project]:
        found: dict[str, Project] = {}
        for scope, data in await self.each_scope("/v9/projects", {"limit": MAX_PROJECTS}):
            for raw in _list(data, "projects"):
                if isinstance(raw, dict) and raw.get("id") and raw["id"] not in found:
                    found[str(raw["id"])] = _project(raw, scope)
        return sorted(found.values(), key=lambda p: p.name.casefold())

    async def find_project(self, name: str) -> Project:
        projects = await self.projects()
        match = pick(projects, name, lambda p: (p.name, p.id))
        if match is None:
            known = ", ".join(p.name for p in projects[:MAX_LISTED]) or "none"
            raise VercelError(f"Vercel has no project matching {name!r}. Its projects: {known}.")
        return match

    async def deployments(self, project: Project | None, limit: int, failed: bool = False) -> list[Deployment]:
        """The newest deployments, of one project or of everything the token sees, newest first."""
        params: dict[str, Any] = {"limit": limit}
        if failed:
            params["state"] = "ERROR"
        if project is not None:
            params["projectId"] = project.id
            answers = [(project.scope, await self.get("/v6/deployments", params, project.scope))]
        else:
            answers = await self.each_scope("/v6/deployments", params)
        found: dict[str, Deployment] = {}
        for scope, data in answers:
            for raw in _list(data, "deployments"):
                if (d := parse_deployment(raw, scope)) is not None and (not failed or d.state.upper() == "ERROR"):
                    found.setdefault(d.id, d)
        newest = sorted(found.values(), key=lambda d: _stamp(d.created), reverse=True)
        return newest[:limit]

    async def deployment(self, ident: str, scope: Scope | None = None) -> Deployment:
        """One deployment by id or URL, with its error message; looked for in every scope unless ``scope`` says."""
        ident = re.sub(r"^https?://", "", ident.strip()).strip("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", ident):
            raise VercelError(f"{ident!r} isn't a deployment id (dpl_...) or a deployment's address")
        for where in [scope] if scope is not None else await self.scopes():
            try:
                raw = await self.get(f"/v13/deployments/{ident}", None, where)
            except VercelError as exc:
                if exc.status in (0, 401) or scope is not None:
                    raise
                continue
            if (d := parse_deployment(raw, where)) is not None:
                return d
        raise VercelError(f"Vercel has no deployment {ident!r} that the token can see")

    async def build_log(self, d: Deployment) -> list[tuple[str, str]]:
        """The deployment's build output, oldest first, as (kind, line): kind is Vercel's ("stdout", "stderr"...)."""
        params = {"builds": 1, "direction": "backward", "limit": LOG_EVENTS, "follow": 0}
        data = await self.get(f"/v3/deployments/{d.id}/events", params, d.scope)
        events = data if isinstance(data, list) else data.get("events") if isinstance(data, dict) else None
        lines: list[tuple[Any, str, str]] = []
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            text = payload.get("text") if payload.get("text") is not None else event.get("text")
            if not isinstance(text, str):
                continue
            kind = str(event.get("type") or "")
            for line in _ANSI.sub("", text).splitlines():
                if line.strip():
                    lines.append((event.get("created") or payload.get("date"), kind, line.rstrip()))
        lines.sort(key=lambda entry: _stamp(entry[0]))
        return [(kind, line) for _, kind, line in lines]

    async def domains(self, project: Project | None) -> list[str]:
        if project is not None:
            data = await self.get(f"/v9/projects/{project.id}/domains", None, project.scope)
            return [_project_domain(raw) for raw in _list(data, "domains")]
        out: list[str] = []
        for scope, data in await self.each_scope("/v5/domains", {"limit": 100}):
            for raw in _list(data, "domains"):
                if isinstance(raw, dict) and raw.get("name"):
                    line = _account_domain(raw)
                    out.append(line if scope.team is None else f"{line} ({scope.label})")
        return out


def _reason(response: httpx.Response) -> str:
    """Vercel's own words for a failure (``{"error": {"message": ...}}``), never anything of the request."""
    try:
        data = response.json()
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return f": {clip(' '.join(str(message).split()), 200)}" if message else ""


def _stamp(value: Any) -> float:
    at = moment(value)
    return at.timestamp() if at else 0.0


def _list(data: Any, key: str) -> list[Any]:
    found = data.get(key) if isinstance(data, dict) else None
    return found if isinstance(found, list) else []


def _project(raw: dict[str, Any], scope: Scope) -> Project:
    targets = raw.get("targets") if isinstance(raw.get("targets"), dict) else {}
    production = targets.get("production") if isinstance(targets.get("production"), dict) else None
    if production is None:
        latest = [d for d in raw.get("latestDeployments") or [] if isinstance(d, dict)]
        production = next((d for d in latest if d.get("target") == "production"), latest[0] if latest else {})
    return Project(
        id=str(raw["id"]),
        name=str(raw.get("name") or raw["id"]),
        scope=scope,
        framework=str(raw.get("framework") or ""),
        production=state_word(production.get("readyState")) if production.get("readyState") else "",
        production_url=str(production.get("url") or ""),
    )


def _project_domain(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    line = str(raw.get("name") or "")
    if raw.get("verified") is False:
        line += " (not verified yet)"
    if raw.get("redirect"):
        line += f" (redirects to {raw['redirect']})"
    if raw.get("gitBranch"):
        line += f" (branch {raw['gitBranch']})"
    return line


def _account_domain(raw: dict[str, Any]) -> str:
    line = str(raw["name"])
    if raw.get("verified") is False:
        line += " (not verified)"
    if expires := when(raw.get("expiresAt")):
        line += f", renews or expires {expires}"
    return line


# ---------------------------------------------------------------------------------------------------- tools


class _VercelTool(Tool):
    risk = RiskLevel.SAFE
    category = "work"
    timeout_s = 60.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, token_file: Path = TOKEN_FILE) -> None:
        self._transport = transport
        self._token_file = token_file

    def client(self) -> VercelClient | None:
        token = read_token(self._token_file)
        return VercelClient(token, self._transport) if token else None

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        return None if read_token(self._token_file) else setup_hint(self._token_file)

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        client = self.client()
        if client is None:
            return ToolResult.error(setup_hint(self._token_file))
        try:
            return await self.ask(client, args)
        except VercelError as exc:
            return ToolResult.error(str(exc))

    async def ask(self, client: VercelClient, args: Any) -> ToolResult:
        raise NotImplementedError


class VercelProjects(_VercelTool):
    name = "vercel_projects"
    description = "List the user's Vercel projects (their sites) and their production status."

    async def ask(self, client: VercelClient, args: Any) -> ToolResult:
        projects = await client.projects()
        if not projects:
            return ToolResult.success("The Vercel token can't see any projects.", data={"count": 0})
        lines = []
        for p in projects[:MAX_LISTED]:
            line = f"{p.name} ({p.scope.label}"
            line += f", {p.framework})" if p.framework else ")"
            if p.production:
                line += f": production {p.production}"
                line += f" at {p.production_url}" if p.production_url else ""
            lines.append(line)
        if len(projects) > MAX_LISTED:
            lines.append(f"...and {len(projects) - MAX_LISTED} more.")
        return ToolResult.success("\n".join(lines), data={"count": len(projects)})


class DeploymentsArgs(ToolArgs):
    project: str = Field(default="", max_length=100, description="Omit for every project.")
    limit: int = Field(default=5, ge=1, le=20)
    failed_only: bool = Field(default=False)


class VercelDeployments(_VercelTool):
    name = "vercel_deployments"
    description = "Recent Vercel deployments, newest first, with state, branch, URL, id and failure reasons."
    Args = DeploymentsArgs

    async def ask(self, client: VercelClient, args: DeploymentsArgs) -> ToolResult:
        project = await client.find_project(args.project) if args.project else None
        found = await client.deployments(project, args.limit, args.failed_only)
        where = f" for {project.name}" if project else ""
        if not found:
            what = "failed deployments" if args.failed_only else "deployments"
            return ToolResult.success(f"No recent {what}{where} on Vercel.", data={"count": 0})
        # The list leaves out why a deployment failed; the first few failures are asked about one by one.
        failures = [i for i, d in enumerate(found) if d.state.upper() == "ERROR" and not d.error][:MAX_ERROR_DETAILS]
        for i in failures:
            try:
                full = await client.deployment(found[i].id, found[i].scope)
            except VercelError:
                continue
            if full.error:
                found[i] = full
        text = "\n".join(describe(d, with_project=project is None) for d in found)
        heading = f"Recent deployments{where}:\n" if project else ""
        return ToolResult.success(heading + text, data={"count": len(found)})


class BuildLogArgs(ToolArgs):
    project: str = Field(default="", max_length=100, description="Omit for any project.")
    deployment: str = Field(default="", max_length=200, description="Id or URL. Omit for the newest.")
    failed: bool = Field(default=False, description="Use the newest failed deployment.")
    errors_only: bool = Field(default=False)
    lines: int = Field(default=60, ge=5, le=300, description="From the end.")


class VercelBuildLogs(_VercelTool):
    name = "vercel_build_logs"
    description = "Read a Vercel deployment's build log and error."
    Args = BuildLogArgs

    async def ask(self, client: VercelClient, args: BuildLogArgs) -> ToolResult:
        if args.deployment:
            d = await client.deployment(args.deployment)
        else:
            project = await client.find_project(args.project) if args.project else None
            newest = await client.deployments(project, 1, args.failed)
            if not newest:
                where = f" for {project.name}" if project else ""
                return ToolResult.success(f"No {'failed ' if args.failed else ''}deployments{where} on Vercel.")
            d = await client.deployment(newest[0].id, newest[0].scope)
        log = await client.build_log(d)
        shown = [line for kind, line in log if not args.errors_only or _is_error(kind, line)][-args.lines :]
        head = describe(d)
        if not log:
            body = "(Vercel has no build output for it.)"
        elif not shown:
            body = "(No error lines in its build output.)"
        else:
            skipped = "" if len(shown) == len(log) else f"(last {len(shown)} of {len(log)} lines)\n"
            body = skipped + clip("\n".join(shown), LOG_CHARS)
        text = f"{head}\nBuild log:\n{body}\n{UNTRUSTED}"
        return ToolResult.success(text, data={"deployment": d.id, "state": state_word(d.state), "lines": len(shown)})


def _is_error(kind: str, line: str) -> bool:
    return kind in ("stderr", "fatal") or _ERROR_LINE.search(line) is not None or "warn" in line.lower()


class DomainsArgs(ToolArgs):
    project: str = Field(default="", max_length=100, description="Omit for every domain on the account.")


class VercelDomains(_VercelTool):
    name = "vercel_domains"
    description = "List the user's Vercel domains."
    Args = DomainsArgs

    async def ask(self, client: VercelClient, args: DomainsArgs) -> ToolResult:
        project = await client.find_project(args.project) if args.project else None
        found = [line for line in await client.domains(project) if line]
        where = f" for {project.name}" if project else ""
        if not found:
            return ToolResult.success(f"No domains{where} on Vercel.", data={"count": 0})
        return ToolResult.success(f"Domains{where}:\n" + "\n".join(found[:MAX_LISTED]), data={"count": len(found)})


TOOLS = [VercelProjects, VercelDeployments, VercelBuildLogs, VercelDomains]
