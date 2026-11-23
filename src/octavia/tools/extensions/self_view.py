"""Her own setup, read from what she runs with: ``check_my_setup``.

"Do you need my okay to run commands?", "what's your local brain set to?", "did Qwen answer that?": she used to answer
these from memory, and got them wrong (on 2026-10-25 she told the user three wrong things about her own permissions,
had him add a rule that made every read-only command ask, and said four times that she couldn't see the router). This
tool answers from the running objects instead:

* **permissions**: what would happen to a call, worked out by the very code that runs calls (``ToolExecutor.preview``,
  which shares ``_judge`` with ``execute``), with the rule behind it and what taking the user's own rule for that tool
  out of ``permissions.tools`` would change. With no tool, the rules she runs with.
* **settings**: any setting by its dotted key or by words (the settings window's catalogue), the value she runs with,
  where it comes from (the default, ``config/local.yaml``, her settings window) and, when the files now say something
  else, whether that's picked up live or waits for a restart. Keys and other secrets are only ever "set" or "not set".
* **brains**: how turns are routed right now (the local brain's mode and model, her quick voice) and which brain or
  route handled each recent turn and why, from ``routing.log_file`` and ``local_brain.route_log_file``. It only reads
  them: routing itself is ``octavia.localbrain``'s.

SAFE: it only reads, and changes nothing. Changing a setting stays the user's (the settings window, or the files).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from octavia.localbrain.shadow import USER_SOURCES
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.permissions import Decision

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.tools.executor import CallPreview, Judgement

log = logging.getLogger(__name__)

MAX_SETTINGS = 8  # matches shown for a search
MAX_VALUE = 300  # characters of one value
_SECRET_KEY = re.compile(r"(?:^|_)(?:key|token|secret|password|passwd|cookie|credentials?)(?:$|_)", re.I)

_DECISIONS = {
    Decision.ALLOW: "run without asking",
    Decision.AUTO: "run without asking while the user is working with me in their own request (and ask otherwise, "
    "for example after I've read a web page in that task, or when I'd do it on my own)",
    Decision.CONFIRM: "ask the user first",
    Decision.DENY: "be blocked: I can't do it at all",
}
_RISKS = {
    RiskLevel.SAFE: "read-only or harmless",
    RiskLevel.CONFIRM: "a change",
    RiskLevel.RESTRICTED: "high-risk",
}


class CheckMySetupArgs(ToolArgs):
    about: Literal["permissions", "settings", "brains"] = "permissions"
    tool: str = Field("", max_length=80, description="permissions: one tool to check, e.g. run_command")
    example: dict[str, Any] = Field(
        default_factory=dict,
        description='permissions: that tool\'s arguments for one call, e.g. {"command": "Get-ChildItem E:\\\\"}',
    )
    setting: str = Field("", max_length=80, description="settings: a dotted key or a few words")
    turns: int = Field(5, ge=1, le=20, description="brains: how many recent turns")


class CheckMySetup(Tool):
    name = "check_my_setup"
    description = (
        "Your own setup as you run it now, never guessed: permissions (what needs the user's yes and why, for a "
        "tool or one example call, and what removing a rule would change), settings (any value, where it's set, "
        "whether a change waits for a restart), brains (local brain mode and which brain or route answered recent "
        "turns, and why). Read-only."
    )
    Args = CheckMySetupArgs
    risk = RiskLevel.SAFE
    category = "system"

    async def run(self, args: CheckMySetupArgs, ctx: ToolContext) -> ToolResult:
        if args.about == "settings":
            return await asyncio.to_thread(settings_report, ctx.config, args.setting)
        if args.about == "brains":
            return await asyncio.to_thread(
                brains_report, ctx.config, args.turns, ctx.extras.get("brains"), ctx.extras.get("engines")
            )
        preview = ctx.extras.get("preview_call")
        if not callable(preview):
            return ToolResult.error("I can't see my permission rules from here (this isn't my main conversation).")
        if args.tool:
            return await tool_report(preview, args.tool, args.example)
        return await permissions_report(preview, ctx.config)


# --------------------------------------------------------------------------- permissions


def _outcome(judged: Judgement, words: bool) -> str:
    """What happens to a judged call, in words."""
    said = _DECISIONS[judged.decision]
    if judged.decision is Decision.CONFIRM and judged.flagged and judged.finding is not None:
        said += f", because it {judged.finding.reason or 'looked risky to its own check'}"
    if judged.decision is Decision.CONFIRM and words and judged.risk is not RiskLevel.RESTRICTED:
        said += " (unless the user's own words plainly asked for it)"
    return said


async def tool_report(preview: Callable[..., Awaitable[CallPreview]], tool: str, example: dict[str, Any]) -> ToolResult:
    found = await preview(tool, example or None)
    if found.error or found.judged is None:
        return ToolResult.error(found.error or f"I couldn't check {tool}.")
    judged = found.judged
    what = "This call" if example else f"An everyday {tool} call"
    lines = [f"{what} would {_outcome(judged, found.words_can_approve)}."]
    if example:
        lines.append(f"I judge it {_RISKS[judged.risk]}.")
    if judged.flagged and judged.decision is not judged.verdict.decision:
        # The rules alone would let it through ("allow" for the tool): it's the tool's own check that asks.
        lines.append(
            f"That's the tool's own check of this call; the rules alone would {_DECISIONS[judged.verdict.decision]}."
        )
    elif judged.verdict.per_tool:
        source = (
            f"the user's own rule {tool}: {found.rule.value} in permissions.tools (config/local.yaml)"
            if found.rule_from == "permissions.tools" and found.rule is not None
            else "a rule from another part of my settings"
        )
        lines.append(f"That's because of {source}, not the tool's own risk.")
    else:
        lines.append(f"That's the {judged.verdict.reason}.")
    if found.without_rule is not None:
        bare = found.without_rule
        if (bare.decision, bare.flagged) == (judged.decision, judged.flagged):
            lines.append("Taking that rule out would change nothing for this.")
        else:
            lines.append(f"Without that rule it would {_outcome(bare, found.words_can_approve)}.")
            if bare.decision is Decision.DENY:
                lines.append("So that rule unblocks a call that's otherwise blocked.")
    if found.varies and not example:
        lines.append(
            f"{tool} judges each call on its own (by what it would do, or what's on screen), so this is its everyday "
            "case: give an example call to check a particular one."
        )
    data = {
        "tool": tool,
        "decision": judged.decision.value,
        "risk": judged.risk.value,
        "flagged": judged.flagged,
        "rule": found.rule.value if found.rule is not None else None,
        "without_rule": found.without_rule.decision.value if found.without_rule is not None else None,
    }
    return ToolResult.success(" ".join(lines), data=data)


async def permissions_report(preview: Callable[..., Awaitable[CallPreview]], config: AppConfig) -> ToolResult:
    perms = config.permissions
    lines = [
        "My permission rules, as I run now:",
        f"- Read-only or harmless calls: {_DECISIONS[perms.safe]}.",
        f"- Calls that change something: {_DECISIONS[perms.confirm]}.",
        f"- High-risk calls: {_DECISIONS[perms.restricted]}.",
        "- Whatever these say, a call a tool's own check finds risky (sending, deleting, buying, a command that "
        "deletes or reaches the internet) still asks the user first.",
    ]
    if perms.tools:
        lines.append("The user's own rules for single tools (permissions.tools in config/local.yaml):")
        for name, rule in perms.tools.items():
            found = await preview(name)
            line = f"- {name}: {rule.value}"
            if found.error:
                line += f" ({found.error})"
            elif found.without_rule is not None and found.judged is not None:
                now, bare = found.judged.decision, found.without_rule.decision
                if bare is not now:
                    line += f". Its everyday calls {_DECISIONS[now]}; without it they'd {_DECISIONS[bare]}"
                else:
                    line += ". Taking it out would change nothing for its everyday calls"
                if found.varies and rule is not Decision.DENY and perms.restricted is Decision.DENY:
                    # A per-tool rule outranks the risk levels, so a call of this tool that would be blocked isn't.
                    line += f". It also covers any {name} call that would otherwise be blocked: " + (
                        "that's let through" if rule is Decision.ALLOW else "that's asked about instead"
                    )
            lines.append(line + ".")
    if perms.submit_without_asking:
        lines.append(f"Sites where I submit without asking: {', '.join(perms.submit_without_asking)}.")
    if perms.trusted_apps:
        lines.append(f"Apps where I delete inside the document without asking: {', '.join(perms.trusted_apps)}.")
    if perms.always_ask_before_sending:
        lines.append(f"Always asked before sending: {', '.join(perms.always_ask_before_sending)}.")
    files = await asyncio.to_thread(_files_now, config)
    if isinstance(files, str):
        lines.append(f"(I couldn't read my config files just now: {files})")
    elif files.permissions.model_dump() != perms.model_dump():
        lines.append(
            "My config files now say something different about permissions; rules for single tools and risk levels "
            "only change when I restart."
        )
    return ToolResult.success("\n".join(lines), data={"tools": {k: v.value for k, v in perms.tools.items()}})


# --------------------------------------------------------------------------- settings


def _files_now(config: AppConfig) -> AppConfig | str:
    """What the config files say right now (they may have changed since she started), or why they couldn't load."""
    from octavia.config import build_config

    try:
        return build_config(None, config.overrides or None, config.root)
    except Exception as exc:  # a typo in local.yaml: she keeps running with what she has
        return f"{type(exc).__name__}: {exc}"


def _lookup(model: Any, key: str) -> Any:
    node = model
    for part in key.split("."):
        if isinstance(node, dict):
            if part not in node:
                raise KeyError(key)
            node = node[part]
        elif isinstance(node, BaseModel) and part in type(node).model_fields:
            node = getattr(node, part)
        else:
            raise KeyError(key)
    return node


def _masked(value: Any) -> Any:
    """A whole section with any secret-named value in it (at any depth) as "set" or "not set"."""
    if isinstance(value, dict):
        return {
            k: (
                ("set" if v else "not set")
                if _SECRET_KEY.search(str(k)) and not str(k).endswith("_env")
                else _masked(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def _shown(key: str, value: Any) -> str:
    if _SECRET_KEY.search(key.rsplit(".", 1)[-1]) and not key.endswith("_env"):
        return "set" if value else "not set"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif hasattr(value, "value") and not isinstance(value, (str, int, float)):
        value = value.value  # an enum
    value = _masked(value)
    text = json.dumps(value, default=str, ensure_ascii=False) if not isinstance(value, str) else repr(value)
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 1] + "…"


def _where(config: AppConfig, key: str) -> str:
    from octavia import user_settings
    from octavia.config import _read_yaml

    if key in getattr(config, "dropped_settings", ()):
        return "chosen in her settings window, but that choice isn't a value she understands, so the files' is used"
    if key in user_settings.read(config.root):
        return "chosen in her settings window"
    local = next((Path(s) for s in config.sources if not s.endswith("default.yaml")), None)
    if local is not None:
        try:
            _lookup(_read_yaml(local), key)
            return f"set in {local.parent.name}/{local.name}"
        except (KeyError, OSError, ValueError):
            pass
    return "the default (config/default.yaml)"


def settings_report(config: AppConfig, query: str) -> ToolResult:
    from octavia import settings_catalog

    query = query.strip()
    if not query:
        return ToolResult.error('Say which setting: a dotted key ("local_brain.mode") or a few words ("wake word").')
    keys: list[tuple[str, str]] = []  # (dotted key, label)
    try:
        _lookup(config, query)
        known = settings_catalog.by_key(query)
        keys.append((query, known.label if known is not None else ""))
    except KeyError:
        for setting in settings_catalog.search(query):  # best first: the cut below keeps what was meant
            if not setting.env:
                keys.append((setting.key, setting.label))
    env = [s for s in settings_catalog.search(query) if s.env]
    if not keys and not env:
        return ToolResult.error(f'No setting matches "{query}". Try other words, or a dotted key like "tts.provider".')
    files = _files_now(config)
    lines = []
    for key, label in keys[:MAX_SETTINGS]:
        value = _lookup(config, key)
        line = f"- {label + ' ' if label else ''}({key}): {_shown(key, value)}, {_where(config, key)}"
        if isinstance(files, str):
            pass
        else:
            try:
                now = _lookup(files, key)
            except KeyError:
                now = value
            if now != value:
                setting = settings_catalog.by_key(key)
                waits = setting is None or settings_catalog.needs_restart(setting)
                line += f". The files now say {_shown(key, now)}: " + (
                    "that waits for a restart" if waits else "I pick that up within a second or so"
                )
        if (known := settings_catalog.by_key(key)) is not None and known.about:
            line += f". What it does: {known.about}"  # so her advice about it starts from the truth
        lines.append(line)
    for setting in env[: max(0, MAX_SETTINGS - len(keys))]:
        from octavia.config import current_env

        name = setting.key.removeprefix(settings_catalog.ENV)
        lines.append(f"- {setting.label} ({name} in .env): {'set' if current_env(name) else 'not set'}")
    more = len(keys) + len(env) - len(lines)
    if more > 0:
        lines.append(f"(and {more} more: narrow the words)")
    if isinstance(files, str):
        lines.append(f"(I couldn't read my config files just now, so I can't say whether a change is waiting: {files})")
    return ToolResult.success("\n".join(lines), data={"keys": [k for k, _ in keys[:MAX_SETTINGS]]})


# --------------------------------------------------------------------------- brains

_MODES = {
    "route": "my local brain hears short, simple turns first and answers them itself or hands them to my cloud brain; "
    "anything risky, long or with several steps goes straight to the cloud brain",
    "fallback_only": "my cloud brain answers everything; my local brain answers only when the cloud brain is down",
    "shadow": "my cloud brain answers everything; my local brain only practises in the background and never answers",
    "off": "my cloud brain answers everything; my local brain isn't running",
}
# Who answers, when the user picked it (``brain.answers``, ``core/engines.py``): it comes before the mode.
_PICKED = {
    "claude": "you set me to Claude only: my cloud brain answers everything, and my local brain never does, not even "
    "when the cloud brain is down",
    "local": "you set me to my local brain only: it answers everything you say and says plainly what it can't do; "
    "nothing you say goes to my cloud brain (my background work still uses it)",
}
_ROUTES = {
    "rule": "a quick built-in command, no brain",
    "preset": "a preset reply, no brain",
    "dismissed": "nothing: I took it as not for me",
}
# Why a turn went straight to the cloud brain without asking the local one (``policy`` in the route log).
_POLICIES = {
    "notes": "it came with a note only my cloud brain handles",
    "long": "it was too long for my local brain",
    "empty": "there were no words left once my name was taken off",
}


def _model_words(model: str | None) -> str:
    from octavia.core.fast_path import spoken_model

    if not model:
        return "no brain"
    if model.startswith("local:"):
        return f"my local brain ({model.removeprefix('local:')})"
    if model.startswith("<"):
        return "my cloud brain, which failed before answering"
    return f"my cloud brain ({spoken_model(model)})"


def _read_lines(path: Path, since: float) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-400:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("ts"), (int, float)) and row["ts"] >= since:
            rows.append(row)
    return rows


def _why(route: dict[str, Any] | None) -> str:
    """Why the router sent a turn where it went, from its line in the route log."""
    if route is None:
        return ""
    if route.get("why") == "down":
        return "because my cloud brain was down"
    decision = route.get("decision")
    if decision == "answer":
        return "it was simple enough for my local brain"
    if decision == "delegate":
        because = route.get("delegated_because")
        return f"my local brain handed it over ({because})" if because else "my local brain handed it over"
    if decision == "fallback":
        return f"my local brain couldn't answer it ({route.get('fallback')})"
    if decision == "policy":
        word = str(route.get("policy") or "")
        return _POLICIES.get(word, f'it had "{word}" in it, which only my cloud brain handles')
    return ""


def brains_report(config: AppConfig, turns: int, setup: Any = None, engines: Any = None) -> ToolResult:
    state = setup() if callable(setup) else (setup or {})
    mode = state.get("local_mode") or ("off" if not config.local_brain.enabled else config.local_brain.mode)
    picked = engines.brain_now() if engines is not None else "auto"
    lines = [f"Routing now: {_PICKED.get(picked) or _MODES.get(mode, mode)}."]
    if mode != "off":
        lines.append(f"My local brain's model: {Path(config.local_brain.model).stem}.")
    if state.get("quick_voice"):
        lines.append(f"Small talk can go to my quick voice ({config.fast_chat.model}) first.")
    if "check_my_setup" not in config.local_brain.route_tools:
        lines.append("This answer comes from my cloud brain: my local brain can't use this tool.")
    since = time.time() - 7 * 86400
    turn_rows = _read_lines(config.resolve_path(config.routing.log_file), since)
    routes = _read_lines(config.resolve_path(config.local_brain.route_log_file), since)
    recent = [r for r in turn_rows if r.get("source") in USER_SOURCES][-turns:]
    if not recent:
        lines.append("I have no record of recent turns.")
        return ToolResult.success("\n".join(lines), data={"mode": mode})
    lines.append("Recent turns, oldest first:")
    for row in recent:
        at = datetime.fromtimestamp(row["ts"]).strftime("%H:%M")
        said = " ".join(str(row.get("text") or "").split())
        said = said if len(said) <= 60 else said[:59] + "…"
        kind = row.get("route")
        if kind in _ROUTES:
            who = _ROUTES[kind]
        elif kind == "fast_chat":
            who = f"my quick voice ({row.get('model') or config.fast_chat.model})"
        else:
            who = _model_words(row.get("model"))
        route = _matching_route(row, routes)
        why = _why(route)
        lines.append(
            f'- {at} "{said}": {who}' + (f", {why}" if why else "") + ("" if row.get("ok", True) else ", and it failed")
        )
    return ToolResult.success("\n".join(lines), data={"mode": mode, "turns": len(recent)})


def _matching_route(row: dict[str, Any], routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The route log's line for this turn: the same words, written within a couple of minutes of it."""
    text = str(row.get("text") or "")[:100]
    best = None
    for route in routes:
        if str(route.get("text") or "")[:100] == text and abs(route["ts"] - row["ts"]) <= 180:
            if best is None or abs(route["ts"] - row["ts"]) < abs(best["ts"] - row["ts"]):
                best = route
    return best


TOOLS = [CheckMySetup]
