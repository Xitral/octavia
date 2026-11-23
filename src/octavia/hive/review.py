"""The maintainer's side of the hivemind: looking at proposals from other installs and taking the good ones.

``review`` reads proposal files (from the inbox folder, or files sent some other way) and says, for each, what it is
and anything wrong with it: personal things the sender's scrub missed (checked again with this install's values
and the secret shapes), a fix that touches what it may not or doesn't apply to this code, a case that doesn't read.

``accept`` takes one:

* a **case** goes into ``evolve/shared_cases.yaml``, so it's run on every build from then on, this install's too;
* a **lesson** goes into ``hive/shared_lessons.yaml``, which every install's prompt gets;
* a **fix** is checked to apply (``git apply --check``) and saved as a patch, with the commands that put it on a
  branch of its own: from there the usual tests and eval gate decide, as for any change.

Accepted cases and lessons are committed like any change, so they reach the public build and, through the feed,
every install.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from octavia.hive import proposals
from octavia.hive.proposals import Proposal

ACCEPTED = Path("data") / "hive" / "accepted"


@dataclass
class Reviewed:
    path: Path
    proposal: Proposal | None
    problems: list[str]

    def summary(self) -> str:
        if self.proposal is None:
            return f"{self.path.name}: unreadable ({'; '.join(self.problems)})"
        p = self.proposal
        head = f'{self.path.name}: {p.kind} "{p.title}" (made on {p.version or "an unknown version"})'
        return head + ("\n    problems: " + "; ".join(self.problems) if self.problems else "\n    looks fine")


def _read(path: Path) -> Proposal | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        proposal = Proposal.from_dict(raw)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return proposal if proposal.kind in ("fix", "case", "lesson") and isinstance(proposal.body, dict) else None


def _case_problems(proposal: Proposal) -> list[str]:
    from octavia.evolve import evals

    try:
        evals._case({**proposal.body, "id": f"{evals.SHARED_PREFIX}{proposal.id}"})
    except (KeyError, TypeError, ValueError) as exc:
        return [f"the case doesn't read: {exc}"]
    return []


def _fix_problems(root: Path, proposal: Proposal) -> list[str]:
    diff = str(proposal.body.get("diff", ""))
    if not diff.strip():
        return ["the fix has no diff"]
    done = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=root, input=diff, capture_output=True, text=True, timeout=60, check=False
    )
    return [] if done.returncode == 0 else [f"doesn't apply to this code: {done.stderr.strip()[:300]}"]


def review(root: Path, paths: list[Path]) -> list[Reviewed]:
    files: list[Path] = []
    for path in paths:
        files += sorted(path.glob("*.json")) if path.is_dir() else [path]
    out = []
    for path in files:
        proposal = _read(path)
        if proposal is None:
            out.append(Reviewed(path, None, ["not a proposal file"]))
            continue
        problems = proposals.check(root, proposal)
        if proposal.kind == "case":
            problems += _case_problems(proposal)
        elif proposal.kind == "fix":
            problems += _fix_problems(root, proposal)
        elif not str(proposal.body.get("text", "")).strip():
            problems.append("the lesson is empty")
        out.append(Reviewed(path, proposal, problems))
    return out


def _append_yaml(path: Path, key: str, row: dict) -> None:
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    rows = data.get(key) or []
    rows.append(row)
    header = "".join(line + "\n" for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("#"))
    path.write_text(header + yaml.safe_dump({key: rows}, sort_keys=False, allow_unicode=True), encoding="utf-8")


def accept(root: Path, path: Path) -> str:
    """Takes the proposal at ``path`` (it must pass ``review``); returns what was done. Raises ValueError."""
    [reviewed] = review(root, [path])
    if reviewed.proposal is None or reviewed.problems:
        raise ValueError(reviewed.summary())
    p = reviewed.proposal
    if p.kind == "case":
        from octavia.evolve import evals

        case_id = f"{evals.SHARED_PREFIX}{p.id}"
        existing = {c.id for c in evals.load_shared(evals.SHARED_CASES_FILE)}
        if case_id in existing:
            raise ValueError(f"{case_id} is already a shared case")
        _append_yaml(evals.SHARED_CASES_FILE, "cases", {"id": case_id, **p.body})
        return f"Added {case_id} to the shared eval cases ({evals.SHARED_CASES_FILE.name}): commit it to ship it."
    if p.kind == "lesson":
        from octavia.hive.shared import LESSONS_FILE

        _append_yaml(LESSONS_FILE, "lessons", {"text": p.body["text"], "about": p.body.get("about", ""), "from": p.id})
        return f"Added the lesson to {LESSONS_FILE.name}: commit it to ship it."
    folder = root / ACCEPTED
    folder.mkdir(parents=True, exist_ok=True)
    patch = folder / f"{p.id}.patch"
    patch.write_text(str(p.body["diff"]), encoding="utf-8")
    (folder / f"{p.id}.md").write_text(f"# {p.title}\n\n{p.body.get('why', '')}\n", encoding="utf-8")
    return (
        f"The fix applies. Saved as {patch.relative_to(root).as_posix()}. Put it on its own branch and let the tests "
        f"and the eval gate judge it:\n  git switch -c hive/{p.id}\n  git apply {patch.relative_to(root).as_posix()}"
    )
