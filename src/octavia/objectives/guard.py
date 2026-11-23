"""What the objective sessions may touch, checked by code on every tool call (a PreToolUse hook; it fails closed).

* The planner and the reviewers only read: files inside their folder (the project, or a step's worktree), and the
  web when they're given it. No shell, no edits.
* A code step edits files inside its worktree, except the ones that decide what its checks run (``PROTECTED`` in
  ``projects``), and runs only two kinds of commands: the project's check (with extra arguments, such as a test name)
  and read-only git. No pipes, chaining, redirects, variables or paths outside the worktree.

The path and command rules are self-improvement's (``evolve/policy.py``), which have been hardened against the
tricks that smuggle a path out (junctions, 8.3 names, data streams, brace expansion).
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

# Private helpers on purpose: one set of path and command rules for every guarded session.
from octavia.evolve.policy import (
    _BRACE_EXPANSION,
    _MAX_COMMAND,
    _SHELL_META,
    GuardDecision,
    _bad_pattern,
    _banned_option,
    _inside,
    _points_outside,
)
from octavia.objectives.projects import PROTECTED

READ_TOOLS = ("Read", "Glob", "Grep")
WEB_TOOLS = ("WebSearch", "WebFetch")
CODE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash", "TodoWrite")
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"  # hands back the answer; must always go through
_GIT_READ = ("status", "diff", "log", "show", "ls-files", "grep", "blame")

Check = Callable[[str, Any], GuardDecision]

_ALLOW = GuardDecision(True)


def _deny(reason: str) -> GuardDecision:
    return GuardDecision(False, reason)


# Never read in her own checkout: her keys, her local settings, everything she keeps about the user, her models.
PRIVATE = (".env", ".env.*", "config/local.yaml", "config/*.local.yaml", "data/**", "models/**", ".venv/**")


def reader(root: Path, *, web: bool, private: tuple[str, ...] = ()) -> Check:
    """A planner's or reviewer's guard: read files under ``root``, and the web when ``web``. Nothing matching
    ``private`` (root-relative globs) is read, and searching needs a path or a glob that stays out of it."""
    allowed = (*READ_TOOLS, "TodoWrite", *(WEB_TOOLS if web else ()))

    def check(name: str, tool_input: Any) -> GuardDecision:
        if name not in allowed:
            return _deny(f"{name} isn't available here; you can only read. Available: {', '.join(allowed)}.")
        if name in WEB_TOOLS or name == "TodoWrite":
            return _ALLOW
        decision = _file_call(name, tool_input, root, writable=False)
        if decision.allowed and private and _reaches_private(name, tool_input, root, private):
            return _deny("That's private (keys, settings, what's kept about the user): read the code instead.")
        return decision

    return check


def _reaches_private(name: str, tool_input: Any, root: Path, private: tuple[str, ...]) -> bool:
    """Whether a read or search could take in a private file: a file that is one, or a search over a folder that
    holds one (a search over everything, or over src/ with no glob, is fine only when it can't reach them)."""
    key = "file_path" if name == "Read" else "path"
    value = tool_input.get(key) if isinstance(tool_input, dict) else None
    base = root.resolve()
    if isinstance(value, str) and value.strip():
        resolved, _ = _inside(root, value)
        rel = resolved.relative_to(base).as_posix() if resolved is not None else ""
    else:
        rel = ""
    if name == "Read":
        return _matches(rel, private)
    # A search: private if it starts at or above a private file's folder
    tops = {p.split("/", 1)[0] for p in private}
    first = rel.split("/", 1)[0] if rel else ""
    return not rel or first in tops or any(_matches(rel, (p,)) for p in private)


def _matches(rel: str, patterns: tuple[str, ...]) -> bool:
    rel = rel.casefold()
    for pattern in patterns:
        pattern = pattern.casefold()
        if pattern.endswith("/**"):
            if rel == pattern[:-3] or rel.startswith(pattern[:-2]):
                return True
        elif fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def coder(worktree: Path, check_command: tuple[str, ...] | None) -> Check:
    """A code step's guard: read and edit inside ``worktree`` (protected files excepted), run the check, the project's
    other checking commands (``commands_for``) or read-only git, and read what its own long commands saved."""
    extra = commands_for(worktree, check_command)
    outputs = cli_output_dir(worktree)

    def check(name: str, tool_input: Any) -> GuardDecision:
        if name == "TodoWrite":
            return _ALLOW
        if name == "Bash":
            if not isinstance(tool_input, dict):
                return _deny("Invalid tool input.")
            reason = check_bash(tool_input.get("command", ""), check_command, extra)
            return _deny(reason) if reason else _ALLOW
        if name in ("Read", "Grep") and _own_output(tool_input, outputs):
            return _ALLOW
        if name in ("Read", "Edit", "Write", "Glob", "Grep"):
            return _file_call(name, tool_input, worktree, writable=True)
        return _deny(f"{name} isn't available. Available: {', '.join(CODE_TOOLS)}.")

    return check


# A package script that only checks, by its name ("test:unit", "lint", "format:check", "typecheck", "build"): never
# one that starts a server, deploys or publishes.
_CHECKING_SCRIPT = re.compile(r"(?:test|lint|format|check|typecheck|type-check|tsc|build)(?:[:\-][\w:.-]*)?", re.I)
# ...unless its name or what it (or its pre/post hook) runs could deploy, publish or start something long-running.
_NOT_A_CHECK = re.compile(r"deploy|publish|release|upload|push|serve|start|\bdev\b|watch|e2e|prod", re.I)
_NPM_FLAGS = frozenset({"--silent", "-s", "--if-present"})  # npm's own options that change only how much it prints


def commands_for(worktree: Path, check_command: tuple[str, ...] | None) -> list[tuple[str, ...]]:
    """What a code step may run besides the check, by the check's program: the project's other checking scripts and
    a single test file (npm), cargo's other checks, pytest's linter. Each runs the project's own configuration, which
    a step can't change (``PROTECTED``)."""
    program = _program(check_command[0]) if check_command else ""
    if program == "npm":
        scripts = _package_scripts(worktree / "package.json")
        runs = [
            ("npm", "run", name)
            for name in sorted(scripts)
            if _CHECKING_SCRIPT.fullmatch(name)
            and not any(
                _NOT_A_CHECK.search(text)
                for text in (name, scripts[name], scripts.get(f"pre{name}", ""), scripts.get(f"post{name}", ""))
            )
        ]
        return [*runs, ("node", "--test")]
    if program == "cargo":
        return [("cargo", "check"), ("cargo", "clippy"), ("cargo", "fmt", "--check"), ("cargo", "build")]
    if program == "python":
        return [("python", "-m", "ruff")]
    if program == "dotnet":
        return [("dotnet", "test")]
    return []


def cli_output_dir(cwd: Path) -> Path:
    """Where Claude Code saves a long command's output for a session working in ``cwd`` (it reads it back itself)."""
    return Path(tempfile.gettempdir()) / "claude" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _own_output(tool_input: Any, outputs: Path) -> bool:
    """A read of one of the session's own saved command outputs (only reading, only there)."""
    if not isinstance(tool_input, dict):
        return False
    value = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(value, str) or not value.strip():
        return False
    resolved, _ = _inside(outputs, value)
    return resolved is not None


def _package_scripts(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}


def hook(
    check: Check, on_call: Callable[[str, Any, GuardDecision], None] | None = None
) -> Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]:
    """The PreToolUse hook for a session: ``check`` decides; ``on_call`` hears every decision (progress lines)."""

    async def guard(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        name = "?"
        try:
            name = str(input_data.get("tool_name", ""))
            if name == STRUCTURED_OUTPUT_TOOL:
                return {}
            decision = check(name, input_data.get("tool_input"))
        except Exception as exc:  # the CLI must never see the hook fail: fail closed instead
            decision = GuardDecision(False, f"Blocked ({type(exc).__name__} while checking the call).")
        if on_call is not None:
            try:
                on_call(name, input_data.get("tool_input") if isinstance(input_data, dict) else None, decision)
            except Exception:
                pass
        if decision.allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason or "Blocked.",
            }
        }

    return guard


def check_bash(
    command: Any, check_command: tuple[str, ...] | None, extra: Sequence[tuple[str, ...]] = ()
) -> str | None:
    """None if a code step may run ``command``; otherwise why not (shown to it)."""
    hint = allowed_hint(check_command, extra)
    if not isinstance(command, str) or not command.strip():
        return "Empty command. " + hint
    if len(command) > _MAX_COMMAND:
        return "That command is too long. " + hint
    raw = command.rstrip("\r\n")
    if any(meta in raw for meta in _SHELL_META) or _BRACE_EXPANSION.search(raw) or "%" in raw:
        return "Shell operators, variables and brace expansion aren't allowed. " + hint
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        return "Couldn't parse that command. " + hint
    if not tokens:
        return "Empty command. " + hint
    if "/" in tokens[0] or "\\" in tokens[0]:
        return f"Run programs by name ({tokens[0]!r} is a path). " + hint
    git = tokens[0] == "git"
    script_args = False  # npm's after "--": the test script's own (paths are still checked)
    if git:
        if len(tokens) < 2 or tokens[1] not in _GIT_READ:
            return f"'{raw}' isn't an allowed git command. " + hint
        args = tokens[2:]
    else:
        prefixes = [*([check_command] if check_command else []), *extra]
        program = _program(tokens[0])
        rest = [t for t in tokens[1:] if not (program == "npm" and t in _NPM_FLAGS)]
        if program == "npm" and "--" in rest:
            # npm's own options stop at "--"; what follows goes to the script ("npm test -- -t inventory")
            cut = rest.index("--")
            head, args, script_args = rest[:cut], rest[cut + 1 :], True
        else:
            head, args = rest, []
        found = next(
            (p for p in prefixes if _program(p[0]) == program and tuple(head[: len(p) - 1]) == tuple(p[1:])), None
        )
        if found is None:
            return f"'{raw}' isn't an allowed command. " + hint
        args = [*head[len(found) - 1 :], *args]
    if any(arg.startswith("@") for arg in args):
        return "Argument files (@file) aren't allowed. " + hint
    for arg in args:
        if _points_outside(arg):
            return f"'{arg}' points outside the worktree. " + hint
        if git and _banned_option(arg, git=True):
            return f"The option '{arg}' isn't allowed. " + hint
        if not git and not script_args and _banned_check_option(_program(tokens[0]), arg):
            return f"The option '{arg}' isn't allowed: it changes what the check runs. " + hint
    return None


def allowed_hint(check_command: tuple[str, ...] | None, extra: Sequence[tuple[str, ...]] = ()) -> str:
    """What a code step may run, in one sentence (its prompt, and every refusal)."""
    runs = [" ".join(c) for c in ([check_command] if check_command else [])] + [" ".join(c) for c in extra]
    listed = (", ".join(f"`{r}`" for r in runs) + " (with extra arguments), and ") if runs else ""
    return (
        f"Only single commands are allowed: {listed}git status / diff / log / show / ls-files / grep / blame. No "
        "pipes, redirects, chaining, variables, other programs, or paths outside the worktree."
    )


# Options of the check programs that point them at other code or configuration than the project's own. Python's
# (pytest's) are self-improvement's list, glued and grouped short flags included.
_CHECK_BANNED = {
    "cargo": frozenset({"--manifest-path", "--config", "-Z", "--target-dir"}),
    "npm": frozenset({"--prefix", "--userconfig", "--globalconfig", "--script-shell", "--workspace", "-w"}),
    "dotnet": frozenset({"--configfile", "--source", "-s"}),
}


def _banned_check_option(program: str, arg: str) -> bool:
    if program == "python":
        return _banned_option(arg, git=False)
    if program == "node":  # node's own options load code (--require, --import, --loader): only its test runner's
        return arg.startswith("-") and not arg.startswith("--test")
    if program == "npm":  # npm takes abbreviations ("--script-sh") and short aliases ("-C"): no options of its own
        return arg.startswith("-") and arg != "--"
    banned = _CHECK_BANNED.get(program, frozenset())
    name = arg.split("=", 1)[0]
    if program == "cargo" and name.startswith("-Z"):  # glued: -Zunstable-options
        return True
    return name in banned or any(name.startswith(b) for b in banned if b.startswith("--"))


def protected(rel_path: str, extra: Iterable[str] = ()) -> bool:
    """Whether a code step may not change ``rel_path`` (worktree-relative, any slash style): a protected name at any
    depth, or anything under a protected folder."""
    path = rel_path.replace("\\", "/").strip("/").casefold()
    parts = path.split("/")
    for pattern in (*PROTECTED, *extra):
        pattern = pattern.casefold()
        if pattern.endswith("/**"):
            folder = pattern[:-3]
            if any("/".join(parts[i : i + folder.count("/") + 1]) == folder for i in range(len(parts))):
                return True
        elif any(fnmatch.fnmatchcase(part, pattern) for part in parts):
            return True
    # Windows spellings of another file: trailing dots or spaces, data streams, 8.3 aliases.
    return any(p != p.rstrip(". ") or ":" in p or "~" in p for p in parts)


def _file_call(name: str, tool_input: Any, root: Path, *, writable: bool) -> GuardDecision:
    if not isinstance(tool_input, dict):
        return _deny("Invalid tool input.")
    if name in ("Glob", "Grep"):
        if tool_input.get("path") not in (None, ""):
            resolved, why = _inside(root, tool_input.get("path"))
            if resolved is None:
                return _deny(why)
        for key in ("pattern", "glob") if name == "Glob" else ("glob",):
            why = _bad_pattern(tool_input.get(key))
            if why:
                return _deny(why)
        return _ALLOW
    resolved, why = _inside(root, tool_input.get("file_path"))
    if resolved is None:
        return _deny(why.replace("the builder", "you"))
    if name == "Read":
        return _ALLOW
    if not writable:
        return _deny("You can only read here.")
    rel = resolved.relative_to(root.resolve()).as_posix()
    if protected(rel):
        return _deny(
            f"{rel} decides what the project's checks run (or which packages it uses), so it can't be changed by "
            "this step. If the work needs it changed, say so in your result instead."
        )
    return _ALLOW


def _program(token: str) -> str:
    """ "npm.cmd", "NPM" and "C:/.../npm" are all npm."""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in (".cmd", ".exe", ".bat"):
        name = name.removesuffix(suffix)
    return name
