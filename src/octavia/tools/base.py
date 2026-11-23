"""Tool primitives.

A tool is a small class with a name, a description the model reads, a pydantic
model describing its arguments, and a risk level. Tools never check
permissions themselves: every call goes through ``ToolExecutor``, which
validates arguments, applies the ``PermissionPolicy`` and asks for confirmation
when needed. That keeps safety logic in one place. (A tool may still say, in
``precheck``, that a call can't work right now, so the user isn't asked first.)

Writing a tool::

    class OpenUrlArgs(ToolArgs):
        url: str = Field(description="An http(s) URL")

    class OpenUrl(Tool):
        name = "open_url"
        description = "Open a web page in the default browser."
        Args = OpenUrlArgs
        risk = RiskLevel.SAFE

        async def run(self, args: OpenUrlArgs, ctx: ToolContext) -> ToolResult:
            ...
            return ToolResult.success("Opened it.")
"""

from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.core.events import EventBus
    from octavia.memory.base import LongTermMemory, ToolStateStore


class RiskLevel(str, Enum):
    """How dangerous a tool call is. The permission policy maps these to decisions."""

    SAFE = "safe"  # read-only or trivially reversible (open an app, read status)
    CONFIRM = "confirm"  # has side effects the user should approve (send, delete, close)
    RESTRICTED = "restricted"  # dangerous; denied unless explicitly enabled per tool


_RISK_ORDER = {RiskLevel.SAFE: 0, RiskLevel.CONFIRM: 1, RiskLevel.RESTRICTED: 2}


def max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if _RISK_ORDER[a] >= _RISK_ORDER[b] else b


class ToolArgs(BaseModel):
    """Base class for tool arguments. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoArgs(ToolArgs):
    pass


@dataclass
class ToolResult:
    ok: bool
    content: str  # what the model sees
    data: dict[str, Any] | None = None  # optional structured payload (for UI/logging)

    @classmethod
    def success(cls, content: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls(ok=True, content=content, data=data)

    @classmethod
    def error(cls, content: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls(ok=False, content=content, data=data)


@dataclass(frozen=True)
class RiskFinding:
    """What a look at the world says about one call (``Tool.inspect_risk``): its risk, and for the question the
    user is asked, what kind of thing it is and why it's risky.

    ``scope`` names the kind of risky thing ("send", "delete"): once the user says yes to one, the rest of the
    same task's calls of this tool with the same scope go ahead without asking again. ``reason`` finishes the
    question "Want me to <description>, which <reason>?", e.g. "sends a message". ``place`` says where it would
    act, when that matters for a standing approval (the window: "Vivaldi.exe#132456"), else ""; ``window`` that
    window's title ("Exercise | DataCamp - Vivaldi"), for the sites the user lets her submit on. ``like`` names
    another tool when what's risky is exactly that tool's own call made along the way (drag's picture afterwards is a
    look_at_screen): it's then decided by the user's rule for that tool, so it asks only when that tool would.
    """

    risk: RiskLevel
    scope: str = ""
    reason: str = ""
    place: str = ""
    window: str = ""
    like: str = ""


@dataclass(frozen=True)
class TurnInfo:
    """The user's turn that's running right now, as tools see it (``ctx.extras["turn"]()``; None between turns).

    ``id`` changes with every turn; ``interrupted`` is set once the user cut it off (a new request, the stop
    hotkey), when a long run of actions should stop; ``outside_content`` once the turn has taken in text from
    outside, such as a web page, which could try to steer what happens next; ``remote`` when the user typed it
    somewhere else (a text from their phone, over the Discord bridge): it's theirs, but they can't see the screen,
    so nothing they trust her with at the PC (``permissions.trusted_apps``) skips a question.
    """

    id: int
    interrupted: bool = False
    outside_content: bool = False
    remote: bool = False


@dataclass
class ToolContext:
    """Services a tool may use. Passed to every ``Tool.run`` call.

    ``extras`` holds what only some tools need, set up by the app: "avatar" (the desktop avatar, when it runs),
    "turn" (a callable returning the running user turn's ``TurnInfo``, or None), "speak" (an async callable that
    says a line now, inside the running turn, and returns once it's been heard: True, or False when it couldn't
    be said) and "evolve" (the self-improvement manager).
    """

    config: AppConfig
    events: EventBus
    state: ToolStateStore | None = None
    memory: LongTermMemory | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Args: ClassVar[type[ToolArgs]] = NoArgs
    risk: ClassVar[RiskLevel] = RiskLevel.SAFE
    category: ClassVar[str] = "general"
    timeout_s: ClassVar[float] = 20.0
    # Arguments whose values stay out of the audit log, tool events and confirmation requests (their length is
    # shown instead): what she types may be a password.
    private_args: ClassVar[frozenset[str]] = frozenset()

    def assess_risk(self, args: ToolArgs) -> RiskLevel:
        """Risk of this specific call. Override for argument-dependent risk.

        The executor never lets a call be *less* risky than the class-level
        ``risk``; overriding can only escalate.
        """
        return self.risk

    async def inspect_risk(self, args: Any, ctx: ToolContext) -> RiskFinding | None:
        """Risk of this call judged from the world, not just its arguments: what's under the mouse, what has the
        keyboard focus. None (the default) when there's nothing to add.

        Awaited by the executor after the permission policy allowed the tool at all (a blocked tool runs none of
        its code) and before any question. Like ``assess_risk`` it can only raise the risk; if it fails, the call
        is treated as needing the user's yes.
        """
        return None

    async def describe_call(self, args: Any, ctx: ToolContext) -> str:
        """Short human phrase for confirmation prompts, e.g. "close Spotify".

        It is read aloud as "Want me to <description>?", so override it with
        natural wording for any tool that can require confirmation. The default names the tool in words and leaves
        the arguments to ``call_detail``: raw values read aloud aren't a question anyone can answer.
        """
        return f"use my {self.name.replace('_', ' ')} tool"

    def call_detail(self, args: Any) -> str:
        """The exact call, for a screen next to the question (the console, her chat window, a text), never said out
        loud: e.g. the command line ``run_command`` would run. "" when the description already says it all.

        The default shows the arguments when ``describe_call`` isn't overridden, since its words leave them out."""
        if type(self).describe_call is not Tool.describe_call:
            return ""
        shown = args.model_dump(exclude_none=True)
        return ", ".join(f"{k}={v!r}" for k, v in shown.items() if k not in self.private_args)

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        """Why this call can't go ahead right now, or None (the default) when it can.

        The executor asks after validating the arguments and before asking the user to confirm: a message
        becomes the call's error, so the user isn't asked a question whose "yes" is sure to fail. It's only a
        courtesy: things can change while the user answers, so ``run`` must still check for itself.
        """
        return None

    @abstractmethod
    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        """Do the work. Raise or return ``ToolResult.error`` on failure."""

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        """JSON schema for the arguments, inlined so any LLM backend can consume it."""
        return _inline_schema(cls.Args.model_json_schema())

    @classmethod
    def spec(cls) -> ToolSpec:
        from octavia.core import home

        schema = cls.input_schema()
        if "{{home" in json.dumps(schema):
            schema = json.loads(home.render(json.dumps(schema)))
        return ToolSpec(
            name=cls.name, description=home.render(cls.description), input_schema=schema, category=cls.category
        )


@dataclass(frozen=True)
class ToolSpec:
    """Backend-neutral tool description handed to brain providers."""

    name: str
    description: str
    input_schema: dict[str, Any]
    category: str = ""  # the tool's group ("media", "school"...): a brain that loads tools on demand loads by group


_NULL = {"type": "null"}


def _drop_optional_null(node: dict[str, Any]) -> dict[str, Any]:
    """``x: T | None = None`` as plain ``T``: leaving it out already means None, so the null branch and the
    ``"default": null`` only cost the model tokens on every request (validation still takes an explicit null).
    An empty-string default goes too: an optional text left out is empty, which the model assumes anyway."""
    if node.get("default") == "" and node.get("type") == "string":
        return {k: v for k, v in node.items() if k != "default"}
    if "default" not in node or node["default"] is not None:
        return node
    rest = {k: v for k, v in node.items() if k != "default"}
    branches = rest.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2 and _NULL in branches:
        other = branches[1 - branches.index(_NULL)]
        if isinstance(other, dict):
            rest.pop("anyOf")
            return {**other, **rest}
    return rest


def _dedupe_items(node: dict[str, Any]) -> dict[str, Any]:
    """A list of objects with the same fields as the object holding it (drag's ``then``): each repeated field's
    description is said once, on the outer field."""
    props = node.get("properties")
    if not isinstance(props, dict):
        return node
    for prop in props.values():
        inner = prop.get("items", {}).get("properties") if isinstance(prop, dict) else None
        if not isinstance(inner, dict) or not isinstance(prop["items"], dict):
            continue
        for name, sub in inner.items():
            outer = props.get(name)
            if isinstance(sub, dict) and isinstance(outer, dict) and sub.get("description") == outer.get("description"):
                sub.pop("description", None)
    return node


def _inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$defs`` references; drop pydantic's cosmetic titles, optional fields' null branch and
    descriptions repeated in a list's items."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def resolve(node: Any, names: bool = False) -> Any:
        """``names``: ``node`` maps property names to schemas, so a property called "title" stays."""
        if isinstance(node, dict):
            ref = node.get("$ref")
            if not names and isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = copy.deepcopy(defs[ref.split("/")[-1]])
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return resolve({**target, **extra})
            out = {
                k: resolve(v, names=not names and k in ("properties", "patternProperties"))
                for k, v in node.items()
                if names or k != "title"
            }
            return out if names else _dedupe_items(_drop_optional_null(out))
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    out = resolve(schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out
