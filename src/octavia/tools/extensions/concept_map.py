"""A course's concept map (``octavia.study.concept_map``): its topics from the study tracker, coloured by how well the
user knows each, with arrows for how they connect and weak spots in red, on a pop-up panel.

``concept_map`` shows it, and records links between topics as she works them out (what builds on what). It grows by
itself: a topic recorded with study_update, or a missed question about a new topic, is on the next map. It writes
only her own tracker and draws on the screen, so it never asks.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from octavia.study.concept_map import build_map, describe_map, map_scene, map_svg
from octavia.study.store import Course, add_link, match, remove_link
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.study import _failed, study_store
from octavia.visuals.host import PanelHost, VisualsError
from octavia.visuals.spec import Visual, _Part

PANEL = "concept-map"


class LinkArg(_Part):
    from_: str = Field(alias="from", min_length=1, max_length=80, description="The topic it builds on.")
    to: str = Field(min_length=1, max_length=80, description="The topic that builds on it.")
    why: str = Field(default="", max_length=80, description="In a few words: 'adds a heuristic'.")


class ConceptMapArgs(ToolArgs):
    course: str = Field(default="", max_length=100, description="Course name; omit for the one with the next exam.")
    links: list[LinkArg] = Field(default_factory=list, max_length=30, description="Links to add (or re-word).")
    unlink: list[LinkArg] = Field(default_factory=list, max_length=30, description="Links to take out.")
    show: bool = Field(default=True, description="Show the map on a panel (false: only record links).")


class ConceptMap(Tool):
    name = "concept_map"
    description = (
        "A course's concept map from the study tracker on a panel: its topics coloured by how well the user knows "
        "them (weak spots red), arrows for what builds on what. Show it when they ask for their map, what they're "
        "weak on at a glance, or how topics connect. Add links between its topics (from: the basis, to: what "
        "builds on it) when you know them; topics come from study_update and missed questions. It's flat: for a 3D "
        "map they can turn and explore, make a live block with oct.graph (show_visual)."
    )
    Args = ConceptMapArgs
    risk = RiskLevel.SAFE
    category = "school"
    timeout_s = 40.0

    async def run(self, args: ConceptMapArgs, ctx: ToolContext) -> ToolResult:
        store = study_store(ctx)
        course = self._course(store.courses(), args.course, store.clock())
        if isinstance(course, str):
            return ToolResult.error(course)
        said: list[str] = []
        if args.links or args.unlink:

            def apply(c: Course) -> list[str]:
                out = []
                for link in args.unlink:
                    if not remove_link(c, link.from_, link.to):
                        out.append(f"there was no link between {link.from_} and {link.to}")
                for link in args.links:
                    add_link(c, link.from_, link.to, link.why)
                return out

            try:
                said = store.change(course.name, apply, create=False)
            except ValueError as exc:
                return ToolResult.error(f"Nothing was changed: {exc}.")
            except OSError as exc:
                return _failed(exc)
            course = store.find(course.name) or course
        cmap = build_map(course)
        text = "\n".join(describe_map(cmap))
        if args.links or args.unlink:
            text = (
                f"Recorded {len(args.links)} link(s), took out {len(args.unlink) - len(said)}."
                + (f" ({'; '.join(said)}.)" if said else "")
                + "\n"
                + text
            )
        if not cmap.concepts:
            return ToolResult.success(
                text + "\nNo topics yet: record what its exams cover with study_update first, then the map has boxes.",
                data={"course": course.name, "topics": 0},
            )
        if args.show:
            text += await self._show(ctx, cmap)
        return ToolResult.success(text, data={"course": course.name, "topics": len(cmap.concepts)})

    @staticmethod
    def _course(courses: list[Course], wanted: str, now: float) -> Course | str:
        if not courses:
            return "Nothing is tracked yet. Record a course's next exam and its topics with study_update first."
        if wanted:
            found, ties = match(courses, wanted, Course.names)
            if found is not None:
                return found
            names = ", ".join(c.name for c in (ties or courses))
            return f"{wanted!r} isn't one tracked course (tracked: {names})."
        if len(courses) == 1:
            return courses[0]
        today = datetime.fromtimestamp(now).date()
        dated = [(c.next_exam(today, now), c) for c in courses]
        soonest = [(e.day, c) for e, c in dated if e is not None and e.day]
        if soonest:
            return min(soonest, key=lambda pair: pair[0])[1]
        return "Which course? " + ", ".join(c.name for c in courses) + "."

    @staticmethod
    async def _show(ctx: ToolContext, cmap) -> str:
        host = ctx.extras.get("visuals")
        if not isinstance(host, PanelHost) or not ctx.config.visuals.enabled:
            return "\nNo panel could show it (pop-up panels are off or not running here): say the weak spots instead."
        weak = cmap.weak_spots()
        extra: list[dict] = []
        if weak:
            extra.append(
                {
                    "kind": "text",
                    "markdown": "**Weak spots:** " + " · ".join(f"{c.name} ({c.why()})" for c in weak[:8])[:1500],
                }
            )
        title = f"{cmap.course}: concept map"[:80]
        try:
            # Made of elements (map_scene), so she can change any card or arrow, or put something among them, later.
            # A map too big for that (hundreds of topics) is drawn as one picture instead.
            try:
                visual = Visual.model_validate({"title": title, "blocks": [map_scene(cmap), *extra], "width": "wide"})
            except ValueError:
                picture = {"kind": "svg", "svg": map_svg(cmap), "alt": f"Concept map of {cmap.course}"}
                visual = Visual.model_validate({"title": title, "blocks": [picture, *extra], "width": "wide"})
            shown = await host.show(visual, panel_id=PANEL)
        except VisualsError as exc:
            return f"\nThe panel couldn't show it ({exc}): say the weak spots instead."
        except ValueError as exc:
            return f"\nThe map couldn't be drawn ({str(exc)[:200]}): say the weak spots instead."
        return f"\nIt's on the panel{' (' + '; '.join(shown.notes) + ')' if shown.notes else ''}."


TOOLS = [ConceptMap]
