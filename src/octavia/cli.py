"""Command-line entry points.

octavia                 run the voice assistant (same as `octavia run`)
octavia login           sign the assistant in to your Claude subscription (one time)
octavia chat            type instead of talk (same brain and tools)
octavia devices         list audio devices
octavia setup           download the local models (VAD, wake word, speech recognition)
octavia doctor          check configuration, devices, models and credentials
octavia say TEXT        speak a sentence through the TTS voice
octavia listen-test     live view of wake-word / VAD / transcription, for tuning
octavia wake-check      test the wake phrase with synthesized speech (after renaming)
octavia autostart ...   install / uninstall / status of start-at-login
octavia revive          start her if her supervisor died (what the OctaviaRevive scheduled task runs)
octavia explorer-menu   install / uninstall / status of her entries in Explorer's right-click menu
octavia settings        her settings window (microphone, speaker, volume, shortcuts, quiet hours)
octavia usage           your Claude limits, and what her own background work used and skipped
octavia evolve ...      self-improvement: setup, list, show, build, install, reject, undo, night, progress, mistakes
octavia avatar ...      the 3D character on the desktop: setup, preview, props
octavia futures ...     Octavia Futures, her evidence-based model: status, events, report, explain, trace, reset...
octavia inner ...       Her inner life: status, thoughts, journal, opinions, history, digest, forget, reset
octavia routines ...    What she learned by watching: list, look, forget, offers-on/off, reset
octavia test            the whole test suite as the gate runs it, one heavy run at a time (--quick: just what changed)
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import subprocess
import sys
import time
import typing
from pathlib import Path
from typing import Any

from octavia.config import AppConfig, WakeConfig, find_project_root, load_config
from octavia.core.logs import exit_now, headless, setup_logging

log = logging.getLogger("octavia")

# True when started without a console (octaviaw.exe / pythonw.exe, e.g. from autostart).
# Captured before stdio is redirected, because headless() is False afterwards.
_STARTED_HEADLESS = False


def _capture_headless_stdio() -> None:
    """Without a console, print() goes nowhere: send it to a file before anything can fail.

    Otherwise a typo in config/local.yaml under autostart would leave no trace at all.
    """
    global _STARTED_HEADLESS
    # Piped or redirected output (`octavia evolve show 3 | more`, a log) uses the ANSI code page, which can't hold
    # "⚠" or "→": a report with one in it crashed. Unencodable characters become "?" instead.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    if not headless():
        return
    _STARTED_HEADLESS = True
    path = find_project_root() / "data" / "logs" / "octavia.stdio.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the process
    stream.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(sys.argv)}\n")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="octavia", description="Local voice-first desktop assistant")
    p.add_argument("--config", help="Path to a config YAML with your overrides (default: config/local.yaml)")
    p.add_argument("-v", "--verbose", action="store_true", help="Show more detail in the console")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the voice assistant")
    run.add_argument(
        "--wake",
        choices=list(typing.get_args(WakeConfig.model_fields["engine"].annotation)),
        help="Override wake engine",
    )
    run.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    run.add_argument(
        "--no-supervisor",
        action="store_true",
        help="Run without the supervisor process (self-improvement can't restart into updates or undo a bad one)",
    )

    chat = sub.add_parser("chat", help="Text chat with the assistant")
    chat.add_argument("--speak", action="store_true", help="Also speak the replies")

    sub.add_parser("login", help="Sign the assistant in to your Claude subscription (opens a browser)")
    wel = sub.add_parser("welcome", help="The setup wizard: sign-in, mic, voice, name, home, connections")
    wel.add_argument("--window", action="store_true", help="The guided window (the console wizard without Electron)")
    release = sub.add_parser("release", help="The public build: a clean, scanned export (never published)")
    rel = release.add_subparsers(dest="release_command", required=True)
    rb = rel.add_parser("build", help="Export the last commit's shippable files, scan them, zip them")
    rb.add_argument("--out", default="dist", help="Folder for the build (default: dist)")
    rs = rel.add_parser("scan", help="Check a folder for anything personal or secret")
    rs.add_argument("folder")
    rg = rel.add_parser("github", help="Build, scan (files, README, title, notes) and publish to the public repository")
    rg.add_argument("--title", required=True)
    rg.add_argument("--notes-file", required=True)
    rg.add_argument("--passed", type=int)
    rg.add_argument("--total", type=int)
    from octavia.hive.cli import add_parser as _hive_parser

    _hive_parser(sub)
    sub.add_parser("devices", help="List audio devices")
    setup = sub.add_parser("setup", help="Download local models")
    setup.add_argument("--force", action="store_true", help="Re-download even if present")

    routing = sub.add_parser("routing", help="How your turns were routed (rules, presets, the brain) and how fast")
    routing.add_argument("--days", type=float, default=7.0, help="Only the last N days (default 7; 0 = everything)")

    memory = sub.add_parser("memory", help="Her saved notes: `memory check` lists notes that disagree")
    memory.add_argument("action", choices=["check"], help="check: notes about the same thing told two ways")
    memory.add_argument("--fix", action="store_true", help="also fold notes the newer one says it corrects")

    shadow = sub.add_parser("shadow", help="What the local model would have done on your turns, next to what she did")
    shadow.add_argument("--days", type=float, default=7.0, help="Only the last N days (default 7; 0 = everything)")

    addressee = sub.add_parser(
        "addressee", help="How often she'd have answered what you said without her name (shadow mode)"
    )
    addressee.add_argument(
        "action", nargs="?", default="report", choices=["report", "show", "mark", "forget"],
        help="report (default); show: also the lines she'd have answered; mark ID yes|no; forget: delete the log",
    )  # fmt: skip
    addressee.add_argument("rest", nargs="*", help="for mark: the line's id and yes or no")
    addressee.add_argument("--days", type=float, default=7.0, help="Only the last N days (default 7; 0 = everything)")

    doctor = sub.add_parser("doctor", help="Check that everything is set up")
    doctor.add_argument("--live", action="store_true", help="Also make one tiny real request to Claude and Fish Audio")

    say = sub.add_parser("say", help="Speak text with the configured voice")
    say.add_argument("text", nargs="+")

    lt = sub.add_parser("listen-test", help="Show wake-word, VAD and transcription live")
    lt.add_argument("--seconds", type=float, default=60.0)
    wc = sub.add_parser("wake-check", help="Test the wake phrase with synthesized speech (use after renaming)")
    wc.add_argument("--phrase", help="Try a different phrase without editing config")

    env = sub.add_parser("env", help="Check the keys in .env (never shows a value); `env tidy` sorts the file")
    env.add_argument("action", nargs="?", choices=["check", "tidy", "example"], default="check")
    env.add_argument("--dry-run", action="store_true", help="tidy: show the new layout (values hidden), change nothing")
    sub.add_parser("settings", help="Her settings window: every setting by section or search, applied while she runs")
    auto = sub.add_parser("autostart", help="Start the assistant when you log in to Windows")
    auto.add_argument("action", choices=["install", "uninstall", "status"])
    auto.add_argument("--method", choices=["RunKey", "StartupFolder", "Task"], default="RunKey")

    sub.add_parser("revive", help="Start her if her supervisor died; does nothing if she was shut down on purpose")
    forget_cmd = sub.add_parser(
        "forget", help="Forget the last 15, 30 or 60 minutes or day of everything she kept (a backup is made first)"
    )
    forget_cmd.add_argument("last", choices=["15", "30", "60", "day"], help="minutes, or 'day'")
    forget_cmd.add_argument("--dry-run", action="store_true", help="only say what would go, store by store")
    forget_cmd.add_argument("--yes", action="store_true", help="don't ask before deleting")

    menu = sub.add_parser("explorer-menu", help="Her entries in Explorer's right-click menu (files, folders, desktop)")
    menu.add_argument("action", choices=["install", "uninstall", "status"])

    usage = sub.add_parser("usage", help="Your Claude limits, and what her own background work used and skipped")
    usage.add_argument("--days", type=float, default=7, help="How far back the job and skip lists go (default 7)")
    evolve = sub.add_parser("evolve", help="Self-improvement: what it noted, built and installed")
    ev = evolve.add_subparsers(dest="evolve_command", required=True)
    ev.add_parser("setup", help="Check git and make the first commit so builds can start")
    ls = ev.add_parser("list", help="Show noted, building, ready and installed improvements")
    ls.add_argument("--all", action="store_true", help="Include closed ones")
    show = ev.add_parser("show", help="Details, checks and the code change of one improvement")
    show.add_argument("id", type=int)
    build = ev.add_parser("build", help="Build an improvement now (in this window)")
    build.add_argument("id", type=int, nargs="?", help="An improvement's number (see `octavia evolve list`)")
    build.add_argument("--request", help="Or describe a new change in plain words")
    install = ev.add_parser("install", help="Install built improvements together (then restart the assistant)")
    install.add_argument("ids", type=int, nargs="*", help="Their numbers (see `octavia evolve list`)")
    install.add_argument("--all", action="store_true", help="Every improvement that's ready")
    reject = ev.add_parser("reject", help="Drop an improvement")
    reject.add_argument("id", type=int)
    ev.add_parser("undo", help="Undo the last installed improvement")
    night = ev.add_parser("night", help="What her night shift would find in the day's turns (--run: do it now)")
    night.add_argument("--run", action="store_true", help="Start the fixes and run the real-request check now")
    ev.add_parser("progress", help="Her night-by-night record, and the chart of it (data/evolve/progress.html)")
    caught = ev.add_parser(
        "mistakes", help="Requests she got wrong and caught herself, kept as cases of her real-request check"
    )
    caught.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "show", "keep", "drop", "scan", "suggest"],
        help="list (default); show N; keep N: grade it for good; drop N: not a mistake; scan: look through the log; "
        "suggest: draft what a good reply does for complaints about a reply",
    )
    caught.add_argument("n", type=int, nargs="?", help="The mistake's number (see `octavia evolve mistakes`)")
    caught.add_argument("--all", action="store_true", help="list: dropped ones too")
    caught.add_argument("--expect", action="append", metavar="TOOL", help='keep: she should call this ("a|b": either)')
    caught.add_argument("--forbid", action="append", metavar="TOOL", help="keep: she shouldn't call this")
    caught.add_argument("--no-tools", action="store_true", help="keep: she should answer without tools")
    caught.add_argument(
        "--judge",
        metavar="TEXT",
        help='keep: what a good reply does, judged on her reply ("asks one question at a time")',
    )
    caught.add_argument("--days", type=float, default=30, help="scan: how far back in the routing log (default 30)")
    caught.add_argument("--dry-run", action="store_true", help="scan: only show what it finds")
    caught.add_argument("--limit", type=int, default=10, help="suggest: at most this many drafts (default 10)")

    avatar = sub.add_parser("avatar", help="The 3D character on the desktop")
    av = avatar.add_subparsers(dest="avatar_command", required=True)
    av.add_parser("setup", help="Install the avatar app (Electron and three.js, via npm)")
    preview = av.add_parser("preview", help="Show the character on its own, looping through its moods")
    preview.add_argument("--prop", metavar="NAME", help="Have her hold this prop the whole time (to tune it)")
    av.add_parser("props", help="List the props she can hold and which activity uses each")

    futures = sub.add_parser("futures", help="Octavia Futures: what she recorded, learned and predicted")
    fu = futures.add_subparsers(dest="futures_command", required=True)
    fu.add_parser("status", help="The database, how much is in it, and your controls")
    events = fu.add_parser("events", help="The newest events")
    events.add_argument("--type", help="Only this type (e.g. task.completed)")
    events.add_argument("--limit", type=int, default=30)
    world = fu.add_parser("world", help="Entities and their relations")
    world.add_argument("--kind", help="Only this kind (task, application, person...)")
    for name, what in (
        ("episodes", "Episodes"),
        ("outcomes", "Measured outcomes"),
        ("hypotheses", "Hypotheses and their evidence"),
        ("predictions", "Predictions, open and settled"),
        ("experiments", "Experiments"),
        ("triggers", '"Remind me when" triggers'),
        ("recommendations", "What she said (or held back) on her own, and why"),
        ("performance", "Her own measured performance"),
    ):
        fu.add_parser(name, help=what).add_argument("--limit", type=int, default=30)
    report = fu.add_parser("report", help="The text a question gets (same as the futures_report tool)")
    report.add_argument("topic")
    habits = fu.add_parser("habits", help="Habits she kept from experiments on herself: list, drop N, restore N")
    habits.add_argument("action", nargs="?", choices=("list", "drop", "restore"), default="list")
    habits.add_argument("number", nargs="?", type=int)
    explain = fu.add_parser("explain", help="Why she said something: latest, or e.g. 'prediction 3'")
    explain.add_argument("what", nargs="*", default=["latest"])
    trace = fu.add_parser("trace", help="The audit trail: what changed, what caused it, what reasoned it")
    trace.add_argument("object", nargs="?", help="e.g. hypothesis:3 (default: everything, newest first)")
    trace.add_argument("--limit", type=int, default=40)
    html = fu.add_parser("html", help="Write a debug page of everything to a file and print its path")
    html.add_argument("--out", help="Where (default data/futures/debug.html)")
    reset = fu.add_parser("reset", help="Delete everything she learned (your controls stay)")
    reset.add_argument("--yes", action="store_true", help="Really")

    from octavia.inner.commands import add_parser as add_inner_parser

    add_inner_parser(sub)

    from octavia.routines.commands import add_parser as add_routines_parser

    add_routines_parser(sub)

    from octavia.evolve.suite import add_parser as add_test_parser

    add_test_parser(sub)
    return p


def _load(args: argparse.Namespace, overrides: dict[str, Any] | None = None) -> AppConfig:
    overrides = dict(overrides or {})
    if args.verbose:
        overrides.setdefault("logging", {})["console"] = "verbose"
    try:
        return load_config(args.config, overrides=overrides)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _init_logging(config: AppConfig, console: bool = True, command: str = "run") -> None:
    level = "DEBUG" if config.logging.console == "verbose" else config.logging.level
    log_file = config.resolve_path(config.logging.file)
    if command != "run":
        # Only the long-running assistant owns the rotating main log; a second process
        # writing it would break rotation on Windows (the file can't be renamed while open).
        log_file = log_file.with_name(f"{log_file.stem}-{command}{log_file.suffix}")
    setup_logging(log_file, level=level, console=console)


def main(argv: list[str] | None = None) -> int:
    _capture_headless_stdio()
    from octavia import edition

    if edition.PUBLIC:  # the user's things live outside the program folder (core/userdir.py)
        from octavia.config import find_project_root
        from octavia.core.userdir import attach

        try:
            attach(find_project_root())
        except OSError as exc:
            print(f"Couldn't reach your OCTAVIA folder: {exc}", file=sys.stderr)
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    command = args.command or "run"
    if command == "run":
        return _cmd_run(args, argv)
    if command == "chat":
        config = _load(args)
        _init_logging(config, command=command)
        from octavia.app import StartupError, run_text

        try:
            asyncio.run(run_text(config, speak=args.speak))
        except KeyboardInterrupt:
            pass
        except StartupError as exc:
            print(f"Startup failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if command == "forget":
        return _cmd_forget(_load(args), 1440 if args.last == "day" else int(args.last), args.dry_run, args.yes)
    if command == "welcome":
        from octavia.config import find_project_root
        from octavia.welcome import run as welcome

        try:
            if args.window:
                from octavia.platform.welcome_window import run as window

                electron = ""
                try:
                    electron = _load(args).avatar.electron
                except SystemExit:
                    pass
                done = window(find_project_root(), electron)
                if done is not None:
                    return done
            return welcome(find_project_root())
        except KeyboardInterrupt:
            print("\nStopped: nothing was saved.")
            return 1
    if command == "release":
        return _cmd_release(args)
    if command in ("ether", "hive"):
        from octavia.hive.cli import run as hive

        return hive(_load(args), args)
    if command == "login":
        from octavia.brain.claude_auth import login

        return login(_load(args))
    if command == "devices":
        return _cmd_devices()
    if command == "setup":
        config = _load(args)
        _init_logging(config, command=command)
        from octavia.setup_models import download_all

        return 0 if asyncio.run(download_all(config, force=args.force)) else 1
    if command == "memory":
        config = _load(args)
        return asyncio.run(_memory_check(config, fix=args.fix))

    if command == "routing":
        config = _load(args)
        import time as _time

        from octavia.core.routing_log import read, report

        since = _time.time() - args.days * 86400 if args.days > 0 else 0.0
        print(report(read(config.resolve_path(config.routing.log_file), since)))
        return 0
    if command == "shadow":
        config = _load(args)
        import time as _time

        from octavia.localbrain.shadow import read as read_shadow
        from octavia.localbrain.shadow import report as shadow_report

        since = _time.time() - args.days * 86400 if args.days > 0 else 0.0
        print(shadow_report(read_shadow(config.resolve_path(config.local_brain.log_file), since)))
        return 0
    if command == "addressee":
        return _cmd_addressee(_load(args), args)
    if command == "doctor":
        config = _load(args)
        _init_logging(config, console=False, command=command)
        from octavia.doctor import run_doctor

        return _reported(lambda: asyncio.run(run_doctor(config, live=args.live)), "doctor")
    if command == "say":
        config = _load(args)
        _init_logging(config, command=command)
        return asyncio.run(_cmd_say(config, " ".join(args.text)))
    if command == "listen-test":
        config = _load(args)
        _init_logging(config, command=command)
        from octavia.doctor import listen_test

        try:
            return _reported(lambda: asyncio.run(listen_test(config, args.seconds)), "listen-test")
        except KeyboardInterrupt:
            return 0
    if command == "wake-check":
        config = _load(args, {"assistant": {"wake_phrase": args.phrase}} if args.phrase else None)
        _init_logging(config, console=False, command=command)
        from octavia.wake_check import run_wake_check

        return _reported(lambda: run_wake_check(config), "wake-check")
    if command == "env":
        return _cmd_env(_load(args), args.action, args.dry_run)
    if command == "autostart":
        return _cmd_autostart(args.action, args.method)
    if command == "revive":
        from octavia.evolve.revive import run as revive

        return revive(Path(__file__).resolve().parents[2])  # no config: it runs every two minutes, and must stay cheap
    if command == "explorer-menu":
        return _cmd_explorer_menu(_load(args), args.action)
    if command == "settings":
        config = _load(args)
        from octavia.platform.settings_window import main as settings_window

        local = config.sources[1] if len(config.sources) > 1 else ""
        return settings_window([str(config.root), local])
    if command == "usage":
        from octavia.core import usage as claude_usage

        print(claude_usage.report(claude_usage.configure(_load(args)), args.days))
        return 0
    if command == "evolve":
        config = _load(args)
        _init_logging(config, command="evolve")
        from octavia.core import usage as claude_usage

        claude_usage.configure(config)  # builds she'd start by herself (`evolve night --run`) keep to her budget
        from octavia.evolve.commands import run_command

        try:
            return _reported(lambda: asyncio.run(run_command(config, args)), "evolve")
        except KeyboardInterrupt:
            return 130
    if command == "futures":
        config = _load(args)
        _init_logging(config, command="futures")
        from octavia.futures.commands import run_command as futures_command

        return _reported(lambda: asyncio.run(futures_command(config, args)), "futures")
    if command == "inner":
        config = _load(args)
        _init_logging(config, command="inner")
        from octavia.inner.commands import run_command as inner_command

        return _reported(lambda: asyncio.run(inner_command(config, args)), "inner")
    if command == "test":
        from octavia.evolve.suite import run_command as test_command

        return test_command(args)
    if command == "routines":
        config = _load(args)
        _init_logging(config, command="routines")
        from octavia.routines.commands import run_command as routines_command

        return _reported(lambda: routines_command(config, args), "routines")
    if command == "avatar":
        from octavia.avatar import commands as avatar_commands

        return _reported(
            lambda: avatar_commands.run_command(_load(args), args.avatar_command, prop=getattr(args, "prop", None)),
            "avatar",
        )
    return 2


def _cmd_env(config: AppConfig, action: str, dry_run: bool) -> int:
    """``octavia env``: every key Octavia reads, whether it's set and what's wrong (``octavia.keys``); ``tidy`` sorts the
    .env file into commented sections, keeping every value; ``example`` writes .env.example from the list."""
    from octavia import keys

    if action == "example":
        path = config.root / ".env.example"
        path.write_text(keys.example(), encoding="utf-8", newline="\n")
        print(f"Wrote {path} from the list of keys.")
        return 0
    if action == "tidy":
        from octavia.core.userdir import state_root

        path = keys.env_file() or state_root(config.root) / ".env"
        if dry_run:
            if not path.is_file():
                print(f"There's no {path} yet.")
                return 1
            try:
                print(keys.masked(keys.tidy(path.read_text(encoding="utf-8-sig", errors="replace"))), end="")
            except ValueError as exc:
                print(f"It can't be tidied as it is: {exc}.")
                return 1
            return 0
        changed, said = keys.tidy_file(path)
        print(said)
        return 0
    lines = keys.report(config)
    print("\n".join(lines))
    problems = sum(line.lstrip().startswith("[FIX ]") for line in lines)
    return 1 if problems else 0


def _reported(run: Any, what: str) -> int:
    """Run a diagnostic command and show its failure on the console, not only in the log."""
    try:
        return int(run())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log.debug("%s failed", what, exc_info=True)
        print(f"{what} failed: {exc}", file=sys.stderr)
        return 1


def main_background() -> int:
    """Entry point for the windowless octaviaw.exe used by autostart (honours its arguments)."""
    return main(sys.argv[1:] or ["run", "--background"])


def _cmd_release(args: argparse.Namespace) -> int:
    from pathlib import Path

    from octavia.config import find_project_root
    from octavia.release import build, scan

    root = find_project_root()
    if args.release_command == "scan":
        findings = scan.scan_folder(Path(args.folder), root, old_name=True)
        print(scan.report(findings))
        return 1 if findings else 0
    if args.release_command == "github":
        from octavia.release import github

        try:
            notes = Path(args.notes_file).read_text(encoding="utf-8")
            print(github.publish(root, args.title, notes, args.passed, args.total))
        except (github.PublishError, build.BuildError, OSError) as exc:
            print(f"Not published: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        result = build.build(root, root / args.out)
    except build.BuildError as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    if not result.ok:
        print("Build stopped: the scan found personal or secret things (nothing was kept).")
        print(scan.report(result.findings))
        return 1
    print(f"Built {result.files} files: {result.folder}")
    print(f"Zipped: {result.archive}")
    print("Scan: clean. Nothing was published.")
    return 0


def _cmd_run(args: argparse.Namespace, argv: list[str] | None = None) -> int:
    from octavia.evolve.supervisor import claim_supervision, is_supervised

    # Before anything can start a process: apps the assistant opens must not inherit the supervision variables.
    claim_supervision()
    from octavia import edition

    if edition.PUBLIC and not is_supervised() and sys.stdin is not None and sys.stdin.isatty():
        from octavia.config import find_project_root
        from octavia.welcome import needed
        from octavia.welcome import run as welcome

        if needed(find_project_root()):
            print("Nothing is set up yet, so the setup wizard comes first.")
            if welcome(find_project_root()) != 0:
                return 1
    overrides: dict[str, Any] = {}
    if getattr(args, "wake", None):
        overrides["wake"] = {"engine": args.wake}
    config = _load(args, overrides)
    background = getattr(args, "background", False) or _STARTED_HEADLESS or headless()
    child = is_supervised()  # started by the supervisor, which already holds the single-instance lock
    supervise = config.self_improvement.enabled and not child and not getattr(args, "no_supervisor", False)

    # Single-instance check first, so a second copy never touches the running one's log.
    guard = None
    if config.system.single_instance and sys.platform == "win32" and not child:
        from octavia.platform.windows import SingleInstance

        guard = SingleInstance(f"Local\\OctaviaAssistant-{_path_hash(config.root)}")
        if guard.already_running:
            print(f"{config.assistant.name} is already running.", file=sys.stderr)
            return 0
    try:
        if supervise:
            code = _run_supervisor(config, argv if argv is not None else sys.argv[1:], background)
        else:
            code = _run_assistant(config, background)
            if child:
                # Everything of hers has shut down: end without the native libraries' exit hooks, which crashed on the
                # way out, so the supervisor gets the exit code it was told (a restart, a quit), not a crash.
                exit_now(code)
    except Exception:
        if guard is not None:
            guard.close()
        if background and not child:
            _stopped_notice(config, "She stopped because of an error.")
        raise
    if guard is not None:
        guard.close()  # first: starting her again while the notice shows must work
    # Without a window, nothing else would say she's gone for good (under the supervisor, the child's failures are
    # the supervisor's to report, once it gives up).
    if code != 0 and background and not child:
        why = (
            "She crashed several times in a row right after starting."
            if supervise
            else f"She couldn't start: {_startup_error}"
            if _startup_error
            else "She couldn't start."
        )
        _stopped_notice(config, why)
    return code


def _run_supervisor(config: AppConfig, argv: list[str], background: bool) -> int:
    """Run the assistant as a child process that can restart into updates (and be rolled back)."""
    _init_logging(config, console=not background, command="supervisor")
    from octavia.evolve.models import self_improvement_author
    from octavia.evolve.supervisor import rollback_update, supervise

    log.info("Supervising the assistant (self-improvement is on)")
    # A crash rollback is committed under the same name as the installs it undoes.
    rollback = functools.partial(rollback_update, author=self_improvement_author(config.assistant.name))
    return supervise([sys.executable, "-m", "octavia", *argv], config.root, background=background, rollback=rollback)


def _run_assistant(config: AppConfig, background: bool) -> int:
    # A forget she was asked for, first: before the log or any of her stores is opened (``core/forget.py``).
    forgot, forget_error = None, None
    try:
        from octavia.core import forget

        forgot = forget.run_pending(config.root)
    except Exception as exc:  # she starts either way; what went wrong is logged once the log is open
        forget_error = exc
    _init_logging(config, console=not background)
    if forget_error is not None:
        log.error("Forgetting what I was asked to forget failed", exc_info=forget_error)
    elif forgot is not None:
        log.info("Forget ran: ok=%s, %d records, backup %s", forgot.ok, forgot.total, forgot.backup or "none")
    if config.system.disable_power_throttling and sys.platform == "win32":
        from octavia.platform.windows import disable_power_throttling

        disable_power_throttling()

    from octavia.app import StartupError, run_voice

    try:
        return asyncio.run(run_voice(config))
    except KeyboardInterrupt:
        return 0
    except StartupError as exc:
        # The console already showed it as an [ERROR] line; this is for the log file.
        log.error("Startup failed: %s", exc, extra={"console": False})
        global _startup_error
        _startup_error = str(exc)[:120]  # for the notice that she stopped
        return 1


_startup_error = ""


def _stopped_notice(config: AppConfig, why: str) -> None:
    """A Windows notification that she's stopped for good, naming the log: running without a window, she'd
    otherwise just go quiet."""
    if sys.platform != "win32":
        return
    log_file = config.resolve_path(config.logging.file)
    try:
        from octavia.tools.extensions._scheduler import show_notification

        show_notification(f"{config.assistant.name} stopped", f"{why} What went wrong is in {log_file}")
    except Exception:
        log.warning("Couldn't show the notification that she stopped", exc_info=True)


def _cmd_forget(config: AppConfig, minutes: int, dry_run: bool, yes: bool) -> int:
    """`octavia forget`: as the settings window's "Forget the last...", from a terminal."""
    import time as _time

    from octavia.core import forget
    from octavia.core.live_settings import read_status

    what = forget.label(minutes)
    if dry_run:
        print(forget.describe(forget.plan(config.root, _time.time() - minutes * 60)))
        return 0
    if not yes:
        try:
            answer = input(f"Forget {what}? Everything she kept from then is deleted (a backup is made first). [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing was forgotten.")
            return 1
    from octavia.platform.settings_window import ask_her_to_forget, forget_while_off

    status = read_status(config.root)
    print(
        ask_her_to_forget(config, int(status["pid"]), minutes)
        if status is not None
        else forget_while_off(config, minutes)
    )
    return 0


def _path_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha1(str(path).lower().encode()).hexdigest()[:10]


def _cmd_devices() -> int:
    import sounddevice as sd

    from octavia.audio.devices import DEFAULT_HOST_API, list_devices

    apis = sd.query_hostapis()
    defaults = {(a["name"], "in"): a["default_input_device"] for a in apis} | {
        (a["name"], "out"): a["default_output_device"] for a in apis
    }
    devices = list_devices()
    for api in [a["name"] for a in apis]:
        rows = [d for d in devices if d["host_api"] == api]
        if not rows:
            continue
        marker = "  <- used by default" if api == DEFAULT_HOST_API else ""
        print(f"\n{api}{marker}")
        for d in rows:
            kinds = []
            if d["inputs"]:
                kinds.append("in" + ("*" if defaults.get((api, "in")) == d["index"] else ""))
            if d["outputs"]:
                kinds.append("out" + ("*" if defaults.get((api, "out")) == d["index"] else ""))
            print(f"  [{d['index']:>3}] {d['name']}  ({'/'.join(kinds)}, {int(d['default_rate'])} Hz)")
    print("\n* = default for that host API. Copy a distinctive part of a name above into config/local.yaml:")
    print('  audio:\n    input_device: "<part of the mic\'s name>"\n    output_device: "<part of the speaker\'s name>"')
    print("If several devices match, the first is used and a warning names them all.")
    return 0


def _cmd_addressee(config: Any, args: argparse.Namespace) -> int:
    import time as _time

    from octavia.addressee import shadow as addressee

    path = config.resolve_path(config.addressee.log_file)
    if args.action == "forget":
        print("Deleted everything it kept." if addressee.forget(path) else "There was nothing kept.")
        print("If she's running, lines from the last minute are still written once their wait is over.")
        return 0
    if args.action == "mark":
        if len(args.rest) != 2 or args.rest[1].lower() not in ("yes", "no"):
            print("Usage: octavia addressee mark ID yes|no", file=sys.stderr)
            return 2
        line_id, answer = args.rest[0], args.rest[1].lower() == "yes"
        if not addressee.mark(path, line_id, answer):
            print(f"No line {line_id!r} (see `octavia addressee show`).", file=sys.stderr)
            return 1
        print(f"Marked {line_id}: {'for her' if answer else 'not for her'}.")
        return 0
    since = _time.time() - args.days * 86400 if args.days > 0 else 0.0
    rows = addressee.read(path, since)
    print(addressee.report(rows, config.addressee.threshold, show=args.action == "show"))
    return 0


async def _cmd_say(config: AppConfig, text: str) -> int:
    from octavia.app import create_speaking_tts
    from octavia.audio.player import SoundDevicePlayer
    from octavia.audio.voice_volume import SETTINGS_FILE as VOICE_VOLUME_FILE
    from octavia.audio.voice_volume import VoiceVolume
    from octavia.core.events import EventBus
    from octavia.core.speech import SpeechOutput

    tts = create_speaking_tts(config)  # with the backup voice, as replies are spoken
    try:
        await tts.start()
    except Exception as exc:
        print(f"TTS unavailable: {exc}", file=sys.stderr)
        return 1
    main = getattr(tts, "primary", tts)
    if (wait_ready := getattr(main, "wait_ready", None)) is not None:
        await wait_ready()  # her local voice loading; if it can't, Fish says it
    player = SoundDevicePlayer(tts.format.sample_rate, config.audio.output_device, config.audio.host_api)
    await player.start()
    try:
        volume = VoiceVolume.load(config.resolve_path(VOICE_VOLUME_FILE), config.audio.output_volume).gain
        speech = SpeechOutput(tts, player, EventBus(), volume=volume)
        speech.effect = config.tts.voice_effect  # as she'd say it, effect and all
        outcome = await speech.say(text)
        if outcome.error:
            print(f"TTS error: {outcome.error}", file=sys.stderr)
            return 1
        return 0
    finally:
        await tts.close()
        await player.close()


def _cmd_autostart(action: str, method: str) -> int:
    if sys.platform != "win32":
        print("Autostart is only supported on Windows.", file=sys.stderr)
        return 1
    from octavia.platform.autostart import command

    return subprocess.call(command(action, method, Path(__file__).resolve().parents[2]))


def _cmd_explorer_menu(config: AppConfig, action: str) -> int:
    """Put her right-click entries in, take them out, or say whether they're there. The setting is kept to match,
    so she doesn't undo it at her next start (``explorer_menu.sync``)."""
    from octavia import user_settings
    from octavia.platform import explorer_menu

    name = config.assistant.name
    if sys.platform != "win32":
        print("Explorer's menu is only on Windows.", file=sys.stderr)
        return 1
    if action == "status":
        state = explorer_menu.status(config.root)
        print({"installed": "In the menu.", "other": "In the menu, but another copy's or out of date.",
               "absent": "Not in the menu."}[state])  # fmt: skip
        if state != "absent":
            print(explorer_menu.where_to_find(name))
        return 0
    on = action == "install"
    try:
        backups = explorer_menu.install(config.root, name) if on else explorer_menu.uninstall(config.root)
    except explorer_menu.MenuError as exc:
        print(f"{exc}\nIf your antivirus blocked it, allow it there and try again.", file=sys.stderr)
        return 1
    user_settings.save(config.root, {"system.explorer_menu": on})
    print(f"{name} is in Explorer's right-click menu. {explorer_menu.where_to_find(name)}" if on
          else f"{name} is out of Explorer's right-click menu.")  # fmt: skip
    for path in backups:
        print(f"What was there before is saved in {path}")
    return 0


async def _memory_check(config: Any, fix: bool) -> int:
    """``octavia memory check``: her live notes that say the same thing two ways (``memory/consistency.py``)."""
    from octavia.memory.consistency import conflicts, settle
    from octavia.memory.sqlite_store import Database, SqliteLongTermMemory

    db = Database(config.resolve_path(config.memory.database))
    try:
        store = SqliteLongTermMemory(db)
        items = await store.list(limit=10_000)
        found = conflicts(items)
        print(f"{len(items)} live notes; {len(found)} pair(s) about the same thing told two ways.")
        for c in found:
            how = "the newer one corrects it" if c.correction else "ask the user"
            print(f"  {c.line()}  ({how}, {c.overlap:.0%} the same words)")
        if fix and found:
            settled, left = await settle(store, items)
            print(f"Folded {len(settled)} into the note that corrects it; {len(left)} left for her to ask about.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
