"""Long jobs: downloads and long commands that run in a process of their own and are followed until they end.

The worker (``_long_job_runner``) does the job and writes its state; this module starts workers, judges how each job
really stands (``check``) and tells the user when one finishes, fails or dies (``JobManager``). The tools are in
``long_jobs``; the app builds the manager (``App._start_long_jobs``).

How a job stands is never taken from what she remembers starting, nor from the worker's word alone. ``check`` reads
the state file and also asks Windows whether that very process (its pid and start time) is alive, and whether its
heartbeat is fresh; for a download it reads the size of the file on disk. So "running" means a live process that
wrote a heartbeat seconds ago and, for a download, has data on disk; a job whose process is gone is "died" whatever
its file last said. A state file that can't be read for a moment (being replaced, held by the antivirus) is
"unreadable", never a verdict, and a death is only said once two checks in a row saw it. A download that died can
be resumed from its partial file.

Every job's files are in ``data/jobs``: ``<id>.job.json`` (what to do, written here), ``<id>.state.json`` (the
worker's), ``<id>.log`` (a command's output) and ``told.json`` (which outcomes she has already said, so a restart
doesn't repeat them and does say what ended while she was away). Everything here that changes those files runs on
the event loop; only reading the jobs' states runs in a thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from octavia.core.jsonfile import read_json
from octavia.tools.extensions import _long_job_runner as runner

log = logging.getLogger(__name__)

JOBS_DIR = "data/jobs"
TOLD_FILE = "told.json"
POLL_S = 5.0
START_GRACE_S = 30.0  # how long a job may take to write its first state before it counts as never started
STALE_S = 20.0  # a heartbeat older than this from a live process: it's stuck
STALL_S = 120.0  # a download with no new data for this long is stalled (said once, after STALL_TELL_S)
STALL_TELL_S = 600.0
MAX_KEPT = 40  # jobs whose files are kept; older finished ones are cleared away
READ_TRIES = 3
EXIT_WAIT_S = 15.0  # how long resuming waits for the job's last process to finish exiting
FINAL = ("done", "failed", "cancelled", "died")


@dataclass
class Check:
    """How a job stands, checked just now: ``status`` is one of starting, connecting (a download whose process is
    alive but has no data yet), running, stalled, stuck, unreadable (its state couldn't be read just now), done,
    failed, cancelled, died; ``line`` says it in words."""

    id: str
    title: str
    kind: str
    status: str
    line: str
    done: int = 0  # bytes on disk (a download) or of output (a command)
    total: int | None = None
    rate: float = 0.0
    error: str = ""
    path: str = ""
    resumable: bool = False
    output: str = ""

    @property
    def final(self) -> bool:
        return self.status in FINAL

    @property
    def alive(self) -> bool:
        return not self.final


def size_words(n: float | None) -> str:
    if n is None:
        return "an unknown size"
    if n < 1024**2:
        return f"{max(0, round(n / 1024))} KB"
    if n < 1024**3:
        return f"{round(n / 1024**2)} MB"
    return f"{n / 1024**3:.1f} GB"


def duration_words(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes // 60} h {minutes % 60} min"


def process_alive(pid: Any, started: Any) -> bool:
    """Whether process ``pid`` is running and is the one that started by ``started`` (epoch seconds; Windows reuses
    pids, so a program that got the same number later doesn't count). ``started`` None: any process with that pid."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        created = proc.create_time()
    except (psutil.Error, OSError):
        return False
    return not isinstance(started, (int, float)) or created <= started + 5.0


def same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def read_spec(folder: Path, job_id: str) -> dict[str, Any] | None:
    spec = read_json(runner.spec_path(folder, job_id))
    return spec if isinstance(spec, dict) else None


def read_state(folder: Path, job_id: str) -> dict[str, Any] | None:
    """The worker's state; {} when it hasn't written one, None when there is one that can't be read right now (it's
    being replaced, or the antivirus holds it). A few quick tries first."""
    path = runner.state_path(folder, job_id)
    for attempt in range(READ_TRIES):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if attempt < READ_TRIES - 1:
                time.sleep(0.02)
            continue
        return state if isinstance(state, dict) else None
    return None


def _disk_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def check(folder: Path, job_id: str, now: float | None = None, *, again: bool = True) -> Check | None:
    """How job ``job_id`` stands right now (see the module docstring); None when there's no such job.

    A worker writes how it ended before it exits, so a process found gone after its state read "running" may have
    finished in between (the state and the partial file are read first; on a busy PC the moment can be long): the
    state is read once more before calling it dead. Without that, a download that had just finished was "died at
    3 MB of 3 MB (100%)" (2026-10-29, in the full test suite)."""
    spec = read_spec(folder, job_id)
    if spec is None:
        return None
    now = time.time() if now is None else now
    kind, title = spec.get("kind", "command"), spec.get("title") or job_id
    dest = Path(spec["dest"]) if spec.get("dest") else None
    read = read_state(folder, job_id)
    state = read or {}
    status = state.get("status", "")
    total = state.get("total") if isinstance(state.get("total"), int) else None
    done = int(state.get("done") or 0)
    if dest is not None:  # the disk, not the worker's last word
        on_disk = _disk_size(dest if status == "done" else runner.part_path(dest, str(spec.get("token"))))
        done = on_disk if on_disk is not None else (done if status == "done" else 0)
    found = Check(job_id, title, kind, status or "starting", "", done, total, float(state.get("rate") or 0.0))
    found.error = str(state.get("error") or "")
    found.output = str(state.get("output") or "")
    found.path = str(dest or "")

    if read is None:
        found.status = "unreadable"
    elif status in ("done", "failed", "cancelled"):
        if status == "done" and dest is not None and _disk_size(dest) is None:
            found.status, found.error = "failed", f"It finished, but {dest} isn't there anymore."
    elif not state or status == "queued":  # no worker has reported in yet (``queued``: a resume carrying its sizes)
        found.status = "starting"
        if now - float(spec.get("created") or 0) > START_GRACE_S:
            crash = _crash_text(folder, job_id)
            found.status = "died"
            found.error = "Its process never started." + (f" It said: {crash}" if crash else "")
    elif not process_alive(state.get("pid"), state.get("started")):
        if again:
            return check(folder, job_id, now, again=False)
        found.status = "died"
        found.error = "Its process is gone: it was stopped, crashed, or the PC restarted."
    elif now - float(state.get("heartbeat") or 0) > STALE_S:
        found.status = "stuck"
    elif kind == "download" and now - float(state.get("progress_at") or 0) > STALL_S:
        found.status = "stalled"
    elif kind == "download" and found.done == 0:
        found.status = "connecting"  # alive, but no data yet: not "running" until something arrives
    else:
        found.status = "running"
    found.resumable = kind == "download" and found.status in ("failed", "cancelled", "died") and dest is not None
    found.line = _line(found, spec, state, now)
    return found


def _crash_text(folder: Path, job_id: str) -> str:
    try:
        text = (folder / f"{job_id}.crash.log").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text.splitlines()[-1][:300] if text else ""


def _line(c: Check, spec: dict[str, Any], state: dict[str, Any], now: float) -> str:
    """Where it stands, in words she can say."""
    what = f"The download of {Path(c.path).name}" if c.kind == "download" else f"The command “{c.title}”"
    started = float(state.get("started") or spec.get("created") or now)
    progress = ""
    if c.kind == "download":
        progress = f"{size_words(c.done)} of {size_words(c.total)}" if c.total else f"{size_words(c.done)} so far"
        if c.total:
            progress += f" ({100 * c.done // max(1, c.total)}%)"
    if c.status == "starting":
        return f"{what} is starting; its process hasn't reported in yet."
    if c.status == "unreadable":
        return f"{what}: I couldn't read how it's doing just now; ask me again in a moment."
    if c.status == "connecting":
        retry = f" It's retrying after: {state['retrying']}." if state.get("retrying") else ""
        return f"{what} is connecting: its process is alive but no data has arrived yet.{retry}"
    if c.status == "running":
        if c.kind != "download":
            return f"{what} is running (for {duration_words(now - started)})."
        speed = f", at {c.rate / 1024**2:.1f} MB/s" if c.rate > 0 else ""
        left = ""
        if c.total and c.rate > 0:
            left = f", about {duration_words((c.total - c.done) / c.rate)} left"
        retry = f" It reconnected after: {state['retrying']}." if state.get("retrying") else ""
        return f"{what} is running: {progress}{speed}{left}.{retry}"
    if c.status == "stalled":
        quiet = duration_words(now - float(state.get("progress_at") or started))
        why = f" It's retrying after: {state['retrying']}." if state.get("retrying") else ""
        return f"{what} is stalled: no new data for {quiet}, at {progress}.{why}"
    if c.status == "stuck":
        return f"{what} looks stuck: its process is there but hasn't reported in for a while."
    finished = float(state.get("finished") or now)
    if c.status == "done":
        took = duration_words(finished - started)
        if c.kind == "download":
            return f"{what} finished: {size_words(c.done)} in {took}, saved as {c.path}."
        return f"{what} finished after {took}."
    if c.status == "cancelled":
        return f"{what} was cancelled at {progress}." if progress else f"{what} was cancelled."
    at = f" at {progress}" if progress else ""
    verb = "died" if c.status == "died" else "failed"
    return f"{what} {verb}{at}. {c.error}".strip()


Announce = Callable[[str, str], None]  # (said out loud, note for the brain's next turn)
Launch = Callable[[Path, str], None]


class JobManager:
    """Starts long jobs, finds them by number or name, and says when each one ends (``watch``). Call it on the
    event loop: what it says goes straight to the assistant."""

    def __init__(
        self,
        folder: Path,
        announce: Announce,
        *,
        launch: Launch = runner.launch,
        clock: Callable[[], float] = time.time,
        poll_s: float = POLL_S,
    ) -> None:
        self.folder = folder
        self._announce = announce
        self._launch = launch
        self._clock = clock
        self._poll_s = poll_s
        self._watcher: asyncio.Task[None] | None = None
        self._told: dict[str, str] = self._load_told()  # job id -> the outcome she said ("stalled" or a final one)
        self._forgotten: set[str] = set()  # ids whose outcome was taken back (resumed): not merged back in
        self._seen_dead: set[str] = set()  # jobs one check found dead: the next one confirms it
        # Told each round of ``watch``'s checks, on the loop (her status panel): what's checked anyway, never read again.
        self.listeners: list[Callable[[list[Check]], None]] = []

    # ------------------------------------------------------------------ the list

    def ids(self) -> list[str]:
        """Every job's id, oldest first."""
        found = []
        for path in self.folder.glob("*.job.json"):
            job_id = path.name.removesuffix(".job.json")
            if job_id.isdigit():
                found.append(job_id)
        return sorted(found, key=int)

    def checks(self) -> list[Check]:
        now = self._clock()
        return [c for c in (check(self.folder, i, now) for i in self.ids()) if c is not None]

    def find(self, ref: str) -> Check | None:
        """By number ("3", "#3") or by name (the latest whose title or file name contains it)."""
        ref = ref.strip()
        if ref.lstrip("#").isdigit():
            return check(self.folder, str(int(ref.lstrip("#"))), self._clock())
        wanted = ref.casefold()
        matches = [c for c in self.checks() if wanted and (wanted in c.title.casefold() or wanted in c.path.casefold())]
        return matches[-1] if matches else None

    def active_for(self, dest: Path) -> Check | None:
        """A job that's still going and downloads to ``dest`` (however the path is written)."""
        return next((c for c in self.checks() if c.alive and c.path and same_path(c.path, dest)), None)

    # ------------------------------------------------------------------ starting and stopping

    def _new_id(self) -> str:
        return str(max((int(i) for i in self.ids()), default=0) + 1)

    async def start_download(self, url: str, dest: Path, title: str = "") -> str:
        spec = {"kind": "download", "url": url, "dest": str(dest), "title": title or dest.name}
        return await self._start({**spec, "token": os.urandom(4).hex()})

    async def start_command(self, command: str, cwd: Path, title: str, time_limit_s: float) -> str:
        spec = {"kind": "command", "command": command, "cwd": str(cwd), "title": title, "time_limit_s": time_limit_s}
        return await self._start(spec)

    async def _start(self, spec: dict[str, Any]) -> str:
        self.folder.mkdir(parents=True, exist_ok=True)
        job_id = self._new_id()
        runner.write_state(runner.spec_path(self.folder, job_id), {**spec, "id": job_id, "created": self._clock()})
        self._clear_away()
        await self._run_launcher(job_id)
        log.info("Long job %s started: %s", job_id, spec.get("title"))
        return job_id

    async def _run_launcher(self, job_id: str) -> None:
        """Start the worker off the event loop. A launcher that fails is written down as the job's failure; one that
        is merely slow isn't (the worker may still start, and ``check`` says if it never does)."""
        try:
            await asyncio.to_thread(self._launch, self.folder, job_id)
        except subprocess.TimeoutExpired:
            log.warning("Long job %s: its launcher is slow to start", job_id)
        except Exception as exc:
            log.warning("Couldn't start long job %s", job_id, exc_info=True)
            runner.write_state(
                runner.state_path(self.folder, job_id),
                {"status": "failed", "error": f"Its process couldn't be started: {exc}", "finished": self._clock()},
            )

    async def resume(self, job_id: str) -> str | None:
        """Start a download that died, failed or was cancelled again, from what its partial file holds. Returns why
        it can't, or None once it's started."""
        from octavia.tools.extensions.long_jobs import destination_problem

        spec = read_spec(self.folder, job_id)
        found = check(self.folder, job_id, self._clock())
        if spec is None or found is None or spec.get("kind") != "download":
            return "Only a download can be resumed."
        state = read_state(self.folder, job_id)
        if state is None:
            return "I couldn't read how it stands just now; try again in a moment."
        if found.alive:
            return f"It's still going. {found.line}"
        # Its worker wrote how it ended but may still be exiting (a moment, more on a busy PC): never two at once.
        deadline = time.monotonic() + EXIT_WAIT_S
        while process_alive(state.get("pid"), state.get("started")):
            if time.monotonic() >= deadline:
                return f"Its last process is still exiting; try again in a moment. {found.line}"
            await asyncio.sleep(0.2)
        problem = destination_problem(Path(spec["dest"]))
        if problem:
            return problem
        runner.cancel_path(self.folder, job_id).unlink(missing_ok=True)
        carried = {key: state[key] for key in runner.CARRIED if state.get(key) is not None}
        runner.write_state(runner.state_path(self.folder, job_id), {"status": "queued", **carried})
        runner.write_state(runner.spec_path(self.folder, job_id), {**spec, "created": self._clock()})
        self._told.pop(job_id, None)
        self._forgotten.add(job_id)
        self._seen_dead.discard(job_id)
        self._save_told()
        await self._run_launcher(job_id)
        return None

    async def wait_for_start(self, job_id: str, timeout_s: float = 12.0) -> Check | None:
        """Wait until the job has shown it works (a download has data on disk, a command is running) or has ended;
        give up after ``timeout_s`` and report where it stands then."""
        deadline = time.monotonic() + timeout_s
        while True:
            found = await asyncio.to_thread(check, self.folder, job_id, self._clock())
            if found is None or found.final or found.status == "running":
                return found
            if time.monotonic() >= deadline:
                return found
            await asyncio.sleep(0.25)

    async def cancel(self, job_id: str, timeout_s: float = 15.0) -> Check | None:
        """Ask the worker to stop, and end its processes if it hasn't within ``timeout_s``. It only counts as
        cancelled once they're gone."""
        runner.cancel_path(self.folder, job_id).touch()
        deadline = time.monotonic() + timeout_s
        found = None
        while time.monotonic() < deadline:
            found = await asyncio.to_thread(check, self.folder, job_id, self._clock())
            if found is None or found.final:
                break
            await asyncio.sleep(0.25)
        else:
            state = read_state(self.folder, job_id) or {}
            procs = []
            pairs = [(state.get("child_pid"), state.get("child_started")), (state.get("pid"), state.get("started"))]
            for pid, started in pairs:
                if started is not None and process_alive(pid, started):  # never a stranger that got its pid
                    with contextlib.suppress(psutil.Error):
                        proc = psutil.Process(pid)
                        procs += [*proc.children(recursive=True), proc]
            for proc in procs:
                with contextlib.suppress(psutil.Error):
                    proc.kill()
            _, still = await asyncio.to_thread(psutil.wait_procs, procs, 5.0)
            if still:
                return await asyncio.to_thread(check, self.folder, job_id, self._clock())
            if not process_alive(state.get("pid"), state.get("started")) and state:
                runner.write_state(
                    runner.state_path(self.folder, job_id),
                    {**state, "status": "cancelled", "finished": self._clock(), "rate": 0.0},
                )
            found = await asyncio.to_thread(check, self.folder, job_id, self._clock())
        if found is not None and found.status == "cancelled":
            self._told[job_id] = "cancelled"  # they asked for it: nothing to announce
            self._save_told()
        return found

    # ------------------------------------------------------------------ telling the user

    def _load_told(self) -> dict[str, str]:
        told = read_json(self.folder / TOLD_FILE, "the long jobs already reported")
        return {str(k): str(v) for k, v in told.items()} if isinstance(told, dict) else {}

    def _save_told(self) -> None:
        """Merged with what's in the file (``octavia chat`` beside her keeps one too), less what was taken back."""
        merged = {k: v for k, v in self._load_told().items() if k not in self._forgotten}
        merged.update(self._told)
        known = set(self.ids())
        self._told = {k: v for k, v in merged.items() if k in known}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            runner.write_state(self.folder / TOLD_FILE, self._told)
        except OSError:
            log.warning("Couldn't save which long jobs were reported", exc_info=True)

    def poll(self, checks: list[Check] | None = None) -> list[Check]:
        """Say what's new: each job that ended (once; a death only once two checks in a row saw it), a job said dead
        that turned out fine after all, and a download that's been stalled a while (once). ``checks``: the jobs as
        just checked (``checks()``, read off the event loop). Returns what was said."""
        said: list[Check] = []
        now = self._clock()
        for found in self.checks() if checks is None else checks:
            told = self._told.get(found.id)
            if found.status == "died" and found.id not in self._seen_dead:
                self._seen_dead.add(found.id)
                continue
            if found.status != "died":
                self._seen_dead.discard(found.id)
            if found.final and told not in FINAL:
                self._tell(found)
            elif found.final and told == "died" and found.status == "done":
                self._announce(f"Correction: {self._spoken(found)}", f"Long job #{found.id} finished after all.")
            elif found.status == "stalled" and told is None:
                state = read_state(self.folder, found.id) or {}
                if now - float(state.get("progress_at") or now) < STALL_TELL_S:
                    continue
                self._announce(found.line, f"Long job #{found.id}: {found.line} long_job_status checks it.")
                self._told[found.id] = "stalled"
                said.append(found)
                continue
            else:
                continue
            self._told[found.id] = found.status
            said.append(found)
        if said:
            self._save_told()
        return said

    @staticmethod
    def _spoken(found: Check) -> str:
        if found.status == "done" and found.kind == "download":
            return found.line.split(", saved as")[0] + "."
        return found.line

    def _tell(self, found: Check) -> None:
        line, note = self._spoken(found), f"Long job #{found.id} ended: {found.line}"
        if found.resumable:
            line += " I can resume it from where it stopped."
            note += " resume_long_job picks it up from its partial file."
        if found.output:
            note += f" Its last output: {found.output[-500:]}"
        log.info("Long job %s %s: %s", found.id, found.status, found.line)
        self._announce(line, note)

    async def watch(self) -> None:
        """Check every few seconds until cancelled: the reading in a thread, the telling on the loop."""
        while True:
            try:
                checks = await asyncio.to_thread(self.checks)
                self.poll(checks)
                for listener in list(self.listeners):
                    try:
                        listener(checks)
                    except Exception:
                        log.warning("A long jobs listener failed", exc_info=True)
            except Exception:
                log.warning("Couldn't check the long jobs", exc_info=True)
            await asyncio.sleep(self._poll_s)

    def start_watching(self) -> None:
        if self._watcher is None:
            self._watcher = asyncio.get_running_loop().create_task(self.watch(), name="long-jobs")

    async def close(self) -> None:
        """Stop watching. The jobs themselves keep running: that's the point of them."""
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
            self._watcher = None

    def _clear_away(self) -> None:
        """Remove the files of the oldest finished jobs she has already reported, beyond ``MAX_KEPT``."""
        ids = self.ids()
        for job_id in ids[: max(0, len(ids) - MAX_KEPT)]:
            if self._told.get(job_id) not in FINAL:
                continue
            for path in self.folder.glob(f"{job_id}.*"):
                if path.name.split(".", 1)[0] == job_id:
                    with contextlib.suppress(OSError):
                        path.unlink()
