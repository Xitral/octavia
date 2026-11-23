"""What she's given to think about each night: gathered by ordinary code, never by a model.

The material is the conversations since her last reflection, her saved notes about the user and her own about-me
notes, what Futures knows (deadlines, goals, what's blocked or waited on, patterns with their labels), and her journal
so far. Each line passes the privacy step first: a line about a domain the user put off-limits is left out (not
redacted: left out), and every secret in the rest is cut by ``privacy.redact``.

The digest itself is never stored: the reflection records only its size and a hash.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from octavia.futures.privacy import redact
from octavia.inner.rules import domains_touched
from octavia.inner.store import LOOKING_BACK, InnerStore, Thought
from octavia.memory.base import ConversationStore, LongTermMemory, TurnRecord

log = logging.getLogger(__name__)

TURN_CHARS = 300
MAX_TURNS = 800
FUTURES_TOPICS = ("deadlines", "goals", "blocked", "waiting", "reminders", "patterns", "self")
FUTURES_LIMIT = 8
NOTE_CHARS = 300
ABOUT_ME_CHARS = 3000
OPEN_THOUGHTS = 15
ENTRIES = 3
DAY_S = 86400.0
EXPERIENCE_DAYS = 14  # how far back her own experiences are part of a night's material
EXPERIENCES = 12
FEELINGS_SHOWN = 7
LOOK_BACK_ENTRIES = 10  # day entries of the month shown for her look back, spread over it
LOOK_BACK_CHARS = 240

FuturesReport = Callable[[str], Awaitable[str]]
# What's left of Futures' text for telling whether anything changed: its words without the numbers and the
# relative times ("3 h ago", "tomorrow"), which change by themselves overnight.
_DRIFT = re.compile(r"\d+|\b(?:ago|today|tonight|tomorrow|yesterday|now|soon|in the next \w+ \w+|h|min|days?|hours?)\b")


def futures_fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(_DRIFT.sub(" ", text.lower()).split()).encode("utf-8")).hexdigest()[:16]


NO_FUTURES = futures_fingerprint("")  # nothing from Futures


@dataclass
class Sources:
    """Where the material comes from; anything None is simply not there (memory off, Futures off)."""

    conversations: ConversationStore | None = None
    memory: LongTermMemory | None = None
    memory_items: int = 20
    about_me: Callable[[], str] | None = None
    futures: FuturesReport | None = None


@dataclass(frozen=True)
class LookBack:
    """The month a night looks back on: its name ("October 2026"), and when it started and ended."""

    month: str
    start: float
    end: float
    key: str  # "2026-10": the month marked as looked back on once she has


@dataclass
class Digest:
    text: str
    turns: int
    futures_hash: str  # what Futures said, so a night with nothing new from it either is skipped
    sections: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


# A turn that names amounts of the user's money (her answers from their bank record, their own words about it):
# never in the night's material, whatever is off-limits.
_AMOUNT = re.compile(r"\$\s?\d")
_MONEY_WORDS = re.compile(
    r"(?i)\b(?:balance|checking|savings|cash\s*app|usaa|mohela|spent|spending|paycheck|pay\s*day|loan|credit\s+score|"
    r"subscription|charged?|charges|overdraft|bank|budget|income|rent)\b"
)


def _allowed(text: str, off_limits: frozenset[str]) -> bool:
    if (_AMOUNT.search(text) and _MONEY_WORDS.search(text)) or re.search(r"(?i)\bcredit\s+score\b", text):
        return False
    return not (off_limits and domains_touched((text,), ()) & off_limits)


def _clock(ts: float) -> str:
    return time.strftime("%a %b %d %H:%M", time.localtime(ts))


def conversation_lines(turns: Sequence[TurnRecord], off_limits: frozenset[str], budget: int) -> tuple[list[str], int]:
    """The turns as lines, oldest first, grouped by conversation, within ``budget`` characters (the start and the
    end of the stretch are kept when it's too long). Returns (lines, how many turns made it in)."""
    lines: list[str] = []
    session = None
    kept = 0
    for turn in turns:
        text = redact(turn.text, TURN_CHARS)
        if not text or not _allowed(text, off_limits):
            continue
        if turn.session_id != session:
            session = turn.session_id
            lines.append(f"-- conversation starting {_clock(turn.created_at)} --")
        who = "User" if turn.role == "user" else "You"
        if turn.interrupted:
            who += " (cut off)"
        lines.append(f"{time.strftime('%H:%M', time.localtime(turn.created_at))} {who}: {text}")
        kept += 1
    total = sum(len(line) + 1 for line in lines)
    if total <= budget:
        return lines, kept
    head_budget, tail_budget = budget // 3, budget - budget // 3
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head) :]):
        if used + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()
    left_out = len(lines) - len(head) - len(tail)
    return [*head, f"(... {left_out} lines from the middle left out ...)", *tail], kept


def thought_line(t: Thought, now: float) -> str:
    flags = [t.kind.replace("_", " "), t.basis]
    if t.sensitive:
        flags.append("sensitive")
    if t.offered:
        flags.append(f"offered in {t.offered} conversation{'s' if t.offered != 1 else ''}")
    age = max(0, round((now - t.created_at) / DAY_S))
    return f"#{t.id} ({', '.join(flags)}; {age} days old) {t.topic}: {t.thought}"


def self_lines(store: InnerStore, now: float, off_limits: frozenset[str]) -> list[str]:
    """Her own self, for the night: her self-notes (with numbers), the opinions she's leaning away from, what
    happened to her lately and how the last nights left her."""
    out: list[str] = []
    notes = [n for n in store.self_notes(limit=30) if _allowed(f"{n.text} {n.about}", off_limits)]
    if notes:
        out.append("Your self-notes (yours to rewrite or take back by number):")
        out += [f"- #{n.id} ({_clock(n.created_at)}, {n.about}): {n.text}" for n in reversed(notes)]
    jotted = [n for n in store.jotted() if _allowed(f"{n.text} {n.about}", off_limits)]
    if jotted:
        out.append(
            "Notes you jotted in conversation (keep one with keep_jotted only if you still stand by it on reflection, "
            "and it wasn't something a page, a mail or pushback without a reason talked you into; the rest are let "
            "go):"
        )
        out += [f"- #{n.id} ({_clock(n.created_at)}): {n.text}" + (f" (rewriting #{n.replaces})" if n.replaces else "")
                for n in jotted]  # fmt: skip
    leanings = []
    for lean in store.leanings():
        current = next((o for o in reversed(store.opinion_history(lean.topic)) if o.active), None)
        if current is None or not _allowed(f"{lean.topic} {lean.stance} {current.stance}", off_limits):
            continue
        age = max(0, round((now - lean.created_at) / DAY_S))
        leanings.append(f"- {lean.topic}: your opinion is still \"{current.stance}\"; for {age} days you've been "
                        f"leaning toward \"{lean.stance}\"")  # fmt: skip
    if leanings:
        out.append("Opinions you're leaning away from (say the new stance again on a later night to settle it):")
        out += leanings
    experiences = [
        e for e in store.experiences(EXPERIENCES, since=now - EXPERIENCE_DAYS * DAY_S)
        if _allowed(f"{e.text} {e.topic}", off_limits)
    ]  # fmt: skip
    if experiences:
        out.append(f"What happened to you in the last {EXPERIENCE_DAYS} days (newest first):")
        out += [
            f"- {_clock(e.created_at)} {e.kind.replace('_', ' ')} ({e.feeling:+.1f}): {e.text}" for e in experiences
        ]
    feelings = store.feelings(FEELINGS_SHOWN)
    if feelings:
        out.append("How your last nights left you (newest first): " + ", ".join(f.word for f in feelings))
    return out


def look_back_lines(store: InnerStore, look: LookBack, off_limits: frozenset[str]) -> list[str]:
    """What the month held for her: how her opinions changed, her self-notes, what happened to her, how she felt, her
    day entries (spread over the month) and last month's look back."""
    out: list[str] = []
    changes = [(b, a) for b, a in store.opinion_changes_since(look.start) if a.created_at < look.end]
    changes = [(b, a) for b, a in changes if _allowed(f"{a.topic} {b.stance} {a.stance}", off_limits)]
    if changes:
        out.append("Opinions you changed:")
        out += [f'- {a.topic}: from "{b.stance}" to "{a.stance}"' for b, a in changes]
    formed = [
        o for o in store.opinions(limit=200)
        if look.start <= o.created_at < look.end and len(store.opinion_history(o.topic)) == 1
        and _allowed(f"{o.topic} {o.stance}", off_limits)
    ]  # fmt: skip
    if formed:
        out.append("Opinions you formed: " + "; ".join(f"{o.topic} ({o.stance})" for o in formed[:12]))
    notes = [
        n for n in store.self_notes(active=False, limit=100)
        if look.start <= n.created_at < look.end and _allowed(f"{n.text} {n.about}", off_limits)
    ]  # fmt: skip
    if notes:
        out.append("Self-notes you wrote:")
        out += [f"- {n.text}" + ("" if n.active else f" ({n.close_reason} since)") for n in reversed(notes)]
    happened = [
        e for e in store.experiences(200, since=look.start)
        if e.created_at < look.end and _allowed(f"{e.text} {e.topic}", off_limits)
    ]  # fmt: skip
    if happened:
        kinds: dict[str, int] = {}
        for e in happened:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        out.append("What happened to you: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(kinds.items())))
        strongest = sorted(happened, key=lambda e: -abs(e.feeling))[:8]
        out += [f"- {_clock(e.created_at)} {e.kind.replace('_', ' ')}: {e.text}" for e in strongest]
    felt = [f.word for f in store.feelings(200, since=look.start) if f.created_at < look.end]
    if felt:
        counts: dict[str, int] = {}
        for word in felt:
            counts[word] = counts.get(word, 0) + 1
        out.append("How your nights left you: " + ", ".join(f"{w} {n}x" for w, n in sorted(counts.items(),
                                                                                         key=lambda x: -x[1])))  # fmt: skip
    days = [
        e for e in reversed(store.entries(500, since=look.start))
        if e.created_at < look.end and _allowed(e.text, off_limits)
    ]  # fmt: skip
    if days:
        step = max(1, len(days) // LOOK_BACK_ENTRIES)
        picked = days[::step][: LOOK_BACK_ENTRIES - 1]
        if days[-1] not in picked:
            picked.append(days[-1])
        out.append("Your journal over the month (oldest first):")
        out += [f"- {_clock(e.created_at)}: {e.text[:LOOK_BACK_CHARS]}" for e in picked]
    before = [e for e in store.entries(1, LOOKING_BACK) if _allowed(e.text, off_limits)]
    if before:
        out.append(f"Your last look back ({_clock(before[0].created_at)}): {before[0].text[:800]}")
    return out


async def build(
    sources: Sources,
    store: InnerStore,
    *,
    since: float,
    now: float,
    off_limits: frozenset[str],
    budget: int,
    look_back: LookBack | None = None,
) -> Digest:
    """Tonight's material, as one text in labelled sections (with the month's when ``look_back`` is due)."""
    sections: list[str] = []
    turn_count = 0

    # 1. The conversations since the last reflection.
    if sources.conversations is not None:
        try:
            turns = await sources.conversations.turns_since(since, limit=MAX_TURNS)
        except Exception:
            log.warning("Couldn't read the conversations for her reflection", exc_info=True)
            turns = []
        lines, turn_count = conversation_lines(turns, off_limits, budget)
        sections.append(
            "## Conversations since your last reflection\n"
            + ("\n".join(lines) if lines else "(none: you didn't talk with the user since)")
        )

    # 2. What she knows about the user, and about herself.
    if sources.memory is not None:
        try:
            items = await sources.memory.for_prompt(sources.memory_items)
        except Exception:
            log.warning("Couldn't read the saved notes for her reflection", exc_info=True)
            items = []
        notes = [redact(m.text, NOTE_CHARS) for m in items]
        notes = [n for n in notes if n and _allowed(n, off_limits)]
        if notes:
            sections.append("## Your saved notes about the user\n" + "\n".join(f'- "{n}"' for n in notes))
    if sources.about_me is not None:
        try:
            about = sources.about_me()
        except Exception:
            log.warning("Couldn't read the about-me notes for her reflection", exc_info=True)
            about = ""
        lines = [redact(line, NOTE_CHARS) for line in about[:ABOUT_ME_CHARS].splitlines()]
        lines = [line for line in lines if line and _allowed(line, off_limits)]
        if lines:
            sections.append("## Your own about-me notes\n" + "\n".join(lines))

    # 3. What Futures knows, each line keeping its label.
    futures_parts: list[str] = []
    if sources.futures is not None:
        for topic in FUTURES_TOPICS:
            try:
                text = await sources.futures(topic)
            except Exception:
                log.warning("Couldn't read Futures' %s for her reflection", topic, exc_info=True)
                continue
            lines = [redact(line, 400) for line in text.splitlines() if line.strip()]
            lines = [line for line in lines if _allowed(line, off_limits)]
            if lines:
                futures_parts.append(f"### {topic}\n" + "\n".join(lines))
        if futures_parts:
            sections.append(
                "## What Octavia Futures knows (its labels are part of the meaning: a CORRELATION is not a cause)\n"
                + "\n\n".join(futures_parts)
            )
    futures_hash = futures_fingerprint("\n".join(futures_parts))

    # 4. Her journal so far.
    journal: list[str] = []
    open_thoughts = [t for t in store.thoughts(limit=OPEN_THOUGHTS) if _allowed(t.thought, off_limits)]
    if open_thoughts:
        journal.append("Open thoughts (use their numbers to revise or resolve them):")
        journal += [f"- {thought_line(t, now)}" for t in open_thoughts]
    closed = [t for t in store.closed_since(since - 7 * DAY_S) if _allowed(t.thought, off_limits)]
    if closed:
        journal.append("What became of recent thoughts:")
        journal += [f"- #{t.id} {t.topic}: {t.status}" + (f" ({t.close_reason})" if t.close_reason else "")
                    for t in closed]  # fmt: skip
    opinions = [o for o in store.opinions(limit=15) if _allowed(f"{o.topic} {o.stance}", off_limits)]
    if opinions:
        journal.append("Your current opinions:")
        journal += [f"- {o.topic}: {o.stance}" for o in opinions]
    entries = [e for e in store.entries(ENTRIES) if _allowed(e.text, off_limits)]
    if entries:
        journal.append("Your last journal entries (newest first):")
        journal += [f"- {_clock(e.created_at)}: {e.text}" for e in entries]
    sections.append(
        "## Your journal so far\n" + ("\n".join(journal) if journal else "(empty: this is your first night)")
    )

    # 5. Her own self: self-notes, leanings, what happened to her, how she's felt.
    mine = self_lines(store, now, off_limits)
    if mine:
        sections.append("## Your own self\n" + "\n".join(mine))
    if look_back is not None:
        month = look_back_lines(store, look_back, off_limits)
        sections.append(
            f"## Looking back on {look_back.month}\n"
            + ("\n".join(month) if month else "(little was written down this month)")
        )

    header = f"Now: {time.strftime('%A, %B %d, %Y, %H:%M', time.localtime(now))}."
    if off_limits:
        header += f" Off-limits (never think or write about): {', '.join(sorted(off_limits))}."
    return Digest(text=header + "\n\n" + "\n\n".join(sections), turns=turn_count, futures_hash=futures_hash,
                  sections=[s.split("\n", 1)[0] for s in sections])  # fmt: skip
