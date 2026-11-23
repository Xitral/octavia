"""How the app starts the rule engine: its database, the tool executor as the actions' runner, and the publisher on
the assistant's bus. Rules only run in voice mode (``octavia chat`` beside it would run each one twice); the tools
read and change the same database either way."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from octavia.automation.engine import RuleEngine
from octavia.automation.publish import BUS_TYPES, Publisher, make_event
from octavia.automation.store import DB_FILE, AutomationStore
from octavia.tools.base import RiskLevel, Tool, ToolResult

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.core.events import EventBus
    from octavia.tools.base import ToolContext
    from octavia.tools.executor import ToolExecutor

log = logging.getLogger(__name__)

NOTE = "(One of the user's own rules said this, unprompted.)"


class ExecutorRunner:
    """Rule actions through the tool executor, the only code path that runs tools: the permission policy, the tool's
    own question and the audit log all apply. ``ask``: the action isn't read-only or a local change, so the user is
    asked first even when its tool wouldn't ask (a confirm-level tool asks in the executor, once)."""

    def __init__(self, executor: ToolExecutor, context: ToolContext) -> None:
        self.executor = executor
        self.context = context

    def tool(self, name: str) -> Tool | None:
        return self.executor.registry.get(name)

    async def run_tool(self, name: str, args: dict[str, Any], *, ask: bool, why: str) -> ToolResult:
        tool = self.tool(name)
        if tool is None:
            return ToolResult.error(f"Unknown tool '{name}'.")
        if ask and tool.risk is RiskLevel.SAFE and not await self._confirm(tool, args, why):
            return ToolResult.error("The user did not approve this action.")
        return await self.executor.execute(name, args)

    async def _confirm(self, tool: Tool, args: dict[str, Any], why: str) -> bool:
        from octavia.tools.permissions import ConfirmationRequest

        try:
            description = await tool.describe_call(tool.Args.model_validate(args), self.context)
        except Exception:
            description = f"run {tool.name}"
        request = ConfirmationRequest(
            tool_name=tool.name,
            description=f"{description}, for your rule '{why}'",
            risk=RiskLevel.CONFIRM,
            args=dict(args),
            scope=f"rule:{tool.name}",
            flagged=True,
        )
        try:
            return await asyncio.wait_for(self.executor.confirmer.confirm(request), self.executor.confirm_timeout_s)
        except Exception:
            log.info("A rule's question about %s got no yes", tool.name, exc_info=True)
            return False

    async def say(self, text: str) -> bool:
        announce = self.context.extras.get("announce")
        if not callable(announce):
            return False
        announce(text, NOTE, cue="reminder")
        return True


async def start_automation(
    config: AppConfig, events: EventBus, executor: ToolExecutor, context: ToolContext, *, voice: bool
) -> tuple[RuleEngine, list[Callable[[], Any]]]:
    """The engine (in ``context.extras["automation"]`` for the tools) and what closes it, in order."""
    store = await asyncio.to_thread(AutomationStore, config.resolve_path(DB_FILE))
    engine = RuleEngine(store, ExecutorRunner(executor, context))
    closers: list[Callable[[], Any]] = []
    if voice:
        engine.start()
        unsubscribe = events.subscribe(Publisher(engine).on_event, *BUS_TYPES)
        closers += [unsubscribe, engine.close]
    closers.append(store.close)
    context.extras["automation"] = engine
    return engine, closers


def phone_notification(engine: RuleEngine, text: str) -> None:
    """Phone notifications the phone alerts just said, published for the rules (their text is live only)."""
    engine.publish(make_event("phone.notification", "phone", sensitivity="personal", transient={"text": text}))
