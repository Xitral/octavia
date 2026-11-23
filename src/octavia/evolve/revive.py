"""Starting her again when her supervisor has died: what ``supervise`` can't do for itself.

On 2026-10-29 both her processes ended at 13:07 with nothing in her logs or Windows' (no crash, no reboot, no "exited"),
and she stayed off until the user started her by hand at 19:42. The supervisor restarts a crashed assistant, but
nothing restarted the supervisor. A scheduled task (``platform/revive_task.py``) runs ``octavia revive`` every couple of
minutes; it lives outside her processes, so whatever ended them doesn't end it.

The one thing it asks is whether she's meant to be running. A supervisor in the background leaves ``RunningMarker`` as
it starts and takes it away whenever it ends on purpose: she was told to stop, she exited normally, Ctrl+C, or a crash
loop it gave up on (the user was told; starting broken code every few minutes helps nobody). So a marker whose process
is gone means the supervisor died without getting to say anything, and only then is she started. A reboot also leaves
the marker behind, but starting her after one is for autostart to decide, so that only clears it. A supervisor with a
console isn't marked: closing the window is how you stop that one.

The check keeps nothing running and writes nothing unless she's down. Being killed over and over (antivirus) is limited
to ``MAX_REVIVES`` an hour, and the next start says what happened through ``RevivedMarker``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from octavia.core.jsonfile import read_json, write_json
from octavia.evolve.models import (
    EvolvePaths,
    QuitMarker,
    RevivedMarker,
    RunningMarker,
    clear_marker,
    read_marker,
    write_marker,
)

log = logging.getLogger("octavia.revive")

MAX_REVIVES = 3  # in REVIVES_WITHIN_S: more than this and something keeps ending her, so it waits
REVIVES_WITHIN_S = 3600.0
GOING_DOWN_S = 300.0  # how long an assistant still shutting down (it saw the supervisor go) is given before starting

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
_ASSISTANT_ARGS = ("-m", "octavia", "run")


def mark_running(paths: EvolvePaths, command: list[str]) -> None:
    """The supervisor is up and means to stay up: if this process vanishes, it didn't choose to."""
    try:
        started = psutil.Process().create_time()
        write_marker(paths.running_marker, RunningMarker(pid=os.getpid(), started=started, command=list(command)))
    except (OSError, psutil.Error) as exc:  # she runs fine without it; she just can't be revived this time
        log.warning("Couldn't leave the mark that I'm running: %s", exc)


def stopped_on_purpose(paths: EvolvePaths) -> None:
    try:
        clear_marker(paths.running_marker)
    except OSError as exc:
        log.warning("Couldn't take away the mark that I'm running: %s", exc)


def _alive(marker: RunningMarker) -> bool:
    """The same process the marker names; a reused pid has another start time."""
    try:
        return psutil.Process(marker.pid).create_time() == marker.started
    except psutil.AccessDenied:
        return True
    except psutil.Error:
        return False


def _assistant_still_there() -> bool:
    """An assistant whose supervisor just went shuts itself down within seconds; a second one beside it would answer
    the user too."""
    for proc in psutil.process_iter(["cmdline"]):
        args = proc.info.get("cmdline") or []
        if any(tuple(args[i : i + 3]) == _ASSISTANT_ARGS for i in range(len(args))):
            return True
    return False


def _start(command: list[str], cwd: Path) -> None:
    """Hidden and on its own: it outlives this check, and any job the scheduler ran it in."""
    common: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name != "nt":
        subprocess.Popen(command, start_new_session=True, **common)
        return
    try:
        subprocess.Popen(command, creationflags=_NO_WINDOW | _NEW_GROUP | _BREAKAWAY, **common)
    except PermissionError:  # the job doesn't allow breaking away
        subprocess.Popen(command, creationflags=_NO_WINDOW | _NEW_GROUP, **common)


def _last_sign_of_life(root: Path) -> float | None:
    """When she last wrote to her log: as near as anything can say to when she went down."""
    try:
        return (root / "data" / "logs" / "octavia.log").stat().st_mtime
    except OSError:
        return None


@dataclass
class _State:
    """What the check keeps between its runs, only while she's down or lately was."""

    path: Path
    revives: list[float]
    down_seen: float | None

    @classmethod
    def load(cls, path: Path) -> _State:
        data = read_json(path)
        data = data if isinstance(data, dict) else {}
        revives = [float(t) for t in data.get("revives", []) if isinstance(t, int | float)]
        seen = data.get("down_seen")
        return cls(path, revives, float(seen) if isinstance(seen, int | float) else None)

    def save(self) -> None:
        write_json(self.path, {"revives": self.revives, "down_seen": self.down_seen})


def check(
    root: Path,
    *,
    clock: Callable[[], float] = time.time,
    boot_time: Callable[[], float] = psutil.boot_time,
    still_there: Callable[[], bool] = _assistant_still_there,
    start: Callable[[list[str], Path], None] = _start,
) -> str:
    """Start her if her supervisor died without meaning to; returns what it found, in a word or two."""
    paths = EvolvePaths(Path(root))
    running: RunningMarker | None = read_marker(paths.running_marker, RunningMarker)
    if running is None:
        return "off"
    if _alive(running):
        return "up"
    told: QuitMarker | None = read_marker(paths.quit_marker, QuitMarker)
    if told is not None and told.created_at >= running.created_at:  # the supervisor went before it could read this
        clear_marker(paths.quit_marker)
        stopped_on_purpose(paths)
        log.info("She was told to stop, so she stays off")
        return "told to stop"
    if boot_time() > running.created_at:
        stopped_on_purpose(paths)
        log.info("The computer restarted since she started; that's for autostart")
        return "rebooted"

    now = clock()
    state = _State.load(paths.revive_state)
    state.revives = [t for t in state.revives if now - t < REVIVES_WITHIN_S]
    if state.down_seen is None:
        state.down_seen = now
    if still_there() and now - state.down_seen < GOING_DOWN_S:
        state.save()
        log.info("Her supervisor (pid %s) is gone and she's still shutting down; waiting", running.pid)
        return "going down"
    if len(state.revives) >= MAX_REVIVES:
        state.save()
        log.warning("Her supervisor keeps dying (%d starts in the last hour); waiting", len(state.revives))
        return "waiting"

    log.warning("Her supervisor (pid %s) died without stopping her on purpose; starting her again", running.pid)
    state.revives.append(now)
    state.down_seen = None
    state.save()  # counted before starting, so a start that fails can't try forever
    write_marker(paths.revived_marker, RevivedMarker(revived_at=now, down_since=_last_sign_of_life(paths.root)))
    try:
        start(list(running.command), paths.root)
    except OSError as exc:
        clear_marker(paths.revived_marker)
        log.error("Couldn't start her: %s", exc)
        return "failed"
    return "revived"


def run(root: Path) -> int:
    """``octavia revive``: the scheduled task's whole job. Logs only when there's something to say."""
    logs = Path(root) / "data" / "logs"
    handler = logging.FileHandler(logs / "octavia-revive.log", encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        logs.mkdir(parents=True, exist_ok=True)
        check(Path(root))
        return 0
    except Exception:
        log.exception("The check for whether she's running failed")
        return 1
    finally:
        log.removeHandler(handler)
        handler.close()
