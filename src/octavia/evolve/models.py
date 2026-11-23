"""Data types shared by the self-improvement modules."""

from __future__ import annotations

import enum
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

RESTART_EXIT_CODE = 75  # the assistant exits with this to ask the supervisor for a restart
SELF_IMPROVEMENT_EMAIL = "self-improvement@localhost"
# A builder progress line that sums up its to-do list ("Plan: 2 of 5 steps done; now: Writing the tests"): the
# manager keeps the latest one apart from the other steps, to say how a build is going.
PLAN_PREFIX = "Plan: "


def self_improvement_author(assistant_name: str) -> tuple[str, str]:
    """The git identity of commits the assistant makes to itself (builds, installs, undos, rollbacks)."""
    return (f"{assistant_name} (self-improvement)", SELF_IMPROVEMENT_EMAIL)


class EvolveError(RuntimeError):
    """A problem worth telling the user about. The message is a short, speakable sentence."""


class Kind(str, enum.Enum):
    BUG = "bug"  # something it does wrong
    MISSING = "missing"  # something it can't do yet
    FEEDBACK = "feedback"  # the user is unhappy with how it works
    IDEA = "idea"  # an improvement it thought of itself
    ERROR = "error"  # noticed automatically: an exception in its own code


# Why one she'd build by herself wasn't: her Claude usage budget (the reason follows). The user may still ask for it.
BUDGET_REFUSAL = "to save the user's Claude usage I'm not building it by myself: "


class Status(str, enum.Enum):
    NOTED = "noted"  # recorded, nothing done yet
    QUEUED = "queued"  # waiting for the builder
    BUILDING = "building"
    READY = "ready"  # built and checked; waiting for the user to install it
    INSTALLED = "installed"
    FAILED = "failed"  # the build or its checks failed
    NO_CHANGE = "no_change"  # the builder concluded nothing in the code should change
    REJECTED = "rejected"  # the user declined it, or the gate refused it
    ROLLED_BACK = "rolled_back"  # installed, then undone


OPEN_STATUSES = (Status.NOTED, Status.QUEUED, Status.BUILDING, Status.READY)


@dataclass
class Improvement:
    id: int
    kind: Kind
    title: str
    details: str
    source: str  # "assistant" | "user" | "watcher" | "cli"
    status: Status
    occurrences: int = 1
    evidence: list[str] = field(default_factory=list)  # recent examples (tracebacks, context), newest last
    fingerprint: str | None = None  # set for automatically noticed errors (dedupe key)
    branch: str | None = None
    base_commit: str | None = None
    merge_commit: str | None = None
    report: dict[str, Any] | None = None  # {"gate": GateReport.to_dict(), "build": BuildResult.to_dict()}
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def label(self) -> str:
        return f'#{self.id} "{self.title}"'


@dataclass
class BuildTask:
    improvement: Improvement
    request: str  # what should change, in plain words (the user's or assistant's request)
    feedback: str | None = None  # why the previous attempt's final checks failed (retry round)
    # A redone build (``EvolveManager._conflicted``): the files where its earlier change, applied on the newer code,
    # clashes and is left with conflict markers for the builder to resolve.
    conflicts: list[str] | None = None


@dataclass
class BuildResult:
    ok: bool  # the builder session finished normally (says nothing about the quality of the change)
    outcome: Literal["changed", "no_change", "gave_up", "error"] = "changed"
    summary: str = ""  # 1-3 plain sentences for the user (spoken)
    details: str = ""  # technical notes for the report
    no_change_reason: str | None = None
    error: str | None = None
    turns: int = 0
    cost_usd: float | None = None
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BuildResult:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


PathClass = Literal["forbidden", "sensitive", "normal"]


@dataclass
class FileChange:
    status: str  # git --name-status letter: A, M, D, R, C, T
    path: str  # repo-relative, forward slashes
    klass: PathClass


@dataclass
class ToolDeclaration:
    name: str
    risk: str  # "safe" | "confirm" | "restricted" | "unknown"
    path: str


@dataclass
class CheckResult:
    name: str  # "ruff check" | "ruff format" | "pytest"
    ok: bool
    summary: str  # e.g. "322 passed, 1 skipped"
    output_tail: str = ""
    # False when the check never ran: Windows wouldn't start the process (``gate._run_check``), which says nothing
    # about the change itself.
    started: bool = True


@dataclass
class GateReport:
    passed: bool
    reasons: list[str] = field(default_factory=list)  # why it didn't pass (empty when passed)
    flags: list[str] = field(default_factory=list)  # what the user should know (sensitive files, edited tests...)
    changes: list[FileChange] = field(default_factory=list)
    new_tools: list[ToolDeclaration] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0

    @property
    def sensitive(self) -> bool:
        return any(c.klass == "sensitive" for c in self.changes) or bool(self.flags)

    @property
    def flags_excused(self) -> bool:
        """A failing test was let through (``gate.recheck``): it isn't true that every test passed."""
        return any(f.startswith(("tests that failed beside", "tests that fail on the code before")) for f in self.flags)

    @property
    def checks_failed(self) -> bool:
        return any(not c.ok for c in self.checks)

    @property
    def checks_unstarted(self) -> bool:
        """A check couldn't even start (``CheckResult.started``): the change was never judged."""
        return any(not c.ok and not c.started for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateReport:
        return cls(
            passed=data.get("passed", False),
            reasons=list(data.get("reasons", [])),
            flags=list(data.get("flags", [])),
            changes=[FileChange(**c) for c in data.get("changes", [])],
            new_tools=[ToolDeclaration(**t) for t in data.get("new_tools", [])],
            checks=[CheckResult(**c) for c in data.get("checks", [])],
            lines_added=data.get("lines_added", 0),
            lines_removed=data.get("lines_removed", 0),
        )


class ProgressCallback(Protocol):
    def __call__(self, message: str) -> None: ...


class BuilderLike(Protocol):
    """What the manager needs from a builder (the real one is ``builder.ClaudeBuilder``). Several builds may run
    at once, each in its own worktree."""

    async def build(self, task: BuildTask, worktree: Path, progress: ProgressCallback) -> BuildResult: ...

    async def cancel(self, worktree: Path | None = None) -> None:
        """Stop the build in ``worktree``, or all of them (None)."""
        ...


# ------------------------------------------------------------------ files shared with the supervisor


@dataclass
class EvolvePaths:
    root: Path  # the project root (main checkout)

    @property
    def dir(self) -> Path:
        return self.root / "data" / "evolve"

    @property
    def journal(self) -> Path:
        return self.dir / "journal.db"

    @property
    def worktrees(self) -> Path:
        return self.dir / "worktrees"

    @property
    def builds(self) -> Path:
        return self.dir / "builds"  # one log per build (progress lines, summary)

    @property
    def restart_marker(self) -> Path:
        return self.dir / "restart.json"

    @property
    def health_marker(self) -> Path:
        return self.dir / "health.json"

    @property
    def quit_marker(self) -> Path:
        return self.dir / "quit.json"

    @property
    def running_marker(self) -> Path:
        return self.dir / "running.json"

    @property
    def revived_marker(self) -> Path:
        return self.dir / "revived.json"

    @property
    def revive_state(self) -> Path:
        return self.dir / "revive_state.json"


@dataclass
class RestartMarker:
    """Written before an intentional restart; read (and deleted) by the next start."""

    # "install" (the user asked) | "auto_install" (installed on its own: restart only once it's idle)
    # | "undo" | "crash_rollback" (written by the supervisor)
    reason: str
    announce: str  # what to say once it's back
    brain_session_id: str | None = None  # resume this conversation
    improvement_id: int | None = None  # the (newest) improvement it's about
    created_at: float = field(default_factory=time.time)
    # Every improvement a crash rollback undid (several installed together are undone together).
    improvement_ids: list[int] = field(default_factory=list)


@dataclass
class QuitMarker:
    """Written by an assistant the user told to stop (the tray's Shut down, ``shut_down_self``) as it goes. Its exit
    code says nothing about that: a crash in the teardown (a driver's audio thread, COM) turns a clean 0 into an
    access violation, and the supervisor then started her again although she'd been told to stop. With this marker,
    written after that assistant was started, any exit is taken as stopped on purpose."""

    pid: int
    created_at: float = field(default_factory=time.time)


@dataclass
class RunningMarker:
    """Left by a background supervisor as it starts, taken away whenever it ends on purpose. Still there with its
    process gone: the supervisor died without choosing to, and ``evolve/revive.py`` starts her again."""

    pid: int
    started: float  # the process's own start time: a reused pid has another
    command: list[str]  # how to start her again
    created_at: float = field(default_factory=time.time)


@dataclass
class RevivedMarker:
    """Left by ``evolve/revive.py`` as it starts her; read (and deleted) by that start, which says what happened."""

    revived_at: float
    down_since: float | None = None  # when she last wrote to her log, the nearest thing to when she went down


@dataclass
class HealthMarker:
    """Written before restarting into a fresh update; removed once the new code has run for a while.

    If the assistant dies while this exists, the supervisor reverts ``merge_commit``, but only when that
    assistant was started after the marker was written (``created_at``): one started earlier still ran the old
    code, so its crash says nothing about the update.

    Several updates installed for the same restart are watched together: ``merge_commit`` is the newest,
    ``earlier`` the others, and a crash undoes them all (there's no telling which one broke it).
    """

    improvement_id: int
    title: str
    merge_commit: str
    previous_head: str
    created_at: float = field(default_factory=time.time)
    # The conversation to resume if the supervisor has to undo this update (the install's restart marker is
    # gone by then when the new code crashed after starting up).
    brain_session_id: str | None = None
    # The other updates the same start runs, oldest first: {"improvement_id", "title", "merge_commit"}.
    earlier: list[dict[str, Any]] = field(default_factory=list)

    def updates(self) -> list[WatchedUpdate]:
        """Every update this marker watches, oldest first."""
        watched = [
            WatchedUpdate(int(u["improvement_id"]), str(u["title"]), str(u["merge_commit"]))
            for u in self.earlier
            if isinstance(u, dict) and {"improvement_id", "title", "merge_commit"} <= u.keys()
        ]
        return [*watched, WatchedUpdate(self.improvement_id, self.title, self.merge_commit)]

    def describe(self) -> str:
        """How the supervisor names them: '"Weather tool"', or '"Weather tool" and 2 other updates'."""
        others = len(self.updates()) - 1
        return f'"{self.title}"' + (f" and {others} other update{'' if others == 1 else 's'}" if others else "")


@dataclass(frozen=True)
class WatchedUpdate:
    improvement_id: int
    title: str
    merge_commit: str


_SIDE_NAME = re.compile(r"^[a-z]+-(\d+)-(\d+)$")


def side_worktree_name(kind: str) -> str:
    """A worktree folder name for work done beside the main checkout (``install``, ``undo``, ``rollback``) that
    names its owner process, so another process never mistakes it for a leftover (``side_worktree_live``)."""
    import psutil

    me = psutil.Process()
    return f"{kind}-{me.pid}-{int(me.create_time())}"


def is_side_worktree_name(name: str) -> bool:
    """Whether a worktree folder (or branch) name is one ``side_worktree_name`` made."""
    return _SIDE_NAME.match(name) is not None


def side_worktree_live(name: str) -> bool:
    """Whether the process that owns this side worktree (``side_worktree_name``) is still running."""
    import psutil

    match = _SIDE_NAME.match(name)
    if match is None:
        return False
    try:
        return abs(psutil.Process(int(match.group(1))).create_time() - int(match.group(2))) < 2.0
    except (psutil.Error, ValueError, OverflowError):
        return False


def write_marker(path: Path, marker: RestartMarker | HealthMarker | QuitMarker) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(marker)), encoding="utf-8")
    tmp.replace(path)  # atomic: a crash never leaves half a marker


def read_marker(
    path: Path, kind: type[RestartMarker | HealthMarker | QuitMarker | RunningMarker | RevivedMarker]
) -> Any:
    """The marker in ``path``, or None if there is none (or it's unreadable)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return kind(**{k: v for k, v in data.items() if k in kind.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def clear_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
