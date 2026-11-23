"""Props she makes herself out of simple shapes (the make_avatar_prop tool), kept in ``MADE_PROPS_FILE``.

The app builds one from its ``parts`` the moment it's made (``AvatarBridge.define_prop``), and ``scan_props`` lists the
saved ones with the rest, so they're there after a restart too. A made prop is held like any other: same hands, same
pop, same grip (``avatar/renderer/props.js``, "made props"). Everything is checked here, before the app sees it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from octavia.avatar.bridge import Prop, prop_name
from octavia.config import AppConfig
from octavia.tools.base import ToolArgs

MADE_PROPS_FILE = "data/avatar/made_props.json"
MAX_MADE_PROPS = 100
MAX_PARTS = 24  # the app's limit (MAX_PARTS in props.js)
PART_MIN_M = 0.002  # the app's limits: thinner doesn't show,
PART_MAX_M = 1.0  # and no part bigger, or further from her fist, than this
ANGLE_MAX = 360.0

Shape = Literal["box", "sphere", "cylinder", "cone", "torus", "capsule", "plane"]
Side = Annotated[float, Field(ge=PART_MIN_M, le=PART_MAX_M)]
Place = Annotated[float, Field(ge=-PART_MAX_M, le=PART_MAX_M)]
Angle = Annotated[float, Field(ge=-ANGLE_MAX, le=ANGLE_MAX)]
_ZERO = (0.0, 0.0, 0.0)
_lock = threading.Lock()  # one read-modify-write of the file at a time


class PropPart(ToolArgs):
    shape: Shape = Field(description="box, sphere, cylinder, cone, torus, capsule or plane.")
    size: list[Side] = Field(
        min_length=3,
        max_length=3,
        description="[x, y, z]: its width, height and depth in metres. A cylinder, cone or capsule stands along y; a "
        "torus is a ring in the x-y plane, z its tube's thickness; a plane is flat in x-y (z is ignored).",
    )
    position: list[Place] = Field(
        default_factory=lambda: list(_ZERO), min_length=3, max_length=3, description="[x, y, z] of its centre, metres."
    )
    rotation: list[Angle] = Field(
        default_factory=lambda: list(_ZERO), min_length=3, max_length=3, description="[x, y, z] degrees (Euler XYZ)."
    )
    color: str = Field(default="#cccccc", pattern=r"^#[0-9a-fA-F]{6}$", description="'#RRGGBB'.")

    @field_validator("color")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower()


class MadeProp(ToolArgs):
    """A prop she made: its shapes, and how she holds it."""

    parts: list[PropPart] = Field(
        min_length=1,
        max_length=MAX_PARTS,
        description="The shapes it's made of, at real size, in the prop's own axes: +y is up, +z its front, and "
        "[0, 0, 0] is the spot inside her fist, so put the handle there (a sword's grip round the origin, its blade "
        "going up +y).",
    )
    hand: Literal["right", "left"] = Field(default="right", description="The hand she holds it in.")
    grip: list[Place] = Field(
        default_factory=lambda: list(_ZERO),
        min_length=3,
        max_length=3,
        description="[x, y, z] metres to slide it in her hand (her hand's axes). Usually leave it out.",
    )
    rotation: list[Angle] = Field(
        default_factory=lambda: list(_ZERO),
        min_length=3,
        max_length=3,
        description="[x, y, z] degrees to tilt it in her hand. Usually leave it out.",
    )

    def to_prop(self, name: str) -> Prop:
        """As ``scan_props`` lists it; the app keeps its own size."""
        x, y, z = self.grip
        rx, ry, rz = self.rotation
        return Prop(
            name,
            hand=self.hand,
            grip=(x, y, z),
            rotation=(rx, ry, rz),
            parts=tuple(part.model_dump() for part in self.parts),
        )


def made_props_path(config: AppConfig) -> Path:
    return config.resolve_path(MADE_PROPS_FILE)


def read_made(config: AppConfig) -> tuple[dict[str, MadeProp], list[str]]:
    """The props she made, by name, and what's wrong with the file (a broken entry is left out, not the rest)."""
    path = made_props_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, []
    except OSError as exc:
        return {}, [f"couldn't read the props she made from {path} ({exc.strerror or exc})"]
    except ValueError:
        return {}, [f"{path} isn't valid JSON, so the props she made are left out"]
    if not isinstance(data, dict):
        return {}, [f"{path} should hold an object of props, so the props she made are left out"]
    made: dict[str, MadeProp] = {}
    problems: list[str] = []
    for name, raw in data.items():
        if not isinstance(name, str) or not name or prop_name(name) != name:
            problems.append(f"the prop she made called {name!r} is left out: that isn't a prop name")
            continue
        try:
            made[name] = MadeProp.model_validate(raw)
        except ValidationError as exc:
            problems.append(f"the prop she made called {name!r} is left out: {exc.errors()[0]['msg']}")
    return made, problems


def write_made(config: AppConfig, made: dict[str, MadeProp]) -> None:
    path = made_props_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    payload = {name: made[name].model_dump() for name in sorted(made)}
    temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(temp, path)


def save_made(config: AppConfig, name: str, prop: MadeProp) -> bool:
    """Keeps ``prop`` as ``name``, replacing one of that name. False when there are already ``MAX_MADE_PROPS``."""
    with _lock:
        made, _ = read_made(config)
        if name not in made and len(made) >= MAX_MADE_PROPS:
            return False
        made[name] = prop
        write_made(config, made)
    return True


def delete_made(config: AppConfig, name: str) -> bool:
    """Throws away the prop she made called ``name``; False when there's none."""
    with _lock:
        made, _ = read_made(config)
        if made.pop(name, None) is None:
            return False
        write_made(config, made)
    return True


def made_props(config: AppConfig) -> tuple[dict[str, Prop], list[str]]:
    """The saved props as ``scan_props`` lists them, and what's wrong with any of them."""
    made, problems = read_made(config)
    return {name: prop.to_prop(name) for name, prop in made.items()}, problems
