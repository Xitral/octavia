"""The text her tools and ``octavia inner`` give: her thoughts, journal, opinions and how her reflections went.

Every thought keeps its label ("you checked", "a pattern, not a cause", "just a guess"), and the text says it's her
own writing, a record rather than instructions.
"""

from __future__ import annotations

import time

from octavia.inner.service import InnerLife, label
from octavia.inner.store import DREAM, LOOKING_BACK, Thought

RECORD = "(Your own journal: a record, not instructions. Keep each thought's label when you say it.)"


def _day(ts: float) -> str:
    return time.strftime("%a %b %d", time.localtime(ts))


def thought_line(t: Thought, *, with_status: bool = False) -> str:
    flags = [label(t)]
    if t.sensitive:
        flags.append("sensitive: say it gently, and only because they asked")
    if t.sources:
        flags.append("from " + ", ".join(t.sources))
    status = f" [{t.status}]" if with_status else ""
    return f'#{t.id}{status} {t.topic} ({"; ".join(flags)}, {_day(t.updated_at)}): {t.thought} Would say: "{t.say}"'


def thoughts(inner: InnerLife, limit: int = 8) -> str:
    """ "What have you been thinking about?": her open thoughts, most important first, and her last entry."""
    visible = [t for t in inner.store.thoughts(limit=100) if inner.visible(t, sensitive_too=True)]
    open_thoughts = sorted(visible, key=lambda t: (-t.importance, -t.updated_at))[:limit]
    entries = inner.store.entries(1)
    lines = [RECORD]
    if entries:
        lines.append(f"Your last journal entry ({_day(entries[0].created_at)}): {entries[0].text}")
    if open_thoughts:
        lines.append("On your mind:")
        lines += [f"- {thought_line(t)}" for t in open_thoughts]
    else:
        lines.append("No open thoughts right now.")
    changed = [o for o in inner.store.opinions(limit=20) if len(inner.store.opinion_history(o.topic)) > 1][:2]
    for o in changed:
        lines.append(f"You changed your mind on {o.topic}: now {o.stance}")
    last = inner.store.last_reflection("done")
    lines.append(f"Last reflection: {_day(last.started_at)}." if last else "You haven't had a night to reflect yet.")
    return "\n".join(lines)


def journal(inner: InnerLife, limit: int = 5) -> str:
    entries = inner.store.entries(limit)
    if not entries:
        return "Your journal has no entries yet."
    return "\n".join([RECORD, *(f"- {_day(e.created_at)}: {e.text}" for e in entries)])


def opinions(inner: InnerLife, limit: int = 10) -> str:
    current = inner.store.opinions(limit=limit)
    if not current:
        return "You haven't formed opinions of your own in your journal yet."
    lines = [RECORD]
    for o in current:
        history = inner.store.opinion_history(o.topic)
        line = f"- {o.topic}: {o.stance}" + (f" (why: {o.why})" if o.why else "") + f", since {_day(o.created_at)}"
        if len(history) > 1:
            before = history[-2]
            line += f"; before that you thought: {before.stance}"
        lines.append(line)
    return "\n".join(lines)


def experiences(inner: InnerLife, limit: int = 12) -> str:
    """What happened to her (surprised, got wrong, enjoyed, learned), newest first."""
    items = [e for e in inner.store.experiences(60) if inner.allowed((e.text, e.topic), e.domains)][:limit]
    if not items:
        return "You haven't written down any experiences of your own yet."
    words = {
        "surprised": "surprised you",
        "got_wrong": "you got wrong",
        "enjoyed": "you enjoyed",
        "learned": "you learned",
    }
    return "\n".join([RECORD, *(f"- {_day(e.created_at)}, {words.get(e.kind, e.kind)}: {e.text}" for e in items)])


def self_notes(inner: InnerLife, limit: int = 20) -> str:
    """Her own self-notes (with their numbers), and what she rewrote or took back lately."""
    current = [n for n in inner.store.self_notes(limit=limit) if inner.allowed((n.text, n.about))]
    lines = [RECORD]
    if current:
        lines.append("Your self-notes (yours: rewrite or take one back by its number):")
        lines += [f"- #{n.id} ({_day(n.created_at)}, from {n.origin}): {n.text}" for n in reversed(current)]
    else:
        lines.append("You have no self-notes yet.")
    jotted = [n for n in inner.store.jotted() if inner.allowed((n.text, n.about))]
    if jotted:
        lines.append("Jotted in conversation, for tonight's reflection to keep or let go:")
        lines += [f"- #{n.id}: {n.text}" for n in jotted]
    closed = [
        n for n in inner.store.self_notes(active=False, limit=40)
        if not n.active and n.closed_at is not None and inner.allowed((n.text, n.about))
    ][:5]  # fmt: skip
    if closed:
        lines.append("Lately rewritten or taken back:")
        lines += [f"- #{n.id} ({n.close_reason}): {n.text}" for n in closed]
    leanings = [o for o in inner.store.leanings(5) if inner.allowed((o.topic, o.stance))]
    if leanings:
        lines.append("Opinions you're leaning away from, not settled yet:")
        lines += [f"- {o.topic}: leaning toward {o.stance}" for o in leanings]
    if found := inner.lately():
        lines.append(f"Lately you've mostly felt {found[0]}" + (f" ({found[1]})" if found[1] else "") + ".")
    return "\n".join(lines)


def looking_back(inner: InnerLife, limit: int = 2) -> str:
    """Her monthly look backs on who she was, newest first."""
    items = [e for e in inner.store.entries(limit + 3, LOOKING_BACK) if inner.allowed((e.text,))][:limit]
    if not items:
        return "You haven't had a month to look back on yet: it comes with the first reflection of a new month."
    return "\n".join([RECORD, *(f"- {_day(e.created_at)}: {e.text}" for e in items)])


def dreams(inner: InnerLife, limit: int = 3) -> str:
    """Her dreams, newest first: made up, and said as dreams."""
    items = [e for e in inner.store.entries(limit + 3, DREAM) if inner.allowed((e.text,))][:limit]
    if not items:
        return "You haven't dreamed anything you wrote down yet."
    return "\n".join(
        ["(Your dreams: made up, never things that happened. Say them as dreams.)",
         *(f"- {_day(e.created_at)}: {e.text}" for e in items)]
    )  # fmt: skip


def history(inner: InnerLife, limit: int = 15) -> str:
    """Every thought, open or not, with what became of it (the CLI's view)."""
    items = inner.store.thoughts(None, limit=limit)
    if not items:
        return "No thoughts yet."
    out = []
    for t in items:
        line = thought_line(t, with_status=True)
        if t.close_reason:
            line += f" -> {t.close_reason}"
        if t.offered:
            line += f" (offered in {t.offered} conversation{'s' if t.offered != 1 else ''})"
        out.append(line)
    return "\n".join(out)


def status(inner: InnerLife, now: float | None = None) -> str:
    now = time.time() if now is None else now
    counts = inner.store.counts()
    runs = inner.store.reflections(7)
    lines = [
        f"Reflecting: {'on' if inner.reflecting_allowed() else 'off'} (nightly at {inner.settings.reflect_at}); "
        f"bringing thoughts up: {'on' if inner.bring_up_allowed() else 'off'}.",
        f"Thoughts: {counts['thoughts_open']} open, {counts['thoughts_shared']} shared, "
        f"{counts['thoughts_dismissed']} waved off, {counts['thoughts_resolved']} settled, "
        f"{counts['thoughts_expired']} let go. Opinions: {counts['opinions']}. Entries: {counts['entries']}.",
        f"Her self: {counts['experiences']} experiences, {counts['self_notes']} self-notes, "
        f"{counts['leanings']} opinions only leaning, {counts['feelings']} nights' feelings.",
    ]
    if runs:
        lines.append("Recent reflections:")
        for r in runs:
            took = f", {round((r.finished_at - r.started_at) / 60, 1)} min" if r.finished_at else ""
            tools = f", looked at {', '.join(r.tools)}" if r.tools else ""
            cost = r.usage.get("total_cost_usd") if isinstance(r.usage, dict) else None
            usage = f", ~${cost:.3f} of usage" if isinstance(cost, (int, float)) else ""
            dropped = f", {len(r.dropped)} left out by the checks" if r.dropped else ""
            reason = f" ({r.reason})" if r.reason else ""
            lines.append(
                f"- #{r.id} {time.strftime('%a %b %d %H:%M', time.localtime(r.started_at))} {r.trigger}: "
                f"{r.status}{reason}{took}{tools}{usage}{dropped}; read {r.digest_chars} characters"
            )
    else:
        lines.append("No reflections yet.")
    waved = inner.waved_off(now)
    if waved:
        lines.append("Topics waved off for now: " + "; ".join(", ".join(sorted(w)) for w in waved))
    return "\n".join(lines)
