"""`octavia test`: the whole test suite as the gate runs it, one heavy run on the PC at a time; ``--quick`` to iterate.

The full run holds the heavy-run lock that every heavy job here takes (a test suite, an eval), so two of them don't
fight over the cores and time each other's tests out: ``.heavy-run.lock`` in the main checkout, holding the holder's
name and its pid on two lines, and stale once that pid is gone. It spreads the tests over as many processes as the
gate does (``gate.suite_workers``).

``--quick`` runs only the test files related to what changed since main (a changed test file, the tests that import a
changed module, tests/test_avatar_js.py for the avatar app), plus the tests that failed last time, without the lock and
over a few processes at most (``QUICK_WORKERS``). It's for iterating: what a merge needs is the full run.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

import psutil

from octavia.config import MODELS_DIR_ENV, find_project_root
from octavia.evolve.gate import spread_args, suite_workers

try:
    import msvcrt
except ImportError:  # not Windows: the pid alone says whether the holder is alive
    msvcrt = None  # type: ignore[assignment]

LOCK_NAME = ".heavy-run.lock"
HOLDER = "octavia test"
POLL_S = 30.0
# ``--quick`` without the lock stays light: one process for one file, at most this many for more (the gate's and
# manager's tests took 5 min 38 s in one process for a change to the gate; a single test file, 7 s).
QUICK_WORKERS = 4
# An empty lock file is one being written (it's created, then filled); one still empty after this long was left
# half-written, and counts as stale. One with text but no pid that can be read is never taken for stale.
_UNFILLED_STALE_S = 60.0
_NAME_PID = re.compile(r"^(?P<name>.*?)\s+(?P<pid>\d+)$")  # the "name pid" one-liner some holders write
_HOLD_AT = 1 << 20  # the byte a holder keeps locked: past the end, so the name and pid stay readable
_AVATAR_TESTS = "tests/test_avatar_js.py"
_LAST_FAILED = Path(".pytest_cache") / "v" / "cache" / "lastfailed"
# Dropped from the tests' environment, as the gate does: tests must never behave as the supervised assistant.
_DROPPED_ENV = frozenset({"OCTAVIA_SUPERVISED", "OCTAVIA_SUPERVISOR_PID"})


def _say(text: str) -> None:
    print(text, flush=True)  # at once, also into a log file: "waiting for ..." may be all it says for an hour


class HeavyRunLock:
    """The PC's one-heavy-run-at-a-time lock. Created atomically (the create fails while another holder's file
    exists), removed when the run ends, however it ends. Waits while a live holder has it; takes over a stale one.

    While held, the file stays open with a byte far past its end locked (Windows): nobody can delete it from under
    the run (another waiter's clean-up, an ``rm -f``), and a holder that has it locked counts as live whatever its pid
    looks like from where the waiter sits. The pid is for a holder that wrote the file by hand, or crashed."""

    def __init__(
        self,
        path: Path,
        name: str = HOLDER,
        *,
        poll_s: float = POLL_S,
        say: Callable[[str], None] = _say,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path)
        self.name = name
        self.poll_s = poll_s
        self.say = say
        self.sleep = sleep
        self._file: BinaryIO | None = None

    def holder(self) -> tuple[str, int | None] | None:
        """Who has the lock (name, pid), or None when nobody does. The pid is the second line's, or for a file
        written as "name pid" on one line the number ending it; None when there's none (being written, or odd)."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except OSError:  # held open in a way that blocks reading: someone has it
            return "", None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1 and lines[1].isdigit():
            return lines[0], int(lines[1])
        if lines and (one := _NAME_PID.match(lines[0])):
            return one["name"], int(one["pid"])
        return (lines[0] if lines else ""), None

    def _locked_open(self) -> bool:
        """Whether a live holder has the file open and locked (only a running process can hold that lock)."""
        if msvcrt is None:
            return False
        try:
            with open(self.path, "rb") as file:
                file.seek(_HOLD_AT)
                try:
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                file.seek(_HOLD_AT)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            return False
        return False

    def _stale(self, pid: int | None) -> bool:
        if self._locked_open():
            return False
        if pid is not None:
            return pid <= 0 or not psutil.pid_exists(pid)
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return False
        return info.st_size == 0 and time.time() - info.st_mtime > _UNFILLED_STALE_S

    def try_acquire(self) -> bool:
        """Take the lock if it's free or stale; False while someone else holds it."""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            pass
        else:
            file = os.fdopen(fd, "r+b")
            try:
                file.write(f"{self.name}\n{os.getpid()}\n".encode())
                file.flush()
                if msvcrt is not None:
                    file.seek(_HOLD_AT)
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                file.close()
                with contextlib.suppress(OSError):
                    self.path.unlink()
                raise
            self._file = file
            return True
        try:
            seen = self.path.read_bytes()
        except FileNotFoundError:
            return self.try_acquire()  # released just now
        except OSError:
            return False
        held = self.holder()
        if held is None or not self._stale(held[1]):
            return False
        self.say(f"The heavy-run lock of {held[0] or 'someone'} (pid {held[1]}) is stale; taking it over.")
        # Only the stale file goes: if another waiter took it over meanwhile, the file is theirs now.
        try:
            if self.path.read_bytes() == seen:
                self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # it's open: someone holds it after all
            return False
        return self.try_acquire()

    def acquire(self) -> None:
        told = None
        while not self.try_acquire():
            held = self.holder()
            if held is not None and held != told:
                if held[1] is None and held[0]:
                    self.say(
                        f"Waiting for {held[0]} to finish its heavy run (its lock names no pid: delete "
                        f"{self.path} by hand if nothing is running)..."
                    )
                else:
                    self.say(f"Waiting for {held[0] or 'someone'} (pid {held[1]}) to finish its heavy run...")
                told = held
            self.sleep(self.poll_s)

    def release(self) -> None:
        """Removes the lock file, if it's still this one's (never another holder's)."""
        file, self._file = self._file, None
        if file is None:
            return
        with contextlib.suppress(OSError):
            if msvcrt is not None:
                file.seek(_HOLD_AT)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        file.close()
        held = self.holder()
        if held is not None and held[1] == os.getpid():
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()

    def __enter__(self) -> HeavyRunLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def main_checkout(root: Path) -> Path:
    """The main checkout ``root`` belongs to: itself, or for a git worktree the checkout it was made from."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return root
    return Path(common).parent if common else root


def _git_lines(root: Path, *args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
    return [line for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []


def changed_files(root: Path, base: str = "main") -> list[str]:
    """Files changed since the branch left ``base`` (committed), plus uncommitted and new ones, as git paths."""
    found = [
        *_git_lines(root, "diff", "--name-only", f"{base}...HEAD"),
        *_git_lines(root, "diff", "--name-only", "HEAD"),
        *_git_lines(root, "ls-files", "--others", "--exclude-standard"),
    ]
    return list(dict.fromkeys(found))


def _module_name(path: str) -> str | None:
    """``src/octavia/a/b.py`` -> ``octavia.a.b`` (a package's ``__init__.py`` is the package)."""
    parts = Path(path).with_suffix("").parts
    if path.endswith(".py") and len(parts) >= 2 and parts[0] == "src":
        parts = parts[1:-1] if parts[-1] == "__init__" else parts[1:]
        return ".".join(parts) or None
    return None


def _imports(path: Path) -> set[str]:
    """Every module a test file imports, and for ``from a import b`` both ``a`` and ``a.b`` (b may be a module)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def related_tests(root: Path, changed: Iterable[str]) -> list[str]:
    """The test files to run for ``changed`` files: changed test files themselves, the ones importing a changed
    module (directly), and the avatar app's tests for a change under avatar/."""
    selected: set[str] = set()
    modules: set[str] = set()
    for path in changed:
        posix = path.replace("\\", "/")
        if posix.startswith("tests/") and Path(posix).name.startswith("test_") and posix.endswith(".py"):
            if (root / posix).is_file():
                selected.add(posix)
        elif posix.startswith("avatar/"):
            selected.add(_AVATAR_TESTS)
        elif module := _module_name(posix):
            modules.add(module)
    if modules:
        for test in sorted((root / "tests").glob("test_*.py")):
            if any(name == m or name.startswith(f"{m}.") for name in _imports(test) for m in modules):
                selected.add(test.relative_to(root).as_posix())
    return sorted(p for p in selected if (root / p).is_file())


def last_failed(root: Path) -> list[str]:
    """The test files with a test that failed in the last run here (pytest's own record), if they still exist. Whole
    files: a test since renamed or removed would stop pytest ("not found") before it ran anything."""
    try:
        record = json.loads((root / _LAST_FAILED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(record, dict):
        return []
    return sorted({path for node in record if (root / (path := node.split("::", 1)[0])).is_file()})


def pytest_command(workers: int, targets: Iterable[str] = (), extra: Iterable[str] = ()) -> list[str]:
    """pytest as the gate runs it (the project's own pyproject.toml, quiet), spread over ``workers`` processes."""
    command = [sys.executable, "-m", "pytest", "-c", "pyproject.toml", "--rootdir", ".", "-q", *spread_args(workers)]
    return [*command, *extra, *targets]


def child_environment(root: Path, checkout: Path) -> dict[str, str]:
    """The tests import ``root``'s code; a worktree's real-model tests use the main checkout's models (git-ignored
    downloads only it has), as in the gate."""
    env = {key: value for key, value in os.environ.items() if key.upper() not in _DROPPED_ENV}
    env["PYTHONPATH"] = str(root / "src")
    if not env.get(MODELS_DIR_ENV) and not (root / "models").is_dir() and (checkout / "models").is_dir():
        env[MODELS_DIR_ENV] = str(checkout / "models")
    return env


@contextlib.contextmanager
def _timed(say: Callable[[str], None], what: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        minutes, seconds = divmod(time.monotonic() - started, 60)
        say(f"{what} took {int(minutes)} min {seconds:.0f} s.")


def add_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    test = sub.add_parser(
        "test",
        help="Run the whole test suite as the gate does, one heavy run at a time (--quick: just what changed)",
    )
    test.add_argument(
        "--quick",
        action="store_true",
        help="only the tests related to changes since main, plus last time's failures; no lock (never a merge check)",
    )
    test.add_argument("--base", default="main", help="what --quick compares against (default: main)")
    test.add_argument("pytest_args", nargs=argparse.REMAINDER, help="passed on to pytest after --, e.g. -- -x")


def run_command(args: argparse.Namespace, root: Path | None = None, say: Callable[[str], None] = _say) -> int:
    root = Path(root or find_project_root())
    checkout = main_checkout(root)
    extra = list(args.pytest_args or ())
    extra = extra[1:] if extra[:1] == ["--"] else extra
    env = child_environment(root, checkout)
    if args.quick:
        files = related_tests(root, changed_files(root, args.base))
        failed = [path for path in last_failed(root) if path not in files]
        if not files and not failed:
            say(f"Nothing to test: no change since {args.base} touches a test, and nothing failed last time.")
            return 0
        say(
            f"Quick run (not a merge check): {len(files)} related test file(s), "
            f"{len(failed)} more with last time's failures."
        )
        targets = [*files, *failed]
        command = pytest_command(min(QUICK_WORKERS, len(targets)), targets, extra)
        with _timed(say, "The quick run"):
            return subprocess.run(command, cwd=root, env=env, check=False).returncode
    with HeavyRunLock(checkout / LOCK_NAME, say=say), _timed(say, "The full run"):
        command = pytest_command(suite_workers(), (), extra)
        say(f"Full suite over {max(1, suite_workers())} process(es), holding {checkout / LOCK_NAME}.")
        return subprocess.run(command, cwd=root, env=env, check=False).returncode
