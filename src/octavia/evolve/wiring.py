"""Builds the self-improvement components from config (used by the app and the CLI)."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octavia.config import AppConfig
from octavia.core import usage as claude_usage
from octavia.core.events import EventBus
from octavia.evolve import mistakes
from octavia.evolve.builder import BuilderSettings, ClaudeBuilder
from octavia.evolve.evals import EvalSettings
from octavia.evolve.frontier import FrontierSettings
from octavia.evolve.gate import Gate, GateSettings
from octavia.evolve.git import Git
from octavia.evolve.journal import Journal
from octavia.evolve.manager import ImprovementManager
from octavia.evolve.models import EvolvePaths
from octavia.evolve.nightly import NightSettings, NightShift
from octavia.evolve.supervisor import is_supervised

log = logging.getLogger(__name__)


def venv_python(root: Path) -> Path:
    """The console python of the project's environment (checks and tests run with it)."""
    candidate = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if candidate.exists():
        return candidate
    current = Path(sys.executable)
    return current.with_name("python.exe") if current.name.lower() == "pythonw.exe" else current


@dataclass
class EvolveComponents:
    manager: ImprovementManager
    journal: Journal
    git: Git
    paths: EvolvePaths
    closers: list[Any] = field(default_factory=list)
    night: NightShift | None = None  # her night shift (``nightly_at``); started by the voice assistant only

    async def summary_for_prompt(self) -> str:
        """The self-improvement status line, and what her last night shift did."""
        parts = [await self.manager.summary_for_prompt()]
        if self.night is not None:
            try:
                parts.append(await self.night.summary_for_prompt())
            except Exception:
                log.warning("Couldn't summarize the night shift", exc_info=True)
        return "\n".join(p for p in parts if p)


def _app_dbs(config: AppConfig) -> tuple[Path, ...]:
    """Where her records say which app was in front when (for mistakes the user fixed by hand)."""
    dbs = []
    if config.screen_history.enabled:
        dbs.append(config.resolve_path(config.screen_history.database))
    if config.futures.enabled:
        dbs.append(config.resolve_path(config.futures.database))
    return tuple(dbs)


def create_evolve(config: AppConfig, events: EventBus, supervised: bool | None = None) -> EvolveComponents:
    si = config.self_improvement
    root = config.root
    # Her Claude usage budget holds back the builds she starts by herself: set up here too (the app does it first),
    # so no change outside this package can leave her building with no budget at all.
    if claude_usage.budget() is None:
        claude_usage.configure(config)
    paths = EvolvePaths(root)
    paths.dir.mkdir(parents=True, exist_ok=True)
    python = venv_python(root)
    extra = tuple(si.extra_protected_paths)
    models = config.resolve_path("models")  # a worktree has no copy of the downloaded models: use these
    git = Git(root)
    journal = Journal(paths.journal)
    gate = Gate(
        git,
        GateSettings(
            python=python,
            test_timeout_s=si.test_timeout_min * 60,
            extra_forbidden=extra,
            models_dir=models,
            evaluate=True,
        ),
    )
    claude = config.brain.claude
    builder = ClaudeBuilder(
        BuilderSettings(
            model=si.builder_model,
            effort=si.builder_effort,
            max_turns=si.builder_max_turns,
            timeout_s=si.build_timeout_min * 60,
            python=python,
            config_dir=config.resolve_path(claude.config_dir) if claude.config_dir else None,
            cli_path=config.claude_cli_path(),
            auth=claude.auth,
            assistant_name=config.assistant.name,
            extra_forbidden=extra,
            models_dir=models,
        )
    )
    manager = ImprovementManager(
        config,
        journal,
        git,
        builder,
        gate,
        events,
        supervised=is_supervised() if supervised is None else supervised,
        paths=paths,
    )
    night = None
    if si.nightly_at:
        night = NightShift(
            manager,
            NightSettings(
                at=si.nightly_at,
                morning=si.morning_report,
                max_builds=si.nightly_builds,
                routing_log=config.resolve_path(config.routing.log_file),
                dir=paths.dir,
                eval=EvalSettings(python=python, repo=root),
                caught=mistakes.pool_path(root),
                app_dbs=_app_dbs(config),
                exclude_apps=tuple(config.screen_history.exclude_apps),
                draft=_drafter(config),
                frontier=FrontierSettings(root=root, python=python) if si.frontier_weekly else None,
            ),
        )
        manager.hooks.quiet = night.quiet
    closers = [manager.close, journal.close]
    if night is not None:
        closers.insert(0, night.close)
    return EvolveComponents(manager=manager, journal=journal, git=git, paths=paths, closers=closers, night=night)


def _drafter(config: Any) -> Any:
    """The night shift's drafts of what a good reply does (``evolve/rubrics.py``), a few a night on her Claude login."""
    from octavia.evolve import rubrics
    from octavia.evolve.commands import rubric_asker, rubric_replies

    ask, replies = rubric_asker(config), rubric_replies(config)

    async def budget() -> str | None:
        from octavia.core import usage

        return await usage.check_fresh("caught-mistake rules")

    async def draft(pool: Any) -> list[Any]:
        return await rubrics.draft(pool, ask, replies, budget=budget)

    return draft
