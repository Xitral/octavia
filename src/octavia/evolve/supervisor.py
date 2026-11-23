"""The small parent process that keeps the assistant running, restarts it into updates and undoes a bad one.

The assistant runs as a child of ``supervise``. It exits with ``RESTART_EXIT_CODE`` to be restarted into
freshly installed code (or an undo). Every install writes a health marker naming the update; the running
assistant clears it once the new code has run fine for a while. If a child that was started after the marker
was written dies while the marker still exists, the update broke startup: the supervisor reverts its merge
commit, leaves a restart marker so the next start can tell the user what happened, and starts the old code
again. A child started before the marker ran the old code, so its crash leaves the marker for the next one.
Ordinary crashes are restarted with a growing delay, and a crash loop ends the supervisor instead of spinning.

Everything the supervisor runs is imported before the first child starts, and only the child loads updated
code, so an update that breaks the assistant can't break the process that repairs it. The child gets the
supervisor's pid (``SUPERVISOR_PID_ENV``) and shuts itself down cleanly if the supervisor disappears (the
console closed, killed in Task Manager), so it never lingers with the microphone open. A job object would do
that too, but everything the assistant starts inherits a job: closing the console would kill the browser it
opened for you. For the same reason the child takes the supervision variables out of its environment at
startup (``claim_supervision``), and ``SpawnedChild.kill`` ends only the assistant's own processes.

Nothing here can restart the supervisor itself. In the background it leaves ``RunningMarker`` as it starts and takes it
away when it ends on purpose, so ``evolve/revive.py`` (a scheduled task) can tell a death from a decision.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import psutil

from octavia.evolve.git import Author, Git, GitError
from octavia.evolve.models import (
    RESTART_EXIT_CODE,
    EvolvePaths,
    HealthMarker,
    QuitMarker,
    RestartMarker,
    clear_marker,
    read_marker,
    self_improvement_author,
    side_worktree_name,
    write_marker,
)
from octavia.evolve.revive import mark_running, stopped_on_purpose

log = logging.getLogger("octavia.supervisor")

SUPERVISED_ENV = "OCTAVIA_SUPERVISED"  # "1" in the child's environment
SUPERVISOR_PID_ENV = "OCTAVIA_SUPERVISOR_PID"  # the child stops when this process is gone
DEFAULT_AUTHOR: Author = self_improvement_author("the assistant")  # callers pass the configured name
STOP_GRACE_S = 20.0  # after Ctrl+C, how long the child gets to shut down cleanly before it's ended
MAX_BACKOFF_S = 30.0
LONG_RUN_RESTART_S = 2.0  # delay before restarting after a crash that followed a long, healthy run
# The venv's python.exe is a launcher for the real interpreter; it exits with 101 ("Unable to create process") at
# once when Windows won't start that interpreter, as when Bitdefender's Advanced Threat Defense briefly blocks
# python.exe over something else (a test run's workers did, 2026-10-25). The assistant never ran, so that's no
# crash of hers: no update is undone over it, and it's tried again for a while instead of given up after three.
LAUNCHER_FAILED_EXIT_CODE = 101
LAUNCHER_FAILED_MAX_S = 60.0  # a 101 this soon after the start is the launcher's (antivirus may hold it a while)
LAUNCHER_RETRY_FOR_S = 600.0  # how long to keep trying to launch her before giving up

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_POLL_S = 0.1
_KILL_WAIT_S = 10.0

# What the assistant runs for itself, by executable name (lowercase, without ".exe"). Pythons are followed down
# one level at a time (the venv's python.exe is a launcher for the real interpreter, and the gate's test runs
# are pythons too); everything under one of these tools is the assistant's as well (the builder's shell and the
# tests it runs). Anything else under the assistant was opened for the user (a browser, an editor, a terminal)
# and is left running, together with whatever it started.
_OWN_TOOLS = frozenset({"claude", "node", "git", "ruff", "uv", "conhost"})

# Set by claim_supervision: (supervised, supervisor pid).
_claimed: tuple[bool, int | None] | None = None


class ChildProcess(Protocol):
    """A started assistant, as ``supervise`` uses it (``spawn`` returns one; tests pass fakes).

    ``wait`` behaves like ``Popen.wait`` (raises ``subprocess.TimeoutExpired`` when ``timeout`` runs out).
    Optional, looked up with ``getattr``: ``kill()`` ends the assistant now, ``close()`` releases resources
    once the child has exited.
    """

    def wait(self, timeout: float | None = None) -> int: ...


def claim_supervision() -> None:
    """Take the supervision variables out of this process's environment, keeping their values.

    ``octavia run`` calls this first thing. Everything the assistant starts (an editor, a terminal) would
    otherwise inherit them, and a ``octavia`` started from there would believe it's the supervised child: it
    would skip the single-instance lock and run as a second copy, or shut down because "its" supervisor is
    gone. Only the first call takes them; ``is_supervised`` and ``supervisor_pid`` then return what it found.
    """
    global _claimed
    if _claimed is not None:
        return
    flag = os.environ.pop(SUPERVISED_ENV, None)
    pid = os.environ.pop(SUPERVISOR_PID_ENV, None)
    _claimed = (flag == "1", _parse_pid(pid))


def is_supervised() -> bool:
    """Whether this process was started by ``supervise`` (so it may exit to be restarted)."""
    if _claimed is not None:
        return _claimed[0]
    return os.environ.get(SUPERVISED_ENV) == "1"


def supervisor_pid() -> int | None:
    """The supervisor's pid, in a supervised child."""
    if _claimed is not None:
        return _claimed[1]
    return _parse_pid(os.environ.get(SUPERVISOR_PID_ENV))


def _parse_pid(raw: str | None) -> int | None:
    try:
        return int(raw or "")
    except ValueError:
        return None


def supervise(
    child_command: list[str],
    root: Path,
    *,
    background: bool = False,
    spawn: Callable[[list[str], dict[str, str], bool], ChildProcess] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    rollback: Callable[[Path], str | None] | None = None,
    max_quick_crashes: int = 3,
    quick_crash_s: float = 60.0,
) -> int:
    """Run ``child_command`` until it exits normally, restarting it as needed; returns the exit code to use.

    ``spawn(command, env, background)`` starts the child (default ``spawn_child``); ``rollback(root)``
    reverts the update named by the health marker and returns the new HEAD, or None if it couldn't
    (default ``rollback_update``). A crash within ``quick_crash_s`` of starting counts towards
    ``max_quick_crashes``; reaching it gives up and returns the child's exit code. ``clock`` measures how
    long a child ran; ``wall_clock`` (``time.time``) dates each start, to compare with the health marker.
    """
    spawn = spawn or spawn_child
    rollback = rollback or rollback_update
    paths = EvolvePaths(Path(root))
    env = {**os.environ, SUPERVISED_ENV: "1", SUPERVISOR_PID_ENV: str(os.getpid())}
    quick_crashes = 0
    launch_failing_since: float | None = None  # clock() of the first of the current run of launcher failures
    launch_failures = 0
    child: ChildProcess | None = None
    if background:  # with a console, closing its window is how you stop her
        mark_running(paths, child_command)
    try:
        while True:
            started_at = clock()
            spawned_at = wall_clock()
            try:
                child = spawn(list(child_command), dict(env), background)
            except OSError as exc:
                log.error("Couldn't start the assistant: %s", exc)
                code, launched = 1, False
            else:
                log.info("Started the assistant (pid %s)", getattr(child, "pid", "?"))
                code = child.wait()
                _release(child)
                child = None
                launched = True
            ran_s = clock() - started_at
            if launched and code == LAUNCHER_FAILED_EXIT_CODE and ran_s < LAUNCHER_FAILED_MAX_S:
                if launch_failing_since is None:
                    launch_failing_since = started_at
                if clock() - launch_failing_since < LAUNCHER_RETRY_FOR_S:
                    launch_failures += 1
                    delay = min(2.0**launch_failures, MAX_BACKOFF_S)
                    log.warning(
                        "Windows wouldn't start Python for the assistant (exit code 101, often antivirus blocking "
                        "python.exe for a moment); trying again in %g s",
                        delay,
                    )
                    sleep(delay)
                    continue
                # The mark stays: this is the antivirus's moment, not a broken assistant, and the task tries later.
                log.error("Windows wouldn't start Python for the assistant for %g s; giving up", LAUNCHER_RETRY_FOR_S)
                return code
            launch_failing_since, launch_failures = None, 0
            told_to_stop = launched and _told_to_stop(paths, spawned_at)

            if code == RESTART_EXIT_CODE:
                log.info("The assistant asked to be restarted")
                quick_crashes = 0
                continue
            # Only a child that actually ran can have been broken by the update; one told to stop wasn't starting up.
            if code != 0 and launched and not told_to_stop and _undo_crashed_update(paths, rollback, spawned_at):
                quick_crashes = 0
                continue
            # In the background every stop she's asked for leaves the quit marker (shut_down_now); a 0 without it is
            # a death that looks clean. With a console, Ctrl+C reaches her too and she ends with 0 unmarked.
            if code == 0 and (told_to_stop or not background):
                log.info("The assistant exited normally")
                stopped_on_purpose(paths)
                return 0
            if told_to_stop:
                log.warning(
                    "The assistant was told to stop and crashed on its way out (exit code %s); not starting it again",
                    _describe(code),
                )
                stopped_on_purpose(paths)
                return 0
            if code == 0:
                # 2026-11-04: Bitdefender's Advanced Threat Defense killed her mid-task, the exit code came back 0, and
                # she was taken for stopped on purpose: off, with nothing to bring her back.
                log.warning(
                    "The assistant ended without being told to stop (exit code 0, no quit marker; an antivirus kill "
                    "looks like this): counting it as a crash"
                )

            if ran_s < quick_crash_s:
                quick_crashes += 1
                if quick_crashes >= max_quick_crashes:
                    log.error(
                        "The assistant stopped %d times in a row within %g s of starting (last exit code %s); "
                        "giving up. Its own log has the details.",
                        quick_crashes,
                        quick_crash_s,
                        _describe(code),
                    )
                    stopped_on_purpose(paths)  # the user gets told (cli); starting it every few minutes helps nobody
                    return code or 1  # unasked 0s: still a failure, so the cli says she stopped
                delay = min(2.0**quick_crashes, MAX_BACKOFF_S)
            else:
                quick_crashes = 0
                delay = LONG_RUN_RESTART_S
            log.warning(
                "The assistant stopped unexpectedly after %.0f s (exit code %s); restarting in %g s",
                ran_s,
                _describe(code),
                delay,
            )
            sleep(delay)
    except KeyboardInterrupt:
        log.info("Stopping (Ctrl+C)")
        if child is not None:
            _stop(child)
        stopped_on_purpose(paths)
        return 0


def _told_to_stop(paths: EvolvePaths, spawned_at: float) -> bool:
    """Whether the child that just exited was told to stop (``QuitMarker``, written after it started). The marker
    is cleared either way: it's about that child only."""
    marker: QuitMarker | None = read_marker(paths.quit_marker, QuitMarker)
    clear_marker(paths.quit_marker)
    return marker is not None and marker.created_at >= spawned_at


def rollback_update(root: Path, *, author: Author = DEFAULT_AUTHOR) -> str | None:
    """Revert the update named by the health marker as ``author``; returns the new HEAD sha.

    None when there's no marker, the update is no longer in the history (the marker is then cleared), or
    git couldn't revert it (the marker is kept; the caller decides). Uncommitted changes don't stop it:
    git refuses on its own if they touch the same files, and otherwise leaves them alone. The journal
    isn't touched here; the assistant marks the improvement rolled back when it reads the restart marker.
    """
    paths = EvolvePaths(Path(root))
    health: HealthMarker | None = read_marker(paths.health_marker, HealthMarker)
    if health is None:
        return None
    git = Git(Path(root))
    name = health.describe()
    try:
        head = git.head()
        # Several installed together are undone together (newest first), in one commit.
        present = [u.merge_commit for u in health.updates() if git.is_ancestor(u.merge_commit, head)]
        # Newest first by history (installs from two processes can be listed out of order): each revert must
        # come before the revert of anything it builds on.
        present.sort(key=lambda c: sum(git.is_ancestor(o, c) for o in present if o != c), reverse=True)
        if not present:
            log.info("Update %s is no longer in the history; nothing to undo", name)
            clear_marker(paths.health_marker)
            return None
        dirty = git.dirty_files()
        if dirty:
            log.warning("Uncommitted changes in %s; trying the undo anyway", ", ".join(dirty[:5]))
        if len(health.updates()) == 1:
            message = f'Undo "{health.title}" (it crashed on startup)'
        else:
            message = f"Undo {len(health.updates())} updates installed together (they crashed on startup)"
        # Reverted beside the main checkout and then fast-forwarded: the user's uncommitted work (staged or not)
        # never ends up in the undo commit, and it's all or nothing.
        side = paths.worktrees / side_worktree_name("rollback")
        new_head = git.on_the_side(
            side, f"octavia/{side.name}", lambda worktree: git.revert_merges(present, author, message, cwd=worktree)
        )
    except GitError as exc:
        log.error("Couldn't undo update %s: %s", name, exc)
        return None
    clear_marker(paths.health_marker)
    log.warning("Undid update %s (now at %s)", name, new_head[:12])
    return new_head


def _undo_crashed_update(
    paths: EvolvePaths, rollback: Callable[[Path], str | None], spawned_at: float | None = None
) -> bool:
    """If the child died before a fresh update proved itself, revert the update. True if it was undone.

    ``spawned_at`` is when that child was started (``time.time``). A marker written after it (an install from
    ``octavia evolve install`` or ``octavia chat``, or one waiting for this child to restart) names code the
    child never ran, so the marker is left for the next child. Otherwise the marker is cleared either way: a
    failed undo must not be retried after every later crash.
    """
    health: HealthMarker | None = read_marker(paths.health_marker, HealthMarker)
    if health is None:
        return False
    if spawned_at is not None and health.created_at > spawned_at:
        log.info(
            'Update #%s "%s" was installed after this assistant started, so it still ran the old code; '
            "the next start is watched instead",
            health.improvement_id,
            health.title,
        )
        return False
    log.warning(
        'The assistant stopped before update #%s "%s" was confirmed healthy; undoing it',
        health.improvement_id,
        health.title,
    )
    try:
        new_head = rollback(paths.root)
    except Exception:
        log.exception('Undoing update "%s" failed', health.title)
        new_head = None
    try:
        clear_marker(paths.health_marker)
    except OSError as exc:
        log.error("Couldn't remove the health marker: %s", exc)
    if not new_head:
        log.error('Couldn\'t undo update "%s"; handling this as an ordinary crash', health.title)
        return False
    # Keep the conversation: the install's restart marker still names it when the new code crashed before it
    # finished starting; after that it's only in the health marker (the restart marker is handled by then).
    previous: RestartMarker | None = read_marker(paths.restart_marker, RestartMarker)
    session = previous.brain_session_id if previous is not None and previous.brain_session_id else None
    updates = health.updates()
    if len(updates) == 1:
        announce = f'The update "{health.title}" crashed on startup, so I undid it.'
    else:
        announce = (
            f"The {len(updates)} updates I installed together crashed on startup, so I undid them all. "
            "Install them one at a time to find the one that broke."
        )
    marker = RestartMarker(
        reason="crash_rollback",
        announce=announce,
        improvement_id=health.improvement_id,
        brain_session_id=session or health.brain_session_id,
        improvement_ids=[u.improvement_id for u in updates],
    )
    try:
        write_marker(paths.restart_marker, marker)
    except OSError as exc:
        log.error("Couldn't write the restart marker: %s", exc)
    log.info("Restarting the assistant on the previous code")
    return True


def _stop(child: ChildProcess) -> None:
    """After Ctrl+C: the child got it too, so give it time to shut down cleanly, then end it."""
    try:
        try:
            child.wait(timeout=STOP_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            log.warning("The assistant didn't stop within %g s; ending it", STOP_GRACE_S)
        except KeyboardInterrupt:
            log.warning("Ending the assistant now")
        kill = getattr(child, "kill", None)
        if kill is not None:
            kill()
    except Exception:
        log.exception("Couldn't stop the assistant")
    finally:
        _release(child)


def _release(child: ChildProcess) -> None:
    close = getattr(child, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        log.debug("Releasing the child failed", exc_info=True)


def _describe(code: int) -> str:
    """Windows reports crashes as NTSTATUS codes, which are only recognisable in hex (0xC0000005 ...)."""
    return f"{code} (0x{code & 0xFFFFFFFF:08X})" if code < 0 or code > 0xFFFF else str(code)


# ------------------------------------------------------------------ starting the child


class SpawnedChild:
    """The assistant process started by ``spawn_child``."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    def wait(self, timeout: float | None = None) -> int:
        """The exit code; raises ``subprocess.TimeoutExpired`` if the child still runs after ``timeout`` s.

        It polls and sleeps instead of calling ``Popen.wait``: on Windows that is one wait Ctrl+C can't
        interrupt (a second Ctrl+C would only be seen once the child exited or the timeout ran out), and its
        own Ctrl+C handling waits for the child again. ``time.sleep`` returns at once on Ctrl+C.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self._popen.poll()
            if code is not None:
                return code
            if deadline is None:
                time.sleep(_POLL_S)
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(self._popen.args, timeout)
            time.sleep(min(_POLL_S, left))

    def kill(self) -> None:
        """End the assistant and the processes it runs for itself (the builder, test runs, git), now.

        Apps it opened for the user (a browser, an editor) are its child processes too, and keep running:
        ending the whole process tree could lose their unsaved work.
        """
        if self._popen.poll() is not None:
            return
        try:  # collected before anything is killed: the parent links break as they die
            own = _own_processes(psutil.Process(self._popen.pid))
        except psutil.Error as exc:
            log.debug("Couldn't list the assistant's processes: %s", exc)
            own = []
        with contextlib.suppress(OSError):  # it may have exited meanwhile
            self._popen.kill()
        for proc in own:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        if own:
            psutil.wait_procs(own, timeout=_KILL_WAIT_S)
        try:
            self.wait(_KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            log.warning("The assistant (pid %s) didn't exit after being killed", self._popen.pid)


def _own_processes(root: psutil.Process) -> list[psutil.Process]:
    """The processes under ``root`` that belong to the assistant (see ``_OWN_TOOLS``), not to the user."""
    own: list[psutil.Process] = []
    pending = [root]
    while pending:
        try:
            children = pending.pop().children()
        except psutil.Error:
            continue
        for child in children:
            try:
                name = child.name().lower().removesuffix(".exe")
            except psutil.Error:
                continue
            if name.startswith("python"):
                own.append(child)
                pending.append(child)
            elif name in _OWN_TOOLS:
                own.append(child)
                with contextlib.suppress(psutil.Error):
                    own.extend(child.children(recursive=True))
    return own


def spawn_child(command: list[str], env: dict[str, str], background: bool) -> SpawnedChild:
    """Start the assistant (windowless in the background). It watches the supervisor's pid itself."""
    return SpawnedChild(subprocess.Popen(command, env=env, creationflags=_NO_WINDOW if background else 0))
