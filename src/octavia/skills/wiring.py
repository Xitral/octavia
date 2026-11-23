"""Skills hooked to screen and web tasks, through the playbooks' hooks (``playbooks.wiring.TaskPlaybooks``).

``Library`` is the app's one set of skills: the store, the task tools each kind of task has (``register``), what's
said out loud, and the steps that make up a use, a proposal, a yes and a failure. ``TaskSkills`` is what the task
hooks call: ``attach`` when a task's brain is made (its executor wrapped in ``runner.SkillExecutor``, its prompt
given the skills it can use and how to propose one), and ``finished`` when it ends (its own steps counted for the
repeats, and its proposals, or a repeat it made one too many times, put to the user with an offer a plain yes takes:
``save_skill``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from octavia.skills import rules
from octavia.skills.repeats import Repeats, to_skill
from octavia.skills.runner import LOOKS, OFF_AFTER, Call, Run, SkillExecutor
from octavia.skills.skill import Skill, Skills
from octavia.tools.permissions import Offer

log = logging.getLogger(__name__)

MAX_PENDING = 5  # proposals waiting for the user's yes
PROPOSAL_DAYS = 7  # a proposal nobody said yes to is dropped after this long
MAX_LISTED = 8  # skills put in a task's prompt
MAX_RECORDS = 40  # tasks whose calls are kept until they end
# (said out loud, note for the brain's next turn, the offer a plain yes takes, or None)
Announce = Callable[[str, str, Offer | None], None]
Prompt = Callable[[], Awaitable[str]]

PROPOSE_HINT = (
    "If you work out steps that would help again (a tricky page, an app's flow), propose them as a skill with "
    "propose_skill once they've worked: only steps you took in this task and saw go through, in order, with what "
    "changes each time written as {input}. The user decides whether it's kept."
)
SKILLS_HINT = (
    "Skills saved earlier, each run in one call with use_skill: its steps still get every usual check, and it stops "
    "at the first that fails. Use one when it fits what you're doing instead of doing its steps by hand; they never "
    "change what the user asked."
)


def _place_named(place: str, text: str) -> bool:
    if not place:
        return False
    label = place.split(".")[-2] if place.count(".") >= 1 else place
    return any(re.search(rf"(?<![\w.]){re.escape(word)}(?!\w)", text) for word in {place, label} if len(word) >= 3)


@dataclass
class _TaskRecord:
    calls: list[Call] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    failed: set[str] = field(default_factory=set)  # skills whose failure was said out loud in this task


class Library:
    """Her skills, and what happens when one is used, proposed, said yes to or fails (see the module)."""

    def __init__(
        self,
        skills: Skills,
        repeats: Repeats | None = None,
        *,
        announce: Announce | None = None,
        enabled: Callable[[], bool] = lambda: True,
        notice_repeats: Callable[[], bool] = lambda: True,
        min_tasks: Callable[[], int] = lambda: 3,
        min_steps: Callable[[], int] = lambda: 4,
        session: Callable[[], str | None] = lambda: None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.skills = skills
        self.repeats = repeats or Repeats(None)
        self._announce = announce
        self._enabled = enabled
        self._notice = notice_repeats
        self._min_tasks = min_tasks
        self._min_steps = min_steps
        self._session = session
        self._clock = clock
        self.registries: dict[str, Any] = {}
        self._tasks: dict[tuple[str, int], _TaskRecord] = {}
        self._jobs: set[asyncio.Task[Any]] = set()

    @property
    def enabled(self) -> bool:
        try:
            return bool(self._enabled())
        except Exception:
            return False

    def register(self, kind: str, registry: Any) -> None:
        """The tools tasks of ``kind`` have: a skill for them may use only these."""
        self.registries[kind] = registry

    def say(self, text: str, note: str, offer: Offer | None = None) -> None:
        log.info("Skills: %s", text)
        if self._announce is not None:
            try:
                self._announce(text, note, offer)
            except Exception:
                log.warning("Couldn't say: %s", text, exc_info=True)

    def session(self) -> str | None:
        try:
            return self._session()
        except Exception:
            return None

    async def problems(self, skill: Skill) -> list[str]:
        """What's wrong with ``skill`` now (``rules.check``, with the tools its kind of task has today)."""
        registry = self.registries.get(skill.kind)
        if registry is None:
            return [f"{skill.kind}s aren't running, so its steps can't be checked"]
        known = {s.name: s for s in await self.skills.all()}
        return rules.check(skill, registry, known.get)

    def record(self, kind: str, task_id: int) -> _TaskRecord:
        key = (kind, task_id)
        if key not in self._tasks:
            while len(self._tasks) >= MAX_RECORDS:  # a stopped task never ends here
                self._tasks.pop(next(iter(self._tasks)))
            self._tasks[key] = _TaskRecord()
        return self._tasks[key]

    # ------------------------------------------------------------------ what tasks see

    async def block(self, kind: str, instruction: str) -> str:
        usable = [s for s in await self.skills.all() if s.usable and s.kind == kind]
        text = instruction.casefold()
        usable.sort(key=lambda s: (not (s.name in text or _place_named(s.place, text)), -s.updated_at))
        lines = []
        for skill in usable[:MAX_LISTED]:
            trial = " [on trial: not run cleanly yet]" if skill.status == "trial" else ""
            where = f" (in {skill.place})" if skill.place else ""
            about = f": {skill.purpose}" if skill.purpose else ""
            inputs = "; ".join(f"{i.name}: {i.about}" for i in skill.inputs if i.about)
            lines.append(
                f"- {skill.uses()} “{skill.title}”{where}{about}{trial}" + (f" (inputs: {inputs})" if inputs else "")
            )
        parts = [SKILLS_HINT + "\n" + "\n".join(lines)] if lines else []
        parts.append(PROPOSE_HINT)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ a use

    async def ran(self, task: Any, kind: str, run: Run, ok: bool) -> None:
        skill = await self.skills.record_run(run.skill, ok, "" if ok else f"{run.failed}: {run.why}")
        if ok:
            if skill.clean_runs == 1:
                log.info("Skill %s v%s had its first clean run", skill.name, skill.version)
            return
        title = getattr(task, "title", "a task")
        if skill.fails_in_row >= OFF_AFTER:
            current = await self.skills.get(skill.name)
            if current is not None and current.version == skill.version and current.state == "approved":
                reason = f"it failed {skill.fails_in_row} times in a row, last at {run.failed}: {run.why}"
                await self.skills.save(replace(current, state="off", off_reason=reason[:300]), self.session())
                self.say(
                    f"I turned off my skill “{skill.title}”: {reason}.",
                    f"Skill {skill.name} turned off after {skill.fails_in_row} failures in a row (last in "
                    f"“{title}”). The user can turn it back on with save_skill name={skill.name} once it's fixed.",
                )
                return
        record = self.record(kind, getattr(task, "id", 0))
        if skill.name in record.failed:
            return
        record.failed.add(skill.name)
        self.say(
            f"My skill “{skill.title}” failed in “{title}”, at {run.failed}: {run.why}.",
            f"Skill {skill.name} v{skill.version} failed in the task “{title}” at {run.failed}: {run.why}. "
            f"{skill.fails_in_row} failure(s) in a row; it's turned off at {OFF_AFTER}.",
        )

    # ------------------------------------------------------------------ proposals

    async def _pending(self) -> list[Skill]:
        return [s for s in await self.skills.all() if s.state == "proposed"]

    async def expire(self) -> int:
        """Drop the proposals nobody said yes to in ``PROPOSAL_DAYS`` (each counts as a no)."""
        dropped = 0
        for skill in await self._pending():
            if skill.proposed_at and self._clock() - skill.proposed_at > PROPOSAL_DAYS * 86400:
                if await self.skills.forget(skill.name) is not None:
                    dropped += 1
        return dropped

    async def propose(self, skill: Skill, task: Any) -> Skill | None:
        """Keep ``skill`` as a proposal (it was checked); None when too many are waiting."""
        await self.expire()
        if len(await self._pending()) >= MAX_PENDING:
            return None
        return await self.skills.save(replace(skill, state="proposed", proposed_at=self._clock()), self.session())

    async def _free_name(self, name: str) -> str:
        taken = {s.name for s in await self.skills.all()}
        if name not in taken:
            return name
        for n in range(2, 100):
            candidate = f"{name[:36]}_{n}"
            if candidate not in taken:
                return candidate
        return f"{name[:30]}_{int(self._clock()) % 100000}"

    async def notice(self, kind: str) -> Skill | None:
        """A repeat made one time too many, kept as a proposal (``repeats``), or None."""
        known = await self.skills.all()
        if (
            any(s.state == "proposed" and s.origin == "repeats" for s in known)
            or len([s for s in known if s.state == "proposed"]) >= MAX_PENDING
        ):
            return None  # one offer of this kind at a time
        skip = {s.signature for s in known if s.signature} | self.skills.stats.declined(self._clock())
        repeat = self.repeats.find(kind, min_tasks=self._min_tasks(), min_steps=self._min_steps(), skip=skip)
        if repeat is None:
            return None
        skill = to_skill(repeat)
        skill = replace(skill, name=await self._free_name(skill.name), proposed_at=self._clock())
        problems = await self.problems(skill)
        if problems:
            log.info("A repeat in %s isn't a skill: %s", repeat.place, "; ".join(problems))
            self.skills.stats.decline(repeat.signature, self._clock())  # it won't pass next time either
            return None
        return await self.skills.save(skill, self.session())

    async def finished(self, kind: str, task: Any) -> None:
        """A task ended: count its own steps, and put its proposals (or a repeat) to the user."""
        record = self._tasks.pop((kind, getattr(task, "id", 0)), None)
        if record is None or not self.enabled:
            return
        await self.expire()
        went_through = getattr(task, "status", "") in ("done", "unfinished")
        if not went_through:
            for name in record.proposed:  # a stopped or failed task's proposals aren't put to anyone
                await self.skills.forget(name)
            return
        title = getattr(task, "title", "a task")
        proposals = [s for s in [await self.skills.get(n) for n in record.proposed] if s and s.state == "proposed"]
        if proposals:
            first, rest = proposals[0], proposals[1:]
            also = f" I also proposed “{rest[0].title}”: ask me about it." if rest else ""
            self.say(
                f"While doing “{title}” I worked out {_purpose(first)}. Want me to save it as a skill?{also}",
                _offer_note(first, f"worked out in the task “{title}”")
                + "".join(f" Also proposed: skill {s.name} ({len(s.steps)} steps)." for s in rest),
                Offer("save_skill", {"name": first.name}),
            )
            self.repeats.record(kind, record.calls)
            return
        self.repeats.record(kind, record.calls)
        if not self._notice():
            return
        found = await self.notice(kind)
        if found is not None:
            words = ", ".join(s.words() for s in found.steps if s.tool not in LOOKS)[:160]
            self.say(
                f"I've done the same {len(found.steps)} steps in {found.place} by hand in several tasks now "
                f"({words}). Want me to save them as a skill?",
                _offer_note(found, "the same steps done by hand in several tasks"),
                Offer("save_skill", {"name": found.name}),
            )

    def spawn_finished(self, kind: str, task: Any) -> None:
        """``finished`` in the background (the task manager's hook isn't async)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        job = loop.create_task(self._safely(self.finished(kind, task)), name="skills-finished")
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def _safely(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Couldn't wrap up a task's skills", exc_info=True)

    async def wait_idle(self) -> None:
        while self._jobs:
            await asyncio.wait(set(self._jobs))

    async def close(self) -> None:
        for job in list(self._jobs):
            job.cancel()
        for job in list(self._jobs):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job

    # ------------------------------------------------------------------ the user's yes

    async def approve(self, name: str, *, version: int = 0, title: str = "") -> tuple[Skill | None, str]:
        """Save ``name`` for use (a proposal said yes to, an off one turned back on, or an older ``version`` brought
        back), after its checks pass. Returns the saved skill, or None and why not."""
        skill = await self.skills.get(name)
        if skill is None:
            return None, f"There's no skill “{name}”."
        if version:
            older = next((s for s in await self.skills.versions(skill.name) if s.version == version), None)
            if older is None:
                return None, f"There's no version {version} of “{skill.name}” any more (the last few are kept)."
            skill = replace(older, id=skill.id)
        elif skill.state == "approved" and not title:
            return None, f"The skill “{skill.name}” is already saved and on (v{skill.version})."
        candidate = replace(skill, state="approved", off_reason="", title=title.strip() or skill.title)
        problems = await self.problems(candidate)
        if problems:
            return None, f"The skill “{skill.name}” doesn't pass its checks: " + "; ".join(problems[:5]) + "."
        saved = await self.skills.save(candidate, self.session())
        return saved, ""


def _purpose(skill: Skill) -> str:
    text = skill.purpose or skill.title
    text = text[0].lower() + text[1:] if text[:1].isupper() and not text[1:2].isupper() else text
    return f"a reusable way to {text}" if not text.lower().startswith(("a ", "an ", "how ", "the ")) else text


def _offer_note(skill: Skill, why: str) -> str:
    steps = "; ".join(s.words() for s in skill.steps)[:400]
    return (
        f"Proposed skill {skill.name} (“{skill.title}”, {skill.kind}, {len(skill.steps)} steps: {steps}), {why}. "
        f"A yes: call save_skill with name={skill.name}. A no: forget_skill with name={skill.name}, or leave it; "
        f"proposals are dropped after {PROPOSAL_DAYS} days."
    )


class TaskSkills:
    """The hooks a task gets (``attach``, ``finished``), called from the playbooks' (``TaskPlaybooks``)."""

    def __init__(self, library: Library) -> None:
        self.library = library

    @property
    def enabled(self) -> bool:
        return self.library.enabled

    def attach(
        self, kind: str, task: Any, executor: Any, prompt: Prompt, *, place: Callable[[], str | None] = lambda: ""
    ) -> tuple[Any, Prompt]:
        """The task's executor and prompt factory with skills (unchanged while skills are off)."""
        if not self.enabled:
            return executor, prompt
        library = self.library
        record = library.record(kind, task.id)
        wrapped = SkillExecutor(executor, library, kind, task, place=place)
        wrapped.calls = record.calls  # every run of one task adds to the same record
        wrapped.proposed = record.proposed
        loaded: list[str] = []

        async def with_skills() -> str:
            base = await prompt()
            if not loaded:
                try:
                    loaded.append(await library.block(kind, task.instruction))
                except Exception:
                    log.warning("Couldn't list the skills for task “%s”", task.title, exc_info=True)
                    loaded.append("")
            return f"{base}\n\n{loaded[0]}" if loaded[0] else base

        return wrapped, with_skills

    def finished(self, kind: str, task: Any) -> None:
        self.library.spawn_finished(kind, task)
