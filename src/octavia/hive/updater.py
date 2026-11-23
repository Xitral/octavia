"""One-click updates for public installs: a notice when a signed release is out, and the update itself.

``ReleaseWatch`` (in the running app) looks at the feed now and then (``hive.check_every_h``). When a release newer
than this install is out, the tray offers "Update OCTAVIA to <version>" and OCTAVIA says so once. The click starts
this module as its own process (``python -m octavia.hive.updater``) and OCTAVIA shuts down; the updater then:

1. waits for OCTAVIA's processes to end (the program files are free to replace);
2. reads the feed again, checks its signature, downloads the release and checks its hash (``channel.apply``), and
   writes it over the program files, never touching the user's folder (``core/userdir.py``);
3. runs ``uv sync`` when the release changed the dependencies;
4. starts OCTAVIA again, which says how it went (``data/hive/last_update.json``).

Only the public edition updates this way: the owner's install is a git checkout.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from octavia.hive import channel, signing

log = logging.getLogger(__name__)

RESULT = Path("data") / "hive" / "last_update.json"
WAIT_S = 180.0
FIRST_CHECK_S = 90.0


class ReleaseWatch:
    """What the feed offers this install (checked off the event loop: ``check`` blocks on the network)."""

    def __init__(self, root: Path, feed: str, public_key: str) -> None:
        self.root = root
        self.feed = feed
        self.public_key = public_key
        self.available: channel.Release | None = None
        self.told: str | None = None  # the version already announced
        self.error: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.feed and self.public_key)

    def check(self) -> channel.Release | None:
        try:
            releases = channel.read(self.feed, self.public_key)
        except (channel.UpdateError, signing.SignatureError, OSError, ValueError) as exc:
            self.error = str(exc)
            log.info("Update check: %s", exc)
            return self.available
        except Exception as exc:  # a network error of httpx's, say: never her problem
            self.error = type(exc).__name__
            log.info("Update check failed: %s", exc)
            return self.available
        self.error = None
        self.available = channel.newer(releases, channel.current_version(self.root))
        return self.available

    def news(self) -> str | None:
        """The line to say about a release not announced yet, once."""
        if self.available is None or self.told == self.available.version:
            return None
        self.told = self.available.version
        return (
            f"There's an update for me, version {self.available.version.split('+')[0]}. When you're ready, pick "
            "Update in my tray menu: it takes about a minute, and everything you've told me stays."
        )

    def launch(self, wait_pids: list[int]) -> None:
        """Starts the updater on its own (it outlives this process, which then shuts down)."""
        python = Path(sys.executable)
        windowless = python.with_name("pythonw.exe")
        exe = windowless if windowless.exists() else python
        command = [str(exe), "-m", "octavia.hive.updater", "--root", str(self.root), "--relaunch"]
        for pid in wait_pids:
            command += ["--wait-pid", str(pid)]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(command, cwd=self.root, creationflags=flags, close_fds=True)  # noqa: S603


def take_result(root: Path) -> str | None:
    """How the last update went, once (read at start, then forgotten)."""
    path = root / RESULT
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
    except (OSError, ValueError):
        return None
    if result.get("ok"):
        return f"I'm updated to version {str(result.get('to', '')).split('+')[0]}."
    return f"The update didn't go through: {result.get('message', 'something went wrong')}. I'm on the old version."


def _wait(pids: list[int], limit_s: float = WAIT_S) -> bool:
    import psutil

    started = time.monotonic()
    while time.monotonic() - started < limit_s:
        if not any(psutil.pid_exists(p) for p in pids):
            return True
        time.sleep(0.5)
    return False


def _uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "uv.exe"
    return str(candidate) if candidate.exists() else None


def _install_dependencies(root: Path) -> None:
    """The release's dependencies: with uv, else (uv missing, or its downloads blocked by an antivirus) with the
    environment's own pip from ``requirements.lock.txt``, as the installer does."""
    uv = _uv()
    if uv is not None:
        done = subprocess.run(
            [uv, "sync", "--frozen"], cwd=root, capture_output=True, text=True, timeout=1800, check=False
        )
        if done.returncode == 0:
            return
        log.warning("uv sync failed, trying pip: %s", done.stderr.strip()[-300:])
    python = root / ".venv" / "Scripts" / "python.exe"
    requirements = root / "requirements.lock.txt"
    if not python.exists() or not requirements.exists():
        raise channel.UpdateError("couldn't install the new dependencies (no uv, and no environment for pip)")
    for args in (["-r", str(requirements)], ["--no-deps", "-e", "."]):
        done = subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", *args],
            cwd=root, capture_output=True, text=True, timeout=1800, check=False,
        )  # fmt: skip
        if done.returncode != 0:
            raise channel.UpdateError(f"pip couldn't install the new dependencies: {done.stderr.strip()[-300:]}")


def _relaunch(root: Path) -> None:
    scripts = root / ".venv" / "Scripts"
    launcher = scripts / "octaviaw.exe"
    command = [str(launcher)] if launcher.exists() else [str(scripts / "pythonw.exe"), "-m", "octavia", "run"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command, cwd=root, creationflags=flags, close_fds=True)  # noqa: S603


def update(root: Path, wait_pids: list[int], relaunch: bool) -> dict[str, object]:
    from octavia import edition
    from octavia.config import load_config

    before = channel.current_version(root)
    result: dict[str, object] = {"from": before, "at": time.time(), "ok": False}
    try:
        if wait_pids and not _wait(wait_pids):
            raise channel.UpdateError("OCTAVIA didn't close in time")
        config = load_config(root=root)
        h = config.hive
        release = channel.newer(channel.read(h.feed, h.public_key), before)
        if release is None:
            result.update(ok=True, to=before, message="already up to date")
        else:
            changed = channel.apply(root, h.feed, h.public_key, release, public=edition.PUBLIC)
            result.update(ok=True, to=release.version, changed=len(changed))
            if any(c.startswith(("uv.lock", "pyproject.toml", "requirements.lock.txt")) for c in changed):
                _install_dependencies(root)
    except Exception as exc:  # whatever went wrong is said at the next start
        result.update(ok=False, message=str(exc) or type(exc).__name__)
        log.exception("Update failed")
    path = root / RESULT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")
    if relaunch:
        _relaunch(root)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m octavia.hive.updater")
    parser.add_argument("--root", required=True)
    parser.add_argument("--wait-pid", type=int, action="append", default=[])
    parser.add_argument("--relaunch", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if (root / "data" / "logs").is_dir():
        logging.basicConfig(filename=str(root / "data" / "logs" / "updater.log"), level=logging.INFO, encoding="utf-8")
    os.chdir(root)
    return 0 if update(root, args.wait_pid, args.relaunch).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
