"""Simulations with sliders (``octavia.study.sims``), for any subject one registers: a panel with a picture and
sliders the user drags to see what a setting changes (A*'s heuristic weight, alpha-beta's move order...).

``simulate`` runs one on the practice problem the user is on when it fits (their own graph or tree), else on one made
for it, and shows it on a panel. Every frame is worked out before the panel opens, so dragging never waits on her;
she gets the whole sweep in words, since she can't see where the sliders are. It only draws on the screen, so it
never asks.
"""

from __future__ import annotations

import random

from pydantic import Field, ValidationError

from octavia.study.sims import Simulation, build, get_simulation, simulations
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.practice import problem_book
from octavia.visuals.host import PanelHost, VisualsError
from octavia.visuals.sim import SimVisual

PANEL = "simulation"


def _catalogue() -> str:
    return "; ".join(f"{name} ({sim.topic}): {sim.about}" for name, sim in sorted(simulations().items()))


class SimulateArgs(ToolArgs):
    kind: str = Field(min_length=1, max_length=40, description="The simulation; see the description.")
    problem: int = Field(
        default=0, ge=0, description="A practice problem to run it on; 0: the latest one if it fits, else a new one."
    )
    fresh: bool = Field(default=False, description="A new made-up problem even when a practice problem fits.")


class Simulate(Tool):
    name = "simulate"
    description = (
        "Show a simulation on a panel: a picture with sliders the user drags to watch what a setting changes "
        "(every position is worked out ahead, so it's instant). It runs on their latest practice_problem when that "
        "fits, else on one made for it. You get what each slider position gives, in words. Kinds: " + _catalogue() + "."
    )
    Args = SimulateArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 40.0

    async def run(self, args: SimulateArgs, ctx: ToolContext) -> ToolResult:
        sim = get_simulation(args.kind)
        if sim is None:
            return ToolResult.error(
                f"There's no {args.kind!r} simulation. The kinds: {', '.join(sorted(simulations()))}."
            )
        found = self._on_problem(sim, args, ctx)
        if isinstance(found, str):
            return ToolResult.error(found)
        data, source = found
        if data is None:
            data, source = sim.make(random.Random()), "a problem made for it"
        try:
            built = build(sim, data, caption="Drag the sliders.")
            visual = SimVisual.model_validate(
                {"title": sim.topic, "blocks": [built.block.model_dump(by_alias=True)], "width": "wide"}
            )
        except (ValueError, ValidationError) as exc:
            return ToolResult.error(f"The simulation couldn't be built ({str(exc)[:300]}).")
        shown = await self._show(ctx, visual)
        sliders = "; ".join(
            f"{p.label} ({', '.join(p.values)}; starts at {p.values[p.start]})" for p in built.block.params
        )
        lines = built.sweep[:40] + ([f"... and {len(built.sweep) - 40} more"] if len(built.sweep) > 40 else [])
        lesson = sim.lesson(data)
        text = (
            f"{sim.topic}, on {source}. Sliders: {sliders}.{shown}\n"
            "What each position gives:\n"
            + "\n".join(f"- {line}" for line in lines)
            + (f"\nPoint out: {lesson}" if lesson else "")
            + "\nYou can't see where they've put the sliders: ask what they notice, or tell them where to drag."
        )
        return ToolResult.success(text, data={"simulation": sim.name, "frames": built.frames})

    @staticmethod
    def _on_problem(sim: Simulation, args: SimulateArgs, ctx: ToolContext) -> tuple[dict | None, str] | str:
        """The practice problem's data when it fits (and where it came from), (None, "") for a made-up one, or what's
        wrong when the problem asked for doesn't fit."""
        if args.fresh:
            return None, ""
        problem = problem_book(ctx).get(args.problem)
        if problem is None:
            return (None, "") if not args.problem else f"There's no practice problem {args.problem}."
        data = sim.from_problem(problem.kind, problem.data) if problem.kind in sim.problem_kinds else None
        if data is None:
            if args.problem:
                return f"{sim.topic} can't run on problem {problem.id} (a {problem.kind} problem)."
            return None, ""
        return data, f"practice problem {problem.id}"

    @staticmethod
    async def _show(ctx: ToolContext, visual: SimVisual) -> str:
        host = ctx.extras.get("visuals")
        if not isinstance(host, PanelHost) or not ctx.config.visuals.enabled:
            return " No panel could show it (pop-up panels are off or not running here): describe the sweep instead."
        try:
            shown = await host.show(visual, panel_id=PANEL, stay_s=0)
        except VisualsError as exc:
            return f" The panel couldn't show it ({exc}): describe the sweep instead."
        return f" (The panel says: {'; '.join(shown.notes)}.)" if shown.notes else " It's on the panel."


TOOLS = [Simulate]
