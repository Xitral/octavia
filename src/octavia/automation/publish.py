"""Existing watchers publish here without changing: their typed events on the assistant's bus become ``BusEvent``s.

* the Gmail watcher's ``EmailArrived`` → ``gmail.received`` (the subject is live only, never logged);
* the app in front (``AppContextChanged``, Futures' context watch) → ``app.opened``;
* TickTick changes through her tools (``TaskChanged``) → ``ticktick.<action>``, and the day's last to-do ticked off
  (``TasksCleared``) → ``ticktick.all_done``;
* timers (``TimerChanged``) → ``timer.<action>``;
* self-improvement (``ImprovementChanged``) → ``build.started/finished/installed/rolled_back``;
* the lead tracker's calls (``add_lead``, ``update_lead_stage``, seen through their tool calls) → ``lead.*``.

A watcher with no bus event of its own calls ``make_event`` and ``RuleEngine.publish`` (phone notifications do).
Whatever is published while a rule's actions run carries that rule's chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from octavia.automation.engine import current_chain
from octavia.automation.events import BusEvent
from octavia.core.events import (
    AppContextChanged,
    EmailArrived,
    Event,
    ImprovementChanged,
    TaskChanged,
    TasksCleared,
    TimerChanged,
    ToolCallFinished,
    ToolCallStarted,
)

if TYPE_CHECKING:
    from octavia.automation.engine import RuleEngine

BUS_TYPES = (
    EmailArrived,
    AppContextChanged,
    TaskChanged,
    TasksCleared,
    TimerChanged,
    ImprovementChanged,
    ToolCallStarted,
    ToolCallFinished,
)

_BUILD_TYPES = {
    "building": "build.started",
    "ready": "build.finished",
    "failed": "build.finished",
    "no_change": "build.finished",
    "rejected": "build.finished",
    "installed": "build.installed",
    "rolled_back": "build.rolled_back",
}
_LEAD_TOOLS = {"add_lead": "lead.added", "update_lead_stage": "lead.stage_changed"}


def make_event(
    type: str,
    source: str,
    payload: dict[str, Any] | None = None,
    *,
    ts: float | None = None,
    sensitivity: str = "normal",
    transient: dict[str, Any] | None = None,
) -> BusEvent:
    """A new event, stamped with the chain of rules running right now (if any)."""
    chain = current_chain()
    extra = {"ts": ts} if ts is not None else {}
    return BusEvent(
        type=type,
        source=source,
        payload=payload or {},
        provenance=f"rule:{chain[-1]}" if chain else "watcher",
        sensitivity=sensitivity,
        transient=transient or {},
        chain=chain,
        **extra,
    )


class Publisher:
    """Subscribes to the assistant's bus (``BUS_TYPES``) and publishes what it sees to the engine."""

    def __init__(self, engine: RuleEngine) -> None:
        self.engine = engine
        self._calls: dict[str, dict[str, Any]] = {}  # call id -> arguments, for the lead tools' calls under way

    def on_event(self, event: Event) -> None:
        published = self.translate(event)
        if published is not None:
            self.engine.publish(published)

    def translate(self, event: Event) -> BusEvent | None:
        match event:
            case EmailArrived():
                return make_event(
                    "gmail.received",
                    "gmail",
                    {"sender": event.sender, "unread": event.unread},
                    ts=event.ts,
                    sensitivity="personal",
                    transient={"subject": event.subject},
                )
            case AppContextChanged():
                payload = {"app": event.app, "process": event.process, "previous": event.previous}
                return make_event("app.opened", "computer", payload, ts=event.ts)
            case TaskChanged():
                payload = {"title": event.title, "task_id": event.task_id, "due": event.due, "priority": event.priority}
                return make_event(f"ticktick.{event.action}", "ticktick", payload, ts=event.ts, sensitivity="personal")
            case TasksCleared():
                return make_event("ticktick.all_done", "ticktick", ts=event.ts)
            case TimerChanged():
                payload = {"label": event.label, "seconds": event.seconds, "id": event.timer_id}
                return make_event(f"timer.{event.action}", "timers", payload, ts=event.ts)
            case ImprovementChanged():
                kind = _BUILD_TYPES.get(event.status)
                if kind is None:
                    return None
                payload: dict[str, Any] = {"id": event.improvement_id, "title": event.title, "status": event.status}
                if kind == "build.finished":
                    payload["ok"] = event.status == "ready"
                return make_event(kind, "evolve", payload, ts=event.ts)
            case ToolCallStarted() if event.name in _LEAD_TOOLS:
                self._calls[event.call_id] = dict(event.args)
            case ToolCallFinished() if event.name in _LEAD_TOOLS:
                args = self._calls.pop(event.call_id, None)
                if args is None or not event.ok or "already" in event.summary:
                    return None
                if event.name == "add_lead":
                    payload = {"company": args.get("company", ""), "stage": args.get("stage", "new")}
                else:
                    payload = {"lead": args.get("lead", ""), "stage": args.get("stage", "")}
                return make_event(_LEAD_TOOLS[event.name], "leads", payload, ts=event.ts, sensitivity="personal")
        return None
