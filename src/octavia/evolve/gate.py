"""The non-AI check between a finished build and the user's decision to install it.

The builder is a model, and it reports what it did; the gate believes none of it. It reads the change from
git (what is actually committed on the build's branch), refuses one that touched protected files, added links,
added a second file for a module that already has one, or declares a reserved tool name, and runs the linter and
the whole test suite itself.

Reasons make a build fail. Flags don't: they tell the user what deserves a look before installing (core
files, edited tests, a new tool that runs without asking or never runs, anything that changes how pytest runs
or ends its run early, the avatar app's code, which the checks can't run) and keep the update from being
installed automatically.

pytest reads only the protected pyproject.toml (``-c``), and its check passes only when its output shows
passing tests and no failures, not on its exit code alone.

Passing tests don't say she still picks the right tool, or how long she takes: once they pass, the speed and quality
check (``evals``, when ``evaluate`` is on) replays real requests through the build's prompt and tools with every
tool faked, and fails a build that fails any one of them for good which the code she runs now handles reliably (each
case judged on its own, tried again as often as its record says, ``evals._settle``), answers slower, or makes every
turn read many more tokens. When it can't tell (Claude unreachable), that is a flag, not a failure.
"""

from __future__ import annotations

import ast
import asyncio
import codecs
import contextlib
import importlib.util
import logging
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octavia.config import MODELS_DIR_ENV
from octavia.evolve import evals, policy
from octavia.evolve.git import Git, GitError
from octavia.evolve.models import CheckResult, FileChange, GateReport, ToolDeclaration

log = logging.getLogger(__name__)

NO_CHANGES = "no changes were made"
# Reasons that mean the build broke a rule rather than failed its checks: retrying won't help, so the
# manager rejects such a build outright (``is_refusal``).
PROTECTED = "changed protected file(s)"
LINKS = "added a link or submodule (not allowed)"
RESERVED = "declares a reserved tool name"
TWIN = "added a second file for an existing module (not allowed)"

CHECK_NAMES = ("ruff check", "ruff format", "pytest")
# A check Windows wouldn't start, after its tries (``_run_check``): the change was never judged.
UNSTARTED = "the checks couldn't start"
# The venv's python.exe is a launcher; when Windows refuses it the real interpreter, it prints this and exits with 101
# (2026-10-27 03:15: every check of three builds, right after a full test run, likely the antivirus).
_LAUNCH_REFUSED = re.compile(r"^Unable to create process using\b", re.M)
_LAUNCH_RETRY_S = (15.0, 60.0)  # waits before trying a check that couldn't start again
# A failed test run gets a second look before it counts against a change (``Gate.recheck``): tests that fail beside
# 8,000 others and pass on their own are timing under load, and tests that fail on the code before the change too
# aren't the change's. Four of eight failed builds in a week (2026-10-20..26) failed only on such tests, each after a
# second builder round that couldn't fix them.
FLAKY = "tests that failed beside the others passed when run again on their own, so they don't count against it"
PREEXISTING = "tests that fail on the code before this change too don't count against it"
_MAX_RECHECKED = 5  # more failures than this aren't a flake or two: they count
_FAILING_TEST = re.compile(r"^(?:FAILED|ERROR) (\S+?\.py(?:::[^\s\[]+(?:\[[^\]]*\])?)*)(?: - |$)", re.M)
_FAILURE_COUNT = re.compile(r"\b(\d+) (?:failed|errors?)\b")
# How a failing test reaches the change's code (``_uses_changed_code``).
_MAX_IMPORTS_FOLLOWED = 3000
_DYNAMIC_IMPORT = re.compile(r"\bimport_module\(|\biter_modules\(|\bwalk_packages\(|\b__import__\(")
_DOTTED = re.compile(r"[\"']([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)+)[\"']")
# What ``Gate.tidy`` runs on a build's changed files: ruff's safe fixes, except removing "unused" imports (a test's
# imported fixture, or an import made for what it registers, looks unused), then the formatter.
_TIDY_ARGS = (
    ("ruff check", ("check", "--fix", "--unfixable", "F401", "--no-cache")),
    ("ruff format", ("format", "--no-cache")),
)
# Lines git leaves where a merge clashed; ======= alone is also a heading underline, so it doesn't count.
MARKERS_CHECK = "conflict markers"
_CONFLICT_MARKER = re.compile(rb"^(?:<{7}|>{7})(?: |\r?$)", re.M)
_MARKER_SCAN_BYTES = 2_000_000
# The build's new tests run on the code before it (``Gate.reproduce``): passing there too means they don't show it works.
REPRO_CHECK = "new tests fail without the change"
UNPROVEN = (
    "its new tests pass on the code before the change too, so nothing shows it fixes or adds what it says: an "
    "unproven change, never installed by itself"
)
_CHECK_ARGS = {
    "ruff check": ("-m", "ruff", "check", "--no-cache", "src", "tests"),
    "ruff format": ("-m", "ruff", "format", "--check", "--no-cache", "src", "tests"),
    # -c: the protected pyproject.toml is the only pytest configuration read, whatever other config files
    # (pytest.toml, tox.ini, setup.cfg, or a name a later pytest adds) the worktree holds.
    "pytest": ("-m", "pytest", "-c", "pyproject.toml", "--rootdir", ".", "-q", "-p", "no:cacheprovider"),
}
_LINT_TIMEOUT_S = 120.0
# The test suite spread over up to this many processes: 5,400 tests took 8 minutes in one, 3 in twelve. More cores
# gain little and would take them from her voice and the builders running beside it.
_MAX_TEST_WORKERS = 12
_KILL_WAIT_S = 10.0
_DRAIN_S = 5.0  # after the check exits: a process it left behind may still hold the output pipe
_CHUNK = 64 * 1024
_KEEP_CHARS = 64_000  # of a check's output; the summary is at the end
_TAIL_LINES = 60
_TAIL_CHARS = 6000
_MAX_SUMMARY = 200
_MAX_LISTED = 10
_SUMMARY_PATTERNS = {
    "ruff check": re.compile(r"^(?:All checks passed!|Found \d+ errors?\b.*)$", re.M),
    "pytest": re.compile(r"\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b[^\n]*"),
}
# pytest's exit code alone doesn't prove the tests passed: pytest.exit(returncode=0) or os._exit(0) in a test
# ends the run with 0. A pass needs a result line with passes and no failures or errors, and no early-end banner.
_PASSED = re.compile(r"\b\d+ passed\b")
_NOT_PASSED = re.compile(r"\b\d+ (?:failed|errors?)\b")
_ENDED_EARLY = re.compile(r"_pytest\.outcomes\.Exit\b[^\n!]*|^!+ *Interrupted\b[^\n!]*", re.M)
# The user's own settings for these would change what the checks run.
# OCTAVIA_SUPERVISED/_SUPERVISOR_PID: tests must never behave as the supervised assistant.
_DROPPED_ENV = frozenset(
    {"OCTAVIA_CONFIG", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "OCTAVIA_SUPERVISED", "OCTAVIA_SUPERVISOR_PID"}
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SPAWN_OPTIONS: dict[str, object] = (
    {"creationflags": _NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
)
_RISK_ORDER = {"safe": 0, "confirm": 1, "restricted": 2}
_TOOL_BASE = "Tool"  # octavia.tools.base.Tool, whose risk defaults to SAFE
_RESTRICTED_HINT = "it won't run until you allow it under permissions.tools"
_PYTEST_CHANGE = "changes how pytest runs"
_ENDS_RUN = "ends the test run early"
# The desktop avatar is an Electron app: the checks can't run it, only node's syntax check and its unit tests
# (tests/test_avatar_js.py runs both, when node is installed).
_AVATAR_CODE = (
    "changes the avatar app's code, which the automatic checks only syntax-check and unit-test, "
    "so look at it before installing"
)
_AVATAR = "avatar/"
_AVATAR_TESTS = "avatar/test/"
_JS_SUFFIXES = (".js", ".mjs", ".cjs")
_AVATAR_CODE_SUFFIXES = (*_JS_SUFFIXES, ".html", ".htm")
# Python source files: an extension in x.pyw is found and imported like one in x.py, so its tools and pytest hooks
# are read too.
_PYTHON_SOURCE = (".py", ".pyw")
# What stops pytest with an exit code of the caller's choosing: pytest.exit, os._exit (or from-imports of them).
_EXIT_IMPORTS = {
    "pytest": {"exit"},
    "_pytest.outcomes": {"exit", "Exit"},
    "os": {"_exit"},
    "nt": {"_exit"},
    "posix": {"_exit"},
}
# Names through which a module plugs into pytest itself: its plugin list, hook implementations, registration.
_PYTEST_PLUGIN_NAMES = frozenset({"pytest_plugins", "hookimpl", "pluginmanager", "HookimplMarker", "pluggy"})


@dataclass
class GateSettings:
    python: Path  # the main venv's interpreter; runs the checks (PYTHONPATH makes it import the worktree's code)
    test_timeout_s: float = 600.0
    extra_forbidden: tuple[str, ...] = ()
    max_changed_lines: int = 4000  # above this the change is flagged as large
    run_checks: bool = True  # off in tests of the classification, which don't need a real test run
    # How many processes the test suite is spread over (pytest-xdist); None = about half the cores, so she stays
    # responsive while it runs. 0 or 1 = one process.
    test_workers: int | None = None
    # The main checkout's models/ (OCTAVIA_MODELS_DIR for the checks): a worktree has no copy of the git-ignored,
    # downloaded models, and without them the real-model tests skip. Only a config rooted at the checked
    # checkout follows it; one on a test's temporary root keeps its own. None = <git.repo>/models.
    models_dir: Path | None = None
    # The speed and quality check on real requests (``evals``) once the tests pass. It uses Claude (her model, a few
    # dozen short turns) whenever a build changes what the model sees, so it's off in tests of the gate.
    evaluate: bool = False
    eval_workers: int = evals.WORKERS
    # Run the build's new test files on the code before it (``Gate.reproduce``); off in tests of the classification.
    reproduce: bool = True


def suite_workers(setting: int | None = None) -> int:
    """Processes for the test run (the gate's, and ``octavia test``'s): ``setting``, or half the cores up to
    ``_MAX_TEST_WORKERS``; 1 without pytest-xdist installed (an older venv), which would otherwise fail on the
    unknown ``-n``."""
    if importlib.util.find_spec("xdist") is None:
        return 1
    if setting is not None:
        return setting
    return max(1, min(_MAX_TEST_WORKERS, (os.cpu_count() or 2) // 2))


def spread_args(workers: int) -> tuple[str, ...]:
    """pytest's arguments for ``workers`` processes. Work-stealing: a process that runs out takes tests from the
    others, instead of each getting a fixed share up front and the run waiting on the one with the slow files (main
    on 2026-10-29: 131 and 134 s with it, 212 and 204 s without)."""
    return ("-n", str(workers), "--dist", "worksteal") if workers > 1 else ()


def is_refusal(reason: str) -> bool:
    """Whether a gate reason means the build broke a rule (as opposed to failing its checks)."""
    return reason.startswith((PROTECTED, LINKS, TWIN, RESERVED))


class Gate:
    """Judges a build from git and from running the checks itself."""

    def __init__(self, git: Git, settings: GateSettings) -> None:
        self.git = git
        self.settings = settings

    async def check(self, worktree: Path, base: str) -> GateReport:
        """The verdict on what is committed in ``worktree`` since ``base`` (the build's starting commit).

        The build must already be committed: the checks run on the worktree's files, and only committed
        files would be installed, so a worktree with anything left uncommitted doesn't pass.
        """
        worktree = Path(worktree)
        try:
            report = await self._inspect(worktree, base)
        except GitError as exc:
            log.warning("Gate couldn't read the change in %s: %s", worktree, exc)
            return GateReport(passed=False, reasons=[f"couldn't read the change from git: {exc}"])
        if report.changes and self.settings.run_checks and not any(is_refusal(r) for r in report.reasons):
            # The lint and formatting checks cover the files the build changed: a file it didn't touch (a protected
            # one left unformatted by hand, or an unused import someone else left on main) must neither fail it nor
            # tempt the builder into editing it. (Unused imports on main failed every night's builds, 2026-10-27.)
            formatted = [c.path for c in report.changes if _is_python(c.path) and c.status[:1] != "D"]
            report.checks = await self.run_checks(worktree, worktree / "src", format_paths=formatted)
            changed = [c.path for c in report.changes if c.status[:1] != "D"]
            marked = await asyncio.to_thread(lambda: [p for p in changed if _has_markers(worktree, p)])
            if marked:  # a clash resolved by the builder (a redone build) and left half done
                report.checks.append(CheckResult(MARKERS_CHECK, False, f"left in {_listing(marked)}"[:_MAX_SUMMARY]))
            unstarted = [c for c in report.checks if not c.ok and not c.started]
            if unstarted:
                # Nothing about the change was judged: say so, rather than that it failed (the builder would get a
                # second round to fix what isn't broken).
                said = "; ".join(f"{c.name}: {c.summary}" for c in unstarted)
                report.reasons.append(f"{UNSTARTED}: {said}"[: _MAX_SUMMARY * 2])
                report.passed = False
                return report
            for i, result in enumerate(report.checks):
                if result.name == "pytest" and not result.ok:
                    own = [c.path for c in report.changes]
                    report.checks[i], flag = await self.recheck(worktree, worktree / "src", result, base, own)
                    report.flags.extend([flag] if flag else [])
            failed = [c.name for c in report.checks if not c.ok]
            if not failed and not report.reasons and self.settings.reproduce:
                shown = await self.reproduce(worktree, base, report.changes)
                if shown is not None:
                    report.checks.append(shown)
                    if not shown.ok:
                        report.flags.append(UNPROVEN)
            if not failed and not report.reasons and self.settings.evaluate:
                outcome = await self.evaluate(worktree, base)
                if outcome.check is not None:
                    report.checks.append(outcome.check)
                    failed += [outcome.check.name] if not outcome.check.ok else []
                report.flags.extend(outcome.flags)
            if failed:
                report.reasons.append(f"checks failed: {', '.join(failed)}")
        report.passed = not report.reasons
        return report

    async def run_checks(self, cwd: Path, src_dir: Path, *, format_paths: list[str] | None = None) -> list[CheckResult]:
        """Lint, format check and the whole test suite in ``cwd``, importing the package from ``src_dir``.

        ``format_paths``: only these files are linted and their formatting checked (a build's changed Python files);
        None = all of src and tests.

        One after another, each in its own process with a timeout. The manager also runs these on the main
        checkout after a merge when it moved on since the build.
        """
        env = _check_environment(Path(src_dir), Path(self.settings.models_dir or self.git.repo / "models"))
        results = []
        for name in CHECK_NAMES:
            timeout = self.settings.test_timeout_s if name == "pytest" else _LINT_TIMEOUT_S
            args = _CHECK_ARGS[name]
            if name == "pytest":
                args = (*args, *spread_args(self._test_workers()))
            if name in ("ruff check", "ruff format") and format_paths is not None:
                if not format_paths:
                    results.append(CheckResult(name, True, "no Python files changed"))
                    continue
                args = (*args[: args.index("src")], "--", *format_paths)  # "--": a file named "--config=..." is a file
            command = [str(self.settings.python), *args]
            started = time.monotonic()
            result = await _run_check(name, command, Path(cwd), env, timeout)
            log.info("Gate %s: %s (%.1f s)", name, result.summary, time.monotonic() - started)
            results.append(result)
        return results

    async def tidy(self, worktree: Path, base: str) -> str:
        """Fix the lint ruff can fix safely and format the Python files a build changed (committed since ``base`` or
        not yet), before it's committed and checked: formatting never fails a build or costs the builder a round. The
        checks then judge what's left. Only those files (never a protected one or a link) are touched, so nothing
        else on main changes. What ruff said, for the build's log ("" when no Python file changed)."""
        worktree = Path(worktree)
        changed, dirty = await _in_threads((self.git.changed_files, worktree, base), (self.git.dirty_files, worktree))
        extra = self.settings.extra_forbidden
        paths = [
            path
            for path in _unique([p for s, p in changed if s[:1] != "D"] + dirty)
            if _is_python(path) and policy.classify_path(path, extra) != "forbidden" and _plain_file(worktree, path)
        ]
        if not paths:
            return ""
        env = _check_environment(worktree / "src", Path(self.settings.models_dir or self.git.repo / "models"))
        said = []
        for name, args in _TIDY_ARGS:
            command = [str(self.settings.python), "-m", "ruff", *args, "--", *paths]
            result = await _run_check(name, command, worktree, env, _LINT_TIMEOUT_S)
            said.append(f"{name}: {result.summary}")
        log.info("Gate tidied %d file(s): %s", len(paths), "; ".join(said))
        return "; ".join(said)

    async def recheck(
        self, cwd: Path, src_dir: Path, tests: CheckResult, base: str, own: Iterable[str]
    ) -> tuple[CheckResult, str | None]:
        """A second look at a failed test run in ``cwd`` before it counts against the change since ``base``
        (``own``: the paths it changed): (the run as it now counts, a flag saying why it was let through, or None).

        Only a few failing tests (``_MAX_RECHECKED``), none in a test file the change touched (the change's own tests
        count as they are). Their files run again on their own, after the change's own test files (a test of the
        change that leaves something behind would break them again): all passing means they failed only beside the
        others. Those still failing run on the code before the change: failing there too, they aren't the change's.
        Anything else, or a run that couldn't start, leaves the failure as it was."""
        failing = _failing_tests(tests)
        own_paths = {_norm(path) for path in own}
        if not failing or len(failing) > _MAX_RECHECKED or any(_norm(t.split("::")[0]) in own_paths for t in failing):
            return tests, None
        models = Path(self.settings.models_dir or self.git.repo / "models")
        own_tests = sorted(p for p in own_paths if _is_test_code(p) and _is_python(p) and (Path(cwd) / p).is_file())
        files = _unique([*own_tests, *(t.split("::")[0] for t in failing)])
        command = [str(self.settings.python), *_CHECK_ARGS["pytest"], *files]
        started = time.monotonic()
        timeout = self.settings.test_timeout_s
        alone = await _run_check("pytest", command, Path(cwd), _check_environment(Path(src_dir), models), timeout)
        log.info("Gate recheck on their own: %s (%.1f s)", alone.summary, time.monotonic() - started)
        if not alone.started:
            return tests, None
        if alone.ok:
            named = _listing(failing)
            if await asyncio.to_thread(_uses_changed_code, Path(cwd), failing, own_paths):
                # The failure stands as it was: the builder's next round needs the test that failed and why (it was
                # handed the passing re-run once, on #224, and couldn't find what failed).
                said = f"{tests.summary}; {named} passed when run again on their own, but they use code this change touched"
                return CheckResult("pytest", False, said[:_MAX_SUMMARY], tests.output_tail), None
            passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", tests.summary)) else 0
            summary = f"{passed + len(failing)} passed ({len(failing)} only when run again on their own: {named})"
            return CheckResult("pytest", True, summary[:_MAX_SUMMARY], tests.output_tail), f"{FLAKY}: {named}"
        stands = CheckResult("pytest", False, tests.summary, alone.output_tail)
        still = _failing_tests(alone)
        if not still or any(_norm(t.split("::")[0]) in own_paths for t in still):
            return stands, None
        try:
            with tempfile.TemporaryDirectory(prefix="octavia-recheck-base-", ignore_cleanup_errors=True) as before:
                await asyncio.to_thread(self.git.run, "archive", "-o", str(Path(before) / "base.tar"), base)
                await asyncio.to_thread(_untar, Path(before) / "base.tar", Path(before))
                env = _check_environment(Path(before) / "src", models)
                command = [str(self.settings.python), *_CHECK_ARGS["pytest"], *still]
                earlier = await _run_check("pytest", command, Path(before), env, timeout)
        except Exception:
            log.warning("Running the failing tests on the code before the change failed", exc_info=True)
            return stands, None
        log.info("Gate recheck on the code before: %s", earlier.summary)
        if earlier.started and set(still) <= set(_failing_tests(earlier)):
            named = _listing(still)
            summary = f"{tests.summary} ({len(still)} failing on the code before the change too: {named})"
            return CheckResult("pytest", True, summary[:_MAX_SUMMARY], alone.output_tail), f"{PREEXISTING}: {named}"
        return stands, None

    async def evaluate(self, worktree: Path, base: str) -> evals.EvalOutcome:
        """The speed and quality check of the build in ``worktree`` against ``base``, the commit it started from: the
        build's own change is judged, not whatever else reached main meanwhile."""
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="octavia-eval-base-", ignore_cleanup_errors=True) as before:
                await asyncio.to_thread(
                    self.git.run, "archive", "-o", str(Path(before) / "base.tar"), base, *_EVAL_PATHS
                )
                await asyncio.to_thread(_untar, Path(before) / "base.tar", Path(before))
                settings = evals.EvalSettings(
                    python=self.settings.python,
                    repo=self.git.repo,
                    workers=self.settings.eval_workers,
                    baseline_src=Path(before) / "src",
                )
                outcome = await evals.evaluate(settings, Path(worktree) / "src", label=Path(worktree).name)
        except Exception as exc:  # the check is new and uses the network: never let it lose the gate's verdict
            log.warning("The speed and quality check failed to run", exc_info=True)
            return evals.EvalOutcome(flags=[f"couldn't run the speed and quality check: {type(exc).__name__}"])
        summary = outcome.check.summary if outcome.check is not None else "; ".join(outcome.flags)
        log.info("Gate eval: %s (%.1f s)", summary, time.monotonic() - started)
        return outcome

    async def reproduce(self, worktree: Path, base: str, changes: list[FileChange]) -> CheckResult | None:
        """The build's new test files run against ``base``, the code before the change: they must fail there, or
        they don't show the change does anything (a fix for approvals that kept asking installed three times with
        tests that passed without it). None when the build added no test file. A flag, not a failure: the user
        decides, and it never installs by itself."""
        added = [c.path for c in changes if c.status[:1] == "A" and _is_test_code(c.path) and _is_python(c.path)]
        if not added:
            return None
        started = time.monotonic()
        try:
            top = set((await asyncio.to_thread(self.git.run, "ls-tree", "--name-only", base)).split())
            with tempfile.TemporaryDirectory(prefix="octavia-repro-base-", ignore_cleanup_errors=True) as before:
                await asyncio.to_thread(
                    self.git.run,
                    "archive",
                    "-o",
                    str(Path(before) / "base.tar"),
                    base,
                    *(path for path in _EVAL_PATHS if path in top),
                )
                await asyncio.to_thread(_untar, Path(before) / "base.tar", Path(before))
                env = _check_environment(
                    Path(before) / "src", Path(self.settings.models_dir or self.git.repo / "models")
                )
                command = [str(self.settings.python), *_CHECK_ARGS["pytest"], *added]
                result = await _run_check("pytest", command, Path(worktree), env, self.settings.test_timeout_s)
        except Exception as exc:
            log.warning("Running the new tests on the code before the change failed", exc_info=True)
            return CheckResult(REPRO_CHECK, True, f"couldn't run them on the code before: {type(exc).__name__}")
        failing = _NOT_PASSED.search(result.output_tail) or _NOT_PASSED.search(result.summary)
        summary = (
            f"they fail on the code before the change ({result.summary})"
            if failing
            else f"they pass on the code before the change too ({result.summary})"
        )
        log.info("Gate %s: %s (%.1f s)", REPRO_CHECK, summary, time.monotonic() - started)
        return CheckResult(REPRO_CHECK, bool(failing), summary[:_MAX_SUMMARY], result.output_tail)

    def _test_workers(self) -> int:
        return suite_workers(self.settings.test_workers)

    # ------------------------------------------------------------------ what the change is

    async def _inspect(self, worktree: Path, base: str) -> GateReport:
        extra = self.settings.extra_forbidden
        changes = [
            FileChange(status, path, policy.classify_path(path, extra))
            for status, path in await asyncio.to_thread(self.git.changed_files, worktree, base)
        ]
        if not changes:
            return GateReport(passed=False, reasons=[NO_CHANGES])
        allowed = [c for c in changes if c.klass != "forbidden"]
        # Every changed source file's old version counts as "before", so a tool moved to another file isn't new.
        old_paths = _unique(c.path for c in allowed if _is_source(c.path))
        # The new version of every changed Python file: sources declare tools, and any of them could hook pytest.
        new_paths = _unique(c.path for c in allowed if _is_python(c.path) and c.status[:1] != "D")
        # Independent read-only git commands, run side by side: on Windows each one costs a process start.
        links, (added, removed), dirty, ignored, tracked, *blobs = await _in_threads(
            (self.git.added_symlinks, worktree, base),
            (self.git.numstat, worktree, base),
            (self.git.dirty_files, worktree),
            (self._ignored_python, worktree),
            (self._tracked, worktree),
            *((self._blob, worktree, base, path, True) for path in old_paths),
            *((self._blob, worktree, "HEAD", path) for path in new_paths),
        )
        report = GateReport(passed=False, changes=changes, lines_added=added, lines_removed=removed)

        forbidden = _unique(c.path for c in changes if c.klass == "forbidden")
        if forbidden:
            report.reasons.append(f"{PROTECTED}: {_listing(forbidden)}")
        if links:
            report.reasons.append(f"{LINKS}: {_listing(links)}")
        twins = _twins([c.path for c in allowed if c.status[:1] in ("A", "R", "C")], tracked)
        if twins:
            report.reasons.append(f"{TWIN}: {_listing(twins)}")
        leftovers = _unique([*dirty, *ignored])
        if leftovers:
            report.reasons.append(f"files in the worktree aren't committed: {_listing(leftovers)}")

        report.flags.extend(
            f"changes core file {p}" for p in _unique(c.path for c in changes if c.klass == "sensitive")
        )
        # Nothing here runs the avatar app, so a change to its code needs the user's eyes more than most.
        avatar_code = _unique(c.path for c in allowed if _is_avatar_code(c.path))
        if avatar_code:
            report.flags.append(f"{_AVATAR_CODE}: {_listing(avatar_code)}")
        # Deleting, editing or renaming an existing test could make a broken change pass (a rename's old path
        # comes as a "D" of its own).
        edited = _unique(
            c.path for c in changes if _is_test(c.path) and c.status[:1] in ("M", "D", "T") and c.klass != "forbidden"
        )
        report.flags.extend(f"edits existing test {p}" for p in edited)
        old_blobs, new_blobs = blobs[: len(old_paths)], dict(zip(new_paths, blobs[len(old_paths) :], strict=True))
        # A conftest.py below the root (the root ones are forbidden), a pytest_plugins list or a hook can skip or
        # deselect failing tests, or report them as passed.
        hooked = _unique(
            [
                *(c.path for c in allowed if _is_conftest(c.path)),
                *(path for path, source in new_blobs.items() if _hooks_pytest(source, path)),
            ]
        )
        report.flags.extend(f"{_PYTEST_CHANGE}: {p}" for p in hooked)
        # A test that stops pytest itself can end the run with exit code 0 before the failing tests ran.
        report.flags.extend(
            f"{_ENDS_RUN}: {path}" for path, source in new_blobs.items() if _is_test(path) and _ends_run(source, path)
        )
        if not any(_is_test_code(c.path) and c.status[:1] != "D" for c in changes):
            report.flags.append("no tests added or changed")
        if added + removed > self.settings.max_changed_lines:
            report.flags.append(f"large change ({added + removed} lines)")

        new_sources = {path: source for path, source in new_blobs.items() if _is_source(path)}
        _judge_tools(dict(zip(old_paths, old_blobs, strict=True)), new_sources, report)
        report.reasons = _unique(report.reasons)
        report.flags = _unique(report.flags)
        return report

    def _ignored_python(self, worktree: Path) -> list[str]:
        """Python files git ignores: the checks would import them, but an install wouldn't include them."""
        out = self.git.run(
            "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", "src", "tests", cwd=worktree
        )
        return [path for path in out.split("\0") if path.endswith(".py")]

    def _tracked(self, worktree: Path) -> list[str]:
        """Every file committed at HEAD: what an install would bring in."""
        out = self.git.run("ls-tree", "-r", "-z", "--name-only", "HEAD", cwd=worktree)
        return [path for path in out.split("\0") if path]

    def _blob(self, worktree: Path, rev: str, path: str, missing_ok: bool = False) -> str:
        """A file's committed content (what an install would merge), not whatever is on disk."""
        return self.git.run("cat-file", "blob", f"{rev}:{path}", cwd=worktree, check=not missing_ok)


# What the eval needs of the build's starting commit: the code, and the prompt and config defaults it reads.
_EVAL_PATHS = ("src", "personality", "config")


def _untar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")


async def _in_threads(*calls: tuple[Any, ...]) -> list[Any]:
    """Run blocking calls side by side; the first failure is raised once all of them are done."""
    results = await asyncio.gather(*(asyncio.to_thread(*call) for call in calls), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def _twins(new_paths: list[str], tracked: list[str]) -> list[str]:
    """The new files that make a module importable from a second file, each with the file it doubles.

    Python imports one of them and silently ignores the other: a package x/ (x/__init__.py) wins over x.py, and a
    octavia/ folder at the top over src/octavia. So a reviewed module could be swapped for one nobody looked at,
    protected or not. No build needs two files for one module; turning a module into a package removes the old
    file.
    """
    files: dict[str, list[str]] = {}
    for path in tracked:
        module = policy.module_name(path)
        if module is not None:
            files.setdefault(module, []).append(path)
    twins = []
    for path in new_paths:
        module = policy.module_name(path)
        others = [other for other in files.get(module, []) if other != path] if module is not None else []
        if others:
            twins.append(f"{path} beside {others[0]}")
    return twins


# ------------------------------------------------------------------ tool declarations


_PARSE_ERRORS = (SyntaxError, ValueError, RecursionError)
_MISSING = object()


def _judge_tools(old_sources: dict[str, str], new_sources: dict[str, str], report: GateReport) -> None:
    """Report the tools the change declares or re-rates, comparing the changed files' new and old versions."""
    before: dict[str, ToolDeclaration] = {}
    before_unnamed: set[str] = set()
    for path, source in old_sources.items():
        try:
            scan = _scan_tools(source, path)
        except _PARSE_ERRORS:
            continue  # its tools can't be compared, so any found now count as new
        for decl in scan.tools:
            before.setdefault(decl.name, decl)
        before_unnamed.update(scan.unnamed)

    for path, source in new_sources.items():
        try:
            scan = _scan_tools(source, path)
        except _PARSE_ERRORS as exc:
            report.reasons.append(f"couldn't parse {path}: {_parse_error(exc)}")
            continue
        report.flags.extend(
            f"can't tell the name of tool class {cls} in {path}" for cls in sorted(scan.unnamed - before_unnamed)
        )
        for decl in scan.tools:
            if decl.name.strip().lower() in policy.RESERVED_TOOL_NAMES:
                report.reasons.append(f"{RESERVED} {decl.name}")
            old = before.get(decl.name)
            if old is None:
                report.new_tools.append(decl)
                if decl.risk == "safe":
                    report.flags.append(f"new tool {decl.name} runs without asking")
                elif decl.risk == "restricted":
                    report.flags.append(f"new tool {decl.name} is restricted: {_RESTRICTED_HINT}")
                elif decl.risk == "unknown":
                    report.flags.append(f"can't tell how risky the new tool {decl.name} is")
            elif old.risk != decl.risk:
                report.new_tools.append(decl)
                if decl.risk == "unknown":
                    report.flags.append(f"can't tell how risky tool {decl.name} is now")
                elif decl.risk == "restricted":
                    report.flags.append(f"tool {decl.name} is restricted now: {_RESTRICTED_HINT}")
                elif _lowered(old.risk, decl.risk):
                    report.flags.append(f"lowers the risk of tool {decl.name}")


def _lowered(old: str, new: str) -> bool:
    if old == "unknown":
        return new == "safe"  # whatever it was, it's the least careful level now
    return _RISK_ORDER[new] < _RISK_ORDER[old]


@dataclass
class _Scan:
    tools: list[ToolDeclaration] = field(default_factory=list)
    unnamed: set[str] = field(default_factory=set)  # tool classes whose name isn't a plain string


def _scan_tools(source: str, path: str) -> _Scan:
    """The tools a module declares: classes deriving from a ``...Tool`` class that set ``name = "..."``.

    Static on purpose: the gate never imports code the builder wrote.
    """
    tree = ast.parse(source.lstrip("\ufeff"), filename=path)
    classes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}

    def is_tool(cls: ast.ClassDef, seen: frozenset[str]) -> bool:
        for base in cls.bases:
            name = _base_name(base)
            if name is None or name in seen:
                continue
            if name.endswith(_TOOL_BASE) or (name in classes and is_tool(classes[name], seen | {name})):
                return True
        return False

    def risk_of(cls: ast.ClassDef, seen: frozenset[str]) -> str:
        value = _class_attr(cls, "risk")
        if value is not _MISSING:
            return _risk_name(value)
        for base in cls.bases:
            name = _base_name(base)
            if name is None or name in seen:
                continue
            if name in classes:
                risk = risk_of(classes[name], seen | {name})
                if risk != "unknown":
                    return risk
            elif name == _TOOL_BASE:
                return "safe"
            elif name.endswith(_TOOL_BASE):
                return "unknown"  # an imported tool base: its default risk isn't in this file
        return "unknown"

    scan = _Scan()
    for cls in classes.values():
        value = _class_attr(cls, "name")
        if value is _MISSING or not is_tool(cls, frozenset({cls.name})):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
            scan.tools.append(ToolDeclaration(value.value.strip(), risk_of(cls, frozenset({cls.name})), path))
        else:
            scan.unnamed.add(cls.name)
    return scan


def _base_name(node: ast.expr) -> str | None:
    """``Tool``, ``base.Tool`` and ``Tool[Args]`` all name "Tool"."""
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _class_attr(cls: ast.ClassDef, attr: str) -> ast.expr | object:
    """The value last assigned to ``attr`` directly in the class body (``x = ...`` or ``x: T = ...``)."""
    value: ast.expr | object = _MISSING
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in stmt.targets):
            value = stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == attr
            and stmt.value is not None
        ):
            value = stmt.value
    return value


def _risk_name(node: object) -> str:
    """``RiskLevel.CONFIRM`` (or the plain string "confirm") -> "confirm"; anything else is "unknown"."""
    if isinstance(node, ast.Attribute):
        name = node.attr.lower()
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = node.value.lower()
    else:
        return "unknown"
    return name if name in _RISK_ORDER else "unknown"


def _hooks_pytest(source: str, path: str) -> bool:
    """Whether a module plugs into pytest: a ``pytest_*`` function, ``pytest_plugins``, hookimpl or pluginmanager.

    Static, like the tool scan. A file that doesn't parse here (python may still read it through a coding
    declaration) counts, except under src/, where that already fails the build.
    """
    try:
        tree = ast.parse(source.lstrip("\ufeff"), filename=path)
    except _PARSE_ERRORS:
        return not _is_source(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("pytest_"):
            return True
        if not _PYTEST_PLUGIN_NAMES.isdisjoint(_names(node)):
            return True
    return False


def _ends_run(source: str, path: str) -> bool:
    """Whether a module stops pytest itself: ``pytest.exit`` or ``os._exit``, called, passed on or imported.

    Either one ends the run with an exit code the caller picks. A bare ``exit`` isn't matched (too common), and
    a file that doesn't parse is already flagged as changing how pytest runs.
    """
    try:
        tree = ast.parse(source.lstrip("﻿"), filename=path)
    except _PARSE_ERRORS:
        return False
    pytest_names = {"pytest"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            pytest_names.update(a.asname for a in node.names if a.name == "pytest" and a.asname)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr == "_exit" or (node.attr == "exit" and _base_name(node.value) in pytest_names):
                return True
        elif isinstance(node, ast.ImportFrom):
            exits = _EXIT_IMPORTS.get(node.module or "", set())
            if any(a.name in exits for a in node.names):
                return True
    return False


def _names(node: ast.AST) -> tuple[str, ...]:
    """The names a node mentions: ``x``, ``a.x``, ``import a.x`` / ``from a import x``, or a string "x"."""
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (node.attr,)
    if isinstance(node, ast.alias):
        parts = node.name.split(".")
        return (parts[0], parts[-1])
    if isinstance(node, ast.ImportFrom):
        return ((node.module or "").split(".")[0],)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)  # getattr(pytest, "hookimpl"), globals()["pytest_plugins"]
    return ()


def _parse_error(exc: BaseException) -> str:
    if isinstance(exc, SyntaxError):
        return f"{exc.msg} (line {exc.lineno})"
    return f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ running the checks


def _check_environment(src_dir: Path, models_dir: Path) -> dict[str, str]:
    """The checks import the package from ``src_dir`` and find the project from there, not from settings.

    The models are the exception: they are git-ignored downloads that only the main checkout has.
    """
    env = {key: value for key, value in os.environ.items() if key.upper() not in _DROPPED_ENV}
    env.update(
        PYTHONPATH=str(src_dir),
        OCTAVIA_HOME="",  # empty = unset: the project root is found from the imported package
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
    )
    env[MODELS_DIR_ENV] = str(models_dir)
    return env


async def _run_check(name: str, command: list[str], cwd: Path, env: dict[str, str], timeout: float) -> CheckResult:
    """One check, tried again after a wait (``_LAUNCH_RETRY_S``) when it couldn't even start: a refusal to start a
    process is Windows' (or the antivirus's) for a moment, and says nothing about the change."""
    waits = iter(_LAUNCH_RETRY_S)
    while True:
        result = await _run_once(name, command, cwd, env, timeout)
        if result.started or (wait := next(waits, None)) is None:
            return result
        log.warning("Gate %s couldn't start (%s); trying again in %g s", name, result.summary, wait)
        await asyncio.sleep(wait)


async def _run_once(name: str, command: list[str], cwd: Path, env: dict[str, str], timeout: float) -> CheckResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,  # a test waiting for input would otherwise hang until the timeout
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_SPAWN_OPTIONS,
        )
    except (OSError, ValueError, NotImplementedError) as exc:
        return CheckResult(name, False, f"couldn't start {name}: {exc}"[:_MAX_SUMMARY], started=False)
    output = _Output()
    reader = asyncio.create_task(output.consume(proc.stdout))
    timed_out = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            timed_out = True
            await _kill_tree(proc)
    finally:
        if proc.returncode is None:  # cancelled: don't leave a test suite running behind
            await _kill_tree(proc)
        with contextlib.suppress(Exception):  # the output is only for the report
            await asyncio.wait_for(reader, _DRAIN_S)
    text = output.text()
    if timed_out:
        return CheckResult(name, False, f"timed out after {timeout:g} s", _tail(text))
    if proc.returncode != 0 and (refused := _LAUNCH_REFUSED.search(text)) and len(text.strip().splitlines()) <= 3:
        line = text[refused.start() :].strip().splitlines()[0]
        return CheckResult(
            name, False, f"Windows wouldn't start it ({line})"[:_MAX_SUMMARY], _tail(text), started=False
        )
    ok, summary = proc.returncode == 0, _summary(name, text, proc.returncode)
    if ok and name == "pytest" and (doubt := _unproven_pass(text)):
        ok, summary = False, doubt[:_MAX_SUMMARY]
    return CheckResult(name, ok, summary, _tail(text))


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a check and everything it started.

    A test can start processes of its own, and the venv's python.exe is itself a launcher that runs the real
    interpreter as its child, so killing just ``proc`` would leave the test suite running.
    """
    if proc.returncode is None:
        if sys.platform == "win32":
            await _taskkill(proc.pid)
        else:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):  # ProcessLookupError included: it may be gone already
            proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), _KILL_WAIT_S)
    except TimeoutError:
        log.warning("Check process %s didn't exit after being killed", proc.pid)


async def _taskkill(pid: int) -> None:
    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    try:
        killer = await asyncio.create_subprocess_exec(
            str(taskkill),
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        await asyncio.wait_for(killer.wait(), _KILL_WAIT_S)
    except (OSError, TimeoutError) as exc:
        log.warning("Couldn't kill the process tree of %s: %s", pid, exc)


class _Output:
    """The end of a check's output: enough for its summary line and a readable tail, in bounded memory."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text = ""

    async def consume(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(_CHUNK):
            self._text = (self._text + self._decoder.decode(chunk))[-_KEEP_CHARS:]

    def text(self) -> str:
        text = self._text + self._decoder.decode(b"", final=True)
        return text.replace("\r\n", "\n").replace("\r", "\n")


def _summary(name: str, text: str, returncode: int | None) -> str:
    """One line: pytest's result line, ruff's verdict, or else the last line of output."""
    pattern = _SUMMARY_PATTERNS.get(name)
    matches = pattern.findall(text) if pattern else []
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    summary = (matches[-1] if matches else lines[-1] if lines else "").strip(" =")
    return (summary or f"exit code {returncode}")[:_MAX_SUMMARY]


def _unproven_pass(text: str) -> str | None:
    """Why pytest's output doesn't show a pass although it exited with 0, or None when it does."""
    early = _ENDED_EARLY.findall(text)
    if early:
        return f"the test run ended early ({early[-1].strip(' !')})"
    results = _SUMMARY_PATTERNS["pytest"].findall(text)
    if not results:
        return "the test run ended without a result (exit code 0)"
    result = results[-1].strip(" =")
    if not _PASSED.search(result) or _NOT_PASSED.search(result):
        return f"exit code 0, but {result}"
    return None


def _tail(text: str) -> str:
    return "\n".join(text.rstrip("\n").split("\n")[-_TAIL_LINES:])[-_TAIL_CHARS:]


def _failing_tests(result: CheckResult) -> list[str]:
    """The failing tests' ids from pytest's summary lines at the end of the output; [] unless every failure and
    error the result line counts is among them (a tail that cut some off can't say which ones failed)."""
    found = _FAILING_TEST.findall(result.output_tail)
    counted = sum(int(n) for n in _FAILURE_COUNT.findall(result.summary))
    return _unique(found) if found and len(found) == counted else []


def _modules(paths: Iterable[str]) -> set[str]:
    """The dotted names of the changed modules under src/ ("src/octavia/x/__init__.py" is "octavia.x")."""
    names = set()
    for path in paths:
        if path.startswith("src/") and path.endswith(_PYTHON_SOURCE):
            parts = path[len("src/") :].rsplit(".", 1)[0].split("/")
            names.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def _uses_changed_code(root: Path, failing: list[str], own_paths: set[str]) -> bool:
    """Whether running a failing test's file loads a module the change changed: then passing on its own may be the
    change's doing too (a cache or port it shares with tests run before it), not load, and it isn't let through
    as flaky.

    Followed statically from the test file through every import that resolves to a file here (``src/`` or the
    tests), as Python would load them: ``import a.b.c`` runs ``a``, ``a.b`` and ``a.b.c``; ``from a.b import c``
    runs ``a``, ``a.b`` and ``a.b.c`` when that's a module. A package's sibling modules don't count unless something
    loaded imports them (#224 on 2026-11-05 imported ``octavia.tools.extensions`` while its build changed
    ``octavia.tools.extensions.page_text``, which nothing it ran imported). Unsure counts as used: a file that can't
    be read or parsed, too many modules, or a module that loads others by name (``importlib``, ``pkgutil``), whose
    reach is every package it names in a string and its own package (``octavia.tools.registry`` loads every
    extension)."""
    changed = _modules(own_paths)
    if not changed:
        return False
    seen: set[str] = set()
    reach: set[str] = set()  # packages a loaded module may import by name
    todo = []
    for path in _unique(t.split("::")[0] for t in failing):
        todo.append(path.removesuffix(".py").replace("/", "."))
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in changed:
            return True
        if len(seen) > _MAX_IMPORTS_FOLLOWED:
            return True
        file = _module_file(root, name)
        if file is None:
            continue  # the standard library or an installed package
        try:
            source = file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(file))
        except (OSError, *_PARSE_ERRORS):
            return True
        package = name if file.name == "__init__.py" else name.rpartition(".")[0]
        if _DYNAMIC_IMPORT.search(source):
            top = name.partition(".")[0]
            named = {m for m in _DOTTED.findall(source) if m.partition(".")[0] == top}
            reach |= named | {package or top}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    todo.extend(_with_parents(alias.name))
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join([*parts, *([node.module] if node.module else [])])
                if not base:
                    continue
                todo.extend(_with_parents(base))
                todo.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return any(m == r or m.startswith(f"{r}.") for m in changed for r in reach)


def _with_parents(name: str) -> list[str]:
    """``a.b.c`` -> ``a``, ``a.b``, ``a.b.c``: importing a module runs every package above it."""
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def _module_file(root: Path, name: str) -> Path | None:
    """The file here that ``name`` loads (under ``src/`` or the checkout's top, e.g. ``tests.fakes``), or None."""
    rel = Path(*name.split("."))
    for folder in (root / "src", root):
        for candidate in (folder / rel.with_suffix(".py"), folder / rel / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def _has_markers(root: Path, path: str) -> bool:
    """Whether a changed text file still holds merge conflict markers."""
    if not _plain_file(root, path):
        return False
    try:
        with (root / path).open("rb") as f:
            data = f.read(_MARKER_SCAN_BYTES)
    except OSError:
        return False
    return b"\0" not in data and _CONFLICT_MARKER.search(data) is not None


def _norm(path: str) -> str:
    return path.replace("\\", "/").lower()


def _plain_file(root: Path, path: str) -> bool:
    """A regular file inside ``root`` (not a link, which could lead out of it)."""
    full = root / path
    try:
        return full.is_file() and not full.is_symlink() and full.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


# ------------------------------------------------------------------ helpers


def _is_test(path: str) -> bool:
    """Anything among the tests: pytest's in tests/, or the avatar app's in avatar/test/ (node runs those)."""
    return path.replace("\\", "/").lower().startswith(("tests/", _AVATAR_TESTS))


def _is_test_code(path: str) -> bool:
    """A file of tests itself, not a fixture or data file next to them."""
    path = path.replace("\\", "/").lower()
    if path.startswith("tests/"):
        return path.endswith(".py")
    return path.startswith(_AVATAR_TESTS) and path.endswith(_JS_SUFFIXES)


def _is_avatar_code(path: str) -> bool:
    """The avatar app's own code: its JavaScript, and the HTML pages that load it (inline scripts included)."""
    path = path.replace("\\", "/").lower()
    return path.startswith(_AVATAR) and not path.startswith(_AVATAR_TESTS) and path.endswith(_AVATAR_CODE_SUFFIXES)


def _is_source(path: str) -> bool:
    path = path.replace("\\", "/").lower()
    return path.startswith("src/") and path.endswith(_PYTHON_SOURCE)


def _is_python(path: str) -> bool:
    return path.lower().endswith(_PYTHON_SOURCE)


def _is_conftest(path: str) -> bool:
    return path.replace("\\", "/").lower().rpartition("/")[2] == "conftest.py"


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _listing(paths: list[str]) -> str:
    shown = ", ".join(paths[:_MAX_LISTED])
    return shown if len(paths) <= _MAX_LISTED else f"{shown} and {len(paths) - _MAX_LISTED} more"
