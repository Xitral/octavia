"""Runs objectives: plans each one, runs its steps as their dependencies are verified, checks every step, and tells
the user once when it ends.

* **Plan**: a read-only Claude Code session in the project (``prompts.PLAN_SCHEMA``). The plan is validated by code:
  at most ``MAX_STEPS`` steps, known kinds, dependencies only on earlier steps (so no cycles), code steps only with a
  project that has commits.
* **Run**: research steps go to the background-task machinery (read-only tools and the web, ``research``), up to its
  limit at once. Code steps run one at a time over all objectives, in plan order, in one worktree per objective on
  branch ``octavia/objective-<id>`` of the project, each on top of the last verified one.
* **Verify**, independently of the agent that did it. Code: the change is committed by code, the project's check is
  run by code (one at a time), and a reviewer session that didn't write it reads the diff against the acceptance
  criteria. Research: a reviewer session grades the result (and spot-checks it on the web). A step that fails gets one
  more attempt with what was wrong; then it's failed, its code is rolled back, and the steps that need it are skipped.
* **Limits**: ``max_open`` objectives at once, ``MAX_ATTEMPTS`` per step, ``MAX_INTERRUPTIONS`` restarts per step, a
  time limit per session and per check, and the usage budget before each Claude session (your limits only: this is
  work you asked for, so it doesn't spend her daily share for her own work).
* **Restarts**: everything is in ``data/objectives/objectives.json``. A step cut off by a restart goes back to
  pending, its unverified code is reset away, and it runs again.
* **Merging** stays the user's: verified code waits on its branch until ``merge``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from octavia.core import usage as claude_usage
from octavia.core.text import spoken_summary
from octavia.evolve.git import Git, GitError, MergeConflictError
from octavia.objectives import guard, prompts
from octavia.objectives.models import (
    CANCELLED,
    CHECKING,
    CODE,
    DONE,
    FAILED,
    KINDS,
    MAX_ATTEMPTS,
    MAX_INTERRUPTIONS,
    MAX_STEPS,
    OBJECTIVE_CANCELLED,
    OBJECTIVE_FAILED,
    PENDING,
    PLANNING,
    RESEARCH,
    RUNNING,
    SELF,
    SKIPPED,
    UNFINISHED,
    VERIFIED,
    WORKING,
    Objective,
    Step,
)
from octavia.objectives.projects import Project, check_environment, executable
from octavia.objectives.session import ClientFactory, SessionResult, SessionSettings, run_session
from octavia.objectives.store import ObjectiveStore

log = logging.getLogger(__name__)

JOB = "objective work"  # the usage budget's name for it
PROACTIVE_JOB = "proactive work"  # ...and for the work she starts on her own overnight
PROACTIVE = "proactive"  # Objective.origin of that work
_HELD = "waiting for your Claude usage to free up"
AUTHOR_EMAIL = "objectives@octavia.local"
OUTPUT_CHARS = 4000  # of a check's output, kept as evidence and shown to the coder and the reviewer
EARLIER_CHARS = 4000  # of each earlier step's result handed to the next one
DIFF_CHARS = 60_000

Announce = Callable[[str, str], None]  # (said once she's free, note for the brain's next turn)
BudgetCheck = Callable[[], Awaitable[str | None]]


async def _budget() -> str | None:
    return await claude_usage.check_fresh(JOB, share=False)


async def _proactive_budget() -> str | None:
    """Work nobody asked for: her daily share of the budget too, like the night shift."""
    return await claude_usage.check_fresh(PROACTIVE_JOB)


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


@dataclass
class RunnerSettings:
    name: str  # hers
    user: str
    folder: Path  # data/objectives
    planner: SessionSettings
    coder: SessionSettings
    reviewer: SessionSettings
    max_open: int = 3
    plan_timeout_s: float = 15 * 60.0
    code_timeout_s: float = 45 * 60.0
    review_timeout_s: float = 15 * 60.0
    check_timeout_s: float = 20 * 60.0
    plan_max_turns: int = 60
    code_max_turns: int = 150
    review_max_turns: int = 60
    web: bool = True  # the planner and research reviewers may use the web
    self_poll_s: float = 15.0  # how often a self step looks at its build
    self_timeout_s: float = 4 * 3600.0  # a build waits its turn and may take a while, but never forever
    own_root: Path | None = None  # her own checkout: what the planner reads for an objective about her
    budget_wait_s: float = 10 * 60.0  # how often a held-back objective asks the budget again


@dataclass
class CheckRun:
    ran: bool  # False: the project has no check
    passed: bool = False
    output: str = ""
    seconds: float = 0.0

    def describe(self) -> str:
        if not self.ran:
            return "The project has no check to run."
        return f"{'Passed' if self.passed else 'FAILED'} in {self.seconds:.0f} s.\n{self.output}".strip()


@dataclass
class _Live:
    """What's running for one objective, in memory only."""

    driver: asyncio.Task[None] | None = None
    steps: dict[int, asyncio.Task[None]] = field(default_factory=dict)


class ObjectiveRunner:
    def __init__(
        self,
        settings: RunnerSettings,
        research: Any,  # a TaskManager (``_background_tasks``) of its own: announces nothing
        announce: Announce,
        projects: Callable[[], list[Project]],
        *,
        store: ObjectiveStore | None = None,
        improvements: Any = None,  # self-improvement's ImprovementManager: self steps (None: she can't change herself)
        client_factory: ClientFactory | None = None,
        budget: BudgetCheck = _budget,
        proactive_budget: BudgetCheck = _proactive_budget,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.settings = settings
        self.research = research
        self._announce = announce
        self._projects = projects
        self.store = store or ObjectiveStore(settings.folder)
        self._client_factory = client_factory
        self.improvements = improvements
        self._budget = budget
        self._proactive_budget = proactive_budget
        self._clock = clock
        self._live: dict[int, _Live] = {}
        self._code_lane = asyncio.Semaphore(1)  # one code step at a time, over every objective
        self._heavy = asyncio.Lock()  # one check (a build and test run) at a time
        self._closing = False
        self.author = (f"{settings.name} (objectives)", AUTHOR_EMAIL)

    # ------------------------------------------------------------------ the list

    @property
    def objectives(self) -> list[Objective]:
        return list(self.store.objectives)

    def find(self, ref: str) -> Objective | None:
        return self.store.find(ref)

    @property
    def busy(self) -> bool:
        """Whether a step is being worked on or checked right now (a restart would cut it off). A step waiting for its
        turn or for your usage limits hasn't started (it stays pending), so it doesn't count: it loses nothing."""
        return any(
            o.status == PLANNING or (o.open and any(s.status in (RUNNING, CHECKING) for s in o.steps))
            for o in self.store.objectives
        )

    def _save(self) -> None:
        self.store.save()

    # ------------------------------------------------------------------ lifecycle

    def start(
        self,
        goal: str,
        project: Project | None,
        *,
        plan: dict[str, Any] | None = None,
        origin: str = "user",
    ) -> Objective:
        """A new objective; planning starts right away. RuntimeError when shutting down or too many are open.

        ``plan`` (``prompts.PLAN_SCHEMA``'s shape) is a plan already made, checked by ``apply_plan`` like the
        planner's, so no planning session runs (ValueError when it's unusable). Its steps are verified like any."""
        if self._closing:
            raise RuntimeError("shutting down")
        open_now = [o for o in self.store.objectives if o.open]
        if len(open_now) >= self.settings.max_open:
            raise RuntimeError(
                f"{len(open_now)} objectives are already under way (at most {self.settings.max_open}); one has to "
                "end or be cancelled first"
            )
        objective = Objective(
            id=self.store.next_id(),
            goal=goal.strip(),
            title=spoken_summary(goal.strip(), 60),
            project=project.name if project else None,
            project_path=str(project.path) if project else None,
            created=self._clock().isoformat(),
            started_at=time.time(),
            origin=origin,
        )
        if plan is not None:
            coding = project is not None and project.has_commits
            if error := apply_plan(objective, plan, coding=coding, self_work=False):
                raise ValueError(error)
            objective.status = WORKING
        self.store.objectives.append(objective)
        self._save()
        self._spawn(objective)
        return objective

    def resume(self) -> None:
        """At start: pick up every objective a restart cut off, rolling unverified work back."""
        for objective in self.store.objectives:
            if not objective.open:
                continue
            for step in objective.steps:
                if step.status not in (RUNNING, CHECKING):
                    continue
                step.interruptions += 1
                step.attempts = max(0, step.attempts - 1)  # cut off, not failed
                step.activity = ""
                if step.interruptions > MAX_INTERRUPTIONS:
                    step.status, step.problem = FAILED, "It was cut off by restarts too many times."
                    step.finished = self._clock().isoformat()
                else:
                    step.status = PENDING
            objective.waiting = ""
            log.info("Resuming objective #%s (%s)", objective.id, objective.title)
        self._save()
        for objective in self.store.objectives:
            if objective.open:
                self._spawn(objective, resumed=True)

    def _spawn(self, objective: Objective, *, resumed: bool = False) -> None:
        live = self._live.setdefault(objective.id, _Live())
        live.driver = asyncio.get_running_loop().create_task(
            self._drive(objective, resumed=resumed), name=f"objective-{objective.id}"
        )

    async def cancel(self, objective: Objective) -> bool:
        """Stop an open objective; its verified work stays (and its branch). False when it wasn't open."""
        if not objective.open:
            return False
        objective.status = OBJECTIVE_CANCELLED
        live = self._live.get(objective.id)
        tasks = [*(live.steps.values() if live else ()), *([live.driver] if live and live.driver else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for step in objective.steps:
            if step.open:
                step.status, step.activity = CANCELLED, ""
                if step.kind == SELF and step.improvement is not None and self.improvements is not None:
                    with contextlib.suppress(Exception):  # its build goes too, unannounced
                        await self.improvements.reject(step.improvement)
        objective.finished, objective.ended_at, objective.reported = self._clock().isoformat(), time.time(), True
        if objective.worktree and objective.base:
            await asyncio.to_thread(self._reset_to_verified, objective)
        self._save()
        return True

    async def close(self) -> None:
        """At shutdown: stop the work; the objectives stay open and resume at the next start."""
        self._closing = True
        tasks = [t for live in self._live.values() for t in (*live.steps.values(), live.driver) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def wait(self, objective: Objective) -> None:
        """Return once the objective's driver has ended (tests)."""
        live = self._live.get(objective.id)
        if live is not None and live.driver is not None:
            await asyncio.wait({live.driver})

    # ------------------------------------------------------------------ one objective

    async def _drive(self, objective: Objective, *, resumed: bool) -> None:
        try:
            if objective.status == PLANNING:
                if not await self._plan(objective):
                    self._end(objective)
                    return
            if any(s.kind == CODE for s in objective.steps):
                project = await self._project(objective)
                why = await asyncio.to_thread(self._prepare_branch, objective, project, resumed)
                self._save()
                if why:
                    objective.status, objective.error = OBJECTIVE_FAILED, why
                    self._end(objective)
                    return
                if objective.baseline is None:
                    await self._baseline(objective, project)
            await self._run_steps(objective)
            self._end(objective)
        except asyncio.CancelledError:
            self._save()
            raise
        except Exception as exc:
            log.exception("Objective #%s failed", objective.id)
            objective.status, objective.error = OBJECTIVE_FAILED, f"something went wrong in my own code ({exc})"
            self._end(objective)

    async def _run_steps(self, objective: Objective) -> None:
        live = self._live.setdefault(objective.id, _Live())
        while True:
            self._skip_blocked(objective)
            for step in self._ready(objective):
                if step.id not in live.steps:  # it stays pending until its attempt really starts
                    live.steps[step.id] = asyncio.get_running_loop().create_task(
                        self._run_step(objective, step), name=f"objective-{objective.id}-step-{step.id}"
                    )
            self._save()
            running = [t for t in live.steps.values() if not t.done()]
            if not running:
                stuck = [s for s in objective.steps if s.status == PENDING]
                for step in stuck:  # can't happen with a valid plan; never loop on it
                    step.status, step.problem = SKIPPED, "It could never start."
                break
            await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for step_id, task in list(live.steps.items()):
                if task.done():
                    del live.steps[step_id]
                    if not task.cancelled() and task.exception() is not None:
                        step = objective.step(step_id)
                        if step is not None:
                            self._fail(step, f"something went wrong in my own code ({task.exception()})")

    def _ready(self, objective: Objective) -> list[Step]:
        """Pending steps whose dependencies are verified. Code steps go in plan order, one at a time: only the first
        pending one, and only when no code step of this objective is running or being checked."""
        verified = {s.id for s in objective.steps if s.status == VERIFIED}
        ready = [s for s in objective.steps if s.status == PENDING and set(s.depends_on) <= verified]
        code_busy = any(s.kind == CODE and s.status in (RUNNING, CHECKING) for s in objective.steps)
        first_code = next((s for s in objective.steps if s.kind == CODE and s.status == PENDING), None)
        return [s for s in ready if s.kind != CODE or (not code_busy and s is first_code)]

    def _skip_blocked(self, objective: Objective) -> None:
        changed = True
        while changed:
            changed = False
            dead = {s.id for s in objective.steps if s.status in (FAILED, SKIPPED, CANCELLED)}
            for step in objective.steps:
                if step.status == PENDING and dead & set(step.depends_on):
                    names = ", ".join(f"step {d}" for d in sorted(dead & set(step.depends_on)))
                    step.status, step.problem = SKIPPED, f"It needs {names}, which didn't pass."
                    step.finished = self._clock().isoformat()
                    changed = True

    def _end(self, objective: Objective) -> None:
        if objective.status in (PLANNING, WORKING):
            ok = objective.steps and all(s.status == VERIFIED for s in objective.steps)
            objective.status = DONE if ok else UNFINISHED
        objective.finished = objective.finished or self._clock().isoformat()
        objective.ended_at = objective.ended_at or time.time()
        objective.waiting = ""
        self._save()
        if not objective.reported:
            objective.reported = True
            self._save()
            if objective.origin == PROACTIVE:  # the morning handoff tells it, checked, at a good moment
                log.info("Proactive objective #%s ended: %s", objective.id, objective.status)
                return
            line, note = ending(objective)
            try:
                self._announce(line, note)
            except Exception:
                log.warning("Couldn't tell the user objective #%s ended", objective.id, exc_info=True)
        log.info("Objective #%s (%s) ended: %s", objective.id, objective.title, objective.status)

    # ------------------------------------------------------------------ planning

    async def _plan(self, objective: Objective) -> bool:
        project = await self._project(objective)
        own = self.settings.own_root if self.improvements is not None and not objective.project_path else None
        cwd = Path(objective.project_path) if objective.project_path else own or self._scratch("plan", objective.id)
        # Her own checkout holds her keys and what she keeps about the user: never read there, and no web beside it.
        web = self.settings.web and own is None
        if project is not None:
            note = (
                "the project is the current directory: its README, layout and the code the objective touches. "
                f"{project.describe()}"
            )
        elif own is not None:
            note = (
                "there's no project for this objective. The current directory is your own code (read-only here): for "
                "an objective about you, read what you need. Plan research and self steps only."
            )
        else:
            note = "there's no project for this objective, so plan research steps only."
        result = await self._session(
            self.settings.planner,
            objective,
            cwd=cwd,
            prompt=prompts.plan_prompt(objective.goal, objective.project, f"{self._clock():%A %B %d, %Y, %H:%M}"),
            system=prompts.planner_system(
                self.settings.name, self.settings.user, note, self_work=self._self_work(objective)
            ),
            tools=(*guard.READ_TOOLS, *(guard.WEB_TOOLS if web else ())),
            check=guard.reader(cwd, web=web, private=guard.PRIVATE if own is not None else ()),
            schema=prompts.PLAN_SCHEMA,
            max_turns=self.settings.plan_max_turns,
            timeout_s=self.settings.plan_timeout_s,
            asked=True,
        )
        if result is None:
            return False
        if not result.ok or result.data is None:
            objective.status = OBJECTIVE_FAILED
            objective.error = f"I couldn't plan it: {result.error or 'the planner gave no plan'}"
            return False
        error = apply_plan(
            objective,
            result.data,
            coding=project is not None and project.has_commits,
            self_work=self._self_work(objective),
        )
        if error:
            objective.status, objective.error = OBJECTIVE_FAILED, f"I couldn't plan it: {error}"
            return False
        objective.status = WORKING
        self._save()
        log.info("Objective #%s planned: %d steps", objective.id, len(objective.steps))
        return True

    # ------------------------------------------------------------------ the branch

    def _git(self, objective: Objective) -> Git:
        assert objective.project_path is not None
        return Git(Path(objective.project_path))

    def _prepare_branch(self, objective: Objective, project: Project | None, resumed: bool) -> str | None:
        """The objective's worktree and branch (made once); on a resume, unverified changes are reset away. Runs in a
        worker thread: it changes the objective but leaves saving to the caller."""
        if not objective.project_path:
            return "it has code steps but no project"
        git = self._git(objective)
        try:
            if objective.worktree and Path(objective.worktree).is_dir() and objective.base:
                if resumed:
                    self._reset_to_verified(objective)
                return None
            if not git.has_commits():
                return f"{objective.project} has no commits yet, so I can't work on a branch of it"
            base = git.head()
            branch = f"octavia/objective-{objective.id}"
            worktree = self.settings.folder / "worktrees" / f"{objective.id}-{objective.project}"
            git.add_worktree(worktree, branch, base)
            objective.branch, objective.worktree, objective.base = branch, str(worktree), base
        except GitError as exc:
            log.warning("Couldn't make objective #%s's branch", objective.id, exc_info=True)
            return f"I couldn't make a branch of {objective.project}: {exc}"
        return None

    # The git-ignored folders a worktree borrows from the checkout (node_modules) exist only while code runs: while a
    # code step works and while the check runs. Every git command that touches the files (add, commit, reset, clean)
    # runs without them, so nothing can ever be staged, reset or deleted through them into the user's own folder.

    @contextlib.contextmanager
    def _borrowed(self, objective: Objective, project: Project | None) -> Iterator[None]:
        worktree = Path(objective.worktree or "")
        made: list[Path] = []
        try:
            for name in project.links if project else ():
                target = worktree / name
                if self._link(self._git(objective), Path(objective.project_path or "") / name, target):
                    made.append(target)
            yield
        finally:
            for target in made:
                _unlink(target)

    @staticmethod
    def _link(git: Git, source: Path, target: Path) -> bool:
        """A directory junction to a folder git ignores in the checkout (so it's never committed); False when not."""
        if not source.is_dir() or os.path.lexists(target):
            return False
        if not git.run("check-ignore", source.name, check=False).strip():
            return False  # not ignored there: borrowing it could put it in a commit
        if os.name == "nt":
            done = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if done.returncode != 0:
                log.warning("Couldn't borrow %s for a check: %s", source, done.stdout or done.stderr)
                return False
        else:
            target.symlink_to(source, target_is_directory=True)
        return True

    def _verified_head(self, objective: Objective) -> str | None:
        return objective.verified_head()

    def _reset_to_verified(self, objective: Objective) -> None:
        """Back to the last verified code step (or where the branch started): nothing unverified stays on it."""
        target = self._verified_head(objective)
        if target and objective.worktree:
            self._reset_to(objective, target)

    async def _baseline(self, objective: Objective, project: Project | None) -> None:
        if project is None or not project.check or not objective.worktree:
            objective.baseline = "none"
        else:
            run = await self._check(objective, project)
            objective.baseline = "passed" if run.passed else "failed: " + run.output[-OUTPUT_CHARS:]
        self._save()

    async def _check(self, objective: Objective, project: Project) -> CheckRun:
        """The project's check, run by code in the worktree, one at a time."""
        assert objective.worktree is not None
        cwd = Path(objective.worktree)
        argv = executable(project, cwd)
        if argv is None:
            return CheckRun(ran=False)
        env = os.environ | check_environment(project, cwd, self.settings.folder / "cache")
        async with self._heavy, contextlib.AsyncExitStack() as stack:
            await asyncio.to_thread(stack.enter_context, self._borrowed(objective, project))
            started = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(cwd),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                return CheckRun(ran=True, passed=False, output=f"couldn't run {' '.join(project.check or ())}: {exc}")
            try:
                async with asyncio.timeout(self.settings.check_timeout_s):
                    out, _ = await proc.communicate()
            except (TimeoutError, asyncio.CancelledError) as exc:
                await asyncio.shield(asyncio.to_thread(_kill_tree, proc.pid))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                minutes = round(self.settings.check_timeout_s / 60)
                return CheckRun(ran=True, passed=False, output=f"It ran over {minutes} minutes and was stopped.")
            text = (out or b"").decode("utf-8", errors="replace").strip()
            if len(text) > OUTPUT_CHARS:
                text = "[...]\n" + text[-OUTPUT_CHARS:]
            return CheckRun(ran=True, passed=proc.returncode == 0, output=text, seconds=time.monotonic() - started)

    # ------------------------------------------------------------------ one step

    async def _run_step(self, objective: Objective, step: Step) -> None:
        start_commit: str | None = None
        while step.attempts < MAX_ATTEMPTS:
            # A code step waits for the code lane, then everyone for the usage budget: pending all the while.
            async with self._code_lane if step.kind == CODE else contextlib.nullcontext():
                if not await self._wait_for_budget(objective, step):
                    return
                step.attempts += 1
                step.status, step.started, step.activity = RUNNING, self._clock().isoformat(), "starting"
                step.problem = ""
                self._save()
                # Work nobody asked for counts against her own daily share, like the night shift's.
                charged = (
                    claude_usage.charged(PROACTIVE_JOB) if objective.origin == PROACTIVE else contextlib.nullcontext()
                )
                async with charged:
                    if step.kind == CODE:
                        if start_commit is None:
                            start_commit = await asyncio.to_thread(self._head, objective)
                        passed, feedback = await self._code_attempt(
                            objective, step, step.feedback or None, start_commit
                        )
                    elif step.kind == SELF:
                        passed, feedback = await self._self_attempt(objective, step)
                    else:
                        passed, feedback = await self._research_attempt(objective, step, step.feedback or None)
            step.feedback = feedback or ""
            if passed is None:  # couldn't run at all (no point trying again)
                break
            if passed:
                step.status, step.finished, step.activity = VERIFIED, self._clock().isoformat(), ""
                self._save()
                return
            if step.attempts < MAX_ATTEMPTS:
                step.status = PENDING  # until the next attempt really starts
        if step.kind == CODE and start_commit and objective.worktree:
            await asyncio.to_thread(self._reset_to, objective, start_commit)
        self._fail(step, step.problem or "It didn't pass its checks.")

    async def refresh_updates(self) -> None:
        """Forget "waiting to install" for self steps whose update the user has since installed or turned down."""
        from octavia.evolve.models import Status

        if self.improvements is None:
            return
        changed = False
        for objective in self.store.objectives:
            for step in objective.steps:
                if not (step.awaiting_install and step.improvement):
                    continue
                with contextlib.suppress(Exception):
                    item = await self.improvements.get(step.improvement)
                    if item is None or item.status is not Status.READY:
                        step.awaiting_install, changed = False, True
        if changed:
            self._save()

    def _self_work(self, objective: Objective) -> bool:
        """Self steps only for an objective about her (no project): work on a project never builds changes to her
        on the side, which would spend her daily builds and skip the question building one asks."""
        return self.improvements is not None and not objective.project_path

    async def _self_attempt(self, objective: Objective, step: Step) -> tuple[bool | None, str | None]:
        """A change to her: self-improvement builds it on a copy and its gate checks it (her tests, the protected
        files, a replay of real requests): code, not the agent's word. Verified once it's ready to install; the user
        installs it, as every update of hers. Its own builder retries a failed check, so a failure here is final."""
        from octavia.evolve.models import EvolveError, Status

        manager = self.improvements
        if manager is None:
            step.problem = "I can't change myself right now (self-improvement is off)."
            return None, None
        if step.improvement is not None:  # after a restart: follow the build already asked for...
            item = await manager.get(step.improvement)
            stopped = item is not None and item.status is Status.FAILED and "interrupt" in str(item.error or "")
            if stopped:  # ...or ask again for one a restart cut off (``resume`` limits how often)
                with contextlib.suppress(EvolveError):
                    await manager.request_build(item.id, silent=True)
        else:
            request = (
                f"{step.instruction}\n\nIt's done when: {step.acceptance}\n\n(Part of an objective: {objective.goal})"
            )
            try:
                item = await manager.request_build(None, request[:2000], title=step.title[:120], silent=True)
            except EvolveError as exc:
                step.problem = str(exc)
                return None, None
            step.improvement = item.id
            self._save()
        step.activity = "being built and checked by my self-improvement"
        self._save()
        deadline = time.monotonic() + self.settings.self_timeout_s
        while True:
            item = await manager.get(step.improvement)
            status = getattr(item, "status", None)
            if item is None or status not in (Status.QUEUED, Status.BUILDING):
                break
            if time.monotonic() > deadline:
                hours = round(self.settings.self_timeout_s / 3600, 1)
                step.problem = f"Update #{step.improvement} was still {status.value} after {hours} h."
                return None, None
            await asyncio.sleep(self.settings.self_poll_s)
        built = ((getattr(item, "report", None) or {}).get("build") or {}) if item is not None else {}
        if status in (Status.READY, Status.INSTALLED):
            step.result = str(built.get("summary") or "")[:1000]
            installed = "and installed" if status is Status.INSTALLED else "and ready to install"
            step.evidence = (
                f"My self-improvement checks passed (my tests and a replay of real requests): update #{item.id}, "
                f"built {installed}."
            )
            step.awaiting_install = status is Status.READY
            return True, None
        why = str(getattr(item, "error", "") or built.get("no_change_reason") or "")
        what = getattr(status, "value", status) or "gone"
        step.problem = f"Update #{step.improvement} ended {what}" + (f": {why}" if why else ".")
        return None, None

    def _fail(self, step: Step, problem: str) -> None:
        step.status, step.problem = FAILED, step.problem or problem
        step.finished, step.activity = self._clock().isoformat(), ""
        self._save()

    async def _wait_for_budget(self, objective: Objective, step: Step) -> bool:
        """Hold the step back while your Claude limits are near; False when the objective ended meanwhile."""
        while True:
            why = await self._budget_for(objective)()
            if why is None:
                if objective.waiting:
                    objective.waiting = ""
                    self._save()
                return True
            if objective.waiting != why or step.activity != _HELD:
                objective.waiting, step.activity = why, _HELD
                self._save()
            await asyncio.sleep(self.settings.budget_wait_s)
            if not objective.open:
                return False

    def _budget_for(self, objective: Objective) -> BudgetCheck:
        """Her own share for work nobody asked for (whatever ``respect_limits`` says), your limits for the rest."""
        return self._proactive_budget if objective.origin == PROACTIVE else self._budget

    def _head(self, objective: Objective) -> str:
        assert objective.worktree is not None
        return self._git(objective).head(Path(objective.worktree))

    def _reset_to(self, objective: Objective, commit: str) -> None:
        assert objective.worktree is not None
        git, worktree = self._git(objective), Path(objective.worktree)
        _unlink_junctions(worktree)  # never reset or clean through a borrowed folder
        try:
            git.run("reset", "--hard", "-q", commit, cwd=worktree)
            git.run("clean", "-fdq", cwd=worktree)
        except GitError:
            log.warning("Couldn't roll back objective #%s's step", objective.id, exc_info=True)

    def _commit(self, objective: Objective, worktree: Path, message: str) -> None:
        """Commit everything in the worktree, without the project's git hooks (a step can edit tracked ones, and a
        formatting hook would change the code after it was checked)."""
        git = self._git(objective)
        _unlink_junctions(worktree)
        no_hooks = self.settings.folder / "no-hooks"
        no_hooks.mkdir(parents=True, exist_ok=True)
        git.run("-c", f"core.hooksPath={no_hooks}", "add", "-A", cwd=worktree)
        if git.run("status", "--porcelain", "--untracked-files=no", cwd=worktree).strip():
            git.run(
                "-c", f"core.hooksPath={no_hooks}", "commit", "-q", "--no-verify", "-m", message,
                cwd=worktree, author=self.author,
            )  # fmt: skip

    def _earlier(self, objective: Objective, step: Step) -> str:
        """The verified results this step builds on: its dependencies, and for code every verified code step."""
        wanted = set(step.depends_on)
        if step.kind == CODE:
            wanted |= {s.id for s in objective.steps if s.kind == CODE and s.id < step.id}
        parts = []
        for other in objective.steps:
            if other.id not in wanted or other.status != VERIFIED:
                continue
            body = other.result
            if other.file:
                with contextlib.suppress(OSError):
                    body = Path(other.file).read_text(encoding="utf-8")
            if len(body) > EARLIER_CHARS:
                body = body[:EARLIER_CHARS] + "\n[...]"
            parts.append(f"Step {other.id}, {other.title} ({other.kind}, verified):\n{body}")
        return "\n\n".join(parts)

    async def _research_attempt(
        self, objective: Objective, step: Step, feedback: str | None
    ) -> tuple[bool | None, str | None]:
        instruction = prompts.research_instruction(
            objective.goal, step.instruction, step.acceptance, self._earlier(objective, step)
        )
        if feedback:
            instruction += "\n\nA reviewer found these problems in an earlier attempt; fix them:\n" + feedback
        task = self.research.start(step.title[:80], instruction[:20_000], "notify")
        step.activity = "researching"
        self._save()
        try:
            await self.research.wait(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(self.research.cancel(task))
            raise
        if task.status != "done" or not task.file:
            step.problem = f"The research didn't finish: {task.error or task.summary or task.status}."
            return False, step.problem
        step.file, step.result = task.file, task.summary
        try:
            text = Path(task.file).read_text(encoding="utf-8")
        except OSError as exc:
            step.problem = f"Its result couldn't be read: {exc}"
            return False, step.problem
        step.status, step.activity = CHECKING, "being reviewed"
        self._save()
        cwd = self._scratch("review", objective.id)
        review = await self._session(
            self.settings.reviewer,
            objective,
            cwd=cwd,
            prompt=prompts.review_research_prompt(objective.goal, step.title, step.acceptance, text[:40_000]),
            system=prompts.reviewer_system(self.settings.name, self.settings.user, RESEARCH, self.settings.web),
            tools=guard.WEB_TOOLS if self.settings.web else guard.READ_TOOLS,
            check=guard.reader(cwd, web=self.settings.web),
            schema=prompts.REVIEW_SCHEMA,
            max_turns=self.settings.review_max_turns,
            timeout_s=self.settings.review_timeout_s,
        )
        return self._verdict(step, review, checks="")

    async def _code_attempt(
        self, objective: Objective, step: Step, feedback: str | None, start_commit: str
    ) -> tuple[bool | None, str | None]:
        project = await self._project(objective)
        if project is None or not objective.worktree:
            step.problem = f"The project {objective.project} isn't there any more."
            return None, None
        worktree = Path(objective.worktree)
        step.activity = "coding"
        self._save()

        def progress(line: str) -> None:
            step.activity = line

        python = project.path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        env = check_environment(project, worktree, self.settings.folder / "cache")
        if project.check and project.check[0] == "python":
            env["PATH"] = f"{python}{os.pathsep}{os.environ.get('PATH', '')}"
        async with contextlib.AsyncExitStack() as stack:
            await asyncio.to_thread(stack.enter_context, self._borrowed(objective, project))
            result = await self._code_session(objective, step, project, worktree, feedback, env, progress)
        if result is None:
            return None, None
        return await self._after_code(objective, step, project, worktree, result, start_commit)

    async def _code_session(
        self,
        objective: Objective,
        step: Step,
        project: Project,
        worktree: Path,
        feedback: str | None,
        env: dict[str, str],
        progress: Callable[[str], None],
    ) -> SessionResult | None:
        return await self._session(
            self.settings.coder,
            objective,
            cwd=worktree,
            prompt=prompts.code_prompt(
                objective.goal, step.title, step.instruction, step.acceptance, self._earlier(objective, step), feedback
            ),
            system=prompts.coder_system(
                self.settings.name,
                self.settings.user,
                guard.allowed_hint(project.check, guard.commands_for(worktree, project.check)),
            ),
            tools=guard.CODE_TOOLS,
            check=guard.coder(worktree, project.check),
            schema=prompts.CODE_SCHEMA,
            max_turns=self.settings.code_max_turns,
            timeout_s=self.settings.code_timeout_s,
            env=env,
            progress=progress,
        )

    async def _after_code(
        self,
        objective: Objective,
        step: Step,
        project: Project,
        worktree: Path,
        result: SessionResult,
        start_commit: str,
    ) -> tuple[bool | None, str | None]:
        data = result.data or {}
        step.result = str(data.get("summary") or result.text or "")[:1000]
        if not result.ok:
            step.problem = f"The coding session didn't finish: {result.error}."
            return False, step.problem
        if data.get("outcome") == "gave_up":
            step.problem = f"It gave up: {step.result}"
            return False, step.problem
        git = self._git(objective)
        try:
            await asyncio.to_thread(
                self._commit, objective, worktree, f"Objective #{objective.id}, step {step.id}: {step.title}"
            )
            changed = await asyncio.to_thread(git.changed_files, worktree, start_commit)
            diff = await asyncio.to_thread(git.diff, worktree, start_commit, DIFF_CHARS)
        except GitError as exc:
            step.problem = f"Its change couldn't be committed: {exc}"
            return False, step.problem
        bad = [path for _, path in changed if guard.protected(path)]
        if bad:
            step.problem = f"It changed files it may not change: {', '.join(bad[:5])}."
            return False, step.problem
        if not changed and data.get("outcome") != "no_change":
            step.problem = "It said it changed the code, but nothing changed."
            return False, step.problem
        step.status, step.activity = CHECKING, "running the project's checks"
        self._save()
        run = await self._check(objective, project)
        checks = run.describe()
        if objective.baseline and objective.baseline.startswith("failed"):
            checks += "\n(The checks already failed before any change on this branch, so a failure isn't this step's.)"
        gate_on_checks = run.ran and objective.baseline == "passed"
        if gate_on_checks and not run.passed:
            step.evidence = "The project's checks failed after the change."
            step.problem = "The project's checks failed after the change."
            return False, f"The project's check ({' '.join(project.check or ())}) failed after your change.\n{checks}"
        step.activity = "being reviewed"
        self._save()
        review = await self._session(
            self.settings.reviewer,
            objective,
            cwd=worktree,
            prompt=prompts.review_code_prompt(
                objective.goal,
                step.title,
                step.acceptance,
                step.result,
                diff,
                checks,
                (objective.baseline or "").removeprefix("failed: ") if not gate_on_checks and run.ran else "",
            ),
            system=prompts.reviewer_system(self.settings.name, self.settings.user, CODE, web=False),
            tools=guard.READ_TOOLS,
            check=guard.reader(worktree, web=False),
            schema=prompts.REVIEW_SCHEMA,
            max_turns=self.settings.review_max_turns,
            timeout_s=self.settings.review_timeout_s,
        )
        passed, feedback = self._verdict(step, review, checks=_checks_evidence(run, objective.baseline))
        if passed:
            step.commit = await asyncio.to_thread(self._head, objective)
        return passed, feedback

    def _verdict(self, step: Step, review: SessionResult | None, *, checks: str) -> tuple[bool | None, str | None]:
        if review is None:
            return None, None
        data = review.data or {}
        if not review.ok or "passed" not in data:
            step.problem = f"The review didn't finish: {review.error or 'no verdict'}."
            return False, step.problem
        reasons = str(data.get("reasons") or "").strip()
        problems = [str(p).strip() for p in data.get("problems") or [] if str(p).strip()]
        step.evidence = " ".join(part for part in (checks, f"Reviewer: {reasons}" if reasons else "") if part)
        if data.get("passed") is True:
            return True, None
        step.problem = problems[0] if problems else (reasons or "The reviewer failed it.")
        return False, "\n".join(f"- {p}" for p in problems) or reasons

    # ------------------------------------------------------------------ sessions

    async def _session(
        self, settings: SessionSettings, objective: Objective, *, asked: bool = False, **kwargs: Any
    ) -> SessionResult | None:
        """One guarded session, after the usage budget allows it (planning, which the user just asked for, doesn't
        wait). None when the objective ended while it waited."""
        if not asked:
            while (why := await self._budget_for(objective)()) is not None:
                objective.waiting = why
                self._save()
                await asyncio.sleep(self.settings.budget_wait_s)
                if not objective.open:
                    return None
            objective.waiting = ""
        return await run_session(settings, client_factory=self._client_factory, **kwargs)

    async def _project(self, objective: Objective) -> Project | None:
        """The objective's project as it is now (looking runs git: off the event loop)."""
        if not objective.project_path:
            return None
        path = Path(objective.project_path)
        try:
            found = await asyncio.to_thread(self._projects)
        except Exception:
            log.warning("Couldn't list the projects", exc_info=True)
            found = []
        return next((p for p in found if os.path.normcase(str(p.path)) == os.path.normcase(str(path))), None)

    def _scratch(self, kind: str, objective_id: int) -> Path:
        folder = self.settings.folder / "work" / f"{objective_id}-{kind}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # ------------------------------------------------------------------ merging

    async def merge(self, objective: Objective) -> str:
        """Merge the objective's verified code into the project's checked-out branch; returns what to tell the user.
        Raises RuntimeError with a speakable reason when it can't."""
        if not objective.branch or not objective.project_path or not objective.worktree:
            raise RuntimeError(f"“{objective.title}” has no code to merge")
        if objective.open:
            raise RuntimeError(f"“{objective.title}” is still being worked on; I'll merge once it's done")
        if objective.merged:
            raise RuntimeError(f"“{objective.title}” is already merged")
        if not objective.code_to_merge:
            raise RuntimeError(
                f"no code step of “{objective.title}” changed anything and passed, so there's nothing to merge"
            )
        said, commit = await asyncio.to_thread(self._merge, objective)
        objective.merged = commit
        self._save()
        return said

    def target_branch(self, objective: Objective) -> str | None:
        """The branch the project has checked out, which a merge goes into (None: a detached HEAD, or unknown)."""
        if not objective.project_path:
            return None
        try:
            name = self._git(objective).run("rev-parse", "--abbrev-ref", "HEAD", timeout=20).strip()
        except GitError:
            return None
        return None if name in ("", "HEAD") else name

    def _merge(self, objective: Objective) -> tuple[str, str]:
        """Merge exactly the verified commit (never whatever else is on the branch). (what to say, the merge commit)"""
        git = self._git(objective)
        verified = objective.verified_head()
        assert verified is not None
        current = self.target_branch(objective)
        if current is None:
            raise RuntimeError(
                f"{objective.project} isn't on a branch right now (a detached HEAD); check one out first"
            )
        try:
            if git.run("status", "--porcelain", "--untracked-files=no").strip():
                raise RuntimeError(
                    f"{objective.project} has uncommitted changes; commit or stash them and ask me again"
                )
            commit = git.merge_no_ff(verified, f"Merge objective #{objective.id}: {objective.title}", self.author)
        except MergeConflictError as exc:
            raise RuntimeError(
                f"the branch clashes with changes made to {objective.project} since; nothing was merged ({exc})"
            ) from exc
        except GitError as exc:
            raise RuntimeError(f"git couldn't merge it: {exc}") from exc
        with contextlib.suppress(GitError, OSError):
            _unlink_junctions(Path(objective.worktree or ""))
            git.remove_worktree(Path(objective.worktree or ""))
        said = f"Merged “{objective.title}” into {objective.project}'s {current} branch as one commit ({commit[:8]})."
        return said, commit


# ---------------------------------------------------------------------------------------------------- the plan


def apply_plan(objective: Objective, data: dict[str, Any], *, coding: bool, self_work: bool = False) -> str | None:
    """Validate the planner's answer and put it on the objective; why it's unusable otherwise."""
    raw = data.get("steps")
    if not isinstance(raw, list) or not raw:
        return "the plan had no steps"
    steps: list[Step] = []
    for number, item in enumerate(raw[:MAX_STEPS], start=1):
        if not isinstance(item, dict):
            return f"step {number} wasn't readable"
        kind = str(item.get("kind") or "")
        if kind not in KINDS:
            return f"step {number} has an unknown kind ({kind!r})"
        if kind == CODE and not coding:
            return "it needs code changes, but there's no project with commits to make them in"
        if kind == SELF and not self_work:
            return "it needs a change to me, but my self-improvement is off"
        title = " ".join(str(item.get("title") or "").split())[:80]
        instruction = str(item.get("instruction") or "").strip()
        acceptance = str(item.get("acceptance") or "").strip()
        if not title or not instruction or not acceptance:
            return f"step {number} is missing its title, instruction or acceptance criteria"
        deps = []
        for dep in item.get("depends_on") or []:
            try:
                dep = int(dep)
            except (TypeError, ValueError):
                return f"step {number} depends on something that isn't a step number"
            if not 1 <= dep < number:
                return f"step {number} depends on step {dep}, which doesn't come before it"
            deps.append(dep)
        steps.append(Step(number, title, kind, instruction[:4000], acceptance[:2000], sorted(set(deps))))
    title = " ".join(str(data.get("title") or "").split())[:60]
    objective.title = title or objective.title
    objective.approach = str(data.get("approach") or "").strip()[:600]
    dropped = len(raw) - len(steps)
    not_doing = str(data.get("not_doing") or "").strip()[:600]
    if dropped > 0:
        not_doing = f"{not_doing} (I also dropped {dropped} step{'s' if dropped > 1 else ''} past {MAX_STEPS}.)".strip()
    objective.not_doing = not_doing
    objective.steps = steps
    return None


def _checks_evidence(run: CheckRun, baseline: str | None) -> str:
    if not run.ran:
        return "Not tested: the project has no check."
    if run.passed:
        return f"The project's checks passed ({run.seconds:.0f} s)."
    if baseline and baseline.startswith("failed"):
        return "The project's checks fail, as they already did before any change."
    return "The project's checks failed."


# ---------------------------------------------------------------------------------------------------- telling


def ending(objective: Objective) -> tuple[str, str]:
    """(the line said when an objective ends, the note her next turn gets)."""
    title = objective.title
    if objective.status == OBJECTIVE_FAILED:
        line = f"I couldn't get “{title}” going: {objective.error or 'something went wrong'}."
        return line, f"Objective #{objective.id} “{title}” failed: {objective.error}"
    total = len(objective.steps)
    verified = sum(1 for s in objective.steps if s.status == VERIFIED)
    code = objective.code_to_merge
    if objective.status == DONE:
        passed = "it passed its checks" if total == 1 else f"all {total} steps passed their checks"
        line = f"I finished “{title}”: {passed}."
    else:
        failed = [s for s in objective.steps if s.status in (FAILED, SKIPPED)]
        problem = spoken_summary(failed[0].problem, 160) if failed else ""
        why = f" The first one that didn't: {failed[0].title}, because {_lower(problem)}" if failed else ""
        line = f"I've done what I could on “{title}”: {verified} of {total} steps passed their checks.{why}"
    if code:
        line += f" The code is on a branch of {objective.project}; say the word and I'll merge it."
    updates = [s.improvement for s in objective.steps if s.status == VERIFIED and s.awaiting_install]
    if updates:
        line += " The change to me is built and checked; say the word and I'll install it."
    note = f"Objective #{objective.id} “{title}” ended ({objective.status}). objective_status reads the details" + (
        "; merge_objective_work merges its verified code when the user says so." if code else "."
    )
    return line.strip(), note


def _lower(text: str) -> str:
    text = text.strip().rstrip(".")
    return (text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text) + "."


def _kill_tree(pid: int) -> None:
    """Stop a check and everything it started (cargo and npm start child processes)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)


def _unlink(path: Path) -> None:
    """Remove a borrowed folder's link, never what it points to."""
    try:
        if path.is_symlink():
            os.unlink(path)
        elif os.path.isjunction(path):
            os.rmdir(path)  # removes the junction itself
    except OSError:
        log.warning("Couldn't remove the link %s", path, exc_info=True)


def _unlink_junctions(worktree: Path) -> None:
    """Remove every borrowed folder link at the top of the worktree before git touches its files or it's deleted."""
    for child in worktree.iterdir() if worktree.is_dir() else ():
        if os.path.isjunction(child) or child.is_symlink():
            _unlink(child)
