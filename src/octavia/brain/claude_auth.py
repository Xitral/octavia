"""Signing the assistant's private Claude Code config in, and checking it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from octavia.brain.claude import bundled_cli_path, cli_environment
from octavia.config import AppConfig


def _cli(config: AppConfig) -> Path | None:
    path = config.claude_cli_path()
    return Path(path) if path else bundled_cli_path()


def _env(config: AppConfig) -> dict[str, str]:
    cfg = config.brain.claude
    config_dir = config.resolve_path(cfg.config_dir) if cfg.config_dir else None
    env = dict(os.environ)
    env.update(cli_environment(config_dir))
    if cfg.auth == "subscription":
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def auth_status(config: AppConfig) -> dict | None:
    """`claude auth status` as a dict, or None if the CLI couldn't be run."""
    cli = _cli(config)
    if cli is None:
        return None
    try:
        out = subprocess.run(
            [str(cli), "auth", "status"],
            env=_env(config),
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def login(config: AppConfig) -> int:
    """Interactive browser sign-in with your Claude subscription (the user completes it)."""
    cli = _cli(config)
    if cli is None:
        print("Claude Code CLI not found; run `uv sync` first.")
        return 1
    cfg = config.brain.claude
    where = config.resolve_path(cfg.config_dir) if cfg.config_dir else Path.home() / ".claude"
    print(f"Signing {config.assistant.name} in to Claude (credentials stored in {where}).")
    print("A browser window will open: sign in and pick your Claude subscription.\n")
    code = subprocess.call([str(cli), "auth", "login", "--claudeai"], env=_env(config))
    status = auth_status(config) or {}
    if status.get("loggedIn"):
        print(f"\nSigned in ({status.get('authMethod', 'ok')}). Check it with: uv run octavia doctor --live")
        return 0
    print("\nNot signed in yet. Try again, or see README.md > Setup.")
    return code or 1
