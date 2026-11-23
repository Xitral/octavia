"""`octavia routines ...`: what she learned by watching, and the user's say over it. Works while the assistant runs
(the databases are in WAL mode). ``look`` reads the records and rewrites the list; ``forget`` and ``reset`` delete."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octavia.config import AppConfig


def add_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    routines = sub.add_parser("routines", help="The routines she learned by watching (see, look again, forget)")
    rc = routines.add_subparsers(dest="routines_command")
    rc.add_parser("list", help="What she learned, with the evidence (the default)")
    rc.add_parser("look", help="Look through the records for routines now, and show them")
    forget = rc.add_parser("forget", help="Never offer one routine again")
    forget.add_argument("id", type=int, help="its number from the list")
    rc.add_parser("offers-off", help="Stop offering routines (she keeps learning them)")
    rc.add_parser("offers-on", help="Offer routines again")
    reset = rc.add_parser("reset", help="Delete every routine she learned")
    reset.add_argument("--yes", action="store_true", help="really delete")


def run_command(config: AppConfig, args: argparse.Namespace) -> int:
    from octavia.routines import reports
    from octavia.routines.wiring import create_learner

    learner = create_learner(config)
    learner.store.open()
    try:
        command = args.routines_command or "list"
        if command == "look" and not config.routines.enabled:
            print("Learning by watching is off (routines.enabled in the settings): nothing to look for.")
            return 1
        if command == "look":
            learned, watching = learner.mine_now()
            print(f"Looked: {learned} learned, {watching} watching.")
        elif command == "forget":
            described = learner.forget(args.id)
            if described is None:
                print(f"There's no routine #{args.id}.")
                return 1
            print(f"Forgotten: she won't offer it again (you {described}).")
            return 0
        elif command in ("offers-on", "offers-off"):
            learner.set_offers(command == "offers-on")
            print(f"Offers {'on' if command == 'offers-on' else 'off'} (restart not needed).")
            return 0
        elif command == "reset":
            if not args.yes:
                print("This deletes every routine she learned (and which ones you turned down). Add --yes to do it.")
                return 1
            print(f"Deleted {learner.store.forget_all()} routines.")
            return 0
        print(reports.listing(learner))
        return 0
    finally:
        learner.store.close()
