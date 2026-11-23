"""``octavia avatar ...``: install the avatar app (``setup``), look at the character without the assistant
(``preview``), and list the props she can hold (``props``)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from octavia.avatar.bridge import (
    APP_DIR,
    PROP_ACTIVITIES,
    PROP_DEFAULT_SIZE_M,
    PropScan,
    app_config,
    electron_path,
    needs_model,
    prop_name,
    scan_props,
)
from octavia.config import AppConfig

# The preview's own config file: the assistant's avatar restarts from bridge.CONFIG_FILE after a crash, and must not
# pick up a preview's settings (a prop held the whole time) from it.
PREVIEW_CONFIG_FILE = "data/avatar/preview.json"


def setup(config: AppConfig) -> int:
    """``npm install`` in avatar/ (Electron, three.js, three-vrm), then check the model is where the config says."""
    app = config.root / APP_DIR
    npm = shutil.which("npm")
    if npm is None:
        print("npm isn't installed. Install Node.js (https://nodejs.org, the LTS version), then run this again.")
        return 1
    print(f"Installing the avatar app's parts in {app} (Electron and three.js, about 200 MB)...")
    result = subprocess.run([npm, "install", "--no-fund", "--no-audit"], cwd=app, check=False)
    if result.returncode != 0:
        print("npm install failed; see the messages above.")
        return 1
    if not electron_path(config.root, config.avatar.electron).is_file():
        print("npm finished, but Electron isn't where it should be. Try deleting avatar/node_modules and again.")
        return 1
    model = config.resolve_path(config.avatar.model)
    if needs_model(config.avatar) and not model.is_file():
        print(f"Installed. Now put your character (a .vrm file) at {model}, or point avatar.model at it.")
        return 0
    print("Installed. See your character with `uv run octavia avatar preview`.")
    if not config.avatar.enabled:
        print("To have it with the assistant, add this to config/local.yaml:\n  avatar:\n    enabled: true")
    return 0


def preview(config: AppConfig, prop: str | None = None) -> int:
    """The character on its own, running through its states, moods and gestures (close it with Ctrl+C).

    With ``prop`` she holds that prop the whole time, for tuning its settings file.
    """
    exe = electron_path(config.root, config.avatar.electron)
    if not exe.is_file():
        print("The avatar app isn't installed yet: run `uv run octavia avatar setup` first.")
        return 1
    model = config.resolve_path(config.avatar.model)
    if needs_model(config.avatar) and not model.is_file():
        print(f"No character at {model} (avatar.model).")
        return 1
    found = scan_props(config)
    if found.problems:
        _print_problems(found)
        print()
    held = None
    if prop is not None:
        held = prop_name(prop)
        if held not in found.props:
            print(f"There's no prop called {prop!r}. Props: {', '.join(sorted(found.props))} (`octavia avatar props`).")
            return 1
    config_path = config.resolve_path(PREVIEW_CONFIG_FILE)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(app_config(config, model, found, preview_prop=held)), encoding="utf-8")
    if held is None:
        print("Showing the preview (it loops through states and moods). Ctrl+C to close it.")
    else:
        file = found.props[held].file
        settings = file.with_suffix(".json") if file is not None else found.folder / f"{held}.json"
        print(f"Showing the preview with her holding {held} (also up to her eye when she looks closely). To change")
        print("how she holds it, edit")
        print(f"{settings} and run this again (docs/avatar.md lists the settings). Ctrl+C to close it.")
    try:
        return subprocess.run(
            [str(exe), str(config.root / APP_DIR), "--preview", "--config", str(config_path)], check=False
        ).returncode
    except KeyboardInterrupt:
        return 0


def props(config: AppConfig) -> int:
    """List the props she can hold, and which one each activity uses."""
    found = scan_props(config)
    uses: dict[str, list[str]] = {}
    for activity, name in found.activities.items():
        if name is not None:
            uses.setdefault(name, []).append(PROP_ACTIVITIES[activity])
    print(f"Props she can hold (from {found.folder}, the built-ins and the ones she made):\n")
    rows = []
    for name, prop in sorted(found.props.items()):
        source = prop.file.name if prop.file is not None else "made by her" if prop.parts else "built-in"
        own_size = prop.file is None and prop.size == PROP_DEFAULT_SIZE_M  # the app keeps a built-in's own size
        size = "own size" if own_size else f"{prop.size:g} m"
        used = f"used when {' and '.join(uses[name])}" if name in uses else ""
        rows.append((name, source, f"{prop.hand} hand", size, used))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for *cells, used in rows:
        line = "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))
        print(f"  {line}  {used}".rstrip())
    empty_handed = [activity for activity, name in found.activities.items() if name is None]
    if empty_handed:
        print()
    for activity in empty_handed:
        print(f"Nothing is held when {PROP_ACTIVITIES[activity]} (avatar.props.{activity}).")
    _print_problems(found)
    print(f"\nTo add one, put a .glb file (not compressed) in {found.folder}")
    print("(its file name is the prop's name), then try it with `uv run octavia avatar preview --prop NAME`.")
    print("docs/avatar.md has the details.")
    return 0


def _print_problems(found: PropScan) -> None:
    if found.problems:
        print("\nProblems with the props:")
        for problem in found.problems:
            print(f"  ! {problem}")


def run_command(config: AppConfig, command: str, *, prop: str | None = None) -> int:
    if command == "setup":
        return setup(config)
    if command == "preview":
        return preview(config, prop)
    if command == "props":
        return props(config)
    print(f"Unknown avatar command: {command}", file=sys.stderr)
    return 2
