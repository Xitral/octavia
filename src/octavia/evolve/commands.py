"""``octavia evolve ...``: self-improvement from the terminal (setup, list, show, build, install, reject, undo,
night, progress, mistakes)."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import sys
import textwrap
import time
from typing import Any

from octavia.config import AppConfig
from octavia.core.console import StatusConsole
from octavia.core.events import EventBus
from octavia.evolve.git import Git, GitError
from octavia.evolve.models import OPEN_STATUSES, EvolveError, GateReport, Improvement, Status
from octavia.evolve.wiring import EvolveComponents, create_evolve

_DIFF_CHARS = 60_000


async def run_command(config: AppConfig, args: argparse.Namespace) -> int:
    command = args.evolve_command
    if command == "setup":
        return setup(config)
    if command == "mistakes":
        return caught_mistakes(config, args)
    events = EventBus()
    StatusConsole(config.assistant.name, "verbose" if command == "build" else "normal").attach(events)
    evolve = create_evolve(config, events, supervised=False)
    manager = evolve.manager
    try:
        if command in ("list", "show"):
            # An update the supervisor undid (it crashed on startup) is still "installed" in the journal until a
            # start reads its marker: show the truth. (``undo`` does this itself, and says so rather than going
            # on to undo the update before it.)
            await manager.apply_pending_rollback()
        if command == "list":
            statuses = None if args.all else OPEN_STATUSES
            items = await evolve.journal.list(statuses, limit=200)
            print(
                _table(items) if items else "Nothing on the list." + ("" if args.all else " (--all shows closed ones)")
            )
        elif command == "show":
            item = await manager.get(args.id)
            if item is None:
                raise EvolveError(f"There's no improvement #{args.id}.")
            print(_details(item, evolve.git))
        elif command == "build":
            if args.id is None and not args.request:
                raise EvolveError('Give an improvement number, or --request "what to build".')
            item = await manager.request_build(args.id, args.request or "")
            print(f"Building {item.label()}. This takes a few minutes; progress follows. Ctrl+C stops it.")
            await manager.wait_idle()
            item = await manager.get(item.id) or item
            print(
                f"\n{item.label()}: {item.status.value.replace('_', ' ')}" + (f" ({item.error})" if item.error else "")
            )
            if item.status is Status.READY:
                print(f"Review it with `octavia evolve show {item.id}`, then `octavia evolve install {item.id}`.")
            return 0 if item.status is Status.READY else 1
        elif command == "install":
            if not args.ids and not args.all:
                raise EvolveError("Give the numbers of the improvements to install, or --all.")
            if await manager.needs_testing(args.ids, all_ready=args.all):
                print("Testing with the newest code first; this takes a few minutes...")
            print(await manager.install(args.ids, all_ready=args.all))
            print("If the assistant is running, restart it to use the update.")
        elif command == "reject":
            print(await manager.reject(args.id))
        elif command == "undo":
            print(await manager.undo_last())
            print("If the assistant is running, restart it.")
        elif command in ("night", "progress"):
            return await _night(config, evolve, args)
        return 0
    except EvolveError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        for close in evolve.closers:
            result = close()
            if hasattr(result, "__await__"):
                await result


async def _night(config: AppConfig, evolve: EvolveComponents, args: argparse.Namespace) -> int:
    """``night``: what tonight's shift would find (``--run``: do it now, fixes and all); ``progress``: the record."""
    from octavia.evolve import nightly

    night = evolve.night
    if night is None:
        raise EvolveError("The night shift is off (self_improvement.nightly_at is empty).")
    if args.evolve_command == "night" and not args.run:
        since = night.state.last_ts or (time.time() - nightly.FIRST_NIGHT_S)
        turns = nightly.read_turns(night.settings.routing_log, since)
        print(f"{len(turns)} turns since {_when(since)}: {nightly.day_numbers(turns)}")
        findings = nightly.scan(turns)
        for finding in findings:
            print(f"\n[{finding.kind}, weight {finding.weight:.1f}] {finding.title}\n{finding.details}")
        if not findings:
            print("Nothing stands out.")
        print("\n(--run starts the worst of these as builds now, and runs the real-request check.)")
        return 0
    if args.evolve_command == "night":
        record = await night.run()
        print(json.dumps(record, indent=1))
        await evolve.manager.wait_idle()
    nights = nightly.read_nights(night.nights_file)
    nightly.write_page(night.page, nights, nightly._eval_history(night.settings))
    for row in nights[-14:]:
        day, built = row["day"], [b for b in row.get("builds", []) if b.get("building")]
        print(
            f"{row['date']}  turns {day.get('turns'):>4}  errors {day.get('errors'):>3}  slow {day.get('slow'):>3}  "
            f"first word p50 {day.get('first_word_p50')}  fixes {', '.join('#' + str(b['id']) for b in built) or '-'}"
        )
    print(f"Chart: {night.page}")
    return 0


def rubric_asker(config: AppConfig) -> Any:
    """What drafts a rule of what a good reply does (``evolve/rubrics.py``): one short Haiku reply, at most a minute."""
    from octavia.evolve import evals

    async def ask(prompt: str) -> str:
        return await asyncio.wait_for(evals.ask_small(prompt, config, "You write short, checkable rules."), 60)

    return ask


def rubric_replies(config: AppConfig) -> Any:
    """Her reply to a request, from her transcript (``rubrics.her_reply``)."""
    from octavia.evolve import rubrics

    db = config.resolve_path(config.memory.database)
    return lambda said: rubrics.her_reply(db, said)


def caught_mistakes(config: AppConfig, args: argparse.Namespace) -> int:
    """``mistakes``: list, show, keep or drop the mistakes she caught; ``scan`` looks through the routing log now."""
    from octavia.evolve import evals, mistakes
    from octavia.evolve.nightly import read_turns
    from octavia.evolve.wiring import _app_dbs

    pool = mistakes.Pool(mistakes.pool_path(config.root))
    try:
        if args.action in ("show", "keep", "drop") and args.n is None:
            raise mistakes.MistakeError(f"Give the mistake's number: octavia evolve mistakes {args.action} N")
        if args.action == "list":
            cases = [c for c in pool.all() if args.all or c.status != "dropped"]
            if not cases:
                print("No mistakes caught yet. (`octavia evolve mistakes scan` looks through the routing log now.)")
            for case in sorted(cases, key=lambda c: (c.status != "gating", -c.last_seen)):
                print(f"{mistakes.describe(case)} [confidence {case.confidence:.1f}]")
            graded = sum(c.status == "gating" for c in pool.all())
            room = f"they can fill {evals.MAX_CAUGHT} turns of the check"
            print(f"\n{graded} graded ({room}). `keep N` grades one, `drop N` drops one.")
        elif args.action == "show":
            case = pool.get(args.n)
            print(mistakes.describe(case))
            print(json.dumps(dataclasses.asdict(case), indent=1, ensure_ascii=False))
        elif args.action == "keep":
            case = pool.keep(args.n, args.expect, args.forbid, True if args.no_tools else None, args.judge)
            print(f"Graded for good: {mistakes.describe(case)}")
        elif args.action == "drop":
            case = pool.drop(args.n)
            print(f"Dropped #{case.n}: not graded, and not caught again.")
        elif args.action == "suggest":
            from octavia.evolve import rubrics

            made = asyncio.run(rubrics.draft(pool, rubric_asker(config), rubric_replies(config), limit=args.limit))
            for case in made:
                print(mistakes.describe(case))
            left = len(rubrics.wanting(pool))
            print(f"{len(made)} drafted ({left} still without one). `keep N` grades a case by its suggested rule.")
        else:
            since = time.time() - args.days * 86400
            turns = read_turns(config.resolve_path(config.routing.log_file), since)
            apps = mistakes.fronted_apps(_app_dbs(config), since)
            found = mistakes.find(turns, apps, config.screen_history.exclude_apps)
            if args.dry_run:
                for case in found:
                    print(mistakes.describe(case))
                print(f"{len(found)} found in {len(turns)} turns (nothing kept: --dry-run).")
                return 0
            added = pool.add(found)
            for case in added.new:
                print(f"new: {mistakes.describe(case)}")
            for case in added.graded:
                print(f"graded (it came back): #{case.n}")
            print(f"{len(added.new)} new, {len(added.again)} seen again, in {len(turns)} turns.")
        return 0
    except mistakes.MistakeError as exc:
        print(exc, file=sys.stderr)
        return 1


def setup(config: AppConfig) -> int:
    """Check git and make the first commit (builds branch from a commit)."""
    git = Git(config.root)
    if not git.available():
        print("Git isn't installed. Install Git for Windows (https://git-scm.com), then run this again.")
        return 1
    if not git.is_repo():
        git.run("init", "-b", "main")
        print(f"Created a git repository in {config.root}.")
    if git.has_commits():
        dirty = git.dirty_files()
        print("Git is set up: builds start from your latest commit.")
        if dirty:
            print(f"Note: {len(dirty)} changed file(s) aren't committed yet, so builds won't include them and")
            print("installing will wait until they are. Commit them when you're happy with them.")
        _enable_hint(config)
        return 0
    files = git.dirty_files()
    print(f"This makes the first commit of your project: {len(files)} files.")
    print("Not included (git-ignored): .env, config/local.yaml, data/, models/, .venv/.")
    if sys.stdin is not None and sys.stdin.isatty():
        answer = input("Commit them now? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("Nothing committed.")
            return 1
    try:
        sha = git.initial_commit("Initial commit")
    except GitError as exc:
        print(f"Couldn't commit: {exc}")
        return 1
    print(f"Committed ({sha[:12]}).")
    _enable_hint(config)
    return 0


def _enable_hint(config: AppConfig) -> None:
    if not config.self_improvement.enabled:
        print("\nTo turn self-improvement on, add this to config/local.yaml and restart the assistant:")
        print("  self_improvement:\n    enabled: true")


# ------------------------------------------------------------------ formatting


def _when(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def _table(items: list[Improvement]) -> str:
    rows = [f"{'#':>4}  {'status':<11} {'kind':<9} {'seen':>4}  {'updated':<16}  title"]
    for i in items:
        rows.append(
            f"{i.id:>4}  {i.status.value:<11} {i.kind.value:<9} {i.occurrences:>4}  {_when(i.updated_at):<16}  {i.title}"
        )
    return "\n".join(rows)


def _block(title: str, text: str, width: int = 100) -> str:
    body = "\n".join(textwrap.fill(p, width, initial_indent="  ", subsequent_indent="  ") for p in text.splitlines())
    return f"{title}:\n{body}" if text.strip() else ""


def _details(item: Improvement, git: Git) -> str:
    report: dict[str, Any] = item.report or {}
    build = report.get("build") or {}
    parts = [
        f"{item.label()}  [{item.status.value}]  {item.kind.value}, from {item.source}, seen {item.occurrences}x",
        f"created {_when(item.created_at)}, updated {_when(item.updated_at)}",
        _block("Details", item.details),
        _block("Request", str(report.get("request") or "")),
        _block("Error", item.error or ""),
        _block("Evidence (latest)", item.evidence[-1] if item.evidence else ""),
        _block("What the builder says it did", str(build.get("summary") or "")),
        _block("Builder's notes", str(build.get("details") or "")),
    ]
    if isinstance(report.get("gate"), dict):
        parts.append(_gate(GateReport.from_dict(report["gate"])))
    parts.append(_diff(item, git))
    return "\n\n".join(p for p in parts if p)


def _gate(report: GateReport) -> str:
    lines = [f"Checks: {'passed' if report.passed else 'NOT passed'}  (+{report.lines_added}/-{report.lines_removed})"]
    lines += [f"  refused/failed: {r}" for r in report.reasons]
    lines += [f"  look at this:   {f}" for f in report.flags]
    for change in report.changes:
        mark = {"forbidden": "  PROTECTED", "sensitive": "  (core file)"}.get(change.klass, "")
        lines.append(f"  {change.status} {change.path}{mark}")
    for tool in report.new_tools:
        lines.append(f"  new/changed tool: {tool.name} (risk: {tool.risk}) in {tool.path}")
    for check in report.checks:
        lines.append(f"  {check.name}: {'ok' if check.ok else 'FAILED'} - {check.summary}")
        if not check.ok and check.output_tail:
            lines.append(textwrap.indent(check.output_tail[-2000:], "    | "))
    return "\n".join(lines)


def _diff(item: Improvement, git: Git) -> str:
    """The code change: the branch of a ready build, or the merge of an installed one."""
    try:
        if item.status is Status.READY and item.branch and item.base_commit:
            text = git.run("diff", "--no-ext-diff", "--no-textconv", "--stat", "-p", item.base_commit, item.branch)
        elif item.merge_commit:
            merge = item.merge_commit
            text = git.run("diff", "--no-ext-diff", "--no-textconv", "--stat", "-p", f"{merge}^1", merge)
        else:
            return ""
    except GitError as exc:
        return f"(Couldn't show the change: {exc})"
    if len(text) > _DIFF_CHARS:
        text = text[:_DIFF_CHARS] + f"\n... (cut; the full change is {len(text)} characters)"
    return f"The change:\n{text}"
