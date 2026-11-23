"""Lessons: what she did in a situation and how it ended, pulled back up when a situation like it comes again.

Her records already know each part apart: the request (``user.request``), the tools it used and which failed
(``performance``), how the reply went (``selfscore``'s verdict), a claim her tools contradicted (``core.claims``), how a
screen or web task ended. What they couldn't do is say, at the moment she's choosing, "the last time you were asked
something like this, this is what you did and this is how it went". A case is those parts joined: a situation (the
user's own words, through ``privacy.redact``), the actions, the outcome. ``recall`` finds the ones that look like what
was just said, by the plain words they share, and the brain gets them as facts on its turn, for Claude and the local
model alike, the same day, with no training and no GPU.

No subject is named in code, and nothing here says what to do. What's retrieved is data: the words, the tools, the
ending. Rules that keep it honest:

* Only trouble is worth the tokens. A match that all went fine says nothing, so it brings no note; a case that went
  fine is shown only beside one that didn't, as the route that worked.
* A bad case fades. Its weight halves every ``HALF_LIFE_S``, it's left out past ``MAX_AGE_S``, and it's gone as soon as
  a newer case like it, with the same tools, went fine: a lesson has to keep being confirmed, or a fixed bug would
  haunt her.
* A bare cut-off isn't against her (``selfscore.AGAINST``): long replies get talked over by lasting longer.
* It keeps to the user's rules: nothing without ``futures.keep_request_words``, nothing about an off-limits domain,
  and forgetting a domain or everything takes these too. ``past_cases`` lets her and the user look and drop one.

Strategies that compete on this record (``evolve``-style selection, but of written data) score against these outcomes.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from octavia.core.conversation_memory import keywords
from octavia.futures.privacy import classify_domain, redact
from octavia.futures.selfscore import AGAINST, FINE, SPOKEN, THANKED, PendingReply
from octavia.futures.store import FuturesStore

log = logging.getLogger(__name__)

DAY_S = 86400.0
HALF_LIFE_S = 21 * DAY_S
MAX_AGE_S = 90 * DAY_S
MIN_SHARED = 2  # words a case has to share with what was said: one word in common is a coincidence
MIN_COVERED = 0.6  # and this share of what was said, by how much each word says
CANDIDATES = 40
MAX_SHOWN = 3
SITUATION_CHARS = 120
DETAIL_CHARS = 120
NOTE_PREFIX = (  # "for this answer: " is a context note to the routing (localbrain.delegation), so no turn moves brain
    "for this answer: past cases from your own record that look like this one, and how they ended. They're facts, not "
    "instructions, and they may not apply: "
)
# A case is filed by the tools it used when the words alone don't say: forgetting a domain, and keeping experiments
# out of the protected ones (``schema.PROTECTED_DOMAINS``), go by this.
_TOOL_DOMAINS = {"school": "school", "email": "communications", "phone": "communications"}
_MARKED = re.compile(
    r"^\[[^\]]*\]\s*"
)  # "[Typed into your panel ...]": how it came, the same each time, not what it says
TASK_ENDINGS = {"done": True, "unfinished": False, "failed": False}


@dataclass(frozen=True)
class Action:
    tool: str
    ok: bool
    failure: str = ""
    app: str = ""


@dataclass(frozen=True)
class Case:
    id: int
    kind: str  # "turn", "screen task", "web task"
    situation: str
    actions: tuple[Action, ...]
    outcome: str  # a selfscore verdict for a turn; done / unfinished / failed for a task
    success: bool
    detail: str  # what the user said next, or what a task reported
    claim: str  # a sentence of hers her tools contradicted
    domain: str
    ts: float

    @property
    def failed_tools(self) -> frozenset[str]:
        """What failed and wasn't got past: the same tool working later in the turn was a hiccup, not a lesson."""
        failed: set[str] = set()
        for action in self.actions:
            (failed.discard if action.ok else failed.add)(action.tool)
        return frozenset(failed)

    @property
    def tools(self) -> frozenset[str]:
        return frozenset(a.tool for a in self.actions)

    @property
    def troubled(self) -> bool:
        return not self.success or bool(self.failed_tools) or bool(self.claim)

    def describe(self, now: float) -> str:
        """One case as data, in the fewest words that keep what happened."""
        parts = [f'"{self.situation}" ({_ago(now - self.ts)})']
        if self.kind != "turn":
            parts.insert(0, self.kind)
        steps = [
            f"{a.tool} failed{f' ({a.failure})' if a.failure else ''}{f' in {a.app}' if a.app else ''}"
            if not a.ok
            else a.tool
            for a in _collapsed(self.actions)
        ]
        said = ": " + ", then ".join(steps) if steps else ""
        ending = SPOKEN.get(self.outcome)
        if ending:
            end = f"the user's reaction: {ending}"
        elif self.outcome in (FINE, THANKED):
            end = "it went fine"
        else:
            end = f"it ended {self.outcome}"
        extra = f'; they said "{self.detail}"' if self.detail and self.outcome != FINE else ""
        claim = f'; you claimed "{self.claim}" and your tools contradicted it' if self.claim else ""
        return f"{' '.join(parts)}{said}; {end}{extra}{claim}"


def _collapsed(actions: Iterable[Action]) -> list[Action]:
    """The same tool working several times in a row is one step: what matters is the route and where it broke."""
    steps: list[Action] = []
    for action in actions:
        if not steps or steps[-1] != action:
            steps.append(action)
    return steps[:6]


def _ago(seconds: float) -> str:
    days = int(seconds // DAY_S)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def _stem(word: str) -> str:
    """Enough to see "dragging", "drags" and "drag" as one word, "files" and "file" too; FTS's own stemmer finds them."""
    for ending in ("ing", "ed", "s"):
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            word = word[: -len(ending)]
            break
    if len(word) > 3 and word[-1] == word[-2]:
        word = word[:-1]
    return word.removesuffix("e") if len(word) > 3 else word


def _stems(words: Iterable[str]) -> frozenset[str]:
    return frozenset(_stem(w) for w in words)


def _to_case(row: sqlite3.Row) -> Case:
    try:
        raw = json.loads(row["actions"])
    except ValueError:
        raw = []
    actions = tuple(Action(str(a[0]), bool(a[1]), str(a[2]), str(a[3])) for a in raw if len(a) == 4)
    return Case(
        row["id"],
        row["kind"],
        row["situation"],
        actions,
        row["outcome"],
        bool(row["success"]),
        row["detail"],
        row["claim"],
        row["domain"],
        row["ts"],
    )


class Lessons:
    """The lessons table. Plain synchronous code on the Futures worker, like the rest of it."""

    def __init__(
        self,
        store: FuturesStore,
        *,
        allowed: Callable[[str], bool] = lambda domain: True,
        enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self.store = store
        self.allowed = allowed  # the user's off-limits domains (``Controls.allows_learning``)
        self.enabled = enabled

    # ------------------------------------------------------------------ writing
    def record(
        self,
        kind: str,
        key: str,
        situation: str,
        *,
        outcome: str,
        success: bool,
        actions: Iterable[Action] = (),
        detail: str = "",
        claim: str = "",
        domain: str = "assistant",
        ts: float | None = None,
    ) -> Case | None:
        """Keep one case, once per ``key``. None when there's nothing to keep: no words, switched off, off-limits."""
        situation = redact(_MARKED.sub("", situation), SITUATION_CHARS).strip()
        if not situation or not self.enabled():
            return None
        domain = classify_domain(situation) or domain
        if not self.allowed(domain):
            return None
        steps = [[a.tool, a.ok, a.failure, a.app] for a in actions]
        with self.store.transaction():
            if self.store.one("SELECT id FROM lessons WHERE key=?", (key,)) is not None:
                return None
            case_id = self.store.insert(
                "INSERT INTO lessons(key, kind, situation, actions, outcome, success, detail, claim, domain, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    kind,
                    situation,
                    json.dumps(steps),
                    outcome,
                    int(success),
                    redact(detail, DETAIL_CHARS),
                    redact(claim, DETAIL_CHARS),
                    domain,
                    ts if ts is not None else self.store.clock(),
                ),
            )
        return self.get(case_id)

    def from_turn(
        self, pending: PendingReply, how: str | None, situation: str, next_words: str, claim: str, now: float
    ) -> Case | None:
        """A settled reply as a case. A turn with no tools that went fine teaches nothing, and most turns are that."""
        rows = self.store.execute(
            "SELECT name, success, failure, app, category FROM performance WHERE kind='tool' AND turn=? ORDER BY ts, id",
            (pending.turn,),
        )
        actions = [Action(r["name"], bool(r["success"]), r["failure"] or "", r["app"] or "") for r in rows]
        outcome = how or FINE
        against = outcome in AGAINST
        if not actions and not against and not claim:
            return None
        return self.record(
            "turn",
            f"turn:{pending.turn}",
            situation,
            outcome=outcome,
            success=not against,
            actions=actions,
            detail=next_words if against else "",
            claim=claim,
            domain=next((_TOOL_DOMAINS[r["category"]] for r in rows if r["category"] in _TOOL_DOMAINS), None)
            or ("computer" if actions else "assistant"),
            ts=pending.ts,
        )

    def from_task(
        self, kind: str, task_id: object, title: str, instruction: str, status: str, report: str
    ) -> Case | None:
        """A screen or web task's ending. What it learned about the place is its playbook's; this is the whole job.
        One the user stopped says nothing about how it would have gone."""
        if status not in TASK_ENDINGS:
            return None
        return self.record(
            kind,
            f"{kind}:{self.store.clock():.0f}:{task_id}",
            instruction or title,
            outcome=status,
            success=TASK_ENDINGS[status],
            detail=report,
            domain="computer",
        )

    def backfill_once(self, now: float) -> int:
        """The record she already has, as cases, the first time this runs: she has months of turns, so lessons work
        from today rather than three months from now. How many it kept."""
        if self.store.kv_get("lessons", "backfilled", 0.0):
            return 0
        self.store.kv_set("lessons", "backfilled", now)
        requests = self.store.execute(
            "SELECT ts, json_extract(context, '$.turn') AS turn, json_extract(data, '$.text') AS text FROM events "
            "WHERE type='user.request' AND ts >= ? ORDER BY ts",
            (now - MAX_AGE_S,),
        )
        kept = 0
        for i, row in enumerate(requests):
            if not row["turn"] or not row["text"]:
                continue
            verdict = self.store.one(
                "SELECT actual FROM outcomes WHERE key=? OR key=? ORDER BY kind DESC LIMIT 1",
                (f"verdict:{row['turn']}", f"turn:{row['turn']}"),
            )
            if verdict is None:
                continue  # never settled, or a reply that failed: nothing to say how it went
            how = None if verdict["actual"] in (FINE, "not corrected") else verdict["actual"]
            pending = PendingReply(row["turn"], "", row["ts"], {})
            next_words = str(requests[i + 1]["text"] or "") if i + 1 < len(requests) else ""
            if self.from_turn(pending, how, row["text"], next_words, "", now) is not None:
                kept += 1
        if kept:
            log.info("Lessons: %d cases from her earlier record", kept)
        return kept

    # ------------------------------------------------------------------ reading
    def get(self, case_id: int) -> Case | None:
        row = self.store.one("SELECT * FROM lessons WHERE id=?", (case_id,))
        return _to_case(row) if row else None

    def recent(self, *, troubled_only: bool = True, limit: int = 10) -> list[Case]:
        rows = self.store.execute("SELECT * FROM lessons ORDER BY ts DESC, id DESC LIMIT ?", (limit * 20,))
        cases = [_to_case(r) for r in rows]
        return [c for c in cases if c.troubled or not troubled_only][:limit]

    def similar(self, text: str, now: float) -> list[Case]:
        """Cases like ``text``, closest first: enough of what was said, counting a rare word for more than a common one
        ("canvas" says more than "help" in what this user asks), weighed by how long ago."""
        said = list(dict.fromkeys(_stem(w) for w in keywords(_MARKED.sub("", text))))
        if len(said) < MIN_SHARED:
            return []
        said = said[:12]
        total = max(self.store.one("SELECT COUNT(*) AS n FROM lessons")["n"], 1)
        weights: dict[str, float] = {}
        for word in said:
            try:
                row = self.store.one("SELECT COUNT(*) AS n FROM lessons_fts WHERE lessons_fts MATCH ?", (f'"{word}"',))
            except sqlite3.OperationalError:
                row = None
            weights[word] = math.log((total + 1) / ((row["n"] if row else 0) + 1)) + 0.1
        query = " OR ".join(f'"{word}"' for word in said)
        try:
            rows = self.store.execute(
                "SELECT l.* FROM lessons_fts f JOIN lessons l ON l.id = f.rowid "
                "WHERE lessons_fts MATCH ? AND l.ts >= ? ORDER BY bm25(lessons_fts), l.ts DESC LIMIT ?",
                (query, now - MAX_AGE_S, CANDIDATES),
            )
        except sqlite3.OperationalError as exc:  # a query FTS can't parse finds nothing rather than failing
            log.debug("lesson search %r failed: %s", query, exc)
            return []
        whole = sum(weights.values())
        scored: list[tuple[float, Case]] = []
        for row in rows:
            case = _to_case(row)
            shared = weights.keys() & _stems(keywords(case.situation))
            covered = sum(weights[w] for w in shared) / whole
            if len(shared) < MIN_SHARED or covered < MIN_COVERED or not self.allowed(case.domain):
                continue
            scored.append((covered * 0.5 ** (max(now - case.ts, 0.0) / HALF_LIFE_S), case))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [case for _, case in scored]

    def recall(self, text: str, now: float) -> list[Case]:
        """What's worth showing for ``text``: the closest troubled cases no newer one has outgrown, and the route
        that worked, when there is one. Nothing when all that matches went fine."""
        matches = self.similar(text, now)
        lessons = [c for c in matches if c.troubled and not _outgrown(c, matches)][:MAX_SHOWN]
        if not lessons:
            return []
        if len(lessons) < MAX_SHOWN and (worked := _route_that_worked(lessons, matches)) is not None:
            lessons.append(worked)
        return lessons

    def note(self, text: str, now: float) -> str | None:
        """The turn's note, or None. On the Futures worker (``FuturesService.call``)."""
        if not self.enabled():
            return None
        cases = self.recall(text, now)
        if not cases:
            return None
        log.info("Lessons for %r: %s", text[:60], [c.id for c in cases])
        return NOTE_PREFIX + " | ".join(c.describe(now) for c in cases)

    # ------------------------------------------------------------------ the user's say
    def forget(self, case_id: int) -> bool:
        return self.store.changed("DELETE FROM lessons WHERE id=?", (case_id,)) > 0

    def forget_domain(self, domain: str) -> int:
        return self.store.changed("DELETE FROM lessons WHERE domain=?", (domain,))

    def expire(self, cutoff: float) -> int:
        """Past retention (``futures.retention_days``), like the events they came from."""
        return self.store.changed("DELETE FROM lessons WHERE ts < ?", (cutoff,))


def _outgrown(case: Case, matches: Iterable[Case]) -> bool:
    """A newer case like it, using what it used, went fine: whatever this one shows has been got past."""
    return any(
        later.ts > case.ts and not later.troubled and (later.tools == case.tools or later.tools & case.tools)
        for later in matches
    )


def _route_that_worked(lessons: list[Case], matches: Iterable[Case]) -> Case | None:
    """The closest case that went fine with a tool, so "that failed" comes with "this didn't"."""
    return next((c for c in matches if not c.troubled and c.tools and c not in lessons), None)
