"""A simulation on a panel: a picture with sliders under it, the user drags a slider and the picture, its caption and
its numbers change at once (A*'s heuristic weight, a game tree's move order...).

Every setting's picture is worked out here beforehand and sent with the panel, so a drag never waits on her: the
panel only picks the frame for where the sliders are (``avatar/renderer/panel/sim.js``). ``index`` maps each mix of
slider positions (the first slider changing slowest) to a frame, so settings that come out the same share one.

It isn't one of the blocks ``show_visual`` offers her (``spec.Block``): only code builds one (``octavia.study.sims``),
through ``SimVisual``, which allows it and a bigger panel than she may draw by hand.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from octavia.visuals.spec import (
    ChartBlock,
    CompareBlock,
    DiagramBlock,
    Fact,
    FactsBlock,
    StepsBlock,
    SvgBlock,
    TextBlock,
    Visual,
    _Part,
)
from octavia.visuals.svg_clean import DrawingError, clean_svg

MAX_PARAMS = 3
MAX_VALUES = 25
MAX_FRAMES = 200
MAX_SIM_WIRE_CHARS = 1_500_000

_VIEWBOX = re.compile(r'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"')


class SimParam(_Part):
    label: str = Field(min_length=1, max_length=40)
    values: list[Annotated[str, Field(min_length=1, max_length=24)]] = Field(min_length=2, max_length=MAX_VALUES)
    start: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _start_in_range(self) -> SimParam:
        if self.start >= len(self.values):
            raise ValueError(f"slider '{self.label}' starts at {self.start} but has {len(self.values)} values")
        return self


class SimFrame(_Part):
    svg: str = Field(min_length=1, max_length=60_000)
    caption: str = Field(default="", max_length=400, description="Markdown, one or two lines.")
    facts: list[Fact] = Field(default_factory=list, max_length=4)

    @field_validator("svg")
    @classmethod
    def _clean(cls, value: str) -> str:
        try:
            return clean_svg(value)
        except DrawingError as exc:
            raise ValueError(str(exc)) from None


class SimBlock(_Part):
    kind: Literal["sim"]
    params: list[SimParam] = Field(min_length=1, max_length=MAX_PARAMS)
    frames: list[SimFrame] = Field(min_length=1, max_length=MAX_FRAMES)
    index: list[int] = Field(min_length=1, description="The frame for each mix of positions, first slider slowest.")
    aspect: float = Field(default=0.6, gt=0.05, le=4.0, description="Height over width of the picture's box.")
    alt: str = Field(default="", max_length=200)
    caption: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _index_fits(self) -> SimBlock:
        mixes = math.prod(len(p.values) for p in self.params)
        if len(self.index) != mixes:
            raise ValueError(f"index has {len(self.index)} entries for {mixes} mixes of slider positions")
        if any(not 0 <= i < len(self.frames) for i in self.index):
            raise ValueError(f"index names a frame that isn't there (there are {len(self.frames)})")
        return self


def aspect_of(svgs: list[str]) -> float:
    """The tallest height-over-width among drawings, read from their viewBoxes, so every frame fits one box and the
    panel never changes size while a slider moves."""
    ratios = []
    for svg in svgs:
        found = _VIEWBOX.search(svg)
        if found and float(found.group(1)) > 0:
            ratios.append(float(found.group(2)) / float(found.group(1)))
    return min(4.0, max(ratios, default=0.6))


class SimVisual(Visual):
    """A ``Visual`` whose blocks may include a simulation."""

    blocks: list[
        Annotated[
            TextBlock | DiagramBlock | ChartBlock | FactsBlock | StepsBlock | CompareBlock | SvgBlock | SimBlock,
            Field(discriminator="kind"),
        ]
    ] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _not_huge(self) -> SimVisual:  # replaces Visual's: frames are many small drawings
        if len(self.model_dump_json(by_alias=True)) > MAX_SIM_WIRE_CHARS:
            raise ValueError(f"the simulation is too big (at most about {MAX_SIM_WIRE_CHARS // 1000}k characters)")
        return self
