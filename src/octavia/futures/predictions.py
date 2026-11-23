"""Predictions: what Octavia expects to happen, written down before it does and scored after.

A prediction is only as good as its basis, and the basis is always counts: "7 of 9 similar ones (due in the
evening) were finished by their due time". The probability is what those counts give under a Beta(1, 1) prior
(``Tally.estimate``): with no history it's a toss-up, and a handful of outcomes moves it less than their raw rate
would. No model is asked for a number. When too few past outcomes share the conditions she was asked about, she
falls back to the overall rate and says so, rather than treat two similar to-dos as a pattern.

Each prediction is scored when it resolves: ``correct`` is whether it came true on the side of 50 % she put it on,
and ``accuracy`` gives her Brier score and calibration (if the 60-80 % ones come true about 70 % of the time, her
numbers mean something). One nobody could resolve expires and counts neither for nor against her; one whose
question went away (the to-do was deleted) is cancelled.

A prediction is made once per ``key`` (``task-on-time:<task id>@<due>``, say), however many times the same thing is
seen: the caller decides what one question is.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from octavia.futures.hypotheses import METRICS, MetricInfo, describe_feature
from octavia.futures.outcomes import Outcomes
from octavia.futures.privacy import redact
from octavia.futures.schema import Knowledge
from octavia.futures.stats import Bucket, Tally, brier, calibration, percent
from octavia.futures.store import FuturesStore, dumps, loads

OPEN, RESOLVED, EXPIRED, CANCELLED = "open", "resolved", "expired", "cancelled"

# For an outcome kind the hypotheses don't describe: plain enough to say about anything.
_GENERIC = MetricInfo("past ones", "went well", "general", ())

NO_HISTORY = "no history yet, so a toss-up"


@dataclass(frozen=True)
class Prediction:
    """A guess about what will happen: ``probability`` that it does, from ``support`` of ``trials`` past outcomes
    (``basis`` says which, in words)."""

    id: int
    key: str
    kind: str
    subject: str | None
    statement: str
    probability: float
    basis: str
    support: int
    trials: int
    features: Mapping[str, str]
    hypothesis_id: int | None
    provider: str
    created_at: float
    resolve_by: float | None
    status: str  # "open", "resolved", "expired" or "cancelled"
    occurred: bool | None
    correct: bool | None
    resolved_at: float | None  # when it was settled, however it was
    resolution_event: str | None
    event_id: str | None

    @property
    def knowledge(self) -> Knowledge:
        return Knowledge.PREDICTION

    @property
    def chance(self) -> str:
        """ "about 70%": the probability as it may be said."""
        return percent(self.probability)


@dataclass(frozen=True)
class Accuracy:
    """How her resolved predictions went: ``correct`` of ``resolved``, the Brier score (0 is perfect, 0.25 is always
    saying 50 %), and calibration buckets. A toss-up (exactly 50 %: she had nothing to go on) made no call either way,
    so it isn't counted right or wrong; ``tossups`` says how many there were (the Brier score still has them)."""

    resolved: int
    correct: int
    brier: float | None
    buckets: list[Bucket] = field(default_factory=list)
    by_kind: dict[str, Tally] = field(default_factory=dict)
    tossups: int = 0

    def describe(self) -> str:
        """ "right 7 of 9 times", or that nothing has been scored yet."""
        if not self.resolved:
            return "no predictions resolved yet"
        return f"right {self.correct} of {self.resolved} times"


def _to_prediction(row: sqlite3.Row) -> Prediction:
    return Prediction(
        id=row["id"],
        key=row["key"],
        kind=row["kind"],
        subject=row["subject"],
        statement=row["statement"],
        probability=row["probability"],
        basis=row["basis"],
        support=row["support"],
        trials=row["trials"],
        features=loads(row["features"], {}),
        hypothesis_id=row["hypothesis_id"],
        provider=row["provider"],
        created_at=row["created_at"],
        resolve_by=row["resolve_by"],
        status=row["status"],
        occurred=None if row["occurred"] is None else bool(row["occurred"]),
        correct=None if row["correct"] is None else bool(row["correct"]),
        resolved_at=row["resolved_at"],
        resolution_event=row["resolution_event"],
        event_id=row["event_id"],
    )


def _clean(features: Mapping[str, Any] | None) -> dict[str, str]:
    """Features as outcomes keep them: strings, empty ones left out."""
    return {str(k): str(v) for k, v in (features or {}).items() if v is not None and v != ""}


class Predictions:
    """The predictions table."""

    def __init__(self, store: FuturesStore, outcomes: Outcomes) -> None:
        self.store = store
        self.outcomes = outcomes

    def base_rate(
        self,
        metric: str,
        features: Mapping[str, str] | None = None,
        *,
        min_similar: int = 5,
        noun: str = "similar ones",
    ) -> tuple[float, Tally, str]:
        """The probability of success for an outcome of ``metric`` like ``features``, the tally it comes from, and
        the basis in words. Past outcomes matching every feature are used when there are ``min_similar`` of them;
        otherwise all of them, and the basis says there were too few like it."""
        info = METRICS.get(metric, _GENERIC)
        wanted = _clean(features)
        similar = Tally()
        described = " and ".join(describe_feature(k, v, bracketed=True) for k, v in wanted.items())
        if wanted:
            similar = self.outcomes.tally(metric, where=wanted)
            if similar.trials and similar.trials >= min_similar:
                return similar.estimate(), similar, f"{similar.describe()} {noun} ({described}) {info.succeeded}"
        overall = self.outcomes.tally(metric)
        if not overall.trials:
            return overall.estimate(), overall, NO_HISTORY
        if not wanted:
            return overall.estimate(), overall, f"{overall.describe()} {info.noun} so far {info.succeeded}"
        few = (
            f"none {described} yet" if not similar.trials else f"only {similar.trials} {described}, too few to compare"
        )
        return overall.estimate(), overall, f"{overall.describe()} {info.noun} overall {info.succeeded} ({few})"

    def predict(
        self,
        kind: str,
        subject: str,
        *,
        key: str,
        statement: str,
        metric: str,
        resolve_by: float | None,
        features: Mapping[str, str] | None = None,
        hypothesis_id: int | None = None,
        provider: str = "deterministic",
        event_id: str | None = None,
        min_similar: int = 5,
        noun: str = "similar ones",
    ) -> Prediction | None:
        """Write down a prediction, its probability and basis from ``base_rate``. None when one with this ``key``
        was already made (whatever became of it): the same question is asked once."""
        wanted = _clean(features)
        with self.store.transaction():
            if self.by_key(key) is not None:
                return None
            probability, tally, basis = self.base_rate(metric, wanted, min_similar=min_similar, noun=noun)
            now = self.store.clock()
            try:
                prediction_id = self.store.insert(
                    "INSERT INTO predictions(key, kind, subject, statement, probability, basis, support, trials, "
                    "features, hypothesis_id, provider, created_at, resolve_by, status, event_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        kind,
                        subject,
                        redact(statement, 300),
                        probability,
                        basis,
                        tally.successes,
                        tally.trials,
                        dumps(wanted),
                        hypothesis_id,
                        provider,
                        now,
                        resolve_by,
                        OPEN,
                        event_id,
                    ),
                )
            except sqlite3.IntegrityError:
                return None
            self.store.record_change(
                f"prediction:{prediction_id}",
                "created",
                cause=event_id,
                provider=provider,
                detail={"probability": probability, "basis": basis},
            )
        return self.get(prediction_id)

    def resolve(self, key: str, *, occurred: bool, event_id: str | None = None) -> Prediction | None:
        """What happened. Only an open prediction is scored: ``correct`` when it came true on the side of 50 % she
        put it (a toss-up counts as expecting it to happen)."""
        with self.store.transaction():
            prediction = self.by_key(key)
            if prediction is None or prediction.status != OPEN:
                return None
            correct = (prediction.probability >= 0.5) == bool(occurred)
            self.store.execute(
                "UPDATE predictions SET status=?, occurred=?, correct=?, resolved_at=?, resolution_event=? WHERE id=?",
                (RESOLVED, int(bool(occurred)), int(correct), self.store.clock(), event_id, prediction.id),
            )
            self.store.record_change(
                f"prediction:{prediction.id}",
                "resolved",
                cause=event_id,
                detail={"occurred": bool(occurred), "correct": correct, "probability": prediction.probability},
            )
        return self.get(prediction.id)

    def cancel(self, key: str, *, reason: str = "") -> Prediction | None:
        """The question went away (the to-do was deleted): an open prediction is closed without a score."""
        with self.store.transaction():
            prediction = self.by_key(key)
            if prediction is None or prediction.status != OPEN:
                return None
            self._close(prediction, CANCELLED, self.store.clock(), {"reason": reason})
        return self.get(prediction.id)

    def expire(self, now: float, *, grace_s: float = 7 * 86400) -> list[Prediction]:
        """Open predictions ``grace_s`` past their ``resolve_by`` with nothing seen to settle them: expired, not
        scored. One without a ``resolve_by`` waits."""
        expired: list[Prediction] = []
        with self.store.transaction():
            rows = self.store.execute(
                "SELECT * FROM predictions WHERE status=? AND resolve_by IS NOT NULL AND resolve_by + ? < ? "
                "ORDER BY resolve_by, id",
                (OPEN, grace_s, now),
            )
            for row in rows:
                prediction = _to_prediction(row)
                self._close(prediction, EXPIRED, now, {"resolve_by": prediction.resolve_by})
                found = self.get(prediction.id)
                if found is not None:
                    expired.append(found)
        return expired

    def _close(self, prediction: Prediction, status: str, now: float, detail: dict[str, Any]) -> None:
        self.store.execute("UPDATE predictions SET status=?, resolved_at=? WHERE id=?", (status, now, prediction.id))
        self.store.record_change(f"prediction:{prediction.id}", status, detail=detail)

    # ------------------------------------------------------------------ reading
    def get(self, prediction_id: int) -> Prediction | None:
        row = self.store.one("SELECT * FROM predictions WHERE id=?", (prediction_id,))
        return _to_prediction(row) if row else None

    def by_key(self, key: str) -> Prediction | None:
        row = self.store.one("SELECT * FROM predictions WHERE key=?", (key,))
        return _to_prediction(row) if row else None

    def open(self, kind: str | None = None) -> list[Prediction]:
        """Open predictions, the soonest to resolve first (those without a ``resolve_by`` last)."""
        sql, params = "SELECT * FROM predictions WHERE status=?", [OPEN]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        rows = self.store.execute(sql + " ORDER BY resolve_by IS NULL, resolve_by, id", params)
        return [_to_prediction(r) for r in rows]

    def list(self, *, status: str | None = None, kind: str | None = None, limit: int = 50) -> list[Prediction]:
        """Predictions, newest first."""
        clauses, params = ["1=1"], []
        for column, wanted in (("status", status), ("kind", kind)):
            if wanted is not None:
                clauses.append(f"{column}=?")
                params.append(wanted)
        rows = self.store.execute(
            f"SELECT * FROM predictions WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        return [_to_prediction(r) for r in rows]

    def accuracy(self, *, kind: str | None = None, since: float | None = None) -> Accuracy:
        """How the resolved predictions went (of one kind, resolved since a time): expired and cancelled ones
        don't count."""
        sql, params = "SELECT * FROM predictions WHERE status=?", [RESOLVED]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        if since is not None:
            sql += " AND resolved_at>=?"
            params.append(since)
        settled = [_to_prediction(r) for r in self.store.execute(sql + " ORDER BY resolved_at, id", params)]
        pairs = [(p.probability, bool(p.occurred)) for p in settled]
        called = [p for p in settled if abs(p.probability - 0.5) > 1e-9]
        by_kind: dict[str, Tally] = {}
        for p in called:
            by_kind[p.kind] = by_kind.get(p.kind, Tally()) + Tally(int(bool(p.correct)), 1)
        return Accuracy(
            resolved=len(called),
            correct=sum(1 for p in called if p.correct),
            brier=brier(pairs),
            buckets=calibration(pairs),
            by_kind=by_kind,
            tossups=len(settled) - len(called),
        )
