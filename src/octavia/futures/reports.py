"""What Octavia can say about her Futures model, as plain text for the brain (the tools) or the terminal (the CLI).

Every line says what kind of knowledge it is: ``[you said]``, ``[fact from TickTick]``, ``[correlation · moderate
evidence]``, ``[tested in an experiment]``, ``[prediction · about 40%]``. The brain is told to keep those
distinctions when it answers: a correlation is never passed off as a cause, and "not enough evidence yet" is a real
answer.

These functions are synchronous and read the model directly: callers run them through ``FuturesService.call``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from octavia.futures.events import FuturesEvent
from octavia.futures.explain import Trace, describe_hypothesis, explain
from octavia.futures.privacy import redact
from octavia.futures.schema import Domain, Knowledge, ref
from octavia.futures.stats import percent, sample_strength

if TYPE_CHECKING:
    from octavia.futures.engine import FuturesService
    from octavia.futures.hypotheses import Hypothesis
    from octavia.futures.predictions import Prediction

DAY_S = 86400.0
TOPICS = (
    "learned",
    "patterns",
    "unsure",
    "missed_deadlines",
    "strategies",
    "changes",
    "predictions",
    "accuracy",
    "experiments",
    "failures",
    "performance",
    "deadlines",
    "blocked",
    "waiting",
    "goals",
    "reminders",
    "episodes",
    "privacy",
    "offers",
    "self",
    "lessons",
)
_RULE = (
    "(Keep these labels' meaning when you answer: a correlation is not a cause; say plainly when there isn't "
    "enough evidence yet.)"
)


def report(service: FuturesService, topic: str, limit: int = 5) -> str:
    """The text for one topic (``TOPICS``)."""
    render = _TOPICS.get(topic)
    if render is None:
        raise ValueError(f"unknown topic {topic!r}; one of {', '.join(TOPICS)}")
    return render(service, max(1, min(limit, 20)))


# ---------------------------------------------------------------------------------------------- labels
def hypothesis_label(h: Hypothesis) -> str:
    if h.knowledge is Knowledge.CAUSAL_CLAIM:
        return f"tested in an experiment · {h.strength} evidence"
    if h.claim == "user":
        return "your idea · my records don't bear it out" if h.status == "rejected" else "your idea · not tested yet"
    if h.knowledge is Knowledge.CORRELATION:
        return f"correlation · {h.strength} evidence"
    return f"possible pattern · {h.strength} evidence"


def hypothesis_line(h: Hypothesis) -> str:
    line = f"- #{h.id} [{hypothesis_label(h)}] {h.statement}"
    if h.status in ("weakened", "stale", "testing", "rejected"):
        line += f" ({h.status})"
    return line


def prediction_line(p: Prediction) -> str:
    return f"- #{p.id} [prediction · {percent(p.probability)}] {p.statement} (because {p.basis})"


def _ago(service: FuturesService, ts: float) -> str:
    seconds = max(0.0, service.clock() - ts)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)} min ago"
    if seconds < DAY_S:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / DAY_S)} days ago"


def _when(service: FuturesService, ts: float) -> str:
    moment = service.local_time(ts)
    today = service.local_time(service.clock()).date()
    if moment.date() == today:
        day = "today"
    elif moment.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = moment.strftime("%a %b %d").replace(" 0", " ")
    return f"{moment.strftime('%I:%M %p').lstrip('0')} {day}"


def _lines(title: str, lines: Iterable[str], empty: str) -> str:
    lines = list(lines)
    return f"{title}\n" + "\n".join(lines) if lines else empty


# ---------------------------------------------------------------------------------------------- topics
def _learned(service: FuturesService, limit: int) -> str:
    out: list[str] = []
    for goal in service.objectives.active()[:limit]:
        out.append(f"- [{goal.label}] You care about: {goal.text}")
    solid = service.hypotheses.list(status=("observed", "supported"), limit=limit)
    out += [hypothesis_line(h) for h in solid]
    for line, count, last in _lessons(service)[:limit]:
        seen = "once" if count == 1 else f"{count} times"
        out.append(f"- [from experience, seen {seen} · last {_ago(service, last)}] {line}")
    return _lines(
        f"What I've learned (from what actually happened; not guesses). {_RULE}",
        out,
        "Nothing solid yet: I learn from how things turn out, and I haven't seen enough of that yet.",
    )


def _patterns(service: FuturesService, limit: int) -> str:
    found = service.hypotheses.list(
        status=("speculative", "observed", "supported", "weakened", "testing"), limit=limit * 2
    )
    lines = []
    for h in found[: limit * 2]:
        line = hypothesis_line(h)
        if h.confounders:
            line += f" Could also be: {', '.join(h.confounders[:2])}."
        lines.append(line)
    return _lines(f"Patterns in my records. {_RULE}", lines, "I haven't noticed any patterns yet.")


def _unsure(service: FuturesService, limit: int) -> str:
    lines = [hypothesis_line(h) for h in service.hypotheses.list(status=("speculative", "weakened"), limit=limit)]
    lines += [prediction_line(p) + " — little history behind it" for p in service.predictions.open() if p.trials < 5][
        :limit
    ]
    lines += [f"- [I noticed; not confirmed] {o.text}" for o in service.objectives.proposed()[:limit]]
    lines += [
        f"- [experiment offered, waiting for your answer] {e.title}"
        for e in service.experiments.list(status="proposed")
    ]
    return _lines(
        "What I'm unsure about: possible patterns and guesses without enough evidence yet.",
        lines,
        "Nothing I'm half-sure about right now: either I have evidence or I don't have a guess.",
    )


def _missed(service: FuturesService, limit: int) -> str:
    overall = service.outcomes.tally("task_on_time")
    if overall.trials == 0:
        return "I haven't seen any of your to-dos come due yet, so I can't say what makes you miss them."
    lines = [f"Overall, {overall.describe()} to-dos with a due time were finished by then."]
    lower = [
        h
        for h in service.hypotheses.list(metric="task_on_time", status=("speculative", "observed", "supported"))
        if h.effect is not None and h.effect < 0 and h.claim != "causal" and h.feature != "nudged"
    ]
    if lower:
        lines.append("Where it goes worse:")
        lines += [hypothesis_line(h) for h in lower[:limit]]
        lines.append(
            "These are correlations in my records, not proof of cause: "
            + ", ".join(lower[0].confounders[:3])
            + " could explain them too."
        )
    else:
        lines.append(f"No condition stands out yet ({sample_strength(overall.trials).value} evidence overall).")
    return "\n".join(lines)


def _strategies(service: FuturesService, limit: int) -> str:
    """What has actually worked: only what was tested (experiments) or measured directly (her suggestions taken up,
    a fix that worked more than once). Patterns that merely go with better results are listed apart, as such."""
    worked: list[str] = []
    didnt: list[str] = []
    for e in service.experiments.list(status="completed", limit=10):
        result = e.result or {}
        if result.get("summary") and result.get("supported") is True:
            worked.append(f"- [tested in an experiment] {result['summary']}")
        elif result.get("summary") and result.get("supported") is False:
            didnt.append(f"- [tested in an experiment] {result['summary']}")
    for kind in ("heads_up", "trigger", "experiment_offer"):
        tally = service.outcomes.tally("recommendation", where={"kind": kind})
        if tally.trials >= 3:
            line = f"- [measured] You took up my {kind.replace('_', ' ')}s {tally.describe()} times."
            (worked if (tally.rate or 0) >= 0.5 else didnt).append(line)
    for line, count, _ in _lessons(service):
        if count >= 2 and "worked" in line:
            worked.append(f"- [from experience, seen {count} times] {line}")
    better = [
        hypothesis_line(h) + " (not tested: it may not be the reason)"
        for h in service.hypotheses.list(status=("observed", "supported"), limit=50)
        if h.knowledge is Knowledge.CORRELATION and (h.effect or 0) > 0 and h.feature != "nudged"
    ][:limit]
    text = _lines(
        f"What has actually worked (tested, or measured directly). {_RULE}",
        worked[: limit * 2],
        "Nothing I can say has clearly worked yet: that takes an experiment or repeated, measured results.",
    )
    if didnt:
        text += "\n" + _lines("Tested or measured, and didn't help:", didnt, "")
    if tried := service.strategies.report(limit):
        text += "\n" + _lines("Approaches I wrote myself and how they're doing:", tried, "")
    if better:
        text += "\n" + _lines("Goes with better results in my records (correlations only):", better, "")
    return text


def _lessons(service: FuturesService) -> list[tuple[str, int, float]]:
    """What closed episodes learned, each lesson with how many episodes taught it and when it last did: one
    retry that worked is one instance, not a rule."""
    found: dict[str, list[float]] = {}
    for episode in service.episodes.recent(limit=200):
        for line in episode.learned:
            found.setdefault(line, []).append(episode.last_at)
    lessons = [(line, len(times), max(times)) for line, times in found.items()]
    return sorted(lessons, key=lambda item: (-item[1], -item[2]))


def _changes(service: FuturesService, limit: int) -> str:
    since = service.clock() - 14 * DAY_S
    lines = []
    for change in service.store.changes(since=since, limit=200):
        kind, _, number = change["object"].partition(":")
        if kind not in ("hypothesis", "experiment") or change["change"] in ("updated",):
            continue
        what = ""
        if kind == "hypothesis" and number.isdigit():
            h = service.hypotheses.get(int(number))
            what = h.statement if h else "(since forgotten)"
        elif kind == "experiment" and number.isdigit():
            e = service.experiments.get(int(number))
            what = e.title if e else ""
        detail = change["detail"]
        status = f" → {detail.get('status')}" if detail.get("status") else ""
        lines.append(f"- {_ago(service, change['ts'])}: {kind} #{number} {change['change']}{status}: {what}")
        if len(lines) >= limit * 2:
            break
    return _lines(
        "What changed my mind recently (evidence that formed, strengthened, weakened or rejected a belief):",
        lines,
        "Nothing has changed my mind in the last two weeks.",
    )


def _predictions(service: FuturesService, limit: int) -> str:
    open_ones = service.predictions.open()
    lines = [prediction_line(p) for p in open_ones[:limit]]
    accuracy = service.predictions.accuracy()
    tail = f"\n(I've scored myself on {accuracy.resolved} predictions so far.)" if accuracy.resolved else ""
    return (
        _lines(
            f"Predictions I've made that are still open ({len(open_ones)}):", lines, "No open predictions right now."
        )
        + tail
    )


def _accuracy(service: FuturesService, limit: int) -> str:
    a = service.predictions.accuracy()
    if a.resolved == 0:
        tossups = f" ({a.tossups} settled, but they were toss-ups: I made no call)" if a.tossups else ""
        return f"None of my predictions has been settled yet{tossups}, so I can't say how accurate I am."
    lines = [f"{a.correct} of {a.resolved} settled predictions came out the way I said."]
    if a.tossups:
        lines.append(f"({a.tossups} more were toss-ups: I had nothing to go on, so I made no call either way.)")
    if a.brier is not None:
        lines.append(f"Brier score {a.brier:.2f} (0 is perfect; always saying 50% scores 0.25).")
    for bucket in a.buckets:
        lines.append(
            f"- When I said {bucket.label}: {bucket.came_true} of {bucket.predictions} came true"
            f" (I averaged {percent(bucket.mean_probability)})."
        )
    for kind, tally in a.by_kind.items():
        lines.append(f"- {kind.replace('_', ' ')}: {tally.describe()} right.")
    return "\n".join(lines)


def _experiments(service: FuturesService, limit: int) -> str:
    lines = []
    for e in service.experiments.list(limit=limit * 2):
        counts = service.store.execute(
            "SELECT variant, COUNT(*) AS n, SUM(success IS NOT NULL) AS done FROM trials WHERE experiment_id=? "
            "GROUP BY variant",
            (e.id,),
        )
        progress = ", ".join(f"{r['variant']}: {r['done'] or 0} measured" for r in counts)
        line = f"- #{e.id} {e.title} [{e.status}]"
        if progress:
            line += f" ({progress}; needs {e.min_per_variant} each)"
        if e.result and e.result.get("summary"):
            line += f" Result: {e.result['summary']}"
        lines.append(line)
    return _lines(
        "Experiments (only the ones on how I talk run without your yes; I never experiment with people, health, "
        "money, school work or communications):",
        lines,
        "No experiments: none running, none offered.",
    )


def _failures(service: FuturesService, limit: int) -> str:
    since = service.clock() - 14 * DAY_S
    lines = []
    for group in service.performance.repeated_failures(since=since)[:limit]:
        where = f" in {group.app}" if group.app else ""
        lines.append(
            f"- {group.name} failed {group.count} times ({group.failure or 'error'}){where}, "
            f"last {_ago(service, group.last_ts)}"
        )
    for stat in service.performance.stats(kind="tool", since=since):
        rate = stat.tally.rate
        if stat.tally.trials >= 5 and rate is not None and rate < 0.7:
            lines.append(f"- {stat.name}: worked {stat.tally.describe()} times in the last two weeks")
    return _lines(
        "Where I keep failing (my own measured record, last two weeks):",
        lines[: limit * 2],
        "No repeated failures in the last two weeks.",
    )


def _past_cases(service: FuturesService, limit: int) -> str:
    now = service.clock()
    lines = [f"- #{c.id} {c.describe(now)}" for c in service.lessons.recent(limit=limit * 2)]
    return _lines("What's gone wrong lately, as I'd recall it next time:", lines, "No cases that went wrong.")


def _performance(service: FuturesService, limit: int) -> str:
    since = service.clock() - 30 * DAY_S
    lines = []
    for stat in service.performance.stats(since=since)[: limit * 2]:
        latency = f", typically {stat.median_latency_s:.1f}s" if stat.median_latency_s is not None else ""
        corrected = f", corrected {stat.corrected}×" if stat.corrected else ""
        lines.append(f"- {stat.kind} {stat.name}: {stat.tally.describe()} worked{latency}{corrected}")
    return _lines("How I've been doing (last 30 days, measured):", lines, "No measurements yet.")


def _deadlines(service: FuturesService, limit: int) -> str:
    now = service.clock()
    midnight = service.local_time(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    lines = []
    for entity, change in service.world.deadlines_changed(midnight)[:limit]:
        old = _when(service, float(change.old)) if change.old else "none"
        new = _when(service, float(change.new)) if change.new else "none"
        lines.append(f"- [fact] {entity.name}: due {old} → {new} (changed {_ago(service, change.ts)})")
    upcoming = [
        f"- [fact] {e.name} ({e.kind}) due {_when(service, float(e.attrs['due_ts']))}"
        for e in service.world.open_with_due(before=now + 2 * DAY_S)[:limit]
    ]
    text = _lines("Deadlines that changed today:", lines, "No deadline changed today that I saw.")
    text += " (Only what TickTick's looks and Canvas showed me: for everything that's due, ask TickTick and Canvas.)"
    return text + ("\n" + _lines("Due in the next two days:", upcoming, "") if upcoming else "")


def _blocked(service: FuturesService, limit: int) -> str:
    lines = []
    for entity, relations in service.world.blocked()[:limit]:
        on = ", ".join(_name(service, r.object) for r in relations) or "something"
        lines.append(f"- [you told me] {entity.name} is blocked on {on}")
    return _lines("Blocked:", lines, "Nothing is marked as blocked.")


def _waiting(service: FuturesService, limit: int) -> str:
    lines = []
    for relation in service.world.waiting_on()[: limit * 2]:
        who = _name(service, relation.object)
        what = _name(service, relation.subject)
        replied = relation.attrs.get("replied_ts")
        state = f"they got back {_ago(service, float(replied))}" if replied else "no reply seen yet"
        lines.append(f"- {what} waiting on {who}: {state}")
    return _lines("Waiting on other people:", lines, "You're not waiting on anyone that I know of.")


def _goals(service: FuturesService, limit: int) -> str:
    lines = [f"- #{g.id} [{g.label}] {g.text}" for g in service.objectives.active()]
    lines += [f"- #{g.id} [{g.label}] {g.text} (say if it's right)" for g in service.objectives.proposed()]
    return _lines(
        "Your priorities (only what you said, or confirmed, is planned around):",
        lines,
        "You haven't told me any goals or priorities yet.",
    )


def _reminders(service: FuturesService, limit: int) -> str:
    lines = []
    for t in service.triggers.armed()[: limit * 3]:
        until = f" (until {_when(service, t.expires_at)})" if t.expires_at else ""
        lines.append(f"- #{t.id} {t.description}: {t.message}{until}")
    return _lines('"Remind me when" reminders waiting:', lines, 'No "remind me when" reminders are waiting.')


_OFFER_NAMES = {"offer_assignment": "help with Canvas work due soon", "offer_meeting": "opening a meeting's link"}
_NOT_HELD = frozenset({"said recently", "you turned this down"})  # repeats kept from being said, not offers held back


def _offers(service: FuturesService, limit: int) -> str:
    """Her offers of help before being asked, and how each kind has gone: the numbers that say whether they help."""
    from octavia.futures.offers import tally

    now = service.clock()
    lines = []
    for t in tally(service.store, now):
        name = _OFFER_NAMES.get(t.kind, t.kind.replace("_", " "))
        held = [(reason, n) for reason, n in t.held if reason not in _NOT_HELD]
        held_text = (
            f" Held back {sum(n for _, n in held)}: " + ", ".join(f"{n} {r}" for r, n in held[:3]) + "." if held else ""
        )
        if not t.made:
            lines.append(f"- {name}: none made yet.{held_text}")
            continue
        answered = t.taken + t.rejected + t.ignored
        rate = f" Taken up {t.taken} of {answered} (deferred ones not counted)." if answered else ""
        lines.append(
            f"- {name}: {t.made} made. Yes {t.accepted}, used without a yes {t.followed}, not now {t.deferred}, "
            f"no {t.rejected}, no answer {t.ignored}.{rate}{held_text}"
        )
    recent = [r for r in service.gate.list(status="delivered", limit=limit * 4) if r.kind.startswith("offer_")][:limit]
    for r in recent:
        lines.append(f'  · {_ago(service, r.delivered_at or r.created_at)}: "{r.text}" → {r.response or "waiting"}')
    return _lines(
        "Offers of help in the last 30 days (counts from my records; few offers say little):",
        lines,
        "No offers of help yet.",
    )


def _episodes(service: FuturesService, limit: int) -> str:
    lines = []
    for e in service.episodes.recent(limit=limit):
        line = f"- #{e.id} {e.title} ({_ago(service, e.started_at)})"
        if e.outcome:
            line += f": {e.outcome}"
        if e.problems:
            line += f"; problems: {', '.join(e.problems[:2])}"
        lines.append(line)
    return _lines("Recent episodes:", lines, "No episodes yet.")


def _privacy(service: FuturesService, limit: int) -> str:
    c = service.controls.snapshot()
    lines = [
        f"- Speaking up on my own: {'on' if c['proactive'] else 'off'}",
        f"- Off-limits (never learned from or brought up): {', '.join(c['off_limits']) or 'nothing'}",
        f"- No experiments in: {', '.join(c['no_experiments']) or 'nothing (and every experiment needs your yes)'}",
        f"- Never recorded: {', '.join(c['excluded_sources']) or 'nothing'}",
        f"- Muted kinds of remarks: {', '.join(c['muted']) or 'none'}",
        f"- Hypotheses you told me to forget: {c['forgotten_hypotheses']}",
        f"- Stored events: {service.log.count()} (older than {service.settings.retention_days} days are deleted)",
    ]
    return "Your controls over what I learn and say:\n" + "\n".join(lines)


_TOPICS: dict[str, Callable[[FuturesService, int], str]] = {
    "learned": _learned,
    "patterns": _patterns,
    "unsure": _unsure,
    "missed_deadlines": _missed,
    "strategies": _strategies,
    "changes": _changes,
    "predictions": _predictions,
    "accuracy": _accuracy,
    "experiments": _experiments,
    "failures": _failures,
    "performance": _performance,
    "deadlines": _deadlines,
    "blocked": _blocked,
    "waiting": _waiting,
    "goals": _goals,
    "reminders": _reminders,
    "episodes": _episodes,
    "privacy": _privacy,
    "offers": _offers,
    "lessons": _past_cases,
    "self": lambda service, limit: service.selfscore.report(service.clock(), local_time=service.local_time),
}


def _name(service: FuturesService, entity_ref: str) -> str:
    entity = service.world.get(entity_ref)
    if entity is not None:
        return entity.name
    kind, _, key = entity_ref.partition(":")
    return "you" if entity_ref == ref("user", "me") else key or kind


# ---------------------------------------------------------------------------------------------- explain
_TARGET = re.compile(r"^\s*(recommendation|prediction|hypothesis|experiment)?\s*#?\s*(\d+)?\s*$", re.I)


def explain_target(service: FuturesService, what: str = "latest") -> str:
    """ "Why?" for the latest thing she recommended, or a specific recommendation, prediction, hypothesis or
    experiment ("prediction 3")."""
    what = (what or "latest").strip().lower()
    if what in ("", "latest", "last", "that", "it", "assumptions"):
        rec = service.latest_recommendation()
        if rec is None:
            return "I haven't recommended or said anything on my own yet, so there's nothing to explain."
        text = explain(
            Trace.from_dict(rec.trace), said=rec.text, policy=rec.policy, status=rec.status, reason=rec.reason
        )
        return f"(recommendation #{rec.id}, {_ago(service, rec.created_at)})\n{text}"
    found = _TARGET.match(what)
    if not found or not found.group(2):
        raise ValueError('say "latest", or a kind and a number, like "prediction 3"')
    kind, number = (found.group(1) or "recommendation").lower(), int(found.group(2))
    if kind == "recommendation":
        rec = service.gate.get(number)
        if rec is None:
            return f"There's no recommendation #{number}."
        return explain(
            Trace.from_dict(rec.trace), said=rec.text, policy=rec.policy, status=rec.status, reason=rec.reason
        )
    if kind == "prediction":
        p = service.predictions.get(number)
        if p is None:
            return f"There's no prediction #{number}."
        lines = [prediction_line(p).removeprefix("- ")]
        if p.status == "resolved":
            lines.append(
                f"It {'came true' if p.occurred else 'did not happen'}, so I was {'right' if p.correct else 'wrong'}."
            )
        elif p.status != "open":
            lines.append(f"It was {p.status}.")
        lines.append(f"Made by: {p.provider}. What it assumes: that {p.basis} still says something about this one.")
        return "\n".join(lines)
    if kind == "hypothesis":
        h = service.hypotheses.get(number)
        if h is None:
            return f"There's no hypothesis #{number} (it may have been forgotten)."
        lines = [describe_hypothesis(_hypothesis_dict(h))]
        if h.confounders:
            lines.append(f"Other things that could explain it: {', '.join(h.confounders)}.")
        for ev in service.hypotheses.evidence(h.id, limit=5):
            sign = {1: "for", -1: "against"}.get(ev["direction"], "about")
            lines.append(f"- evidence {sign} it, {_ago(service, ev['ts'])}: {ev['note']}")
        return "\n".join(lines)
    e = service.experiments.get(number)
    if e is None:
        return f"There's no experiment #{number}."
    lines = [
        f"Experiment #{e.id}: {e.title} [{e.status}]",
        f"Variants: {', '.join(e.variants)}; measured by {e.metric}.",
    ]
    lines += [f"- {c}" for c in e.constraints]
    if e.result and e.result.get("summary"):
        lines.append(f"Result: {e.result['summary']}")
    return "\n".join(lines)


def _hypothesis_dict(h: Hypothesis) -> dict[str, Any]:
    return {"id": h.id, "statement": h.statement, "strength": h.strength, "claim": h.claim, "status": h.status}


# ---------------------------------------------------------------------------------------------- the user's say
FORGETTABLE = ("hypothesis", "reminder", "episode", "prediction", "goal", "lesson", "approach", "source", "domain", "everything")


def forget(service: FuturesService, what: str, *, number: int | None = None, name: str = "") -> str:
    """Delete what the user asked her to forget."""
    if what == "hypothesis":
        h = service.hypotheses.forget(_need(number))
        if h is None:
            return f"There's no hypothesis #{number}."
        service.controls.suppress_hypothesis(h.key)
        service.store.record_change(f"hypothesis:{h.id}", "forgotten", detail={"by": "the user"})
        return f'Forgotten: "{h.statement}" — and I won\'t form it again from the same data.'
    if what == "reminder":
        t = service.triggers.cancel(_need(number))
        return f"Cancelled: {t.description}." if t else f"There's no reminder #{number}."
    if what == "episode":
        return "Forgotten." if service.episodes.forget(_need(number)) else f"There's no episode #{number}."
    if what == "prediction":
        n = service.store.changed("DELETE FROM predictions WHERE id=?", (_need(number),))
        if n:
            service.store.record_change(f"prediction:{number}", "forgotten", detail={"by": "the user"})
        return "Forgotten." if n else f"There's no prediction #{number}."
    if what == "goal":
        return "Forgotten." if service.objectives.forget(_need(number)) else f"There's no goal #{number}."
    if what == "approach":
        return "Dropped." if service.strategies.forget(_need(number)) else f"There's no approach #{number}."
    if what == "lesson":
        return "Forgotten." if service.lessons.forget(_need(number)) else f"There's no lesson #{number}."
    if what == "source":
        source = name.strip().lower()
        n = service.log.delete(source=source)
        service.store.record_change(f"source:{source}", "forgotten", detail={"events": n})
        return f"Deleted {n} events from {source}. (To stop recording it, add it to futures.exclude_sources.)"
    if what == "domain":
        domain = Domain(name.strip().lower()).value
        n = forget_domain(service, domain)
        return f"Deleted what I had about {domain} ({n} records)."
    if what == "everything":
        n = reset(service)
        service.store.record_change("futures", "reset", detail={"records": n})
        return f"Everything I'd learned is gone ({n} records). Your settings stay as they are."
    raise ValueError(f"can't forget {what!r}; one of {', '.join(FORGETTABLE)}")


# What else goes with a domain when the user asks her to forget it: her own measurements (no domain column), the
# snapshots her watches keep, and the "remind me when" kinds that watch it.
_DOMAIN_PERFORMANCE = {"computer": ("tool",), "assistant": ("brain",), "self": ("build",)}
_DOMAIN_SNAPSHOTS = {"productivity": ("ticktick",), "school": ("canvas",)}


def forget_domain(service: FuturesService, domain: str) -> int:
    """Everything she has about one domain: its events, outcomes, episodes, entities (and relations), hypotheses
    (suppressed, so they don't come back), the predictions about those entities, what she said about them, the
    reminders that watch it, her measurements and snapshots of it, and the audit trail of all of those."""
    from octavia.futures.prospective import CONDITION_KINDS
    from octavia.futures.schema import event_type

    store = service.store
    gone: list[str] = []  # the audit-trail objects to scrub
    with store.transaction():
        n = service.log.delete(domain=domain)
        n += store.changed("DELETE FROM outcomes WHERE domain=?", (domain,))
        n += service.lessons.forget_domain(domain)
        n += service.strategies.forget_domain(domain)
        refs = [r["ref"] for r in store.execute("SELECT ref FROM entities WHERE domain=?", (domain,))]
        for row in store.execute("SELECT id FROM episodes WHERE domain=?", (domain,)):
            service.episodes.forget(row["id"])
            n += 1
        for entity_ref in refs:
            for row in store.execute("SELECT id, key FROM predictions WHERE subject=?", (entity_ref,)):
                store.changed("DELETE FROM trials WHERE unit=?", (row["key"],))
                store.changed("DELETE FROM predictions WHERE id=?", (row["id"],))
                gone.append(f"prediction:{row['id']}")
                n += 1
            for row in store.execute("SELECT id FROM recommendations WHERE subject=?", (entity_ref,)):
                gone.append(f"recommendation:{row['id']}")
            n += store.changed("DELETE FROM recommendations WHERE subject=?", (entity_ref,))
            n += int(service.world.forget(entity_ref))
        for row in store.execute("SELECT id FROM recommendations WHERE domain=?", (domain,)):
            gone.append(f"recommendation:{row['id']}")
        n += store.changed("DELETE FROM recommendations WHERE domain=?", (domain,))
        watching = [
            kind
            for kind, (types, *_rest) in CONDITION_KINDS.items()
            if any(event_type(t).domain.value == domain for t in types)
        ]
        for kind in watching:
            for row in store.execute("SELECT id FROM triggers WHERE kind=?", (kind,)):
                gone.append(f"trigger:{row['id']}")
            n += store.changed("DELETE FROM triggers WHERE kind=?", (kind,))
        for kind in _DOMAIN_PERFORMANCE.get(domain, ()):
            n += store.changed("DELETE FROM performance WHERE kind=?", (kind,))
        for namespace in _DOMAIN_SNAPSHOTS.get(domain, ()):
            n += store.changed("DELETE FROM kv WHERE namespace=?", (namespace,))
        if domain == "general":  # the words of her requests are general; the conversations keep only their shape
            store.changed("UPDATE episodes SET title='Conversation' WHERE kind='conversation'")
        for h in service.hypotheses.list(domain=domain, limit=1000):
            service.controls.suppress_hypothesis(h.key)
            gone.append(f"hypothesis:{h.id}")
            n += int(service.hypotheses.forget(h.id) is not None)
        for obj in gone:
            store.changed("DELETE FROM changes WHERE object=?", (obj,))
    store.record_change(f"domain:{domain}", "forgotten", detail={"records": n})
    return n


_RESETTABLE = (
    "events",
    "entities",
    "entity_changes",
    "relations",
    "episodes",
    "outcomes",
    "lessons",
    "strategies",
    "strategy_trials",
    "hypotheses",
    "hypothesis_evidence",
    "predictions",
    "experiments",
    "trials",
    "performance",
    "triggers",
    "recommendations",
    "objectives",
    "habits",
    "kv",
)


def reset(service: FuturesService) -> int:
    """Delete everything learned, and the audit trail of it (the user's controls stay: they're settings, not
    knowledge, and so does the record of the user setting them)."""
    n = 0
    with service.store.transaction():
        for table in _RESETTABLE:
            n += service.store.changed(f"DELETE FROM {table}")
        service.store.changed("DELETE FROM changes WHERE object NOT LIKE 'controls:%'")
    return n


def _need(number: int | None) -> int:
    if number is None:
        raise ValueError("say which one (its number)")
    return number


CONTROLS = (
    "status",
    "proactive_off",
    "proactive_on",
    "off_limits",
    "allow_domain",
    "no_experiments",
    "allow_experiments",
    "mute",
    "unmute",
)


def control(service: FuturesService, action: str, *, domain: str = "", kind: str = "") -> str:
    c = service.controls
    if action == "status":
        return _privacy(service, 5)
    if action == "proactive_off":
        c.set_proactive(False)
        return 'Okay: I won\'t speak up on my own any more. (The "remind me when" ones you set still come.)'
    if action == "proactive_on":
        c.set_proactive(True)
        return "Okay: I'll speak up on my own again, sparingly."
    if action == "off_limits":
        from octavia.futures.privacy import WORD_LISTED

        target = _domain(domain)
        c.set_off_limits(target, True)
        stopped = [
            service.experiments.stop(e.id, reason="the user put its domain off-limits")
            for e in service.experiments.running()
            if e.domain == target
        ]
        for e in stopped:
            service.release_experiment(e, "The experiment was stopped: the user put its domain off-limits.")
        text = f"{target} is off-limits: I won't record, learn from or bring up anything I file under it."
        if target in WORD_LISTED:
            text += (
                f" I recognize {target} by its words in your requests and to-dos (a word list, so something I don't "
                "recognize can still slip through)."
            )
        elif target in ("people", "general"):
            text += " Only a little is filed under it, so this doesn't reach everything that might be about it."
        if stopped:
            text += " I stopped the experiment that was running there."
        text += " What I already have stays until you ask me to forget it."
        return text
    if action == "allow_domain":
        c.set_off_limits(_domain(domain), False)
        return f"{domain} isn't off-limits any more."
    if action == "no_experiments":
        target = "*" if domain.strip() in ("", "*", "all", "anything", "everything") else _domain(domain)
        c.set_experiments(target, False)
        stopped = [
            service.experiments.stop(e.id, reason="the user asked for no experiments")
            for e in service.experiments.running()
            if target == "*" or e.domain == target
        ]
        for e in stopped:
            service.release_experiment(e, "The user asked for no experiments, so it was stopped.")
        extra = f" I stopped {len(stopped)} that was running." if stopped else ""
        return f"No experiments {'at all' if target == '*' else 'with ' + target}.{extra}"
    if action == "allow_experiments":
        target = "*" if domain.strip() in ("", "*", "all", "anything", "everything") else _domain(domain)
        c.set_experiments(target, True)
        return "Experiments are allowed again (each one still needs your yes)."
    if action in ("mute", "unmute"):
        kind = kind.strip() or _latest_kind(service)
        if not kind:
            raise ValueError("say which kind of remark (like heads_up)")
        if action == "mute":
            c.mute(kind, service.clock() + 30 * DAY_S)
            return f"Muted my {kind.replace('_', ' ')} remarks for 30 days."
        c.unmute(kind)
        return f"Unmuted my {kind.replace('_', ' ')} remarks."
    raise ValueError(f"unknown action {action!r}; one of {', '.join(CONTROLS)}")


def _latest_kind(service: FuturesService) -> str:
    rec = service.latest_recommendation()
    return rec.kind if rec is not None else ""


def _domain(value: str) -> str:
    try:
        return Domain(value.strip().lower()).value
    except ValueError:
        raise ValueError(f"unknown domain {value!r}; one of {', '.join(d.value for d in Domain)}") from None


GOAL_ACTIONS = ("list", "add", "done", "drop", "confirm", "decline", "progress", "blocked", "unblocked", "waiting_on")


def goal(service: FuturesService, action: str, *, text: str = "", number: int | None = None, on: str = "") -> str:
    """The user's goals and priorities, and what's blocked or awaited."""
    o = service.objectives
    now = service.clock()
    if action == "list":
        return _goals(service, 10)
    if action == "add":
        if not text.strip():
            raise ValueError("say what the goal or priority is")
        g = o.add(text)
        service._record(
            FuturesEvent(type="goal.created", source="assistant", subject=f"goal:{g.id}", data={"text": g.text}), _fx()
        )
        return (
            f"Noted as your priority #{g.id}: {g.text}. When you ask what to work on, I'll weigh what counts toward it."
        )
    if action in ("done", "drop", "confirm", "decline"):
        n = _need(number)
        g = {"done": o.done, "drop": o.archive, "confirm": o.confirm, "decline": o.decline}[action](n)
        if g is None:
            return f"There's no goal #{n}."
        if action == "done":
            service._record(
                FuturesEvent(type="goal.completed", source="assistant", subject=f"goal:{n}", data={"text": g.text}),
                _fx(),
            )
        return {
            "done": f"Marked done: {g.text}. Nice.",
            "drop": f"Dropped: {g.text}.",
            "confirm": f"Okay, I'll plan around it: {g.text}.",
            "decline": f"Okay, I won't treat it as a goal: {g.text}.",
        }[action]
    if action == "progress":
        n = _need(number)
        g = o.get(n)
        if g is None:
            return f"There's no goal #{n}."
        service._record(
            FuturesEvent(
                type="goal.progress", source="assistant", subject=f"goal:{n}", data={"text": redact(text or g.text)}
            ),
            _fx(),
        )
        return f"Noted progress on {g.text}."
    thing = text.strip()
    if not thing:
        raise ValueError("say what it's about (the project or goal)")
    subject = ref("project", thing)
    if action == "blocked":
        if not on.strip():
            raise ValueError("say what it's blocked on")
        service.world.upsert(subject, source="user", name=thing, status="blocked", domain="goals", ts=now)
        service.world.relate(subject, "blocked_by", ref("commitment", on), ts=now)
        service.world.upsert(ref("commitment", on), source="user", name=on.strip(), domain="goals", ts=now)
        return f"Noted: {thing} is blocked on {on.strip()}."
    if action == "unblocked":
        service.world.upsert(subject, source="user", name=thing, status="active", domain="goals", ts=now)
        for relation in service.world.related(subject, "blocked_by"):
            service.world.unrelate(relation.subject, "blocked_by", relation.object)
        return f"Noted: {thing} isn't blocked any more."
    if action == "waiting_on":
        if not on.strip():
            raise ValueError("say who it's waiting on")
        service.world.upsert(subject, source="user", name=thing, domain="goals", ts=now)
        service.world.upsert(ref("person", on), source="user", name=on.strip(), domain="people", ts=now)
        service.world.relate(subject, "waiting_on", ref("person", on), ts=now)
        return f"Noted: {thing} is waiting on {on.strip()}."
    raise ValueError(f"unknown action {action!r}; one of {', '.join(GOAL_ACTIONS)}")


def _fx() -> Any:
    from octavia.futures.engine import Effects

    return Effects()


EXPERIMENT_ACTIONS = ("list", "offer", "approve", "stop", "reject")


def experiment(service: FuturesService, action: str, *, number: int | None = None, template: str = "task_nudge") -> str:
    from octavia.futures.experiments import TEMPLATES, ExperimentRefused

    if action == "list":
        return _experiments(service, 10)
    if action == "offer":
        if service.settings.experiments == "off":
            return "Experiments are switched off in the settings (futures.experiments)."
        try:
            e = service.experiments.propose(template)
        except ExperimentRefused as exc:
            return f"I can't run that one: {exc}"
        t = TEMPLATES[template]
        return (
            f"Experiment #{e.id} [{e.status}]: {t.question}\n{t.description}\nRules: "
            + "; ".join(t.constraints)
            + ". It only starts if the user clearly says yes (then approve it)."
        )
    n = _need(number)
    if action == "approve":
        try:
            e = service.experiments.approve(n)
        except ExperimentRefused as exc:
            return f"I can't start it: {exc}"
        if e.hypothesis_id is not None:
            service.hypotheses.mark_testing(e.hypothesis_id)
        service._internal("experiment.started", f"experiment:{e.id}", data={"key": e.key})
        return f"Started experiment #{e.id}: {e.title}. It ends after {e.min_per_variant} of each, or in a few weeks."
    if action == "stop":
        e = service.experiments.stop(n, reason="the user stopped it")
        if e is None:
            return f"There's no running experiment #{n}."
        service.release_experiment(e, "The user stopped the experiment before it was done.")
        service._internal("experiment.completed", f"experiment:{e.id}", data={"stopped": True})
        service.publish_tuning()  # her turns stop taking part at once
        return f"Stopped experiment #{n}. Nothing it measured so far is treated as a result."
    if action == "reject":
        e = service.experiments.reject(n)
        if e is not None:
            service.release_experiment(e, "The user turned the experiment down.")
        return f"Okay, not running #{n}." if e else f"There's no experiment #{n} waiting for an answer."
    raise ValueError(f"unknown action {action!r}; one of {', '.join(EXPERIMENT_ACTIONS)}")


# ---------------------------------------------------------------------------------------------- comparisons
def comparison_text(branches: list[dict[str, Any]], scenario: Any) -> str:
    """The simulator's branches as text for the brain to talk through, possibilities, never promises."""
    if not branches:
        return "I couldn't work out any options."
    lines = [
        f"Ways to spend your {scenario.hours:g} hours (possibilities, not promises; best-looking first — the ordering "
        "is my rough judgement, not a probability):"
    ]
    for i, b in enumerate(branches):
        letter = chr(ord("A") + i)
        lines.append(f"{letter}. {b['label']} [{b.get('uncertainty', 'insufficient')} evidence]")
        for key, title in (
            ("plan", "Plan"),
            ("benefits", "Good"),
            ("risks", "Risks"),
            ("dependencies", "Depends on"),
            ("assumptions", "Assuming"),
        ):
            values = b.get(key) or []
            if values:
                lines.append(f"   {title}: " + "; ".join(values))
    return "\n".join(lines)


def comparison_trace(branches: list[dict[str, Any]], scenario: Any, provider: str) -> Trace:
    top = branches[0] if branches else {}
    return Trace(
        observations=tuple(
            {"event": "", "what": f"{item.title} ({item.kind}) is open", "at": scenario.now}
            for item in scenario.items[:5]
        ),
        hypotheses=tuple(
            {"id": None, "statement": p["statement"], "strength": p["strength"], "claim": p["claim"]}
            for p in scenario.patterns
        ),
        goals=tuple(
            {"id": o["id"], "text": o["text"], "label": o["label"]}
            for o in scenario.objectives
            if any(o["text"] in item.goals for item in scenario.items)
        ),
        assumptions=tuple(top.get("assumptions") or ()),
        confidence=str(top.get("uncertainty") or "insufficient"),
        provider=provider,
    )


HABIT_ACTIONS = ("list", "drop", "restore")


def habit(service: FuturesService, action: str, *, number: int | None = None) -> str:
    """The habits she kept from her experiments on herself: list them, drop one (she stops at once), or bring a
    dropped one back. With no number, drop means the one kept most recently."""
    habits = service.habits
    if action == "list":
        found = habits.list(limit=20)
        if not found:
            return (
                "I haven't kept any habits yet: I keep a change only when one of my experiments on myself clearly "
                "shows it works better for you."
            )
        lines = []
        for h in found:
            state = "active" if h.active else f"dropped ({h.note})" if h.note else "dropped"
            origin = f"from experiment #{h.experiment_id}" if h.experiment_id is not None else "your choice"
            lines.append(f"- #{h.id} {h.name} [{state}], {origin}. {h.evidence}".rstrip())
        return "Habits I kept from experiments on myself:\n" + "\n".join(lines)
    if action == "drop":
        if number is None:
            active = habits.active()
            if not active:
                return "I have no habits to drop: none is active."
            number = max(active, key=lambda h: (h.created_at, h.id)).id
        dropped = habits.drop(number, reason="the user dropped it")
        if dropped is None:
            return f"There's no active habit #{number}."
        service.publish_tuning()
        return f"Dropped habit #{dropped.id}: I've stopped {dropped.name}, and I won't test it again on my own."
    if action == "restore":
        restored = habits.restore(_need(number), reason="the user asked for it back")
        if restored is None:
            return f"There's no dropped habit #{number}."
        service.publish_tuning()
        return f"Restored habit #{restored.id}: I'm back to {restored.name}."
    raise ValueError(f"unknown action {action!r}; one of {', '.join(HABIT_ACTIONS)}")
