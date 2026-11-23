"""Reasoning without a fixed provider: Claude is one cognitive resource among many, and nothing in Futures needs it.

A ``ReasoningTask`` is a kind of question ("simulate": what happens if the user does A instead of B) with a payload
of plain data. A ``Reasoner`` says which kinds it can take and answers with a ``ReasoningResult``. The
``ReasonerRouter`` asks the capable ones in the order it was given them: when one raises or answers with an error,
the next is asked, and when none can, the answer says so rather than making something up.

Every attempt is measured (``selfeval.Performance``, kind "reasoner", category = the task's kind), so how well each
provider does at each kind of task is known from counts. The order is still the one the router was built with:
choosing by those counts is a decision for whoever builds the router, not a rule hidden in here.

``DeterministicReasoner`` is the one that always works: plain code (``simulate``), no model, no network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, ClassVar

from octavia.futures.selfeval import Performance
from octavia.futures.simulate import Scenario, describe, simulate

log = logging.getLogger(__name__)

NONE = "none"  # the provider of an answer nobody could give

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ReasoningTask:
    kind: str
    payload: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)


@dataclass(frozen=True)
class ReasoningResult:
    """An answer: ``data`` for code, ``text`` for the brain to put in its own words. ``error`` set means there is
    no answer (and ``data`` should not be relied on)."""

    provider: str
    kind: str
    data: Mapping[str, Any]
    text: str = ""
    latency_s: float = 0.0
    cost_usd: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Reasoner(ABC):
    """A provider of reasoning: plain code, a local model, Claude. ``kinds`` are the task kinds it takes."""

    name: ClassVar[str]
    kinds: ClassVar[frozenset[str]]

    def can(self, kind: str) -> bool:
        return kind in self.kinds

    @abstractmethod
    async def reason(self, task: ReasoningTask) -> ReasoningResult:
        """Answer ``task``; an error goes in the result's ``error`` (raising works too: the router moves on)."""

    async def close(self) -> None:
        """Let go of whatever it holds (a connection, a model); nothing by default."""


class DeterministicReasoner(Reasoner):
    """Plain code: "simulate" through ``simulate.simulate``."""

    name = "deterministic"
    kinds = frozenset({"simulate"})

    def __init__(self, *, local_time: Callable[[float], datetime] = datetime.fromtimestamp) -> None:
        self._local_time = local_time

    async def reason(self, task: ReasoningTask) -> ReasoningResult:
        if not self.can(task.kind):
            return ReasoningResult(self.name, task.kind, {}, error=f"{self.name} can't do {task.kind!r}")
        try:
            scenario = Scenario.from_dict(task.payload)
        except (TypeError, ValueError) as exc:
            return ReasoningResult(self.name, task.kind, {}, error=f"a scenario that can't be read: {exc}")
        branches = simulate(scenario, local_time=self._local_time)
        return ReasoningResult(
            self.name,
            task.kind,
            {"branches": [branch.to_dict() for branch in branches]},
            text=describe(branches),
        )


class ReasonerRouter:
    """Asks the reasoners that can take a task, in order, until one answers. ``timeout_s`` bounds each attempt,
    so a provider that hangs is treated like one that failed."""

    def __init__(
        self,
        reasoners: Sequence[Reasoner],
        *,
        performance: Performance | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float | None = None,
    ) -> None:
        self._reasoners = list(reasoners)
        self._performance = performance
        self._clock = clock
        self._timeout_s = timeout_s

    def providers(self) -> list[str]:
        return [reasoner.name for reasoner in self._reasoners]

    def kinds(self) -> frozenset[str]:
        """Every task kind some reasoner here takes."""
        return frozenset().union(*(reasoner.kinds for reasoner in self._reasoners))

    async def reason(self, task: ReasoningTask) -> ReasoningResult:
        """The first good answer from a capable reasoner; when there's none, a result with provider "none" and an
        error saying why."""
        capable = [reasoner for reasoner in self._reasoners if reasoner.can(task.kind)]
        if not capable:
            return ReasoningResult(NONE, task.kind, {}, error=f"nothing here can reason about {task.kind!r}")
        failures = []
        for reasoner in capable:
            started = self._clock()
            result: ReasoningResult | None = None
            failure: str | None = None
            error = ""
            try:
                answer = reasoner.reason(task)
                result = await (asyncio.wait_for(answer, self._timeout_s) if self._timeout_s is not None else answer)
            except TimeoutError:
                failure, error = "timed_out", "it took too long"
            except Exception as exc:
                log.warning("Reasoner %s failed on %s: %s", reasoner.name, task.kind, exc, exc_info=True)
                failure, error = "exception", f"{type(exc).__name__}: {exc}"
            else:
                if not isinstance(result, ReasoningResult):
                    failure, error, result = "error", "it gave no answer", None
                elif result.error:
                    failure, error = "error", result.error
            latency = max(0.0, self._clock() - started)
            self._record(reasoner, task, success=failure is None, failure=failure, latency=latency, result=result)
            if failure is None and result is not None:
                return result if result.latency_s else replace(result, latency_s=latency)
            log.info("Reasoner %s couldn't do %s (%s); trying the next", reasoner.name, task.kind, error)
            failures.append(f"{reasoner.name}: {error}")
        return ReasoningResult(NONE, task.kind, {}, error="no reasoner could do it (" + "; ".join(failures) + ")")

    def _record(
        self,
        reasoner: Reasoner,
        task: ReasoningTask,
        *,
        success: bool,
        failure: str | None,
        latency: float,
        result: ReasoningResult | None,
    ) -> None:
        if self._performance is None:
            return
        try:
            self._performance.record(
                "reasoner",
                reasoner.name,
                category=task.kind,
                success=success,
                failure=failure,
                latency_s=latency,
                provider=reasoner.name,
                cost_usd=result.cost_usd if result is not None else None,
            )
        except Exception:  # measuring must never cost the answer
            log.warning("Couldn't record how reasoner %s did", reasoner.name, exc_info=True)

    async def close(self) -> None:
        for reasoner in self._reasoners:
            try:
                await reasoner.close()
            except Exception:
                log.warning("Couldn't close reasoner %s", reasoner.name, exc_info=True)
