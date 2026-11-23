"""Tools for her skills (``octavia.skills``): what she has, saving one on the user's yes, and forgetting one.

Listing them is safe. Saving one is asked (it lets her tasks run its steps in one call), and a plain yes to her own
offer of it, or the user's own words asking for it, is the yes (``tools.asked``). Forgetting a proposal is a "no" and
isn't asked; forgetting a saved skill deletes it and its versions for good, so it is. Running one, and proposing one,
are a task's own tools (``octavia.skills.tools``).
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import Field

from octavia.skills.wiring import Library
from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_UNAVAILABLE = "Skills aren't available right now (they start with the voice assistant, with long-term memory on)."
_STATUS = {
    "proposed": "proposed, waiting for your yes",
    "trial": "saved, on trial until its first clean run",
    "ready": "saved",
    "off": "turned off",
}


def _library(ctx: ToolContext) -> Library | None:
    library = ctx.extras.get("skills")
    return library if isinstance(library, Library) else None


def _name(text: str) -> str:
    return "_".join(text.casefold().replace("-", " ").split())


def _line(skill: Any) -> str:
    where = f", in {skill.place}" if skill.place else ""
    runs = f", {skill.runs} run{'s' if skill.runs != 1 else ''}" if skill.runs else ""
    return f"{skill.name}: “{skill.title}” ({skill.kind}{where}, {len(skill.steps)} steps, v{skill.version}, " \
        f"{_STATUS.get(skill.status, skill.status)}{runs})"  # fmt: skip


class SkillsArgs(ToolArgs):
    name: str = Field(default="", max_length=60, description="One skill to show in full. Empty: list them all.")


class SkillsTool(Tool):
    name = "skills"
    description = (
        "Your skills: step sequences your screen and web tasks worked out or kept repeating, saved so a task can run "
        "them in one call. Lists them, or shows one (its steps, inputs, versions, how it's been doing). To run one, "
        "start a screen or web task that names it."
    )
    Args = SkillsArgs
    risk = RiskLevel.SAFE
    category = "tasks"

    async def run(self, args: SkillsArgs, ctx: ToolContext) -> ToolResult:
        library = _library(ctx)
        if library is None:
            return ToolResult.error(_UNAVAILABLE)
        found = await library.skills.all()
        if not args.name:
            if not found:
                return ToolResult.success(
                    "No skills yet. Tasks propose them after working something out, and I offer one when I keep "
                    "doing the same steps by hand."
                )
            return ToolResult.success("Skills:\n" + "\n".join(_line(s) for s in found))
        skill = await library.skills.get(_name(args.name))
        if skill is None:
            close = [s.name for s in found if _name(args.name) in s.name or _name(args.name) in s.title.casefold()]
            hint = f" Did you mean {', '.join(close[:3])}?" if close else ""
            return ToolResult.error(f"There's no skill “{args.name}”.{hint}")
        lines = [_line(skill)]
        if skill.purpose:
            lines.append(f"Purpose: {skill.purpose}")
        if skill.inputs:
            lines.append("Inputs: " + "; ".join(f"{i.name} ({i.about})" if i.about else i.name for i in skill.inputs))
        lines += [f"{n}. {step.words()}" + (f" (expects “{step.expect}”)" if step.expect else "")
                  for n, step in enumerate(skill.steps, 1)]  # fmt: skip
        if skill.off_reason:
            lines.append(f"Turned off because {skill.off_reason}")
        if skill.last_error and skill.fails_in_row:
            lines.append(f"Failed {skill.fails_in_row} time(s) in a row; last: {skill.last_error}")
        older = [s for s in await library.skills.versions(skill.name) if s.version != skill.version]
        if older:
            lines.append("Older versions: " + ", ".join(
                f"v{s.version} ({s.state}, {time.strftime('%b %d', time.localtime(s.updated_at))})" for s in older
            ))  # fmt: skip
        return ToolResult.success("\n".join(lines), data={"name": skill.name, "version": skill.version})


class SaveSkillArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="The skill's name.")
    version: int = Field(default=0, ge=0, description="Bring back this older version instead. 0: the current one.")
    title: str = Field(default="", max_length=80, description="A new title for it, if the user gave one.")


class SaveSkill(Tool):
    name = "save_skill"
    description = (
        "Save a proposed skill so tasks can use it, turn a turned-off one back on, or bring back an older version. "
        "Only on the user's yes; its checks run first."
    )
    Args = SaveSkillArgs
    risk = RiskLevel.CONFIRM  # what makes its steps something a task runs in one call
    category = "tasks"

    async def describe_call(self, args: SaveSkillArgs, ctx: ToolContext) -> str:
        library = _library(ctx)
        skill = await library.skills.get(_name(args.name)) if library is not None else None
        if skill is None:
            return f"save the skill {args.name}"
        if args.version:
            return f"bring back version {args.version} of my skill “{skill.title}”"
        where = f" in {skill.place}" if skill.place else ""
        verb = "turn my skill" if skill.state == "off" else "save the skill"
        tail = " back on" if skill.state == "off" else ""
        return f"{verb} “{skill.title}”{tail} ({len(skill.steps)} steps{where})"

    async def precheck(self, args: SaveSkillArgs, ctx: ToolContext) -> str | None:
        library = _library(ctx)
        if library is None:
            return _UNAVAILABLE
        skill = await library.skills.get(_name(args.name))
        if skill is None:
            return f"There's no skill “{args.name}”; the skills tool lists them."
        if not args.version and skill.state == "approved" and not args.title:
            return f"The skill “{skill.name}” is already saved and on."
        if args.version:  # the older version is checked when it's brought back
            return None
        problems = await library.problems(skill)
        if problems:
            return f"The skill “{skill.name}” doesn't pass its checks: " + "; ".join(problems[:5]) + "."
        return None

    async def run(self, args: SaveSkillArgs, ctx: ToolContext) -> ToolResult:
        library = _library(ctx)
        if library is None:
            return ToolResult.error(_UNAVAILABLE)
        saved, why = await library.approve(_name(args.name), version=args.version, title=args.title)
        if saved is None:
            return ToolResult.error(why)
        trial = " It's on trial until its first clean run in a task." if saved.status == "trial" else ""
        return ToolResult.success(
            f"Saved the skill “{saved.title}” (v{saved.version}): screen and web tasks can use it now.{trial}",
            data={"name": saved.name, "version": saved.version},
        )


class ForgetSkillArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="The skill to delete (or the proposal to turn down).")


class ForgetSkill(Tool):
    name = "forget_skill"
    description = "Delete one of your skills and its versions, or turn down a proposed one."
    Args = ForgetSkillArgs
    risk = RiskLevel.SAFE  # turning down a proposal; deleting a saved skill is asked (``inspect_risk``)
    category = "tasks"

    async def inspect_risk(self, args: ForgetSkillArgs, ctx: ToolContext) -> RiskFinding | None:
        library = _library(ctx)
        skill = await library.skills.get(_name(args.name)) if library is not None else None
        if skill is None or skill.state == "proposed":
            return None
        return RiskFinding(RiskLevel.CONFIRM, scope="delete", reason="deletes it and its earlier versions for good")

    async def describe_call(self, args: ForgetSkillArgs, ctx: ToolContext) -> str:
        return f"delete my skill {args.name}"

    async def run(self, args: ForgetSkillArgs, ctx: ToolContext) -> ToolResult:
        library = _library(ctx)
        if library is None:
            return ToolResult.error(_UNAVAILABLE)
        gone = await library.skills.forget(_name(args.name))
        if gone is None:
            return ToolResult.error(f"There's no skill “{args.name}”; the skills tool lists them.")
        if gone.state == "proposed":
            return ToolResult.success(f"Dropped the proposed skill “{gone.title}”; I won't offer it again soon.")
        return ToolResult.success(f"Deleted the skill “{gone.title}” and its earlier versions.")


TOOLS = [SkillsTool, SaveSkill, ForgetSkill]
