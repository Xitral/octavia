"""What a skill may hold and do, checked by code before it's saved and again before each use. Protected from her own
builds (``evolve.policy.FORBIDDEN``), like the permission policy: a build can't loosen what a skill may do.

A skill is a shortcut, never a permission. Its steps are run one by one through the task's own executor, with every
check the task's own steps get (``runner``), so the rules here keep a skill to what a task could do anyway, in a form
that can't carry anything it shouldn't:

* only the tools of the kind of task it's for (the task's own registry), never a restricted one, and never the tools
  that run, propose, save or forget skills (a skill runs another only as a skill step, which is checked here too);
* arguments the tool really takes (its own ``Args``, with every input filled in), whose text names only the skill's
  own inputs, and every input used;
* nothing secret or personal written into it: what it types or puts on the clipboard, and any private argument
  (``Tool.private_args``), is always an input (it may be a password), also where it fills in another skill's inputs,
  and no text looks like a secret, an email address or a long number;
* a few levels of skills in skills at most, never one that runs itself, and no more steps in all than a task should
  hand to one call.

The skills it runs are checked step by step as it fills them in, against the tools tasks have today.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from octavia.futures.privacy import looks_secret
from octavia.recall import privacy
from octavia.tools.base import RiskLevel

if TYPE_CHECKING:
    from octavia.skills.skill import Skill, Step

KINDS = ("screen task", "web task")
STATES = ("proposed", "approved", "off")
ORIGINS = ("task", "repeats", "user")
# An input in a step's text: "{course}".
INPUT = re.compile(r"\{([a-z][a-z0-9_]{0,29})\}")

NAME = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
INPUT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
MAX_STEPS = 25  # in one skill
MAX_RUN_STEPS = 80  # in all, with the skills it runs
MAX_DEPTH = 3  # a skill in a skill in a skill
MAX_INPUTS = 6
TITLE_CHARS = 80
PURPOSE_CHARS = 300
EXPECT_CHARS = 100
INPUT_CHARS = 2000  # one input's value when it's used
# Never a step of a skill: running, proposing, saving or forgetting skills, and her self-improvement tools.
NEVER = frozenset({
    "use_skill", "propose_skill", "save_skill", "forget_skill", "skills", "note_improvement", "build_improvement",
    "install_improvement", "reject_improvement", "undo_last_improvement", "restart_now", "shut_down_self",
})  # fmt: skip
# What goes into the app or page, typed or put on the clipboard: always an input (it may be a password, and its
# private_args don't always say so: web_type's value). web_type's "text" names the field, so it isn't one.
TYPED = {"type_text": frozenset({"text"}), "web_type": frozenset({"value"}), "copy_to_clipboard": frozenset({"text"})}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_NUMBER = re.compile(r"\d[\d ,.-]{5,}\d")
_SAMPLE = "1"  # what an input stands for while a step's arguments are checked: fits text and numbers alike

Lookup = Callable[[str], "Skill | None"]


def _bad_text(text: str) -> bool:
    return bool(_EMAIL.search(text) or _LONG_NUMBER.search(text) or looks_secret(text) or privacy.secret_line(text))


def _literal(value: str) -> str:
    """The part of a text that's written into the skill: what's left once its inputs are taken out."""
    return INPUT.sub(" ", value).strip()


def _check_step_args(step: Step, number: int | str, tool: Any, problems: list[str]) -> None:
    where = f"step {number} ({step.tool})" if isinstance(number, int) else f"{number} ({step.tool})"
    for key, value in step.args.items():
        if not isinstance(key, str):
            problems.append(f"{where}: an argument's name isn't text")
            continue
        if isinstance(value, (dict, list)):
            problems.append(f"{where}: {key} must be plain text, a number or true/false")
            continue
        if not isinstance(value, str):
            continue
        literal = _literal(value)
        if literal and (key in tool.private_args or key in TYPED.get(step.tool, ())):
            problems.append(f"{where}: what it types ({key}) must be an input, not written into the skill")
        if literal and _bad_text(literal):
            problems.append(f"{where}: {key} looks like it holds something secret or personal")
    sample = {name: _SAMPLE for name in INPUT.findall(" ".join(str(v) for v in step.args.values()))}
    filled = {key: fill(value, sample) for key, value in step.args.items()}
    try:
        tool.Args.model_validate(filled)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", ())) or "its arguments"
        problems.append(f"{where}: {field}: {first.get('msg', 'not what the tool takes')}")


def check(skill: Skill, registry: Any, lookup: Lookup = lambda name: None) -> list[str]:
    """What's wrong with ``skill`` for a task whose tools are in ``registry`` (a ``ToolRegistry``), in plain words;
    empty when nothing is. ``lookup`` finds the other skills it runs."""
    problems: list[str] = []
    if not NAME.match(skill.name):
        problems.append("its name must be 3 to 40 lowercase letters, digits or _, starting with a letter")
    if not skill.title.strip() or len(skill.title) > TITLE_CHARS:
        problems.append(f"it needs a title of at most {TITLE_CHARS} characters")
    if len(skill.purpose) > PURPOSE_CHARS:
        problems.append(f"its purpose is longer than {PURPOSE_CHARS} characters")
    if skill.kind not in KINDS:
        problems.append(f"its kind must be one of: {', '.join(KINDS)}")
    if skill.state not in STATES or skill.origin not in ORIGINS:
        problems.append("its state or origin isn't one there is")
    for text in (skill.title, skill.purpose, skill.place):
        if text and _bad_text(text):
            problems.append("its title, purpose or place looks like it holds something secret or personal")
            break
    names = [i.name for i in skill.inputs]
    if len(names) > MAX_INPUTS:
        problems.append(f"it has more than {MAX_INPUTS} inputs")
    if len(set(names)) != len(names) or not all(INPUT_NAME.match(n) for n in names):
        problems.append("its inputs need different names of lowercase letters, digits or _")
    if not skill.steps:
        problems.append("it has no steps")
    if len(skill.steps) > MAX_STEPS:
        problems.append(f"it has more than {MAX_STEPS} steps")
    if problems:
        return problems
    declared = set(names)
    for number, step in enumerate(skill.steps, 1):
        if bool(step.tool) == bool(step.skill):
            problems.append(f"step {number} must name either a tool or a skill")
            continue
        unknown = sorted(n for n in inputs_used([step]) if n not in declared)
        if unknown:
            problems.append(f"step {number} uses {', '.join(unknown)}, which isn't one of its inputs")
        if step.skill:
            problems.extend(_check_skill_step(skill, step, number, lookup))
            continue
        tool = registry.get(step.tool)
        if step.tool in NEVER or tool is None:
            problems.append(f"step {number}: {step.tool} isn't a tool a {skill.kind} has")
            continue
        if tool.risk == RiskLevel.RESTRICTED:
            problems.append(f"step {number}: {step.tool} is restricted, so it's never part of a skill")
            continue
        if len(step.expect) > EXPECT_CHARS:
            problems.append(f"step {number}: what it expects is longer than {EXPECT_CHARS} characters")
        _check_step_args(step, number, tool, problems)
    unused = sorted(declared - inputs_used(skill.steps))
    if unused:
        problems.append(f"input {', '.join(unused)} is never used")
    if not problems:
        total, why = _expanded(skill, lookup, (skill.name,))
        if why:
            problems.append(why)
        elif total > MAX_RUN_STEPS:
            problems.append(f"with the skills it runs it has {total} steps, more than {MAX_RUN_STEPS}")
    if not problems:
        # The skills it runs, step by step as this one fills them in: each checked against today's tools, and what
        # this one writes into their inputs held to the same rules as its own steps (a password typed by a child).
        for label, step in expand(skill, lookup, {i.name: "{" + i.name + "}" for i in skill.inputs}):
            if not label.count(" > "):
                continue  # its own steps, checked above
            tool = registry.get(step.tool)
            if step.tool in NEVER or tool is None or tool.risk == RiskLevel.RESTRICTED:
                problems.append(f"{label}: {step.tool} isn't a tool a {skill.kind} may use")
                continue
            _check_step_args(step, label, tool, problems)
    return problems


def expand(skill: Skill, lookup: Lookup, inputs: Mapping[str, str], path: str = "") -> list[tuple[str, Step]]:
    """The tool steps ``skill`` runs, the skills it runs taken step by step, each with its arguments filled in from
    ``inputs`` (text the caller gives, which may itself hold the caller's ``{inputs}``) and a label naming where it
    is ("step 1 > pick_theme step 2"). Assumes ``_expanded`` found no loop."""
    found: list[tuple[str, Step]] = []
    for number, step in enumerate(skill.steps, 1):
        label = f"{path}step {number}" if not path else f"{path}{skill.name} step {number}"
        if step.skill:
            other = lookup(step.skill)
            if other is None:
                continue
            sub = {k: str(fill(v, inputs)) for k, v in step.inputs.items()}
            found.extend(expand(other, lookup, sub, f"{label} > "))
            continue
        found.append((label, replace(step, args={k: fill(v, inputs) for k, v in step.args.items()})))
    return found


def _check_skill_step(skill: Skill, step: Step, number: int, lookup: Lookup) -> list[str]:
    if step.skill == skill.name:
        return [f"step {number}: a skill can't run itself"]
    other = lookup(step.skill)
    if other is None:
        return [f"step {number}: there's no skill {step.skill}"]
    problems = []
    if not other.usable:
        problems.append(f"step {number}: the skill {step.skill} is {other.status}, so it can't be run")
    if other.kind != skill.kind:
        problems.append(f"step {number}: the skill {step.skill} is for a {other.kind}")
    wanted = {i.name for i in other.inputs}
    if set(step.inputs) != wanted:
        problems.append(f"step {number}: the skill {step.skill} takes {', '.join(sorted(wanted)) or 'no inputs'}")
    for value in step.inputs.values():
        if _literal(value) and _bad_text(_literal(value)):
            problems.append(f"step {number}: an input for {step.skill} looks like something secret or personal")
    return problems


def _expanded(skill: Skill, lookup: Lookup, path: tuple[str, ...]) -> tuple[int, str]:
    """How many steps ``skill`` runs in all, or why it can't be run (a loop, too many levels)."""
    if len(path) > MAX_DEPTH:
        return 0, f"it runs skills more than {MAX_DEPTH} levels deep ({' > '.join(path)})"
    total = 0
    for step in skill.steps:
        if not step.skill:
            total += 1
            continue
        if step.skill in path:
            return 0, f"it runs itself through {' > '.join((*path, step.skill))}"
        other = lookup(step.skill)
        if other is None:
            return 0, f"there's no skill {step.skill}"
        count, why = _expanded(other, lookup, (*path, step.skill))
        if why:
            return 0, why
        total += count
    return total, ""


def check_inputs(skill: Skill, given: Mapping[str, Any]) -> tuple[dict[str, str], str]:
    """The inputs a use of ``skill`` gives, as text, or why they won't do (one missing, one it doesn't take, too
    long)."""
    wanted = [i.name for i in skill.inputs]
    missing = [n for n in wanted if n not in given or given[n] in (None, "")]
    extra = sorted(set(given) - set(wanted))
    if missing:
        return {}, f"it needs {', '.join(missing)}"
    if extra:
        return {}, f"it doesn't take {', '.join(extra)} (its inputs: {', '.join(wanted) or 'none'})"
    values = {n: str(given[n]) for n in wanted}
    if any(len(v) > INPUT_CHARS for v in values.values()):
        return {}, f"an input is longer than {INPUT_CHARS} characters"
    return values, ""


def inputs_used(steps: Iterable[Step]) -> set[str]:
    """The input names the steps' text refers to (``{name}``)."""
    used: set[str] = set()
    for step in steps:
        values: Iterable[Any] = step.inputs.values() if step.skill else step.args.values()
        for value in values:
            if isinstance(value, str):
                used.update(INPUT.findall(value))
    return used


def fill(value: Any, inputs: Mapping[str, str]) -> Any:
    """``value`` with each ``{name}`` replaced by that input; a text that is only one ``{name}`` becomes the input
    as given, and anything not text is left as it is."""
    if not isinstance(value, str):
        return value
    whole = INPUT.fullmatch(value)
    if whole is not None and whole.group(1) in inputs:
        return inputs[whole.group(1)]
    return INPUT.sub(lambda m: inputs.get(m.group(1), m.group(0)), value)
