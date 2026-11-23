"""Tools for the work she prepares overnight (``octavia/proactive``): what's waiting, the user's answer to each item,
and her picture of their week.

All SAFE. Answering changes only her own records; approving an item she only planned starts it as an objective (the
same as start_objective: read-only research, or code on a branch), and nothing here sends, submits or merges.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from octavia.proactive.service import ProactiveService
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_UNAVAILABLE = (
    "Overnight work isn't running (it starts with the assistant in voice mode, unless proactive.level is off)."
)


def _service(ctx: ToolContext) -> ProactiveService | None:
    service = ctx.extras.get("proactive")
    return service if isinstance(service, ProactiveService) else None


class WorkArgs(ToolArgs):
    report: bool = Field(
        default=False,
        description="True for the fuller record: the last two weeks, and how well you guess the user's answers.",
    )


class OvernightWork(Tool):
    name = "overnight_work"
    description = (
        "What you prepared on your own overnight and what's waiting for the user's answer (ready and checked, "
        "didn't pass its check, only planned, still running), with each item's number and where its result is. "
        "report=true adds the last two weeks and how often you guess their answers right."
    )
    Args = WorkArgs
    risk = RiskLevel.SAFE
    category = "proactive"

    async def run(self, args: WorkArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        service.sync()
        if args.report:
            return ToolResult.success(service.report())
        waiting = service.store.waiting() + [i for i in service.store.items if i.status == "running" and not i.decided]
        if not waiting:
            return ToolResult.success("Nothing from overnight is waiting for an answer. " + service.report(days=3))
        from octavia.proactive.handoff import note

        return ToolResult.success(note(waiting))


class DecideArgs(ToolArgs):
    item: str = Field(
        min_length=1,
        max_length=200,
        description='The item\'s number ("12" or "#12") or words of its title; "all" for every item waiting.',
    )
    decision: Literal["approve", "reject", "redirect"] = Field(
        description="approve: they want it (keep a ready one; start one you only planned). reject: they don't. "
        "redirect: they want it differently (say how in note)."
    )
    note: str = Field(default="", max_length=600, description="What they said with it, in their words.")
    redo: bool = Field(
        default=False, description="For a redirect: redo it their way now (only when they asked for that)."
    )


class DecideOvernightWork(Tool):
    name = "decide_overnight_work"
    description = (
        "Record the user's answer to something you prepared overnight: approve, reject, or redirect with how they "
        "want it instead. Their answers teach you what they'd pick next time. Call it for each item they answered."
    )
    Args = DecideArgs
    risk = RiskLevel.SAFE
    category = "proactive"

    async def run(self, args: DecideArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        if args.item.strip().casefold() == "all":
            items = service.answerable()
            if not items:
                return ToolResult.error("Nothing from overnight is waiting for an answer.")
        else:
            item = service.find(args.item)
            if item is None:
                return ToolResult.error(f"There's no overnight item “{args.item}”; overnight_work lists them.")
            items = [item]
        try:
            said = [await service.decide(item, args.decision, args.note, redo=args.redo) for item in items]
        except ValueError as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(" ".join(said))


class PictureArgs(ToolArgs):
    fresh: bool = Field(default=False, description="True to read Canvas, TickTick and the rest again now.")


class MyPictureOfYou(Tool):
    name = "my_picture_of_you"
    description = (
        "Your picture of the user's week that your overnight work plans from: deadlines, exams and how far along "
        "they are, their calendar and to-dos, their projects, what they've told you matters, their usual hours and "
        'how they answered your overnight work, each with where it came from. For "what do you know about my '
        'week?" or "why did you prepare that?".'
    )
    Args = PictureArgs
    risk = RiskLevel.SAFE
    category = "proactive"

    async def run(self, args: PictureArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.proactive import model as user_model

        if args.fresh:
            model = await service.build_model()
        else:
            model = user_model.load(service.folder / "model.json") or await service.build_model()
        return ToolResult.success(model.text())


TOOLS = [OvernightWork, DecideOvernightWork, MyPictureOfYou]
