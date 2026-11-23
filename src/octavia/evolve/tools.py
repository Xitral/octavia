"""The brain's self-improvement tools: note, list, build, install, reject and undo; and the mistakes she caught.

They only talk to the ``ImprovementManager`` (``ctx.extras["evolve"]``), which enforces the rules. Noting,
listing and dropping are SAFE; anything that uses Claude usage, changes the code or restarts the assistant
is CONFIRM, so the user always hears what will happen and says yes first. Their names are reserved
(``policy.RESERVED_TOOL_NAMES``): a build that declares a tool with one of them is refused, so a build can't
shadow ``install_improvement`` with a version that doesn't ask.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, PrivateAttr, model_validator

from octavia.evolve.models import BUDGET_REFUSAL, OPEN_STATUSES, EvolveError, Improvement, Status
from octavia.tools.base import NoArgs, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

if TYPE_CHECKING:
    from octavia.evolve.manager import ImprovementManager

TURNED_OFF = "Self-improvement is turned off (self_improvement.enabled in config)."
_SHOWN_REQUEST = 80

_LISTS: dict[str, tuple[Status, ...] | None] = {"open": OPEN_STATUSES, "ready": (Status.READY,), "all": None}


def _manager(ctx: ToolContext) -> ImprovementManager | None:
    return ctx.extras.get("evolve")


def _short(text: str, limit: int = _SHOWN_REQUEST) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


async def _find(ctx: ToolContext, improvement_id: int) -> Improvement | None:
    manager = _manager(ctx)
    return await manager.get(improvement_id) if manager is not None else None


def _restarts(ctx: ToolContext) -> bool:
    """Whether installing or undoing restarts the assistant (only under the supervisor)."""
    manager = _manager(ctx)
    return manager is not None and manager.supervised


def _when_it_starts(manager: ImprovementManager, improvement_id: int) -> str:
    """When a build that was just queued starts: nothing starts before a pending restart, and only as many run at
    once as there are build slots (``parallel_builds``): with all of them taken, it starts as one frees up."""
    if manager.restart_pending:
        return "it's queued, and I'll start it right after I restart"
    ahead = manager.builds_ahead(improvement_id)
    if ahead == 0:
        return "I've started building it (a few minutes)"
    return f"it's queued, and {_starts_when(manager, ahead)}"


def _starts_when(manager: ImprovementManager, ahead: int) -> str:
    """When it starts, to follow "it": "starts when one of the builds running now finishes", for example."""
    from octavia.evolve.manager import starts_when  # imported late: this module stays light for the registry

    return starts_when(ahead, manager.parallel_builds)


def _spoken_list(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _label(item: Improvement) -> str:
    """How a confirmation question names it (read aloud): an error's technical title isn't."""
    from octavia.evolve.manager import spoken_label  # imported late: this module stays light for the registry

    return spoken_label(item)


class _ImprovementTool(Tool):
    """Runs ``act`` with the manager; a missing manager or an EvolveError becomes an error the model can say."""

    category = "self_improvement"

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        manager = _manager(ctx)
        if manager is None:
            return None  # ``run`` says it's off
        try:
            return await self.refusal(manager, args)
        except EvolveError as exc:
            return str(exc)

    async def refusal(self, manager: ImprovementManager, args: Any) -> str | None:
        """Why the manager would refuse this call right now, checked before the user is asked (None: go ahead)."""
        return None

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        manager = _manager(ctx)
        if manager is None:
            return ToolResult.error(TURNED_OFF)
        try:
            return await self.act(manager, args)
        except EvolveError as exc:
            return ToolResult.error(str(exc))

    @abstractmethod
    async def act(self, manager: ImprovementManager, args: Any) -> ToolResult: ...


class NoteArgs(ToolArgs):
    kind: Literal["bug", "missing", "feedback", "idea"] = Field(
        description="bug: you did something wrong. missing: you couldn't do what was asked. "
        "feedback: the user is unhappy with how you work. idea: an improvement you thought of."
    )
    title: str = Field(min_length=3, max_length=120, description="A short name, e.g. 'Can't read my calendar'.")
    details: str = Field(
        default="",
        max_length=2000,
        description="What happened and what would fix it: what the user asked or said, what you tried, what failed.",
    )


class NoteImprovement(_ImprovementTool):
    name = "note_improvement"
    description = (
        "Record something about yourself to improve later. Use it when you couldn't do what the user asked, got "
        "something wrong, or the user is unhappy with how you work. Call it once per problem. It's only recorded, "
        "unless automatic building is on: then it's built right away, and the result says so."
    )
    Args = NoteArgs
    risk = RiskLevel.SAFE

    async def act(self, manager: ImprovementManager, args: NoteArgs) -> ToolResult:
        item, building, blocked, came_back = await manager.note(args.kind, args.title, args.details, source="assistant")
        if came_back is not None:  # said first: the user heard it was fixed
            again = (
                f"This looks like the problem #{came_back.id} ({came_back.title}) was meant to fix, installed this "
                "week. If it is the same thing, that update didn't work: tell the user so plainly, and don't call it "
                "fixed until a new one is in. "
            )
        else:
            again = ""
        if building:  # automatic building queued it: offering to build it would be offering what's under way
            text = (
                f"Noted as #{item.id}. Automatic building is on, so {_when_it_starts(manager, item.id)}. Tell the "
                "user you're on it and will say when it's ready; don't offer to build it."
            )
        elif blocked and blocked.startswith(BUDGET_REFUSAL):  # her usage budget: one the user asks for still builds
            text = f"Noted as #{item.id}, but {blocked} You may offer to build it if the user wants it now."
        elif blocked:  # a build the user said yes to would be refused just the same
            text = f"Noted as #{item.id}, but it can't be built right now: {blocked} Don't offer to build it now."
        elif item.status in (Status.QUEUED, Status.BUILDING):  # a repeat doesn't change what's being built
            text = f"Already noted as #{item.id}, which is being built."
        elif item.status is Status.READY:
            text = f"Already noted as #{item.id}, which is built and ready to install."
        elif item.occurrences > 1:
            text = f"Already noted as #{item.id} (seen {item.occurrences} times)."
        else:
            text = f"Noted as #{item.id}."
        data: dict[str, int] = {"id": item.id}
        if came_back is not None:
            data["came_back"] = came_back.id
        return ToolResult.success(again + text, data=data)


class ListArgs(ToolArgs):
    status: Literal["open", "ready", "all"] = Field(
        default="open",
        description="open: noted, being built or ready to install. ready: built and waiting to be installed. "
        "all: also installed, failed and dropped ones.",
    )


class ListImprovements(_ImprovementTool):
    name = "list_improvements"
    description = (
        "List what you've noted about yourself to improve, what's being built (several can run at once) and what's "
        "ready to install. For each build in progress it says how it's going: how long so far, what it's doing now, "
        "its plan and latest steps; for a queued one, when it starts."
    )
    Args = ListArgs
    risk = RiskLevel.SAFE

    async def act(self, manager: ImprovementManager, args: ListArgs) -> ToolResult:
        return ToolResult.success(await manager.describe(_LISTS[args.status]))


class BuildArgs(ToolArgs):
    improvement_id: int | None = Field(
        default=None, ge=1, description="The number of an improvement that's already noted (see list_improvements)."
    )
    request: str = Field(
        default="",
        max_length=2000,
        description="What to build or fix, in plain words: the behaviour the user wants. Required without an id; "
        "with one, extra wishes the user added.",
    )
    title: str = Field(
        default="",
        max_length=120,
        description="A short name for the change, 2-6 words, e.g. 'Uptime tool'. Used for a new request.",
    )
    install_when_ready: bool = Field(
        default=False,
        description="Install it by itself once it's built and passes its checks (one restart), so the user needn't "
        "say yes again: when they said so ('build it and install it', 'just put it in when it's done').",
    )

    @model_validator(mode="after")
    def _needs_something(self) -> BuildArgs:
        if self.improvement_id is None and not self.request:
            raise ValueError("give improvement_id or request")
        return self


class BuildImprovement(_ImprovementTool):
    name = "build_improvement"
    description = (
        "Have a developer agent change your own code: fix a bug or add a capability. It works in the background on "
        "a copy (a few minutes), the result is tested automatically, and you'll be told when it's ready to install. "
        "Use it when the user asks you to fix, change or add something about yourself. It can't change how you "
        "build, test, install or undo updates, your permissions, settings or operating instructions: those are "
        "protected, so don't try; say the user can change them in the code themselves."
    )
    Args = BuildArgs
    risk = RiskLevel.CONFIRM

    async def refusal(self, manager: ImprovementManager, args: BuildArgs) -> str | None:
        return await manager.build_blocker(args.improvement_id)

    async def describe_call(self, args: BuildArgs, ctx: ToolContext) -> str:
        item = await _find(ctx, args.improvement_id) if args.improvement_id is not None else None
        if item is not None:
            what = _label(item)
        else:
            what = f'"{_short(args.title) or _short(args.request) or f"improvement {args.improvement_id}"}"'
        manager = _manager(ctx)
        wait = manager.builds_ahead_of_new() if manager is not None else 0
        if manager is not None and manager.restart_pending:
            when = "it starts once I've restarted for an update, then takes a few minutes"
        elif wait and manager.parallel_builds == 1:
            when = "it waits for the build in progress, then takes a few minutes"
        elif wait == 1:  # every build slot is taken
            when = "it waits for one of the builds in progress to finish, then takes a few minutes"
        elif wait:
            when = f"it waits for {wait} of the builds ahead of it to finish, then takes a few minutes"
        else:
            when = "it takes a few minutes"
        install = ""
        if args.install_when_ready:
            install = " and install it once it's ready" + (", with a short restart" if _restarts(ctx) else "")
        return f"build {what}{install} ({when} and uses some Claude usage)"

    async def act(self, manager: ImprovementManager, args: BuildArgs) -> ToolResult:
        item = await manager.request_build(
            args.improvement_id, args.request, title=args.title, install_when_ready=args.install_when_ready
        )
        ahead = manager.builds_ahead(item.id)
        if manager.restart_pending:  # nothing starts before the restart; the next start builds it
            text = f"Queued #{item.id}; I'll start it right after I restart. I'll say when it's ready."
        elif ahead == 0:
            text = f"Started building #{item.id}. I'll say when it's ready."
        else:
            text = f"Queued #{item.id}; it {_starts_when(manager, ahead)}. I'll say when it's ready."
        if args.install_when_ready:
            text += " Once it passes, I install it myself (unless something in it deserves a look first)."
        return ToolResult.success(text, data={"id": item.id})


class ImprovementIdArgs(ToolArgs):
    improvement_id: int = Field(ge=1, description="The improvement's number (see list_improvements).")


class InstallArgs(ToolArgs):
    improvement_ids: list[Annotated[int, Field(ge=1)]] = Field(
        default_factory=list,
        max_length=20,
        description="The numbers of the ready improvements to install, e.g. [3] or [3, 5] (see list_improvements). "
        "Several go in together, with one restart.",
    )
    all_ready: bool = Field(
        default=False, description="Install every improvement that's ready instead ('install them all')."
    )

    @model_validator(mode="after")
    def _needs_something(self) -> InstallArgs:
        if not self.improvement_ids and not self.all_ready:
            raise ValueError("give improvement_ids or all_ready")
        return self


class InstallImprovement(_ImprovementTool):
    name = "install_improvement"
    description = (
        "Install built improvements (ones that are ready) into your code: one, several or all of them, with one "
        "restart of a few seconds after which this conversation continues. Several at once, or one built on older "
        "code, are first tested together with your newest code (a few minutes, in the background: you're told how "
        "it went). Fine while builds are running: you restart once they're done. Only when the user wants it."
    )
    Args = InstallArgs
    risk = RiskLevel.CONFIRM
    timeout_s = 900.0  # waits for another install that's being tested; this one's testing runs in the background

    async def refusal(self, manager: ImprovementManager, args: InstallArgs) -> str | None:
        return await manager.install_blocker(args.improvement_ids, all_ready=args.all_ready)

    async def describe_call(self, args: InstallArgs, ctx: ToolContext) -> str:
        manager = _manager(ctx)
        ids = list(dict.fromkeys(args.improvement_ids))
        if manager is not None and args.all_ready:
            ids = await manager.ready_ids()
        items = [await _find(ctx, i) for i in ids]
        names = [_label(item) if item is not None else f"improvement {i}" for i, item in zip(ids, items, strict=True)]
        if not names:
            what = "every update that's ready"
        elif args.all_ready and len(names) > 1:
            what = f"all {len(names)} ready updates ({_spoken_list(names)})"
        else:
            what = _spoken_list(names)
        if manager is not None and await manager.needs_testing(ids):
            # the only thing the user hears before the tests run
            what += (
                " (I'll test " + ("it" if len(names) == 1 else "them") + " with my newest code first, a few minutes)"
            )
        restart = " and restart for a few seconds" if _restarts(ctx) else ""
        return f"install {what}{restart}"

    async def act(self, manager: ImprovementManager, args: InstallArgs) -> ToolResult:
        # Which updates this install was of (the ones ready now, for "all of them"): rebuilding one of those, when it
        # clashes with newer code, is part of the install the user approved (``asked.steps_after``).
        ids = list(dict.fromkeys(args.improvement_ids))
        if args.all_ready:
            ids = list(dict.fromkeys([*ids, *await manager.ready_ids()]))
        try:
            said = await manager.install(args.improvement_ids, all_ready=args.all_ready, wait=False)
        except EvolveError as exc:
            return ToolResult.error(str(exc), data={"improvement_ids": ids})
        return ToolResult.success(said, data={"improvement_ids": ids})


class RejectImprovement(_ImprovementTool):
    name = "reject_improvement"
    description = "Drop a noted or built improvement the user doesn't want (a running build is stopped)."
    Args = ImprovementIdArgs
    risk = RiskLevel.SAFE

    async def act(self, manager: ImprovementManager, args: ImprovementIdArgs) -> ToolResult:
        return ToolResult.success(await manager.reject(args.improvement_id))


class UndoArgs(NoArgs):
    # Which update "the last one" is when the user is asked (set before the question): the undo takes back that
    # one or nothing, even if another install finishes while they answer.
    _expected: int | None = PrivateAttr(default=None)
    _fixed: bool = PrivateAttr(default=False)


class UndoLastImprovement(_ImprovementTool):
    name = "undo_last_improvement"
    description = "Take back the most recently installed improvement (you restart for a few seconds)."
    Args = UndoArgs
    risk = RiskLevel.CONFIRM
    timeout_s = 120.0

    async def refusal(self, manager: ImprovementManager, args: UndoArgs) -> str | None:
        blocker = await manager.undo_blocker()
        if blocker is None:
            last = await manager.last_installed()
            args._expected, args._fixed = (last.id if last is not None else None), True
        return blocker

    async def describe_call(self, args: UndoArgs, ctx: ToolContext) -> str:
        manager = _manager(ctx)
        item = None
        if manager is not None:
            item = await manager.get(args._expected) if args._fixed else await manager.last_installed()
        what = f" {_label(item)}" if item is not None else ""
        restart = " and restart" if _restarts(ctx) else ""
        return f"undo the last update{what}{restart}"

    async def act(self, manager: ImprovementManager, args: UndoArgs) -> ToolResult:
        if args._fixed:
            return ToolResult.success(await manager.undo_last(args._expected))
        return ToolResult.success(await manager.undo_last())


class CaughtArgs(ToolArgs):
    action: Literal["list", "keep", "drop"] = Field(
        default="list",
        description="list: the mistakes you caught (numbered). keep: grade one in your real-request check for good "
        "(only when the user says to: one with a suggested rule is graded by that rule, so read it to them first). "
        "drop: stop grading one and don't catch it again (the user said it wasn't a mistake).",
    )
    number: int | None = Field(default=None, description="The mistake's number, for keep and drop.")
    expect: list[str] = Field(
        default_factory=list,
        description="keep: tools you should have called, when the user says (one of a|b counts). Exact tool names.",
    )
    forbid: list[str] = Field(default_factory=list, description="keep: tools you shouldn't have called.")


class CaughtMistakes(_ImprovementTool):
    name = "caught_mistakes"
    description = (
        "The mistakes you caught yourself: requests you got wrong that the user corrected, asked again or did by "
        "hand right after. Each is kept as a case of your real-request check, so you can't make it again without "
        "noticing. Use it for 'what mistakes have you caught?', and to keep or drop one when the user says so."
    )
    Args = CaughtArgs
    risk = RiskLevel.SAFE

    async def act(self, manager: ImprovementManager, args: CaughtArgs) -> ToolResult:
        from octavia.evolve import mistakes  # imported late: this module stays light for the registry

        pool = mistakes.Pool(mistakes.pool_path(manager.config.root))
        if args.action == "list":
            return ToolResult.success(await asyncio.to_thread(mistakes.spoken_list, pool))
        if args.number is None:
            return ToolResult.error("Say which mistake, by its number (list them first).")
        try:
            if args.action == "keep":
                case = await asyncio.to_thread(pool.keep, args.number, args.expect or None, args.forbid or None)
                rule = f' A good reply now has to: "{case.judge}".' if case.judge else ""
                return ToolResult.success(f"Kept: I'll grade #{case.n} for good.{rule} {mistakes.describe(case)}")
            case = await asyncio.to_thread(pool.drop, args.number, "the user said it wasn't a mistake")
            return ToolResult.success(f"Dropped #{case.n}: I won't grade it or catch it again.")
        except mistakes.MistakeError as exc:
            return ToolResult.error(str(exc))


TOOLS = [
    NoteImprovement,
    ListImprovements,
    BuildImprovement,
    InstallImprovement,
    RejectImprovement,
    UndoLastImprovement,
    CaughtMistakes,
]
