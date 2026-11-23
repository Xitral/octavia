"""Restarting into code the user committed themselves (with Claude Code, say), once nobody would notice.

Her own updates restart her when they're installed, but a commit made outside her didn't: she kept running the old
code until the user said "I just made updates in Claude, restart into them", which they did again and again. This
watches the checkout she runs from. When its HEAD has moved, has stayed put for a while (``quiet_s``: a series of
commits or merges being made goes in as one, not one restart each) and the change touches more than docs and tests,
she restarts into it the way an update she installed on her own does: only once she's idle (no conversation, nothing
to say), never cutting off a build, an install, a screen or background task, or a call (``App._restart_for_new_code``).

What's on disk has to be runnable first (``NewCodeCheck``): not in the middle of a merge or rebase, on the branch she
started on, with no half-made edits to her code, and importing cleanly in a separate process. A change to her
dependencies (``pyproject.toml``, ``uv.lock``) is never restarted into on her own: it needs ``uv sync`` first, which
can't run while she holds the environment's files open, so she says so instead.

``system.restart_for_new_code: false`` turns it off. It needs the supervisor (``octavia run`` under autostart or
``octavia evolve``), which is what starts her again.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

# Changes that never need a restart: what she runs is the same. Markdown only at the top (README.md): her prompt
# is Markdown too (personality/system.md).
_NOT_HER_CODE = ("docs/", "tests/", ".claude/", ".github/")
DEPENDENCIES = ("pyproject.toml", "uv.lock")
# In the git directory while a merge, rebase, cherry-pick, revert or bisect is under way: the files are half done.
_UNDER_WAY = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def needs_restart(paths: Sequence[str]) -> bool:
    """Whether a change to these paths (relative, with forward slashes) changes what she runs."""
    return any(not (p.startswith(_NOT_HER_CODE) or ("/" not in p and p.endswith(".md"))) for p in paths)


class NewCodeWatch:
    """Notices the checkout's HEAD moving past the commit she started on.

    ``head`` returns the current commit (None when git can't tell); ``changed`` the paths that differ between two
    commits. ``poll`` is called every so often and returns the new commit once a restart into it is due; ``paths``
    then holds what changed.
    """

    def __init__(
        self,
        head: Callable[[], str | None],
        changed: Callable[[str, str], Sequence[str]],
        quiet_s: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._head = head
        self._changed = changed
        self.quiet_s = quiet_s
        self._clock = clock
        self.running = head()  # the code this process started with
        self._seen: str | None = self.running
        self._since = clock()
        self._judged: dict[str, bool] = {}
        self.paths: dict[str, list[str]] = {}  # commit -> what changed since ``running`` ([] when unknown)

    def poll(self) -> str | None:
        head = self._head()
        if head is None or self.running is None:
            if self.running is None:
                self.running = head  # git couldn't tell at start: take the first answer as what's running
                self._seen, self._since = head, self._clock()
            return None
        if head != self._seen:
            self._seen, self._since = head, self._clock()  # it moved (again): wait for it to settle
            return None
        if head == self.running or self._clock() - self._since < self.quiet_s:
            return None
        if head not in self._judged:
            try:
                self.paths[head] = list(self._changed(self.running, head))
                self._judged[head] = needs_restart(self.paths[head])
            except Exception:
                log.warning("Couldn't tell what changed in the new code; restarting into it anyway", exc_info=True)
                self.paths[head] = []
                self._judged[head] = True
            if not self._judged[head]:
                log.info("New commits only change docs or tests: no restart needed")
        return head if self._judged[head] else None


Verdict = tuple[Literal["ready", "wait", "never"], str]


class NewCodeCheck:
    """Whether the new code on disk is ready to restart into. ``git(*args)`` returns git's output (raising on an
    error), ``git_dir`` is the checkout's git directory, and ``load`` imports her in a separate process and returns
    what went wrong (None when it loads)."""

    def __init__(
        self,
        git: Callable[..., str],
        git_dir: Path,
        load: Callable[[], str | None],
    ) -> None:
        self._git = git
        self._git_dir = git_dir
        self._load = load
        self.branch = self._branch()  # the branch she started on

    def _branch(self) -> str:
        try:
            return self._git("symbolic-ref", "-q", "HEAD").strip()
        except Exception:
            return ""  # a detached HEAD

    def __call__(self, paths: Sequence[str]) -> Verdict:
        """ "ready"; "wait" (not settled yet: look again later) or "never" (not this commit), with why."""
        if under_way := [name for name in _UNDER_WAY if (self._git_dir / name).exists()]:
            return "wait", f"a git operation is under way ({under_way[0]})"
        if self._branch() != self.branch:
            return "wait", "the checkout is on another branch"
        try:
            dirty = self._git("status", "--porcelain", "-z", "--untracked-files=no").split("\0")
        except Exception as exc:
            return "wait", f"git status failed ({exc})"
        if needs_restart([entry[3:] for entry in dirty if len(entry) > 3]):
            return "wait", "her code has changes that aren't committed"
        if any(p in DEPENDENCIES for p in paths):
            return "never", "it changes my dependencies, so it needs a uv sync with me off first"
        if problem := self._load():
            return "never", f"it doesn't load ({problem})"
        return "ready", ""


def load_in_subprocess(root: Path, timeout_s: float = 120.0) -> str | None:
    """Import her app module from ``root`` in a separate Python: None when it loads, else the last line of the error."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import octavia.app"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "importing it took too long"
    except OSError as exc:
        return f"couldn't start Python: {exc}"
    if result.returncode == 0:
        return None
    lines = [line for line in (result.stderr or "").splitlines() if line.strip()]
    return lines[-1].strip()[:200] if lines else f"exit code {result.returncode}"
