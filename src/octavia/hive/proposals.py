"""Proposals: what this install learned, made ready to send upstream, scrubbed first.

Three kinds, all things that make the next version more capable rather than more full of memories:

* ``fix``: a change her self-improvement built, passed its gate and installed (its diff, title and why).
* ``case``: a request she got wrong that became a graded eval case (``data/eval/caught.json``): the failure itself,
  so every later fix has to handle it.
* ``lesson``: a short text rule worth everyone having (a note on how an app or site works, a habit that tested well).

Scrubbing: every proposal is checked as text with the leak scanner (``release/scan.py``) using **this install's**
personal values (its .env, its settings, its account names, its private words), and a fix may only touch code
(``ALLOWED_PATHS``). Anything that still finds something is held in the outbox with the reasons, never sent. The
user's own words in a case are a risk of their own: a case is only proposed from a caught mistake the user kept
(``kept``), and its words go through the same check.

The outbox is ``data/hive/outbox``; ``send`` posts the ready ones to ``hive.inbox`` (an https address) or copies them
into it (a folder), and nothing is sent while ``hive.share`` is off.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from octavia.release import scan

OUTBOX = Path("data") / "hive" / "outbox"
PROPOSED = Path("data") / "hive" / "proposed.json"  # keys already made into proposals, so none is made twice
ALLOWED_PATHS = ("src/octavia/", "avatar/", "personality/system.md", "personality/quick_replies.yaml", "tests/", "docs/")
NEVER_PATHS = (
    "src/octavia/release/",
    "src/octavia/hive/",
    "src/octavia/edition.py",
)  # the trust path stays the maintainer's
MAX_DIFF_BYTES = 400_000

Kind = Literal["fix", "case", "lesson"]


@dataclass
class Proposal:
    kind: Kind
    title: str
    body: dict[str, Any]
    key: str  # what it came from (an improvement's id, a case's number...), for not proposing it twice
    version: str = ""  # the release it was made on
    made_at: float = 0.0
    status: Literal["ready", "held", "sent"] = "ready"
    held_because: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return hashlib.sha256(f"{self.kind}:{self.key}:{self.made_at}".encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(asdict(self) | {"id": self.id}, indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Proposal:
        fields = {
            k: raw[k]
            for k in ("kind", "title", "body", "key", "version", "made_at", "status", "held_because")
            if k in raw
        }
        return cls(**fields)


def _paths_in_diff(diff: str) -> list[str]:
    return sorted(set(re.findall(r"^(?:\+\+\+|---) (?:[ab]/)?(\S+)", diff, re.MULTILINE)) - {"/dev/null"})


def check(root: Path, proposal: Proposal) -> list[str]:
    """Why ``proposal`` can't be sent ([] = it can): personal or secret things in it, or a fix outside code."""
    why: list[str] = []
    compiled = scan._compile(scan.needles(root))
    text = json.dumps(proposal.body, ensure_ascii=False, indent=1) + "\n" + proposal.title
    for f in scan.scan_text("proposal", text, compiled):
        if f.what not in why:
            why.append(f.what)
    if proposal.kind == "fix":
        diff = str(proposal.body.get("diff", ""))
        if len(diff.encode()) > MAX_DIFF_BYTES:
            why.append("the change is too big to review")
        for path in _paths_in_diff(diff):
            if path.startswith(NEVER_PATHS) or not path.startswith(ALLOWED_PATHS):
                why.append(f"it changes {path}, which a shared fix may not")
    return why


def make(root: Path, kind: Kind, title: str, body: dict[str, Any], key: str, version: str = "") -> Proposal:
    """A checked proposal (``held`` when ``check`` finds anything), written to the outbox."""
    proposal = Proposal(kind=kind, title=title.strip()[:200], body=body, key=key, version=version, made_at=time.time())
    reasons = check(root, proposal)
    if reasons:
        proposal.status = "held"
        proposal.held_because = reasons
    folder = root / OUTBOX
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{proposal.id}.json").write_text(proposal.to_json(), encoding="utf-8")
    _remember(root, f"{kind}:{key}")
    return proposal


def _proposed(root: Path) -> set[str]:
    try:
        return set(json.loads((root / PROPOSED).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def _remember(root: Path, key: str) -> None:
    keys = _proposed(root) | {key}
    path = root / PROPOSED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(keys)), encoding="utf-8")


def outbox(root: Path) -> list[Proposal]:
    folder = root / OUTBOX
    out = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else ():
        try:
            out.append(Proposal.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (ValueError, TypeError, KeyError):
            continue
    return out


# ------------------------------------------------------------------------------------------------ gathering
def collect_cases(root: Path, version: str = "") -> list[Proposal]:
    """Graded caught mistakes the user kept, not proposed yet, as ``case`` proposals."""
    from octavia.evolve import evals

    try:
        rows = json.loads(evals._caught(root).read_text(encoding="utf-8")).get("cases") or []
    except (OSError, ValueError, AttributeError):
        return []
    done = _proposed(root)
    made = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("status") != "gating" or not row.get("kept"):
            continue
        key = str(row.get("n"))
        if f"case:{key}" in done:
            continue
        body = {k: row[k] for k in evals._CASE_KEYS if k in row and k not in ("id", "log", "documents")}
        made.append(make(root, "case", f"Got wrong: {str(row.get('text', ''))[:80]}", body, key, version))
    return made


def collect_fixes(root: Path, version: str = "") -> list[Proposal]:
    """Self-improvement builds installed here, not proposed yet, as ``fix`` proposals (their diff from git)."""
    import sqlite3

    db = root / "data" / "evolve" / "journal.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, details, base_commit, merge_commit FROM improvements WHERE status = 'installed'"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    done = _proposed(root)
    made = []
    for row in rows:
        key = str(row["id"])
        if f"fix:{key}" in done or not row["base_commit"] or not row["merge_commit"]:
            continue
        diff = subprocess.run(
            ["git", "diff", f"{row['base_commit']}..{row['merge_commit']}", "--", "."],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
        ).stdout  # fmt: skip
        if not diff.strip():
            continue
        body = {"why": str(row["details"] or "")[:4000], "diff": diff}
        made.append(make(root, "fix", str(row["title"]), body, key, version))
    return made


def propose_lesson(root: Path, text: str, about: str = "", version: str = "") -> Proposal:
    key = hashlib.sha256(text.strip().encode()).hexdigest()[:12]
    return make(
        root, "lesson", text.strip()[:80], {"text": text.strip()[:2000], "about": about.strip()[:200]}, key, version
    )


# ------------------------------------------------------------------------------------------------ sending
def send(root: Path, inbox: str, share: str) -> tuple[int, str]:
    """Sends the ready proposals; returns (how many, what happened)."""
    if share != "on":
        return 0, "Sharing is off (hive.share), so nothing was sent."
    if not inbox:
        return 0, f"No inbox is set (hive.inbox): the proposals wait in {OUTBOX.as_posix()}."
    sent = 0
    for proposal in outbox(root):
        if proposal.status != "ready":
            continue
        if check(root, proposal):  # checked again: the user's values may have changed since
            continue
        payload = proposal.to_json()
        if inbox.startswith("https://"):
            import httpx

            from octavia.net import tls_context

            r = httpx.post(inbox, content=payload.encode("utf-8"), headers={"Content-Type": "application/json"},
                           timeout=60, verify=tls_context())  # fmt: skip
            if r.status_code >= 300:
                return sent, f"The inbox answered HTTP {r.status_code}; the rest wait in the outbox."
        else:
            folder = Path(inbox)
            if not folder.is_dir():
                return sent, f"The inbox folder {inbox} isn't there."
            (folder / f"{proposal.id}.json").write_text(payload, encoding="utf-8")
        proposal.status = "sent"
        (root / OUTBOX / f"{proposal.id}.json").write_text(proposal.to_json(), encoding="utf-8")
        sent += 1
    return sent, f"Sent {sent} proposal(s)."


def clear_held(root: Path) -> int:
    """Removes the held proposals (the user looked and doesn't want them)."""
    gone = 0
    for path in (root / OUTBOX).glob("*.json") if (root / OUTBOX).exists() else ():
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("status") == "held":
                path.unlink()
                gone += 1
        except (OSError, ValueError):
            continue
    return gone
