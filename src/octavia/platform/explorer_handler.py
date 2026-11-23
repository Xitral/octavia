"""What an entry of her Explorer menu runs (``explorer_menu``): hands the request to her and returns at once.

``pythonw -I -m octavia.platform.explorer_handler <verb> <path>``. It writes the request as a small file in
``data/explorer_requests`` (written whole, then renamed in, so she never reads half of one), where she picks it up
within a moment (``core/explorer_requests.py``). With several files selected, Explorer runs this once per file;
she gathers the runs that come in together into one request. When she isn't running it starts her (the way
autostart does) and she picks the request up as she starts, unless it's gone stale by then.

Only the standard library here, and nothing slow: it runs on every click.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

VERBS = frozenset({"ask", "summarize", "explain", "study", "talk", "chat", "running", "clipboard"})
REQUESTS = Path("data") / "explorer_requests"
STARTING = "starting"  # a marker while one run starts her, so the runs of a multi-file click start her once
STARTING_FOR_S = 90.0
MAX_PATH_CHARS = 32_767  # Windows' longest path


def project_root() -> Path:
    env = os.environ.get("OCTAVIA_HOME")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[3]


def mutex_name(root: Path) -> str:
    """The single-instance lock she holds while running (``cli._cmd_run``)."""
    return "Local\\OctaviaAssistant-" + hashlib.sha1(str(root).lower().encode()).hexdigest()[:10]


def running(root: Path) -> bool:
    """Whether she (or her supervisor) holds her lock: opening it by name works only while someone does."""
    if sys.platform != "win32":
        return False
    import ctypes

    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(synchronize, False, mutex_name(root))
    if not handle:
        return False
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


def write_request(root: Path, verb: str, path: str, now: float | None = None) -> Path:
    """The request as a file she reads: whole or not at all."""
    folder = root / REQUESTS
    folder.mkdir(parents=True, exist_ok=True)
    at = time.time() if now is None else now
    data = {"verb": verb, "path": path, "at": at, "pid": os.getpid()}
    name = f"{int(at * 1000)}-{os.getpid()}-{secrets.token_hex(4)}"
    tmp = folder / f"{name}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    final = folder / f"{name}.json"
    os.replace(tmp, final)
    return final


def _claim_start(root: Path) -> bool:
    """Whether this run is the one to start her: the first to make the marker (or one finding it stale)."""
    marker = root / REQUESTS / STARTING
    for _ in range(2):
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - marker.stat().st_mtime < STARTING_FOR_S:
                    return False
                marker.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return False
        os.close(fd)
        return True
    return False


def start_command(root: Path) -> list[str] | None:
    """How autostart starts her (``scripts/autostart.ps1``): the windowless launcher, else pythonw."""
    scripts = root / ".venv" / "Scripts"
    if (scripts / "octaviaw.exe").exists():
        return [str(scripts / "octaviaw.exe"), "run", "--background"]
    if (scripts / "pythonw.exe").exists():
        return [str(scripts / "pythonw.exe"), "-m", "octavia", "run", "--background"]
    return None


def start(root: Path) -> bool:
    command = start_command(root)
    if command is None:
        return False
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def _let_her_window_forward() -> None:
    """This run came from the user's click, so Windows lets it bring a window forward; it passes that on, so her
    chat window comes to the front with the keyboard (else the user's typing would go to Explorer)."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY


def _tell(text: str) -> None:
    """A message box: the only way to say anything from a run with no window, used when she can't be reached."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Octavia", 0x40 | 0x10000)  # information, foreground


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in VERBS:
        return 2
    verb = args[0]
    path = " ".join(args[1:]).strip()
    if len(path) > MAX_PATH_CHARS:
        return 2
    root = root or project_root()
    try:
        write_request(root, verb, path)
    except OSError as exc:
        _tell(f"Couldn't hand that to her: {exc}")
        return 1
    _let_her_window_forward()
    if running(root):
        return 0
    if _claim_start(root):
        if not start(root):
            with contextlib.suppress(OSError):
                (root / REQUESTS / STARTING).unlink()
            _tell("She isn't running, and she couldn't be started (no environment found). Start her, then try again.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
