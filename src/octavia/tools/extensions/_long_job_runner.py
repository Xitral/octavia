"""The process that carries out one long job (``_long_jobs``): a download or a long command, on its own.

She used to start long downloads with ``run_command`` (a BITS transfer, a hidden ``curl``). Those were children of
her own process tree, nobody watched them, and they died quietly: a 7 GB model download vanished halfway and she said
another was "running on its own" while its file sat at 0 bytes. A job here instead:

* runs in a process of its own, started through a launcher that exits at once, so it isn't in her process tree (the
  supervisor ends her tree's Python processes when it restarts her) and breaks away from her job object where
  Windows allows it. It keeps going while she restarts, and she finds it again when she's back;
* writes what it's doing to ``<id>.state.json`` beside its spec: a heartbeat every couple of seconds, its pid, the
  bytes it has and expects, its speed, the error it's retrying after, and how it ended. Every write is made under one
  lock, and the heartbeat thread is stopped and joined before the last one, so a late heartbeat can never turn
  "done" back into "running". She never takes a job's word for it alone: ``_long_jobs.check`` also asks Windows
  whether that very process is still alive and looks at the file on disk;
* downloads into a partial file of its own (``<file>.<token>.part``, the token is the job's) and resumes from it
  after a dropped connection (a few tries, waiting longer each time), so a dead job can be picked up where it
  stopped. A resume asks for the rest only if the file on the server is unchanged (``If-Range`` with its ETag or
  date) and takes the bytes exactly as sent (no compression), so a changed file is started over, never spliced. The
  file gets its real name only once it's complete, never over another file, marked as from the internet;
* checks its destination against the same rules the tool did (``long_jobs.destination_problem``), so a spec that
  didn't come from the tool can't write anywhere else;
* stops when ``<id>.cancel`` appears.

Run as ``python -m octavia.tools.extensions._long_job_runner spawn|run <folder> <id>``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

MODULE = "octavia.tools.extensions._long_job_runner"
HEARTBEAT_S = 2.0
CHUNK = 64 * 1024  # small enough that a slow link still shows progress every second or so
MAX_TRIES = 8  # failed connections in a row (without new data) before a download gives up
BACKOFF_S = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
CONNECT_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 60.0
OUTPUT_TAIL = 2000  # characters of a command's output kept in its state
WRITE_TRIES = 20  # Windows refuses to replace a file another process is reading at that moment
FINAL_WRITE_TRIES = 400  # the last write says how it ended: worth waiting up to 20 s for
LAUNCH_TIMEOUT_S = 60.0
# Kept from an earlier run of the same job, so a resume knows the size and version it was downloading.
CARRIED = ("total", "etag", "last_modified", "tries")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)


def spec_path(folder: Path, job_id: str) -> Path:
    return folder / f"{job_id}.job.json"


def state_path(folder: Path, job_id: str) -> Path:
    return folder / f"{job_id}.state.json"


def cancel_path(folder: Path, job_id: str) -> Path:
    return folder / f"{job_id}.cancel"


def log_path(folder: Path, job_id: str) -> Path:
    return folder / f"{job_id}.log"


def part_path(dest: Path, token: str) -> Path:
    """The job's own partial file: another job downloading to the same name never resumes from it."""
    return dest.with_name(f"{dest.name}.{token}.part")


def write_state(path: Path, data: dict[str, Any], tries: int = WRITE_TRIES) -> None:
    """All at once through a temporary file of this thread's own. Windows won't replace a file while another
    process has it open (she may be reading it), so it tries again for a moment before giving up."""
    temp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for attempt in range(tries):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == tries - 1:
                with contextlib.suppress(OSError):
                    temp.unlink()
                raise
            time.sleep(0.05)


# --------------------------------------------------------------------------- starting one, outside her tree


def _popen_detached(command: list[str], cwd: Path, log: Path | None = None) -> subprocess.Popen[bytes]:
    """Hidden, in a process group of its own, and out of the caller's job object when Windows allows it (a job that
    kills its processes when it closes would otherwise take this one with it). It runs in ``cwd`` (the jobs folder),
    not her project folder, so it doesn't hold that folder open for hours."""
    out = log.open("ab") if log is not None else subprocess.DEVNULL
    common: dict[str, Any] = {"cwd": str(cwd), "stdin": subprocess.DEVNULL, "stdout": out, "stderr": out}
    try:
        if os.name != "nt":
            return subprocess.Popen(command, start_new_session=True, close_fds=True, **common)
        hidden = _NO_WINDOW | _NEW_GROUP
        try:
            return subprocess.Popen(command, creationflags=hidden | _BREAKAWAY, **common)
        except PermissionError:  # the job doesn't allow breaking away
            return subprocess.Popen(command, creationflags=hidden, **common)
    finally:
        if log is not None:
            out.close()  # type: ignore[union-attr]


def launch(folder: Path, job_id: str) -> None:
    """Start the job's worker. A launcher process starts the worker and exits, so the worker's parent is gone: it
    isn't one of her processes for anything that ends her process tree. Blocks while the launcher starts (two
    Python start-ups): call it off the event loop. Raises OSError when the launcher can't run or fails, and
    subprocess.TimeoutExpired when it's still going after ``LAUNCH_TIMEOUT_S`` (the worker may yet start)."""
    launcher = _popen_detached([sys.executable, "-m", MODULE, "spawn", str(folder), job_id], folder)
    launcher.wait(timeout=LAUNCH_TIMEOUT_S)
    if launcher.returncode != 0:
        raise OSError(f"the job's launcher exited with code {launcher.returncode}")


def _spawn(folder: Path, job_id: str) -> int:
    # Anything the worker prints before it can write its state (an import error) goes here, for its error message.
    _popen_detached([sys.executable, "-m", MODULE, "run", str(folder), job_id], folder, folder / f"{job_id}.crash.log")
    return 0


# --------------------------------------------------------------------------- the worker


class Cancelled(Exception):
    pass


class GiveUp(Exception):
    """A failure that trying again won't fix (a 404, a full disk, a file in the way)."""


class _Retry(Exception):
    pass


class _StartOver(Exception):
    """The file on the server isn't the one the partial file holds the start of."""


class Worker:
    def __init__(self, folder: Path, job_id: str) -> None:
        self.folder = folder
        self.id = job_id
        self.spec: dict[str, Any] = json.loads(spec_path(folder, job_id).read_text(encoding="utf-8"))
        self._state_file = state_path(folder, job_id)
        self._cancel_file = cancel_path(folder, job_id)
        self._lock = threading.Lock()
        self._stop_beats = threading.Event()
        self._beats: threading.Thread | None = None
        self._final = False
        now = time.time()
        earlier: dict[str, Any] = {}
        with contextlib.suppress(OSError, ValueError):
            earlier = json.loads(self._state_file.read_text(encoding="utf-8"))
        self.state: dict[str, Any] = {
            "id": job_id,
            "status": "running",
            "pid": os.getpid(),
            "started": now,
            "heartbeat": now,
            "progress_at": now,  # when new data last arrived (a download) or the job last showed life
            "done": 0,
            "total": None,
            "etag": "",
            "last_modified": "",
            "rate": 0.0,
            "tries": 0,
            "retrying": "",  # the error it's waiting out before trying again
            "error": "",
            "finished": None,
        }
        if isinstance(earlier, dict):
            self.state.update({key: earlier[key] for key in CARRIED if earlier.get(key) is not None})

    # ------------------------------------------------------------------ state

    def update(self, *, final: bool = False, **changes: Any) -> None:
        """Change the state and write it, all under the lock: writes never cross, and none comes after the final one."""
        with self._lock:
            if self._final:
                return
            self.state.update(changes)
            self.state["heartbeat"] = time.time()
            self._final = final
            with contextlib.suppress(OSError):
                write_state(self._state_file, self.state, FINAL_WRITE_TRIES if final else WRITE_TRIES)

    def _beat(self) -> None:
        while not self._stop_beats.wait(HEARTBEAT_S):
            self.update()

    def cancelled(self) -> bool:
        return self._cancel_file.exists()

    def check_cancel(self) -> None:
        if self.cancelled():
            raise Cancelled

    def run(self) -> int:
        self.update()
        self._beats = threading.Thread(target=self._beat, name="heartbeat", daemon=True)
        self._beats.start()
        try:
            if self.spec["kind"] == "download":
                self.download()
            else:
                self.command()
        except Cancelled:
            self.finish("cancelled")
        except GiveUp as exc:
            self.finish("failed", error=str(exc))
        except Exception as exc:  # anything unexpected still ends in a state she can read
            self.finish("failed", error=f"{type(exc).__name__}: {exc}")
        return 0

    def finish(self, status: str, **changes: Any) -> None:
        self._stop_beats.set()
        if self._beats is not None and self._beats is not threading.current_thread():
            self._beats.join()
        self.update(final=True, status=status, finished=time.time(), retrying="", rate=0.0, **changes)

    def wait(self, seconds: float) -> None:
        """Sleep, but stop at once when the job is cancelled."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check_cancel()
            time.sleep(min(0.25, max(0.0, end - time.monotonic())))
        self.check_cancel()

    # ------------------------------------------------------------------ a download

    def download(self) -> None:
        import httpx

        from octavia.tools.extensions.long_jobs import destination_problem

        dest = Path(self.spec["dest"])
        problem = destination_problem(dest)
        if problem:
            raise GiveUp(problem)
        part = part_path(dest, str(self.spec["token"]))
        timeout = httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S)
        # The bytes exactly as stored: a compressed answer's sizes and ranges count the compressed bytes.
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) octavia-download/1",
            "Accept-Encoding": "identity",
        }
        failures = 0
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            while True:
                self.check_cancel()
                have = part.stat().st_size if part.exists() else 0
                self.update(done=have)
                try:
                    finished = self._fetch(client, part, have)
                except _StartOver as exc:
                    part.unlink(missing_ok=True)
                    self.update(total=None, etag="", last_modified="", done=0, retrying=_why(exc))
                    continue
                except (httpx.TransportError, httpx.DecodingError, _Retry) as exc:
                    gained = (part.stat().st_size if part.exists() else 0) > have
                    failures = 1 if gained else failures + 1
                    if failures >= MAX_TRIES:
                        raise GiveUp(f"The connection kept failing ({_why(exc)}), {failures} times in a row.") from exc
                    delay = BACKOFF_S[min(failures - 1, len(BACKOFF_S) - 1)]
                    self.update(tries=self.state["tries"] + 1, retrying=_why(exc), rate=0.0)
                    self.wait(delay)
                    continue
                if finished:
                    break
        size = part.stat().st_size
        total = self.state["total"]
        if total is not None and size != total:
            raise GiveUp(f"The file came out at {size} bytes instead of {total}.")
        try:
            part.rename(dest)  # on Windows this fails when there's a file there: it's never replaced
        except FileExistsError as exc:
            raise GiveUp(f"A file appeared at {dest} while I was downloading, so I left mine as {part.name}.") from exc
        _mark_from_internet(dest, self.spec["url"])
        self.finish("done", done=size, path=str(dest))

    def _fetch(self, client: Any, part: Path, have: int) -> bool:
        """One connection: True when the whole file is in ``part``."""
        headers: dict[str, str] = {}
        if have:
            headers["Range"] = f"bytes={have}-"
            # Only the rest of that very version: a changed file comes back whole (200), and is started over.
            etag = str(self.state.get("etag") or "")
            validator = etag if etag and not etag.startswith("W/") else str(self.state.get("last_modified") or "")
            if validator:
                headers["If-Range"] = validator
        with client.stream("GET", self.spec["url"], headers=headers) as response:
            code = response.status_code
            if code == 416 and have:  # nothing after ``have``
                _, total = _content_range(response.headers.get("content-range", ""))
                known = total if total is not None else self.state["total"]
                if known == have:
                    self.update(total=have)
                    return True
                raise _StartOver("the partial file is longer than the file on the server")
            if code in (408, 425, 429) or code >= 500:
                raise _Retry(f"the server answered {code}")
            if code >= 400:
                raise GiveUp(f"The server answered {code} {response.reason_phrase}.".replace(" .", "."))
            if code == 206:
                start, total = _content_range(response.headers.get("content-range", ""))
                if start != have:
                    raise _Retry("the server resumed at the wrong place")
                if total is None:
                    total = self.state["total"]
                elif self.state["total"] not in (None, total):
                    raise _StartOver("the file on the server changed size")
                mode = "ab"
            else:  # 200: the whole file, from the start (a server that ignores ranges, or a new version)
                have, mode = 0, "wb"
                length = response.headers.get("content-length")
                total = int(length) if length and length.isdigit() else None
            if total is not None:
                free = shutil.disk_usage(part.parent).free
                if total - have > free:
                    raise GiveUp(
                        f"There isn't enough space on the drive: it needs {_size(total - have)} more and has "
                        f"{_size(free)}."
                    )
            self.update(
                total=total,
                done=have,
                retrying="",
                etag=response.headers.get("etag", ""),
                last_modified=response.headers.get("last-modified", ""),
            )
            done, window_start, window_bytes = have, time.monotonic(), 0
            with part.open(mode) as out:
                for chunk in response.iter_raw(CHUNK):
                    out.write(chunk)
                    done += len(chunk)
                    window_bytes += len(chunk)
                    elapsed = time.monotonic() - window_start
                    if elapsed >= 1.0:
                        out.flush()
                        self.update(done=done, rate=window_bytes / elapsed, progress_at=time.time())
                        window_start, window_bytes = time.monotonic(), 0
                        self.check_cancel()
            self.update(done=done, progress_at=time.time())
            if total is not None and done < total:
                raise _Retry("the connection closed early")
            return True

    # ------------------------------------------------------------------ a command

    def command(self) -> None:
        import psutil

        from octavia.tools.extensions.terminal import _PREAMBLE, _child_env, _kill_tree, _shell

        script = base64.b64encode((_PREAMBLE + self.spec["command"]).encode("utf-16-le")).decode("ascii")
        out_file = log_path(self.folder, self.id)
        limit = float(self.spec.get("time_limit_s") or 6 * 3600)
        with out_file.open("ab") as out:
            proc = subprocess.Popen(
                [_shell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", script],
                cwd=self.spec.get("cwd") or None,
                env=_child_env(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
            )
        child_started = None
        with contextlib.suppress(psutil.Error):
            child_started = psutil.Process(proc.pid).create_time()
        self.update(child_pid=proc.pid, child_started=child_started)
        started, seen = time.monotonic(), 0
        while proc.poll() is None:
            if self.cancelled():
                _kill_tree(proc.pid)
                proc.wait()
                raise Cancelled
            if time.monotonic() - started > limit:
                _kill_tree(proc.pid)
                proc.wait()
                raise GiveUp(f"It was still running after {round(limit / 60)} minutes, so I stopped it.")
            size = out_file.stat().st_size if out_file.exists() else 0
            if size != seen:  # new output: it's doing something
                seen = size
                self.update(done=size, progress_at=time.time())
            time.sleep(0.5)
        tail = _tail(out_file)
        if proc.returncode != 0:
            self.finish(
                "failed", exit_code=proc.returncode, output=tail, error=f"It exited with code {proc.returncode}."
            )
        else:
            self.finish("done", exit_code=0, output=tail)


def _mark_from_internet(path: Path, url: str) -> None:
    """The mark Windows puts on what a browser downloads (``Zone.Identifier``), so opening it warns like any other
    download from the internet would."""
    if os.name != "nt":
        return
    with contextlib.suppress(OSError):
        Path(f"{path}:Zone.Identifier").write_text(f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={url}\r\n", encoding="utf-8")


def _why(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:200]


def _content_range(value: str) -> tuple[int, int | None]:
    """``bytes 100-199/1000`` → (100, 1000), ``bytes */1000`` → (-1, 1000); the total is None when the server
    doesn't say ("/*")."""
    _, _, rest = value.partition(" ")
    span, _, total = rest.partition("/")
    first = span.split("-")[0]
    return (int(first) if first.isdigit() else -1), (int(total) if total.strip().isdigit() else None)


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - OUTPUT_TAIL * 2))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return text.replace("\r\n", "\n").strip()[-OUTPUT_TAIL:]


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("spawn", "run"):
        print(f"usage: python -m {MODULE} spawn|run <folder> <id>", file=sys.stderr)
        return 2
    folder, job_id = Path(argv[2]), argv[3]
    if argv[1] == "spawn":
        return _spawn(folder, job_id)
    return Worker(folder, job_id).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
