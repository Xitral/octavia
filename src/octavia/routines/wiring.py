"""How the app (and ``octavia routines``) put learning by watching together, and its one hook into the proactive gate."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from octavia.routines.service import PREFIX, RoutineLearner, running_processes
from octavia.routines.store import RoutineStore

if TYPE_CHECKING:
    from octavia.config import AppConfig


def create_learner(
    config: AppConfig,
    *,
    idle_s: Callable[[], float | None] = lambda: None,
    running: Callable[[], set[str] | None] = running_processes,
) -> RoutineLearner:
    settings = config.routines
    screen = config.screen_history
    return RoutineLearner(
        RoutineStore(config.resolve_path(settings.database)),
        settings,
        futures_db=config.resolve_path(config.futures.database),
        screen_db=config.resolve_path(screen.database) if screen.enabled else None,
        off_limits_config=config.futures.off_limits,
        exclude_apps=[*settings.exclude_apps, *screen.exclude_apps],
        exclude_sources=config.futures.exclude_sources,
        idle_s=idle_s,
        running=running,
    )


def connect_proactive(futures: Any, learner: RoutineLearner) -> bool:
    """Hand the gate an offer when a learned routine's moment comes (``FuturesService.add_source``): the only way
    learning by watching ever speaks up. False when this Futures has no such hook."""
    add_source = getattr(futures, "add_source", None)
    if add_source is None:
        return False
    from octavia.futures.engine import RemarkSource

    add_source(
        RemarkSource(
            prefix=PREFIX,
            candidates=learner.candidates,
            shared=learner.shared,
            declined=learner.declined,
            offer_tool=learner.offer_tool,
        )
    )
    return True
