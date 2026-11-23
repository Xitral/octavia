"""``octavia hive ...``: the hivemind from the command line (see ``octavia/hive/__init__.py``)."""

from __future__ import annotations

import argparse
from pathlib import Path

from octavia.config import AppConfig


def add_parser(sub: argparse._SubParsersAction) -> None:
    hive = sub.add_parser("hive", help="The hivemind: signed updates, and what she learned proposed upstream")
    hv = hive.add_subparsers(dest="hive_command", required=True)
    hv.add_parser("status", help="The feed's releases, their shared-eval pass rates, and this install's")
    up = hv.add_parser("update", help="Apply the newest signed release (public installs)")
    up.add_argument("--yes", action="store_true", help="Don't ask first")
    hv.add_parser("collect", help="Turn installed fixes and kept caught cases into proposals (scrubbed)")
    lesson = hv.add_parser("lesson", help="Propose a lesson: a short general rule worth every Octavia knowing")
    lesson.add_argument("text")
    lesson.add_argument("--about", default="", help="What it's about, e.g. an app's name")
    hv.add_parser("outbox", help="The proposals waiting, held (with why) or sent")
    hv.add_parser("send", help="Send the ready proposals to hive.inbox (only with hive.share: on)")
    hv.add_parser("drop-held", help="Delete the held proposals")
    # The maintainer's side
    hv.add_parser("keygen", help="Maintainer: make the signing key (data/hive/maintainer_key.pem)")
    pub = hv.add_parser("publish", help="Maintainer: add a release zip to a local feed folder and sign it")
    pub.add_argument("archive", help="The zip `octavia release build` made")
    pub.add_argument("--feed", required=True, help="The feed folder (local: putting it online is your step)")
    pub.add_argument("--notes", default="")
    pub.add_argument("--passed", type=int, help="Shared eval cases the release passed...")
    pub.add_argument("--total", type=int, help="...of this many")
    rev = hv.add_parser("review", help="Maintainer: check proposal files (or a folder of them)")
    rev.add_argument("paths", nargs="+")
    acc = hv.add_parser("accept", help="Maintainer: take one proposal (case, lesson, or fix as a patch)")
    acc.add_argument("path")


def run(config: AppConfig, args: argparse.Namespace) -> int:
    from octavia import edition
    from octavia.hive import channel, proposals, review, signing

    root = config.root
    h = config.hive
    command = args.hive_command
    version = channel.current_version(root)
    try:
        if command == "status":
            releases = channel.read(h.feed, h.public_key)
            print(channel.status(releases, version))
        elif command == "update":
            release = channel.newer(channel.read(h.feed, h.public_key), version)
            if release is None:
                print("Up to date.")
                return 0
            print(f"{release.version} is out{': ' + release.notes if release.notes else ''}")
            if not args.yes and input("Apply it now? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
            changed = channel.apply(root, h.feed, h.public_key, release, public=edition.PUBLIC)
            print(f"Updated {len(changed)} file(s) to {release.version}. Run `uv sync`, then restart her.")
        elif command == "collect":
            made = proposals.collect_fixes(root, version) + proposals.collect_cases(root, version)
            held = [p for p in made if p.status == "held"]
            print(f"{len(made)} new proposal(s), {len(held)} held back (`octavia hive outbox` says why).")
        elif command == "lesson":
            p = proposals.propose_lesson(root, args.text, args.about, version)
            print("Ready to send." if p.status == "ready" else f"Held back: {', '.join(p.held_because)}")
        elif command == "outbox":
            waiting = proposals.outbox(root)
            for p in waiting:
                why = f" ({'; '.join(p.held_because)})" if p.held_because else ""
                print(f"[{p.status}] {p.kind}: {p.title}{why}")
            print(f"{len(waiting)} in the outbox." if waiting else "The outbox is empty.")
        elif command == "send":
            _, said = proposals.send(root, h.inbox, h.share)
            print(said)
        elif command == "drop-held":
            print(f"Deleted {proposals.clear_held(root)} held proposal(s).")
        elif command == "keygen":
            public = signing.generate(root / signing.KEY_FILE)
            print(f"Key made: {signing.KEY_FILE.as_posix()} (keep it safe, never share it).")
            print(f"Public key (goes in config/public.yaml as hive.public_key):\n{public}")
        elif command == "publish":
            archive = Path(args.archive)
            name = channel.zip_version(archive)
            r = channel.publish(
                Path(args.feed), archive, root / signing.KEY_FILE, name, args.notes, args.passed, args.total
            )
            print(
                f"Added {r.version} to {args.feed} and signed it. Nothing was uploaded: putting the folder online is yours."
            )
        elif command == "review":
            for item in review.review(root, [Path(p) for p in args.paths]):
                print(item.summary())
        elif command == "accept":
            print(review.accept(root, Path(args.path)))
    except (channel.UpdateError, signing.SignatureError, ValueError, FileExistsError, OSError) as exc:
        print(f"Couldn't: {exc}")
        return 1
    return 0
