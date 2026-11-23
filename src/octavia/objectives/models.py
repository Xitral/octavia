"""An objective and its steps.

What each one is keeps apart what was asked, what was planned, what was tried, what was checked and what's still
open, so a report never passes off an agent's word as a result:

* ``Objective.goal``: the user's words (asked). ``approach`` and the steps: the plan.
* ``Step.result``: what the agent that did the step said it did (attempted).
* ``Step.evidence``: what the checks found, by code and by a reviewer that didn't do the work. Only a step whose
  checks passed is ``verified``.
* ``Step.problem`` and the failed or skipped steps: what's unresolved, and why.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

# Step kinds: research runs on the background-task machinery (read-only tools and the web); code works in a git
# worktree of one of the user's projects.
RESEARCH = "research"
CODE = "code"
SELF = (
    "self"  # a change to her own code: built and checked by self-improvement (``evolve``), installed on the user's word
)
KINDS = (RESEARCH, CODE, SELF)

# Steps
PENDING = "pending"  # waiting for its turn or for the steps it needs
RUNNING = "running"  # an agent is working on it
CHECKING = "checking"  # done by its agent, being verified
VERIFIED = "verified"  # its checks passed
FAILED = "failed"  # its last attempt failed its checks, or it couldn't run
SKIPPED = "skipped"  # a step it needs failed
CANCELLED = "cancelled"
STEP_OPEN = (PENDING, RUNNING, CHECKING)

# Objectives
PLANNING = "planning"
WORKING = "working"
DONE = "done"  # every step verified
UNFINISHED = "unfinished"  # it ended with some steps failed or skipped
OBJECTIVE_FAILED = "failed"  # it couldn't be planned or started
OBJECTIVE_CANCELLED = "cancelled"
OPEN = (PLANNING, WORKING)

MAX_STEPS = 8  # a plan with more is cut to the first ones (and says so): no endless decomposition
MAX_ATTEMPTS = 2  # an attempt that fails its checks gets one more with the reviewer's findings; then it's failed
MAX_INTERRUPTIONS = 3  # restarts that cut a step off; after these it's failed rather than resumed forever


@dataclass
class Step:
    id: int
    title: str
    kind: str
    instruction: str
    acceptance: str  # how to tell it worked: what the reviewer checks
    depends_on: list[int] = field(default_factory=list)
    status: str = PENDING
    attempts: int = 0
    interruptions: int = 0
    started: str | None = None
    finished: str | None = None
    activity: str = ""  # what it's doing right now, for "how's it going?"
    result: str = ""  # its agent's own account (attempted, not verified)
    evidence: str = ""  # what the checks found
    problem: str = ""  # why it failed or was skipped
    file: str | None = None  # a research step's result
    commit: str | None = None  # a code step's verified commit on the objective's branch
    feedback: str = ""  # what was wrong with its last attempt, for the next one (kept over a restart)
    improvement: int | None = None  # a self step's improvement number in self-improvement's journal
    awaiting_install: bool = False  # a self step's update is built and checked, waiting for the user to install it

    @property
    def open(self) -> bool:
        return self.status in STEP_OPEN


@dataclass
class Objective:
    id: int
    goal: str  # what the user asked, in their words
    title: str = ""
    project: str | None = None  # the project's folder name, when it codes
    project_path: str | None = None
    status: str = PLANNING
    created: str = ""
    finished: str | None = None
    approach: str = ""  # the plan in a sentence or two, and what it assumed
    not_doing: str = ""  # what the plan left out on purpose
    steps: list[Step] = field(default_factory=list)
    branch: str | None = None  # code steps' branch in the project
    worktree: str | None = None
    base: str | None = None  # the commit the branch started from
    baseline: str | None = None  # the project's checks before any change: "passed", "failed: ...", "none"
    error: str | None = None  # why it couldn't be planned or run
    waiting: str = ""  # why it's held back right now (the usage budget), "" when it isn't
    reported: bool = False  # the user was told it ended
    merged: str | None = None  # the merge commit, once the user merged the work into the project
    started_at: float | None = None  # epoch seconds: what it cost is measured from here
    ended_at: float | None = None
    # "user" (handed to her) or "proactive" (her own overnight work, ``octavia/proactive``): prepared work only, on her
    # own share of the usage budget, and not announced when it ends (the morning handoff says it).
    origin: str = "user"

    @property
    def open(self) -> bool:
        return self.status in OPEN

    def step(self, step_id: int) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def verified_head(self) -> str | None:
        """The last verified code step's commit, else where the branch started: all of the branch that's checked."""
        commits = [s.commit for s in self.steps if s.kind == CODE and s.status == VERIFIED and s.commit]
        return commits[-1] if commits else self.base

    @property
    def code_to_merge(self) -> bool:
        """Verified code that changed something is on the branch, and it isn't merged yet."""
        head = self.verified_head()
        return bool(self.branch and head and head != self.base and not self.merged)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.steps:
            out[s.status] = out.get(s.status, 0) + 1
        return out

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Objective:
        names = {f.name for f in fields(cls)}
        step_names = {f.name for f in fields(Step)}
        raw = {k: v for k, v in data.items() if k in names and k != "steps"}
        steps = [
            Step(**{k: v for k, v in s.items() if k in step_names})
            for s in data.get("steps") or []
            if isinstance(s, dict)
        ]
        return cls(**raw, steps=steps)
