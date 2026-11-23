"""When-this-do-that rules (``octavia.automation``): create one, see it dry-run against the recent event log, turn it
on, pause, delete, its history, and the recent events themselves.

A new rule is saved paused, with its dry run: it only runs once the user turns it on (``pause_rule`` resume, asked
first), which is the authorization for its read-only and local actions to run unattended. The rest are asked when
they run, and sending or drafting email is refused outright. The kill switch (``pause_rule`` stop_all) stops every
rule at once.

Every tool reaches the engine through ``ctx.extras["automation"]``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from pydantic import Field

from octavia.automation.engine import RuleEngine, check_rule, dry_run, plan
from octavia.automation.events import KNOWN_TYPES
from octavia.automation.rules import MAX_ACTIONS, Action, Condition, Rule, Window, type_matches
from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_UNAVAILABLE = "The rule engine isn't running (its database couldn't open)."
_TYPES = ", ".join(KNOWN_TYPES)


def _engine(ctx: ToolContext) -> RuleEngine | None:
    engine = ctx.extras.get("automation")
    return engine if isinstance(engine, RuleEngine) else None


async def _rule(engine: RuleEngine, rule_id: int | None) -> Rule | None:
    return None if rule_id is None else await asyncio.to_thread(engine.store.rule, rule_id)


async def _replay(engine: RuleEngine, rule: Rule, hours: float) -> str:
    events = await asyncio.to_thread(engine.store.recent, since=time.time() - hours * 3600, limit=5000)
    return dry_run(rule, events, engine.runner, hours)


class CreateRuleArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="Short, e.g. 'lead won'.")
    event: str = Field(min_length=1, max_length=60, description=f"Event type or glob ('build.*'): {_TYPES}.")
    conditions: list[Condition] = Field(default_factory=list, max_length=6, description="All must hold.")
    window: Window = Field(default_factory=Window, description="When it applies; empty = always.")
    max_age_min: int | None = Field(default=None, ge=1, le=10080, description="Ignore data older than this.")
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS)
    max_per_hour: int = Field(default=6, ge=1, le=60)


class CreateRule(Tool):
    name = "create_rule"
    description = (
        "Make a when-this-do-that rule: when an event happens (with conditions), call tools, run another rule or "
        "say a line. It's saved paused with a dry run over the last day: show the user, and only on their yes turn "
        "it on with pause_rule resume. Just being told something when an event happens is remind_me_when."
    )
    Args = CreateRuleArgs
    risk = RiskLevel.SAFE  # saved paused: nothing runs until the user turns it on (asked)
    category = "rules"

    async def run(self, args: CreateRuleArgs, ctx: ToolContext) -> ToolResult:
        engine = _engine(ctx)
        if engine is None:
            return ToolResult.error(_UNAVAILABLE)
        draft = Rule(
            id=0,
            name=args.name,
            event=args.event,
            conditions=list(args.conditions),
            window=args.window,
            max_age_s=args.max_age_min * 60 if args.max_age_min else None,
            actions=list(args.actions),
            max_per_hour=args.max_per_hour,
        )
        problems, warnings = check_rule(draft, engine.runner, await asyncio.to_thread(engine.store.rules))
        if problems:
            return ToolResult.error("I can't make that rule: " + "; ".join(problems) + ".")
        rule = await asyncio.to_thread(engine.store.add_rule, draft)
        text = [f"Saved rule #{rule.id} '{rule.name}', paused: {rule.describe()}."]
        text += [f"Note: {w}." for w in warnings]
        text.append(await _replay(engine, rule, 24))
        text.append("Tell the user what it would do; only if they say yes, turn it on with pause_rule resume.")
        return ToolResult.success("\n".join(text), data={"id": rule.id})


class ListRules(Tool):
    name = "list_rules"
    description = "The user's when-this-do-that rules: on or paused, what each does, how often it ran."
    risk = RiskLevel.SAFE
    category = "rules"

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        engine = _engine(ctx)
        if engine is None:
            return ToolResult.error(_UNAVAILABLE)
        rules = await asyncio.to_thread(engine.store.rules)
        lines = ["All rules are stopped (the kill switch is on)."] if engine.killed else []
        for rule in rules:
            state = "on" if rule.enabled else "paused"
            ran = f", ran {rule.fire_count} time{'s' if rule.fire_count != 1 else ''}" if rule.fire_count else ""
            lines.append(f"#{rule.id} '{rule.name}' ({state}{ran}): {rule.describe()}")
        if not rules:
            lines.append("There are no rules yet.")
        reminders = await _reminders(ctx)
        if reminders:
            lines.append(
                f"Also {reminders} 'remind me when' reminder{'s' if reminders != 1 else ''} armed "
                "(futures_report reminders lists them)."
            )
        return ToolResult.success("\n".join(lines), data={"rules": len(rules)})


async def _reminders(ctx: ToolContext) -> int:
    service = ctx.extras.get("futures")
    if service is None:
        return 0
    try:
        return len(await service.call(service.triggers.armed))
    except Exception:
        return 0


class DryRunArgs(ToolArgs):
    id: int
    hours: float = Field(default=72, ge=1, le=336, description="How far back in the event log.")


class DryRunRule(Tool):
    name = "dry_run_rule"
    description = (
        "Replay a rule against the recent event log: what would have fired and what it would do. Runs nothing."
    )
    Args = DryRunArgs
    risk = RiskLevel.SAFE
    category = "rules"

    async def run(self, args: DryRunArgs, ctx: ToolContext) -> ToolResult:
        engine = _engine(ctx)
        if engine is None:
            return ToolResult.error(_UNAVAILABLE)
        rule = await _rule(engine, args.id)
        if rule is None:
            return ToolResult.error(f"There's no rule #{args.id}.")
        return ToolResult.success(await _replay(engine, rule, args.hours))


class PauseArgs(ToolArgs):
    action: Literal["pause", "resume", "stop_all", "allow_all"] = Field(
        description="resume: turn a rule on (only after the user said yes to it); stop_all/allow_all: kill switch."
    )
    id: int | None = Field(default=None, description="For pause and resume.")


class PauseRule(Tool):
    name = "pause_rule"
    description = "Pause a rule or turn it on, or stop every rule at once (the kill switch) and allow them again."
    Args = PauseArgs
    risk = RiskLevel.SAFE  # stopping never asks; turning on asks (assess_risk)
    category = "rules"

    def assess_risk(self, args: PauseArgs) -> RiskLevel:
        return RiskLevel.CONFIRM if args.action in ("resume", "allow_all") else RiskLevel.SAFE

    async def inspect_risk(self, args: PauseArgs, ctx: ToolContext) -> RiskFinding | None:
        if args.action not in ("resume", "allow_all"):
            return None
        return RiskFinding(risk=RiskLevel.CONFIRM, scope=f"{args.action}:{args.id}", reason="runs it on its own")

    async def describe_call(self, args: PauseArgs, ctx: ToolContext) -> str:
        if args.action == "allow_all":
            return "let your rules run again"
        engine = _engine(ctx)
        rule = await _rule(engine, args.id) if engine is not None else None
        if rule is None:
            return f"turn on rule #{args.id}"
        return f"turn on your rule '{rule.name}' ({rule.describe()})"

    async def run(self, args: PauseArgs, ctx: ToolContext) -> ToolResult:
        engine = _engine(ctx)
        if engine is None:
            return ToolResult.error(_UNAVAILABLE)
        if args.action in ("stop_all", "allow_all"):
            await engine.set_killed(args.action == "stop_all")
            return ToolResult.success(
                "Stopped every rule; they stay stopped until you allow them again."
                if engine.killed
                else "Rules can run again."
            )
        rule = await _rule(engine, args.id)
        if rule is None:
            return ToolResult.error(f"There's no rule #{args.id}." if args.id is not None else "Which rule?")
        updated = await asyncio.to_thread(engine.store.set_enabled, rule.id, args.action == "resume")
        assert updated is not None
        if args.action == "pause":
            return ToolResult.success(f"Paused rule #{rule.id} '{rule.name}'.")
        steps = " | ".join(plan(updated, engine.runner))
        stopped = " (All rules are stopped by the kill switch until you allow them again.)" if engine.killed else ""
        return ToolResult.success(f"Rule #{rule.id} '{rule.name}' is on. Each time: {steps}.{stopped}")


class DeleteArgs(ToolArgs):
    id: int


class DeleteRule(Tool):
    name = "delete_rule"
    description = "Delete one of the user's rules for good (pausing keeps it)."
    Args = DeleteArgs
    risk = RiskLevel.CONFIRM
    category = "rules"

    async def describe_call(self, args: DeleteArgs, ctx: ToolContext) -> str:
        engine = _engine(ctx)
        rule = await _rule(engine, args.id) if engine is not None else None
        return f"delete your rule '{rule.name}'" if rule is not None else f"delete rule #{args.id}"

    async def run(self, args: DeleteArgs, ctx: ToolContext) -> ToolResult:
        engine = _engine(ctx)
        if engine is None:
            return ToolResult.error(_UNAVAILABLE)
        users = [r.id for r in await asyncio.to_thread(engine.store.rules) if any(a.rule == args.id for a in r.actions)]
        if not await asyncio.to_thread(engine.store.delete_rule, args.id):
            return ToolResult.error(f"There's no rule #{args.id}.")
        note = f" Rule {', '.join(f'#{i}' for i in users)} ran it, and will now fail at that step." if users else ""
        return ToolResult.success(f"Deleted rule #{args.id}.{note}")


class HistoryArgs(ToolArgs):
    id: int | None = Field(default=None, description="Omit for every rule.")
    limit: int = Field(default=10, ge=1, le=50)


class RuleHistory(Tool):
    name = "rule_history"
    description = "When rules ran, on which event, and how each action went (also what was held back and why)."
    Args = HistoryArgs
    risk = RiskLevel.SAFE
    category = "rules"

    async def run(self, args: HistoryArgs, ctx: ToolContext) -> ToolResult:
        engine = _engine(ctx)
        if engine is None:
            return ToolResult.error(_UNAVAILABLE)
        runs = await asyncio.to_thread(engine.store.runs, args.id, args.limit)
        if not runs:
            return ToolResult.success("No runs yet.")
        lines = [
            f"{time.strftime('%a %d %b %H:%M', time.localtime(r['ts']))} rule #{r['rule_id']} on {r['event_type']}: "
            f"{r['status']}" + (f" ({r['detail']})" if r["detail"] else "")
            for r in runs
        ]
        return ToolResult.success("\n".join(lines))


class EventsArgs(ToolArgs):
    type: str = Field(default="*", max_length=60, description="Event type or glob.")
    hours: float = Field(default=24, ge=0.1, le=336)
    limit: int = Field(default=20, ge=1, le=100)


class RecentEvents(Tool):
    name = "recent_events"
    description = "The recent event log the rules see (mail arrived, apps opened, to-dos, timers, builds, leads)."
    Args = EventsArgs
    risk = RiskLevel.SAFE
    category = "rules"

    async def run(self, args: EventsArgs, ctx: ToolContext) -> ToolResult:
        engine = _engine(ctx)
        if engine is None:
            return ToolResult.error(_UNAVAILABLE)
        events = await asyncio.to_thread(engine.store.recent, since=time.time() - args.hours * 3600, limit=5000)
        shown = [e for e in events if type_matches(args.type, e.type)][-args.limit :]
        if not shown:
            return ToolResult.success(f"No '{args.type}' events in the last {args.hours:g} hours.")
        return ToolResult.success("\n".join(_event_line(e) for e in shown), data={"events": len(shown)})


def _event_line(event: Any) -> str:
    payload = ", ".join(f"{k}={v}" for k, v in event.payload.items() if v not in ("", None))
    origin = f", from {event.provenance}" if event.provenance != "watcher" else ""
    at = time.strftime("%a %H:%M", time.localtime(event.ts))
    return f"{at} {event.type} ({event.source}{origin}){': ' + payload if payload else ''}"


TOOLS = [CreateRule, ListRules, DryRunRule, PauseRule, DeleteRule, RuleHistory, RecentEvents]
