"""The user's projects she may code in, and how each one is checked.

A project is a git repository directly inside one of ``objectives.project_roots`` (her own checkout and its worktrees
are left out: changes to her go through self-improvement, which has its own gate). Its check is the command that
says whether a change broke it, found from the project's files by code, never chosen by a model:

* ``Cargo.toml``: ``cargo test`` (builds share one target folder per project, so a step doesn't rebuild every crate);
* ``package.json`` with a real ``test`` script: ``npm test``, else a ``build`` script: ``npm run build``;
* a project ``.venv`` with ``tests/``: that venv's ``python -m pytest``;
* a ``.sln``: ``dotnet build``;
* or the command in ``objectives.checks`` for that project, which always wins.

No check means code steps are only reviewed, never tested, and every report says so.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from octavia.evolve.git import Git, GitError

log = logging.getLogger(__name__)

# Files a code step may never change: what decides which code runs as "the checks" (manifests, lock files, build
# scripts, test-runner settings), what git does with the files (ignores, attributes, hooks), and CI, so a step can't make
# its own checks pass by changing them, pull in packages nobody chose, or get files committed or hidden from review.
PROTECTED = (
    "Cargo.toml",
    "Cargo.lock",
    "build.rs",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "uv.lock",
    "requirements*.txt",
    "setup.py",
    "setup.cfg",
    "*.csproj",
    "*.sln",
    "Directory.Build.props",
    ".npmrc",
    ".cargo/**",
    ".github/**",
    ".git/**",
    ".gitmodules",
    ".gitignore",
    ".gitattributes",
    ".husky/**",
    ".githooks/**",
    ".pre-commit-config.yaml",
    "node_modules/**",
    "npm-shrinkwrap.json",
    ".yarnrc*",
    "jest.config.*",
    "vitest.config.*",
    "vite.config.*",
    "karma.conf.*",
    "pytest.ini",
    "tox.ini",
    "sitecustomize.py",
    "usercustomize.py",
    "rust-toolchain*",
    "nuget.config",
    "global.json",
    "Directory.Build.targets",
    "Directory.Packages.props",
)
_NO_TEST = "no test specified"  # npm init's placeholder test script


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    check: tuple[str, ...] | None  # the command as it is typed ("npm", "test"); None: no check
    check_note: str  # where the check came from, or why there's none
    has_commits: bool
    links: tuple[str, ...] = ()  # git-ignored folders a worktree borrows from the checkout (node_modules)

    def describe(self) -> str:
        check = " ".join(self.check) if self.check else f"none ({self.check_note})"
        commits = "" if self.has_commits else " It has no commits yet, so I can't work on a branch of it."
        return f"{self.name} ({self.path}): checked with {check}.{commits}"


def discover(roots: list[Path], own_root: Path, checks: dict[str, str] | None = None) -> list[Project]:
    """Every git project directly inside ``roots``, her own repository and its worktrees left out."""
    checks = {k.casefold(): v for k, v in (checks or {}).items()}
    own = _common_dir(own_root)
    found: dict[str, Project] = {}
    for root in roots:
        try:
            children = sorted(p for p in Path(root).expanduser().iterdir() if p.is_dir())
        except OSError:
            continue
        for path in children:
            if not (path / ".git").exists() or path.name.casefold() in found:
                continue
            git = Git(path)
            if not git.is_repo() or (own is not None and _common_dir(path) == own):
                continue
            found[path.name.casefold()] = make_project(path, checks.get(path.name.casefold()))
    return sorted(found.values(), key=lambda p: p.name.casefold())


def make_project(path: Path, override: str | None = None) -> Project:
    git = Git(path)
    try:
        commits = git.has_commits()
    except GitError:
        commits = False
    if override:
        check, note = tuple(shlex.split(override, posix=False)), "set in objectives.checks"
    else:
        check, note = infer_check(path)
    links = ("node_modules",) if (path / "node_modules").is_dir() and check and check[0] == "npm" else ()
    return Project(path.name, path, check, note, commits, links)


def infer_check(path: Path) -> tuple[tuple[str, ...] | None, str]:
    """(the check command, where it came from) or (None, why there's none)."""
    if (path / "Cargo.toml").is_file():
        return (("cargo", "test"), "Cargo.toml") if shutil.which("cargo") else (None, "cargo isn't installed")
    if (path / "package.json").is_file():
        scripts = _scripts(path / "package.json")
        if not shutil.which("npm"):
            return None, "npm isn't installed"
        if not (path / "node_modules").is_dir():
            return None, "its packages aren't installed (no node_modules)"
        test = scripts.get("test", "")
        if test and _NO_TEST not in test:
            return ("npm", "test"), "package.json's test script"
        if scripts.get("build"):
            return ("npm", "run", "build"), "package.json's build script (it has no tests)"
        return None, "package.json has no test or build script"
    venv_python = path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if (path / "pyproject.toml").is_file() or (path / "setup.py").is_file():
        if venv_python.is_file() and (path / "tests").is_dir():
            return ("python", "-m", "pytest"), "its .venv and tests folder"
        return None, "no .venv with a tests folder"
    if any(path.glob("*.sln")):
        return (("dotnet", "build"), "the .sln") if shutil.which("dotnet") else (None, "dotnet isn't installed")
    return None, "no build or test setup I recognize"


def resolve(projects: list[Project], ref: str) -> list[Project]:
    """The projects ``ref`` names: an exact folder name, else every one whose name contains it (any case, spaces,
    dashes and underscores ignored)."""
    wanted = _squash(ref)
    if not wanted:
        return []
    exact = [p for p in projects if _squash(p.name) == wanted]
    return exact or [p for p in projects if wanted in _squash(p.name)]


def executable(project: Project, cwd: Path) -> list[str] | None:
    """The check as a command line to run in ``cwd``: the program resolved (npm is npm.cmd on Windows), a project's
    own venv for python."""
    if not project.check:
        return None
    program, *args = project.check
    venv = project.path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if program == "python" and venv.is_file():
        return [str(venv), *args]
    found = shutil.which(program)
    return [found or program, *args]


def check_environment(project: Project, cwd: Path, cache: Path) -> dict[str, str]:
    """Environment overrides for the check (and for the code step's own runs of it)."""
    env = {"CI": "1", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"}
    if project.check and project.check[0] == "cargo":
        env["CARGO_TARGET_DIR"] = str(cache / "cargo-target" / project.name)
    if project.check and project.check[0] == "python":
        env["PYTHONPATH"] = str(cwd / "src") if (cwd / "src").is_dir() else str(cwd)
    return env


def _scripts(package: Path) -> dict[str, str]:
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}


def _common_dir(path: Path) -> Path | None:
    try:
        out = Git(path).run("rev-parse", "--path-format=absolute", "--git-common-dir", timeout=20).strip()
    except GitError:
        return None
    return Path(os.path.normcase(str(Path(out).resolve()))) if out else None


def _squash(text: str) -> str:
    return "".join(c for c in text.casefold() if c.isalnum())
