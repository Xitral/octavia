"""The two skill tools a screen or web task has: ``use_skill`` and ``propose_skill``.

They're registered only in the tasks' own registries (``App._start_screen_tasks``, ``App._start_web_tasks``), never
in the conversation's, and answered by the task's ``runner.SkillExecutor``, which runs a skill's steps through the
task's own executor. Reached any other way, they refuse. The conversation's tools (list, save, forget) are in
``tools.extensions.skills``.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_ONLY_IN_TASKS = "Skills only run inside a screen or web task."


class UseSkillArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="The skill's name, as listed.")
    inputs: dict[str, str] = Field(default_factory=dict, description="A value for each of the skill's inputs.")


class UseSkill(Tool):
    name = "use_skill"
    description = (
        "Run one of your saved skills (listed in your instructions) in a single call: its steps run one by one with "
        "every usual check, and it stops at the first that fails and says which."
    )
    Args = UseSkillArgs
    risk = RiskLevel.SAFE  # each of its steps is checked as the task's own steps are
    category = "tasks"
    timeout_s = 600.0

    async def run(self, args: UseSkillArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.error(_ONLY_IN_TASKS)


class SkillInput(ToolArgs):
    name: str = Field(min_length=1, max_length=30, description="Lowercase letters, digits and _.")
    about: str = Field(default="", max_length=200, description="What it is, in a few words.")


class SkillStep(ToolArgs):
    tool: str = Field(default="", max_length=60, description="A tool you used in this task.")
    args: dict[str, Any] = Field(default_factory=dict, description="Its arguments; write what changes as {input}.")
    expect: str = Field(default="", max_length=100, description="Words its result must show for it to count.")
    skill: str = Field(default="", max_length=60, description="Or: another saved skill to run as this step.")
    inputs: dict[str, str] = Field(default_factory=dict, description="That skill's inputs (may use {input}).")


class ProposeSkillArgs(ToolArgs):
    name: str = Field(min_length=3, max_length=40, description="Short, lowercase letters, digits and _.")
    title: str = Field(min_length=1, max_length=80, description="What it does, in a few words.")
    purpose: str = Field(default="", max_length=300, description="When it helps.")
    place: str = Field(default="", max_length=80, description="The app or site it works in.")
    inputs: list[SkillInput] = Field(default_factory=list, max_length=6)
    steps: list[SkillStep] = Field(min_length=1, max_length=25)


class ProposeSkill(Tool):
    name = "propose_skill"
    description = (
        "Propose steps you took in this task and saw work as a reusable skill (a tricky page, an app's flow). It's "
        "kept only if the user says yes, and only if its steps match what you did."
    )
    Args = ProposeSkillArgs
    risk = RiskLevel.SAFE  # a proposal can't run until the user's yes (save_skill)
    category = "tasks"

    async def run(self, args: ProposeSkillArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.error(_ONLY_IN_TASKS)


TASK_TOOLS: list[type[Tool]] = [UseSkill, ProposeSkill]
