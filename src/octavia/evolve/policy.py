"""What the builder agent may touch and run. Deterministic, so a model can't talk its way past it.

Three layers use these rules:

* the builder's PreToolUse hook (``check_tool_call``) blocks each file access or
  command as it happens;
* the gate classifies every changed path from git (``classify_path``) and rejects a
  build that touched a forbidden one, however it got there;
* the gate flags sensitive paths, so they're always shown to the user and never
  installed automatically.

FORBIDDEN is the machinery that keeps the assistant safe: this package, the
permission policy and tool executor, the list of calls the user's own words
can approve (and which words count), the configuration, the operating
instructions, the dependency lists (Python's and the avatar app's npm ones),
the git setup and the settings of the gate's own checks (pytest's and ruff's
config files). A build may never change them, which is also why a build can
never loosen its own rules.

SENSITIVE is what the user should look at before it goes in: the core loop,
the brain, the built-in tools, the screen tools (they decide what of the
screen goes to Claude, and the user's words can approve them), the
personality, voice input (wake word, speech to text, voice activity, audio),
since an update that breaks hearing also takes away the spoken way to undo it,
and the avatar app, whose JavaScript the checks can only syntax-check and
unit-test.

Both cover every file Python would import in place of a protected module: a
package folder of the same name, another importable suffix, a cached .pyc, or a
copy at the project's top level (``_aliases``). What code does once it runs,
such as rebinding another module's names, is beyond any path rule.
"""

from __future__ import annotations

import importlib.machinery
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from octavia.evolve.models import PathClass

FORBIDDEN: tuple[str, ...] = (
    "src/octavia/evolve/**",
    "src/octavia/tools/permissions.py",
    "src/octavia/tools/executor.py",
    # Which calls the user's own words approve without a question, and which words count: part of the permissions.
    "src/octavia/tools/asked.py",
    # The terminal's rules for which commands just run, which ask and which are blocked (and their tests).
    "src/octavia/tools/extensions/terminal.py",
    "tests/test_terminal.py",
    # Downloads and long commands: where a download may write, which ones are always asked (and their tests). The
    # user's words can start a download (octavia.tools.asked names this class).
    "src/octavia/tools/extensions/long_jobs.py",
    "tests/test_long_jobs_rules*.py",
    # Real turns the router must give to Claude (each once went to the local model and got a guess): a build can't
    # drop a case to get a routing change through.
    "tests/test_routing_replay*.py",
    # The worker that carries the downloads out: it checks those rules again and never replaces a file.
    "src/octavia/tools/extensions/_long_job_runner.py",
    # The packages those modules are imported through: a package's __init__ can send its imports elsewhere
    # (``__path__.insert(0, ...)``), which would load a loosened copy of any module above in its place.
    "src/octavia/__init__.py",
    "src/octavia/tools/__init__.py",
    "src/octavia/core/__init__.py",
    "src/octavia/tools/base.py",
    "src/octavia/tools/registry.py",
    # Screen history: what of the screen is never kept (password managers, private windows, banks) and how the rest
    # is cleaned, and the package's __init__. A build can't loosen them.
    "src/octavia/recall/__init__.py",
    "src/octavia/recall/privacy.py",
    "tests/test_screen_history_privacy*.py",
    # Octavia Futures: the user's controls over what she learns, keeps and brings up (and their tests), and the
    # package's __init__, which could send those imports elsewhere. A build can't loosen them.
    "src/octavia/futures/__init__.py",
    "src/octavia/futures/privacy.py",
    "tests/test_futures_privacy*.py",
    # Her experiments on herself: what a habit may say or change, and how clear a result must be to keep one (and
    # its tests). A build can't make a habit say something new or keep one on weaker evidence.
    "src/octavia/futures/habit_rules.py",
    "tests/test_habit_rules*.py",
    # Money: what of the user's money is never kept (whole account numbers), which bank pages may be read and that
    # the bank browser never acts on a page, the purge, and the package's __init__ (and their tests). A build can't
    # loosen them.
    "src/octavia/finance/__init__.py",
    "src/octavia/finance/privacy.py",
    "tests/test_finance_privacy*.py",
    # Forgetting: which stores a "forget the last..." reaches, and the backup made before it (and their tests). A build
    # can't make it reach less, or delete without the backup.
    "src/octavia/core/forget.py",
    "tests/test_forget*.py",
    # Her inner life: what a night's reflection may keep (privacy, sensitivity, no cause from a correlation, no
    # instructions) and which tools it may use, its tests, and the package's __init__.
    "src/octavia/inner/__init__.py",
    "src/octavia/inner/rules.py",
    "tests/test_inner_rules*.py",
    # Learning by watching: what may be learned and offered (never titles or request words, only read-only tools,
    # the evidence a routine needs), its tests, and the package's __init__.
    "src/octavia/routines/__init__.py",
    "src/octavia/routines/rules.py",
    "tests/test_routines_rules*.py",
    # Skills: what a saved skill may hold and do (only the task's own tools, never a restricted one, nothing secret
    # written into it, no loops), its tests, and the package's __init__. A build can't loosen what a skill may do.
    "src/octavia/skills/__init__.py",
    "src/octavia/skills/rules.py",
    "tests/test_skills_rules*.py",
    # Her Claude usage budget: how much of the user's subscription her own background work (builds included) may
    # use, and its tests. A build can't raise its own allowance.
    "src/octavia/core/usage.py",
    "tests/test_usage_budget*.py",
    "config/**",
    "personality/system.md",
    "tests/test_evolve*.py",
    "tests/conftest.py",
    "pyproject.toml",
    "uv.lock",
    # The avatar app's npm dependencies and install script, like pyproject.toml and uv.lock for Python: "octavia
    # avatar setup" runs npm install with them. A shrinkwrap or .npmrc would change what that installs too.
    # node_modules is what npm installed: git ignores it, but a nested .gitignore can bring it back.
    "avatar/package.json",
    "avatar/package-lock.json",
    "avatar/npm-shrinkwrap.json",
    "avatar/.npmrc",
    "avatar/node_modules/**",
    ".python-version",
    ".gitignore",
    ".gitattributes",
    ".git/**",
    ".github/**",
    "scripts/**",
    ".env",
    ".env.*",
    "data/**",
    "models/**",
    ".venv/**",
    # What the gate's own checks read: a root conftest.py can report failures as passes, pytest.ini can
    # add --co, a nested ruff.toml can switch rules off, and sitecustomize runs in every python on src.
    # A nested conftest.py (fixtures for a subfolder) is allowed, but the gate flags it, and any other
    # pytest hook or plugin, as changing how pytest runs. The gate's pytest reads only pyproject.toml (-c);
    # the other config files pytest knows are protected too, since the builder's own test runs read them.
    "conftest.py",
    "pytest.ini",
    ".pytest.ini",
    "pytest.toml",
    ".pytest.toml",
    "tox.ini",
    "setup.cfg",
    "**/pyproject.toml",
    "**/ruff.toml",
    "**/.ruff.toml",
    "**/sitecustomize.py",
    "**/usercustomize.py",
    "**/.gitattributes",
)

SENSITIVE: tuple[str, ...] = (
    "src/octavia/core/assistant.py",
    # The assistant's parts: its ears, what it says unprompted, answers without the brain, and the confirmer that
    # decides when a risky call is asked (they were all assistant.py once).
    "src/octavia/core/hearing.py",
    "src/octavia/core/announcing.py",
    "src/octavia/core/quick_turns.py",
    "src/octavia/core/approvals.py",
    "src/octavia/core/turn_types.py",
    "src/octavia/core/yes_or_no.py",
    # The backstop that stops a stuck turn: a build that broke it could leave her hung again, unseen.
    "src/octavia/core/watchdog.py",
    "src/octavia/app.py",
    "src/octavia/cli.py",
    "src/octavia/config.py",
    # How every key is read from .env (and said never to be shown), and which certificates every connection trusts.
    "src/octavia/keys.py",
    "src/octavia/net.py",
    "src/octavia/brain/**",
    # Which brain answers a turn and which tools the local model may run (its router, delegation rules and server):
    # seven self-builds changed it in one night (2026-10-25) and it began answering "yes" and builds with guesses.
    "src/octavia/localbrain/**",
    "src/octavia/platform/**",
    "src/octavia/tools/builtin/**",
    # The screen tools: what of the screen goes to Claude, and the tools the user's words can approve
    # (octavia.tools.asked names these very classes), so a change to what they do is never installed unseen.
    "src/octavia/tools/extensions/screen_capture.py",
    "src/octavia/tools/extensions/screen_pointing.py",
    "src/octavia/tools/extensions/_uia.py",
    # Circling: when a picture of the screen goes with the user's words without a look of hers.
    "src/octavia/gesture/**",
    # Money: what's read of the user's banks and when, what's told, and the tools that read and delete it.
    "src/octavia/finance/**",
    "src/octavia/tools/extensions/finance.py",
    # Screen history: when and how the screen is read and kept, and the user's controls over it.
    "src/octavia/recall/**",
    "src/octavia/tools/extensions/screen_history.py",
    "src/octavia/visuals/film.py",
    # The extensions package itself: its __init__ could redirect where those modules are imported from.
    "src/octavia/tools/extensions/__init__.py",
    # Mouse and keyboard control, and opening files: which clicks, keys and files count as risky decides what
    # she does without asking, so a change there is always shown to the user first.
    "src/octavia/tools/extensions/computer_control.py",
    "src/octavia/tools/extensions/_input.py",
    "src/octavia/tools/extensions/files.py",
    # Restarting herself: the user's words can approve it (octavia.tools.asked names this class), only when its own look
    # finds no build it would cut off.
    "src/octavia/tools/extensions/restart.py",
    # Shutting herself down: the same, only when said at the PC and it stops nothing she's working on.
    "src/octavia/tools/extensions/shutdown.py",
    # Screen tasks: the user's words can start one (octavia.tools.asked names this class), and what a running task may
    # do on its own (never send, buy or delete) is decided there.
    "src/octavia/tools/extensions/screen_tasks.py",
    "src/octavia/tools/extensions/_screen_tasks.py",
    # Web tasks: what a task in her own signed-in browser may click, press and answer without being asked to (never
    # send, buy or delete), and how the page tells.
    "src/octavia/tools/extensions/web_tasks.py",
    "src/octavia/tools/extensions/_web_tasks.py",
    "src/octavia/tools/extensions/_web_browser.py",
    # Her quick voice's say over approvals (when the user's words or yes stand in for the question), and sending mail.
    "src/octavia/core/consent.py",
    "src/octavia/tools/extensions/gmail_send.py",
    # TickTick: which of its tools only read (never asked) and which change or delete things.
    "src/octavia/tools/extensions/ticktick.py",
    "src/octavia/tools/extensions/_ticktick_reminders.py",
    # Octavia Futures: what she records about the user, what she says on her own and when, and which of its tools
    # ask first (forgetting, loosening a control, starting an experiment).
    "src/octavia/futures/**",
    "src/octavia/tools/extensions/futures.py",
    # Her inner life: what she's given to think about overnight, when she reflects, and what she brings up.
    "src/octavia/inner/**",
    "src/octavia/tools/extensions/inner_life.py",
    # Learning by watching: what it reads, when it offers, and the user's say over it.
    "src/octavia/routines/**",
    "src/octavia/tools/extensions/routines.py",
    # Skills: how one runs inside a task, what's proposed and offered, and the user's say over saving one (the user's
    # words can approve save_skill: octavia.tools.asked names this class).
    "src/octavia/skills/**",
    "src/octavia/tools/extensions/skills.py",
    "personality/**",
    "src/octavia/wake/**",
    "src/octavia/stt/**",
    "src/octavia/vad/**",
    "src/octavia/audio/**",
    # The desktop avatar (Electron): the automatic checks can only syntax-check and unit-test its code.
    "avatar/**",
)

# The self-improvement tools. A build declaring a tool with one of these names is rejected:
# it could shadow install_improvement with a version that doesn't ask first.
RESERVED_TOOL_NAMES = frozenset(
    {
        "note_improvement",
        "list_improvements",
        "build_improvement",
        "install_improvement",
        "reject_improvement",
        "undo_last_improvement",
        "caught_mistakes",
        "restart_now",
        "shut_down_self",
        # Her skills' tools: a build's own save_skill could save one without asking, its use_skill run steps unchecked.
        "use_skill",
        "propose_skill",
        "save_skill",
        "forget_skill",
    }
)

BUILDER_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Glob", "Grep", "Bash", "TodoWrite")

_ALLOWED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "pytest"),
    ("python", "-m", "ruff"),
    ("pytest",),
    ("ruff",),
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("git", "ls-files"),
    ("git", "grep"),
)
# "$" covers $(...), ${...}, $HOME and $'\x2e\x2e': bash expands them, shlex doesn't.
_SHELL_META = (";", "&", "|", ">", "<", "`", "$", "\n", "\r")
# Bash turns {a,b} and {a..b} into several words (".{.,}/x" is "../x"); shlex sees one harmless word.
_BRACE_EXPANSION = re.compile(r"\{[^{}]*(?:,|\.\.)")
_MAX_COMMAND = 10_000  # shlex is slow on huge input, and no real command is this long
# Options that point a tool at other files, or make it run other programs.
_GIT_BANNED = ("--output", "--ext-diff", "--exec-path", "--git-dir", "--work-tree", "--textconv",
               "--open-files-in-pager", "-O", "-c", "-C")  # fmt: skip
_GIT_BANNED_LETTERS = "O"  # grep -O<pager> runs a program, also inside a group of flags: -iO<pager>
# git accepts any unambiguous prefix of a long option ("--open=calc" is --open-files-in-pager); these real
# options only look like such a prefix.
_GIT_EXACT = frozenset({"--text"})
_CHECK_BANNED = ("--config", "--rootdir", "--basetemp", "--confcutdir", "--cache-dir", "--pyargs", "--override-ini",
                 "--output-file", "--pastebin", "-c", "-p", "-o")  # fmt: skip
# pytest -c (config file), -p (plugin), -o (ini override); ruff -o (output file). Also grouped: "-qc<file>".
_CHECK_BANNED_LETTERS = "cpo"
_ALLOWED_HINT = (
    "Only single commands are allowed: python -m pytest [test files] [-q] [-k EXPR], python -m ruff check|format "
    "[files], and git status / diff / log / show / ls-files / grep. No pipes, redirects, chaining, variables, "
    "other programs, or paths outside the worktree."
)
_DRIVE = re.compile(r"^[A-Za-z]:")
_VALUE_START = re.compile(r"[=:]")
_SEGMENT = re.compile(r"[\\/:=,]")
# Windows device names: "NUL", "con.txt" or "COM1" is a device wherever it sits, not a file in the worktree.
_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
)
_SHORT_NAME = re.compile(r"~\d")  # 8.3 alias such as PYPROJ~1.TOM
# The file endings Python imports a module from: "x.pyw", "x.pyc" and "x.cp312-win_amd64.pyd" are the module x as
# much as x.py is (an extension module even wins over it). The fixed ones make the rules the same on any platform.
_MODULE_SUFFIXES = tuple(sorted({*importlib.machinery.all_suffixes(), ".py", ".pyw", ".pyc", ".pyd", ".so"}))
_SRC = "src/"


# ------------------------------------------------------------------ paths


def _normalize(rel_path: str) -> str:
    path = rel_path.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/").lower()


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """``**`` spans any number of directories (including none), ``*`` stays within one."""
    out, i = [], 0
    pattern = _normalize(pattern)
    while i < len(pattern):
        if pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")  # "config/**" also covers a file named "config" put in the directory's place
            i += 3
        elif pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


_FORBIDDEN_RE = [_glob_regex(p) for p in FORBIDDEN]
_SENSITIVE_RE = [_glob_regex(p) for p in SENSITIVE]


def classify_path(rel_path: str, extra_forbidden: Iterable[str] = ()) -> PathClass:
    """Classify a repo-relative path (any slash style, any case) as "forbidden", "sensitive" or "normal".

    The strictest class of the path and of every module file Python could take it for (``_aliases``): a
    src/octavia/tools/asked/__init__.py would be imported instead of the protected asked.py, so it is just as
    protected.
    """
    path = "/".join(PurePosixPath(_normalize(rel_path)).parts)  # drops "./" and "//" inside the path
    extra = [_glob_regex(p) for p in extra_forbidden if p.strip()]
    aliases = _aliases(path)
    if any(r.match(alias) for r in [*_FORBIDDEN_RE, *extra] for alias in aliases):
        return "forbidden"
    # A path escaping upward, or a spelling Windows maps onto another file (trailing dots or spaces, alternate
    # data streams like "pyproject.toml::$DATA", 8.3 aliases like PYPROJ~1.TOM), is never a normal source file.
    if any(p != p.rstrip(". ") or ":" in p or _SHORT_NAME.search(p) for p in path.split("/")):
        return "forbidden"
    if any(r.match(alias) for r in _SENSITIVE_RE for alias in aliases):
        return "sensitive"
    return "normal"


def module_name(rel_path: str) -> str | None:
    """The module Python imports from the file ``rel_path`` ("octavia.tools.asked"), or None when it imports nothing.

    src/octavia/tools/asked.py, src/octavia/tools/asked/__init__.py, src/octavia/tools/asked.pyw and (with the
    project folder on sys.path too, see ``_aliases``) octavia/tools/asked.py are all octavia.tools.asked.
    """
    module = _module_file(PurePosixPath(_normalize(rel_path)).parts)
    return module.removeprefix(_SRC).removesuffix(".py").replace("/", ".") if module is not None else None


def _module_file(parts: Sequence[str]) -> str | None:
    """The plain ``.py`` file of the module Python imports from this file (as path parts), or None.

    "x/y.pyw", "x/y.cp312-win_amd64.pyd", "x/__pycache__/y.cpython-312.pyc" and "x/y/__init__.py" are all the
    module x/y, as x/y.py is.
    """
    if not parts or not parts[-1].endswith(_MODULE_SUFFIXES):
        return None
    stem = parts[-1].partition(".")[0]  # a module name has no dots: "y.cp312-win_amd64.pyd" is y
    folder = list(parts[:-1])
    if folder and folder[-1] == "__pycache__":  # an unchecked cached .pyc is loaded whatever the source says
        folder.pop()
    if stem == "__init__":
        stem = folder.pop() if folder else ""
    return "/".join([*folder, f"{stem}.py"]) if stem else None


def _aliases(path: str) -> list[str]:
    """``path`` (normalized) and every module file Python could take it for, which share its protection.

    * Each folder on the way, as a module: a package x/y (x/y/__init__.py) is imported instead of x/y.py.
    * The module the file itself is, whatever its suffix (``_module_file``).
    * The same path under src/, for a path outside it: ``python -m`` puts the working folder ahead of src on
      sys.path, and both the gate's checks and the supervisor (started from the project folder) run that way, so
      a octavia/ folder at the top would be imported instead of src/octavia.
    """
    found: list[str] = []
    for candidate in (path,) if path.startswith(_SRC) else (path, _SRC + path):
        parts = candidate.split("/")
        found.append(candidate)
        found.extend("/".join(parts[:i]) + ".py" for i in range(1, len(parts)))
        module = _module_file(parts)
        if module is not None:
            found.append(module)
    return found


# ------------------------------------------------------------------ commands


def _looks_absolute(arg: str) -> bool:
    return bool(_DRIVE.match(arg)) or arg.startswith(("/", "\\", "~"))


def _has_parent_segment(arg: str) -> bool:
    return ".." in re.split(r"[\\/]", arg)


def _points_outside(arg: str) -> bool:
    """Whether a command argument could name something outside the worktree."""
    if arg.startswith("-") and not arg.startswith("--"):
        # A short option can carry its value glued on, even behind other flags: "-qc/elsewhere/pytest.ini".
        return ".." in arg or any(c in arg for c in "/\\:~")
    # Values start after "=" or ":" too: "--basetemp=/x", "HEAD:/x", and bash expands "cache_dir=a:~/x".
    if any(_looks_absolute(arg[i : i + 2]) for i in (0, *(m.end() for m in _VALUE_START.finditer(arg)))):
        return True
    # ".?" or ".*" can glob to ".." in older bash versions.
    return any(s == ".." or (s.startswith(".") and any(c in s for c in "*?[")) for s in _SEGMENT.split(arg))


def _banned_option(arg: str, git: bool) -> bool:
    if not arg.startswith("-"):
        return False
    banned = _GIT_BANNED if git else _CHECK_BANNED
    if not arg.startswith("--"):
        letters = _GIT_BANNED_LETTERS if git else _CHECK_BANNED_LETTERS
        # "-c", "-cvalue" (case-sensitive: -c vs -C), and a banned flag grouped with others: "-qpplugin".
        return any(arg.startswith(o) for o in banned if not o.startswith("--")) or any(c in letters for c in arg[1:])
    name = arg.split("=", 1)[0]
    for option in (o for o in banned if o.startswith("--")):
        if name.startswith(option):  # "--output", "--output=x", "--output-directory"
            return True
        if git and len(name) > 2 and option.startswith(name) and name not in _GIT_EXACT:  # "--open" (abbreviated)
            return True
    return False


def check_bash(command: str) -> str | None:
    """None if the builder may run ``command``; otherwise why not (shown to the builder)."""
    if not isinstance(command, str) or not command.strip():
        return "Empty command. " + _ALLOWED_HINT
    if len(command) > _MAX_COMMAND:
        return "That command is too long. " + _ALLOWED_HINT
    raw = command.rstrip("\r\n")
    if any(meta in raw for meta in _SHELL_META) or _BRACE_EXPANSION.search(raw):
        return "Shell operators, variables and brace expansion aren't allowed. " + _ALLOWED_HINT
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        return "Couldn't parse that command. " + _ALLOWED_HINT
    prefix = next((p for p in _ALLOWED_COMMANDS if tuple(tokens[: len(p)]) == p), None)
    if prefix is None:
        return f"'{raw}' isn't an allowed command. " + _ALLOWED_HINT
    git = prefix[0] == "git"
    args = tokens[len(prefix) :]
    if not git and "%" in raw:  # pytest expands %NAME% in --junitxml and ini paths
        return "Environment variables (%NAME%) aren't allowed. " + _ALLOWED_HINT
    if not git and any(arg.startswith("@") for arg in args):  # pytest and ruff read more arguments from @file
        return "Argument files (@file) aren't allowed. " + _ALLOWED_HINT
    for arg in args:
        if _points_outside(arg):
            return f"'{arg}' points outside the worktree. " + _ALLOWED_HINT
        if _banned_option(arg, git):
            return f"The option '{arg}' isn't allowed. " + _ALLOWED_HINT
    return None


# ------------------------------------------------------------------ tool calls


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str = ""


_ALLOW = GuardDecision(True)


def _deny(reason: str) -> GuardDecision:
    return GuardDecision(False, reason)


def _inside(worktree: Path, value: Any) -> tuple[Path | None, str]:
    """(resolved path inside the worktree, "") or (None, why not)."""
    if not isinstance(value, str) or not value.strip():
        return None, "A file path is required."
    if value.lstrip().startswith("~"):  # the CLI expands a leading ~ to the home directory
        return None, f"{value} starts with ~ (the home directory). Use a path inside the worktree."
    candidate = Path(value)
    if candidate.drive.startswith(("\\", "/")):  # \\server\share, \\?\C:\ - and don't touch the network resolving it
        return None, f"{value} is a network or device path. Only files in the worktree are available."
    root = worktree.resolve()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()  # follows symlinks and junctions: they can't smuggle a path out
    except (OSError, RuntimeError):
        return None, f"Couldn't resolve {value}."
    if not resolved.is_relative_to(root):
        return None, f"{value} is outside the worktree ({root}). Only files in the worktree are available."
    for part in resolved.relative_to(root).parts:
        name = part.lower()
        if name.rstrip(". ") == ".git":
            return None, "The .git metadata isn't available to the builder."
        # A data stream ("x.py:s"), a device ("NUL"), or a variable someone might expand ("$HOME", "%TEMP%").
        if any(c in name for c in ":$%") or name.split(".")[0].rstrip(" ") in _DEVICE_NAMES:
            return None, f"{value} isn't a plain file in the worktree (a device, data stream or variable)."
    return resolved, ""


def _brace_reaches_out(pattern: str) -> bool:
    """``{..,src}/*`` or ``{/,}x`` expands to a path outside; plain alternatives like ``*.{py,md}`` are fine."""
    depth = 0
    for c in pattern:
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth and c in "./\\~":
            return True
    return False


def _bad_pattern(pattern: Any) -> str | None:
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        return "The pattern must be text."
    if ":" in pattern or _looks_absolute(pattern) or _has_parent_segment(pattern) or _brace_reaches_out(pattern):
        return f"The pattern '{pattern}' reaches outside the worktree; use a relative pattern."
    return None


def check_tool_call(
    tool_name: str, tool_input: Mapping[str, Any] | Any, worktree: Path, extra_forbidden: Iterable[str] = ()
) -> GuardDecision:
    """Whether the builder may make this tool call. Never raises."""
    try:
        return _check(tool_name, tool_input, worktree, tuple(extra_forbidden))
    except Exception as exc:  # a guard that crashes must fail closed
        return _deny(f"Blocked ({type(exc).__name__} while checking the call).")


def _check(tool_name: str, tool_input: Any, worktree: Path, extra_forbidden: tuple[str, ...]) -> GuardDecision:
    if not isinstance(tool_input, Mapping):
        return _deny("Invalid tool input.")
    if tool_name == "TodoWrite":
        return _ALLOW
    if tool_name == "Bash":
        reason = check_bash(tool_input.get("command", ""))
        return _deny(reason) if reason else _ALLOW
    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
        resolved, why = _inside(worktree, tool_input.get(key))
        if resolved is None:
            return _deny(why)
        if tool_name != "Read":
            rel = resolved.relative_to(worktree.resolve()).as_posix()
            if classify_path(rel, extra_forbidden) == "forbidden":
                return _deny(
                    f"{rel} is protected: the assistant's safety machinery, configuration and dependencies "
                    "can't be changed by a build, under their own names or any other Python would import in "
                    "their place. Only the user can change this by hand: make no change and give the reason "
                    "in no_change_reason."
                )
        return _ALLOW
    if tool_name in ("Glob", "Grep"):
        if tool_input.get("path") not in (None, ""):
            resolved, why = _inside(worktree, tool_input.get("path"))
            if resolved is None:
                return _deny(why)
        for key in ("pattern", "glob") if tool_name == "Glob" else ("glob",):
            why = _bad_pattern(tool_input.get(key))
            if why:
                return _deny(why)
        return _ALLOW
    return _deny(f"{tool_name} isn't available to the builder. Available: {', '.join(BUILDER_TOOLS)}.")
