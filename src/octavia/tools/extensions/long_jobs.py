"""Tools for long jobs: downloading a file, running a long command, and checking on either (``_long_jobs``).

A job runs in a process of its own that outlives her restarts, and she says when it finishes, fails or dies. Her
word on a job comes only from checking it: starting one waits until it has shown it works (data on disk, a running
process) and says what it saw, and ``long_job_status`` checks the process and the file each time.

Risk: a download reaches the internet and writes a file, so it's asked, unless the user's own words asked for this
very file (``tools/asked.py``). A program (``.exe``, a script, an installer) or a download after she read outside
content is always asked. It never writes into her own folder or Windows', nor to a network or device path
(``\\server``, ``\\?\\``), and never replaces a file; the worker checks the same rules again
(``destination_problem``). A long command is judged by exactly the terminal's rules (``terminal.classify``).
Cancelling is asked, and so is resuming a download the user cancelled.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import Field

from octavia.config import find_project_root
from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._long_jobs import Check, JobManager
from octavia.tools.extensions.terminal import RunCommand, RunCommandArgs, _untrusted_turn

_UNAVAILABLE = "Long jobs aren't available right now (they start with the assistant)."
MAX_LIMIT_MIN = 24 * 60
START_WAIT_S = 12.0  # how long starting a download waits to see data arrive before it answers
# A file that runs code when it's opened: always asked, one by one.
_PROGRAM = re.compile(
    r"\.(?:exe|msi|msix|msixbundle|appx|appxbundle|bat|cmd|com|scr|pif|ps1|psm1|vbs|vbe|js|jse|wsf|wsh|hta|jar|"
    r"lnk|reg|dll|cpl|sys|inf|application|appref-ms|appinstaller|py|pyw|pth|iso|img|vhd|vhdx|chm|msp|xll|docm|dotm|"
    r"xlsm|xltm|xlam|pptm|potm|ppam|scf|url|settingcontent-ms)$",
    re.IGNORECASE,
)
_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com\d|lpt\d)(?:\..*)?$", re.IGNORECASE)
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SYSTEM = re.compile(r"\\windows(?:\\|$)|\\program files|\\programdata(?:\\|$)", re.IGNORECASE)


def _manager(ctx: ToolContext) -> JobManager | None:
    manager = ctx.extras.get("long_jobs")
    return manager if isinstance(manager, JobManager) else None


def _downloads() -> Path:
    try:
        from octavia.tools.extensions.files import known_folder

        found = known_folder("downloads")
    except Exception:
        found = None
    return found or Path.home() / "Downloads"


def name_from_url(url: str) -> str:
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]).strip()


class DownloadArgs(ToolArgs):
    url: str = Field(min_length=8, max_length=2000, description="The file's http(s) address.")
    folder: str = Field("", description="Folder to save it in (default: Downloads).")
    file_name: str = Field("", max_length=200, description="Name to save it as (default: from the address).")


def destination(args: DownloadArgs) -> Path:
    folder = Path(os.path.expandvars(args.folder)).expanduser() if args.folder else _downloads()
    return folder / (args.file_name or name_from_url(args.url))


def destination_problem(dest: Path) -> str | None:
    """Why a download may not be written to ``dest`` (the tool asks before starting one, the worker again before it
    writes), or None. Only a plain path on one of this PC's drives; never her own folder or Windows'."""
    if str(dest).startswith(("\\", "//")) or not dest.drive:
        return "I only download to a full folder path on this PC's drives, not a network or device path."
    real = os.path.normcase(os.path.realpath(dest))
    if real.startswith(("\\", "//")):  # a link that leads to a network or device path
        return "I only download to a full folder path on this PC's drives, not a network or device path."
    root = os.path.normcase(os.path.realpath(find_project_root()))
    if real == root or real.startswith(root.rstrip("\\/") + os.sep):
        return "I don't download into my own folder; pick another one."
    if _SYSTEM.search(real):
        return "I don't download into Windows' or installed programs' folders; pick another one."
    return None


def problem_with(args: DownloadArgs) -> str | None:
    """Why this download can't be started as asked, or None."""
    parts = urlsplit(args.url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"{args.url} isn't a web address I can download from (http or https)."
    if parts.username or parts.password:
        return "That address has a password in it; I don't download with those."
    name = args.file_name or name_from_url(args.url)
    if not name or name in (".", "..") or _BAD_NAME.search(name) or _RESERVED.match(name) or name.endswith((" ", ".")):
        return "I can't tell what to call the file from that address; give it a file name."
    dest = destination(args)
    problem = destination_problem(dest)
    if problem:
        return problem
    if not dest.parent.is_dir():
        return f"There's no folder {dest.parent}."
    if dest.exists():
        return f"There's already a file at {dest}; I won't replace it. Give it another name, or delete that one first."
    return None


def _started_line(found: Check | None) -> str:
    """What starting it showed, for her to pass on: only what was checked."""
    if found is None:
        return "It didn't start: its files are gone."
    if found.status == "running":
        return f"Checked just now: {found.line}"
    if found.final:
        return f"It already ended. {found.line}"
    return (
        f"It hasn't shown that it works yet: {found.line} Don't tell the user it's running; say it's starting and "
        "you'll tell them if it fails, and check with long_job_status before saying more."
    )


class StartDownload(Tool):
    name = "start_download"
    description = (
        "Download a file from the web (a model, an installer, a video, a dataset), however big, in a process of its "
        "own that keeps going while you restart and resumes after dropped connections. The user hears when it "
        "finishes or fails. Use this, not run_command, for any download that isn't tiny."
    )
    Args = DownloadArgs
    risk = RiskLevel.CONFIRM
    category = "system"
    timeout_s = 40.0

    async def inspect_risk(self, args: DownloadArgs, ctx: ToolContext) -> RiskFinding | None:
        name = destination(args).name
        if _PROGRAM.search(name):
            return RiskFinding(RiskLevel.CONFIRM, f"program:{args.url}", "is a program or script that can run")
        if _untrusted_turn(ctx):
            reason = "comes after reading something from outside, which could be steering me"
            return RiskFinding(RiskLevel.CONFIRM, f"outside:{args.url}", reason)
        return None

    async def describe_call(self, args: DownloadArgs, ctx: ToolContext) -> str:
        dest = destination(args)
        host = urlsplit(args.url).hostname or "the web"
        return f"download {dest.name} from {host} into {dest.parent}"

    async def precheck(self, args: DownloadArgs, ctx: ToolContext) -> str | None:
        manager = _manager(ctx)
        if manager is None:
            return _UNAVAILABLE
        busy = await asyncio.to_thread(manager.active_for, destination(args))
        if busy is not None:
            return f"That file is already being downloaded (long job #{busy.id}). {busy.line}"
        return problem_with(args)

    async def run(self, args: DownloadArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        problem = problem_with(args)
        if problem:
            return ToolResult.error(problem)
        dest = destination(args)
        if await asyncio.to_thread(manager.active_for, dest) is not None:
            return ToolResult.error("That file is already being downloaded; long_job_status shows it.")
        job_id = await manager.start_download(args.url, dest)
        found = await manager.wait_for_start(job_id, START_WAIT_S)
        data = {"id": job_id, "status": found.status if found else "missing", "path": str(dest)}
        text = f"Long job #{job_id}: downloading {dest.name} into {dest.parent}. {_started_line(found)}"
        if found is not None and found.final and found.status != "done":
            return ToolResult.error(text, data)
        return ToolResult.success(text + " The user hears when it ends; don't wait for it.", data)


class LongCommandArgs(ToolArgs):
    command: str = Field(min_length=1, max_length=4000, description="One PowerShell command line")
    title: str = Field(min_length=1, max_length=80, description="A short name for it.")
    cwd: str = Field("", description="Folder to run it in (default: the user's home folder)")
    time_limit_min: int = Field(360, ge=1, le=MAX_LIMIT_MIN, description="Stop it after this many minutes.")

    def as_run_command(self) -> RunCommandArgs:
        return RunCommandArgs(command=self.command, cwd=self.cwd)


class RunLongCommand(Tool):
    name = "run_long_command"
    description = (
        "Run a PowerShell command that takes longer than a few minutes (a big build, a model conversion, a backup) "
        "in a process of its own that keeps going while you restart; the user hears when it finishes or fails. "
        "Same rules as run_command for what's asked or blocked. Not for downloads (start_download) or for programs "
        "that keep running (dev servers, apps)."
    )
    Args = LongCommandArgs
    risk = RiskLevel.SAFE  # raised per command by the terminal's rules
    category = "system"
    timeout_s = 40.0

    def assess_risk(self, args: LongCommandArgs) -> RiskLevel:
        return RunCommand().assess_risk(args.as_run_command())

    async def inspect_risk(self, args: LongCommandArgs, ctx: ToolContext) -> RiskFinding | None:
        return await RunCommand().inspect_risk(args.as_run_command(), ctx)

    async def describe_call(self, args: LongCommandArgs, ctx: ToolContext) -> str:
        return "start the long " + (await RunCommand().describe_call(args.as_run_command(), ctx)).removeprefix("run ")

    async def precheck(self, args: LongCommandArgs, ctx: ToolContext) -> str | None:
        if _manager(ctx) is None:
            return _UNAVAILABLE
        return await RunCommand().precheck(args.as_run_command(), ctx)

    async def run(self, args: LongCommandArgs, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        folder = Path(os.path.expandvars(args.cwd)).expanduser() if args.cwd else Path.home()
        if not folder.is_dir():
            return ToolResult.error(f"There's no folder {folder}.")
        job_id = await manager.start_command(args.command, folder, args.title, args.time_limit_min * 60.0)
        found = await manager.wait_for_start(job_id, timeout_s=5.0)
        data = {"id": job_id, "status": found.status if found else "missing"}
        text = f"Long job #{job_id} “{args.title}”. {_started_line(found)}"
        if found is not None and found.final:
            tail = f"\nOutput:\n{found.output}" if found.output else ""
            return (ToolResult.success if found.status == "done" else ToolResult.error)(text + tail, data)
        return ToolResult.success(text + " The user hears when it ends; don't wait for it.", data)


class JobRef(ToolArgs):
    job: str = Field("", description="Number or name; empty for all of them.")


class LongJobStatus(Tool):
    name = "long_job_status"
    description = (
        "Check downloads and long commands for real: whether each one's process is alive, how much is on disk, "
        "speed, time left, how it ended. Call it before saying any job is running, how far it is, or that it "
        "finished."
    )
    Args = JobRef
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: JobRef, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(_UNAVAILABLE)
        if args.job:
            found = await asyncio.to_thread(manager.find, args.job)
            if found is None:
                return ToolResult.error(f"There's no long job “{args.job}”; long_job_status with no job lists them.")
            extra = f"\nIts last output:\n{found.output}" if found.output else ""
            hint = " resume_long_job picks it up from where it stopped." if found.resumable else ""
            return ToolResult.success(f"#{found.id}: {found.line}{hint}{extra}", data=_data(found))
        checks = await asyncio.to_thread(manager.checks)
        if not checks:
            return ToolResult.success("No downloads or long commands yet.")
        going = [c for c in checks if c.alive]
        ended = [c for c in checks if not c.alive][-8:]
        lines = [f"#{c.id}: {c.line}" for c in (*going, *reversed(ended))]
        if not going:
            lines.insert(0, "None running right now.")
        return ToolResult.success("\n".join(lines), data={"running": len(going), "count": len(checks)})


def _data(found: Check) -> dict[str, object]:
    return {"id": found.id, "status": found.status, "done": found.done, "total": found.total, "path": found.path}


class ResumeLongJob(Tool):
    name = "resume_long_job"
    description = "Start a download that died, failed or was cancelled again, from where it stopped."
    Args = JobRef
    risk = RiskLevel.SAFE  # the same download they already said yes to, into the same file
    category = "system"
    timeout_s = 40.0

    async def inspect_risk(self, args: JobRef, ctx: ToolContext) -> RiskFinding | None:
        manager = _manager(ctx)
        found = await asyncio.to_thread(manager.find, args.job) if manager and args.job else None
        if found is not None and found.status == "cancelled":  # they stopped it: starting it again is theirs to say
            return RiskFinding(RiskLevel.CONFIRM, f"cancelled:{found.id}", "was cancelled")
        return None

    async def describe_call(self, args: JobRef, ctx: ToolContext) -> str:
        manager = _manager(ctx)
        found = await asyncio.to_thread(manager.find, args.job) if manager and args.job else None
        return f"resume the download of {Path(found.path).name}" if found and found.path else "resume a download"

    async def precheck(self, args: JobRef, ctx: ToolContext) -> str | None:
        manager = _manager(ctx)
        if manager is None:
            return _UNAVAILABLE
        found = await asyncio.to_thread(manager.find, args.job) if args.job else None
        if found is None:
            return f"There's no long job “{args.job}”; long_job_status lists them."
        if not found.resumable:
            if found.alive:
                return f"It's still going. {found.line}"
            return f"Only a download that stopped can be resumed. {found.line}"
        return None

    async def run(self, args: JobRef, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        found = await asyncio.to_thread(manager.find, args.job) if manager and args.job else None
        if manager is None or found is None or not found.resumable:
            return ToolResult.error("That job can't be resumed; long_job_status shows why.")
        if Path(found.path).exists():
            return ToolResult.error(f"There's already a file at {found.path} now, so I won't download over it.")
        kept = f" from {found.done // 1024**2} MB" if found.done else ""
        try:
            problem = await manager.resume(found.id)
        except OSError as exc:
            problem = f"Couldn't start it again: {exc}"
        if problem:
            return ToolResult.error(problem)
        started = await manager.wait_for_start(found.id, START_WAIT_S)
        return ToolResult.success(
            f"Resumed long job #{found.id}{kept}. {_started_line(started)}", data={"id": found.id}
        )


class CancelLongJob(Tool):
    name = "cancel_long_job"
    description = "Stop a download or long command that's running. A download's partial file is kept for resuming."
    Args = JobRef
    risk = RiskLevel.CONFIRM
    category = "system"
    timeout_s = 40.0

    async def precheck(self, args: JobRef, ctx: ToolContext) -> str | None:
        manager = _manager(ctx)
        if manager is None:
            return _UNAVAILABLE
        found = await asyncio.to_thread(manager.find, args.job) if args.job else None
        if found is None:
            return f"There's no long job “{args.job}”; long_job_status lists them."
        if not found.alive:
            return f"It isn't running. {found.line}"
        return None

    async def describe_call(self, args: JobRef, ctx: ToolContext) -> str:
        manager = _manager(ctx)
        found = await asyncio.to_thread(manager.find, args.job) if manager and args.job else None
        if found is None:
            return "stop a long job"
        return f"stop the download of {Path(found.path).name}" if found.kind == "download" else f"stop “{found.title}”"

    async def run(self, args: JobRef, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        found = await asyncio.to_thread(manager.find, args.job) if manager and args.job else None
        if manager is None or found is None:
            return ToolResult.error(f"There's no long job “{args.job}”.")
        after = await manager.cancel(found.id)
        if after is None or after.alive:
            return ToolResult.error(f"I asked it to stop, but it's still going. {after.line if after else ''}".strip())
        return ToolResult.success(f"#{found.id}: {after.line}", data=_data(after))


TOOLS = [StartDownload, RunLongCommand, LongJobStatus, ResumeLongJob, CancelLongJob]
