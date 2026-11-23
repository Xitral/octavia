"""What of the user's money is never kept, which bank pages may be read, and how all of it is deleted.

A build can't change this file (``evolve/policy.py``), so these hold whatever else changes:

* **Numbers.** Account, card and routing numbers never reach the store: any run of 7 or more digits in a description
  or memo is cut to its last 4 (``mask``), and an account is named by its last 4 only (``last4``).
* **Pages.** The bank browser opens only pages on ``READ_HOSTS`` (``may_read``), and a page it ends up on anywhere
  else (a redirect) is never read. It has no code that clicks, types or presses keys (a test reads ``reader.py``'s
  source for it: ``FORBIDDEN_PAGE_CALLS``).
* **Purge.** ``purge`` deletes everything under the data folder: the database, the imported statements, the bank
  browser's profile (its sign-ins), the state files. Nothing is kept anywhere else.
* **Her tools.** ``is_finance_tool`` names them, so the parts of her that send old turns to Claude again (caught
  mistakes, replays) leave money turns out.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

DATA_DIR = "data/finance"
TOOL_PREFIX = "finance_"

# Hosts the bank browser may read (a subdomain matches too). Read-only pages of the user's own accounts.
READ_HOSTS: frozenset[str] = frozenset({"cash.app", "usaa.com", "mohela.studentaid.gov", "mohela.com"})

# What reader.py must never call on a page: anything that acts on it.
FORBIDDEN_PAGE_CALLS: frozenset[str] = frozenset(
    {"click", "type", "press", "element", "around", "evaluate", "back", "scroll"}
)

_LONG_NUMBER = re.compile(r"(?<![\d$])(?:\d[ -]?){6,}\d(?!\d)")  # "acct 123456789." and "ref.123456789" too

# Marks a turn of hers about money (the heads-ups add it to the note she gets): replays and eval cases leave such
# conversations out (``evolve/replay.py``), and its words ask her to keep it private.
MONEY_MARK = "Money is private"
MONEY_NOTE = (
    f" {MONEY_MARK}: bring it up only when the user is alone at the PC, never on a call or with others listening, "
    "and never read out more amounts than they need."
)


def mask(text: str | None) -> str:
    """``text`` with every run of 7+ digits (spaces or dashes inside count) cut to its last 4: ``…1234``."""
    if not text:
        return ""

    def cut(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return "…" + digits[-4:]

    return _LONG_NUMBER.sub(cut, str(text))


def last4(number: str | None) -> str:
    """The last 4 digits of an account number ("" when it has fewer than 4)."""
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-4:] if len(digits) >= 4 else ""


def _host_ok(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in READ_HOSTS)


def may_read(url: str) -> bool:
    """May the bank browser read this page? https, on one of ``READ_HOSTS``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and _host_ok(parts.hostname or "")


def is_finance_tool(name: str) -> bool:
    return str(name).startswith(TOOL_PREFIX)


def purge(root: Path) -> list[str]:
    """Delete all of it: everything under ``root``/data/finance. Returns what was there (top-level names)."""
    folder = Path(root) / DATA_DIR
    if not folder.exists():
        return []
    gone = sorted(p.name for p in folder.iterdir())
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():  # a file held open (Windows): try each again, then say what's left
        for path in sorted(folder.rglob("*"), reverse=True):
            try:
                path.unlink() if path.is_file() else path.rmdir()
            except OSError:
                pass
        if folder.exists() and any(folder.rglob("*")):
            raise OSError(f"Some of it is still in use: {', '.join(p.name for p in folder.iterdir())}")
        folder.rmdir()
    return gone
