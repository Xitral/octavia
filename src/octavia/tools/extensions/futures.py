"""Octavia Futures in conversation (docs/futures.md): what she's learned and how sure she is, why she said something,
what might happen if the user does A instead of B, "remind me when...", and the user's control over all of it.

Every tool reaches the running model through ``ctx.extras["futures"]`` (``futures.engine.FuturesService``) and does
its work in the model's own turn (``FuturesService.call``), so nothing races the event pipeline. The answers are
plain text that labels each piece of knowledge (``futures.reports``): the brain keeps those labels' meaning when it
answers, so a correlation is never passed off as a cause.

Deleting what she learned, and loosening a control the user set, is asked first; tightening one never is.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from octavia.futures.privacy import FORGET_ASKS, LOOSENING
from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_UNAVAILABLE = "Octavia Futures isn't running (futures.enabled is off, or it couldn't start)."

Topic = Literal[
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
]
When = Literal[
    "app_opened",
    "email_from",
    "email_about",
    "build_succeeded",
    "build_failed",
    "task_completed",
    "timer_done",
    "assignment_posted",
    "grade_posted",
]
Forgettable = Literal["hypothesis", "reminder", "episode", "prediction", "goal", "source", "domain", "everything"]
ControlAction = Literal[
    "status",
    "proactive_off",
    "proactive_on",
    "off_limits",
    "allow_domain",
    "no_experiments",
    "allow_experiments",
    "mute",
    "unmute",
]
GoalAction = Literal[
    "list", "add", "done", "drop", "confirm", "decline", "progress", "blocked", "unblocked", "waiting_on"
]
ExperimentAction = Literal["list", "offer", "approve", "stop", "reject"]


def _service(ctx: ToolContext) -> Any:
    from octavia.futures.engine import FuturesService

    service = ctx.extras.get("futures")
    return service if isinstance(service, FuturesService) else None


def _run_error(exc: Exception) -> ToolResult:
    return ToolResult.error(str(exc) or type(exc).__name__)


def _one_yes(scope: str, reason: str) -> RiskFinding:
    """A question of its own for this very call: a yes to forgetting one hypothesis doesn't also cover forgetting
    everything, the next call in the same turn."""
    return RiskFinding(risk=RiskLevel.CONFIRM, scope=scope, reason=reason)


# ---------------------------------------------------------------------------------------------- reading the model
class ReportArgs(ToolArgs):
    topic: Topic = Field(
        description=(
            "unsure: weak-evidence patterns; missed_deadlines: why they miss them; failures: yours; deadlines: "
            "which moved today (what's due: TickTick/Canvas); offers: how your offers of help went; self: how your "
            "replies went (thanked, corrected, cut short...), your experiments on yourself and the habits you kept "
            '(for "how am I doing?").'
        )
    )
    limit: int = Field(default=5, ge=1, le=20)


class FuturesReport(Tool):
    name = "futures_report"
    description = (
        "What Octavia Futures (your evidence-based model of the user and yourself) holds on a topic. Keep each "
        "line's knowledge label, never call a correlation a cause, and say plainly when evidence is thin."
    )
    Args = ReportArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: ReportArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.reports import report

        try:
            text = await service.call(report, service, args.topic, args.limit)
        except ValueError as exc:
            return _run_error(exc)
        if args.topic == "self":
            text += _overnight_guesses(ctx)
        return ToolResult.success(text)


def _overnight_guesses(ctx: ToolContext) -> str:
    """How well she guesses the user's answers to her overnight work (``proactive.learn``): part of her own record."""
    from octavia.proactive import learn
    from octavia.proactive.service import ProactiveService

    proactive = ctx.extras.get("proactive")
    if not isinstance(proactive, ProactiveService):
        return ""
    try:
        score = learn.agreement(proactive.store.items)
    except Exception:
        return ""
    return f"\nOvernight work: {score.describe()}."


class ExplainArgs(ToolArgs):
    what: str = Field(
        default="latest",
        max_length=40,
        description=("'latest', or kind + number: 'recommendation 5', 'prediction 3', 'hypothesis 2', 'experiment 1'."),
    )


class FuturesExplain(Tool):
    name = "futures_explain"
    description = "Why you said, predicted or recommended something: its evidence, goals and assumptions."
    Args = ExplainArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: ExplainArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.reports import explain_target

        try:
            return ToolResult.success(await service.call(explain_target, service, args.what))
        except ValueError as exc:
            return _run_error(exc)


class CompareArgs(ToolArgs):
    hours: float = Field(ge=0.25, le=24, description="Time the user has.")
    options: list[str] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Options the user named, short ('the physics lab', 'rest'); empty = build them from open work and goals."
        ),
    )
    focus: str = Field(default="", max_length=120, description="What matters most to them, if said.")


class FuturesCompare(Tool):
    name = "futures_compare"
    description = (
        "Compare ways the user could spend their time ('I have four hours tonight, what should I do?', 'what if I "
        "do A instead of B?'). Present possibilities, never guarantees."
    )
    Args = CompareArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: CompareArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.reports import comparison_text, comparison_trace

        scenario, result = await service.compare(args.hours, [o[:80] for o in args.options], args.focus)
        if result.error:
            return ToolResult.error(f"Couldn't compare the options: {result.error}")
        branches = list(result.data.get("branches") or [])
        text = result.text or comparison_text(branches, scenario)
        rec_id = await service.call(
            service.record_answer, "comparison", text, comparison_trace(branches, scenario, result.provider)
        )
        return ToolResult.success(
            text + f"\n(Worked out by: {result.provider}. futures_explain 'recommendation {rec_id}' says why.)",
            data={"branches": len(branches), "recommendation": rec_id},
        )


class WeekArgs(ToolArgs):
    first: str = Field(
        default="",
        max_length=80,
        description="What they'd start with, to compare ('the essay'). Empty: soonest due first.",
    )
    free_hours_per_day: float | None = Field(
        default=None, ge=0, le=16, description="If they said how much time they have."
    )
    estimates: dict[str, float] = Field(
        default_factory=dict,
        description="Their own estimates: a few words of the work's name to hours ('lab report': 3).",
    )


class WeekOutlook(Tool):
    name = "week_outlook"
    description = (
        "Thousands of sampled ways the coming week could go, from what's due and how long such work takes: which "
        "deadlines are at risk, and what starting one thing first does to the rest ('if I start the essay tonight, is "
        "Thursday's lab okay?'). For planning the week and deadline talk. Plain code: it costs nothing. Ask them how "
        "long things will take when it says the hours are guesses."
    )
    Args = WeekArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: WeekArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        return ToolResult.success(await service.week_outlook(args.first, args.free_hours_per_day, args.estimates))


class WorkHoursArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=80, description="What the work was.")
    hours: float = Field(gt=0, le=200, description="How long it took, as they said.")


class NoteWorkHours(Tool):
    name = "note_work_hours"
    description = (
        "Keep how long a piece of work really took, when they say ('that lab took me three hours'). The week's outlook "
        "draws on these instead of guessing."
    )
    Args = WorkHoursArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: WorkHoursArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        await service.call(service.note_work_hours, args.title, args.hours)
        return ToolResult.success(f"Kept: {args.title} took {args.hours:g} h.")


# ---------------------------------------------------------------------------------------------- remind me when
class RemindWhenArgs(ToolArgs):
    when: When = Field(description=("build_*: your self-improvement builds; assignment_posted/grade_posted: Canvas."))
    value: str = Field(
        default="",
        max_length=80,
        description="The app, person, subject, to-do or course, e.g. 'Blender'.",
    )
    message: str = Field(min_length=1, max_length=300, description="What to tell them then.")
    once: bool = Field(default=True, description="False = every time.")
    days: int = Field(default=30, ge=1, le=365, description="Expire after this many days.")


class RemindMeWhen(Tool):
    name = "remind_me_when"
    description = (
        "Tell the user something when an event happens ('when Professor Smith replies, remind me to update the "
        "assignment'). For a clock time, make a TickTick task."
    )
    Args = RemindWhenArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def inspect_risk(self, args: RemindWhenArgs, ctx: ToolContext) -> RiskFinding | None:
        """She'll later say it as something the user asked for, even with her own remarks switched off: so it's
        asked first when it didn't come straight from the user's own turn (a scheduled job), or when the turn took in
        text from outside (a web page, an email) that could have planted it."""
        turn = ctx.extras.get("turn")
        info = turn() if callable(turn) else None
        service = _service(ctx)
        if service is not None and info is not None:
            await service.settle()  # the turn's tool calls so far are taken in
            outside = service.normalizer.read_outside(info.id)
        else:
            outside = False
        if info is None or info.outside_content or outside:
            return RiskFinding(risk=RiskLevel.CONFIRM, scope="remind_me_when", reason="you'd hear it later as your own")
        return None

    async def describe_call(self, args: RemindWhenArgs, ctx: ToolContext) -> str:
        from octavia.futures.prospective import describe

        return f'remember to tell you {describe(args.when, args.value)}: "{args.message}"'

    async def run(self, args: RemindWhenArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.prospective import CONDITION_KINDS, condition
        from octavia.futures.schema import event_type

        try:
            condition(args.when, args.value)
        except ValueError as exc:
            return _run_error(exc)
        from octavia.futures.privacy import classify_domain

        source = _WATCHED_BY.get(args.when)
        if source and not service.controls.allows_source(source):
            return ToolResult.error(f"I don't record {source} (futures.exclude_sources), so I'd never see that happen.")
        about = classify_domain(args.value) if args.when == "task_completed" else None
        if about is not None and not service.controls.allows_learning(about):
            return ToolResult.error(f"{about} is off-limits, so I'd never see that to-do.")
        for kind in CONDITION_KINDS[args.when][0]:
            domain = event_type(kind).domain.value
            if not service.controls.allows_learning(domain):
                return ToolResult.error(f"{domain} is off-limits, so I'd never see that happen.")
        trigger = await service.call(
            service.add_trigger, args.when, args.value, args.message, once=args.once, days=args.days
        )
        note = _unseen(args.when, service)
        return ToolResult.success(
            f"Okay, #{trigger.id}: I'll tell you {trigger.description}.{note}", data={"id": trigger.id}
        )


# Which source's events each kind of "remind me when" waits for.
_WATCHED_BY = {
    "email_from": "gmail",
    "email_about": "gmail",
    "assignment_posted": "canvas",
    "grade_posted": "canvas",
    "task_completed": "ticktick",
    "timer_done": "timers",
    "build_succeeded": "evolve",
    "build_failed": "evolve",
    "app_opened": "computer",
}


def _unseen(when: str, service: Any) -> str:
    """A caveat when what the reminder waits for can't be seen right now (the watch that would see it is off)."""
    if when in ("build_succeeded", "build_failed") and not service.config.self_improvement.enabled:
        return " (Self-improvement is off, so there are no builds to watch until it's on.)"
    if when == "app_opened" and not service.settings.context_watch:
        return " (Only when I open it for you: noticing the app in front is off, futures.context_watch.)"
    if when in ("email_from", "email_about"):
        try:
            from octavia.tools.extensions.gmail import WATCHER, credentials

            if credentials() is None and not WATCHER.accounts:
                return " (Gmail isn't connected, so I won't see new mail until it is.)"
            if not WATCHER.enabled:
                return " (Email alerts are off, so I won't see new mail until they're back on.)"
        except Exception:
            return ""
    if when in ("assignment_posted", "grade_posted"):
        from octavia.tools.extensions.canvas import canvas_token

        if not service.settings.canvas_watch or not canvas_token():
            return " (I'm not watching Canvas right now: it needs futures.canvas_watch and a Canvas token.)"
    return ""


# ---------------------------------------------------------------------------------------------- the user's say
class ForgetArgs(ToolArgs):
    what: Forgettable = Field(
        description=("Single items need id; source: e.g. 'gmail', 'canvas'; domain: e.g. 'health', 'school'.")
    )
    id: int | None = Field(default=None, description="For a single item.")
    name: str = Field(default="", max_length=40, description="The source or domain.")


class FuturesForget(Tool):
    name = "futures_forget"
    description = (
        "Forget part of what Octavia Futures holds ('forget that hypothesis', 'cancel that reminder', 'forget "
        "everything about my health'). A forgotten hypothesis isn't re-formed from the same data."
    )
    Args = ForgetArgs
    risk = RiskLevel.SAFE  # cancelling a reminder; everything else asks first (assess_risk)
    category = "futures"

    def assess_risk(self, args: ForgetArgs) -> RiskLevel:
        return RiskLevel.CONFIRM if args.what in FORGET_ASKS else RiskLevel.SAFE

    async def inspect_risk(self, args: ForgetArgs, ctx: ToolContext) -> RiskFinding | None:
        if args.what not in FORGET_ASKS:
            return None
        return _one_yes(f"{args.what}:{args.id if args.id is not None else args.name}", "can't be undone")

    async def describe_call(self, args: ForgetArgs, ctx: ToolContext) -> str:
        if args.what == "everything":
            return "forget everything I've learned about you (your settings stay)"
        if args.what in ("source", "domain"):
            return f"forget everything I have {'from' if args.what == 'source' else 'about'} {args.name or 'that'}"
        service = _service(ctx)
        label = ""
        if service is not None and args.id is not None and args.what == "hypothesis":
            h = await service.call(service.hypotheses.get, args.id)
            label = f' "{h.statement}"' if h else ""
        return f"forget {args.what} number {args.id}{label}"

    async def run(self, args: ForgetArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.reports import forget

        try:
            return ToolResult.success(await service.call(forget, service, args.what, number=args.id, name=args.name))
        except ValueError as exc:
            return _run_error(exc)


class ControlsArgs(ToolArgs):
    action: ControlAction = Field(
        description=(
            "proactive_*: speaking up unprompted; off_limits: never learn about or raise a domain; "
            "no_experiments/allow_experiments: a domain, or all; mute/unmute: a kind of remark (empty = the last one)."
        )
    )
    domain: str = Field(
        default="",
        max_length=30,
        description="assistant, general, computer, productivity, school, communications, time, goals, routines, "
        "self, relationships, health, finance, employment, people",
    )
    kind: str = Field(default="", max_length=30, description="e.g. heads_up")


class FuturesControls(Tool):
    name = "futures_controls"
    description = (
        "The user's privacy controls for Octavia Futures ('stop being proactive', 'don't learn about my health', "
        "'stop telling me about deadlines'). Tightening needs no question; loosening is asked."
    )
    Args = ControlsArgs
    risk = RiskLevel.SAFE
    category = "futures"

    def assess_risk(self, args: ControlsArgs) -> RiskLevel:
        return RiskLevel.CONFIRM if args.action in LOOSENING else RiskLevel.SAFE

    async def inspect_risk(self, args: ControlsArgs, ctx: ToolContext) -> RiskFinding | None:
        if args.action not in LOOSENING:
            return None
        return _one_yes(f"{args.action}:{args.domain or args.kind}", "undoes a limit you set")

    async def describe_call(self, args: ControlsArgs, ctx: ToolContext) -> str:
        return {
            "proactive_on": "start speaking up on my own again",
            "allow_domain": f"let myself learn about {args.domain or 'that'} again",
            "allow_experiments": f"allow experiments {'with ' + args.domain if args.domain else 'again'}",
            "unmute": f"unmute my {(args.kind or 'last kind of').replace('_', ' ')} remarks",
        }.get(args.action, f"change a Futures setting ({args.action})")

    async def run(self, args: ControlsArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.reports import control

        try:
            text = await service.call(control, service, args.action, domain=args.domain, kind=args.kind)
        except ValueError as exc:
            return _run_error(exc)
        return ToolResult.success(text)


class GoalArgs(ToolArgs):
    action: GoalAction = Field(
        description=(
            "add: one the user states; done/drop/progress, confirm/decline (one you asked about): by id; "
            "blocked/unblocked/waiting_on: a project."
        )
    )
    text: str = Field(default="", max_length=200, description="The goal, or the project.")
    id: int | None = None
    on: str = Field(default="", max_length=80, description="What/who it's blocked or waiting on.")


class FuturesGoal(Tool):
    name = "futures_goal"
    description = (
        "The user's goals and priorities ('I care about getting my GPA up'), and blocked or waiting projects. Plan "
        "only around what they said or confirmed, never what you inferred."
    )
    Args = GoalArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: GoalArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.reports import goal

        try:
            return ToolResult.success(
                await service.call(goal, service, args.action, text=args.text, number=args.id, on=args.on)
            )
        except ValueError as exc:
            return _run_error(exc)


class ExperimentArgs(ToolArgs):
    action: ExperimentAction = Field(
        description=("offer: propose and explain one; approve: start it, ONLY after the user clearly agreed.")
    )
    id: int | None = Field(default=None, description="For approve, stop, reject.")
    template: Literal["task_nudge"] = Field(
        default="task_nudge", description="Does a heads-up before a to-do is due help finish it?"
    )


class FuturesExperiment(Tool):
    name = "futures_experiment"
    description = (
        "Small personal experiments that tell cause from coincidence. Nothing starts without the user's clear yes; "
        "never experiment with people, relationships, health, money, school work or communications."
    )
    Args = ExperimentArgs
    risk = RiskLevel.SAFE
    category = "futures"

    def assess_risk(self, args: ExperimentArgs) -> RiskLevel:
        return RiskLevel.CONFIRM if args.action == "approve" else RiskLevel.SAFE

    async def inspect_risk(self, args: ExperimentArgs, ctx: ToolContext) -> RiskFinding | None:
        if args.action != "approve":
            return None
        return _one_yes(f"approve:{args.id}", "changes what I do for a few weeks")

    async def describe_call(self, args: ExperimentArgs, ctx: ToolContext) -> str:
        return f"start experiment number {args.id}" if args.action == "approve" else f"{args.action} an experiment"

    async def run(self, args: ExperimentArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.reports import experiment

        try:
            return ToolResult.success(
                await service.call(experiment, service, args.action, number=args.id, template=args.template)
            )
        except ValueError as exc:
            return _run_error(exc)


class FeedbackArgs(ToolArgs):
    verdict: Literal["helpful", "not_helpful", "stop_this_kind"] = Field(
        description="stop_this_kind also mutes that kind for 30 days."
    )
    id: int | None = Field(default=None, description="Omit for the last thing you said.")


class FuturesFeedback(Tool):
    name = "futures_feedback"
    description = "Record how the user took something you said unprompted: 'that was helpful', 'stop telling me that'."
    Args = FeedbackArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: FeedbackArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        response = "accepted" if args.verdict == "helpful" else "rejected"
        rec = await service.call(service.respond, args.id, response)
        if rec is None:
            return ToolResult.error("There's nothing of mine to give feedback on yet.")
        text = f"Noted ({args.verdict.replace('_', ' ')}) for: {rec.text}"
        if args.verdict == "stop_this_kind" and rec.kind == "trigger" and (rec.subject or "").startswith("trigger:"):
            # One reminder the user set: stop that one, not every reminder they ever asked for.
            trigger_id = int(rec.subject.split(":", 1)[1])
            cancelled = await service.call(service.triggers.cancel, trigger_id)
            text += " I've cancelled that reminder." if cancelled else " That reminder was already done."
        elif args.verdict == "stop_this_kind":
            from octavia.futures.reports import control

            text += " " + await service.call(control, service, "mute", kind=rec.kind)
        return ToolResult.success(text)


class HabitArgs(ToolArgs):
    action: Literal["list", "drop", "restore"] = "list"
    id: int | None = Field(default=None, description="The habit's number. drop without one: the newest active habit.")


class MyHabits(Tool):
    name = "my_habits"
    description = (
        "The habits you kept from your experiments on yourself (like keeping answers short when the user sounds "
        'stressed), each with the result it came from. drop stops one at once ("drop that habit", "stop doing '
        'that"); restore brings a dropped one back.'
    )
    Args = HabitArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: HabitArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)
        from octavia.futures.reports import habit

        try:
            return ToolResult.success(await service.call(habit, service, args.action, number=args.id))
        except ValueError as exc:
            return _run_error(exc)


class PastCasesArgs(ToolArgs):
    about: str = Field(description="What the situation is, in plain words")


class PastCases(Tool):
    name = "past_cases"
    description = (
        "What you did before in situations like this one, and how each ended (worked, corrected, failed, unfinished), "
        "from your own record. Cases that went wrong come up on their own; use this to look further before something "
        'costly. futures_forget what="lesson" drops one that no longer holds.'
    )
    Args = PastCasesArgs
    risk = RiskLevel.SAFE
    category = "futures"

    async def run(self, args: PastCasesArgs, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        if service is None:
            return ToolResult.error(_UNAVAILABLE)

        def look() -> str:
            now = service.clock()
            cases = service.lessons.similar(args.about, now)[:8]
            return "\n".join(f"#{c.id} {c.describe(now)}" for c in cases) or "No cases like that on record."

        return ToolResult.success(await service.call(look))


TOOLS = [
    WeekOutlook,
    NoteWorkHours,
    PastCases,
    FuturesReport,
    FuturesExplain,
    FuturesCompare,
    RemindMeWhen,
    FuturesForget,
    FuturesControls,
    FuturesGoal,
    FuturesExperiment,
    FuturesFeedback,
    MyHabits,
]
