"""A worksheet in one of her own pop-up panels: the problem drawn in her style with boxes placed on it to write in
(each node's value, α and β, each state's expansion number, the path) and branches to click to cross out. "Check my
work" is graded by code (the kind's ``mark``), never by the model: every box is coloured in place (green right, red
wrong with what belongs there beside it, orange left out), the node or step where it first went wrong is lit up on
the drawing, and she says the verdict.

It needs the panels' sheet block (``kind: "sheet"``: blanks placed on an SVG, one send for all of them, ``claim`` to
route that send here instead of making it a turn, and the ``mark`` edit). Until the panel app has it,
``supported`` says no and the worksheet opens as a page in the browser instead; the page also stays the way to print.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from octavia.study.practice import Marked, PracticeKind, Problem, Worksheet

log = logging.getLogger(__name__)

PANEL = "worksheet"
# (problem, kind, the marked sheet or None when it couldn't be graded, whether the marks made it onto the panel)
Verdict = Callable[[Problem, PracticeKind, "Marked | None", bool], None]


def supported(host: Any) -> bool:
    """Whether this panel app can show a worksheet: the sheet block, claiming a panel's sends, and marking them."""
    if not callable(getattr(host, "claim", None)):
        return False
    try:  # asked of the panel spec itself: a tiny sheet and a mark it accepts
        from octavia.visuals.spec import Edit, Visual

        blank = {"id": "v", "x": 5, "y": 5, "kind": "number", "label": "", "width": 20}
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="10" height="10"/></svg>'
        block = {"kind": "sheet", "svg": svg, "width": 100, "height": 100, "blanks": [blank], "submit": "Check"}
        Visual.model_validate({"title": "t", "blocks": [block]})
        Edit.model_validate({"op": "mark", "index": 0, "marks": {"v": "right"}, "tips": {}, "message": ""})
    except Exception:
        return False
    return True


def wire_id(problem: int, n: int) -> str:
    """A blank's id on the panel (the panel takes letters, digits, - and _ only; a kind's own ids say more, like
    "cut:#5" or "n:RIMNICU VILCEA"): the problem and the blank's place on its sheet. The problem is in it so a new
    worksheet never inherits what was typed on the last one (the panel keeps typed values while the ids match)."""
    return f"p{problem}_{n}"


# Room left for what a check adds (a tip on every box, the message, a lit-up redraw): a sheet bigger than this goes
# to the browser instead of failing to be marked later.
MAX_SHEET_CHARS = 100_000
MAX_SVG_CHARS = 100_000


def sheet_block(sheet: Worksheet, problem: int, submit: str = "Check my work") -> dict[str, Any]:
    """The worksheet as the panel's sheet block, its blanks named by problem and place (``wire_id``)."""
    return {
        "kind": "sheet",
        "svg": sheet.svg,
        "width": round(sheet.width, 1),
        "height": round(sheet.height, 1),
        "blanks": [
            {
                "id": wire_id(problem, n),
                "x": round(b.x, 1),
                "y": round(b.y, 1),
                "kind": b.kind,
                "label": b.label,
                "width": round(b.width, 1),
            }
            for n, b in enumerate(sheet.blanks)
        ],  # fmt: skip
        "submit": submit,
    }


def _tip(mark: str, want: str, kind: str) -> str:
    if mark in ("", "right"):
        return ""
    if kind == "cut":
        return "cross out" if want == "x" else "don't cross out"
    return "leave empty" if want == "" else want[:190]


def mark_edit(marked: Marked, ids: list[str], kinds: list[str], problem: int, index: int = 1) -> dict[str, Any]:
    """The panel edit that colours every box and shows what belongs in the ones that aren't right. Every box gets a
    mark and a tip, empty ones included, so nothing from an earlier check stays. ``ids`` and ``kinds``: the kind's
    blank ids and kinds, in sheet order."""
    marks, tips = {}, {}
    for n, own in enumerate(ids):
        mark = marked.marks.get(own, "")
        marks[wire_id(problem, n)] = mark
        tips[wire_id(problem, n)] = _tip(mark, marked.expected.get(own, ""), kinds[n])
    return {"op": "mark", "index": index, "marks": marks, "tips": tips,
            "message": "\n\n".join(marked.grade.lines)[:1990]}  # fmt: skip


class SheetPanel:
    """One worksheet panel at a time: shows it, grades what's sent from it, and marks it in place."""

    def __init__(self, host: Any, verdict: Verdict | None = None) -> None:
        self.host = host
        self.verdict = verdict
        self.problem: Problem | None = None
        self.kind: PracticeKind | None = None
        self.ids: list[str] = []  # the kind's blank ids, in sheet order (the panel names them by place)
        self.kinds: list[str] = []
        self._busy = asyncio.Lock()  # one open or check at a time
        self._generation = 0  # which worksheet is up: a check for an older one marks nothing

    async def open(self, problem: Problem, kind: PracticeKind) -> str:
        """Show ``problem`` as a worksheet panel; what to tell her ("" when it's up). Raises ValueError when the sheet
        is too big for a panel (the browser takes it)."""
        from octavia.visuals.spec import Visual

        sheet = kind.worksheet(problem.data, theme="panel")
        if sheet is None:
            return f" {kind.topic} has no worksheet."
        raw = {
            "title": kind.title(problem)[:80],
            "width": "wide",
            "blocks": [{"kind": "text", "markdown": sheet.instructions[:7900]}, sheet_block(sheet, problem.id)],
        }
        visual = Visual.model_validate(raw)
        if len(sheet.svg) > MAX_SVG_CHARS or len(json.dumps(raw)) > MAX_SHEET_CHARS:
            raise ValueError("the worksheet is too big for a panel")
        async with self._busy:
            await self.host.show(visual, panel_id=PANEL, stay_s=0)
            self._generation += 1
            self.problem, self.kind = problem, kind
            self.ids, self.kinds = [b.id for b in sheet.blanks], [b.kind for b in sheet.blanks]
            self.host.claim(PANEL, self.check)  # what's sent from it comes here, never as a turn; ends when it closes
        return ""

    async def check(self, values: dict[str, str]) -> Marked | None:
        """Their sheet, sent (the panel's values by place): graded by code and marked in place."""
        async with self._busy:  # one check at a time, and never across a new worksheet opening
            problem, kind, ids, kinds = self.problem, self.kind, self.ids, self.kinds
            if problem is None or kind is None:
                return None
            generation = self._generation
            try:
                prefix = f"p{problem.id}_"
                filled = {}
                for key, value in values.items():
                    place = key.removeprefix(prefix)
                    if key.startswith(prefix) and place.isdigit() and int(place) < len(ids):
                        filled[ids[int(place)]] = str(value)
                marked = kind.mark(problem.data, filled)
                lit = kind.worksheet(problem.data, theme="panel", highlight=marked.grade.first_wrong)
                edits = []
                if lit is not None and [b.id for b in lit.blanks] == ids:  # lit up where it went wrong, or unlit
                    edits.append(self._edit({"op": "replace_block", "index": 1, "block": sheet_block(lit, problem.id)}))
                mark = self._edit(mark_edit(marked, ids, kinds, problem.id))
            except Exception:
                log.warning("Couldn't grade the worksheet", exc_info=True)
                self._said(problem, kind, None, False)
                return None
            shown = False
            if generation == self._generation:
                for attempt in ([*edits, mark], [mark]):  # without the redraw, if that's what didn't fit
                    try:
                        await self.host.update(PANEL, attempt)
                    except Exception:
                        log.warning("Couldn't mark the worksheet panel", exc_info=True)
                        continue
                    shown = True
                    break
            self._said(problem, kind, marked, shown)
            return marked

    def _said(self, problem: Problem, kind: PracticeKind, marked: Marked | None, shown: bool) -> None:
        if self.verdict is None:
            return
        try:
            self.verdict(problem, kind, marked, shown)
        except Exception:
            log.warning("Couldn't pass on the worksheet's verdict", exc_info=True)

    @staticmethod
    def _edit(raw: dict[str, Any]) -> Any:
        from octavia.visuals.spec import Edit

        return Edit.model_validate(raw)
