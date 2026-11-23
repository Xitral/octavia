"""The rule engine: every published event is logged, matched against the rules the user turned on, and their
actions run.

Safeguards, all in code:

* chain depth: an event caused by a rule's action carries that rule in its ``chain`` (``current_chain``, a context
  variable set while the actions run, so whatever they publish is stamped); a chain is at most ``MAX_DEPTH`` rules;
* loop detection: a rule already in the chain never fires again in it;
* rate limits: each rule at most ``max_per_hour`` times an hour, and all rules together ``MAX_PER_MINUTE``;
* the kill switch (``set_killed``): while it's on, events are still logged but no rule runs.

Every firing and every refusal is written to the run log, with each action's result: an action that fails stops the
rest, and the rule's run is logged as failed (and published as ``rule.failed``, so a rule can tell the user).

Tool actions go through ``ActionRunner``, which in the app is the tool executor: a tool that asks for its calls asks
as usual, and an action ``rules.classify`` puts outside read-only and local changes is asked first even when its tool
wouldn't ask. Nothing runs in a dry run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol

from octavia.automation.events import KNOWN_TYPES, BusEvent
from octavia.automation.rules import (
    MAX_DEPTH,
    Rule,
    classify,
    describe_action,
    fill,
    forbidden,
    runs_unattended,
    type_matches,
    unmet,
)

if TYPE_CHECKING:
    from octavia.automation.store import AutomationStore
    from octavia.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

MAX_PER_MINUTE = 20  # rule firings, all rules together
PRUNE_EVERY_S = 3600.0
KILLED = "killed"

_CHAIN: ContextVar[tuple[int, ...]] = ContextVar("automation_chain", default=())


def current_chain() -> tuple[int, ...]:
    """The rules whose actions are running right now in this task (empty outside them)."""
    return _CHAIN.get()


class ActionRunner(Protocol):
    def tool(self, name: str) -> Tool | None: ...

    async def run_tool(self, name: str, args: dict[str, Any], *, ask: bool, why: str) -> ToolResult: ...

    async def say(self, text: str) -> bool: ...


class RuleEngine:
    def __init__(self, store: AutomationStore, runner: ActionRunner, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.runner = runner
        self.clock = clock
        self.killed = store.get_flag(KILLED)
        self._queue: asyncio.Queue[BusEvent] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._hourly: dict[int, deque[float]] = {}
        self._minute: deque[float] = deque()
        self._pruned = 0.0

    # --------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work(), name="automation-rules")

    async def close(self) -> None:
        for task in [*self._tasks, *([self._worker] if self._worker else [])]:
            task.cancel()
        for task in [*self._tasks, *([self._worker] if self._worker else [])]:
            try:
                await task
            except BaseException:  # noqa: BLE001 - shutting down, a cancelled or failed run doesn't matter now
                pass
        self._queue = None

    async def settle(self) -> None:
        """Wait until every published event is handled and every run it started is done (tests, shutdown)."""
        while self._queue is not None:
            await self._queue.join()
            if not self._tasks:
                if self._queue.empty():
                    return
                continue
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def publish(self, event: BusEvent) -> None:
        """Hand ``event`` over (from any code on the loop, sync handlers included). Before ``start`` it's dropped."""
        if self._queue is not None:
            self._queue.put_nowait(event)

    async def set_killed(self, killed: bool) -> None:
        self.killed = killed
        await asyncio.to_thread(self.store.set_flag, KILLED, killed)

    # ---------------------------------------------------------------------------------------------- handling
    async def _work(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                await self.handle(event)
            except Exception:
                log.exception("Automation failed to handle a %s event", event.type)
            finally:
                self._queue.task_done()

    async def handle(self, event: BusEvent) -> None:
        await asyncio.to_thread(self.store.append, event)
        now = self.clock()
        if now - self._pruned > PRUNE_EVERY_S:
            self._pruned = now
            await asyncio.to_thread(self.store.prune, now)
        if self.killed:
            return
        for rule in await asyncio.to_thread(self.store.rules):
            if not rule.enabled or unmet(rule, event, now) is not None:
                continue
            refusal = self._guard(rule, event.chain, now)
            if refusal is not None:
                await asyncio.to_thread(self.store.log_run, rule.id, event, refusal[0], refusal[1])
                continue
            run_id = await asyncio.to_thread(self.store.start_run, rule.id, event)
            task = asyncio.create_task(self._fire(rule, event, run_id), name=f"rule-{rule.id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _guard(self, rule: Rule, chain: tuple[int, ...], now: float) -> tuple[str, str] | None:
        """(status, why) when ``rule`` may not fire now, else None; a firing it allows is counted."""
        if rule.id in chain:
            return "loop", f"it already ran in this chain ({_chain_text(chain)})"
        if len(chain) >= MAX_DEPTH:
            return "depth_limit", f"the chain is already {MAX_DEPTH} rules deep ({_chain_text(chain)})"
        hourly = self._hourly.setdefault(rule.id, deque())
        while hourly and now - hourly[0] >= 3600:
            hourly.popleft()
        while self._minute and now - self._minute[0] >= 60:
            self._minute.popleft()
        if len(hourly) >= rule.max_per_hour:
            return "rate_limited", f"it already ran {rule.max_per_hour} times this hour"
        if len(self._minute) >= MAX_PER_MINUTE:
            return "rate_limited", f"rules already ran {MAX_PER_MINUTE} times this minute"
        hourly.append(now)
        self._minute.append(now)
        return None

    async def _fire(self, rule: Rule, event: BusEvent, run_id: int) -> None:
        chain = (*event.chain, rule.id)
        token = _CHAIN.set(chain)
        try:
            status, lines = await self._run_actions(rule, event, chain)
        except Exception as exc:
            log.exception("Rule %s failed", rule.id)
            status, lines = "failed", [f"error: {type(exc).__name__}: {exc}"]
        finally:
            _CHAIN.reset(token)
        detail = "; ".join(lines)
        await asyncio.to_thread(self.store.fired, rule.id, self.clock())
        await asyncio.to_thread(self.store.finish_run, run_id, status, detail)
        if status == "failed":
            self.publish(
                BusEvent(
                    type="rule.failed",
                    source="automation",
                    payload={"rule": rule.id, "name": rule.name, "error": detail[:200]},
                    provenance=f"rule:{rule.id}",
                    chain=chain,
                )
            )

    async def _run_actions(self, rule: Rule, event: BusEvent, chain: tuple[int, ...]) -> tuple[str, list[str]]:
        """Run ``rule``'s actions in order, checking each result. ("ok" | "failed", one line per action)."""
        lines: list[str] = []
        for action in rule.actions:
            if action.say:
                said = await self.runner.say(fill(action.say, event))
                lines.append(f"said it: {fill(action.say, event)[:80]}" if said else "couldn't say it")
                if not said:
                    return "failed", lines
            elif action.rule is not None:
                ok, line = await self._run_rule(action.rule, event, chain)
                lines.append(line)
                if not ok:
                    return "failed", lines
            else:
                ok, line = await self._run_tool(rule, action.tool, action.args)
                lines.append(line)
                if not ok:
                    return "failed", lines
        return "ok", lines

    async def _run_rule(self, rule_id: int, event: BusEvent, chain: tuple[int, ...]) -> tuple[bool, str]:
        target = await asyncio.to_thread(self.store.rule, rule_id)
        if target is None:
            return False, f"rule #{rule_id} no longer exists"
        if target.id in chain:
            await asyncio.to_thread(self.store.log_run, target.id, event, "loop", f"already in {_chain_text(chain)}")
            return False, f"rule #{rule_id}: loop stopped"
        if len(chain) >= MAX_DEPTH:
            await asyncio.to_thread(self.store.log_run, target.id, event, "depth_limit", _chain_text(chain))
            return False, f"rule #{rule_id}: too deep (more than {MAX_DEPTH} rules)"
        if not target.enabled:
            return True, f"rule #{rule_id} is paused, skipped"
        why = unmet(target, event, self.clock(), pattern=False)
        if why is not None:
            return True, f"rule #{rule_id} skipped: {why}"
        inner = (*chain, target.id)
        token = _CHAIN.set(inner)
        try:
            status, lines = await self._run_actions(target, event, inner)
        finally:
            _CHAIN.reset(token)
        await asyncio.to_thread(self.store.fired, target.id, self.clock())
        detail = f"run by rule #{chain[-1]}: " + "; ".join(lines)
        await asyncio.to_thread(self.store.log_run, target.id, event, status, detail)
        return status == "ok", f"rule #{rule_id}: {status}"

    async def _run_tool(self, rule: Rule, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        refused = forbidden(name)
        if refused:
            return False, f"{name}: refused, {refused}"
        tool = self.runner.tool(name)
        if tool is None:
            return False, f"{name}: no such tool any more"
        result = await self.runner.run_tool(name, args, ask=not runs_unattended(tool), why=rule.name)
        outcome = "ok" if result.ok else "failed"
        return result.ok, f"{name}: {outcome}, {' '.join(result.content.split())[:160]}"


# ------------------------------------------------------------------------------------------------ checking, dry run
def check_rule(rule: Rule, runner: ActionRunner, rules: Iterable[Rule]) -> tuple[list[str], list[str]]:
    """(problems that stop the rule being saved, warnings) for a new ``rule``."""
    problems: list[str] = []
    warnings: list[str] = []
    by_id = {r.id: r for r in rules}
    if not any(type_matches(rule.event, t) for t in KNOWN_TYPES):
        warnings.append(f"no watcher publishes '{rule.event}' yet, so it won't fire until one does")
    for action in rule.actions:
        if action.tool:
            refused = forbidden(action.tool)
            tool = runner.tool(action.tool)
            if refused:
                problems.append(f"{action.tool}: {refused}")
            elif tool is None:
                problems.append(f"there's no tool called {action.tool}")
            else:
                try:
                    tool.Args.model_validate(action.args)
                except Exception as exc:
                    problems.append(f"{action.tool}'s arguments: {' '.join(str(exc).split())[:200]}")
        elif action.rule is not None:
            if action.rule not in by_id:
                problems.append(f"there's no rule #{action.rule}")
            elif _depth(action.rule, by_id, set()) + 1 > MAX_DEPTH:
                problems.append(f"rule #{action.rule} chains too deep (at most {MAX_DEPTH} rules)")
    return problems, warnings


def _depth(rule_id: int, by_id: dict[int, Rule], seen: set[int]) -> int:
    """How many rules deep running ``rule_id`` goes through its rule actions (a cycle counts as too deep)."""
    if rule_id in seen:
        return MAX_DEPTH + 1
    rule = by_id.get(rule_id)
    if rule is None:
        return 1
    inner = [_depth(a.rule, by_id, seen | {rule_id}) for a in rule.actions if a.rule is not None]
    return 1 + max(inner, default=0)


def plan(rule: Rule, runner: ActionRunner) -> list[str]:
    """Each action with what it is and whether it would run unattended."""
    lines = []
    for action in rule.actions:
        if action.tool:
            tool = runner.tool(action.tool)
            if tool is None or forbidden(action.tool):
                lines.append(f"{describe_action(action)}: not allowed")
                continue
            how = "runs on its own" if runs_unattended(tool) else "asks you first"
            lines.append(f"{describe_action(action)}: {classify(tool).value}, {how}")
        elif action.rule is not None:
            lines.append(f"{describe_action(action)}: its own actions, each asked or not as that rule says")
        else:
            lines.append(f"{describe_action(action)}: read-only, runs on its own")
    return lines


def dry_run(rule: Rule, events: list[BusEvent], runner: ActionRunner, hours: float) -> str:
    """What ``rule`` would have done over ``events`` (the recent log, oldest first). Nothing is run."""
    of_type = [e for e in events if type_matches(rule.event, e.type)]
    fired: list[BusEvent] = []
    misses: dict[str, int] = {}
    limited = 0
    times: deque[float] = deque()
    for event in of_type:
        why = unmet(rule, event, event.ts)
        if why is not None:
            misses[why] = misses.get(why, 0) + 1
            continue
        while times and event.ts - times[0] >= 3600:
            times.popleft()
        if len(times) >= rule.max_per_hour:
            limited += 1
            continue
        times.append(event.ts)
        fired.append(event)
    span = f"the last {hours:g} hours"
    lines = [f"Dry run of '{rule.name}' over {span} (nothing was run): {len(of_type)} '{rule.event}' events."]
    if fired:
        lines.append(f"It would have fired {len(fired)} time{'s' if len(fired) != 1 else ''}:")
        for event in fired[-8:]:
            lines.append(f"- {time.strftime('%a %H:%M', time.localtime(event.ts))}: {_event_text(event)}")
    else:
        lines.append("It wouldn't have fired.")
    if misses:
        lines.append("Didn't match: " + ", ".join(f"{n} {why}" for why, n in misses.items()) + ".")
    if limited:
        lines.append(f"{limited} more held back by its limit of {rule.max_per_hour} an hour.")
    live_only = [c.field for c in rule.conditions if of_type and all(e.get(c.field) is None for e in of_type)]
    if live_only:
        lines.append(f"Not in the log ({', '.join(live_only)}): checked live only, so this replay can't tell.")
    lines.append("Each time: " + " | ".join(plan(rule, runner)))
    return "\n".join(lines)


def _event_text(event: BusEvent) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in list(event.payload.items())[:4])
    return f"{event.type} ({shown})" if shown else event.type


def _chain_text(chain: tuple[int, ...]) -> str:
    return " → ".join(f"#{i}" for i in chain) or "none"
