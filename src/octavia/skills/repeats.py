"""The manual sequences her tasks keep repeating: counted across tasks, with no Claude usage, so she can offer to save
one as a skill.

After each screen or web task that went through (done or unfinished, never stopped or failed), the steps it took
itself are written down as a list of the calls (``record``), in a form fit to keep: what was typed or put on the
clipboard, and the files it opened or looked for, become ``{text}`` and ``{file}``; everything else is checked with
``futures.privacy.redact``: a value it would change, or one too long to keep whole, breaks the list there, as does a
step that didn't go through or was somewhere private (a bank, a password manager, a private window). The steps a
skill ran aren't the task's own. The last ``KEEP`` tasks are kept.

``find`` looks for the longest run of at least ``min_steps`` calls, with at least two that act (not just look), found
in at least ``min_tasks`` of them in the same app or site, that isn't already a skill or a proposal and that the user
hasn't turned down lately, nor part of one. ``to_skill`` turns it into a proposed skill, each ``{text}`` and ``{file}`` an input.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from octavia.futures.privacy import redact
from octavia.skills.runner import LOOKS, Call
from octavia.skills.skill import Input, Skill, Step

log = logging.getLogger(__name__)

KEEP = 60  # tasks kept
MAX_LEN = 12  # the longest run looked for
MIN_TASKS = 3
MIN_STEPS = 4
VALUE_CHARS = 60
# What was typed or put on the clipboard, and files opened: never kept, only that something was.
TEXT_TOOLS = {
    "type_text": ("text",),
    "web_type": ("value",),
    "copy_to_clipboard": ("text",),
}  # web_type's text: the field
FILE_TOOLS = {"open_file": ("path", "number"), "find_files": ("query", "where")}
# Not part of a sequence: they say nothing about how the app or the site works.
SKIP = frozenset({"get_current_time", "recall", "read_about_me", "list_running_apps", "read_clipboard"})
_SLUG = re.compile(r"[^a-z0-9]+")


def token(call: Call) -> str | None:
    """The call as kept: a JSON list of the tool and its arguments; None when it isn't kept (``SKIP``), "" when it
    can't be kept as it was (a long or secret-looking value), which cuts the run there."""
    if call.tool in SKIP:
        return None
    args: dict[str, Any] = {}
    for key, value in sorted(call.args.items()):
        if call.tool in TEXT_TOOLS and key in TEXT_TOOLS[call.tool]:
            args[key] = "{text}"
        elif call.tool in FILE_TOOLS and key in FILE_TOOLS[call.tool]:
            args[key] = "{file}"
        elif isinstance(value, bool) or isinstance(value, (int, float)):
            args[key] = value
        elif isinstance(value, str):
            plain = " ".join(value.split())
            if len(plain) > VALUE_CHARS or redact(plain, VALUE_CHARS) != plain:
                return ""
            args[key] = plain
        # anything else (lists, objects) says too much or too little: left out
    return json.dumps([call.tool, args], sort_keys=True, ensure_ascii=False)


def runs_of(calls: Iterable[Call]) -> list[tuple[str, list[str]]]:
    """The task's calls cut into runs that can be repeated: (the place, its tokens), cut at every step that didn't
    go through, went somewhere private, or moved to another app or site."""
    found: list[tuple[str, list[str]]] = []
    place, current = "", []
    for call in calls:
        kept = token(call)
        if kept is None:
            continue
        cut = not kept or not call.ok or call.place is None
        if cut or (current and call.place and call.place != place):
            if len(current) >= 2:
                found.append((place, current))
            current, place = [], ""
            if cut:
                continue
        place = place or (call.place or "")
        current.append(kept)
    if len(current) >= 2:
        found.append((place, current))
    return [(p, toks) for p, toks in found if p]


def signature(tokens: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(tokens).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Repeat:
    kind: str
    place: str
    tokens: tuple[str, ...]
    tasks: int  # how many of the tasks kept have it

    @property
    def signature(self) -> str:
        return signature(self.tokens)


class Repeats:
    """The record of the tasks' own steps (a JSON-lines file; in memory only without one)."""

    def __init__(self, path: Path | None, *, clock: Any = time.time) -> None:
        self.path = path
        self._clock = clock
        self._records: list[dict[str, Any]] | None = None if path is not None else []

    def records(self) -> list[dict[str, Any]]:
        if self._records is None:
            self._records = []
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines() if self.path else []
            except FileNotFoundError:
                lines = []
            except OSError:
                log.warning("Couldn't read the task steps in %s", self.path, exc_info=True)
                lines = []
            for line in lines[-KEEP:]:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and isinstance(item.get("runs"), list):
                    self._records.append(item)
        return self._records

    def record(self, kind: str, calls: Iterable[Call]) -> bool:
        """Write down a finished task's own steps; False when it had nothing worth keeping."""
        runs = runs_of(calls)
        if not runs:
            return False
        records = self.records()
        records.append({"at": self._clock(), "kind": kind, "runs": [{"place": p, "steps": t} for p, t in runs]})
        del records[:-KEEP]
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(".tmp")
                temp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
                temp.replace(self.path)
            except OSError:
                log.warning("Couldn't write the task steps to %s", self.path, exc_info=True)
        return True

    def find(
        self,
        kind: str,
        *,
        min_tasks: int = MIN_TASKS,
        min_steps: int = MIN_STEPS,
        skip: Iterable[str] = (),
    ) -> Repeat | None:
        """The longest run done in at least ``min_tasks`` tasks of ``kind`` in one place (see the module), or None.
        ``skip``: the signatures of skills and proposals there are, and of repeats turned down."""
        skip = set(skip)
        counts: Counter[tuple[str, tuple[str, ...]]] = Counter()
        for record in self.records():
            if record.get("kind") != kind:
                continue
            seen: set[tuple[str, tuple[str, ...]]] = set()
            for run in record["runs"]:
                if not isinstance(run, dict):
                    continue
                place, steps = str(run.get("place", "")), run.get("steps")
                if not place or not isinstance(steps, list):
                    continue
                steps = [s for s in steps if isinstance(s, str)]
                for length in range(min_steps, min(MAX_LEN, len(steps)) + 1):
                    for start in range(len(steps) - length + 1):
                        window = tuple(steps[start : start + length])
                        if _worth(window):
                            seen.add((place, window))
            counts.update(seen)
        # A run already a skill, a proposal or turned down covers every shorter run inside it too.
        covered = [(place, window) for (place, window) in counts if signature(window) in skip]
        best: Repeat | None = None
        for (place, window), tasks in counts.items():
            if tasks < min_tasks or any(p == place and _inside(window, w) for p, w in covered):
                continue
            candidate = Repeat(kind, place, window, tasks)
            if best is None or (len(window), tasks) > (len(best.tokens), best.tasks):
                best = candidate
        return best


def _inside(window: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    """Whether ``window`` is ``longer`` or a run of steps within it."""
    n = len(window)
    return any(longer[i : i + n] == window for i in range(len(longer) - n + 1))


def _tool(tok: str) -> str:
    try:
        return str(json.loads(tok)[0])
    except (ValueError, IndexError, TypeError):
        return ""


def _worth(window: tuple[str, ...]) -> bool:
    """A run worth a skill: it starts and ends with a step that acts, and has at least two."""
    acts = [tok for tok in window if _tool(tok) not in LOOKS]
    return len(acts) >= 2 and _tool(window[0]) not in LOOKS and _tool(window[-1]) not in LOOKS


def to_skill(repeat: Repeat) -> Skill:
    """The repeat as a proposed skill: each ``{text}`` and ``{file}`` its own input, named after the place and its
    first steps."""
    steps: list[Step] = []
    inputs: list[Input] = []
    for tok in repeat.tokens:
        tool, args = json.loads(tok)
        filled: dict[str, Any] = {}
        for key, value in args.items():
            if value in ("{text}", "{file}"):
                kind = "text" if value == "{text}" else "file"
                name = f"{kind}{sum(1 for i in inputs if i.name.startswith(kind)) + 1}"
                about = "what to type" if kind == "text" else "the file"
                inputs.append(Input(name, f"{about} at step {len(steps) + 1}"))
                filled[key] = "{" + name + "}"
            else:
                filled[key] = value
        steps.append(Step(tool=tool, args=filled))
    words = [s.words() for s in steps if s.tool not in LOOKS][:3]
    place_slug = _SLUG.sub("_", repeat.place.casefold()).strip("_")[:16] or "task"
    first = _SLUG.sub("_", " ".join(words[:2]).casefold()).strip("_")[:20]
    name = f"{place_slug}_{first}".strip("_")[:36]
    if not name[:1].isalpha():
        name = f"s_{name}"[:36]
    return Skill(
        name=name,
        title=f"{len(steps)} steps in {repeat.place}"[:80],
        kind=repeat.kind,
        steps=tuple(steps),
        purpose=f"What I did by hand in {repeat.tasks} tasks: {'; '.join(words)}"[:300],
        place=repeat.place,
        inputs=tuple(inputs),
        state="proposed",
        origin="repeats",
        signature=repeat.signature,
        proposed_at=time.time(),
    )
