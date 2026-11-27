"""``octavia hive ...``: the ETHER from the command line (see ``octavia/hive/__init__.py``)."""

from __future__ import annotations

import argparse
from pathlib import Path

from octavia.config import AppConfig


def add_parser(sub: argparse._SubParsersAction) -> None:
    hive = sub.add_parser(
        "ether", aliases=["hive"], help="The ETHER: signed updates, and what it learned proposed upstream"
    )
    hv = hive.add_subparsers(dest="hive_command", required=True)
    hv.add_parser("status", help="The feed's releases, their shared-eval pass rates, and this install's")
    hv.add_parser(
        "items", help="What was shared on its own (lessons): each one's measured effect, and if it's in force"
    )
    drop = hv.add_parser("drop", help="Leave a shared item out on this install (take it back with --undo)")
    drop.add_argument("id")
    drop.add_argument("--undo", action="store_true")
    hv.add_parser("rollback", help="Undo the last update (public installs)")
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
    pub.add_argument("--passed", type=int, help=argparse.SUPPRESS)  # no longer taken: the cases are run
    pub.add_argument("--total", type=int, help=argparse.SUPPRESS)
    pub.add_argument("--measure", action="store_true", help=argparse.SUPPRESS)  # always, now
    pub.add_argument("--allow-drop", action="store_true", help="Publish though it passes fewer than the last release")
    renew = hv.add_parser("renew", help="Maintainer: sign the feed again when nothing new was published for a while")
    renew.add_argument("--feed", required=True)
    item = hv.add_parser(
        "share-lesson", help="Maintainer: measure a lesson on the case it fixes, then add it to the feed"
    )
    item.add_argument("text")
    item.add_argument("--case", required=True, help="The shared case it fixes (fails without it, passes with it)")
    item.add_argument("--about", default="")
    item.add_argument("--id", default="", help="Its id (default: from its text); an id in the feed gets a new version")
    item.add_argument("--feed", required=True, help="The feed folder")
    item.add_argument("--tries", type=int, default=5, help="Asks of the case each way (3 at least)")
    revoke = hv.add_parser("revoke", help="Maintainer: withdraw a shared item from every install")
    revoke.add_argument("id")
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--feed", required=True)
    revoke.add_argument("--shipped", action="store_true", help="The id is a lesson a release shipped, not the feed's")
    rev = hv.add_parser("review", help="Maintainer: check proposal files (or a folder of them)")
    rev.add_argument("paths", nargs="+")
    acc = hv.add_parser("accept", help="Maintainer: take one proposal (case, lesson, or fix as a patch)")
    acc.add_argument("path")


def scan_lesson(root: Path, text: str, about: str) -> list[str]:
    """What in a lesson's words looks personal, by this install's own values (the release build's scanner)."""
    from octavia.release import scan

    found = scan.scan_text("lesson", f"{text}\n{about}", scan._compile(scan.needles(root)))
    return list(dict.fromkeys(f.what for f in found))


def run(config: AppConfig, args: argparse.Namespace) -> int:
    from octavia import edition
    from octavia.hive import channel, proposals, review, signing

    root = config.root
    h = config.hive
    command = args.hive_command
    version = channel.current_version(root)
    try:
        if command == "status":
            feed = channel.read_trusted(root, h.feed, h.public_key)
            print(channel.status(feed.releases, version))
            in_force = sum(1 for i in feed.items if i.status == channel.ACTIVE)
            print(f"{in_force} shared item(s) in force (`ether items` lists them); feed serial {feed.serial}.")
        elif command == "items":
            from octavia.hive import shared

            feed = channel.read_trusted(root, h.feed, h.public_key)
            channel.sync_items(root, feed)
            print(channel.items_status(feed.items, shared.dropped(root)))
        elif command == "drop":
            from octavia.hive import shared

            changed = shared.set_dropped(root, args.id, not args.undo)
            word = "back in force" if args.undo else "left out"
            print(f"{args.id} is {word} here." if changed else f"{args.id} was already {word} here.")
        elif command == "rollback":
            back = channel.rollback(root, public=edition.PUBLIC)
            print(f"Back on {back or 'the version before'}. Restart her.")
        elif command == "share-lesson":
            import asyncio
            import hashlib

            from octavia.hive import measure

            found = scan_lesson(root, args.text, args.about)
            if found:
                raise ValueError("the lesson holds something personal: " + "; ".join(found))
            if len(" ".join(args.text.split())) > channel.MAX_ITEM_CHARS:
                raise ValueError(f"a shared lesson is a sentence or two ({channel.MAX_ITEM_CHARS} characters at most)")
            effect = asyncio.run(measure.lesson_effect(root, args.text, args.about, args.case, tries=args.tries))
            print(f"Its case {effect['case']}: passed {effect['without']} without it, {effect['with']} with it.")
            if effect.get("refused"):
                raise ValueError(f"not shared: {effect['refused']}")
            effect.pop("refused", None)
            item_id = args.id or "lesson-" + hashlib.sha256(" ".join(args.text.split()).encode()).hexdigest()[:8]
            item = channel.put_item(
                Path(args.feed),
                root / signing.KEY_FILE,
                channel.Item(item_id, "lesson", args.text, args.about, effect=effect),
            )
            print(f"Added {item.id} v{item.version} to {args.feed} and signed it. Putting the folder online is yours.")
        elif command == "revoke":
            channel.revoke_item(Path(args.feed), root / signing.KEY_FILE, args.id, args.reason, shipped=args.shipped)
            print(f"{args.id} is revoked in {args.feed}: every install drops it at its next look, once it's online.")
        elif command == "renew":
            channel.renew(Path(args.feed), root / signing.KEY_FILE)
            print(f"Signed {args.feed} again. Nothing was uploaded: putting the folder online is yours.")
        elif command == "update":
            release = channel.newer(channel.read_trusted(root, h.feed, h.public_key).releases, version)
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
            import asyncio

            from octavia.hive import measure

            # The shared cases are run here, on this code: a pass rate is never typed.
            measured = asyncio.run(measure.shared_total(root))
            passed, total = measured[0], measured[2]
            print(f"The shared cases: {passed} of {total} passed.")
            releases = channel._raw_channel(Path(args.feed))["releases"]
            last = next((r["shared_eval"] for r in reversed(releases) if r.get("shared_eval")), None)
            if why := measure.publishable(measured, last, args.allow_drop):
                raise ValueError(f"not published: {why}")
            r = channel.publish(Path(args.feed), archive, root / signing.KEY_FILE, name, args.notes, passed, total)
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
