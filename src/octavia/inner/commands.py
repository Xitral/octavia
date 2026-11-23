"""`octavia inner ...`: everything she wrote in her journal, and exactly what she'd be given to think about tonight.

It works while the assistant runs (the journal is in WAL mode) and only reads, except ``forget`` and ``reset``.
Reflecting itself happens in the voice assistant (nightly, or "think it over").
"""

from __future__ import annotations

import argparse
import time
from typing import TYPE_CHECKING

from octavia.inner import reports

if TYPE_CHECKING:
    from octavia.config import AppConfig


async def run_command(config: AppConfig, args: argparse.Namespace) -> int:
    from octavia.inner.wiring import create_inner_life

    command = args.inner_command
    db = futures = None
    conversations = memory = None
    if command == "digest":
        if config.memory.enabled:
            from octavia.memory.sqlite_store import Database, SqliteConversationStore, SqliteLongTermMemory

            db = Database(config.resolve_path(config.memory.database))
            conversations, memory = SqliteConversationStore(db), SqliteLongTermMemory(db)
        if config.futures.enabled:
            from octavia.futures.engine import FuturesService

            futures = FuturesService(config)
    inner = create_inner_life(config, conversations=conversations, memory=memory, futures=futures)
    try:
        if command == "reset":
            if not args.yes:
                print("This deletes her whole journal (thoughts, opinions, entries). Add --yes to do it.")
                return 1
            print(f"Deleted {inner.forget('all')} items.")
            return 0
        if command == "forget":
            try:
                removed = (
                    inner.forget("thought", thought_id=args.id)
                    if args.id
                    else inner.forget("topic", topic=" ".join(args.topic))
                )
            except ValueError as exc:
                print(exc)
                return 1
            print(f"Deleted {removed} items.")
            return 0
        if command == "digest":
            print(await _digest(inner))
            return 0
        if command == "undo-note":
            note = inner.undo_self_note(args.id)
            if note is None:
                print(f"Self-note #{args.id} isn't one of her current ones.")
                return 1
            print(f"Took back self-note #{args.id}." + (f" #{note.id} is back." if note.id != args.id else ""))
            return 0
        render = {
            "status": reports.status,
            "thoughts": reports.thoughts,
            "journal": reports.journal,
            "opinions": reports.opinions,
            "history": reports.history,
            "experiences": reports.experiences,
            "self": reports.self_notes,
            "lookback": reports.looking_back,
            "dreams": reports.dreams,
        }[command]
        print(render(inner))
        return 0
    finally:
        inner.store.close()
        if futures is not None:
            futures.store.close()
        if db is not None:
            db.close()


async def _digest(inner: object) -> str:
    """Tonight's material, exactly as the reflection would get it (nothing runs, nothing is kept)."""
    from octavia.inner import digest
    from octavia.inner.service import FIRST_NIGHT_S, InnerLife

    assert isinstance(inner, InnerLife)
    now = time.time()
    last = inner.store.last_reflection("done")
    since = last.started_at if last is not None else now - FIRST_NIGHT_S
    material = await digest.build(
        inner.sources,
        inner.store,
        since=since,
        now=now,
        off_limits=await inner.off_limits(),
        budget=inner.settings.digest_chars,
    )
    return f"{material.text}\n\n({material.chars} characters, {material.turns} conversation lines)"


def add_parser(sub: argparse._SubParsersAction) -> None:
    inner = sub.add_parser("inner", help="Her inner life: her journal, thoughts and opinions (docs/inner-life.md)")
    cmds = inner.add_subparsers(dest="inner_command", required=True)
    cmds.add_parser("status", help="Her settings, how many thoughts, and her recent reflections")
    cmds.add_parser("thoughts", help="What's on her mind (the same text 'what have you been thinking about?' gets)")
    cmds.add_parser("journal", help="Her journal entries")
    cmds.add_parser("opinions", help="Her opinions, and what they were before")
    cmds.add_parser("history", help="Every thought and what became of it")
    cmds.add_parser("digest", help="Exactly what she'd be given to think about tonight (runs nothing)")
    cmds.add_parser("experiences", help="What surprised her, what she got wrong, enjoyed and learned")
    cmds.add_parser("self", help="Her own self-notes, opinions she's leaning away from, and her undertone")
    cmds.add_parser("lookback", help="Her monthly look backs on who she was")
    cmds.add_parser("dreams", help="Her dreams (made up)")
    undo = cmds.add_parser("undo-note", help="Take back one of her self-notes (and put back the one it rewrote)")
    undo.add_argument("id", type=int)
    forget = cmds.add_parser("forget", help="Delete one thought (--id) or everything about a topic")
    forget.add_argument("--id", type=int)
    forget.add_argument("topic", nargs="*")
    reset = cmds.add_parser("reset", help="Delete her whole journal (her settings stay)")
    reset.add_argument("--yes", action="store_true", help="Really")
