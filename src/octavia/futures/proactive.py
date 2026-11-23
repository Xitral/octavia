"""The proactive gate: whether something Octavia could say on her own is actually said, now, later or never.

Everything she might bring up unprompted (a heads-up before a to-do is due, a pattern worth mentioning, a "remind me
when" that went off) arrives here as a ``Candidate``. The gate is deliberately stingy, because an assistant that
talks too much gets switched off: each remark has a cooldown, a kind of remark has a shorter one, there is a budget
per hour and per day (and a smaller one for offers of help), nothing is said during quiet hours, focus mode or Do
Not Disturb, or while the user is busy, and the day's morning rundown comes first (what waited for it is said inside
it: ``release_for_morning``), and two turn-downs of a kind mute that kind for a week ("not now" is no
turn-down). Sensitive things (a grade, someone else's message) are never brought up unprompted.

The one exception to most of it is ``Policy.INFORM``: the user asked to be told ("remind me when..."), so it still
gets through with proactive remarks switched off and isn't judged on importance. It waits (``pending``) rather than
being dropped when the budget is spent, it's quiet hours or the user is busy, and ``due`` says it at the next good
moment.

Every decision is kept, with its reason, in the ``recommendations`` table and on the audit trail: "why didn't she
tell me?" has an answer too. The gate only decides; the caller says a delivered recommendation out loud.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from types import MappingProxyType
from typing import Any

from octavia.futures.privacy import Controls
from octavia.futures.schema import Policy, Sensitivity
from octavia.futures.store import FuturesStore, dumps, loads

log = logging.getLogger(__name__)

# "deferred": "not now" to an offer. The moment was wrong, not the idea: it counts toward no mute.
RESPONSES = frozenset({"accepted", "rejected", "ignored", "engaged", "followed", "deferred"})
OFFER_PREFIX = "offer_"  # kinds of remark that offer to do something ("Want me to pull up the rubric?")
RESPOND_WINDOW_S = 10 * 60  # "no thanks" with no id is about what she said in the last ten minutes
MUTE_AFTER_REJECTIONS = 2  # turn-downs of one kind...
REJECTION_WINDOW_S = 7 * 86400  # ...within a week...
MUTE_FOR_S = 7 * 86400  # ...mute that kind for a week

# A remark of her own waiting for the day's morning rundown, which says it along (``release_for_morning``).
HELD_FOR_MORNING = "waiting for the morning rundown"
MORNING_MARGIN_S = 15 * 60  # a remark that would run out of time before the rundown (plus this) doesn't wait for it
_LISTEN = "_listen"  # kept inside the stored trace: the table has no column for it
_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Candidate:
    """Something she might say on her own. ``key`` identifies the remark itself (the same heads-up about the same
    to-do has the same key), ``kind`` the sort of remark ("task_due", "pattern", "trigger"): cooldowns and mutes
    work on those. ``urgency`` and ``relevance`` are 0..1. ``note`` is for the brain, never said; ``trace`` is
    ``explain.Trace.to_dict()``. ``listen``: open the microphone after saying it (a question)."""

    kind: str
    key: str
    text: str
    policy: Policy
    urgency: float
    relevance: float
    domain: str
    sensitivity: str = "personal"
    subject: str | None = None
    note: str | None = None
    trace: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    expires_at: float | None = None
    listen: bool = False
    experiment_id: int | None = None
    variant: str | None = None


@dataclass(frozen=True)
class Recommendation:
    """A candidate as the gate decided on it. ``status``: "delivered" (the caller says it), "pending" (waiting
    for a better moment), "suppressed" (never said; ``reason`` says why) or "expired"."""

    id: int
    key: str
    kind: str
    text: str
    policy: Policy
    urgency: float
    relevance: float
    domain: str
    sensitivity: str
    status: str
    reason: str
    trace: Mapping[str, Any]
    note: str | None
    subject: str | None
    created_at: float
    expires_at: float | None
    delivered_at: float | None
    response: str | None
    response_at: float | None
    experiment_id: int | None
    variant: str | None
    listen: bool = False


@dataclass(frozen=True)
class GateSettings:
    max_per_hour: int = 2
    max_per_day: int = 6  # since local midnight
    cooldown_s: float = 12 * 3600  # the same remark again
    kind_cooldown_s: float = 20 * 60  # another remark of the same kind
    rejected_cooldown_s: float = 3 * 86400  # a remark the user turned down
    threshold: float = 0.35  # urgency * relevance below this isn't worth interrupting for
    quiet_hours: str = "22:30-08:00"  # local "HH:MM-HH:MM", may wrap midnight; "" for none
    pending_max_s: float = 24 * 3600  # a held-back remark with no expiry of its own is dropped after this
    offers_per_day: int = 3  # offers of help (kinds starting "offer_") since local midnight, within max_per_day


def parse_quiet_hours(text: str) -> tuple[time, time] | None:
    """``"22:30-08:00"`` as its start and end; None for "" (no quiet hours) or a window that starts where it ends.
    A ValueError for anything else."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        start_text, end_text = (part.strip() for part in text.split("-"))
        start, end = time.fromisoformat(start_text), time.fromisoformat(end_text)
    except ValueError:
        raise ValueError(f"quiet hours look like '22:30-08:00', not {text!r}") from None
    return None if start == end else (start, end)


def in_window(moment: time, window: tuple[time, time] | None) -> bool:
    """Whether a local time of day falls in ``[start, end)``, a window that may wrap midnight."""
    if window is None:
        return False
    start, end = window
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def _to_recommendation(row: sqlite3.Row) -> Recommendation:
    trace = loads(row["trace"], {})
    trace = dict(trace) if isinstance(trace, dict) else {}
    listen = bool(trace.pop(_LISTEN, False))
    return Recommendation(
        id=row["id"],
        key=row["key"],
        kind=row["kind"],
        text=row["text"],
        policy=Policy(row["policy"]),
        urgency=row["urgency"],
        relevance=row["relevance"],
        domain=row["domain"],
        sensitivity=row["sensitivity"],
        status=row["status"],
        reason=row["reason"],
        trace=MappingProxyType(trace),
        note=row["note"],
        subject=row["subject"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        delivered_at=row["delivered_at"],
        response=row["response"],
        response_at=row["response_at"],
        experiment_id=row["experiment_id"],
        variant=row["variant"],
        listen=listen,
    )


def _value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _trace_of(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):  # an explain.Trace passed as is
        value = value.to_dict()
    return dict(value) if isinstance(value, Mapping) else {}


class ProactiveGate:
    """Decides on candidates and keeps the decisions. ``local_time`` turns epoch seconds into the user's local
    time (quiet hours, "since midnight")."""

    def __init__(
        self,
        store: FuturesStore,
        controls: Controls,
        settings: GateSettings,
        *,
        local_time: Callable[[float], datetime] = datetime.fromtimestamp,
        morning_rundown: Callable[[float], float | None] = lambda now: None,
        holds_for_morning: Callable[[str, str], bool] = lambda kind, key: not kind.startswith(OFFER_PREFIX),
    ) -> None:
        self.store = store
        self.controls = controls
        self.settings = settings
        self.local_time = local_time
        # When today's morning rundown is due (epoch seconds) while it's still to come, else None: her own remarks
        # wait for it and go out inside it, so the day doesn't start with a nudge ahead of the rundown, or a rundown
        # that contradicts it. Only what the rundown can say waits (``holds_for_morning``, by kind and key: not an
        # offer of help), and only if it would still be current then.
        self.morning_rundown = morning_rundown
        self.holds_for_morning = holds_for_morning
        self._quiet = parse_quiet_hours(settings.quiet_hours)

    # ------------------------------------------------------------------ deciding
    def consider(
        self, candidate: Candidate, *, now: float, busy: bool = False, focusing: bool = False
    ) -> Recommendation:
        """Decide on ``candidate`` and keep the decision. A candidate whose key is already waiting (pending) updates
        that one instead of queueing a second copy; one held back again for the same reason as last time updates
        that record rather than piling up identical ones. ``focusing``: the user is in focus mode or Do Not Disturb, so
        remarks of her own wait (what they asked to be told doesn't)."""
        policy = Policy(candidate.policy)
        with self.store.transaction():
            if policy is Policy.IGNORE:
                status, reason = "suppressed", "ignored"
            else:
                status, reason = self._check(
                    key=candidate.key,
                    kind=candidate.kind,
                    policy=policy,
                    urgency=candidate.urgency,
                    relevance=candidate.relevance,
                    domain=candidate.domain,
                    sensitivity=candidate.sensitivity,
                    expires_at=candidate.expires_at,
                    now=now,
                    busy=busy,
                    focusing=focusing,
                )
            existing = self._row_to_reuse(candidate.key, status, reason)
            values = self._values(candidate, policy)
            delivered_at = now if status == "delivered" else None
            if existing is None:
                rec_id = self.store.insert(
                    "INSERT INTO recommendations(key, kind, text, policy, urgency, relevance, domain, sensitivity, "
                    "trace, note, subject, expires_at, experiment_id, variant, status, reason, created_at, "
                    "delivered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, status, reason, now, delivered_at),
                )
                changed = True
            else:
                rec_id = existing["id"]
                changed = (existing["status"], existing["reason"]) != (status, reason)
                self.store.changed(
                    "UPDATE recommendations SET key=?, kind=?, text=?, policy=?, urgency=?, relevance=?, domain=?, "
                    "sensitivity=?, trace=?, note=?, subject=?, expires_at=?, experiment_id=?, variant=?, status=?, "
                    "reason=?, delivered_at=? WHERE id=?",
                    (*values, status, reason, delivered_at, rec_id),
                )
            if changed:
                self.store.record_change(f"recommendation:{rec_id}", status, detail={"reason": reason})
        if status != "suppressed":
            log.debug("Proactive %s %r: %s%s", candidate.kind, candidate.key, status, f" ({reason})" if reason else "")
        recommendation = self.get(rec_id)
        assert recommendation is not None
        return recommendation

    def check(self, candidate: Candidate, *, now: float, busy: bool = False, focusing: bool = False) -> tuple[str, str]:
        """What ``consider`` would decide for ``candidate`` right now, as (status, reason), without keeping anything:
        for deciding whether something could be said before committing to it (an experiment's turn)."""
        policy = Policy(candidate.policy)
        if policy is Policy.IGNORE:
            return "suppressed", "ignored"
        with self.store.transaction():
            return self._check(
                key=candidate.key,
                kind=candidate.kind,
                policy=policy,
                urgency=candidate.urgency,
                relevance=candidate.relevance,
                domain=candidate.domain,
                sensitivity=candidate.sensitivity,
                expires_at=candidate.expires_at,
                now=now,
                busy=busy,
                focusing=focusing,
            )

    def due(self, *, now: float, busy: bool, focusing: bool = False) -> list[Recommendation]:
        """What was held back and may be said now: at most one, the oldest that passes the checks again. Held-back
        remarks that ran out of time expire, and those the checks now rule out (proactive switched off, the kind
        muted, said since) are dropped; the rest keep waiting."""
        with self.store.transaction():
            self._expire_pending(now)
            rows = self.store.execute("SELECT * FROM recommendations WHERE status='pending' ORDER BY created_at, id")
            for row in rows:
                rec = _to_recommendation(row)
                status, reason = self._check(
                    key=rec.key,
                    kind=rec.kind,
                    policy=rec.policy,
                    urgency=rec.urgency,
                    relevance=rec.relevance,
                    domain=rec.domain,
                    sensitivity=rec.sensitivity,
                    expires_at=rec.expires_at,
                    now=now,
                    busy=busy,
                    focusing=focusing,
                )
                if (status, reason) == (rec.status, rec.reason):
                    continue
                self.store.changed(
                    "UPDATE recommendations SET status=?, reason=?, delivered_at=? WHERE id=?",
                    (status, reason, now if status == "delivered" else None, rec.id),
                )
                if status != "pending":
                    self.store.record_change(f"recommendation:{rec.id}", status, detail={"reason": reason})
                if status == "delivered":
                    delivered = self.get(rec.id)
                    return [delivered] if delivered else []
        return []

    def release_for_morning(
        self, *, now: float, focusing: bool = False, keep: Callable[[Recommendation], bool] = lambda rec: True
    ) -> list[Recommendation]:
        """The morning rundown is being said now: the remarks of her own that were held back (and ``keep`` takes)
        and pass the checks with the rundown no longer to come are delivered, to be said inside it. INFORM ones are
        left to ``due``: they never waited for it. A conversation can't be going on: the rundown is said now."""
        delivered: list[Recommendation] = []
        with self.store.transaction():
            self._expire_pending(now)
            rows = self.store.execute(
                "SELECT * FROM recommendations WHERE status='pending' AND policy<>? ORDER BY created_at, id",
                (Policy.INFORM.value,),
            )
            for row in rows:
                rec = _to_recommendation(row)
                if not keep(rec):
                    continue
                status, reason = self._check(
                    key=rec.key,
                    kind=rec.kind,
                    policy=rec.policy,
                    urgency=rec.urgency,
                    relevance=rec.relevance,
                    domain=rec.domain,
                    sensitivity=rec.sensitivity,
                    expires_at=rec.expires_at,
                    now=now,
                    busy=False,
                    focusing=focusing,
                    in_rundown=True,
                )
                if (status, reason) == (rec.status, rec.reason):
                    continue
                self.store.changed(
                    "UPDATE recommendations SET status=?, reason=?, delivered_at=? WHERE id=?",
                    (status, reason, now if status == "delivered" else None, rec.id),
                )
                if status != "pending":
                    self.store.record_change(f"recommendation:{rec.id}", status, detail={"reason": reason})
                if status == "delivered":
                    found = self.get(rec.id)
                    if found is not None:
                        delivered.append(found)
        return delivered

    def _held_for_morning(self, kind: str, key: str, expires_at: float | None, now: float) -> bool:
        try:
            due = self.morning_rundown(now)
            if due is None or not self.holds_for_morning(kind, key):
                return False
        except Exception:
            log.warning("Couldn't tell whether the morning rundown is still to come", exc_info=True)
            return False
        return expires_at is None or expires_at >= max(now, due) + MORNING_MARGIN_S

    def _check(
        self,
        *,
        key: str,
        kind: str,
        policy: Policy,
        urgency: float,
        relevance: float,
        domain: str,
        sensitivity: str,
        expires_at: float | None,
        now: float,
        busy: bool,
        focusing: bool = False,
        in_rundown: bool = False,
    ) -> tuple[str, str]:
        """Checks 2 to 16 of the gate, in order: the first that applies decides. Returns (status, reason).
        ``in_rundown``: for saying inside the morning rundown, which is being said now: no quiet hours, no waiting
        for it."""
        s = self.settings
        inform = policy is Policy.INFORM
        if not self.controls.allows_learning(domain):
            return "suppressed", "off-limits"
        if not inform and not self.controls.proactive_enabled:
            return "suppressed", "proactive off"
        if self.controls.muted(kind, now):
            return "suppressed", "muted"
        if expires_at is not None and expires_at <= now:
            return "expired", "expired"
        # An offer is made once for its deadline or meeting (the key says which): never again, however long ago.
        since = -1.0 if kind.startswith(OFFER_PREFIX) else now - s.cooldown_s
        if self._delivered_count(since, key=key, within=True):
            return "suppressed", "said recently"
        if self._rejected(key, now - s.rejected_cooldown_s):
            return "suppressed", "you turned this down"
        if not inform and urgency * relevance < s.threshold:
            return "suppressed", "not important enough"
        if not inform and _value(sensitivity) == Sensitivity.SENSITIVE.value:
            return "suppressed", "too sensitive to bring up"
        # The budget and quiet hours are for remarks of her own: what the user asked to be told (INFORM) is said
        # whenever nobody's talking, and doesn't use up her budget either.
        if not inform and (
            self._delivered_count(now - 3600, within=True) >= s.max_per_hour
            or self._delivered_count(self._midnight(now)) >= s.max_per_day
        ):
            return "suppressed", "over budget"
        if (
            not inform
            and kind.startswith(OFFER_PREFIX)
            and self._delivered_count(self._midnight(now), kind=OFFER_PREFIX + "%") >= s.offers_per_day
        ):
            return "suppressed", "enough offers today"
        if not inform and self._delivered_count(now - s.kind_cooldown_s, kind=kind, within=True):
            return "pending", "just said something like it"
        if not inform and not in_rundown and in_window(self.local_time(now).time(), self._quiet):
            return "pending", "quiet hours"
        if not inform and not in_rundown and self._held_for_morning(kind, key, expires_at, now):
            return "pending", HELD_FOR_MORNING
        if not inform and focusing:
            return "pending", "focusing"
        if busy:
            return "pending", "busy"
        return "delivered", ""

    # ------------------------------------------------------------------ the user's reaction
    def respond(self, recommendation_id: int | None, response: str, *, now: float) -> Recommendation | None:
        """How the user took something she said. ``None`` means the last thing she said on her own, if that was in
        the last ten minutes. Two turn-downs of the same kind within a week mute that kind for a week. None when
        there's nothing delivered to respond to."""
        if response not in RESPONSES:
            raise ValueError(f"unknown response {response!r}; expected one of {', '.join(sorted(RESPONSES))}")
        with self.store.transaction():
            if recommendation_id is None:
                row = self.store.one(
                    "SELECT * FROM recommendations WHERE status='delivered' AND delivered_at>=? "
                    "ORDER BY delivered_at DESC, id DESC LIMIT 1",
                    (now - RESPOND_WINDOW_S,),
                )
            else:
                row = self.store.one(
                    "SELECT * FROM recommendations WHERE id=? AND status='delivered'", (recommendation_id,)
                )
            if row is None:
                return None
            rec_id, kind, policy = row["id"], row["kind"], row["policy"]
            self.store.changed(
                "UPDATE recommendations SET response=?, response_at=? WHERE id=?", (response, now, rec_id)
            )
            self.store.record_change(f"recommendation:{rec_id}", "response", detail={"response": response})
            # What the user asked to be told ("remind me when") is never muted for being waved off: a "that's all"
            # after a reminder isn't a turn-down of reminders. Only the user can mute those, and says so.
            if response == "rejected" and _value(policy) != Policy.INFORM.value:
                found = self.store.one(
                    "SELECT COUNT(*) AS n FROM recommendations WHERE kind=? AND response='rejected' AND response_at>=?",
                    (kind, now - REJECTION_WINDOW_S),
                )
                if found is not None and found["n"] >= MUTE_AFTER_REJECTIONS:
                    self.controls.mute(kind, now + MUTE_FOR_S, cause=f"recommendation:{rec_id}")
                    log.info("Proactive remarks of kind %r muted for a week: turned down twice", kind)
        return self.get(rec_id)

    # ------------------------------------------------------------------ reading
    def latest(self, *, delivered_only: bool = True) -> Recommendation | None:
        """The last thing she said on her own (or, with ``delivered_only=False``, the last candidate decided on)."""
        if delivered_only:
            row = self.store.one(
                "SELECT * FROM recommendations WHERE status='delivered' ORDER BY delivered_at DESC, id DESC LIMIT 1"
            )
        else:
            row = self.store.one("SELECT * FROM recommendations ORDER BY created_at DESC, id DESC LIMIT 1")
        return _to_recommendation(row) if row else None

    def get(self, recommendation_id: int) -> Recommendation | None:
        row = self.store.one("SELECT * FROM recommendations WHERE id=?", (recommendation_id,))
        return _to_recommendation(row) if row else None

    def list(self, *, status: str | None = None, limit: int = 20) -> list[Recommendation]:
        """Recommendations newest first, optionally only those with ``status``."""
        where, params = (" WHERE status=?", [status]) if status else ("", [])
        rows = self.store.execute(
            f"SELECT * FROM recommendations{where} ORDER BY created_at DESC, id DESC LIMIT ?", (*params, limit)
        )
        return [_to_recommendation(r) for r in rows]

    def delivered_since(self, since: float) -> int:
        """How many things she said on her own since ``since``."""
        return self._delivered_count(since)

    def delete_all(self) -> int:
        """Forget every recommendation (the user wiped what she keeps). Returns how many there were."""
        return self.store.changed("DELETE FROM recommendations")

    # ------------------------------------------------------------------ helpers
    def _values(self, candidate: Candidate, policy: Policy) -> tuple[Any, ...]:
        trace = _trace_of(candidate.trace)
        if candidate.listen:
            trace[_LISTEN] = True
        return (
            candidate.key,
            candidate.kind,
            candidate.text,
            policy.value,
            float(candidate.urgency),
            float(candidate.relevance),
            candidate.domain,
            _value(candidate.sensitivity),
            dumps(trace),
            candidate.note,
            candidate.subject,
            candidate.expires_at,
            candidate.experiment_id,
            candidate.variant,
        )

    def _row_to_reuse(self, key: str, status: str, reason: str) -> sqlite3.Row | None:
        """The record a new decision on ``key`` updates rather than adds to: the one waiting to be said, or, for a
        remark held back again for the same reason, the last such record."""
        row = self.store.one(
            "SELECT * FROM recommendations WHERE key=? AND status='pending' ORDER BY created_at, id LIMIT 1", (key,)
        )
        if row is not None or status not in ("suppressed", "expired"):
            return row
        last = self.store.one(
            "SELECT * FROM recommendations WHERE key=? ORDER BY created_at DESC, id DESC LIMIT 1", (key,)
        )
        if last is not None and (last["status"], last["reason"]) == (status, reason):
            return last
        return None

    def _expire_pending(self, now: float) -> None:
        oldest = now - self.settings.pending_max_s
        rows = self.store.execute(
            "SELECT id, expires_at FROM recommendations WHERE status='pending' "
            "AND ((expires_at IS NOT NULL AND expires_at<=?) OR (expires_at IS NULL AND created_at<=?))",
            (now, oldest),
        )
        for row in rows:
            reason = "expired" if row["expires_at"] is not None else "waited too long"
            self.store.changed("UPDATE recommendations SET status='expired', reason=? WHERE id=?", (reason, row["id"]))
            self.store.record_change(f"recommendation:{row['id']}", "expired", detail={"reason": reason})

    def _delivered_count(
        self, since: float, *, key: str | None = None, kind: str | None = None, within: bool = False
    ) -> int:
        """How many were delivered from ``since`` on; ``within``: only strictly after it (said less than a cooldown
        ago, so one said exactly a cooldown ago no longer counts). A ``kind`` ending in "%" is a prefix."""
        clauses = ["status='delivered'", "policy<>'inform'", "delivered_at>?" if within else "delivered_at>=?"]
        params: list[Any] = [since]
        for column, value in (("key", key), ("kind", kind)):
            if value is not None and value.endswith("%"):
                clauses.append(f"substr({column}, 1, ?)=?")
                params.extend((len(value) - 1, value[:-1]))
            elif value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        row = self.store.one(f"SELECT COUNT(*) AS n FROM recommendations WHERE {' AND '.join(clauses)}", params)
        return int(row["n"]) if row else 0

    def _rejected(self, key: str, since: float) -> bool:
        row = self.store.one(
            "SELECT 1 FROM recommendations WHERE key=? AND response='rejected' AND response_at>? LIMIT 1",
            (key, since),
        )
        return row is not None

    def _midnight(self, now: float) -> float:
        """Epoch seconds of the user's last local midnight (by the wall clock ``local_time`` reads)."""
        local = self.local_time(now)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return now - (local - start).total_seconds()
