"""Running a skill inside a task, and checking a skill a task proposes against what it really did.

``SkillExecutor`` wraps a task's executor (outside the playbooks' ``PlaybookExecutor``), the way that one does:
everything else is the wrapped executor's. It answers two calls itself:

* ``use_skill``: the skill's steps are run one by one through the wrapped executor, so each gets every check the
  task's own steps get (the screen task's look-after-a-failure rule and its questions out loud, the web task's
  send/buy/delete rules, the user pausing or stopping the task) and is written down for the playbooks. It stops at
  the first step that fails or doesn't show what it expects, and says which and why; the rules (``rules.check``) are
  checked again first, since a tool or another skill may have changed since it was saved. Each run is counted
  (``Skills.record_run``): a skill that fails ``OFF_AFTER`` times in a row is turned off, and every failure is said
  out loud once per task (``Library.failed``).
* ``propose_skill``: a skill the task worked out, kept as a proposal for the user's yes (``save_skill``) only when
  its rules pass and its steps are steps this task took and that went through, in order (``matches_calls``): a
  proposal can't hold a step nobody has seen work.

It also keeps the task's own calls (``calls``), for that check and for the repeats (``repeats``); the steps a skill
ran aren't among them, since they're the skill's, not the task's.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from octavia.playbooks.book import HINTS
from octavia.skills import rules
from octavia.skills.skill import Skill, Step, skill_from
from octavia.skills.tools import ProposeSkillArgs, UseSkillArgs
from octavia.tools.base import ToolResult

if TYPE_CHECKING:
    from octavia.skills.wiring import Library

log = logging.getLogger(__name__)

OFF_AFTER = 3  # failures in a row that turn a skill off
MAX_CALLS = 300  # of a task's own calls kept for the checks
RESULT_CHARS = 2000  # of the last step's result handed back
MAX_PROPOSALS = 2  # per task
PLAYBOOK_HINT = HINTS.split("{name}")[0]  # how a playbook handed over in a result begins
_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("skill_depth", default=0)  # skills running in this call
# Steps that only look: where a proposal's steps are matched against the task's, a look may stand anywhere.
LOOKS = frozenset({"look_at_screen", "zoom_screen", "find_on_screen", "wait_for_screen_change", "web_read",
                   "web_screenshot", "get_current_time"})  # fmt: skip


@dataclass(frozen=True)
class Call:
    """One of the task's own calls: the tool, its arguments as given, whether it went through, and where it was
    (a playbook's place key, or None somewhere private)."""

    tool: str
    args: Mapping[str, Any]
    ok: bool
    place: str | None = ""


@dataclass
class Run:
    """How one use of a skill went."""

    skill: Skill
    done: int = 0  # steps that went through, the skills it ran counted step by step
    total: int = 0
    failed: str = ""  # the step that failed, in words ("step 4 of 7 (click label=Submit)")
    why: str = ""
    last: str = ""  # the last result
    stopped: bool = False  # a step was refused or said no to, or the task was paused or stopped: not its fault
    playbooks: list[str] = field(default_factory=list)  # handed over during its steps, passed on to the task


def matches_calls(skill: Skill, calls: list[Call], lookup: rules.Lookup = lambda name: None) -> str:
    """Why ``skill``'s steps aren't steps from ``calls`` that went through in an app or site she knows (not somewhere
    private, not unknown), in the same order, or "" when they are. The skills it runs are matched step by step as it
    fills them in. A step takes the very arguments the call had, no more and no fewer (a click the task marked risky
    stays marked); one whose text holds an input matches any value there. Looks aren't matched (they change nothing),
    but a skill of nothing else isn't one."""
    good = [c for c in calls if c.ok and c.place]
    at, acts = 0, 0
    for label, step in rules.expand(skill, lookup, {i.name: "{" + i.name + "}" for i in skill.inputs}):
        if step.tool in LOOKS:
            continue
        while at < len(good) and not _same(step, good[at]):
            at += 1
        if at == len(good):
            return f"{label} ({step.words()}) isn't a step this task took and saw go through, in that order"
        at, acts = at + 1, acts + 1
    return "" if acts else "it only looks, so there's nothing to save"


def _same(step: Step, call: Call) -> bool:
    if step.tool != call.tool or set(step.args) != set(call.args):
        return False
    for key, value in step.args.items():
        if isinstance(value, str) and rules.INPUT.search(value):
            continue  # what the input stood for this time doesn't matter
        if call.args[key] != value and str(call.args[key]) != str(value):
            return False
    return True


# The task's own refusals and the user's "no": the step was stopped, it didn't fail. Not the skill's fault, so not
# counted against it (``TaskExecutor`` and ``ToolExecutor`` word them so).
_NOT_ITS_FAULT = ("Not done:", "The user did not approve")


def _innermost(executor: Any) -> Any:
    """The executor at the bottom of the wrappers (``inner`` of ``inner`` ...): a screen task's ``TaskExecutor``."""
    seen = 0
    while "inner" in getattr(executor, "__dict__", {}) and seen < 10:
        executor, seen = executor.__dict__["inner"], seen + 1
    return executor


def _split_playbooks(content: str) -> tuple[str, list[str]]:
    """A step's result without the playbooks handed over with it (``PlaybookExecutor``), and those playbooks."""
    marker = "\n\n" + PLAYBOOK_HINT
    if marker not in content:
        return content, []
    head, *blocks = content.split(marker)
    return head, [PLAYBOOK_HINT + block for block in blocks]


class SkillExecutor:
    """A task's executor with skills: ``use_skill`` and ``propose_skill`` are answered here (see the module), every
    other call goes to the wrapped executor."""

    def __init__(
        self,
        inner: Any,
        library: Library,
        kind: str,
        task: Any,
        *,
        place: Callable[[], str | None] = lambda: "",
    ) -> None:
        self.inner = inner
        self.library = library
        self.kind = kind
        self.task = task
        self._place = place
        self.calls: list[Call] = []
        self.proposed: list[str] = []  # the names of the skills this task proposed
        self.used: list[Run] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def execute(self, name: str, raw_args: dict[str, Any] | None, call_id: str | None = None) -> ToolResult:
        if name in ("use_skill", "propose_skill"):
            model = UseSkillArgs if name == "use_skill" else ProposeSkillArgs
            try:
                args = model.model_validate(raw_args or {}).model_dump()
            except ValidationError as exc:
                first = exc.errors()[0] if exc.errors() else {}
                field = ".".join(str(p) for p in first.get("loc", ())) or "the arguments"
                return ToolResult.error(f"Invalid arguments for {name}: {field}: {first.get('msg', 'invalid')}.")
            return await (self._use(args) if name == "use_skill" else self._propose(args))
        result = await self.inner.execute(name, raw_args, call_id)
        if _DEPTH.get() == 0:
            try:
                where = self._place()
            except Exception:
                where = ""
            self.calls.append(Call(name, dict(raw_args or {}), result.ok, where))
            del self.calls[:-MAX_CALLS]
        return result

    # ------------------------------------------------------------------ use_skill

    async def _use(self, args: Mapping[str, Any]) -> ToolResult:
        name = str(args.get("name", "")).strip().casefold()
        given = args.get("inputs") or {}
        if not isinstance(given, Mapping):
            return ToolResult.error("inputs must be an object of input names and values.")
        skill = await self.library.skills.get(name)
        if skill is None:
            return ToolResult.error(f"There's no skill “{name}”. Do it step by step instead.")
        if not skill.usable:
            why = "the user hasn't said yes to it yet" if skill.state == "proposed" else "it's turned off"
            return ToolResult.error(f"The skill “{name}” can't be used: {why}. Do it step by step instead.")
        if skill.kind != self.kind:
            return ToolResult.error(f"The skill “{name}” is for a {skill.kind}, not this {self.kind}.")
        values, why = rules.check_inputs(skill, given)
        if why:
            return ToolResult.error(f"The skill “{name}” wasn't run: {why}.")
        if _DEPTH.get():  # skills run others as skill steps, never through use_skill
            return ToolResult.error("A skill can't call use_skill.")
        bottom = _innermost(self.inner)
        if getattr(bottom, "unchecked", None):
            return ToolResult.error(
                f"Not run: an earlier step ({bottom.unchecked}) didn't go through. Look at the screen first, then use "
                "the skill if it still fits."
            )
        problems = await self.library.problems(skill)
        if problems:
            run = Run(skill, failed="its checks", why="; ".join(problems[:3]))
            await self._finish(run, False)
            self._must_look(bottom, f"the skill {name}")
            return ToolResult.error(
                f"The skill “{name}” wasn't run: it no longer passes its checks ({run.why}). Do it step by step "
                "instead, and say in your final reply that the skill needs fixing."
            )
        run = Run(skill)
        token = _DEPTH.set(_DEPTH.get() + 1)
        try:
            ok = await self._steps(skill, values, run, prefix="")
        finally:
            _DEPTH.reset(token)
        playbooks = "".join(f"\n\n{block}" for block in run.playbooks)
        if not ok and (run.stopped or getattr(self.task, "status", "running") != "running"):
            return ToolResult.error(
                f"The skill “{skill.title}” stopped at {run.failed}: {run.why} {_done_words(run.done)} "
                "Do what that says." + playbooks
            )
        await self._finish(run, ok)
        trial = " It was on trial; this was its first clean run." if ok and skill.status == "trial" else ""
        if ok:
            last = run.last[:RESULT_CHARS]
            return ToolResult.success(
                f"Ran the skill “{skill.title}” (v{skill.version}): all {run.done} steps went through.{trial}"
                + (f"\nThe last step said: {last}" if last else "")
                + playbooks
            )
        self._must_look(bottom, f"the skill {skill.name}, {run.failed}")
        return ToolResult.error(
            f"The skill “{skill.title}” (v{skill.version}) stopped at {run.failed}: {run.why} "
            f"{_done_words(run.done)} The screen or page may be partway through it: look first, then finish by hand "
            "or say what's left, and say in your final reply that the skill failed." + playbooks
        )

    @staticmethod
    def _must_look(bottom: Any, what: str) -> None:
        """After a skill that didn't go through, the screen task's own rule holds as after any failed step: no mouse
        or keyboard step until it has looked again (``TaskExecutor.unchecked``)."""
        if "unchecked" in getattr(bottom, "__dict__", {}) and not bottom.unchecked:
            bottom.unchecked = what

    async def _steps(self, skill: Skill, inputs: Mapping[str, str], run: Run, prefix: str) -> bool:
        count = len(skill.steps)
        for number, step in enumerate(skill.steps, 1):
            label = f"{prefix}step {number} of {count} ({step.words()})"
            if step.skill:
                other = await self.library.skills.get(step.skill)
                if other is None or not other.usable:
                    run.failed, run.why = label, f"the skill {step.skill} isn't there or is turned off."
                    return False
                sub = {k: str(rules.fill(v, inputs)) for k, v in step.inputs.items()}
                if not await self._steps(other, sub, run, prefix=f"{label}, "):
                    return False
                continue
            filled = {key: rules.fill(value, inputs) for key, value in step.args.items()}
            run.total += 1
            result = await self.inner.execute(step.tool, filled)
            content, books = _split_playbooks(result.content)
            run.playbooks += books
            if not result.ok:
                run.failed, run.why = label, _sentence(_first_line(content) or "it didn't go through")
                run.stopped = content.startswith(_NOT_ITS_FAULT)
                return False
            if step.expect and step.expect.casefold() not in content.casefold():
                run.failed, run.why = label, f"its result didn't show “{step.expect}”."
                return False
            run.done += 1
            run.last = content
        return True

    async def _finish(self, run: Run, ok: bool) -> None:
        self.used.append(run)
        try:
            await self.library.ran(self.task, self.kind, run, ok)
        except Exception:
            log.warning("Couldn't count the run of skill %s", run.skill.name, exc_info=True)

    # ------------------------------------------------------------------ propose_skill

    async def _propose(self, args: Mapping[str, Any]) -> ToolResult:
        if _DEPTH.get():
            return ToolResult.error("A skill can't propose skills.")
        if len(self.proposed) >= MAX_PROPOSALS:
            return ToolResult.error(f"This task has proposed {MAX_PROPOSALS} skills already; that's enough for one.")
        data = dict(args)
        data.pop("state", None)
        data.pop("origin", None)
        data.pop("version", None)
        try:
            skill = skill_from({**data, "kind": self.kind, "state": "proposed", "origin": "task"})
        except ValueError as exc:
            return ToolResult.error(f"That isn't a skill: {exc}.")
        skill = replace(skill, name=skill.name.casefold(), proposed_at=time.time())
        existing = await self.library.skills.get(skill.name)
        if existing is not None:
            return ToolResult.error(f"There's already a skill “{skill.name}”: pick another name.")
        problems = await self.library.problems(skill)
        if problems:
            return ToolResult.error("Not proposed: " + "; ".join(problems[:5]) + ".")
        known = {s.name: s for s in await self.library.skills.all()}
        why = matches_calls(skill, self.calls, known.get)
        if why:
            return ToolResult.error(
                f"Not proposed: {why}. Propose only steps you took in this task and saw work, with what changes "
                "each time as {inputs}."
            )
        saved = await self.library.propose(skill, self.task)
        if saved is None:
            return ToolResult.error("Not proposed: there are too many proposals waiting for the user already.")
        self.proposed.append(saved.name)
        return ToolResult.success(
            f"Proposed the skill “{saved.title}”. The user is asked about it when this task ends; it can't be used "
            "until they say yes."
        )


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else text + "."


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:300]


def _done_words(done: int) -> str:
    if done == 0:
        return "None of its steps went through."
    return f"The {done} step{'s' if done != 1 else ''} before it went through."
