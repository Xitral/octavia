"""The only code path that runs tools.

Brain providers call ``ToolExecutor.execute(name, raw_args)``; the executor
validates arguments, applies the permission policy, lets the tool look at the
world to raise a call's risk (``Tool.inspect_risk``: what's under the mouse)
and refuse a call that can't work right now (``Tool.precheck``), asks for
confirmation when required, enforces a timeout, reports events and writes an
audit log entry.

A call that needs the user's yes goes ahead without the question when:
* the policy says AUTO for it and the user is working with the assistant right
  now (``Confirmer.unattended``), and the tool's own look didn't flag it;
* the user's own words already asked for it (only the tools listed in
  ``octavia.tools.asked``: looking at the screen, installing her updates; see
  ``Confirmer.requested``);
* the user's plain yes answered her offer of exactly this call (only the tools
  ``octavia.tools.asked`` lists, see ``Confirmer.offered``);
* the user already said yes to the same kind of call in this task
  (``Confirmer.already_approved``).
Never for a restricted call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from octavia.core.events import (
    EventBus,
    PermissionRequested,
    PermissionResolved,
    ToolCallFinished,
    ToolCallStarted,
)
from octavia.tools.asked import matcher_for, offer_matcher_for
from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult, max_risk
from octavia.tools.permissions import (
    ConfirmationRequest,
    Confirmer,
    Decision,
    DenyAllConfirmer,
    NotAnswered,
    PermissionPolicy,
    PolicyDecision,
)
from octavia.tools.registry import ToolRegistry

log = logging.getLogger(__name__)
audit = logging.getLogger("octavia.audit")

# Why a call that needed confirmation ran without a question. Said in PermissionResolved and in the audit log.
ASKED_FOR = "the user asked for it"  # their own words asked for it (``Confirmer.requested``)
ON_ITS_OWN = "allowed while the user is working with it"  # AUTO (``Confirmer.unattended``)
ALREADY_APPROVED = "already approved in this task"  # ``Confirmer.already_approved``
OFFER_TAKEN = "the user said yes to her offer"  # ``Confirmer.offered``
NOT_ANSWERED = "not answered"  # nobody answered the question: silence, an unclear reply, no answer in time
# When a tool's look at the world fails, the call gets the user's yes rather than the benefit of the doubt.
_INSPECTION_FAILED = RiskFinding(RiskLevel.CONFIRM, reason="may not be safe: I couldn't check it first")


class _InvalidArgs(Exception):
    """A tool rejected a call's arguments while assessing its risk."""


@dataclass(frozen=True)
class Judgement:
    """What the policy and the tool's own looks make of one call (``ToolExecutor._judge``): its risk, the policy's
    verdict, the decision once the look is taken into account (DENY, ALLOW, AUTO or CONFIRM), what the look found
    (None when it found nothing, or wasn't taken because the tool is blocked) and whether it flagged the call."""

    risk: RiskLevel
    verdict: PolicyDecision
    decision: Decision
    finding: RiskFinding | None
    flagged: bool


@dataclass(frozen=True)
class CallPreview:
    """What would happen to a call right now (``ToolExecutor.preview``), for her to tell the user truthfully.

    ``judged`` is None when the call couldn't be judged (``error`` says why). ``rule`` is the per-tool rule and
    ``rule_from`` where it's from ("permissions.tools", "elsewhere" or ""); ``without_rule`` the call judged without
    the user's own line for the tool. ``words_can_approve``: the user's own plain words may stand in for the question
    (``octavia.tools.asked``). ``varies``: the tool judges each call by its arguments or by a look at the screen, so a
    tool-wide answer isn't the whole story."""

    tool: str
    judged: Judgement | None = None
    without_rule: Judgement | None = None
    rule: Decision | None = None
    rule_from: str = ""
    words_can_approve: bool = False
    varies: bool = False
    error: str = ""


def _varies(tool: Tool) -> bool:
    cls = type(tool)
    return cls.assess_risk is not Tool.assess_risk or cls.inspect_risk is not Tool.inspect_risk


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        context: ToolContext,
        events: EventBus,
        confirmer: Confirmer | None = None,
        confirm_timeout_s: float = 30.0,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.context = context
        self.events = events
        self.confirmer: Confirmer = confirmer or DenyAllConfirmer()
        self.confirm_timeout_s = confirm_timeout_s

    async def execute(self, name: str, raw_args: dict[str, Any] | None, call_id: str | None = None) -> ToolResult:
        call_id = call_id or uuid.uuid4().hex[:8]
        tool = self.registry.get(name)
        if tool is None:
            return self._audit(call_id, name, raw_args, "unknown", ToolResult.error(f"Unknown tool '{name}'."))
        # Validated from what was given; the log and the events only see the private arguments' lengths.
        given, raw_args = raw_args, _redacted(raw_args, tool.private_args)

        try:
            args = tool.Args.model_validate(given or {})
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}" for e in exc.errors())
            return self._audit(call_id, name, raw_args, "invalid", ToolResult.error(f"Invalid arguments: {problems}"))

        try:
            judged = await self._judge(tool, args, self.policy)
        except _InvalidArgs as exc:  # a tool may reject arguments while assessing them
            return self._audit(call_id, name, raw_args, "invalid", ToolResult.error(f"Invalid arguments: {exc}"))
        risk, verdict, finding, flagged, decision = (
            judged.risk,
            judged.verdict,
            judged.finding,
            judged.flagged,
            judged.decision,
        )
        if decision is Decision.DENY:
            return self._denied(call_id, name, raw_args, verdict.reason)

        # After the policy (a blocked tool runs none of its code), before the question: no "yes" that must fail.
        try:
            problem = await tool.precheck(args, self.context)
        except Exception:
            log.exception("precheck failed for %s", name)  # the tool checks again when it runs
            problem = None
        if problem:
            return self._audit(call_id, name, raw_args, "refused", ToolResult.error(problem))

        unasked: str | None = None  # why a call that needed a yes went ahead without the question
        if decision in (Decision.CONFIRM, Decision.AUTO):
            try:
                description = await tool.describe_call(args, self.context)
            except Exception:
                log.exception("describe_call failed for %s", name)
                description = f"run {name}"
            if flagged and finding is not None and finding.reason:
                description = f"{description}, which {finding.reason}"
            try:
                detail = tool.call_detail(args)
            except Exception:
                log.exception("call_detail failed for %s", name)
                detail = ""
            self.events.emit(PermissionRequested(tool=name, description=description, detail=detail))
            # Which words would ask for this call comes from the fixed list in octavia.tools.asked, never from the
            # tool itself: a tool (or a build that adds one) can't declare that it needn't ask.
            matcher = matcher_for(tool, args.model_dump(), finding.scope if flagged and finding is not None else None)
            scope = f"{name}:{finding.scope}" if flagged and finding is not None and finding.scope else name
            request = ConfirmationRequest(
                tool_name=name,
                description=description,
                risk=risk,
                args=_redacted(args.model_dump(), tool.private_args),
                requested_by=matcher,
                accepts_offer=offer_matcher_for(tool),
                scope=scope,
                place=finding.place if flagged and finding is not None else "",
                window=finding.window if flagged and finding is not None else "",
                # Asked about specifically: its own look flagged it, or the user's own rules say ask (a rule for the
                # tool, or asking about safe tools too), or its arguments made it riskier than its kind.
                flagged=flagged
                or (decision is Decision.CONFIRM and (verdict.per_tool or risk is not RiskLevel.CONFIRM)),
                detail=detail,
            )
            # A restricted call that the config lets through with a question always gets the question.
            if risk is not RiskLevel.RESTRICTED:
                if decision is Decision.AUTO and self._ask_confirmer("unattended", request):
                    unasked = ON_ITS_OWN
                elif matcher is not None and self._ask_confirmer("requested", request):
                    unasked = ASKED_FOR
                elif (request.accepts_offer is not None or matcher is not None) and self._ask_confirmer(
                    "offered", request
                ):
                    unasked = OFFER_TAKEN
                elif self._ask_confirmer("already_approved", request):
                    unasked = ALREADY_APPROVED
            unanswered: str | None = None  # why nobody answered: never a no (``NotAnswered``)
            if unasked is not None:
                approved, why = True, unasked
            else:
                try:
                    approved = await asyncio.wait_for(self.confirmer.confirm(request), self._confirm_limit(request))
                except TimeoutError:
                    approved, why, unanswered = False, NOT_ANSWERED, "no answer came in time"
                except NotAnswered as exc:
                    approved, why, unanswered = False, NOT_ANSWERED, exc.reason
                except Exception:
                    log.exception("Confirmer failed for %s", name)
                    approved, why = False, "confirmation failed"
                else:
                    why = "approved" if approved else "declined"
            self.events.emit(PermissionResolved(tool=name, approved=approved, reason=why))
            if unanswered is not None:
                return self._audit(
                    call_id,
                    name,
                    raw_args,
                    f"not confirmed ({why}: {unanswered})",
                    ToolResult.error(
                        f"Not done: the user hasn't answered the question about this ({unanswered}). That is NOT a "
                        "no: don't say they declined it. Ask them again in plain words, and do it once they say yes."
                    ),
                )
            if not approved:
                return self._audit(
                    call_id,
                    name,
                    raw_args,
                    f"not confirmed ({why})",
                    ToolResult.error(f"The user did not approve this action ({why}). Do not retry unless asked."),
                )

        self.events.emit(
            ToolCallStarted(call_id=call_id, name=name, args=_redacted(args.model_dump(), tool.private_args))
        )
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(tool.run(args, self.context), tool.timeout_s)
            if not isinstance(result, ToolResult):
                result = ToolResult.success(str(result))
        except TimeoutError:
            result = ToolResult.error(f"'{name}' timed out after {tool.timeout_s:.0f}s.")
        except asyncio.CancelledError:
            self.events.emit(
                ToolCallFinished(
                    call_id=call_id, name=name, ok=False, summary="cancelled", duration_s=time.perf_counter() - started
                )
            )
            raise
        except Exception as exc:
            log.exception("Tool %s failed", name)
            result = ToolResult.error(f"'{name}' failed: {type(exc).__name__}: {exc}")
        duration = time.perf_counter() - started
        self.events.emit(
            ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=result.ok,
                summary=_summarize(result.content),
                duration_s=duration,
                data=result.data,
            )
        )
        outcome = "ok" if result.ok else "error"
        return self._audit(call_id, name, raw_args, f"{outcome} ({unasked})" if unasked else outcome, result)

    async def _judge(self, tool: Tool, args: ToolArgs, policy: PermissionPolicy) -> Judgement:
        """What the permission policy and the tool's own looks make of this call, before any question: the one
        place it's worked out, for running a call (``execute``) and for telling about one (``preview``). Raises
        ``_InvalidArgs`` when the tool rejects the arguments while assessing them."""
        try:
            risk = max_risk(tool.risk, tool.assess_risk(args))
        except Exception as exc:
            raise _InvalidArgs(str(exc)) from exc
        verdict = policy.decide(tool, risk)
        if verdict.decision is Decision.DENY:
            return Judgement(risk, verdict, verdict.decision, None, False)

        # The tool's look at the world (what's under the mouse), after the policy: a blocked tool runs none of its
        # code. It can only raise the risk, and a call it raised is never waved through as AUTO.
        try:
            finding = await tool.inspect_risk(args, self.context)
        except Exception:
            log.exception("inspect_risk failed for %s", tool.name)
            finding = _INSPECTION_FAILED
        # Flagged: the look found something that needs the user's yes, even when the call's risk was already that
        # high (a tool that asks about changes may still spot that this one deletes).
        flagged = finding is not None and finding.risk is not RiskLevel.SAFE
        if flagged and (lent := self._as_its_like(finding, verdict, policy)) is not None:
            # Only another tool's own call along the way (drag's picture afterwards is a look_at_screen), which that
            # tool's rule lets go ahead: decided like that call, the stricter of the two rules, and not flagged.
            return Judgement(max_risk(risk, finding.risk), lent, lent.decision, finding, False)
        if flagged and max_risk(risk, finding.risk) is not risk:
            risk = max_risk(risk, finding.risk)
            verdict = policy.decide(tool, risk)
            if verdict.decision is Decision.DENY:
                return Judgement(risk, verdict, verdict.decision, finding, flagged)
        decision = verdict.decision
        if flagged and decision is Decision.ALLOW and verdict.per_tool:
            # A per-tool "allow" covers the tool's everyday calls; one its own look flags as risky is still asked.
            decision = Decision.CONFIRM
        if decision is Decision.AUTO and (flagged or risk is RiskLevel.RESTRICTED):
            decision = Decision.CONFIRM
        return Judgement(risk, verdict, decision, finding, flagged)

    def _as_its_like(
        self, finding: RiskFinding, own: PolicyDecision, policy: PermissionPolicy
    ) -> PolicyDecision | None:
        """How a finding that's only another tool's own call (``RiskFinding.like``) is decided: by the user's rule for
        that tool, when it goes ahead without a question (allow, or auto while they're working with her) and the
        calling tool's own rule isn't stricter. None when it's asked as before: that tool isn't there, its rule asks,
        or the finding is about something else (a restricted risk is never lent)."""
        if not finding.like or finding.risk is RiskLevel.RESTRICTED:
            return None
        other = self.registry.get(finding.like)
        if other is None:
            return None
        theirs = policy.decide(other, max_risk(other.risk, finding.risk))
        if theirs.decision not in (Decision.ALLOW, Decision.AUTO):
            return None
        order = (Decision.ALLOW, Decision.AUTO, Decision.CONFIRM, Decision.DENY)
        if order.index(own.decision) > order.index(theirs.decision):
            return own if own.decision is Decision.AUTO else None  # the calling tool's own rule is stricter
        return PolicyDecision(theirs.decision, f"{theirs.reason}, as {other.name}", per_tool=theirs.per_tool)

    async def preview(self, name: str, raw_args: dict[str, Any] | None = None) -> CallPreview:
        """What would happen to this call right now, without running it or asking anyone: the same judgement
        ``execute`` makes (``_judge``), so what she says about her own permissions is what they do. With no
        arguments, the tool's everyday call (its own risk level). ``without_rule`` is the same call judged without
        the user's own ``permissions.tools`` line for the tool, when there is one: what taking it out would change.
        """
        tool = self.registry.get(name)
        if tool is None:
            return CallPreview(name, error=f"I have no tool called {name}.")
        rule, rule_from = self.policy.rule_for(name)
        words = False
        if raw_args:
            try:
                args = tool.Args.model_validate(raw_args)
            except ValidationError as exc:
                problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}" for e in exc.errors())
                return CallPreview(name, rule=rule, rule_from=rule_from, error=f"Invalid arguments: {problems}")
            try:
                judged = await self._judge(tool, args, self.policy)
                bare = (
                    await self._judge(tool, args, self.policy.without_rule(name))
                    if rule_from == "permissions.tools"
                    else None
                )
            except _InvalidArgs as exc:
                return CallPreview(name, rule=rule, rule_from=rule_from, error=f"Invalid arguments: {exc}")
            flagged_scope = judged.finding.scope if judged.flagged and judged.finding is not None else None
            words = matcher_for(tool, args.model_dump(), flagged_scope) is not None
        else:
            # No call to judge: what the policy says for the tool's own risk level, with no look at the world.
            verdict = self.policy.decide(tool, tool.risk)
            judged = Judgement(tool.risk, verdict, verdict.decision, None, False)
            bare = None
            if rule_from == "permissions.tools":
                plain = self.policy.without_rule(name).decide(tool, tool.risk)
                bare = Judgement(tool.risk, plain, plain.decision, None, False)
        return CallPreview(
            name,
            judged=judged,
            without_rule=bare,
            rule=rule,
            rule_from=rule_from,
            words_can_approve=words,
            varies=_varies(tool),
        )

    def _ask_confirmer(self, check: str, request: ConfirmationRequest) -> bool:
        """One of the confirmer's "no question needed" checks (requested, unattended, already_approved). Anything
        short of a clear True asks the user."""
        try:
            return getattr(self.confirmer, check)(request) is True
        except Exception:
            log.exception("Confirmer check %s failed for %s", check, request.tool_name)
            return False

    def _confirm_limit(self, request: ConfirmationRequest) -> float:
        """How long the question may take: the confirmer's own limit when it asks somewhere slower (a text to the
        user's phone, ``Confirmer.timeout_s``), else ``confirm_timeout_s``."""
        try:
            limit = self.confirmer.timeout_s(request)
        except Exception:
            log.exception("Confirmer timeout check failed for %s", request.tool_name)
            limit = None
        return limit if isinstance(limit, int | float) and limit > 0 else self.confirm_timeout_s

    def _denied(self, call_id: str, name: str, raw_args: object, reason: str) -> ToolResult:
        return self._audit(
            call_id,
            name,
            raw_args,
            "denied",
            ToolResult.error(
                f"Not allowed: '{name}' is blocked by the permission policy ({reason}). "
                "Tell the user it is disabled; do not try to work around it."
            ),
        )

    def _audit(self, call_id: str, name: str, raw_args: Any, outcome: str, result: ToolResult) -> ToolResult:
        try:
            args_text = json.dumps(raw_args, default=str)[:500]
        except Exception:
            args_text = repr(raw_args)[:500]
        audit.info(
            "tool=%s call=%s outcome=%s args=%s result=%s",
            name,
            call_id,
            outcome,
            args_text,
            _summarize(result.content),
        )
        return result


def _redacted(args: Any, private: frozenset[str]) -> Any:
    """``args`` with the values of the ``private`` ones replaced by their length ("[12 characters]")."""
    if not private or not isinstance(args, dict):
        return args
    return {k: (f"[{len(str(v))} characters]" if k in private and v is not None else v) for k, v in args.items()}


def _summarize(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
