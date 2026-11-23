"""The proactive service: one night's planning and work, keeping each item's status in step with its objective, the
morning handoff, the user's answers and the report.

When it runs: once a night, inside ``proactive.night``, once the user has had no turn for ``away_min`` minutes, and
only while the usage budget allows her own background work (her daily share, and your 5-hour and weekly limits:
the same check as the night shift's). Each step of the objectives it starts asks the budget again
(``ObjectiveRunner``'s proactive budget), so work pauses when your limits fill up.

Nothing is said at night: proactive objectives aren't announced when they end. The handoff rides inside the morning
rundown when there is one (``morning_remarks``), and otherwise waits for the user's first turn after the night.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from octavia.core.jsonfile import read_json, write_json
from octavia.proactive import handoff, learn, planner, rules
from octavia.proactive import model as user_model
from octavia.proactive.store import (
    APPROVE,
    DECISIONS,
    FAILED,
    PLANNED,
    READY,
    REDIRECT,
    REJECT,
    RUNNING,
    UNVERIFIED,
    Item,
    ItemStore,
)

log = logging.getLogger(__name__)

TICK_S = 300.0
BUDGET_RETRY_S = 1800.0  # after the budget said no, ask it again this much later (each ask is a small Claude call)
FIRST_TICK_S = 90.0
CLAIM_WINDOW_S = 3 * 86400.0  # what she may say she prepared: items ready within this
HANDOFF_STALE_S = 2 * 86400.0  # items older than this aren't handed over by themselves (still listed when asked)

Announce = Callable[[str, str], None]


def parse_window(text: str) -> tuple[clock_time, clock_time]:
    start, end = (part.strip() for part in text.split("-", 1))
    return clock_time.fromisoformat(start.zfill(5)), clock_time.fromisoformat(end.zfill(5))


def night_of(moment: datetime, window: tuple[clock_time, clock_time]) -> date | None:
    """The morning a moment's night belongs to (the date the night window ends on), or None outside the window."""
    start, end = window
    t = moment.time()
    if start <= end:
        return moment.date() if start <= t < end else None
    if t >= start:
        return moment.date() + timedelta(days=1)
    return moment.date() if t < end else None


@dataclass
class _State:
    last_night: str = ""  # the morning of the last night it ran (ISO)
    last_run: float = 0.0


class ProactiveService:
    def __init__(
        self,
        settings: Callable[[], Any],  # the ProactiveConfig in force (it can change while she runs)
        store: ItemStore,
        *,
        sources: Callable[[], user_model.Sources],
        runner: Any = None,  # the ObjectiveRunner; None: plan only
        projects: Callable[[], Sequence[Any]] = list,
        user: str = "the user",
        name: str = "Octavia",
        ranker: planner.Ranker | None = None,
        budget: Callable[[], Awaitable[str | None]] | None = None,
        announce: Announce | None = None,
        last_user_turn: Callable[[], float | None] = lambda: None,
        rundown_due: Callable[[float], float | None] = lambda now: None,
        max_open: int = 3,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self.store = store
        self._sources = sources
        self.runner = runner
        self._projects = projects
        self.user = user
        self.name = name
        self._ranker = ranker
        self._budget = budget
        self._announce = announce
        self._last_user_turn = last_user_turn
        self._rundown_due = rundown_due
        self._max_open = max_open
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._retry_at = 0.0
        self._state = self._load_state()

    @property
    def settings(self) -> Any:
        return self._settings()

    @property
    def folder(self) -> Path:
        return self.store.folder

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="proactive")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_TICK_S)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Proactive: a look went wrong", exc_info=True)
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        self.sync()
        if self.due():
            await self.run_night()

    # ------------------------------------------------------------------ when

    def _window(self) -> tuple[clock_time, clock_time]:
        return parse_window(self.settings.night)

    def _away_s(self, now: float) -> float:
        last = self._last_user_turn()
        return float("inf") if last is None else now - last

    def due(self, now: float | None = None) -> bool:
        """Tonight's planning hasn't run, it's night, and the user has been away long enough."""
        now = self._clock() if now is None else now
        if self.settings.level == "off" or self._running:
            return False
        night = night_of(datetime.fromtimestamp(now), self._window())
        if night is None or self._state.last_night == night.isoformat() or now < self._retry_at:
            return False
        return self._away_s(now) >= self.settings.away_min * 60

    # ------------------------------------------------------------------ one night

    async def run_night(self, now: float | None = None) -> dict[str, Any]:
        """Plan tonight and start what was chosen. Returns what happened (also kept in ``nights.jsonl``)."""
        now = self._clock() if now is None else now
        cfg = self.settings
        night = night_of(datetime.fromtimestamp(now), self._window()) or datetime.fromtimestamp(now).date()
        record: dict[str, Any] = {"ts": round(now, 3), "night": night.isoformat(), "level": cfg.level}
        self._running = True
        try:
            if cfg.level == "off":
                record["skipped"] = "off"
                return record
            if self._budget is not None and (why := await self._budget()):
                record["skipped"] = f"usage budget: {why}"
                self._retry_at = now + BUDGET_RETRY_S
                log.info("Proactive: not now (%s)", why)
                return self._ran(record, night, done=False)
            model = await self.build_model(now)
            coding = cfg.level == "projects"
            found = rules.candidates(model, now=now, coding=coding, projects=tuple(cfg.projects))
            kept, skipped = planner.fresh(found, self.store.items, now)
            steer = learn.steering(
                self.store.items, min_decisions=cfg.steer_min_decisions, min_agreement=cfg.steer_agreement
            )
            picks = planner.guesses(kept, self.store.items, steer=steer)
            chosen, how = await planner.choose(
                picks,
                max_items=cfg.max_items,
                model_text=model.text(),
                ranker=self._ranker if cfg.rank else None,
                name=self.name,
                user=self.user,
                steer=steer,
            )
            record.update(found=len(found), fresh=len(kept), how=how, steering=steer, unread=model.unread)
            started, refused = [], []
            free = self._free_slots() if cfg.level != "suggest" else len(chosen)
            for pick in chosen:
                candidate = pick.candidate
                if why := rules.refused(candidate, coding=coding):
                    refused.append(f"{candidate.title}: {why}")
                    log.warning("Proactive: refused “%s”: %s", candidate.title, why)
                    continue
                item = self._item(candidate, pick, night)
                if cfg.level == "suggest":
                    item.status = PLANNED
                elif free <= 0:
                    skipped.append(f"{candidate.title}: no objective free")
                    continue
                else:
                    await self._start(item)
                    free -= 1
                self.store.add(item)
                started.append(f"#{item.id} {item.title} ({item.status})")
            record.update(items=started, skipped=skipped, refused=refused)
            log.info("Proactive night %s: %s", night, "; ".join(started) or "nothing worth preparing")
            return self._ran(record, night, done=True)
        finally:
            self._running = False

    async def build_model(self, now: float | None = None) -> user_model.UserModel:
        """Her picture of the user's week, read again now (and kept in ``model.json``)."""
        model = await user_model.build(
            self._sources(),
            user=self.user,
            look_ahead_days=self.settings.look_ahead_days,
            now=self._clock() if now is None else now,
        )
        model.decisions = self.decision_facts()
        await asyncio.to_thread(user_model.save, model, self.folder / "model.json")
        return model

    def _item(self, candidate: rules.Candidate, pick: planner.Pick, night: date) -> Item:
        return Item(
            id=self.store.next_id(),
            night=night.isoformat(),
            key=candidate.key,
            kind=candidate.kind,
            title=candidate.title,
            goal=candidate.goal,
            why=candidate.why,
            features=dict(candidate.features),
            due=candidate.due,
            project=candidate.project,
            prediction=pick.guess.p,
            basis=pick.guess.basis,
            rank_why=pick.why,
            subject=candidate.subject,
            plan=candidate.plan,
        )

    def _free_slots(self) -> int:
        """Objectives she may start now: always one left for the user's own."""
        if self.runner is None:
            return 0
        open_now = sum(1 for o in self.runner.objectives if o.open)
        return max(0, self._max_open - 1 - open_now)

    def _project(self, name: str | None) -> Any:
        if not name:
            return None
        wanted = name.casefold()
        return next((p for p in self._projects() if p.name.casefold() == wanted), None)

    async def _start(self, item: Item, *, origin: str = "proactive", extra: str = "") -> None:
        """Run ``item`` as an objective with the plan the rules wrote for it (checked by the reviewer), through the
        same limits as the night's (``rules.refused``) whoever started it; failed when it can't start."""
        from octavia.objectives.runner import PROACTIVE

        if self.runner is None:
            item.status, item.problem = FAILED, "objectives aren't running"
            return
        candidate = rules.Candidate(item.key, item.kind, item.title, item.goal, item.plan, item.why,
                                    project=item.project)  # fmt: skip
        if why := rules.refused(candidate, coding=self.settings.level == "projects"):
            item.status, item.problem = FAILED, f"it can't run as overnight work: {why}"
            return
        project = await asyncio.to_thread(self._project, candidate.project)  # looking runs git: off the loop
        if candidate.project and project is None:
            item.status, item.problem = FAILED, f"the project {candidate.project} wasn't found"
            return
        plan = rules.with_limits(candidate, self.user)
        said = f"“{' '.join(extra.split())}”" if extra else ""  # their words, quoted: data, not her instructions
        if said and plan is not None:
            for step in plan["steps"]:
                step["instruction"] += f"\n\nWhat {self.user} asked for instead, in their words: {said}"
        goal = candidate.goal + (f" What {self.user} wants: {said}" if said else "")
        try:
            objective = self.runner.start(
                goal, project, plan=plan, origin=PROACTIVE if origin == "proactive" else origin
            )
        except (RuntimeError, ValueError) as exc:
            item.status, item.problem = FAILED, str(exc)
            return
        item.status, item.objective = RUNNING, objective.id

    def _ran(self, record: dict[str, Any], night: date, *, done: bool) -> dict[str, Any]:
        if done:
            self._state.last_night = night.isoformat()
        self._state.last_run = record["ts"]
        self._save_state()
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with (self.folder / "nights.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.warning("Couldn't write the proactive night record", exc_info=True)
        return record

    # ------------------------------------------------------------------ in step with the objectives

    def sync(self) -> bool:
        """Each running item's status from its objective. Ready only when every step passed its check."""
        from octavia.objectives.models import CODE, DONE, OBJECTIVE_CANCELLED, OBJECTIVE_FAILED, VERIFIED

        if self.runner is None:
            return False
        changed = False
        for item in self.store.items:
            if item.status != RUNNING or item.objective is None:
                continue
            objective = next((o for o in self.runner.objectives if o.id == item.objective), None)
            if objective is None:
                item.status, item.problem, changed = FAILED, "its objective's record is gone", True
                continue
            if objective.open:
                continue
            steps = objective.steps
            item.files = [s.file for s in steps if s.status == VERIFIED and s.file]
            item.evidence = " ".join(" ".join(s.evidence.split())[:200] for s in steps if s.status == VERIFIED)[:400]
            if objective.status == DONE and steps and all(s.status == VERIFIED for s in steps):
                item.status = READY
                if any(s.kind == CODE for s in steps) and objective.code_to_merge:
                    item.branch = objective.branch
            elif objective.status in (OBJECTIVE_FAILED, OBJECTIVE_CANCELLED) or not any(s.attempts for s in steps):
                item.status = FAILED
                item.problem = objective.error or ("cancelled" if objective.status == OBJECTIVE_CANCELLED else "")
            else:
                item.status = UNVERIFIED
                failed = next((s for s in steps if s.status != VERIFIED and s.problem), None)
                item.problem = failed.problem if failed else "it didn't pass its check"
            changed = True
        if changed:
            self.store.save()
        return changed

    # ------------------------------------------------------------------ the handoff

    def to_hand_over(self, now: float | None = None) -> list[Item]:
        """What's to be told, from recent nights: finished (or only planned) items not handed over yet, and how the
        ones the user kept (or redirected) while they still ran ended, failures too."""
        now = self._clock() if now is None else now

        def due(i: Item) -> bool:
            if i.told or i.decision == REJECT or now - (i.created or now) >= HANDOFF_STALE_S:
                return False
            if i.decided:
                return i.status in (READY, UNVERIFIED, FAILED)
            return i.status in (READY, UNVERIFIED, PLANNED)

        return [i for i in self.store.items if due(i)]

    def answerable(self, now: float | None = None) -> list[Item]:
        """What "all of it" means: handed over lately and not answered yet."""
        now = self._clock() if now is None else now
        return [
            i
            for i in self.store.items
            if i.told
            and not i.decided
            and i.status in (READY, UNVERIFIED, PLANNED)
            and now - (i.created or now) < HANDOFF_STALE_S
        ]

    def _in_night(self, now: float) -> bool:
        return night_of(datetime.fromtimestamp(now), self._window()) is not None

    def hand_over(self, now: float | None = None) -> tuple[str, str] | None:
        """The handoff's (spoken lines, note), marking the items told; None when there's nothing, or it's night."""
        now = self._clock() if now is None else now
        self.sync()
        if self._in_night(now):
            return None
        items = self.to_hand_over(now)
        running = [i for i in self.store.items if i.status == RUNNING and not i.decided]
        said = handoff.morning_text(items, running)
        if said is None:
            return None
        text, note = said
        for item in items:
            item.told = True
        self.store.save()
        return text, note

    async def morning_remarks(self) -> list[tuple[str, str | None]]:
        """For the morning rundown (``Reminders``' remarks): the handoff said inside it."""
        said = self.hand_over()
        return [said] if said else []

    def on_user_turn(self, now: float | None = None) -> None:
        """The user is here. Without a rundown still to come today, hand over what's waiting (once)."""
        now = self._clock() if now is None else now
        if self._announce is None or self._rundown_due(now) is not None:
            return
        said = self.hand_over(now)
        if said:
            self._announce(*said)

    # ------------------------------------------------------------------ the user's answers

    async def decide(self, item: Item, decision: str, note: str = "", *, redo: bool = False) -> str:
        """Record the user's answer to ``item``; what happened, for her to say. One still running stays to be told
        how it ended."""
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        if decision == REDIRECT and not note.strip():
            raise ValueError("a redirect needs what they want instead")
        item.decision, item.note, item.decided_at = decision, note.strip()[:600], self._clock()
        item.told = item.status != RUNNING
        out = ""
        if decision == APPROVE:
            if item.status == RUNNING:
                out = f"Noted; “{item.title}” is still being checked, and I'll say how it ends."
            elif item.status == PLANNED:
                await self._start(item, origin="user")
                out = (
                    f"Started “{item.title}” (objective #{item.objective}); it says when it's checked."
                    if item.status == RUNNING
                    else f"Couldn't start it: {item.problem}."
                )
            elif item.status == READY:
                where = f" It's at {', '.join(item.files)}." if item.files else ""
                merge = (
                    f" Its code is on branch {item.branch}: merge_objective_work #{item.objective} merges it when "
                    "they say so."
                    if item.branch
                    else ""
                )
                out = f"Kept “{item.title}”.{where}{merge}"
            elif item.status == UNVERIFIED:
                out = f"Kept “{item.title}”, though it didn't pass its check ({item.problem}): say so if it's used."
            else:
                out = f"Noted; “{item.title}” is {item.status}."
        elif decision == REJECT:
            out = f"Dropped “{item.title}”; I won't prepare it again for a while."
        else:
            if redo:
                again = Item(
                    id=self.store.next_id(),
                    night=item.night,
                    key=item.key + ":redo",
                    kind=item.kind,
                    title=item.title,
                    goal=item.goal,
                    why=f"redone as {self.user} asked: {item.note}",
                    features=dict(item.features),
                    project=item.project,
                    decision=APPROVE,
                    decided_at=self._clock(),
                    subject=item.subject,
                    plan=item.plan,
                )
                await self._start(again, origin="user", extra=item.note)
                again.told = again.status != RUNNING
                self.store.add(again)
                out = (
                    f"Noted, and redoing it their way (objective #{again.objective}); it says when it's checked."
                    if again.status == RUNNING
                    else f"Noted, but couldn't redo it: {again.problem}."
                )
            else:
                out = f"Noted how they want “{item.title}” done; I'll go by it next time."
        self.store.save()
        return out

    def decision_facts(self, limit: int = 12) -> list[user_model.Fact]:
        decided = [i for i in self.store.items if i.decided][-limit:]
        return [
            user_model.Fact(
                "decision",
                f"{i.title}: {i.decision}" + (f" ({i.note[:120]})" if i.note else ""),
                "decisions",
                f"item:{i.id}",
            )
            for i in decided
        ]

    # ------------------------------------------------------------------ what she says about it

    def verified_titles(self, now: float | None = None) -> list[str]:
        """What she may say she prepared: the subjects of items whose check passed, lately."""
        now = self._clock() if now is None else now
        return [
            i.subject or i.title
            for i in self.store.items
            if i.status == READY and now - (i.created or 0) < CLAIM_WINDOW_S
        ]

    async def summary_for_prompt(self) -> str:
        waiting = [i for i in self.store.items if not i.decided and i.status in (READY, UNVERIFIED, PLANNED, RUNNING)]
        waiting = [i for i in waiting if self._clock() - (i.created or 0) < HANDOFF_STALE_S]
        if not waiting:
            return ""
        words = {READY: "ready, checked", UNVERIFIED: "didn't pass its check", PLANNED: "only planned",
                 RUNNING: "still running"}  # fmt: skip
        rows = "; ".join(f"#{i.id} {i.title[:60]} ({words[i.status]})" for i in waiting[:4])
        return f"Your overnight work (overnight_work, decide_overnight_work): {rows}."

    def report(self, days: float = 14, now: float | None = None) -> str:
        now = self._clock() if now is None else now
        cfg = self.settings
        items = [i for i in self.store.items if now - (i.created or 0) < days * 86400]
        score = learn.agreement(self.store.items)
        steer = learn.steering(
            self.store.items, min_decisions=cfg.steer_min_decisions, min_agreement=cfg.steer_agreement
        )
        lines = [
            f"Proactive work: level {cfg.level}, up to {cfg.max_items} a night, {cfg.night} once you've been away "
            f"{cfg.away_min:g} min.",
            f"What would you pick: {score.describe()}"
            + (f", Brier {score.brier}" if score.brier is not None else "")
            + ("; my guesses shape the plan now." if steer else "; my guesses don't shape the plan yet."),
        ]
        if self._state.last_night:
            lines.append(f"Last night planned: {self._state.last_night}.")
        for item in reversed(items):
            answer = f", you said {item.decision}" + (f" ({item.note})" if item.note else "") if item.decided else ""
            guess = f", my guess {round(item.prediction * 100)}%" if item.prediction is not None else ""
            extra = f": {item.problem}" if item.problem and item.status in (UNVERIFIED, FAILED) else ""
            lines.append(f"#{item.id} {item.night} {item.title}: {item.status}{extra}{guess}{answer}")
        if not items:
            lines.append("Nothing prepared in that time.")
        return "\n".join(lines)

    def find(self, ref: str) -> Item | None:
        ref = ref.strip().lstrip("#")
        if ref.isdigit():
            return self.store.get(int(ref))
        wanted = ref.casefold()
        matches = [i for i in self.store.items if wanted and wanted in i.title.casefold()]
        return matches[-1] if matches else None

    # ------------------------------------------------------------------ state

    def _load_state(self) -> _State:
        raw = read_json(self.folder / "state.json", "the proactive state")
        if not isinstance(raw, dict):
            return _State()
        return _State(last_night=str(raw.get("last_night") or ""), last_run=float(raw.get("last_run") or 0))

    def _save_state(self) -> None:
        try:
            write_json(
                self.folder / "state.json", {"last_night": self._state.last_night, "last_run": self._state.last_run}
            )
        except OSError:
            log.warning("Couldn't save the proactive state", exc_info=True)
