"""Where the user lives: ``assistant.home`` (and its optional coordinates) in the settings.

The weather, directions and maps use it when no place is named, and their descriptions say it, so the model knows
what "here" means. Descriptions are written with ``{{home}}`` in them; ``render`` fills it in when the tools are
handed to a brain (``Tool.spec``). With no home set the tools say so and ask for a place, never guessing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNSET = "not set yet: ask the user where"


@dataclass(frozen=True)
class Home:
    name: str = ""
    latitude: float | None = None
    longitude: float | None = None

    @property
    def is_set(self) -> bool:
        return bool(self.name.strip())

    @property
    def city(self) -> str:
        """The first part of the name: "Portland" for "Portland, Oregon"."""
        return self.name.split(",")[0].strip()

    @property
    def located(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def area(self, half_width_deg: float = 0.4, half_height_deg: float = 0.35) -> str | None:
        """A lon/lat box around home that place lookups prefer (Nominatim's ``viewbox``), or None when the
        coordinates aren't known."""
        if not self.located:
            return None
        lat, lon = float(self.latitude), float(self.longitude)  # type: ignore[arg-type]
        west, east = lon - half_width_deg, lon + half_width_deg
        north, south = lat + half_height_deg, lat - half_height_deg
        return f"{west:g},{north:g},{east:g},{south:g}"


_current = Home()


def from_config(assistant: Any) -> Home:
    return Home(
        name=" ".join(str(getattr(assistant, "home", "") or "").split()),
        latitude=getattr(assistant, "home_latitude", None),
        longitude=getattr(assistant, "home_longitude", None),
    )


def configure(assistant: Any) -> None:
    """Called whenever the settings are (re)loaded."""
    global _current
    _current = from_config(assistant)


def current() -> Home:
    return _current


def render(text: str, home: Home | None = None) -> str:
    """``text`` with ``{{home}}`` replaced by the home's name (or a note that it isn't set)."""
    if "{{home" not in text:
        return text
    home = home or _current
    return text.replace("{{home}}", home.name if home.is_set else UNSET)
