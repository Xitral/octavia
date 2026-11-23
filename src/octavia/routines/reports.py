"""What she learned by watching, as text (the ``routines`` tool and ``octavia routines``)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from octavia.routines import rules

if TYPE_CHECKING:
    from octavia.routines.service import RoutineLearner

_HEADINGS = {
    "learned": "Learned (she may offer these)",
    "watching": "Watching (not sure yet: never offered)",
    "dismissed": "Turned down (never offered again)",
    "retired": "Stopped offering (not taken up)",
}


def listing(learner: RoutineLearner) -> str:
    store = learner.store
    rows = store.all()
    last = store.meta("last_mined")
    when = datetime.fromtimestamp(float(last)).strftime("%a %b %d, %I:%M %p") if last else "never"
    lines = [
        f"Last looked for routines: {when} (over the last {learner.settings.history_days} days). "
        f"Offers: {'on' if learner.offers_on else 'off'}."
    ]
    if not rows:
        lines.append(
            "Nothing yet. A routine needs at least "
            f"{learner.settings.min_days} separate days, out of at least {rules.MIN_HISTORY_DAYS} days of records."
        )
        return "\n".join(lines)
    for status, heading in _HEADINGS.items():
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        lines.append(f"{heading}:")
        for row in group:
            routine = store.as_routine(row)
            extra = ""
            if row["offers"]:
                extra = f"; offered {row['offers']}x, taken up {row['taken']}x"
            if row["reason"] and status in ("dismissed", "retired"):
                extra += f"; {row['reason']}"
            said = routine.describe()
            lines.append(f"  #{row['id']} {said[:1].upper()}{said[1:]}{extra}")
    return "\n".join(lines)
