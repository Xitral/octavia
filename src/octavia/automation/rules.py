"""A rule: when an event like this happens, under these conditions, do these actions.

The pattern is an event type, or a glob over types ("build.*"). Conditions compare the event's fields; a time window
limits when it applies; ``max_age_s`` refuses stale data (an event whose ``observed_at``, or else its own time, is
older than that when the rule looks at it). Each action is one of: a tool call, another saved rule (so skills compose:
its conditions are checked against the same event, then its actions run), or a line she says.

``classify`` sorts what an action would do into the kinds the user cares about: only read-only and reversible local
actions run unattended once the user turned the rule on; the rest are asked at run time, and sending or drafting
email is never allowed in a rule at all.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from octavia.tools.base import RiskLevel

if TYPE_CHECKING:
    from octavia.automation.events import BusEvent
    from octavia.tools.base import Tool

MAX_DEPTH = 3  # rules in one chain: a rule's action may set off another rule, at most this many deep
MAX_ACTIONS = 5
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Op = Literal["eq", "ne", "contains", "not_contains", "gt", "gte", "lt", "lte", "exists"]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Condition(_Spec):
    field: str = Field(min_length=1, max_length=60, description="An event field, e.g. 'sender' or 'stage'.")
    op: Op = "eq"
    value: str | float | bool | None = Field(default=None, description="Not needed for exists.")


class Action(_Spec):
    tool: str = Field(default="", max_length=60, description="A tool to call.")
    args: dict[str, Any] = Field(default_factory=dict, description="The tool's arguments.")
    rule: int | None = Field(default=None, description="Or: run another saved rule (by id).")
    say: str = Field(
        default="", max_length=300, description="Or: a line to say; {field} puts in an event field, e.g. {sender}."
    )

    @model_validator(mode="after")
    def _one_kind(self) -> Action:
        if sum((bool(self.tool), self.rule is not None, bool(self.say))) != 1:
            raise ValueError("each action is exactly one of: tool, rule or say")
        if self.args and not self.tool:
            raise ValueError("args go with a tool")
        return self


class Window(_Spec):
    start: str = Field(default="", description="HH:MM; empty = any time.")
    end: str = Field(default="", description="HH:MM; may be past midnight (22:00-07:00).")
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        default_factory=list, description="Empty = every day."
    )

    @field_validator("start", "end")
    @classmethod
    def _clock(cls, value: str) -> str:
        if value and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("times are HH:MM")
        return value

    @model_validator(mode="after")
    def _both(self) -> Window:
        if bool(self.start) != bool(self.end):
            raise ValueError("a time window needs both start and end")
        return self

    def allows(self, ts: float) -> bool:
        at = datetime.fromtimestamp(ts)
        if self.days and DAYS[at.weekday()] not in self.days:
            return False
        if not self.start:
            return True
        now = at.hour * 60 + at.minute
        start, end = (_minutes(t) for t in (self.start, self.end))
        return start <= now < end if start <= end else now >= start or now < end

    @property
    def always(self) -> bool:
        return not self.start and not self.days

    def describe(self) -> str:
        parts = []
        if self.start:
            parts.append(f"between {self.start} and {self.end}")
        if self.days:
            parts.append("on " + ", ".join(self.days))
        return " ".join(parts)


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass
class Rule:
    id: int
    name: str
    event: str  # an event type, or a glob over types
    conditions: list[Condition] = field(default_factory=list)
    window: Window = field(default_factory=Window)
    max_age_s: float | None = None
    actions: list[Action] = field(default_factory=list)
    enabled: bool = False  # the user turned it on (authorized it); a new rule waits for that, after its dry run
    max_per_hour: int = 6
    created_at: float = 0.0
    authorized_at: float | None = None
    fire_count: int = 0
    last_fired: float | None = None

    def describe(self) -> str:
        when = f"when {self.event}"
        if self.conditions:
            when += " if " + " and ".join(describe_condition(c) for c in self.conditions)
        if not self.window.always:
            when += " " + self.window.describe()
        if self.max_age_s:
            when += f" (data under {_duration(self.max_age_s)} old)"
        return f"{when}: " + "; then ".join(describe_action(a) for a in self.actions)


def _duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)} h"
    return f"{round(seconds / 60)} min"


def describe_condition(cond: Condition) -> str:
    words = {
        "eq": "is",
        "ne": "isn't",
        "contains": "contains",
        "not_contains": "doesn't contain",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }
    if cond.op == "exists":
        return f"{cond.field} is set"
    return f"{cond.field} {words[cond.op]} {cond.value!r}"


def describe_action(action: Action) -> str:
    if action.say:
        return f'say "{action.say}"'
    if action.rule is not None:
        return f"run rule #{action.rule}"
    args = ", ".join(f"{k}={v!r}" for k, v in action.args.items())
    return f"{action.tool}({args})"


# ------------------------------------------------------------------------------------------------------- matching
def type_matches(pattern: str, event_type: str) -> bool:
    return fnmatch.fnmatchcase(event_type, pattern)


def check_condition(cond: Condition, event: BusEvent) -> bool:
    actual = event.get(cond.field)
    if cond.op == "exists":
        return actual is not None and actual != ""
    if actual is None:
        return cond.op in ("ne", "not_contains")
    wanted = cond.value
    if cond.op in ("gt", "gte", "lt", "lte"):
        try:
            a, w = float(actual), float(wanted)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return {"gt": a > w, "gte": a >= w, "lt": a < w, "lte": a <= w}[cond.op]
    a_text, w_text = _text(actual), _text(wanted)
    if cond.op in ("contains", "not_contains"):
        found = w_text in a_text
        return found if cond.op == "contains" else not found
    same = a_text == w_text
    return same if cond.op == "eq" else not same


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).casefold().split())


def unmet(rule: Rule, event: BusEvent, now: float, *, pattern: bool = True) -> str | None:
    """Why ``rule`` doesn't apply to ``event`` seen at ``now``, or None when it does. ``pattern`` False skips the
    event type (a rule run as another rule's action)."""
    if pattern and not type_matches(rule.event, event.type):
        return "another kind of event"
    for cond in rule.conditions:
        if not check_condition(cond, event):
            return f"not {describe_condition(cond)}"
    if not rule.window.allows(event.ts):
        return "outside its time window"
    if rule.max_age_s is not None:
        observed = event.get("observed_at")
        at = float(observed) if isinstance(observed, int | float) else event.ts
        if now - at > rule.max_age_s:
            return "the data was stale"
    return None


_FIELD = re.compile(r"\{([a-z_][a-z0-9_.]*)\}", re.IGNORECASE)


def fill(text: str, event: BusEvent) -> str:
    """``text`` with each ``{field}`` replaced by the event's value (left as it is when the event hasn't one)."""

    def value(m: re.Match[str]) -> str:
        found = event.get(m.group(1))
        return m.group(0) if found is None else str(found)[:200]

    return _FIELD.sub(value, text)


# ------------------------------------------------------------------------------------------------- classification
class ActionClass(str, Enum):
    READ_ONLY = "read-only"
    LOCAL_CHANGE = "local change"
    EXTERNAL_COMMS = "external comms"
    DESTRUCTIVE = "destructive"
    FINANCIAL = "financial"
    NEW_PERMISSION = "new permission"


UNATTENDED = frozenset({ActionClass.READ_ONLY, ActionClass.LOCAL_CHANGE})

# Tools a rule may never call: sending or drafting email (never from a rule, asked or not), and the rule tools
# themselves (a rule that writes rules is how a loop outlives its kill switch).
NEVER_EMAIL = frozenset({"send_gmail", "reply_gmail", "create_gmail_draft"})
RULE_TOOLS = frozenset({"create_rule", "dry_run_rule", "pause_rule", "delete_rule"})
_EMAIL_WRITE = re.compile(r"(mail|email).*(send|reply|draft|compose)|(send|reply|draft|compose).*(mail|email)")

_FINANCIAL = {"buy", "purchase", "pay", "payment", "order", "checkout", "donate", "subscribe", "transfer", "tip"}
_PERMISSION = {"connect", "setup", "controls", "settings", "authorize", "install", "allow", "permission", "enter"}
_DESTRUCTIVE = {
    "delete",
    "forget",
    "remove",
    "cancel",
    "close",
    "kill",
    "shut",
    "disconnect",
    "reject",
    "undo",
    "wipe",
    "reset",
    "clear",
    "merge",
}
_EXTERNAL = {"send", "reply", "post", "share", "publish", "message", "web", "discord", "phone", "sms", "tweet"}
_READ_FIRST = {
    "get",
    "list",
    "read",
    "search",
    "check",
    "look",
    "describe",
    "find",
    "recall",
    "identify",
    "see",
    "zoom",
    "point",
    "explain",
    "recent",
}
_READ_LAST = {
    "status",
    "report",
    "outline",
    "history",
    "due",
    "vitals",
    "activities",
    "logs",
    "health",
    "projects",
    "deployments",
    "domains",
    "tables",
    "usage",
    "grades",
    "announcements",
    "material",
    "events",
    "notifications",
    "assignment",
    "time",
    "weather",
    "directions",
    "result",
    "rules",
    "messages",
}


def forbidden(name: str) -> str | None:
    """Why no rule may call tool ``name``, or None."""
    if name in NEVER_EMAIL or _EMAIL_WRITE.search(name):
        return "rules never send or draft email"
    if name in RULE_TOOLS:
        return "a rule can't change rules"
    return None


def classify(tool: Tool) -> ActionClass:
    """What calling ``tool`` would do, from its name and declared risk. When in doubt it lands in the class that
    asks: a confirm-level tool is never read-only."""
    words = set(tool.name.lower().split("_"))
    if words & _FINANCIAL:
        return ActionClass.FINANCIAL
    if words & _PERMISSION:
        return ActionClass.NEW_PERMISSION
    if words & _DESTRUCTIVE:
        return ActionClass.DESTRUCTIVE
    first, last = tool.name.split("_")[0], tool.name.split("_")[-1]
    if tool.risk is RiskLevel.SAFE and (first in _READ_FIRST or last in _READ_LAST) and not words & {"send", "web"}:
        return ActionClass.READ_ONLY
    if words & _EXTERNAL:
        return ActionClass.EXTERNAL_COMMS
    return ActionClass.LOCAL_CHANGE


def runs_unattended(tool: Tool) -> bool:
    """Whether a rule the user turned on may call ``tool`` without asking: only read-only and reversible local
    actions, from a tool that doesn't ask for its calls anyway."""
    return tool.risk is RiskLevel.SAFE and classify(tool) in UNATTENDED
