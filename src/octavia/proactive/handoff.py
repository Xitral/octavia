"""The morning handoff: what she did overnight, said in a line or two, and the note her next turn gets so the user's
"keep the first, skip the second" can be acted on (``decide_overnight_work``).

Only what passed its check is called ready. What ran but didn't pass is said as that, and what she only planned (the
suggest level) is offered, never described as done.
"""

from __future__ import annotations

from collections.abc import Sequence

from octavia.proactive.store import FAILED, PLANNED, READY, UNVERIFIED, Item

_NUMBERS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def _count(n: int, word: str) -> str:
    return f"{_NUMBERS.get(n, str(n))} {word}{'' if n == 1 else 's'}"


def _and(parts: Sequence[str]) -> str:
    parts = list(parts)
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def morning_text(items: Sequence[Item], running: Sequence[Item] = ()) -> tuple[str, str] | None:
    """(the spoken lines, the note for her next turn), or None when there's nothing to hand over. ``running``: work
    still under way, named as not checked yet."""
    ready = [i for i in items if i.status == READY]
    unverified = [i for i in items if i.status == UNVERIFIED]
    planned = [i for i in items if i.status == PLANNED]
    failed = [i for i in items if i.status == FAILED]  # only ones the user kept while they ran
    if not (ready or unverified or planned or failed):
        return None
    lines = []
    if ready:
        checked = "checked" if len(ready) == 1 else "each one checked"
        lines.append(f"Overnight I prepared {_and([i.title for i in ready])}, {checked}.")
    if unverified:
        names = _and([i.title for i in unverified])
        opening = "I also tried" if ready else "Overnight I tried"
        lines.append(f"{opening} {names}, but {'it' if len(unverified) == 1 else 'they'} didn't pass my check.")
    if planned:
        lines.append(f"I could prepare {_and([i.title for i in planned])} if you want.")
    if failed:
        lines.append(f"I couldn't finish {_and([i.title for i in failed])}.")
    if running:
        lines.append(f"I'm still checking {_and([i.title for i in running])}.")
    if any(not i.decided for i in (*ready, *unverified)):
        lines.append("Keep, drop or change any of it?")
    elif planned:
        lines.append("Want me to?")
    return " ".join(lines), note(items)


def note(items: Sequence[Item]) -> str:
    """What her next turn needs to act on the user's answer."""
    rows = []
    for item in items:
        if item.status == READY:
            where = f"; result: {', '.join(item.files)}" if item.files else ""
            branch = f"; code on branch {item.branch} (merge_objective_work #{item.objective})" if item.branch else ""
            rows.append(f"#{item.id} {item.title}: ready, checked ({item.evidence[:160]}){where}{branch}")
        elif item.status == UNVERIFIED:
            rows.append(f"#{item.id} {item.title}: didn't pass its check ({item.problem[:160]}); not to be called done")
        elif item.status == PLANNED:
            rows.append(f"#{item.id} {item.title}: only planned, nothing done yet (approve runs it now)")
        elif item.status == FAILED:
            rows.append(f"#{item.id} {item.title}: couldn't be finished ({item.problem[:160]}); not done")
        if item.decided:
            rows[-1] += f" (they already said {item.decision})"
    return (
        "Your overnight work, waiting for the user's answer. Record each answer with decide_overnight_work "
        "(approve, reject, or redirect with what they want instead); never call an item done unless it says ready. "
        + "; ".join(rows)
    )
