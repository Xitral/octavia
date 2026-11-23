"""The scheduled task that starts her again if her supervisor dies (``evolve/revive.py`` does the deciding).

A task in the user's own Task Scheduler, run while they're logged on, no admin needed. It's Windows' to run, so nothing
that ends her processes ends it. She puts it in at each supervised start to match ``system.revive``; a checkout
without its own environment (a worktree) leaves it be.
"""

from __future__ import annotations

import getpass
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from octavia.edition import windows_name

log = logging.getLogger(__name__)

NAME = windows_name("OctaviaRevive", "OCTAVIARevive")
EVERY = "PT2M"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ReviveTaskError(Exception):
    """Windows wouldn't make or remove the task; the message says what it said."""


def launcher(root: Path) -> Path:
    return Path(root) / ".venv" / "Scripts" / "octaviaw.exe"


def _user() -> str:
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()


def task_xml(root: Path, user: str | None = None) -> str:
    """No battery rules (a laptop on battery still wants her), one at a time, ended if it ever hangs."""
    exe, folder = escape(str(launcher(root))), escape(str(root))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the assistant again if her supervisor died. Does nothing if she was shut down on purpose.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>{EVERY}</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user or _user())}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>revive</Arguments>
      <WorkingDirectory>{folder}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, errors="replace", timeout=30, creationflags=_NO_WINDOW
    )


def status(root: Path, name: str = NAME) -> str:
    """Whether it's there: "installed", "other" (one that runs another checkout's copy) or "absent"."""
    try:
        done = _schtasks("/Query", "/TN", f"\\{name}", "/XML")
    except (OSError, subprocess.SubprocessError):
        return "absent"
    if done.returncode != 0:
        return "absent"
    text = done.stdout.replace("\0", "")
    return "installed" if escape(str(launcher(root))).lower() in text.lower() else "other"


def install(root: Path, name: str = NAME) -> None:
    """Make it, replacing any there. schtasks takes a task's XML only from a file."""
    fd, raw = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    path = Path(raw)
    try:
        path.write_text(task_xml(root), encoding="utf-16")
        done = _schtasks("/Create", "/TN", f"\\{name}", "/XML", str(path), "/F")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviveTaskError(str(exc)) from exc
    finally:
        path.unlink(missing_ok=True)
    if done.returncode != 0:
        raise ReviveTaskError((done.stderr or done.stdout).strip() or f"schtasks exited with {done.returncode}")


def uninstall(root: Path, name: str = NAME) -> None:
    try:
        done = _schtasks("/Delete", "/TN", f"\\{name}", "/F")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviveTaskError(str(exc)) from exc
    if done.returncode != 0 and status(root, name) != "absent":
        raise ReviveTaskError((done.stderr or done.stdout).strip() or f"schtasks exited with {done.returncode}")


def sync(root: Path, on: bool, name: str = NAME) -> str | None:
    """Make the task match the setting; "added" or "removed" when it changed, else None. Blocks. Another checkout's
    task is that one's to take out, but turning this on takes it over: only one copy can be the one she runs as."""
    if sys.platform != "win32" or not launcher(root).exists():
        return None
    now = status(root, name)
    if on and now != "installed":
        install(root, name)
        return "added"
    if not on and now == "installed":
        uninstall(root, name)
        return "removed"
    return None
