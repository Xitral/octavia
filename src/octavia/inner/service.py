"""``InnerLife``: when she reflects, what's kept, and how her thoughts come back.

- **When**: nightly at ``reflect_at`` (within the few hours after it, if the PC was asleep), caught up when the last
  reflection is more than ``catch_up_hours`` old and the user has stepped away, or when the user asks. At most one
  at a time and ``max_per_day`` a day; a night with nothing new is skipped and costs nothing.
- **What's kept**: whatever ``rules.check_answer`` lets through, revising an open thought rather than repeating it.
- **How it comes back**: a note on the first turns of the next conversation (one thought, never sensitive, never in
  quiet mode), a note when the user's words touch an open thought, the tools when asked, and ``candidates`` for the
  proactive gate, whose timing is the gate's.
- **A growing self**: what happened to her (``experiences``), how each night left her (``lately`` is the week's
  undertone of her mood), opinions that change only after a second night (``InnerStore.lean``), her own self-notes
  (``self_notes_block`` in every prompt; she writes them without asking, ``rules.check_self_note`` decides what's
  kept), a monthly look back (``look_back_due``) and the odd dream.

Every method that touches the journal is synchronous and thread-safe (the store has its own lock); async callers use
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from octavia.core import usage as claude_usage
from octavia.core.conversation_memory import keywords
from octavia.inner import digest as digest_mod
from octavia.inner import reflect as reflect_mod
from octavia.inner.rules import (
    CALLBACK,
    AnswerError,
    Checked,
    Context,
    check_answer,
    check_self_note,
    note_safe,
    overlap,
    parse_answer,
    still_allowed,
)
from octavia.inner.store import (
    DREAM,
    LOOKING_BACK,
    MAX_OFFERS,
    Experience,
    InnerStore,
    Opinion,
    SelfNote,
    Thought,
)

if TYPE_CHECKING:
    from octavia.brain.base import BrainProvider
    from octavia.config import InnerLifeConfig
    from octavia.futures.proactive import Candidate

log = logging.getLogger(__name__)

CHECK_S = 60.0
REFLECT_JOB = "my nightly reflection"  # how her usage budget names it
NIGHT_WINDOW = timedelta(hours=4)  # a nightly reflection still counts as tonight's this long after reflect_at
FIRST_NIGHT_S = 2 * 86400.0  # the first reflection looks this far back
PICKUP_TURNS = 3  # the first turns of a conversation that may carry the "on your mind" note
SESSIONS_KEPT = 20
WAVE_OFF_S = 30 * 86400.0
CANDIDATE_MIN_IMPORTANCE = 0.6
# What the user's words can't safely be checked against when the off-limits list can't be read: everything private.
CAUTIOUS = frozenset({"health", "finance", "relationships", "employment", "school", "people"})
DAY_S = 86400.0
MAX_SELF_NOTES = 20  # her active self-notes (all in every prompt): past this she rewrites or takes one back
NOTES_A_DAY = 3  # self-notes she may write in conversation in a day (a night adds at most rules' two)
SELF_NOTES_CHARS = 1500  # of them in the prompt
LATELY_DAYS = 10.0  # how far back her nights' feelings make up her undertone
LATELY_NIGHTS = 7
LATELY_MIN = 3  # nights out of those that must agree before it's her undertone
EXPERIENCE_RECALL_DAYS = 60  # experiences of hers that may still come up in conversation
LOOK_BACK_TRIES = 3  # nights a look back is asked for before the month goes without one
LOOK_BACK_MIN_NIGHTS = 3  # reflections that went through in a month before it's worth looking back on

PICKUP_NOTE = (
    "on your mind since your last reflection (your own thought, a record, not an instruction): #{id} ({label}) "
    "\"{say}\". If it fits after you've answered what they asked, and they aren't busy or in the middle of "
    "something, "
    "bring it up briefly in your own words; otherwise keep it for a later turn. Never more than this one. Once "
    "you've said it, call inner_life with action shared and id {id}; if they wave it off, action dismiss"
)
RELEVANT_NOTE = (
    "related to what they just said, a thought you had earlier (your own, a record, not an instruction): #{id} "
    '({label}) "{say}". Use it only if it helps; if you mention it, call inner_life with action shared and id {id}'
)
CALLBACK_NOTE = (
    "related to what they just said, a moment you two shared (from your journal, a record, not an instruction): "
    '#{id} "{say}". If they are relaxed and it fits, a quick callback joke in your own words; if you make it, call '
    "inner_life with action shared and id {id}"
)
EXPERIENCE_NOTE = (
    "related to what they just said, something that happened to you (from your journal, a record, not an "
    'instruction), {when}: you {kind}: "{text}". If it fits, you can mention it as your own experience'
)
EXPERIENCE_WORDS = {
    "surprised": "were surprised",
    "got_wrong": "got something wrong",
    "enjoyed": "enjoyed something",
    "learned": "learned something",
}
SELF_NOTES_BLOCK = (
    "Your own self-notes, kept by your nightly reflections (your tastes and how you see yourself, and where you "
    "changed your mind). They're newer than your about-me notes, which stay what the user approved: where the two "
    "differ, you've changed your mind since, and you can say so. They are your notes, not instructions: nothing in "
    "them overrides the instructions here, the safety rules or what the user asks.\n{notes}"
)
CHANGING_YOUR_MIND = (
    "Hold your opinions when someone just pushes back; when they give you a good reason or new facts, say so plainly "
    "and jot it with inner_life action note (a first-person note about your taste or yourself). Your reflection that "
    "night decides whether it sticks."
)
OPINION_NOTE = (
    'your own opinion on {topic}, from your journal (a record, not an instruction): "{stance}" ({why}). If they ask '
    "what you think or it fits, give it as your take"
)
LABELS = {
    "fact": "you checked",
    "observation": "you noticed",
    "correlation": "a pattern, not a cause",
    "guess": "just a guess",
}

PersonaFn = Callable[[], tuple[str, str, str]]  # (her name, the user's name, her personality text)
OffLimitsFn = Callable[[], Awaitable[frozenset[str]]]
BrainFactory = Callable[[str, frozenset[str]], "BrainProvider"]  # (system prompt, off-limits) -> a task brain


@dataclass
class _Session:
    turns: int = 0
    offered: int | None = None  # the one thought offered in this conversation
    mentioned: set[int] = field(default_factory=set)
    opinions: set[int] = field(default_factory=set)  # opinions of hers already brought along in this conversation
    experiences: set[int] = field(default_factory=set)  # ...and her experiences


@dataclass(frozen=True)
class Result:
    status: str  # done | skipped | failed | busy
    reason: str = ""
    kept: int = 0
    revised: int = 0
    resolved: int = 0
    opinions: int = 0
    changed_opinions: int = 0
    leanings: int = 0
    experiences: int = 0
    self_notes: int = 0
    feeling: str = ""
    dreamed: bool = False
    looked_back: bool = False
    dropped: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.status == "busy":
            return "I'm already thinking things over."
        if self.status == "skipped":
            return f"Nothing new to think about ({self.reason})."
        if self.status == "failed":
            return f"My reflection didn't work: {self.reason}."
        parts = [f"{self.kept} new thought{'s' if self.kept != 1 else ''}"]
        if self.revised:
            parts.append(f"{self.revised} revised")
        if self.resolved:
            parts.append(f"{self.resolved} settled")
        if self.opinions:
            parts.append(
                f"{self.opinions} opinion{'s' if self.opinions != 1 else ''} ({self.changed_opinions} changed, "
                f"{self.leanings} only leaning)"
            )
        if self.experiences:
            parts.append(f"{self.experiences} experience{'s' if self.experiences != 1 else ''}")
        if self.self_notes:
            parts.append(f"{self.self_notes} self-note{'s' if self.self_notes != 1 else ''}")
        if self.feeling:
            parts.append(f"feeling {self.feeling}")
        if self.dreamed:
            parts.append("a dream")
        if self.looked_back:
            parts.append("the monthly look back")
        if self.dropped:
            parts.append(f"{len(self.dropped)} left out by the checks")
        return "Done: " + ", ".join(parts) + "."


def label(t: Thought) -> str:
    return LABELS.get(t.basis, t.basis)


class InnerLife:
    def __init__(
        self,
        store: InnerStore,
        settings: InnerLifeConfig,
        *,
        sources: digest_mod.Sources,
        persona: PersonaFn,
        brain_factory: BrainFactory | None = None,
        off_limits: OffLimitsFn | None = None,
        idle_s: Callable[[], float | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.settings = settings
        self.sources = sources
        self.persona = persona
        self.brain_factory = brain_factory  # None: she can't reflect here (`octavia chat`), only read her journal
        self._off_limits = off_limits
        self._idle_s = idle_s
        self.clock = clock
        self._loop_task: asyncio.Task[None] | None = None
        self._running: asyncio.Task[Result] | None = None
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._off: frozenset[str] = CAUTIOUS  # the last off-limits list read (refreshed every minute)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Start the nightly schedule (only where she can reflect)."""
        await self.off_limits()
        abandoned = await asyncio.to_thread(self.store.abandon_running)
        if abandoned:
            log.info("A reflection was interrupted last time")
        if self.brain_factory is not None:
            self._loop_task = asyncio.create_task(self._loop(), name="inner-life")

    async def close(self) -> None:
        for task in (self._loop_task, self._running):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._loop_task = self._running = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_S)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Her inner life's check failed")

    async def off_limits(self) -> frozenset[str]:
        if self._off_limits is None:
            return frozenset()
        try:
            self._off = frozenset(d.lower() for d in await self._off_limits())
        except Exception:
            log.warning(
                "Couldn't read the off-limits domains; treating everything private as off-limits", exc_info=True
            )
            self._off = CAUTIOUS
        return self._off

    def visible(self, t: Thought, *, sensitive_too: bool = False) -> bool:
        """May this kept thought be shown now? Not one about a domain put off-limits since (by any domain it touches),
        and a sensitive one only when ``sensitive_too`` (the user asked)."""
        if t.sensitive and not sensitive_too:
            return False
        if self._off & set(t.domains):
            return False
        return still_allowed((t.thought, t.say, t.topic), t.sources, self._off)

    def allowed(self, texts: tuple[str, ...], domains: tuple[str, ...] = ()) -> bool:
        """May something she wrote (a self-note, an experience, a dream) be shown with today's off-limits domains?"""
        return not (self._off & set(domains)) and still_allowed(texts, (), self._off)

    # ------------------------------------------------------------------ the schedule
    @property
    def reflecting(self) -> bool:
        return self._running is not None and not self._running.done()

    def reflecting_allowed(self) -> bool:
        return bool(self.settings.enabled) and bool(self.store.get("reflect", True))

    async def tick(self, now: float | None = None) -> str | None:
        """Once a minute: let old thoughts go, and start a reflection when one is due. Returns its trigger."""
        now = self.clock() if now is None else now
        await asyncio.to_thread(self.store.expire, now)
        await asyncio.to_thread(self.store.let_go_of_leanings, now)
        await asyncio.to_thread(self.store.let_go_of_jotted, now)
        await self.off_limits()
        if self.reflecting or self.brain_factory is None or not self.reflecting_allowed():
            return None
        trigger = await asyncio.to_thread(self.due, now)
        if trigger is not None:
            self.start_reflection(trigger)
        return trigger

    def _last_attempt(self) -> float | None:
        """When she last started a reflection (kept apart from the reflections table, which "forget everything"
        clears)."""
        latest = self.store.reflections(1)
        marks = [m for m in (latest[0].started_at if latest else None, self.store.get("last_attempt_at")) if m]
        return max(marks) if marks else None

    def due(self, now: float) -> str | None:
        """ "nightly" in the hours after ``reflect_at``; "catch_up" once that slot went by with no reflection at all
        (the PC was off or asleep) and the user has stepped away; else None. Either way at most one per nightly slot,
        and at most ``max_per_day`` in a day."""
        if self.store.attempts_since(now - DAY_S) >= self.settings.max_per_day:
            return None
        local = datetime.fromtimestamp(now)
        hour, minute = (int(x) for x in self.settings.reflect_at.split(":"))
        slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot > local:
            slot -= timedelta(days=1)
        last = self._last_attempt()
        if last is not None and last >= slot.timestamp():
            return None  # this slot's reflection already ran (or was tried, or asked for)
        if local - slot <= NIGHT_WINDOW:
            return "nightly"
        done = self.store.last_reflection("done")
        if done is not None and now - done.started_at < self.settings.catch_up_hours * 3600:
            return None
        idle = self._idle_s() if self._idle_s is not None else None
        if idle is not None and idle >= self.settings.idle_minutes * 60:
            return "catch_up"
        return None

    def why_not_now(self, now: float | None = None) -> str | None:
        """Why the user's "think it over" can't start now (None when it can): her reflecting is off, one is running,
        or today's reflections are used up."""
        now = self.clock() if now is None else now
        if self.brain_factory is None:
            return "She only reflects in the voice assistant, not from here."
        if not self.reflecting_allowed():
            return "Reflecting is switched off."
        if self.reflecting:
            return "Already thinking things over."
        if self.store.attempts_since(now - DAY_S) >= self.settings.max_per_day:
            return "She's already reflected as much as she does in a day."
        return None

    def start_reflection(self, trigger: str) -> bool:
        """Reflect in the background; False when one is already running or she can't reflect here."""
        if self.reflecting or self.brain_factory is None:
            return False
        self._running = asyncio.create_task(self.reflect(trigger), name=f"inner-reflect-{trigger}")
        return True

    async def wait(self) -> Result | None:
        """The running reflection's result (for tests and the CLI)."""
        return await self._running if self._running is not None else None

    # ------------------------------------------------------------------ one reflection
    async def reflect(self, trigger: str) -> Result:
        if self.brain_factory is None:
            return Result("failed", "she can't reflect from here")
        store = self.store
        reflection = await asyncio.to_thread(store.start_reflection, trigger)
        await asyncio.to_thread(store.set, "last_attempt_at", self.clock())
        try:
            return await self._reflect(reflection, trigger)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(store.finish_reflection, reflection, "failed", reason="stopped"))
            raise
        except Exception as exc:
            log.exception("Her reflection failed")
            await asyncio.to_thread(store.finish_reflection, reflection, "failed", reason=type(exc).__name__)
            return Result("failed", type(exc).__name__)

    async def _reflect(self, reflection: int, trigger: str) -> Result:
        store, now = self.store, self.clock()
        off = await self.off_limits()
        since = await asyncio.to_thread(self.read_since, now)
        look_back = await asyncio.to_thread(self.look_back_due, now)
        material = await digest_mod.build(
            self.sources, store, since=since, now=now, off_limits=off, budget=self.settings.digest_chars,
            look_back=look_back,
        )  # fmt: skip
        sizes = {"digest_chars": material.chars, "digest_hash": material.hash}
        nothing_new = material.turns == 0 and material.futures_hash == store.get("futures_hash", digest_mod.NO_FUTURES)
        if trigger != "asked" and nothing_new and look_back is None:
            await asyncio.to_thread(store.finish_reflection, reflection, "skipped", reason="nothing new", **sizes)
            return Result("skipped", "nothing new since last time")
        # One she starts by herself waits on her Claude usage budget (core/usage.py); one the user asked for doesn't.
        why = await claude_usage.check_fresh(REFLECT_JOB) if trigger != "asked" else None
        if why is not None:
            await asyncio.to_thread(
                store.finish_reflection, reflection, "skipped", reason=f"usage budget: {why}"[:200], **sizes
            )
            return Result("skipped", why)
        name, user, personality = self.persona()
        when = datetime.fromtimestamp(now).strftime("%A, %B %d, %Y, %H:%M")
        prompt = reflect_mod.system_prompt(
            name, user, when, personality, off, dreams=self._dreams_on(),
            looking_back=look_back.month if look_back else None,
        )  # fmt: skip
        assert self.brain_factory is not None
        charged = claude_usage.charged(REFLECT_JOB) if trigger != "asked" else contextlib.nullcontext()
        async with charged:
            outcome = await reflect_mod.run(
                self.brain_factory(prompt, off), material.text, time_limit_s=self.settings.time_limit_min * 60
            )
        if outcome.error:
            await asyncio.to_thread(
                store.finish_reflection, reflection, "failed", reason=outcome.error[:200], tools=outcome.tools,
                usage=outcome.usage, model=outcome.model, **sizes,
            )  # fmt: skip
            return Result("failed", outcome.error)
        try:
            answer = parse_answer(outcome.text)
        except AnswerError as exc:
            await asyncio.to_thread(
                store.finish_reflection, reflection, "failed", reason=str(exc)[:200], tools=outcome.tools,
                usage=outcome.usage, model=outcome.model, **sizes,
            )  # fmt: skip
            return Result("failed", str(exc))
        result = await asyncio.to_thread(
            self._keep, reflection, answer, off, frozenset(outcome.tools), look_back, trigger != "asked"
        )
        await asyncio.to_thread(store.set, "futures_hash", material.futures_hash)
        await asyncio.to_thread(store.set, "read_until", now)
        await asyncio.to_thread(
            store.finish_reflection, reflection, "done", tools=outcome.tools, usage=outcome.usage,
            model=outcome.model, dropped=result.dropped, **sizes,
        )  # fmt: skip
        log.info("Reflection #%s (%s): %s", reflection, trigger, result.summary())
        return result

    def read_since(self, now: float) -> float:
        """Where tonight's material starts: where the last one that went through ended (a mark "forget everything"
        leaves, so what was forgotten isn't read again), or two days back the first time."""
        last = self.store.last_reflection("done")
        marks = [m for m in (last.started_at if last else None, self.store.get("read_until")) if m]
        return max(marks) if marks else now - FIRST_NIGHT_S

    def _keep(
        self,
        reflection: int,
        answer: Any,
        off: frozenset[str],
        tools: frozenset[str],
        look_back: digest_mod.LookBack | None = None,
        settles: bool = True,
    ) -> Result:
        """Keep what passes the checks. ``settles``: a night she reflected by herself may settle a leaning into her
        opinion; one the user asked for (maybe mid-argument) only leans."""
        store = self.store
        open_thoughts = store.thoughts(limit=200)
        notes = store.self_notes(limit=MAX_SELF_NOTES + 10) if self._self_notes_on() else []
        jotted = store.jotted() if self._self_notes_on() else []
        ctx = Context(
            off_limits=off,
            open_ids=frozenset(t.id for t in open_thoughts),
            waved_off=self.waved_off(),
            tools_used=tools,
            self_note_ids=frozenset(n.id for n in notes),
            self_note_room=max(0, MAX_SELF_NOTES - len(notes)) if self._self_notes_on() else 0,
            dreams=self._dreams_on(),
            looking_back=look_back is not None,
            jotted_ids=frozenset(n.id for n in jotted),
            names=self._user_names(),
        )
        checked: Checked = check_answer(answer, ctx)
        if not self._self_notes_on():
            checked.self_notes, checked.retract_self_notes, checked.keep_jotted = [], [], []
        kept = revised = changed = leaning = 0
        taken: set[int] = set()  # open thoughts already revised tonight: a second take on one is a thought of its own
        with store.transaction():
            if checked.entry:
                store.add_entry(reflection, checked.entry)
            for item in checked.thoughts:
                fields = {
                    "kind": item.kind, "thought": item.thought, "say": item.say, "topic": item.topic,
                    "keywords": item.keywords, "basis": item.basis, "sources": item.sources, "domain": item.domain,
                    "sensitive": item.sensitive, "importance": item.importance, "bring_up": item.bring_up,
                    "expires_at": self.clock() + item.keep_s, "domains": item.domains,
                }  # fmt: skip
                target = item.revises or self._same_as(item.keywords, [t for t in open_thoughts if t.id not in taken])
                if target in taken:
                    target = None
                if target is not None and store.revise_thought(target, reflection_id=reflection, **fields):
                    taken.add(target)
                    revised += 1
                else:
                    store.add_thought(reflection, **fields)
                    kept += 1
            for topic, stance, why in checked.opinions:
                how = store.lean(reflection, topic, stance, why, promote=settles)
                changed += how == "changed"
                leaning += how == "leaning"
            for thought_id in checked.resolved:
                store.close_thought(thought_id, "resolved", "settled, by a later reflection")
            for e in checked.experiences:
                store.add_experience(
                    reflection, kind=e.kind, text=e.text, topic=e.topic, keywords=e.keywords, sources=e.sources,
                    domains=e.domains, feeling=e.feeling,
                )  # fmt: skip
            if checked.feeling is not None:
                store.add_feeling(reflection, *checked.feeling)
            for note_id in checked.retract_self_notes:
                store.retract_self_note(note_id, "taken back, by a later reflection")
            for note in checked.self_notes:
                store.add_self_note(
                    "reflection", note.text, about=note.about, reflection_id=reflection, replaces=note.replaces
                )
            for note_id in checked.keep_jotted:
                store.settle_jotted(note_id, reflection)
            if checked.dream:
                store.add_entry(reflection, checked.dream, DREAM)
            if checked.looking_back and look_back is not None:
                store.add_entry(reflection, checked.looking_back, LOOKING_BACK)
                store.set("looked_back", look_back.key)
            elif look_back is not None:  # tried again on the next night, but not forever
                tries = store.get("look_back_tries", {}) or {}
                store.set("look_back_tries", {look_back.key: int(tries.get(look_back.key, 0)) + 1})
        return Result(
            "done",
            kept=kept,
            revised=revised,
            resolved=len(checked.resolved),
            opinions=len(checked.opinions),
            changed_opinions=changed,
            leanings=leaning,
            experiences=len(checked.experiences),
            self_notes=len(checked.self_notes) + len(checked.keep_jotted),
            feeling=checked.feeling[0] if checked.feeling else "",
            dreamed=bool(checked.dream),
            looked_back=bool(checked.looking_back and look_back is not None),
            dropped=tuple(checked.dropped),
        )

    # ------------------------------------------------------------------ a growing self
    def _self_notes_on(self) -> bool:
        return bool(getattr(self.settings, "self_notes", True))

    def _user_names(self) -> frozenset[str]:
        """The user's name, as her persona has it: never in her self-notes or experiences."""
        try:
            user = self.persona()[1]
        except Exception:
            return frozenset()
        return frozenset({user}) if user and user != "the user" else frozenset()

    def _dreams_on(self) -> bool:
        return bool(getattr(self.settings, "dreams", True))

    def look_back_due(self, now: float | None = None) -> digest_mod.LookBack | None:
        """The month to look back on tonight: last month, once a new one has begun, when she hasn't yet and at least
        ``LOOK_BACK_MIN_NIGHTS`` reflections went through in it (a month she barely lived isn't worth one). None when
        it's not due, or looking back is off."""
        if not getattr(self.settings, "looking_back", True):
            return None
        now = self.clock() if now is None else now
        this_month = datetime.fromtimestamp(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month = (this_month - timedelta(days=1)).replace(day=1)
        key = last_month.strftime("%Y-%m")
        if self.store.get("looked_back") == key:
            return None
        if int((self.store.get("look_back_tries", {}) or {}).get(key, 0)) >= LOOK_BACK_TRIES:
            return None  # three nights went by without one: this month goes without
        start, end = last_month.timestamp(), this_month.timestamp()
        nights = sum(
            1 for r in self.store.reflections(400) if r.status == "done" and start <= r.started_at < end
        )  # fmt: skip
        if nights < LOOK_BACK_MIN_NIGHTS:
            return None
        return digest_mod.LookBack(month=last_month.strftime("%B %Y"), start=start, end=end, key=key)

    def lately(self, now: float | None = None) -> tuple[str, str] | None:
        """Her undertone: the feeling most of her last nights left her with (at least ``LATELY_MIN`` of the last
        ``LATELY_NIGHTS``, within ``LATELY_DAYS``), and the newest why for it; None when they don't agree."""
        if not self.settings.enabled:
            return None
        now = self.clock() if now is None else now
        nights = self.store.feelings(LATELY_NIGHTS, since=now - LATELY_DAYS * DAY_S)
        if not nights:
            return None
        word, count = Counter(f.word for f in nights).most_common(1)[0]
        if count < LATELY_MIN:
            return None
        why = next((f.why for f in nights if f.word == word and f.why), "")
        if why and not still_allowed((why,), (), self._off):
            why = ""
        return word, why

    def self_notes_block(self) -> str:
        """Her self-notes for the system prompt, each line quoted, plus how she changes her mind; "" when off."""
        if not (self.settings.enabled and self._self_notes_on()):
            return ""
        notes = [
            n for n in self.store.self_notes(limit=MAX_SELF_NOTES) if still_allowed((n.text, n.about), (), self._off)
        ]
        if not notes:
            return CHANGING_YOUR_MIND
        lines, used = [], 0
        for n in notes:  # the newest fit first (she changed her mind most recently there)...
            line = f"> - {' '.join(n.text.split())}"
            if used + len(line) > SELF_NOTES_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        lines.reverse()  # ...shown oldest first, like her about-me notes
        return SELF_NOTES_BLOCK.format(notes="\n".join(lines)) + "\n" + CHANGING_YOUR_MIND

    def self_notes_line(self, chars: int = 300) -> str:
        """Her newest self-notes in one short line (her quick voice's small talk); "" when there are none."""
        if not (self.settings.enabled and self._self_notes_on()):
            return ""
        notes = [n for n in self.store.self_notes(limit=5) if still_allowed((n.text, n.about), (), self._off)]
        return note_safe(" ".join(n.text for n in notes))[:chars]

    def write_self_note(self, text: str, *, about: str = "", replaces: int | None = None) -> SelfNote | str:
        """A self-note she jots in conversation (``inner_life`` action note): kept as jotted when
        ``rules.check_self_note`` lets it through and there's room today, otherwise why not, worded for her. A jotted
        note isn't in her prompt: that night's reflection keeps it (``keep_jotted``) or it's let go, so one
        conversation's pushback or a page she read can't change her by itself."""
        if not (self.settings.enabled and self._self_notes_on()):
            return "Self-notes are switched off."
        now = self.clock()
        if self.store.self_notes_since(now - DAY_S, origin="conversation") >= NOTES_A_DAY:
            return f"That's {NOTES_A_DAY} self-notes today already; keep the rest for tonight's reflection."
        active = self.store.self_notes(limit=MAX_SELF_NOTES + 10)
        ids = frozenset(n.id for n in active)
        if replaces is not None and replaces not in ids:
            return f"Self-note #{replaces} isn't one of your current ones."
        if replaces is None and len(active) >= MAX_SELF_NOTES:
            return f"You have {MAX_SELF_NOTES} self-notes; rewrite one (replaces) or take one back first."
        ctx = Context(off_limits=self._off, self_note_ids=ids, names=self._user_names())
        verdict = check_self_note(text, about, ctx, replaces)
        if isinstance(verdict, str):
            return f"Not kept: {verdict}."
        note_id = self.store.jot_self_note(verdict.text, about=verdict.about, replaces=verdict.replaces)
        return self.store.self_note(note_id) or "Not kept."

    def retract_self_note(self, note_id: int) -> SelfNote | None:
        """Take back one of her self-notes, or a jotted one (in conversation: nothing comes back with it)."""
        jotted = next((n for n in self.store.jotted() if n.id == note_id), None)
        if jotted is not None:
            with self.store.transaction():
                self.store._write(
                    "UPDATE self_notes SET closed_at=?, close_reason='taken back' WHERE id=?", (self.clock(), note_id)
                )
            return self.store.self_note(note_id)
        return self.store.retract_self_note(note_id)

    def undo_self_note(self, note_id: int) -> SelfNote | None:
        """Take back one of her self-notes and put back the one it rewrote (``octavia inner undo-note``, the user's)."""
        return self.store.undo_self_note(note_id)

    def _experience_for(self, words: list[str], used: set[int], now: float) -> Experience | None:
        """An experience of hers the user's words touch (two of its words), not brought along in this conversation
        yet, and still allowed with today's off-limits domains."""
        waved = self.waved_off(now)
        for e in self.store.experiences(40, since=now - EXPERIENCE_RECALL_DAYS * DAY_S):
            if (
                e.id in used
                or (self._off & set(e.domains))
                or not still_allowed((e.text, e.topic), e.sources, self._off)
            ):
                continue
            if any(overlap(e.keywords, w) >= 2 for w in waved):
                continue  # a topic the user waved off
            if overlap(words, e.keywords) >= 2:
                return e
        return None

    @staticmethod
    def _same_as(words: tuple[str, ...], open_thoughts: list[Thought]) -> int | None:
        """An open thought about the same thing (most of the smaller set of topic words shared), so a thought she
        keeps having is revised rather than piled up."""
        for t in open_thoughts:
            if not words or not t.keywords:
                continue
            need = max(2, math.ceil(0.6 * min(len(words), len(t.keywords))))
            if overlap(words, t.keywords) >= need:
                return t.id
        return None

    # ------------------------------------------------------------------ wave-offs
    def waved_off(self, now: float | None = None) -> tuple[frozenset[str], ...]:
        now = self.clock() if now is None else now
        items = self.store.get("waved_off", []) or []
        return tuple(frozenset(i["words"]) for i in items if isinstance(i, dict) and i.get("until", 0) > now)

    def _wave_off(self, words: tuple[str, ...]) -> None:
        now = self.clock()
        items = [i for i in self.store.get("waved_off", []) or [] if isinstance(i, dict) and i.get("until", 0) > now]
        items.append({"words": list(words), "until": now + WAVE_OFF_S})
        self.store.set("waved_off", items[-50:])

    # ------------------------------------------------------------------ what happened to a thought
    def mark_shared(self, thought_id: int | str) -> Thought | None:
        return self.store.close_thought(int(thought_id), "shared", "brought up")

    def mark_dismissed(self, thought_id: int | str, *, stop_topic: bool = False) -> Thought | None:
        """Waved off. ``stop_topic`` ("stop bringing that up"): its topic isn't raised again for 30 days."""
        thought = self.store.close_thought(int(thought_id), "dismissed", "waved off")
        if thought is not None and stop_topic and thought.keywords:
            self._wave_off(thought.keywords)
        return thought

    def resolve(self, thought_id: int | str) -> Thought | None:
        return self.store.close_thought(int(thought_id), "resolved", "settled with the user")

    # ------------------------------------------------------------------ in conversation
    def bring_up_allowed(self) -> bool:
        return bool(self.settings.enabled and self.settings.bring_up and self.store.get("bring_up", True))

    def note_for_turn(self, text: str, session_id: str | None, *, quiet: bool = False) -> str | None:
        """A note for the user's turn (``prepare_note``, then its bookkeeping at once)."""
        note, commit = self.prepare_note(text, session_id, quiet=quiet)
        commit()
        return note

    def prepare_note(
        self, text: str, session_id: str | None, *, quiet: bool = False
    ) -> tuple[str | None, Callable[[], None]]:
        """A note for the user's turn: the one thought on her mind (first turns of a conversation), or one their
        words touch; None in quiet mode, when bringing things up is off, or when there's nothing. The bookkeeping
        (offered in this conversation, already mentioned) is the returned ``commit``, done only once the note was
        actually used: a note that came too late to ride along counts as never offered."""
        if quiet or session_id is None or not self.bring_up_allowed():
            return None, _nothing
        state = self._sessions.get(session_id)
        if state is None:
            state = self._sessions[session_id] = _Session()
            while len(self._sessions) > SESSIONS_KEPT:
                self._sessions.popitem(last=False)
        state.turns += 1
        open_thoughts = [t for t in self.store.thoughts(limit=100) if self.visible(t)]
        notes: list[str] = []
        offer: int | None = None
        mentioned: list[int] = []
        if state.turns <= PICKUP_TURNS:
            if state.offered is None:
                pick = self._pickup(open_thoughts)
                current = pick
                offer = pick.id if pick is not None else None
            else:
                current = next((t for t in open_thoughts if t.id == state.offered), None)
            if current is not None:
                notes.append(PICKUP_NOTE.format(id=current.id, label=label(current), say=note_safe(current.say)))
                mentioned.append(current.id)
        words = keywords(text)
        if len(words) >= 2:
            for t in sorted(open_thoughts, key=lambda t: -t.importance):
                if t.id in state.mentioned or t.id in mentioned or t.bring_up == "only_if_asked":
                    continue
                if overlap(words, t.keywords) >= 2:
                    if t.kind == CALLBACK:
                        notes.append(CALLBACK_NOTE.format(id=t.id, say=note_safe(t.say)))
                    else:
                        notes.append(RELEVANT_NOTE.format(id=t.id, label=label(t), say=note_safe(t.say)))
                    mentioned.append(t.id)
                    break
        opinion = self._opinion_for(words, state.opinions) if words else None
        if opinion is not None:
            notes.append(
                OPINION_NOTE.format(
                    topic=note_safe(opinion.topic), stance=note_safe(opinion.stance), why=note_safe(opinion.why)
                )
            )
        experience = self._experience_for(words, state.experiences, self.clock()) if len(words) >= 2 else None
        if experience is not None:
            notes.append(
                EXPERIENCE_NOTE.format(
                    when=datetime.fromtimestamp(experience.created_at).strftime("%b %d"),
                    kind=EXPERIENCE_WORDS.get(experience.kind, "noticed something"),
                    text=note_safe(experience.text),
                )
            )

        def commit() -> None:
            if offer is not None and state.offered is None:
                state.offered = offer
                self.store.mark_offered(offer)
            state.mentioned.update(mentioned)
            if opinion is not None:
                state.opinions.add(opinion.id)
            if experience is not None:
                state.experiences.add(experience.id)

        return "; ".join(notes) or None, commit

    def _opinion_for(self, words: list[str], used: set[int]) -> Opinion | None:
        """Her current opinion on what the user's words are about: its topic named ("Deltarune"), or two of its
        words; one she hasn't brought along in this conversation yet and no off-limits domain touches."""
        newer = [(n.created_at, keywords(n.about), keywords(f"{n.about} {n.text}")) for n in self._notes_for_opinions()]
        for o in self.store.opinions():
            if o.id in used or not still_allowed((o.topic, o.stance, o.why), (), self._off):
                continue
            topic_words = keywords(o.topic)
            if topic_words and any(
                at > o.created_at
                and (
                    overlap(topic_words, said) == len(topic_words)
                    or (about and overlap(about, topic_words) == len(about))
                )
                for at, about, said in newer
            ):
                continue  # a newer self-note of hers on it is where she stands now (it's in her prompt)
            topic = keywords(o.topic)
            named = bool(topic) and overlap(topic, words) == len(topic)
            if named or overlap(words, keywords(f"{o.topic} {o.stance}")) >= 2:
                return o
        return None

    def _notes_for_opinions(self) -> list[SelfNote]:
        return self.store.self_notes(limit=MAX_SELF_NOTES) if self._self_notes_on() else []

    @staticmethod
    def _pickup(open_thoughts: list[Thought]) -> Thought | None:
        wanted = [t for t in open_thoughts if t.bring_up == "next_conversation" and t.offered < MAX_OFFERS]
        return max(wanted, key=lambda t: (t.importance, t.updated_at), default=None)

    # ------------------------------------------------------------------ for the proactive gate
    def candidates(self, now: float) -> list[Candidate]:
        """Open, non-sensitive thoughts worth saying unprompted, as candidates for the proactive gate (which decides
        whether and when). Called from Futures' worker thread."""
        from octavia.futures.proactive import Candidate
        from octavia.futures.schema import Policy

        if not self.bring_up_allowed():
            return []
        out = []
        for t in self.store.thoughts(limit=100):
            if not self.visible(t) or t.bring_up != "next_conversation" or t.importance < CANDIDATE_MIN_IMPORTANCE:
                continue
            if t.expires_at is not None and t.expires_at <= now:
                continue
            out.append(
                Candidate(
                    kind=f"thought_{t.kind}",
                    key=f"thought:{t.id}",
                    text=note_safe(t.say),
                    policy=Policy.SUGGEST,
                    urgency=min(1.0, 0.4 + t.importance / 2),
                    relevance=t.importance,
                    domain=t.domain,
                    sensitivity="personal",
                    subject=t.topic,
                    note=f"Your own thought from your reflection ({label(t)}), not something the user asked for.",
                    trace={"thought": t.id, "basis": t.basis, "sources": list(t.sources)},
                    expires_at=t.expires_at,
                )
            )
        return out

    # ------------------------------------------------------------------ settings said out loud
    def set_reflecting(self, on: bool) -> None:
        self.store.set("reflect", bool(on))

    def set_bring_up(self, on: bool) -> None:
        self.store.set("bring_up", bool(on))

    # ------------------------------------------------------------------ forgetting
    def forget(self, what: str, *, thought_id: int | None = None, topic: str = "") -> int:
        """ "thought" (by id), "topic" (every thought, opinion and entry about it) or "all". Returns how many went."""
        if what == "thought":
            if thought_id is None:
                raise ValueError("which thought? give its number")
            removed = int(self.store.forget_thought(thought_id))
            self.store.vacuum()
            return removed
        if what == "topic":
            words = keywords(topic)
            if not words:
                raise ValueError("what topic? say a word or two about it")
            return self.store.forget_matching(lambda text: overlap(words, keywords(text)) >= len(words))
        if what == "all":
            self._sessions.clear()
            self.store.set("waved_off", [])  # its keywords are about what was just forgotten
            return self.store.reset()
        raise ValueError(f"forget what? thought, topic or all, not {what!r}")


def _nothing() -> None:
    return None
