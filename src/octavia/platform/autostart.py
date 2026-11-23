"""Whether she starts when the user logs in to Windows, and switching it (``scripts/autostart.ps1`` does the work).

``octavia autostart`` and the tray's "Start with Windows" both come here. The script knows three ways (a Run value,
a Startup-folder shortcut, a logon task); she starts with Windows when any of them is there and on (Task Manager's
Startup apps can switch the first two off, and Task Scheduler the task). Turning it on from the tray uses the Run
value (no admin needed) and clears a switch-off left in Task Manager; turning it off removes all three.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from octavia.edition import windows_name

log = logging.getLogger(__name__)

NAME = windows_name("Octavia", "OCTAVIA")  # the script's -Name: the Run value, the shortcut and the task
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"  # \Run, \StartupFolder
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def command(action: str, method: str = "RunKey", root: Path | None = None) -> list[str]:
    """The PowerShell command line that runs the script: ``action`` is install, uninstall or status."""
    root = root or project_root()
    # 64-bit Windows PowerShell (Sysnative when called from a 32-bit process)
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    ps = system_root / "Sysnative" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not ps.exists():
        ps = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    script = root / "scripts" / "autostart.ps1"
    cmd = [str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", action.capitalize()]
    cmd += ["-Name", NAME]
    if action == "install":
        cmd += ["-Method", method]
    return cmd + ["-ProjectDir", str(root)]


def _switched_off(where: str, name: str) -> bool:
    """Task Manager's Startup apps has this one switched off (``where``: "Run" or "StartupFolder")."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{APPROVED_KEY}\\{where}") as key:
            flags, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return False  # never touched in Task Manager: on
    return isinstance(flags, bytes) and bool(flags[:1]) and bool(flags[0] & 1)  # an odd first byte: off


def _clear_switch_off() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{APPROVED_KEY}\\Run", 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, NAME)
    except OSError:
        pass  # nothing there


def _run_value() -> bool:
    """The Run value is there and not switched off in Task Manager."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, NAME)
    except OSError:
        return False
    return not _switched_off("Run", NAME)


def _shortcut() -> bool:
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return (startup / f"{NAME}.lnk").exists() and not _switched_off("StartupFolder", f"{NAME}.lnk")


def _task() -> bool:
    """The logon task is there and not disabled (its XML says so in any language)."""
    try:
        done = subprocess.run(
            ["schtasks", "/Query", "/TN", f"\\{NAME}", "/XML"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and "<Enabled>false</Enabled>" not in done.stdout.replace(" ", "").replace(chr(0), "")


def is_on() -> bool:
    """Whether she starts at login. Blocks briefly (it may ask Task Scheduler): call it off the event loop."""
    if sys.platform != "win32":
        return False
    return _run_value() or _shortcut() or _task()


def switch(on: bool) -> str | None:
    """Turn starting at login on (the Run value) or off (every way); None when it worked, else why not. Blocks."""
    if on:
        _clear_switch_off()  # else the new Run value stays switched off
    try:
        done = subprocess.run(
            command("install" if on else "uninstall"),
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if done.returncode != 0:
        why = (done.stderr or done.stdout or "").strip().splitlines()
        log.warning("Autostart %s failed: %s", "install" if on else "uninstall", done.stderr or done.stdout)
        return why[-1] if why else "the autostart script didn't manage it"
    if is_on() != on:
        log.warning("Autostart %s ran, but it's still %s", "install" if on else "uninstall", "off" if on else "on")
        if on:
            return "it's still switched off; check Task Manager > Startup apps"
        return "something else still starts her; `octavia autostart status` shows what"
    return None
