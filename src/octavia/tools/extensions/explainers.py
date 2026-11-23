"""Explainers: a pop-up panel that plays step by step as she explains it out loud (``octavia.visuals.explainer``).

``explain`` puts up the panel at its opening picture with its timeline of steps; her next sentences, each begun with
a beat (``{{step-id}}``), move it on as they're said. Its call is long (a whole scene and its steps), and the model
writes it before saying a word: 12 to 22 s of silence in her log, so its description has her say a short line first. ``explainer_control`` plays, pauses, steps or replays one (the
panel has the same buttons). Both SAFE: they only draw on the user's own screen, like show_visual.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.visuals import NOT_RUNNING, OFF, panel_on
from octavia.visuals.explainer import Director, Explainer
from octavia.visuals.host import VisualsError

NO_DIRECTOR = "Explainers aren't available in this session; use show_visual and update_visual instead."


def _director(ctx: ToolContext) -> Director | None:
    director = ctx.extras.get("explainers")
    return director if isinstance(director, Director) else None


class ExplainArgs(Explainer, ToolArgs):
    id: str | None = Field(
        default=None,
        max_length=64,
        description="A panel to play it on (replaces what's there); pass the id of one already up on this topic.",
    )
    stay_s: float | None = Field(default=None, ge=0, le=3600, description="Seconds on screen; 0 = until closed.")


class Explain(Tool):
    name = "explain"
    description = (
        "Explain something with a panel that changes as you talk: an animated, narrated walkthrough. Give the opening "
        "picture (usually a scene block of elements you place: cards, ellipses, text, emoji, paths, and lines or arrows "
        "with joins: [a, b] that stay attached to what they join), optional setup edits (arrange: row, column, grid, "
        "circle, or tree with parents and link to lay elements out for you), and steps: each step is a few of "
        "update_visual's edits (add, change to move/restyle/retext, remove, arrange, highlight...), what pulses, what "
        "your pointer points at, and a short caption. Moves ease, new things pop in, gone ones fade. Compose any "
        "explanation from these pieces: algorithms, processes, proofs, anatomy, history. Then narrate: begin the "
        "sentence for each step with its beat, {{step-id}}; as that sentence is said the panel plays that step. "
        "Beats are never said or shown. The user can pause, step and replay it. Writing the picture and steps takes "
        "you many seconds of silence, so say one short line out loud first (what you're about to show, e.g. "
        "“Let me draw that out.”), then call this in the same reply."
    )
    Args = ExplainArgs
    risk = RiskLevel.SAFE  # draws on their own screen only, like show_visual
    category = "general"
    timeout_s = 45.0

    async def describe_call(self, args: ExplainArgs, ctx: ToolContext) -> str:
        return f"show the explainer “{args.title}”"

    async def run(self, args: ExplainArgs, ctx: ToolContext) -> ToolResult:
        director = _director(ctx)
        if ctx.extras.get("visuals") is None:
            return ToolResult.error(NOT_RUNNING)
        if not ctx.config.visuals.enabled:
            return ToolResult.error(OFF)
        if director is None:
            return ToolResult.error(NO_DIRECTOR)
        explainer = Explainer.model_validate(args.model_dump(by_alias=True, exclude={"id", "stay_s"}))
        try:
            pid = args.id or panel_on(director.host, explainer.title)
            shown = await director.start(explainer, panel_id=pid, stay_s=args.stay_s)
        except VisualsError as exc:
            return ToolResult.error(f"{exc} Nothing was shown, so explain it out loud instead.")
        steps = ", ".join(f"{{{{{s.id}}}}}" + (f" ({s.caption})" if s.caption else "") for s in explainer.steps)
        content = (
            f"The explainer “{shown.title}” (id {shown.id}) is up at its opening picture. Steps in order: {steps}. "
            "Narrate it now: begin the sentence for each step with its beat, in order, one or two sentences per step "
            "(e.g. “{{" + explainer.steps[0].id + "}} First, ...”). Beats aren't said or shown. Don't read the "
            "captions out. A reply without beats moves on a step per sentence. If they're reading rather than "
            "listening, explainer_control play runs it without your voice."
        )
        if shown.replaced:
            content += " It plays on the panel that was already up on this topic, in place of what was there."
        if shown.notes:
            content += " The panel noted: " + "; ".join(shown.notes) + "."
        return ToolResult.success(content, data={"id": shown.id, "steps": [s.id for s in explainer.steps]})


class ExplainerControlArgs(ToolArgs):
    id: str = Field(min_length=1, max_length=64, description="The explainer's panel id.")
    action: Literal["play", "pause", "next", "back", "replay", "step"]
    step: str | None = Field(default=None, max_length=40, description="For step: a step id, or a number (0: opening).")


class ExplainerControl(Tool):
    name = "explainer_control"
    description = (
        'Play, pause, step forward or back, jump to a step, or replay an explainer from explain ("replay that", '
        '"go back a step", "pause it"). Replay and play say your lines for each step again as it plays.'
    )
    Args = ExplainerControlArgs
    risk = RiskLevel.SAFE
    category = "general"
    timeout_s = 10.0

    async def describe_call(self, args: ExplainerControlArgs, ctx: ToolContext) -> str:
        return f"{args.action} the explainer {args.id}"

    async def run(self, args: ExplainerControlArgs, ctx: ToolContext) -> ToolResult:
        director = _director(ctx)
        if director is None:
            return ToolResult.error(NO_DIRECTOR)
        try:
            said = await director.control(args.id, args.action, args.step)
        except KeyError:
            up = ", ".join(director.runs) or "none"
            return ToolResult.error(f"No explainer {args.id} is up (explainers up: {up}).")
        except ValueError as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(said, data={"id": args.id})


TOOLS = [Explain, ExplainerControl]
